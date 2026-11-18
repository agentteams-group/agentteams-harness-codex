import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

root = Path(os.environ["AT_SOURCE"])
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "src"),
                str(root / "deepseek-harness/scripts") if (root / "deepseek-harness").exists() else str(root)]
from agentteams_codex.bridge import Journal
from agentteams_codex.managed_matrix import ManagedMatrix
from agentteams_codex.tool_hook import checkpoint_tool, confirm_native_tool
from agentteams_receipts import Receipts
from task_recovery import RecoveryConflict
from worker_matrix_state import matrix_event_key


class ManagedTests(unittest.TestCase):
    def test_receipts_follow_the_authenticated_worker_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            managed = ManagedMatrix.__new__(ManagedMatrix)
            managed.receipts = Receipts(directory)
            managed.bridge = Mock(user_id="@worker:test", rooms={}, config={}, agent_ids=set())
            managed.bridge.matrix.request.return_value = {"joined_rooms": ["!room:test"]}
            event = {"sender": "@human:test", "event_id": "$source", "type": "m.room.message"}
            joined = {"timeline": {"events": [event, {**event, "sender": "@worker:test", "event_id": "$own"}]}}
            managed.observe({"rooms": {"join": {"!room:test": joined, "!left:test": joined}}})
            managed.raw = Mock()
            managed.raw.poll.side_effect = RecoveryConflict("credential no longer belongs to this Worker")
            with self.assertRaises(RecoveryConflict):
                managed.poll()
            self.assertEqual(managed.bridge.matrix.request.call_count, 1)
            managed.raw.poll.side_effect = None
            managed.raw.poll.return_value = ({"state": {"pending": [], "rooms": {}}}, {})
            replacement = Mock()
            managed.bridge.matrix = replacement
            managed.poll()
            sent = replacement.request.call_args.args
            self.assertEqual((sent[0], sent[2]["status"], sent[2]["m.relates_to"]["event_id"]), ("PUT", "received", "$source"))
            managed.receipts.committed(["$source"])
            managed.poll()
            self.assertEqual([call.args[2]["status"] for call in replacement.request.call_args_list], ["received", "read"])

    def test_private_admission_needs_verification_before_new_work(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = Mock()
            bridge.journal = Journal(Path(directory) / "codex.json")
            event = {"room_id": "!r:test", "event_id": "$old", "kind": "text", "body": "text"}
            key = matrix_event_key(event["room_id"], event["event_id"])
            bridge.journal.ingest([event], "old cursor")
            bridge.journal.data["events"]["$old"]["status"] = "running"
            managed = ManagedMatrix.__new__(ManagedMatrix)
            managed.bridge = bridge
            managed.matrix = Mock()
            managed.schedule([{**event, "shared_key": key}])
            record = bridge.journal.data["events"]["$old"]
            claim = {"identity": {"event_key": key}, "execution": {}, "recovery": "resume"}
            managed.matrix.message.return_value = {"recovery": "resume"}
            managed.matrix.message_claim.return_value = claim
            managed.message_turn(record)
            op = managed.matrix.message_checkpoint.call_args.args[1]["operation"]
            self.assertEqual(op["state"], "unknown")
            bridge.run_turn.assert_not_called()
            self.assertEqual(bridge.journal.data["cursor"], "old cursor")

    def test_successful_native_completion_must_match_started_operation(self):
        matrix = Mock()
        identity = {"event_key": "key"}
        hook = {"hook_event_name": "PreToolUse", "tool_use_id": "call1", "tool_name": "Bash", "tool_input": {"cmd": "echo ok"}}
        checkpoint_tool(matrix, identity, hook)
        matrix.message_begin_tools.assert_called_once_with(identity, [{"id": "call1", "name": "Bash", "arguments": {"cmd": "echo ok"}}])
        matrix.message.return_value = {"execution": {"checkpoint": {"operation": {"state":"started", "id":"op",
            "evidence": json.dumps([{"id":"call1"}])}}}}
        item = {"id":"call1","type":"commandExecution","status":"completed","exitCode":0}
        for change in ({"status":"inProgress"}, {"exitCode":None}, {"exitCode":2}, {"id":"stale"}):
            confirm_native_tool(matrix, identity, {**item, **change})
            matrix.message_finish_tools.assert_not_called()
        confirm_native_tool(matrix, identity, item)
        matrix.message_finish_tools.assert_called_once_with(identity,"op",[{"id":"call1","content":item}])
