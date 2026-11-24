import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

root = Path(os.environ["AT_SOURCE"])
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "src"),
                str(root / "deepseek-harness/scripts") if (root / "deepseek-harness").exists() else str(root)]
from agentteams_codex.bridge import Journal, actionable_events
from agentteams_codex.managed_matrix import ManagedMatrix
from agentteams_codex.tool_hook import checkpoint_tool, confirm_native_tool
from agentteams_receipts import Receipts
from task_recovery import RecoveryConflict
from worker_matrix_state import matrix_event_key


class ManagedTests(unittest.TestCase):
    def test_private_reply_mentions_projected_sender_without_team_roster_membership(self):
        for room_kind, sender, automatic, expected in [
            ("personal", "@manager:test", False, ["@manager:test"]),
            ("personal", "@human:test", False, ["@human:test"]),
            ("team", "@manager:test", False, []),
            ("personal", "@stranger:test", False, []),
            ("personal", "@worker:test", False, []),
            ("personal", "@manager:test", True, []),
        ]:
            with self.subTest(room=room_kind, sender=sender, automatic=automatic):
                managed = ManagedMatrix.__new__(ManagedMatrix)
                event = {"room_id": "!private:test", "event_id": "$request", "shared_key": "key",
                         "sender": sender, "auto_source_mention": automatic}
                bridge = managed.bridge = Mock(user_id="@worker:test", agent_ids={"@worker:test"},
                    rooms={event["room_id"]: room_kind}, config={"matrix": {"transport": {"dm": {
                        "policy": "allowlist", "allowFrom": ["@manager:test", "@human:test"]}}}})
                managed.matrix = Mock()
                managed.matrix.message.return_value = {"recovery": "deliver"}
                managed.matrix.message_claim.return_value = {"recovery": "deliver", "identity": {},
                    "saved_result": "Configuration applied", "execution": {"checkpoint": {}}}
                with patch("agentteams_codex.managed_matrix.publish_raw_saved", return_value="$reply") as send:
                    managed.message_turn({"event": event})
                content = send.call_args.kwargs["content"]
                self.assertEqual(content.get("m.mentions", {}).get("user_ids", []), expected)
                self.assertEqual(bool(content.get("com.agentteams.auto_source_mention")), bool(expected))
                managed.matrix.message_delivered.assert_called_once_with({}, "$reply")
                if sender == "@manager:test" and expected:
                    # The registered Worker room also contains the Human admin.
                    # Its "personal" projection does not make it a two-person
                    # DM for the Manager receiving this reply.
                    reply = {"type": "m.room.message", "event_id": "$reply",
                             "sender": bridge.user_id, "content": {
                                 **content, "msgtype": "m.text", "body": "Configuration applied"}}
                    payload = {"rooms": {"join": {event["room_id"]: {
                        "timeline": {"events": [reply]}}}}}
                    received = actionable_events(payload, {event["room_id"]: "team"},
                                                 sender, {bridge.user_id})
                    self.assertEqual(len(received), 1)
                    self.assertTrue(received[0]["auto_source_mention"])

    def test_manager_invite_uses_projected_direct_sender_allowlist(self):
        managed = ManagedMatrix.__new__(ManagedMatrix)
        managed.receipts = Mock()
        managed.bridge = Mock(user_id="@worker:test", rooms={}, agent_ids={"@leader:test"},
            config={"matrix": {"transport": {"dm": {"policy": "allowlist", "allowFrom": ["@manager:test"]}}}})
        managed.bridge.matrix.request.return_value = {"joined_rooms": []}
        managed.observe({"rooms": {}})
        trusted = managed.bridge.join_invites.call_args.args[1]
        self.assertIn("@manager:test", trusted)
        self.assertNotIn("@stranger:test", trusted)

    def test_restored_input_obeys_current_room_sender_policy(self):
        matrix = ManagedMatrix.__new__(ManagedMatrix)
        room, direct = "!team:test", "!personal:test"
        matrix.joined = {room, direct}
        matrix.decode = actionable_events
        transport = {"groupPolicy": "allowlist", "groupAllowFrom": ["@leader:test"],
                     "dm": {"policy": "allowlist", "allowFrom": ["@owner:test"]}}
        matrix.bridge = Mock(user_id="@worker:test", agent_ids={"@leader:test"},
                             rooms={room: "team", direct: "personal"},
                             config={"matrix": {"transport": transport}})
        message = {"event_id": "$old", "type": "m.room.message",
                   "content": {"body": "Continue", "msgtype": "m.text", "m.mentions": {"user_ids": ["@worker:test"]}}}
        with patch.dict(os.environ, {"AGENTTEAMS_MATRIX_DOMAIN": "test"}):
            self.assertIsNone(matrix.classify(room, {**message, "sender": "@owner:test"}))
            self.assertEqual(matrix.classify(room, {**message, "sender": "@leader:test"}), ("message", None))
            self.assertIsNone(matrix.classify(direct, {**message, "sender": "@leader:test"}))
            self.assertEqual(matrix.classify(direct, {**message, "sender": "@owner:test"}), ("message", None))
            transport["groupAllowFrom"] = ["*"]
            self.assertEqual(matrix.classify(room, {**message, "sender": "@owner:test"}), ("message", None))
            transport["groupPolicy"] = "disabled"
            self.assertIsNone(matrix.classify(room, {**message, "sender": "@owner:test"}))
            packet = {**message, "sender": "@controller:test",
                      "content": {**message["content"], "org.agentteams.coordination": {"id": "signed"}}}
            with patch("task_coordination.notification", return_value={"text": "Task assignment", "session_id": "coordination-task:T:assign"}) as verified:
                self.assertEqual(matrix.classify(room, packet), ("task_notification", "T"))
                verified.assert_called_once()
            with patch("task_coordination.notification", side_effect=ValueError("wrong sender")):
                self.assertIsNone(matrix.classify(room, packet))

    def test_media_stages_stable_bytes_and_requires_a_confirmed_matrix_event(self):
        event = {"room_id": "!room:test", "sender": "@manager:test", "event_id": "$source"}
        for response in ("$media", ""):
            with self.subTest(response=response), tempfile.TemporaryDirectory() as directory:
                managed = ManagedMatrix.__new__(ManagedMatrix)
                bridge = managed.bridge = Mock(user_id="@worker:test", agent_ids={"@manager:test"},
                                               workspace=Path(directory))
                managed.matrix = Mock()
                bridge.matrix.request.return_value = {"user_id": bridge.user_id, "device_id": "one"}
                outbox = bridge.workspace / "outbox"
                outbox.mkdir()
                file = outbox / "report.txt"
                file.write_bytes(b"original")
                def stage(identity, calls, *, reply, output_bytes):
                    self.assertEqual(list(output_bytes), [b"original"])
                    self.assertEqual(reply, "Done")
                    file.write_bytes(b"later unrelated write")
                    return "operation"
                def send(room, path, parent, txn, mentions, automatic, *, before_send):
                    before_send()
                    self.assertEqual((room, parent, path.name, path.read_bytes()),
                                     (event["room_id"], "$source", file.name, b"original"))
                    return response
                managed.matrix.message_begin_tools.side_effect = stage
                bridge.matrix.send_file.side_effect = send
                with patch("agentteams_codex.managed_matrix.require_unencrypted_room") as encryption:
                    if response:
                        managed.save_outputs({"event_key": "key"}, event, "Done", [file.name])
                        args, kwargs = managed.matrix.message_finish_tools.call_args
                        self.assertEqual(args[2][0]["content"]["event_id"], response)
                        self.assertEqual(kwargs, {"deliver_result": True})
                    else:
                        with self.assertRaises(RecoveryConflict):
                            managed.save_outputs({"event_key": "key"}, event, "Done", [file.name])
                        managed.matrix.message_finish_tools.assert_not_called()
                    self.assertEqual(encryption.call_count, 3)
                    self.assertEqual(managed.matrix.message_pending_tools.call_count, 2)

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
