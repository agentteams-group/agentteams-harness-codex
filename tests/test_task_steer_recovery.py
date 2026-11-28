import json
from pathlib import Path
from unittest.mock import Mock

from agentteams_codex.bridge import Bridge
from agentteams_codex.tool_progress import ToolProgress


def test_task_supplement_uses_core_authority():
    bridge = Bridge.__new__(Bridge)
    bridge.user_id = '@dev:test'
    bridge.config = {'member': {'matrixUserId': '@dev:test'}, 'team': {'name': 'alpha', 'leaderName': 'lead', 'members': [
        {'name': 'lead', 'role': 'team_leader', 'matrixUserId': '@lead:test'}]}}
    bridge.active = {'event': {'room_id': '!task:test', 'recovery': {'task_id': 'exec-one', 'team_id': 'alpha'}}}
    event = {'event_id': '$source', 'room_id': '!task:test', 'sender': '@lead:test', 'body': '@dev:test Use the updated requirements'}
    assert bridge.task_supplement(event)
    assert not bridge.task_supplement({**event, 'sender': '@other:test'})
    assert not bridge.task_supplement({**event, 'room_id': '!other:test'})
    bridge.active['event'].pop('recovery')
    assert not bridge.task_supplement(event)


def test_ended_progress_has_same_run_and_idempotent_terminal_marker():
    matrix = Mock()
    progress = ToolProgress(matrix, {'event_id': '$original', 'room_id': '!task:test'}, 'codex:task:team:exec:1')
    progress.finish('failed')
    room, content, transaction = matrix.send_content.call_args.args
    assert room == '!task:test'
    assert content['org.agentteams.run']['run_id'] == progress.run_id
    assert content['org.agentteams.run']['blocks'][0]['payload']['kind'] == 'failed'
    assert content['org.agentteams.progress_only'] is True
    progress.finish('failed')
    assert matrix.send_content.call_args.args[2] == transaction


def test_recovery_only_settles_exact_framework_denied_tool(tmp_path):
    bridge = Bridge.__new__(Bridge)
    bridge.home = tmp_path
    root = tmp_path / 'agentteams-native'
    root.mkdir()
    call = {'id': 'tool-one', 'name': 'Bash', 'arguments_sha256': 'hash-one'}
    frame = {'recovery': {'identity': {'task_id': 'exec-one', 'team_id': 'team'}},
             'denied_tools': {'tool-one': {**call, 'error': 'RecoveryUnavailable'}}}
    (root / 'scope.json').write_text(json.dumps(frame))
    task = {'task_id': 'exec-one', 'execution': {'generation': 2, 'instance_id': 'instance', 'process_id': 'process',
             'checkpoint': {'operation': {'id': 'op', 'state': 'started', 'evidence': json.dumps([call])}}}}
    recovery = Mock()
    assert bridge.recover_denied_tool(recovery, task, 'team')
    assert recovery.finish_tools.call_args.args[2][0]['content']['executed'] is False
    recovery.reset_mock()
    frame['denied_tools']['tool-one']['arguments_sha256'] = 'different'
    (root / 'scope.json').write_text(json.dumps(frame))
    assert not bridge.recover_denied_tool(recovery, task, 'team')
    recovery.finish_tools.assert_not_called()
