"""Fence native Codex tool execution for a claimed ordinary Matrix message."""
import json
import os
import sys

from task_recovery import RecoveryConflict
from worker_matrix_state import WorkerMatrix

COMMAND = "/opt/venv/bin/python -m agentteams_codex.tool_hook"


def require_hooks(client, workspace):
    reply = client.request("hooks/list", {"cwds": [str(workspace)]})
    active = {h["eventName"] for entry in reply["data"] if not entry["errors"]
              for h in entry["hooks"] if h["enabled"] and h["isManaged"] and h["trustStatus"] == "managed"
              and h["command"] == COMMAND}
    if "preToolUse" not in active:
        raise RecoveryConflict("Codex native Worker tool hooks are unavailable")


def checkpoint_tool(matrix, identity, hook):
    call = {"id": hook["tool_use_id"], "name": hook["tool_name"], "arguments": hook["tool_input"]}
    if hook["hook_event_name"] != "PreToolUse":
        raise RecoveryConflict("Unexpected Codex tool hook")
    matrix.message_begin_tools(identity, [call])


def confirm_native_tool(matrix, identity, item):
    """Only a terminal SDK item with a known successful outcome verifies the operation."""
    kind = item.get("type")
    if item.get("status") != "completed":
        return
    if kind == "commandExecution":
        if item.get("exitCode") != 0:
            return
    elif kind == "mcpToolCall":
        if item.get("error") or not isinstance(item.get("result"), dict) or item["result"].get("isError"):
            return
    elif kind == "dynamicToolCall":
        if item.get("success") is not True:
            return
    elif kind != "fileChange":
        return
    execution = matrix.message(identity["event_key"])["execution"]
    operation = (execution.get("checkpoint") or {}).get("operation") or {}
    if operation.get("state") != "started":
        return
    calls = json.loads(operation["evidence"])
    if len(calls) == 1 and calls[0]["id"] == item["id"]:
        matrix.message_finish_tools(identity, operation["id"], [{"id": item["id"], "content": item}])


def main():
    scoped = os.getenv("AGENTTEAMS_CODEX_MESSAGE_EXECUTION")
    if not scoped:
        return
    try:
        checkpoint_tool(WorkerMatrix("codex", device_delivery=True), json.loads(scoped), json.load(sys.stdin))
    except Exception as error:
        # The SDK treats exit 2 as a blocking hook. Never print tool arguments
        # or full Controller responses into the model or runtime logs.
        print(f"Worker tool checkpoint requires verification ({type(error).__name__})", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
