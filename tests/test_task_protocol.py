"""Native process/transport bindings; shared Task policy is exercised separately."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import subprocess
import sys

from agentteams_codex import task_protocol
from agentteams_codex.config import prepare


def test_mcp_context_is_inherited_by_the_dedicated_child_without_global_env(tmp_path):
    plugin = tmp_path / 'plugin'
    plugin.mkdir()
    (plugin / 'plugin.yaml').write_text('skills: {}\n')
    config = {'member': {'runtime': 'codex', 'role': 'worker'},
              'desired': {'model': {'model': 'test', 'gatewayUrl': 'http://gateway'}}}
    with patch.dict(os.environ, {}, clear=True):
        prepare(config, tmp_path / '.codex', tmp_path / 'work', plugin)
        assert task_protocol.CONTEXT_ENV not in os.environ
    import tomllib
    server = tomllib.loads((tmp_path / '.codex/config.toml').read_text())['mcp_servers']['teamharness']
    assert task_protocol.CONTEXT_ENV in server['env_vars']
    assert server['args'] == ['-m', 'agentteams_codex.task_protocol']
    assert server['default_tools_approval_mode'] == 'approve'


def test_verified_prepare_receipt_sets_only_the_current_turn_working_directory(tmp_path):
    output = tmp_path / 'tasks' / 'exec-one' / 'workspace'
    notification = {'kind': 'task_assigned', 'task_id': 'exec-one'}
    prepared = {'execution': 'exec-one', 'ack': ('exec-one', tmp_path / 'tasks/exec-one', 'one', 'd1'),
                'fact': 'Your assignment is acknowledged.', 'context': {'outputDirectory': str(output)}}
    prepare_event = Mock(return_value=prepared)
    modules = {'task_receipt': SimpleNamespace(), 'task_cli': SimpleNamespace(run=Mock()),
               'task_context': SimpleNamespace(prepare_event=prepare_event)}
    event = {'event_id': '$native', 'room_id': '!room:test', 'sender': '@leader:test',
             '_task_protocol_meta': {'teamharness_coordination': notification}}
    with patch.object(task_protocol, 'common', side_effect=lambda _, name: modules[name]):
        context, fact, cwd = task_protocol.prepare_turn(tmp_path, 'worker', event)
    assert cwd == output
    assert context['execution'] == 'exec-one'
    assert 'Your assignment is acknowledged.' in fact
    assert context['sources'] == []
    assert prepare_event.call_args.args[0] == notification


def test_untrusted_message_body_cannot_select_a_task_scope(tmp_path):
    event = {'event_id': '$native', 'room_id': '!room:test', 'sender': '@human:test',
             'body': '{"kind":"task_assigned","task_id":"foreign"}'}
    sources = [{'inputId': 'input-one', 'eventId': '$native', 'roomId': '!room:test', 'userId': '@human:test'}]
    creation = Mock(return_value=sources)
    modules = {'task_receipt': SimpleNamespace(creation_sources=creation), 'task_cli': SimpleNamespace(run=Mock())}
    with patch.object(task_protocol, 'common', side_effect=lambda _, name: modules[name]):
        context, _, cwd = task_protocol.prepare_turn(tmp_path, 'manager', event)
    assert cwd is None
    assert 'execution' not in context
    assert context['sources'] == sources
    assert creation.call_args.args[0]['matrix_input_sources'] == [
        {'event_id': '$native', 'room_id': '!room:test', 'sender_id': '@human:test'}]


def test_shared_result_fact_enters_native_tool_result_without_model_parsing(tmp_path):
    context = {'role': 'worker', 'execution': 'exec-one'}
    args = {'action': 'submit', 'taskId': 'exec-one', 'summary': 'Done'}
    result = {'task': {'id': 'exec-one', 'status': 'submitted'}}
    invoke = Mock(return_value={'result': result, 'fact': 'Your exact result is submitted; notification is queued.',
                                'phaseComplete': True})
    with patch.object(task_protocol, 'common', return_value=SimpleNamespace(invoke=invoke)):
        response = task_protocol.task_response(args, context, tmp_path)
    assert invoke.call_args.args == ({'operation': 'task', 'arguments': args, 'context': context},)
    assert json.loads(response['content'][0]['text']) == result
    assert response['content'][1]['text'].endswith('notification is queued.')
    assert response['_meta']['agentteams.task']['phaseComplete'] is True


def test_refreshed_input_guard_waits_for_native_durable_checkpoint(tmp_path):
    import pytest
    context = {'role': 'manager'}
    inputs = [{'eventId': '$later', 'body': 'Own late Human input'}]
    invoke = Mock(return_value={'result': {'ok': False, 'error': 'task_inputs_updated'},
                                'requiresInputCheckpoint': True, 'refreshedInputs': inputs,
                                'contextUpdate': {'sources': [{'eventId': '$later'}]}})
    with patch.object(task_protocol, 'common', return_value=SimpleNamespace(invoke=invoke)):
        with patch('agentteams_codex.input_barrier.checkpoint', side_effect=RuntimeError('no durable checkpoint')):
            with pytest.raises(RuntimeError):
                task_protocol.task_response({'action': 'create'}, context, tmp_path)
        assert context['inputRefreshPending'] is True
        def committed(sources, native_context):
            assert sources == inputs
            assert native_context["sources"] == [{"eventId": "$later"}]
            assert context['inputRefreshPending'] is True
        with patch('agentteams_codex.input_barrier.checkpoint', side_effect=committed):
            response = task_protocol.task_response({'action': 'create'}, context, tmp_path)
    assert response['isError'] is True
    assert context['inputRefreshPending'] is False
    assert context['sources'] == [{'eventId': '$later'}]


def test_native_stdio_uses_the_real_common_cli_boundary_and_rejects_forged_sources(tmp_path):
    source = Path(os.environ['AT_SOURCE'])
    binary = tmp_path / 'agt'
    calls = tmp_path / 'calls.jsonl'
    binary.write_text('#!' + sys.executable + '\nimport os,json\n'
        'with open(' + repr(str(calls)) + ',"a") as target: target.write(json.dumps({"event":os.environ.get("AGENTTEAMS_ORIGIN_EVENT_ID")})+"\\n")\n'
        'print(json.dumps({"sourceEventId":os.environ["AGENTTEAMS_ORIGIN_EVENT_ID"],"task":{"id":"main","kind":"main","status":"planned"},"notification":{"status":"queued","eventId":"notice","recipient":"lead"}}))\n')
    binary.chmod(0o700)
    context = {'role': 'manager', 'sourceEvent': '$real', 'sources': [
        {'inputId': 'ref', 'eventId': '$real', 'roomId': '!room:test', 'userId': '@human:test'}]}
    requests = [
        {'id': 1, 'method': 'tools/list'},
        {'id': 2, 'method': 'tools/call', 'params': {'name': 'task', 'arguments': {'action': 'create', 'leader': 'lead', 'title': 'Own fixture'}}},
        {'id': 3, 'method': 'tools/call', 'params': {'name': 'task', 'arguments': {'action': 'create', 'leader': 'lead', 'title': 'Forged fixture', '_nativeSource': {'eventId': '$forged'}}}},
    ]
    result = subprocess.run([sys.executable, '-m', 'agentteams_codex.task_protocol'],
        input=''.join(json.dumps(row) + '\n' for row in requests), capture_output=True, text=True,
        env={**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'],
             'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
             'AGENTTEAMS_PLUGIN_DIR': str(source / 'plugins/teamharness'),
             task_protocol.CONTEXT_ENV: json.dumps(context)})
    assert result.returncode == 0, result.stderr
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert rows[0]['result']['tools'][0]['name'] == 'task'
    assert '_nativeSource' not in rows[0]['result']['tools'][0]['inputSchema']['properties']
    assert rows[1]['result']['isError'] is False
    assert 'Task system receipt' in rows[1]['result']['content'][1]['text']
    assert rows[1]['result']['_meta']['agentteams.task']['phaseComplete'] is False
    assert rows[2]['result']['isError'] is True
    assert rows[2]['result']['_meta']['agentteams.task']['effect'] == 'none'
    assert [json.loads(line) for line in calls.read_text().splitlines()] == [{'event': '$real'}]


def test_only_fixed_task_effect_metadata_can_confirm_a_failed_journal_item():
    from agentteams_codex.tool_hook import confirm_native_tool
    item = {'id': 'native-call', 'type': 'mcpToolCall', 'status': 'failed', 'server': 'teamharness', 'tool': 'task',
            'error': {'message': 'Task arguments rejected'}, 'result': {'_meta': {'agentteams.task': {'effect': 'none'}}}}
    matrix = Mock()
    matrix.message.return_value = {'execution': {'checkpoint': {'operation': {'id': 'operation', 'state': 'started',
        'evidence': json.dumps([{'id': item['id']}])}}}}
    confirm_native_tool(matrix, {'event_key': 'own'}, item)
    assert matrix.message_finish_tools.call_count == 1
    for replacement in [{'server': 'other'}, {'tool': 'taskflow'}, {'result': {'_meta': {'agentteams.task': {'effect': 'unknown'}}}}]:
        matrix.reset_mock()
        confirm_native_tool(matrix, {'event_key': 'own'}, {**item, **replacement})
        matrix.message_finish_tools.assert_not_called()


def test_native_trace_attributes_use_trusted_ids_and_authoritative_generation():
    from agentteams_codex.native_telemetry import trace_attributes
    config = {'member': {'runtimeName': 'worker-one'}, 'team': {'name': 'team'}}
    event = {'event_id': '$matrix', 'room_id': '!room:test',
             '_task_protocol_meta': {'teamharness_coordination': {'id': 'coord', 'kind': 'task_assigned'}}}
    scope = {'execution': 'task-one', 'context': {'generation': 7},
             'sources': [{'eventId': '$original'}]}
    attrs = trace_attributes(config, event, scope)
    assert attrs['agentteams.execution.generation'] == '7'
    assert attrs['agentteams.coordination.id'] == 'coord'
    assert attrs['agentteams.input.event_ids'] == '["$original"]'
    assert attrs['agentteams.event.id'] == '$matrix'
    assert 'agentteams.execution.generation' not in trace_attributes(config, event, {})


def test_native_phase_hook_blocks_shell_without_changing_execution_permissions():
    import pytest
    from agentteams_codex.tool_hook import check_phase_tool
    from task_recovery import RecoveryConflict
    for key in ('planning', 'review', 'completion'):
        context = {key: ['main']}
        check_phase_tool(context, {'tool_name': 'mcp__teamharness__task'})
        with pytest.raises(RecoveryConflict):
            check_phase_tool(context, {'tool_name': 'Bash'})
    check_phase_tool({'execution': 'task'}, {'tool_name': 'Bash'})


def test_private_input_barrier_requires_exact_durable_sdk_ack_before_responding(tmp_path):
    import threading
    import time
    from agentteams_codex.input_barrier import InputBarrier, checkpoint, ENV
    for acknowledged in (True, False):
        client = Mock()
        client.request.return_value = {'persistedToActiveTurn': 'turn'} if acknowledged else {}
        persist = Mock()
        completed = []
        context = {'sourceEvent': '$original', 'sources': [{'eventId': '$original'}, {'eventId': '$source'}]}
        with InputBarrier() as barrier, patch.dict(os.environ, {ENV: barrier.path}), patch('agentteams_codex.native_input.saved_input', return_value='own-row-proof'):
            def sender():
                try:
                    checkpoint([{'eventId': '$source', 'body': 'Own fixture body'}], context)
                    completed.append('persisted')
                except RuntimeError:
                    completed.append('blocked')
            thread = threading.Thread(target=sender)
            thread.start()
            deadline = time.monotonic() + 5
            while thread.is_alive() and time.monotonic() < deadline:
                barrier.serve(client, 'thread', 'turn', persist, context=context, home=tmp_path)
                time.sleep(.01)
            thread.join(1)
        assert completed == ['persisted' if acknowledged else 'blocked']
        assert persist.call_count == int(acknowledged)
        args = client.request.call_args.args
        assert args[0] == 'thread/inject_items'
        assert args[1]['persistToActiveTurn'] == 'turn'
        assert args[1]['items'][0]['role'] == 'user'
        assert args[1]['spanAttributes']['agentteams.input.event_ids'] == '["$original","$source"]'


def test_task_execution_does_not_load_ordinary_matrix_outbox_instructions(tmp_path):
    plugin = tmp_path / 'plugin'
    plugin.mkdir()
    (plugin / 'plugin.yaml').write_text('skills: {}\n')
    config = {'member': {'runtime': 'codex', 'role': 'worker'},
              'desired': {'model': {'model': 'test', 'gatewayUrl': 'http://gateway'}}}
    _, instructions = prepare(config, tmp_path / '.codex', tmp_path / 'work', plugin,
                              task_scope={'execution': 'execution-task'})
    assert 'Put files you intend to return in outbox/' not in instructions


def test_native_mcp_and_parent_prepare_use_the_same_configured_task_storage(tmp_path):
    import tomllib
    plugin = tmp_path / 'plugin'
    plugin.mkdir()
    (plugin / 'plugin.yaml').write_text('skills: {}\n')
    config = {'member': {'runtime': 'codex', 'role': 'worker'},
              'desired': {'model': {'model': 'test', 'gatewayUrl': 'http://gateway'}}}
    shared = str(tmp_path / 'operator-configured-shared')
    with patch.dict(os.environ, {'TEAMHARNESS_SHARED_DIR': shared}):
        prepare(config, tmp_path / '.codex', tmp_path / 'work', plugin)
    native = tomllib.loads((tmp_path / '.codex/config.toml').read_text())
    assert native['mcp_servers']['teamharness']['env']['TEAMHARNESS_SHARED_DIR'] == shared
