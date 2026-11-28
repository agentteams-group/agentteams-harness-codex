"""Bind a dedicated native Codex turn to the shared Task protocol."""
import importlib
import asyncio
import json
import os
from pathlib import Path
import sys


CONTEXT_ENV = "AGENTTEAMS_CODEX_TASK_CONTEXT"


class InactiveTaskPhase(ValueError):
    """Controller settled or superseded this native phase before model start."""


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
    context = {"role": role, "sourceEvent": event["event_id"], "sourceRoom": event["room_id"], "sources": []}
    notification = meta.get("teamharness_coordination")
    if notification:
        notification = dict(notification)
        if notification.get("id") and notification.get("kind") != "assistant_digest":
            admission = asyncio.run(common(plugin, "phase_recovery").notify(
                notification["id"], event["event_id"], "begin"))
            if admission.get("active") is not True:
                if admission.get("active") is False and any(
                    admission.get(reason) is True for reason in ("settled", "reused", "replaced", "failed")
                ):
                    raise InactiveTaskPhase("Controller retired this native Task phase")
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
    if not notification and os.getenv("AGENTTEAMS_CONTROLLER_URL"):
        context.update(common(plugin, "task_native").invoke({"operation": "room_context", "room": event["room_id"]}, call=call))
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
    if arguments.get("action") == "plan" and context.get("planning") and not str(arguments.get("overview") or "").strip():
        return {"content": [{"type": "text", "text": "请在 overview 中提供任务目标、要求、验收标准、分工和最终产物；保存规划后系统会先发布概要，再派单。"}], "isError": True,
                "_meta": {"agentteams.task": {"effect": "none", "phaseComplete": False}}}
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
    for line in sys.stdin:
        request = None
        try:
            request = json.loads(line)
            identity, method = request.get("id"), request.get("method")
            if identity is None:
                continue
            if method == "initialize":
                result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "teamharness-task", "version": "1"}}
            else:
                plugin = Path(os.environ["AGENTTEAMS_PLUGIN_DIR"])
                from .admission import load
                frame = load() if not os.getenv(CONTEXT_ENV) else None
                context = json.loads(os.environ[CONTEXT_ENV]) if os.getenv(CONTEXT_ENV) else (frame or {}).get("context")
                if not isinstance(context, dict) or not context.get("role"):
                    raise RuntimeError("Task context is unavailable; retry after runtime context is attached")
                schema = common(plugin, "task_cli").schema(context["role"])
                if context.get('planning'):
                    schema['description'] += " 规划时 overview 必填：简述任务目标、要求、验收标准、节点分工和最终产物。系统保存后先发概要、再派单。"
                    schema['inputSchema']['allOf'] = [{'if': {'properties': {'action': {'const': 'plan'}}, 'required': ['action']},
                                                     'then': {'required': ['overview']}}]
                server = importlib.import_module('mcp.server')
                coordination = any(context.get(key) for key in ('planning', 'review', 'completion'))
                other_tools = [] if coordination else [tool for tool in server.list_tools()
                    if tool['name'] not in {'task', 'taskflow', 'filesync', 'projectflow'}]
                if method == "tools/list":
                    result = {"tools": [{"name": "task", **schema}, *other_tools]}
                elif method == "tools/call" and request.get("params", {}).get("name") == "task":
                    result = task_response(request["params"].get("arguments", {}), context, plugin)
                elif method == 'tools/call' and request.get('params', {}).get('name') in {tool['name'] for tool in other_tools}:
                    result = server.call_tool(request['params']['name'], request['params'].get('arguments', {}))
                else:
                    raise ValueError("unsupported native Task request")
            response = {"jsonrpc": "2.0", "id": identity, "result": result}
        except (ValueError, KeyError, TypeError) as error:
            response = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                        "error": {"code": -32602, "message": str(error)}}
        except Exception as error:
            # Keep a failed request separate from the stdio server lifetime.
            # The next request reloads the runtime-owned context; no fallback
            # identity, model call or tool operation is admitted here.
            message = str(error) if isinstance(error, (RuntimeError, OSError)) else "Task request failed (" + type(error).__name__ + ")"
            response = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                        "error": {"code": -32000, "message": message}}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
