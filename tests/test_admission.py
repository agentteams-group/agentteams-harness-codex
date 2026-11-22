"""Exercise native-owned policy boundaries rather than Task business rules."""
import json
from unittest.mock import patch

import pytest

from agentteams_codex import admission, tool_hook
from task_recovery import RecoveryConflict


def frame():
    return {'context': {'sourceEvent': '$origin', 'role': 'worker'},
            'registrations': {'mcp__teamharness__task': admission.REGISTRATION},
            'recovery': {'kind': 'task', 'identity': {'task_id': 'actual'}}}


def hook(event='PreToolUse', name='mcp__teamharness__task'):
    return {'hook_event_name': event, 'tool_name': name,
            'tool_use_id': 'call-native', 'tool_input': {'registration': admission.REGISTRATION},
            'tool_response': {'error': 'ordinary returned error'}}


def test_only_private_factory_registration_delegates():
    from task_operation import tool_policy
    calls = []
    def execute(request):
        calls.append(request)
        return tool_policy(request.get('registration'))
    with patch('task_recovery_rpc.execute', side_effect=execute), patch.object(admission, 'admit') as gate:
        tool_hook.scoped_tool(frame(), hook())
        assert gate.call_count == 1
        assert [r['action'] for r in calls] == ['tool_policy']
        tool_hook.scoped_tool(frame(), hook(name='mcp__custom__task'))
    assert calls[-1]['action'] == 'begin_tools'
    assert calls[-2]['registration'] is None  # model input cannot forge registration


def test_unknown_admission_blocks_before_tool_start():
    from task_operation import tool_policy
    requests = []
    def execute(request):
        requests.append(request)
        return tool_policy(request.get('registration'))
    with patch('task_recovery_rpc.execute', side_effect=execute), patch.object(
            admission, 'admit', side_effect=RecoveryConflict('unknown')):
        with pytest.raises(RecoveryConflict):
            tool_hook.scoped_tool(frame(), hook(name='Bash'))
    assert [r['action'] for r in requests] == ['tool_policy']


def test_post_tool_finishes_exact_owned_started_even_returned_error():
    from task_operation import tool_policy
    requests = []
    def execute(request):
        requests.append(request)
        if request['action'] == 'tool_policy':
            return tool_policy(request.get('registration'))
        if request['action'] == 'current':
            return {'task': {'execution': {'checkpoint': {'operation': {
                'id': 'operation-owned', 'state': 'started',
                'evidence': json.dumps([{'id': 'call-native'}])}}}}}
    with patch('task_recovery_rpc.execute', side_effect=execute), patch.object(admission, 'admit') as gate:
        tool_hook.scoped_tool(frame(), hook('PostToolUse', 'Bash'))
    gate.assert_not_called()  # started fences inference, but the exact finish is permitted
    assert requests[-1]['action'] == 'finish_tools'
    assert requests[-1]['operation_id'] == 'operation-owned'
    assert requests[-1]['results'][0]['content'] == {'error': 'ordinary returned error'}


def test_wrong_tool_id_cannot_finish_started_operation():
    from task_operation import tool_policy
    def execute(request):
        if request['action'] == 'tool_policy':
            return tool_policy(request.get('registration'))
        return {'task': {'execution': {'checkpoint': {'operation': {
            'state': 'started', 'evidence': '[{"id":"other-call"}]'}}}}}
    with patch('task_recovery_rpc.execute', side_effect=execute):
        with pytest.raises(RecoveryConflict):
            tool_hook.scoped_tool(frame(), hook('PostToolUse', 'Bash'))
