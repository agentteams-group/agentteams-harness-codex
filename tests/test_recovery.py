"""Recovery adapter unit regressions; these do not run the native model."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

source = Path(os.environ["AT_SOURCE"])
lib = source / "shared/lib" if (source / "shared/lib").is_dir() else Path("/opt/agentteams/scripts/lib")
transport = source / "deepseek-harness/scripts" if (source / "deepseek-harness").is_dir() else source
sys.path[:0] = [str(lib), str(transport), str(Path(__file__).resolve().parents[1] / "src")]
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
        self.recovery.current.return_value = self.task
        self.factory = patch("task_recovery.Recovery", return_value=self.recovery)
        self.factory.start().digest.side_effect = Recovery.digest
        self.addCleanup(self.factory.stop)
        self.enable = patch("task_recovery.enabled", return_value=True)
        self.enable.start()
        self.addCleanup(self.enable.stop)

    def test_startup_recovers_task_without_new_matrix_input(self):
        self.bridge.journal.ingest([{"event_id": "$unrelated", "body": "keep me"},
            {"event_id": "$assignment", "room_id": "!task:test", "coordination_session": "coordination-task:t1:d1"},
            {"event_id": "$other-team", "room_id": "!other:test", "coordination_session": "coordination-task:t1:d2"}], "keep-all-backlog")
        self.bridge.recover_tasks()
        events = self.bridge.journal.data["events"]
        recovered = events["recovery:team/t1:2"]["event"]
        self.assertIn('"completed_steps": ["one"]', recovered["body"])
        self.assertEqual(recovered["recovery"]["task_id"], "t1")
        self.assertIsNone(recovered["recovery"]["thread_id"])
        self.assertEqual(events["$unrelated"]["status"], "queued")
        self.assertEqual(events["$assignment"]["status"], "superseded")
        self.assertEqual(events["$other-team"]["status"], "queued")
        self.assertEqual(recovered["session_source_event_ids"], ["$assignment"])
        self.assertEqual(self.bridge.journal.data["cursor"], "keep-all-backlog")
        self.bridge.matrix.send_text.assert_not_called()

    def test_self_discovery_does_not_depend_on_local_team_cache(self):
        self.bridge.config = {"member": {"role": "standalone"}}
        self.bridge.recover_tasks()
        self.recovery.discover.assert_called_once()
        self.assertIn("recovery:team/t1:2", self.bridge.journal.data["events"])

    def test_recovery_sources_survive_another_generation_without_synthetic_receipts(self):
        self.bridge.journal.ingest([{"event_id": "recovery:team/t1:1", "room_id": "!task:test",
            "session_source_event_ids": ["$assignment", "recovery:team/t1:0"],
            "recovery": {"task_id": "t1", "team_id": "team"}}], "keep-all-backlog")
        self.bridge.recover_tasks()
        recovered = self.bridge.journal.data["events"]["recovery:team/t1:2"]["event"]
        self.assertEqual(recovered["session_source_event_ids"], ["$assignment"])

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
        self.task["status"] = "submitted"
        cp = self.task["execution"]["checkpoint"]
        cp.update(delivery="pending", reply_txn="stable-reply")
        self.bridge.matrix.send_text.return_value = "$sent"
        self.bridge.recover_tasks()
        self.bridge.matrix.send_text.assert_called_once_with("!task:test", "Saved answer", transaction_key="stable-reply", progress_only=True)
        self.recovery.delivered.assert_called_once_with("t1", "team", "$sent")
        self.bridge.run_turn.assert_not_called()
        self.assertFalse(self.bridge.journal.data["events"])

    def test_delivered_progress_is_not_authoritative_task_completion(self):
        self.task["execution"]["checkpoint"].update(delivery="delivered", reply_txn="step-one-reply")
        self.bridge.recover_tasks()
        record = self.bridge.journal.data["events"]["recovery:team/t1:2"]
        self.assertIn('"remaining_steps": ["two", "three"]', record["event"]["body"])

    def test_pending_progress_is_delivered_before_resuming_active_task(self):
        self.task["execution"]["checkpoint"].update(delivery="pending", reply_txn="step-one-reply")
        self.recovery.current.return_value = self.task
        self.bridge.recover_tasks()
        self.recovery.delivered.assert_called_once()
        self.assertIn("recovery:team/t1:2", self.bridge.journal.data["events"])

    def test_next_generation_runs_after_progress_but_its_delivered_turn_is_not_repeated(self):
        cp = self.task["execution"]["checkpoint"]
        cp.update(delivery="delivered", reply_txn="step-one-reply")
        self.recovery.process = "process"
        self.bridge.persist_codex = Mock()
        self.bridge.run_turn.return_value = "Step two done"
        saved = {**cp, "version": 4, "delivery": "pending", "reply_txn": "step-two-reply"}
        self.recovery.save_reply.return_value = {"execution": {"checkpoint": saved}}
        delivered = {**saved, "version": 5, "delivery": "delivered"}
        self.recovery.delivered.return_value = {"execution": {"checkpoint": delivered}}
        record = {"event": {"room_id": "!task:test", "recovery": {"task_id": "t1", "team_id": "team", "spec": "Three steps"}},
                  "status": "queued", "thread_id": "native"}
        self.bridge.process_recovered_task("recovery:team/t1:2", record)
        self.bridge.run_turn.assert_called_once()
        self.assertEqual(record["reply_txn"], "step-two-reply")
        self.task["execution"]["checkpoint"] = delivered
        record["status"] = "running"  # Crash after delivery, before local completion persisted.
        self.bridge.process_recovered_task("recovery:team/t1:2", record)
        self.bridge.run_turn.assert_called_once()
        self.assertEqual(record["status"], "completed")

    def test_reassignment_prevents_model_execution(self):
        self.recovery.current.return_value = None
        record = {"event": {"recovery": {"task_id": "t1", "team_id": "team"}}, "status": "queued"}
        self.bridge.process_recovered_task("recovery:t1", record)
        self.assertEqual(record["status"], "superseded")
        self.bridge.run_turn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
