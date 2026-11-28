"""Fence native Codex tool execution for a claimed ordinary Matrix message."""
import json
import os
import sys

from task_recovery import RecoveryConflict
from worker_matrix_state import WorkerMatrix
from .task_protocol import native_task_metadata

COMMAND = "/opt/venv/bin/python -m agentteams_codex.tool_hook"


def require_hooks(client, workspace):
    reply = client.request("hooks/list", {"cwds": [str(workspace)]})
    active = {h["eventName"] for entry in reply["data"] if not entry["errors"]
              for h in entry["hooks"] if h["enabled"] and h["isManaged"] and h["trustStatus"] == "managed"
              and h["command"] == COMMAND}
    if not {"preToolUse", "postToolUse"}.issubset(active):
        raise RecoveryConflict("Codex native Worker tool hooks are unavailable")


def checkpoint_tool(matrix, identity, hook):
    call = {"id": hook["tool_use_id"], "name": hook["tool_name"], "arguments": hook["tool_input"]}
    if hook["hook_event_name"] != "PreToolUse":
        raise RecoveryConflict("Unexpected Codex tool hook")
    matrix.message_begin_tools(identity, [call])


def confirm_native_tool(matrix, identity, item):
    """Settle terminal native outcomes, including failures with possible partial effects."""
    kind = item.get("type")
    no_effect = native_task_metadata(item).get('effect') == 'none'
    if item.get("status") not in {"completed", "failed"}:
        return
    failed = False
    if kind == "commandExecution":
        if type(item.get("exitCode")) is not int:
            return
        failed = item["exitCode"] != 0
    elif kind == "mcpToolCall":
        if not no_effect and (item.get("status") != "completed" or item.get("error") or not isinstance(item.get("result"), dict)):
            return
        failed = no_effect or item["result"].get("isError") is True
    elif kind == "dynamicToolCall":
        if item.get("status") != "completed" or item.get("success") is not True:
            return
    elif kind != "fileChange" or item.get("status") != "completed":
        return
    execution = matrix.message(identity["event_key"])["execution"]
    operation = (execution.get("checkpoint") or {}).get("operation") or {}
    if operation.get("state") != "started":
        return
    calls = json.loads(operation["evidence"])
    if len(calls) == 1 and calls[0]["id"] == item["id"]:
        receipt = {"id": item["id"], "content": item}
        if failed:
            receipt["status"] = "error"
        matrix.message_finish_tools(identity, operation["id"], [receipt])


def check_phase_tool(context, hook):
    if any(context.get(key) for key in ("planning", "review", "completion")):
        if hook.get("tool_name") != "mcp__teamharness__task":
            raise RecoveryConflict("This Task phase permits only the public Task tool")


def scoped_tool(frame, hook):
    """Use private SDK registration and the common operation journal."""
    from task_recovery_rpc import execute
    from .admission import admit
    policy = execute({'action': 'tool_policy',
                      'registration': frame['registrations'].get(hook['tool_name'])})
    before = hook['hook_event_name'] == 'PreToolUse'
    if hook['hook_event_name'] not in {'PreToolUse', 'PostToolUse'}:
        raise RecoveryConflict('Unexpected native tool hook')
    if before:
        check_phase_tool(frame['context'], hook)
        admit(frame)
    if policy['mode'] == 'delegate':
        return
    recovery = frame.get('recovery')
    if not recovery:
        # Manager turns use canonical Task authority and native tool permissions;
        # they do not own the Worker execution journal.
        if frame['context'].get('role') == 'manager':
            return
        raise RecoveryConflict('Native tool requires a durable execution admission')
    request = {'identity': recovery['identity'], 'runtime': 'codex', 'device_delivery': True}
    prefix = '' if recovery['kind'] == 'task' else 'matrix.message.'
    if before:
        execute({**request, 'action': prefix + 'begin_tools',
                 'calls': [{'id': hook['tool_use_id'], 'name': hook['tool_name'],
                            'arguments': hook['tool_input']}]})
        return
    # A started operation blocks normal admission. Only the owned finish path
    # may inspect it after the SDK has returned an actual tool response.
    current = execute({**request, 'action': 'current' if recovery['kind'] == 'task'
                       else 'matrix.message.owned'})
    task = current['task'] if recovery['kind'] == 'task' else current
    operation = (task['execution'].get('checkpoint') or {}).get('operation') or {}
    calls = json.loads(operation.get('evidence', '[]')) if operation.get('state') == 'started' else []
    if len(calls) != 1 or calls[0]['id'] != hook['tool_use_id']:
        raise RecoveryConflict('Native tool response does not own the started operation')
    response = hook['tool_response']
    receipt = {'id': hook['tool_use_id'], 'content': response}
    # Native MCP PostToolUse exposes CallToolResult directly. User content
    # inside structuredContent is not the protocol's failure flag.
    if hook['tool_name'].startswith('mcp__') and isinstance(response, dict) and response.get('isError') is True:
        receipt['status'] = 'error'
    execute({**request, 'action': prefix + 'finish_tools', 'operation_id': operation['id'],
             'results': [receipt]})


def main():
    from .admission import load
    frame = load()
    scoped = os.getenv("AGENTTEAMS_CODEX_MESSAGE_EXECUTION")
    context = json.loads(os.getenv("AGENTTEAMS_CODEX_TASK_CONTEXT", "{}"))
    if not frame and not scoped and not context:
        return
    try:
        hook = json.load(sys.stdin)
        if frame:
            scoped_tool(frame, hook)
        else:
            check_phase_tool(context, hook)
            if scoped:
                checkpoint_tool(WorkerMatrix("codex", device_delivery=True), json.loads(scoped), hook)
    except Exception as error:
        if frame and hook.get("hook_event_name") == "PreToolUse":
            from .admission import ENV, locked, write
            from pathlib import Path
            from task_recovery import Recovery
            path = Path(os.environ[ENV])
            with locked(path):
                saved = json.loads(path.read_text())
                denied = saved.setdefault("denied_tools", {})
                denied[hook["tool_use_id"]] = {
                    "id": hook["tool_use_id"], "name": hook["tool_name"],
                    "arguments_sha256": Recovery.digest(hook["tool_input"]),
                    "error": type(error).__name__}
                write(path, saved)
        print(f"Worker tool checkpoint requires verification ({type(error).__name__})", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
