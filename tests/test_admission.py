"""Exercise native-owned policy boundaries rather than Task business rules."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

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


class AdmissionTests(unittest.TestCase):
    def test_terminal_phase_without_business_text_remains_quiet_after_restore(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            context = frame()['context']
            scope = admission.Scope(root / '.codex', root / 'plugin', context)
            self.assertIsNone(scope.presentation())
            self.assertIsNone(scope.reply())
            with patch.dict(os.environ, {admission.ENV: str(scope.path)}):
                admission.phase_complete({'phaseComplete': True, 'receiptId': 'planned',
                    'fact': '[Task system receipt] execution plan saved'}, context)
            restored = admission.Scope(root / '.codex', root / 'plugin', context)
            self.assertEqual(restored.presentation(), '')
            self.assertEqual(restored.reply(), '[Task system receipt] execution plan saved')
            self.assertEqual(json.loads(restored.path.read_text())['complete']['fact'],
                             '[Task system receipt] execution plan saved')
            with patch.dict(os.environ, {admission.ENV: str(scope.path)}):
                admission.phase_complete({'phaseComplete': True, 'receiptId': 'submitted',
                    'fact': 'submission saved', 'displayText': '已生成 result.json，检查通过。'}, context)
            self.assertEqual(scope.presentation(), '已生成 result.json，检查通过。')
            self.assertEqual(scope.reply(), '已生成 result.json，检查通过。')

    def test_only_private_factory_registration_delegates(self):
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


    def test_unknown_admission_blocks_before_tool_start(self):
        from task_operation import tool_policy
        requests = []
        def execute(request):
            requests.append(request)
            return tool_policy(request.get('registration'))
        with patch('task_recovery_rpc.execute', side_effect=execute), patch.object(
                admission, 'admit', side_effect=RecoveryConflict('unknown')):
            with self.assertRaises(RecoveryConflict):
                tool_hook.scoped_tool(frame(), hook(name='Bash'))
        assert [r['action'] for r in requests] == ['tool_policy']


    def test_post_tool_finishes_exact_owned_started_even_returned_error(self):
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


    def test_scoped_mcp_receipt_preserves_native_success_and_failure(self):
        from task_operation import tool_policy
        from itertools import product
        for (response, failed), kind in product([
            ({'isError': True, 'content': [{'type': 'text', 'text': 'Concrete MCP failure'}]}, True),
            ({'isError': False, 'structuredContent': {'ok': True, 'idempotent_hit': True}}, False),
            ({'content': [], 'structuredContent': {'error': 'ordinary user field'}}, False),
        ], ['task', 'message']):
            with self.subTest(response=response, kind=kind):
                requests = []
                def execute(request):
                    requests.append(request)
                    if request['action'] == 'tool_policy':
                        return tool_policy(request.get('registration'))
                    if request['action'] in {'current', 'matrix.message.owned'}:
                        saved = {'execution': {'checkpoint': {'operation': {
                            'id': 'operation-owned', 'state': 'started',
                            'evidence': json.dumps([{'id': 'call-native'}])}}}}
                        return {'task': saved} if kind == 'task' else saved
                event = dict(hook('PostToolUse', 'mcp__fixture__write'), tool_response=response)
                with patch('task_recovery_rpc.execute', side_effect=execute):
                    context = frame()
                    context['recovery']['kind'] = kind
                    tool_hook.scoped_tool(context, event)
                result = requests[-1]['results'][0]
                self.assertEqual(result.get('status') == 'error', failed)
                self.assertEqual(result['content'], response)
                self.assertNotIn('effect', result)

    def test_wrong_tool_id_cannot_finish_started_operation(self):
        from task_operation import tool_policy
        def execute(request):
            if request['action'] == 'tool_policy':
                return tool_policy(request.get('registration'))
            return {'task': {'execution': {'checkpoint': {'operation': {
                'state': 'started', 'evidence': '[{"id":"other-call"}]'}}}}}
        with patch('task_recovery_rpc.execute', side_effect=execute):
            with self.assertRaises(RecoveryConflict):
                tool_hook.scoped_tool(frame(), hook('PostToolUse', 'Bash'))


    def test_private_callback_blocked_outcome_survives_scope_restore(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            scope = admission.Scope(root / '.codex', root / 'plugin', frame()['context'])
            with patch.dict(os.environ, {admission.ENV: str(scope.path)}):
                admission.mark_blocked(True)
                restored = admission.Scope(root / '.codex', root / 'plugin', frame()['context'])
                with self.assertRaisesRegex(RuntimeError, 'admission blocked'):
                    restored.require_admitted()
                admission.mark_blocked(False)
                restored.require_admitted()


    def test_manager_generic_tools_use_canonical_admission_without_a_worker_execution(self):
        from task_operation import tool_policy
        manager = {**frame(), 'context': {'sourceEvent': '$manager-dm', 'role': 'manager'}, 'recovery': None}
        with patch('task_recovery_rpc.execute', side_effect=lambda request: tool_policy(request.get('registration'))), \
                patch.object(admission, 'admit') as gate:
            tool_hook.scoped_tool(manager, hook(name='Bash'))
            gate.assert_called_once_with(manager)
            tool_hook.scoped_tool(manager, hook('PostToolUse', 'Bash'))
            tool_hook.scoped_tool(manager, hook())

    def test_worker_cannot_omit_its_durable_execution(self):
        from task_operation import tool_policy
        with patch('task_recovery_rpc.execute', side_effect=lambda request: tool_policy(request.get('registration'))), \
                patch.object(admission, 'admit'):
            with self.assertRaisesRegex(RecoveryConflict, 'durable execution admission'):
                tool_hook.scoped_tool({**frame(), 'recovery': None}, hook(name='Bash'))
