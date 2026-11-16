"""Recovery adapter unit regressions; these do not run the native model."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

source = Path(os.environ["AT_SOURCE"])
sys.path[:0] = [str(source / "shared/lib"), str(source / "deepseek-harness/scripts"), str(Path(__file__).resolve().parents[1] / "src")]
from task_recovery import Recovery
from agentteams_codex.bridge import Bridge, Journal


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bridge = Bridge.__new__(Bridge)
        self.bridge.config = {"team": {"name": "team"}}
        self.bridge.user_id = "@alice:test"
        self.bridge.journal = Journal(Path(self.tmp.name)/"journal.json")
        self.bridge.journal.data["cursor"] = "keep-all-backlog"
        self.bridge.matrix = Mock()
        self.bridge.run_turn = Mock()
        self.task = {"task_id": "t1", "status": "in_progress", "room_id": "!task:test", "execution": {
            "generation": 2, "process_id": "process", "checkpoint": {"version": 3, "completed_steps": ["one"], "remaining_steps": ["two", "three"]}}}
        self.recovery = Mock()
        self.recovery.session_usable.side_effect = Recovery.session_usable
        self.recovery.resume_text.side_effect = Recovery.resume_text
        self.recovery.digest.side_effect = Recovery.digest
        self.recovery.discover.return_value = {"tasks": [{"task": self.task, "team_id": "team", "etag": "e"}], "instance_id": "new-start"}
        self.recovery.claim.return_value = {"task": self.task, "spec": "Do one, two, three", "saved_result": "Saved answer"}
        self.factory = patch("task_recovery.Recovery", return_value=self.recovery)
        self.factory.start().digest.side_effect = Recovery.digest
        self.addCleanup(self.factory.stop)
        self.enable = patch("task_recovery.enabled", return_value=True)
        self.enable.start()
        self.addCleanup(self.enable.stop)

    def test_startup_recovers_task_without_new_matrix_input(self):
        self.bridge.journal.ingest([{"event_id": "$unrelated", "body": "keep me"},
            {"event_id": "$assignment", "coordination_session": "coordination-task:t1:d1"}], "keep-all-backlog")
        self.bridge.recover_tasks()
        events = self.bridge.journal.data["events"]
        recovered = events["recovery:team/t1:2"]["event"]
        self.assertIn('"completed_steps": ["one"]', recovered["body"])
        self.assertEqual(recovered["recovery"]["task_id"], "t1")
        self.assertIsNone(recovered["recovery"]["thread_id"])
        self.assertEqual(events["$unrelated"]["status"], "queued")
        self.assertEqual(events["$assignment"]["status"], "superseded")
        self.assertEqual(self.bridge.journal.data["cursor"], "keep-all-backlog")
        self.bridge.matrix.send_text.assert_not_called()

    def test_matching_local_session_is_selected_but_older_checkpoint_is_not(self):
        session = {"thread_id": "native-thread", "runtime": "codex", "expires_at": 10**12,
                   "checkpoint_digest": Recovery.digest(self.task["execution"]["checkpoint"])}
        self.bridge.journal.data["task_sessions"] = {"team/t1": session}
        self.bridge.recover_tasks()
        event = self.bridge.journal.data["events"]["recovery:team/t1:2"]["event"]
        self.assertEqual(event["recovery"]["thread_id"], "native-thread")
        self.task["execution"]["generation"] = 3
        self.task["execution"]["checkpoint"]["version"] = 4
        self.bridge.recover_tasks()
        event = self.bridge.journal.data["events"]["recovery:team/t1:3"]["event"]
        self.assertIsNone(event["recovery"]["thread_id"])

    def test_saved_result_only_resends_stable_transaction(self):
        cp = self.task["execution"]["checkpoint"]
        cp.update(delivery="pending", reply_txn="stable-reply")
        self.bridge.matrix.send_text.return_value = "$sent"
        self.bridge.recover_tasks()
        self.bridge.matrix.send_text.assert_called_once_with("!task:test", "Saved answer", transaction_key="stable-reply", progress_only=True)
        self.recovery.delivered.assert_called_once_with("t1", "team", "$sent")
        self.bridge.run_turn.assert_not_called()
        self.assertFalse(self.bridge.journal.data["events"])

    def test_reassignment_prevents_model_execution(self):
        self.recovery.current.return_value = None
        record = {"event": {"recovery": {"task_id": "t1", "team_id": "team"}}, "status": "queued"}
        self.bridge.process_recovered_task("recovery:t1", record)
        self.assertEqual(record["status"], "superseded")
        self.bridge.run_turn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
