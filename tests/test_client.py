import sys
import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agentteams_codex.client import Client, CodexError, completed_answer

FAKE = """import json, sys
for line in sys.stdin:
 m=json.loads(line)
 if "id" not in m: continue
 method=m["method"]
 if method=="initialize": result={}
 elif method=="thread/start": result={"thread":{"id":"thread-1"}}
 elif method=="turn/start":
  print(json.dumps({"method":"item/completed","params":{"item":{"type":"agentMessage","text":"result"}}}),flush=True)
  result={"turn":{"id":"turn-1"}}
 else:
  print(json.dumps({"id":m["id"],"error":{"message":"unsupported"}}),flush=True)
  continue
 print(json.dumps({"id":m["id"],"result":result}),flush=True)
"""


class ClientTests(unittest.TestCase):
    def test_close_stops_children_after_clean_server_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "orphan-ran"
            child = f"import time,pathlib; time.sleep(1); pathlib.Path({str(marker)!r}).touch()"
            server = (
                "import subprocess,json,sys\n"
                f"subprocess.Popen([sys.executable,'-c',{child!r}],stdout=subprocess.DEVNULL)\n"
                "for line in sys.stdin:\n"
                " m=json.loads(line)\n"
                " if 'id' in m: print(json.dumps({'id':m['id'],'result':{}}),flush=True)\n"
            )
            with Client(Path(temp), command=[sys.executable, "-u", "-c", server]):
                pass
            time.sleep(1.1)
            self.assertFalse(marker.exists(), "a child survived App Server shutdown")

    def test_rpc_preserves_notifications_before_response(self):
        with tempfile.TemporaryDirectory() as temp:
            with Client(
                Path(temp), command=[sys.executable, "-u", "-c", FAKE], timeout=1
            ) as client:
                thread = client.open_thread(None, Path(temp), "model", "instructions")
                self.assertEqual(thread["id"], "thread-1")
                self.assertEqual(client.start(thread["id"], "task")["id"], "turn-1")
                self.assertEqual(
                    client.next_event()["params"]["item"]["text"], "result"
                )
                with self.assertRaisesRegex(CodexError, "unsupported"):
                    client.request("unknown", {})

    def test_recovery_matches_event_and_does_not_replay_uncertain_turn(self):
        thread = {
            "turns": [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "type": "userMessage",
                            "content": [{"text": "[AgentTeams event $one] do work"}],
                        },
                        {
                            "type": "agentMessage",
                            "text": "working",
                            "phase": "commentary",
                        },
                        {
                            "type": "agentMessage",
                            "text": "done",
                            "phase": "final_answer",
                        },
                    ],
                }
            ]
        }
        self.assertEqual(completed_answer(thread, "$one"), "done")
        self.assertIsNone(completed_answer(thread, "$other"))
        thread["turns"][0]["status"] = "inProgress"
        self.assertIn("not replayed", completed_answer(thread, "$one"))

    def test_cold_mcp_retry_happens_only_before_turn_start(self):
        client = Client.__new__(Client)
        with (
            patch.object(
                client,
                "request",
                side_effect=[
                    CodexError("required MCP servers failed to initialize"),
                    {"thread": {"id": "thread-1"}},
                ],
            ) as request,
            patch("agentteams_codex.client.time.sleep"),
        ):
            self.assertEqual(
                client.open_thread(None, Path("/tmp"), "model", "prompt")["id"],
                "thread-1",
            )
            self.assertEqual(
                [call.args[0] for call in request.call_args_list],
                ["thread/start", "thread/start"],
            )
        with patch.object(
            client, "request", side_effect=CodexError("model not available")
        ) as request:
            with self.assertRaises(CodexError):
                client.open_thread(None, Path("/tmp"), "model", "prompt")
            request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
