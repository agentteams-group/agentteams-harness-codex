import io
import os
from pathlib import Path
import queue
import sqlite3
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
source = Path(os.environ.get("AT_SOURCE", "/opt/agentteams/transport"))
transport = (
    source / "deepseek-harness/scripts"
    if (source / "deepseek-harness").is_dir()
    else source
)
sys.path.insert(0, str(transport))
from agentteams_codex.bridge import (
    Bridge,
    Journal,
    snapshot_codex,
    restore_codex,
    actionable_events,
)
from agentteams_codex.client import CodexError


class StateTests(unittest.TestCase):
    def test_read_checkpoint_requires_native_user_input_and_a_successful_snapshot(self):
        bridge = Bridge.__new__(Bridge)
        bridge.journal = Mock()
        bridge.persist_codex = Mock()
        client = Mock()
        client.request.return_value = {"thread": {"turns": [{"id": "turn", "items": []}]}}
        record = {}
        bridge.checkpoint_input(client, "thread", "turn", record)
        self.assertNotIn("input_checkpointed", record)
        client.request.return_value["thread"]["turns"][0]["items"] = [{"type": "userMessage"}]
        bridge.persist_codex.side_effect = OSError("checkpoint unavailable")
        bridge.checkpoint_input(client, "thread", "turn", record)
        self.assertNotIn("input_checkpointed", record)
        bridge.persist_codex.side_effect = None
        bridge.checkpoint_input(client, "thread", "turn", record)
        self.assertTrue(record["input_checkpointed"])
        bridge.journal.save.assert_called_once()

    def test_supplement_is_not_lost_when_completion_wins_interrupt(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = Bridge.__new__(Bridge)
            bridge.workspace = bridge.home = bridge.plugin = Path(temporary)
            bridge.runtime_path = Path(temporary) / "runtime.yaml"
            bridge.runtime_path.write_text("member:\n  runtime: codex\n")
            bridge.config = {}
            bridge.journal = Journal(Path(temporary) / "journal.json")
            bridge.controls = queue.Queue()
            bridge.stop = threading.Event()
            bridge.pending_requests = {}
            bridge.reply = Mock()
            bridge.direct_member = Mock(return_value=False)
            event = {"event_id": "$original", "room_id": "!dm", "sender": "@human:test", "body": "original", "kind": "text"}
            supplement = {**event, "event_id": "$supplement", "body": "additional input", "control": True}
            bridge.journal.ingest([event, supplement], "cursor")
            bridge.controls.put((supplement, "/redirect", "additional input", "thread-1", "turn-1"))
            with patch("agentteams_codex.bridge.prepare", return_value=("model", "instructions")), patch("agentteams_codex.bridge.Client") as factory:
                client = factory.return_value.__enter__.return_value
                client.open_thread.return_value = {"id": "thread-1", "turns": []}
                client.start.return_value = {"id": "turn-1"}
                client.control.side_effect = CodexError("turn already completed")
                client.next_event.return_value = {"method": "turn/completed", "params": {"turn": {"id": "turn-1", "status": "completed"}}}
                bridge.run_turn(event, bridge.journal.data["events"]["$original"])
            pending = Journal(bridge.journal.path).data["events"]["$supplement"]
            self.assertEqual(pending["status"], "queued")
            self.assertFalse(pending["event"]["control"])
            self.assertEqual(pending["event"]["body"], "additional input")

    def test_only_valid_cross_session_self_trigger_bypasses_self_filter(self):
        event = {
            "type": "m.room.message",
            "sender": "@leader:matrix",
            "event_id": "$trigger",
            "content": {
                "msgtype": "m.text",
                "body": "continue project",
                "m.teamharness.trigger": {
                    "type": "PROJECT_REQUESTED",
                    "kind": "self_cross_session",
                    "agentId": "@leader:matrix",
                    "sourceSession": "matrix:!source",
                    "targetSession": "matrix:!target",
                    "targetRoomId": "!target",
                },
            },
        }
        payload = {"rooms": {"join": {"!target": {"timeline": {"events": [event]}}}}}
        self.assertEqual(
            len(
                actionable_events(payload, {"!target": "team"}, "@leader:matrix", set())
            ),
            1,
        )
        event["content"]["m.teamharness.trigger"]["agentId"] = "@other:matrix"
        self.assertEqual(
            actionable_events(payload, {"!target": "team"}, "@leader:matrix", set()), []
        )

    def test_ingress_is_durable_and_duplicate_keeps_original_status(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(os.environ, {}, clear=True),
        ):
            path = Path(temporary) / "journal.json"
            journal = Journal(path)
            journal.ingest([{"event_id": "$one", "body": "task"}], "cursor-1")
            journal.data["events"]["$one"]["status"] = "completed"
            journal.ingest([{"event_id": "$one", "body": "duplicate"}], "cursor-2")
            restored = Journal(path)
            self.assertEqual(restored.data["events"]["$one"]["status"], "completed")
            self.assertEqual(restored.data["events"]["$one"]["event"]["body"], "task")
            self.assertEqual(restored.data["cursor"], "cursor-2")

    def test_native_snapshot_restores_sqlite_and_sessions_without_auth(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            (home / "sessions").mkdir(parents=True)
            (home / "sessions/turn.jsonl").write_text("session history\n")
            (home / "auth.json").write_text("never-export-this-secret")
            with sqlite3.connect(home / "state.sqlite") as database:
                database.execute("create table threads (id text)")
                database.execute("insert into threads values ('thread-1')")
            snapshot_codex(home, root / "state.tar.gz")
            restored = root / "restored"
            restored.mkdir()
            restore_codex(root / "state.tar.gz", restored)
            self.assertFalse((restored / "auth.json").exists())
            self.assertEqual(
                (restored / "sessions/turn.jsonl").read_text(), "session history\n"
            )
            with sqlite3.connect(restored / "state.sqlite") as database:
                self.assertEqual(
                    database.execute("select id from threads").fetchone(), ("thread-1",)
                )

    def test_snapshot_rejects_path_escape_and_auth(self):
        for filename in ("../escape", "auth.json"):
            with (
                self.subTest(filename=filename),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                with tarfile.open(root / "bad.tar.gz", "w:gz") as archive:
                    info = tarfile.TarInfo(filename)
                    info.size = 1
                    archive.addfile(info, io.BytesIO(b"x"))
                with self.assertRaises(ValueError):
                    restore_codex(root / "bad.tar.gz", root / "home")

    def test_delivery_retry_does_not_repeat_model_or_side_effects(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(os.environ, {}, clear=True),
        ):
            bridge = Bridge.__new__(Bridge)
            bridge.name = "worker"
            bridge.workspace = Path(temporary)
            bridge.journal = Journal(Path(temporary) / "journal.json")
            bridge.matrix = Mock()
            bridge.matrix.send_text.side_effect = [
                OSError("temporary delivery failure"),
                "$reply",
            ]
            bridge.agent_ids = set()
            bridge.user_id = "@worker:matrix"
            bridge.run_turn = Mock(return_value="completed result")
            bridge.persist_codex = Mock()
            event = {
                "event_id": "$one",
                "room_id": "!room:matrix",
                "sender": "@human:matrix",
                "body": "do work",
                "kind": "text",
            }
            bridge.journal.ingest([event], "cursor")
            record = bridge.journal.data["events"]["$one"]
            with self.assertRaises(OSError):
                bridge.process("$one", record)
            bridge.process("$one", record)
            bridge.run_turn.assert_called_once()
            self.assertEqual(record["status"], "completed")
            self.assertEqual(
                bridge.matrix.send_text.call_args_list[0],
                bridge.matrix.send_text.call_args_list[1],
            )

    def test_control_is_scoped_to_room_and_requesting_human(self):
        bridge = Bridge.__new__(Bridge)
        bridge.config = {"team": {"admin": {"matrixUserId": "@admin:matrix"}}}
        bridge.agent_ids = {"@other-agent:matrix"}
        bridge.controls = queue.Queue()
        bridge.reply = Mock()
        bridge.active = {"event": {"room_id": "!one", "sender": "@human:matrix"}}
        event = {"body": "/cancel", "room_id": "!one", "sender": "@other-agent:matrix"}
        self.assertFalse(bridge.handle_control({**event, "body": "/redirect ordinary text"}))
        self.assertTrue(bridge.handle_control(event))
        self.assertTrue(bridge.controls.empty())
        bridge.handle_control({**event, "sender": "@human:matrix", "room_id": "!other"})
        self.assertTrue(bridge.controls.empty())
        bridge.handle_control({**event, "sender": "@human:matrix"})
        self.assertEqual(bridge.controls.get()[1], "/cancel")


if __name__ == "__main__":
    unittest.main()
