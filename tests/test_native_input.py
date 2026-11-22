"""Only native persisted rows authorize a Matrix input receipt."""
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from agentteams_codex.native_input import commit_source
from task_recovery import RecoveryUnavailable


class NativeInputTests(unittest.TestCase):
    def setUp(self):
        self.view = {'input': {'room_id': '!room:test', 'event_id': '$input',
                             'input': {'sender': '@human:test', 'content': {'body': 'original'}}}}
        self.client, self.matrix, self.persist = Mock(), Mock(), Mock()
        self.client.request.return_value = {'persistedToActiveTurn': 'actual-turn'}
        self.matrix.native_current.return_value = True

    def test_active_control_requires_exact_native_persistence_receipt_and_row(self):
        with patch('agentteams_codex.native_input.saved_input', side_effect=[None, 'row-sha']):
            self.assertEqual(commit_source(self.client, Path('/own-fixture'), 'thread', self.matrix,
                'key', self.view, self.persist, active_turn_id='actual-turn'), 'row-sha')
        self.assertEqual(self.client.request.call_args.args[1]['persistToActiveTurn'], 'actual-turn')

    def test_queued_ack_cannot_publish_an_active_control_source(self):
        self.client.request.return_value = {}
        with patch('agentteams_codex.native_input.saved_input', return_value=None):
            with self.assertRaises(RecoveryUnavailable):
                commit_source(self.client, Path('/own-fixture'), 'thread', self.matrix,
                    'key', self.view, self.persist, active_turn_id='actual-turn')
        self.persist.assert_not_called()
        self.matrix.native_committed.assert_not_called()

    def test_initial_source_does_not_request_active_turn_persistence(self):
        with patch('agentteams_codex.native_input.saved_input', side_effect=[None, 'row-sha']):
            commit_source(self.client, Path('/own-fixture'), 'thread', self.matrix,
                          'key', self.view, self.persist)
        self.assertNotIn('persistToActiveTurn', self.client.request.call_args.args[1])
