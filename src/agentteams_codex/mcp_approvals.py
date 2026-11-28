"""Translate SDK-marked MCP tool approvals to AT's current shared policy."""
from runtime_control import approval_mode, mcp_approval


def automatic_native_response(config, method, params, thread_id, turn_id):
    if (method in {'item/commandExecution/requestApproval', 'item/fileChange/requestApproval'}
            and params.get('threadId') == thread_id and params.get('turnId') == turn_id
            and approval_mode(config) == 'allow'):
        return {'decision': 'accept'}
    return None


def is_tool_approval(params, thread_id, turn_id):
    return (params.get('mode') == 'form'
            and (params.get('_meta') or {}).get('codex_approval_kind') == 'mcp_tool_call'
            and params.get('threadId') == thread_id
            and params.get('turnId') in (None, turn_id)
            and isinstance(params.get('serverName'), str))


def automatic_response(config, params, thread_id, turn_id):
    if is_tool_approval(params, thread_id, turn_id) and mcp_approval(config, params['serverName']) == 'allow':
        return {'action': 'accept', 'content': None}
    return None


def decision(approved):
    return {'action': 'accept' if approved else 'decline', 'content': None}
