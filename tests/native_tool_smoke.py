"""Check packaged Codex tool admission and confirmed effects against a local Controller fixture."""
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading

from agentteams_codex.client import Client
from agentteams_codex.tool_hook import confirm_native_tool, require_hooks
from native_model_fixture import Model
from worker_matrix_state import WorkerMatrix


class Controller(BaseHTTPRequestHandler):
    identity = {"instance_id": "instance", "generation": 1, "process_id": "worker-process"}
    checkpoint = None
    commits = []
    rejected = 0

    def do_GET(self):
        assert self.path == "/api/v1/runtime/matrix/events/source/execution"
        cp = type(self).checkpoint
        view = {
            "execution": {**self.identity, "state": "active", "runtime": "codex", "checkpoint": cp},
            "instance_id": "instance", "runtime": "codex", "etag": str(len(self.commits)),
            "recovery": "verify" if cp and cp["operation"]["state"] == "started" else "resume",
            "input": {"room_id": "!room:test", "event_id": "$source", "member_prefix": "agents/worker"},
        }
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(view).encode())

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert all(request[k] == v for k, v in self.identity.items())
        if self.deny:
            type(self).rejected += 1
            self.send_error(409, "Execution replaced")
            return
        cp = request["checkpoint"]
        self.commits.append((cp["operation"]["state"], Model.marker.exists()))
        type(self).checkpoint = cp
        self.do_GET()

    def log_message(self, *args):
        pass


with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
    root = Path(tmp)
    home, work = root / ".codex", root / "workspace"
    home.mkdir()
    work.mkdir()
    Model.marker = work / "effect"
    sandbox = sys.argv[2] if len(sys.argv) > 2 else "danger-full-access"
    assert sandbox in {"workspace-write", "danger-full-access"}
    outside = Path("/opt/agentteams") / f"native-shell-fixture-{root.name}"
    Model.tool_command = f"echo confirmed > {Model.marker}"
    if sandbox == "workspace-write":
        Model.tool_command += f" && ! touch {outside}"
    Controller.deny = sys.argv[1] == "deny"
    model, controller = [stack.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), handler))
                         for handler in (Model, Controller)]
    for server in (model, controller):
        threading.Thread(target=server.serve_forever, daemon=True).start()
        stack.callback(server.shutdown)
    token = root / "token"
    token.write_text("fixture")
    identity = {**Controller.identity, "event_key": "source"}
    os.environ.update(CODEX_HOME=str(home), CODEX_GATEWAY_KEY="fixture", CODEX_SANDBOX_MODE=sandbox,
                      AGENTTEAMS_CODEX_MESSAGE_EXECUTION=json.dumps(identity), AGENTTEAMS_AUTH_TOKEN_FILE=str(token),
                      AGENTTEAMS_PROCESS_ID=identity["process_id"], AGENTTEAMS_STORAGE_PREFIX=str(root / "storage"),
                      AGENTTEAMS_CONTROLLER_URL=f"http://127.0.0.1:{controller.server_port}")
    (home / "config.toml").write_text(f'model = "test-model"\nmodel_provider = "fixture"\n[model_providers.fixture]\n'
        f'name = "fixture"\nbase_url = "http://127.0.0.1:{model.server_port}"\nenv_key = "CODEX_GATEWAY_KEY"\nwire_api = "responses"\n')
    matrix = WorkerMatrix("codex", device_delivery=True)
    commands = []
    with Client(work, env=dict(os.environ)) as client:
        require_hooks(client, work)
        thread = client.open_thread(None, work, "test-model", "Run the fixture command")["id"]
        client.start(thread, "Run the fixture command")
        while True:
            event = client.next_event(20)
            if event.get("method") == "item/completed":
                item = event["params"]["item"]
                if item["type"] == "commandExecution":
                    commands.append((item["id"], item.get("status"), item.get("exitCode"), item.get("aggregatedOutput")))
                confirm_native_tool(matrix, identity, item)
            if event.get("method") == "turn/completed":
                break
    if sandbox == "workspace-write":
        assert not outside.exists(), "Native shell escaped its writable workspace"
    if Controller.deny:
        assert Controller.rejected and not Model.marker.exists() and not Controller.commits
    else:
        assert Controller.commits == [("started", False), ("verified", True)], (Controller.commits, commands)
        artifact = Controller.checkpoint["artifacts"][-1]
        data = (root / "storage" / artifact["key"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == artifact["sha256"]
        assert json.loads(data)["results"][0]["content"]["exitCode"] == 0
    print("Native tool admission passed:", sys.argv[1], sandbox)
