"""Codex consumes the public Teams reminder as ordinary work, not a Task."""
import os
from pathlib import Path
import sys
from unittest import TestCase
from unittest.mock import Mock

core = Path(os.environ["AT_SOURCE"])
support = Path(os.environ.get("AT_ASSISTANT_TEST_SUPPORT", core / "tests/lib"))
transport = core / "deepseek-harness/scripts" if (core / "deepseek-harness").is_dir() else core
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "src"),
                str(support), str(core / "shared/lib"), str(transport)]
from assistant_fixture import AssistantFixture
from agentteams_codex.bridge import actionable_events
from agentteams_codex.managed_matrix import ManagedMatrix
from agentteams_codex.task_protocol import prepare_turn


class AssistantProtocolTest(TestCase):
    def test_background_tool_progress_is_silent_and_approval_is_visible(self):
        from agentteams_codex.bridge import Bridge
        bridge = Bridge.__new__(Bridge)
        bridge.matrix = Mock()
        event = {'room_id': '!assistant:test', 'event_id': '$assistant',
            '_task_protocol_meta': {'teamharness_coordination': {'kind': 'assistant_digest'}}}
        bridge.reply(event, 'Codex is working: commandExecution.', 'progress')
        bridge.matrix.send_text.assert_not_called()
        bridge.reply(event, 'Approve?', 'approval', approval={'request_id': 'one'})
        bridge.matrix.send_content.assert_called_once()

    def test_saved_native_reply_is_not_sent_for_background_assistant(self):
        from agentteams_codex.bridge import Bridge
        from unittest.mock import patch
        bridge = Bridge.__new__(Bridge)
        bridge.workspace, bridge.name = core, "leader"
        bridge.matrix, bridge.journal = Mock(), Mock()
        record = {"event":{"event_id":"$assistant","room_id":"!assistant:test","_task_protocol_meta":{"teamharness_coordination":{"kind":"assistant_digest"}}},"answer":"No new updates","outputs":[]}
        with patch("agentteams_codex.bridge.restore_output_paths"):
            bridge.process("$assistant",record)
        self.assertEqual(record["status"],"completed")
        bridge.matrix.send_text.assert_not_called()

    def test_managed_saved_reply_is_closed_without_native_delivery(self):
        managed = ManagedMatrix.__new__(ManagedMatrix)
        managed.matrix, managed.bridge = Mock(), Mock()
        managed.matrix.message.return_value={"recovery":"deliver"}
        managed.matrix.message_claim.return_value={"recovery":"deliver","identity":{"event_key":"assistant"}}
        record={"event":{"shared_key":"assistant","_task_protocol_meta":{"teamharness_coordination":{"kind":"assistant_digest"}}}}
        managed.message_turn(record)
        managed.matrix.message_cancel.assert_called_once_with({"event_key":"assistant"})
        managed.matrix.message_delivered.assert_not_called()
        self.assertEqual(record["status"],"completed")

    def test_assistant_preparation_does_not_enter_business_task_phase(self):
        with AssistantFixture() as f:
            from unittest.mock import patch
            with patch("agentteams_codex.task_protocol.asyncio.run", side_effect=AssertionError("assistant entered Task phase")):
                plugin = Path(os.environ.get("AGENTTEAMS_PLUGIN_DIR", core / "plugins/teamharness"))
                context, _fact, cwd = prepare_turn(plugin, "leader", {
                    "event_id": "$assistant", "room_id": f.room, "sender": f.own,
                    "_task_protocol_meta": f.control["channel_meta"],
                })
            self.assertIsNone(cwd)
            self.assertFalse(context.get("execution"))
    def test_reminder_uses_standard_ingress_and_retains_canonical_input(self):
        with AssistantFixture() as f:
            matrix = ManagedMatrix.__new__(ManagedMatrix)
            matrix.joined = {f.room}
            matrix.decode = actionable_events
            matrix.bridge = Mock(user_id=f.own, agent_ids=set(), rooms={f.room: "personal"},
                config={"matrix": {"transport": {"dm": {"policy": "allowlist", "allowFrom": ["@admin:test"]}}}})
            raw = {"event_id": "$reminder", "type": "m.room.message", "sender": f.own, "content": f.content()}
            self.assertEqual(matrix.classify(f.room, raw), ("message", None))
            event = matrix.event(f.room, raw)
            self.assertEqual(event["body"], f.text)
            self.assertEqual(event["coordination_session"], f.control["session_id"])
            f.context["owner"] = "replaced"
            self.assertIsNone(matrix.classify(f.room, raw))
