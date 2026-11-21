"""Codex consumes the public Teams reminder as ordinary work, not a Task."""
import os
from pathlib import Path
import sys
from unittest import TestCase
from unittest.mock import Mock

core = Path(os.environ["AT_SOURCE"])
support = Path(os.environ.get("AT_ASSISTANT_TEST_SUPPORT", core / "tests/lib"))
sys.path[:0] = [str(support), str(core / "shared/lib")]
from assistant_fixture import AssistantFixture
from agentteams_codex.bridge import actionable_events
from agentteams_codex.managed_matrix import ManagedMatrix


class AssistantProtocolTest(TestCase):
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
