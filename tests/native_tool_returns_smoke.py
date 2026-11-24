"""Real SDK: returned process handles and errors close their own tool receipt."""
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading

from agentteams_codex.admission import Scope, ENV, COMMAND_ENV
from agentteams_codex.client import Client
from agentteams_codex.config import prepare
from agentteams_codex.task_protocol import CONTEXT_ENV
from agentteams_codex.tool_hook import require_hooks


class Controller(BaseHTTPRequestHandler):
    identity = {"instance_id": "instance", "generation": 1, "process_id": "worker-process"}
    checkpoint = None
    commits = []

    def do_GET(self):
        assert self.path == "/api/v1/runtime/matrix/events/source/execution"
        cp = type(self).checkpoint
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({
            "execution": {**self.identity, "state": "active", "runtime": "codex", "checkpoint": cp},
            "instance_id": "instance", "runtime": "codex", "etag": str(len(self.commits)),
            "recovery": "verify" if cp and cp["operation"]["state"] == "started" else "resume",
            "input": {"room_id": "!room:test", "event_id": "$source", "member_prefix": "agents/worker"},
        }).encode())

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/v1/workers/worker/skill-warnings":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{}')
            return
        if self.path == "/api/v1/tasks/native-operations":
            assert request["mode"] == "admit", request
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"mode":"admitted"}')
            return
        assert self.path == "/api/v1/runtime/matrix/events/source/execution", (self.path, list(request))
        assert all(request.get(k) == v for k, v in self.identity.items()), list(request)
        type(self).checkpoint = request["checkpoint"]
        self.commits.append(request["checkpoint"]["operation"]["state"])
        self.do_GET()

    def log_message(self, *_args):
        pass


class Model(BaseHTTPRequestHandler):
    requests = []

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append(request)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        output = [entry for entry in request["input"] if entry.get("type") == "function_call_output"]
        if not output:
            arguments = {"cmd": self.tool_command, "yield_time_ms": 1000}
            if self.mode == "handler-error":
                arguments["workdir"] = str(self.work / "nonexistent")
            name = "exec_command"
        else:
            pending = re.search(r"Process running with session ID (\d+)", json.dumps(output[-1]))
            arguments = {"session_id": int(pending[1]), "chars": "", "yield_time_ms": 1000} if pending else None
            name = "write_stdin"
        if arguments is not None:
            item = {"id": "fc_" + str(len(self.requests)), "type": "function_call",
                    "call_id": "call_" + str(len(self.requests)), "name": name,
                    "arguments": json.dumps(arguments)}
        else:
            item = {"id": "final", "type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": "NATIVE_RETURN_COMPLETE", "annotations": []}]}
        response = {"id": "response_" + str(len(self.requests)), "object": "response", "output": [item]}
        for kind, fields in [
            ("response.created", {"response": {**response, "status": "in_progress"}}),
            ("response.output_item.added", {"output_index": 0, "item": item}),
            ("response.output_item.done", {"output_index": 0, "item": item}),
            ("response.completed", {"response": {**response, "status": "completed",
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}}),
        ]:
            self.wfile.write(("event: " + kind + "\ndata: " + json.dumps({"type": kind, **fields}) + "\n\n").encode())
            self.wfile.flush()

    def log_message(self, *_args):
        pass


def main(mode):
    assert mode in {"async", "failure", "handler-error"}
    with tempfile.TemporaryDirectory(prefix="native-returns-") as directory, ExitStack() as stack:
        root = Path(directory)
        home, work = root / ".codex", root / "workspace"
        work.mkdir()
        (root / "runtime").mkdir()
        Model.mode, Model.work = mode, work
        Model.tool_command = ("python3 -c 'import time; time.sleep(2.5); print(\"ASYNC_RETURN_OK\")'"
                         if mode == "async" else "python3 -c 'import sys; print(\"KNOWN_FAILURE\"); sys.exit(7)'")
        model, controller = [stack.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), handler))
                             for handler in (Model, Controller)]
        for server in (model, controller):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            stack.callback(server.shutdown)
        token = root / "token"
        token.write_text("fixture")
        identity = {**Controller.identity, "event_key": "source"}
        plugin = Path(os.environ["AT_SOURCE"]) / "plugins/teamharness"
        context = {"role": "worker", "sourceEvent": "$source", "sources": []}
        scope = Scope(home, plugin, context, {"kind": "message", "identity": identity})
        os.environ.update(HOME=str(root), CODEX_HOME=str(home), CODEX_GATEWAY_KEY="fixture",
            CODEX_SANDBOX_MODE="danger-full-access", AGENTTEAMS_AGENT_ROLE="worker",
            AGENTTEAMS_WORKER_NAME="worker", AGENTTEAMS_PROCESS_ID=identity["process_id"],
            AGENTTEAMS_AUTH_TOKEN_FILE=str(token), AGENTTEAMS_STORAGE_PREFIX=str(root / "storage"),
            AGENTTEAMS_CONTROLLER_URL=f"http://127.0.0.1:{controller.server_port}")
        os.environ[ENV] = str(scope.path)
        os.environ[CONTEXT_ENV] = json.dumps(context)
        os.environ[COMMAND_ENV] = json.dumps([sys.executable, "-m", "agentteams_codex.admission"])
        config = {"member": {"role": "worker", "runtime": "codex"}, "desired": {"model": {
            "model": "test-model", "gatewayUrl": f"http://127.0.0.1:{model.server_port}"}}}
        model_name, instructions = prepare(config, home, work, plugin)
        with Client(work, env=dict(os.environ)) as client:
            require_hooks(client, work)
            thread = client.open_thread(None, work, model_name, instructions)["id"]
            client.start(thread, "Run the bounded tool fixture")
            while True:
                event = client.next_event(30)
                if event.get("method") == "turn/completed":
                    break
            scope.require_admitted()
        assert len(Model.requests) >= 2, "Known tool return blocked the next native request"
        assert Controller.commits and Controller.commits[-1] == "verified", Controller.commits
        assert all(Controller.commits[i:i + 2] == ["started", "verified"]
                   for i in range(0, len(Controller.commits), 2)), Controller.commits
        actual_outputs = json.dumps([item.get("output") for item in Model.requests[-1]["input"]
                                    if item.get("type") == "function_call_output"])
        if mode == "async":
            assert any("write_stdin" in json.dumps(request["input"]) for request in Model.requests), [
                item for request in Model.requests for item in request["input"]
                if item.get("type") in {"function_call", "function_call_output"}]
            assert "ASYNC_RETURN_OK" in actual_outputs
        elif mode == "failure":
            assert "KNOWN_FAILURE" in actual_outputs
            assert "code 7" in actual_outputs
        else:
            assert re.search(r"nonexistent|No such file|CreateProcess", actual_outputs), actual_outputs
        print(json.dumps({"case": mode, "nativeRequests": len(Model.requests),
                          "toolReceipts": Controller.commits, "realTaskCertification": False}))


if __name__ == "__main__":
    main(sys.argv[1])
