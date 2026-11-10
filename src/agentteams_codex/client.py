"""Small synchronous client for the pinned Codex App Server JSON-RPC API."""

from collections import deque
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time


class CodexError(RuntimeError):
    pass


class Client:
    def __init__(self, workspace: Path, *, command=None, env=None, timeout=60):
        self.proc = subprocess.Popen(
            command or ["codex", "app-server"],
            cwd=workspace,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        self.messages = queue.Queue()
        self.pending = deque()
        self.sequence = 0
        self.timeout = timeout
        self.write_lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "agentteams_codex",
                        "title": "AgentTeams Codex Harness",
                        "version": "0.1.0",
                    }
                },
            )
            self.send({"method": "initialized", "params": {}})
        except Exception:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.proc.stdout:
                self.messages.put(json.loads(line))
        except (ValueError, OSError) as error:
            self.messages.put(CodexError(f"invalid App Server stream: {error}"))
        finally:
            self.messages.put(CodexError("Codex App Server disconnected"))

    def send(self, message):
        with self.write_lock:
            self.proc.stdin.write(json.dumps(message) + "\n")
            self.proc.stdin.flush()

    def _receive(self, timeout):
        try:
            message = self.messages.get(timeout=max(0.01, timeout))
        except queue.Empty:
            raise TimeoutError("Codex App Server response timed out") from None
        if isinstance(message, Exception):
            raise message
        return message

    def request(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Codex App Server {method} timed out")
            message = self._receive(deadline - time.monotonic())
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise CodexError(
                        f"{method}: {message['error'].get('message', 'RPC error')}"
                    )
                return message.get("result", {})
            self.pending.append(message)

    def next_event(self, timeout=1):
        return self.pending.popleft() if self.pending else self._receive(timeout)

    def open_thread(self, thread_id, workspace, model, instructions):
        params = {
            "cwd": str(workspace),
            "model": model,
            "developerInstructions": instructions,
            "approvalPolicy": os.getenv("CODEX_APPROVAL_POLICY", "never"),
            "sandbox": os.getenv("CODEX_SANDBOX_MODE", "workspace-write"),
        }
        if thread_id:
            params["threadId"] = thread_id
        method = "thread/resume" if thread_id else "thread/start"
        for attempt in range(2):
            try:
                return self.request(method, params)["thread"]
            except CodexError as error:
                # A cold required-MCP handshake can time out before any model
                # turn starts. Retrying this boundary cannot replay tool work.
                if attempt or "required MCP servers failed to initialize" not in str(
                    error
                ):
                    raise
                time.sleep(1)

    def start(self, thread_id, text, image=None):
        inputs = [{"type": "text", "text": text}]
        if image:
            inputs.append({"type": "localImage", "path": str(image)})
        return self.request("turn/start", {"threadId": thread_id, "input": inputs})[
            "turn"
        ]

    def control(self, method, thread_id, turn_id, text=""):
        params = {"threadId": thread_id}
        if method == "turn/steer":
            params.update(
                expectedTurnId=turn_id, input=[{"type": "text", "text": text}]
            )
        else:
            params["turnId"] = turn_id
        return self.request(method, params)

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        if self.proc.poll() is None:
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # Kill the process group, including tools/MCP children.
                os.killpg(self.proc.pid, signal.SIGTERM)
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                    self.proc.wait()
        # App Server can exit while a background plugin fetch is still alive.
        # Reap the entire owned process group even after a clean parent exit.
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.proc.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def completed_answer(thread, event_id):
    """Recover only terminal turns belonging to this Matrix event."""
    marker = f"[AgentTeams event {event_id}]"
    for turn in reversed(thread.get("turns", [])):
        if not any(
            marker in json.dumps(item, ensure_ascii=False)
            for item in turn.get("items", [])
            if item.get("type") == "userMessage"
        ):
            continue
        status = turn.get("status")
        if status == "completed":
            answer = "\n".join(
                item.get("text", "")
                for item in turn.get("items", [])
                if item.get("type") == "agentMessage"
                and item.get("phase") != "commentary"
            )
            return answer or "Codex completed the task."
        return f"Codex turn {turn.get('id', '')} was {status}; execution was not replayed. Send a new message to continue."
    return None
