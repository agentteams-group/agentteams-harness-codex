import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
source = Path(os.environ.get("AT_SOURCE", "/opt/agentteams/transport"))
sys.path.insert(
    0,
    str(
        source / "deepseek-harness/scripts"
        if (source / "deepseek-harness").exists()
        else source
    ),
)
from agentteams_codex.bridge import Bridge, Journal
from agentteams_codex.client import Client

FAKE = """import json,sys
def send(m): print(json.dumps(m),flush=True)
for line in sys.stdin:
 m=json.loads(line)
 if m.get("id")=="human-request":
  send({"method":"item/completed","params":{"item":{"type":"agentMessage","text":json.dumps(m["result"])}}})
  send({"method":"turn/completed","params":{"turn":{"id":"turn-1","status":"completed"}}})
  continue
 if "id" not in m: continue
 if m["method"]=="initialize": result={}
 elif m["method"]=="thread/start": result={"thread":{"id":"thread-1"}}
 elif m["method"]=="turn/start":
  result={"turn":{"id":"turn-1"}}
  send({"id":"human-request","method":METHOD,"params":{"threadId":"thread-1","turnId":"turn-1","command":"printf approved","questions":[{"id":"q","question":"Choose"}]}})
 else: result={}
 send({"id":m["id"],"result":result})
"""


class InteractionTests(unittest.TestCase):
    def run_case(self, method, command, stale_context=False):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(os.environ, {}, clear=True),
        ):
            root = Path(temporary)
            runtime = root / "runtime.yaml"
            runtime.write_text("member:\n  runtime: codex\n")
            bridge = Bridge.__new__(Bridge)
            bridge.name = "codex"
            bridge.runtime_path = runtime
            bridge.workspace = root
            bridge.home = root / ".codex"
            bridge.plugin = root
            bridge.config = {}
            bridge.agent_ids = set()
            bridge.rooms = {"!room:matrix": "team"}
            bridge.journal = Journal(root / "journal.json")
            bridge.controls = queue.Queue()
            bridge.stop = threading.Event()
            bridge.pending_requests = {}
            bridge.active = None
            event = {
                "event_id": "$request",
                "sender": "@human:matrix",
                "room_id": "!room:matrix",
                "body": "task",
                "kind": "text",
            }

            def respond(_event, text, _suffix, approval=None):
                if "needs human input" in text:
                    if stale_context:
                        bridge.handle_control({**event, "body": "/approve human-request old:turn"})
                    bridge.handle_control({**event, "body": command})

            bridge.reply = Mock(side_effect=respond)

            def factory(*args, **kwargs):
                return Client(
                    *args,
                    command=[
                        sys.executable,
                        "-u",
                        "-c",
                        "METHOD=" + repr(method) + "\n" + FAKE,
                    ],
                    **kwargs,
                )

            with (
                patch(
                    "agentteams_codex.bridge.prepare",
                    return_value=("test", "instructions"),
                ),
                patch("agentteams_codex.bridge.Client", side_effect=factory),
            ):
                result = bridge.run_turn(event, {"status": "queued"})
            approvals = [call.args[3] for call in bridge.reply.call_args_list if len(call.args) > 3 and call.args[3]]
            if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
                self.assertEqual([data["status"] for data in approvals], ["pending", "approved" if command.startswith("/approve") else "rejected"])
                for data in approvals:
                    self.assertEqual(data["request_id"], "human-request")
                    self.assertEqual(data["context_id"], "thread-1:turn-1")
                    self.assertEqual(data["runtime"], "codex")
                self.assertEqual(json.loads(approvals[0]["parameters"])["command"], "printf approved")
                self.assertGreater(approvals[0]["expires_at"], 0)
                if stale_context:
                    self.assertTrue(any("belongs to another turn" in str(call) for call in bridge.reply.call_args_list))
            else:
                self.assertEqual(approvals, [])
            return json.loads(result)

    def test_human_accepts_command_approval(self):
        self.assertEqual(
            self.run_case(
                "item/commandExecution/requestApproval", "/approve human-request"
            ),
            {"decision": "accept"},
        )

    def test_human_rejects_file_approval(self):
        self.assertEqual(
            self.run_case("item/fileChange/requestApproval", "/reject human-request"),
            {"decision": "decline"},
        )

    def test_card_context_rejects_a_stale_turn_before_accepting_current_turn(self):
        self.assertEqual(self.run_case("item/commandExecution/requestApproval", "/approve human-request thread-1:turn-1", stale_context=True), {"decision": "accept"})

    def test_human_answers_question(self):
        self.assertEqual(
            self.run_case(
                "item/tool/requestUserInput", "/answer human-request chosen option"
            ),
            {"answers": {"q": {"answers": ["chosen option"]}}},
        )

    def test_reply_serializes_contract_into_matrix_event(self):
        bridge = Bridge.__new__(Bridge)
        bridge.matrix = Mock()
        event = {"room_id": "!room:test", "event_id": "$turn"}
        approval = {"version": 1, "runtime": "codex", "request_id": "1", "context_id": "thread:turn", "status": "pending", "tool_name": "fileChange"}
        bridge.reply(event, "Please approve", "request:1", approval)
        args = bridge.matrix.send_content.call_args.args
        self.assertEqual(args[1]["org.agentteams.approval"], approval)
        self.assertEqual(args[1]["m.relates_to"]["m.in_reply_to"]["event_id"], "$turn")
        bridge.matrix.send_text.assert_not_called()

    def test_only_requesting_human_or_admin_can_resolve(self):
        bridge = Bridge.__new__(Bridge)
        bridge.config = {"team": {"admin": {"matrixUserId": "@admin:test"}}}
        bridge.agent_ids = {"@worker:test"}
        bridge.controls = queue.Queue()
        bridge.active = {"event": {"room_id": "!room:test", "sender": "@human:test"}, "thread_id": "thread", "turn_id": "turn"}
        bridge.reply = Mock()
        for sender in ["@worker:test", "@stranger:test"]:
            bridge.handle_control({"room_id": "!room:test", "sender": sender, "body": "/approve req-1"})
        self.assertTrue(bridge.controls.empty())
        bridge.handle_control({"room_id": "!room:test", "sender": "@human:test", "body": "/approve req-1"})
        self.assertEqual(bridge.controls.get_nowait()[1:3], ("/approve", "req-1"))


if __name__ == "__main__":
    unittest.main()
