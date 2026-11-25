"""Bind a dedicated native Codex turn to the shared Task protocol."""
import importlib
import asyncio
import json
import os
from pathlib import Path
import sys


CONTEXT_ENV = "AGENTTEAMS_CODEX_TASK_CONTEXT"


def common(plugin, name):
    root = str(Path(plugin).resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module("mcp.common." + name)


def prepare_turn(plugin, role, event, call=None):
    """Use only transport-owned identities and verified notification metadata."""
    receipt = common(plugin, "task_receipt")
    cli = common(plugin, "task_cli")
    call = call or cli.run
    meta = event.get("_task_protocol_meta") or {}
    context = {"role": role, "sourceEvent": event["event_id"], "sources": []}
    notification = meta.get("teamharness_coordination")
    if notification:
        notification = dict(notification)
        if notification.get("id") and notification.get("kind") != "assistant_digest":
            admission = asyncio.run(common(plugin, "phase_recovery").notify(
                notification["id"], event["event_id"], "begin"))
            if admission.get("active") is not True:
                raise ValueError("Task phase is not active for this native message")
            if notification.get("kind") == "handoff":
                notification["pending_task_ids"] = admission.get("pending_task_ids")
        prepared = common(plugin, "task_context").prepare_event(notification, call)
        if prepared.get("ok") is False:
            raise ValueError("Task preparation is unavailable; no native model was started")
        context.update(prepared)
        if meta.get("agent_matrix_user_id"):
            context["actor"] = meta["agent_matrix_user_id"]
    elif event["event_id"].startswith("$"):
        frames = event.get("matrix_input_sources")
        if frames is None:
            frames = [{"event_id": source, "room_id": event["room_id"], "sender_id": event["sender"]}
                      for source in event.get("session_source_event_ids", [event["event_id"]])]
        context["sources"] = receipt.creation_sources({"matrix_input_sources": frames})
    facts = []
    if context.get("fact"):
        facts.append(context["fact"] + "\n" + json.dumps(common(plugin, "task_context").model_context(context["context"]), ensure_ascii=False))
    if context["sources"]:
        facts.append("[Task input references]\n" + json.dumps([
            {key: source[key] for key in ("inputId", "eventId")} for source in context["sources"]]))
    output = (context.get("context") or {}).get("outputDirectory")
    # Directory validation and Task identity belong to common.prepare_event.
    return context, "\n\n".join(facts), Path(output) if output else None


def task_response(arguments, context, plugin):
    from .admission import load, admit, publish_context, phase_complete
    frame = load()
    if frame:
        admit(frame)
    try:
        outcome = common(plugin, "task_native").invoke({"operation": "task", "arguments": arguments,
                                                       "context": context})
    except ValueError as error:
        if getattr(error, "effect", None) != "none":
            raise
        outcome = {"result": {"ok": False, "error": "native_task_context_unavailable", "message": str(error), "effect": "none"},
                   "effect": "none", "phaseComplete": False}
    result = outcome.get("result", outcome)
    if outcome.get("contextUpdate"):
        context.update(outcome["contextUpdate"])
    # A validated terminal receipt closes this phase. Keep only its identity
    # and disposition in native tool history, not a second full Task snapshot.
    visible = result
    if outcome.get("phaseComplete") is True and outcome.get("receiptId"):
        task = result.get("task") or {}
        visible = {"task": {key: task[key] for key in ("id", "status", "submissionId", "completionId", "planId") if key in task},
                   "receiptId": outcome["receiptId"]}
        if result.get("notification"):
            visible["notification"] = result["notification"]
    content = [{"type": "text", "text": json.dumps(visible, ensure_ascii=False)}]
    if outcome.get("fact"):
        content.append({"type": "text", "text": outcome["fact"]})
    # A refresh remains fenced until a native User message is checkpointed.
    # Returning tool text must never silently clear this common guard.
    if outcome.get("requiresInputCheckpoint"):
        context["inputRefreshPending"] = True
        from .input_barrier import checkpoint
        checkpoint(outcome["refreshedInputs"], context)
        context["inputRefreshPending"] = False
    publish_context(context)
    phase_complete(outcome, context)
    metadata = {key: outcome[key] for key in
                ("refreshedInputs", "requiresInputCheckpoint", "phaseComplete", "receiptId") if key in outcome}
    metadata['effect'] = outcome.get('effect') or result.get('effect', 'unknown')
    return {"content": content, "isError": result.get("ok") is False,
            "_meta": {"agentteams.task": metadata}}


def native_task_metadata(item):
    if item.get('type') != 'mcpToolCall' or item.get('server') != 'teamharness' or item.get('tool') != 'task':
        return {}
    result = item.get('result') or {}
    metadata = (result.get('_meta') or {}).get('agentteams.task')
    return metadata if isinstance(metadata, dict) else {}


def delivery_context(plugin, role, room, middle):
    if role != "manager" or not os.getenv("AGENTTEAMS_CONTROLLER_URL"):
        return "", {}
    result = common(plugin, "task_native").invoke({"operation": "delivery_context", "room": room, "middle": middle})
    return "\n\n".join(result["facts"]), result["acknowledgements"]


def main():
    plugin = Path(os.environ["AGENTTEAMS_PLUGIN_DIR"])
    context = json.loads(os.environ[CONTEXT_ENV])
    schema = common(plugin, "task_cli").schema(context["role"])
    server = importlib.import_module('mcp.server')
    coordination = any(context.get(key) for key in ('planning', 'review', 'completion'))
    other_tools = [] if coordination else [tool for tool in server.list_tools()
        if tool['name'] not in {'task', 'taskflow', 'filesync', 'projectflow'}]
    for line in sys.stdin:
        try:
            request = json.loads(line)
            identity, method = request.get("id"), request.get("method")
            if identity is None:
                continue
            if method == "initialize":
                result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "teamharness-task", "version": "1"}}
            elif method == "tools/list":
                result = {"tools": [{"name": "task", **schema}, *other_tools]}
            elif method == "tools/call" and request.get("params", {}).get("name") == "task":
                result = task_response(request["params"].get("arguments", {}), context, plugin)
            elif method == 'tools/call' and request.get('params', {}).get('name') in {tool['name'] for tool in other_tools}:
                result = server.call_tool(request['params']['name'], request['params'].get('arguments', {}))
            else:
                raise ValueError("unsupported native Task request")
            response = {"jsonrpc": "2.0", "id": identity, "result": result}
        except (ValueError, KeyError, TypeError) as error:
            response = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(locals().get("request"), dict) else None,
                        "error": {"code": -32602, "message": str(error)}}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
