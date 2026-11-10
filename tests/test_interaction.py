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
    def run_case(self, method, command):
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

            def respond(_event, text, _suffix):
                if "needs human input" in text:
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

    def test_human_answers_question(self):
        self.assertEqual(
            self.run_case(
                "item/tool/requestUserInput", "/answer human-request chosen option"
            ),
            {"answers": {"q": {"answers": ["chosen option"]}}},
        )


if __name__ == "__main__":
    unittest.main()
