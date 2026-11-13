"""Durable Matrix ingress and Codex execution, using pinned AT transport assets."""

import json
from contextlib import closing
import os
from pathlib import Path
import queue
import shutil
import signal
import sqlite3
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .client import Client, CodexError, completed_answer
from .config import prepare, read_runtime

# The transport and file helpers are consumed unchanged from a pinned AT
# commit. Runtime execution and Codex state belong to this repository.
from matrix_channel import MatrixClient, matrix_events
from matrix_bridge import (
    materialize_attachment,
    snapshot_outbox,
    changed_outbox_files,
    sync_output_paths,
    restore_output_paths,
    send_output_paths,
    runtime_matrix_context,
    runtime_agent_user_ids,
    reply_mention_user_ids,
    source_agent_reply_user_id,
    mc,
)


def actionable_events(payload, rooms, user_id, agents):
    events = matrix_events(payload, rooms, user_id, agents)
    # TeamHarness uses an authenticated same-agent event to continue Project
    # Work in a task room. Ordinary self messages must remain suppressed.
    for room_id, room in payload.get("rooms", {}).get("join", {}).items():
        if room_id not in rooms:
            continue
        for event in room.get("timeline", {}).get("events", []):
            content = event.get("content", {})
            trigger = content.get("m.teamharness.trigger", {})
            if (
                event.get("type") == "m.room.message"
                and event.get("sender") == user_id
                and content.get("msgtype") == "m.text"
                and event.get("event_id")
                and content.get("body")
                and not event.get("unsigned", {}).get("redacted_because")
                and trigger.get("type") == "PROJECT_REQUESTED"
                and trigger.get("kind") == "self_cross_session"
                and trigger.get("agentId") == user_id
                and trigger.get("targetRoomId") == room_id
                and trigger.get("targetSession") == f"matrix:{room_id}"
                and trigger.get("sourceSession") != trigger.get("targetSession")
            ):
                events.append(
                    {
                        "room_id": room_id,
                        "event_id": event["event_id"],
                        "sender": user_id,
                        "body": content["body"],
                        "kind": "text",
                    }
                )
    return events


class Journal:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.data = (
            json.loads(self.path.read_text())
            if self.path.exists()
            else {"version": 1, "cursor": "", "rooms": {}, "events": {}}
        )
        if self.data.get("version") != 1:
            raise ValueError("unsupported Codex bridge journal version")

    def save(self):
        with self.lock:
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.data, ensure_ascii=False) + "\n")
            temporary.replace(self.path)
            prefix = os.getenv("AGENTTEAMS_STORAGE_PREFIX")
            if prefix:
                mc(
                    "cp",
                    str(self.path),
                    f"{prefix}/agents/{os.environ['AGENTTEAMS_WORKER_NAME']}/runtime/{self.path.name}",
                )

    def ingest(self, events, cursor):
        with self.lock:
            for event in events:
                self.data["events"].setdefault(
                    event["event_id"], {"event": event, "status": "queued"}
                )
            self.data["cursor"] = cursor
            self.save()


def snapshot_codex(home, destination):
    """Snapshot native history and consistent SQLite backups, excluding auth."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        if (home / "sessions").exists():
            shutil.copytree(home / "sessions", root / "sessions")
        for database in home.glob("*.sqlite"):
            with (
                closing(sqlite3.connect(database)) as source,
                closing(sqlite3.connect(root / database.name)) as target,
            ):
                source.backup(target)
        temporary = destination.with_suffix(".tmp")
        with tarfile.open(temporary, "w:gz") as archive:
            for child in root.iterdir():
                archive.add(child, arcname=child.name)
        temporary.replace(destination)


def restore_codex(archive, home):
    if not archive.exists():
        return
    with tarfile.open(archive, "r:gz") as source:
        for item in source.getmembers():
            path = Path(item.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not (item.isfile() or item.isdir())
            ):
                raise ValueError("unsafe Codex state archive")
            if path.parts[0] != "sessions" and not (
                len(path.parts) == 1 and path.suffix == ".sqlite"
            ):
                raise ValueError("unexpected file in Codex state archive")
        # Debian's Python 3.11.2 predates the extractall(filter=...) API.
        # Every path and member type is validated above before extracting.
        source.extractall(home)


class Bridge:
    def __init__(self):
        self.name = os.environ["AGENTTEAMS_WORKER_NAME"]
        self.runtime_path = Path(os.environ["TEAMHARNESS_RUNTIME_CONFIG"])
        self.workspace = Path(os.environ["TEAMHARNESS_WORKSPACE"])
        self.home = Path(os.environ["CODEX_HOME"])
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.home.mkdir(parents=True, exist_ok=True)
        self.plugin = Path(
            os.getenv("AGENTTEAMS_PLUGIN_DIR", "/opt/agentteams/plugins/teamharness")
        )
        self.journal = Journal(self.runtime_path.parent / "codex-bridge-state.json")
        restore_codex(self.runtime_path.parent / "codex-state.tar.gz", self.home)
        self.matrix = MatrixClient(
            os.environ["AGENTTEAMS_MATRIX_URL"],
            os.environ["AGENTTEAMS_WORKER_MATRIX_TOKEN"],
        )
        self.stop = threading.Event()
        self.controls = queue.Queue()
        self.active = None
        self.pending_requests = {}
        self.last_sync = 0
        self.config = read_runtime(self.runtime_path)
        self.refresh_context()

    def refresh_context(self):
        self.user_id, self.rooms = runtime_matrix_context(
            self.runtime_path, self.name, os.environ["AGENTTEAMS_MATRIX_DOMAIN"]
        )
        self.agent_ids = runtime_agent_user_ids(self.runtime_path)
        if not self.rooms:
            raise ValueError("runtime.yaml has no Matrix rooms")

    def persist_codex(self):
        prefix = os.getenv("AGENTTEAMS_STORAGE_PREFIX")
        if prefix:
            mc(
                "mirror",
                str(self.workspace) + "/",
                f"{prefix}/agents/{self.name}/workspace/",
                "--overwrite",
            )
            self.journal.data["workspace_has_files"] = any(
                path.is_file() for path in self.workspace.rglob("*")
            )
        path = self.runtime_path.parent / "codex-state.tar.gz"
        snapshot_codex(self.home, path)
        if prefix:
            mc("cp", str(path), f"{prefix}/agents/{self.name}/runtime/{path.name}")

    def reply(self, event, text, suffix, approval=None):
        if approval:
            return self.matrix.send_content(
                event["room_id"],
                {"msgtype": "m.text", "body": text,
                 "m.relates_to": {"m.in_reply_to": {"event_id": event["event_id"]}},
                 "org.agentteams.approval": approval},
                f"codex:{event['event_id']}:{suffix}",
            )
        return self.matrix.send_text(
            event["room_id"],
            text,
            event["event_id"],
            f"codex:{event['event_id']}:{suffix}",
        )

    def handle_control(self, event):
        body = event["body"]
        verb, _, argument = body.partition(" ")
        if verb not in {"/cancel", "/steer", "/approve", "/reject", "/answer"}:
            return False
        active = self.active
        admin = self.config.get("team", {}).get("admin", {}).get("matrixUserId")
        if not active or event["room_id"] != active["event"]["room_id"]:
            self.reply(event, "No Codex turn is active in this room.", "control")
        elif event["sender"] in self.agent_ids or event["sender"] not in {
            active["event"]["sender"],
            admin,
        }:
            self.reply(
                event,
                "Only the requesting human or Team admin can control this turn.",
                "control",
            )
        else:
            self.controls.put(
                (
                    event,
                    verb,
                    argument.strip(),
                    active.get("thread_id"),
                    active.get("turn_id"),
                )
            )
        return True

    def poll(self):
        while not self.stop.is_set():
            try:
                prefix = os.getenv("AGENTTEAMS_STORAGE_PREFIX")
                if prefix:
                    temporary = self.runtime_path.with_suffix(".incoming")
                    mc(
                        "cp",
                        f"{prefix}/agents/{self.name}/runtime/runtime.yaml",
                        str(temporary),
                    )
                    if temporary.read_bytes() != self.runtime_path.read_bytes():
                        read_runtime(temporary)
                        temporary.replace(self.runtime_path)
                        self.config = read_runtime(self.runtime_path)
                        self.refresh_context()
                    else:
                        temporary.unlink()
                cursor = self.journal.data["cursor"]
                payload = self.matrix.sync(cursor or None, 1000)
                trusted = self.agent_ids | {
                    self.config.get("team", {}).get("admin", {}).get("matrixUserId", "")
                }
                for room_id, room in payload.get("rooms", {}).get("invite", {}).items():
                    if any(
                        event.get("sender") in trusted
                        and event.get("state_key") == self.user_id
                        and event.get("content", {}).get("membership") == "invite"
                        for event in room.get("invite_state", {}).get("events", [])
                    ):
                        self.matrix.request(
                            "POST",
                            "/_matrix/client/v3/join/"
                            + urllib.parse.quote(room_id, safe=""),
                            {},
                        )
                # Matrix membership is the access boundary. Task rooms created
                # by TeamHarness may not appear in the initial runtime.yaml.
                for room_id in payload.get("rooms", {}).get("join", {}):
                    self.rooms.setdefault(room_id, "team")
                if cursor:
                    self.matrix.fill_timeline_gaps(payload, cursor, self.rooms)
                events = (
                    actionable_events(payload, self.rooms, self.user_id, self.agent_ids)
                    if cursor
                    else []
                )
                queued = []
                for event in events:
                    if event["event_id"] in self.journal.data["events"]:
                        continue
                    policy = self.config.get("desired", {}).get("channelPolicy", {})
                    deny = (
                        "groupDenyExtra"
                        if self.rooms[event["room_id"]] == "team"
                        else "dmDenyExtra"
                    )
                    domain = os.environ["AGENTTEAMS_MATRIX_DOMAIN"]
                    denied = {
                        (
                            value
                            if value == "*" or (value.startswith("@") and ":" in value)
                            else f"@{value.lstrip('@')}:{domain}"
                        )
                        for value in policy.get(deny, [])
                    }
                    if "*" in denied or event["sender"] in denied:
                        continue
                    if self.handle_control(event):
                        queued.append({**event, "control": True})
                    else:
                        queued.append(event)
                self.journal.ingest(queued, payload.get("next_batch", cursor))
                self.last_sync = time.time()
            except Exception as error:
                # Never include upstream response bodies or credentials in logs.
                print(
                    f"[agentteams-codex] Matrix poll failed: {type(error).__name__}",
                    flush=True,
                )
                self.stop.wait(2)

    def run_turn(self, event, record):
        config = read_runtime(self.runtime_path)
        model, instructions = prepare(config, self.home, self.workspace, self.plugin)
        credential = os.getenv("AGENTTEAMS_WORKER_GATEWAY_KEY") or config.get(
            "desired", {}
        ).get("model", {}).get("gatewayKey", "")
        environment = {
            **os.environ,
            "CODEX_GATEWAY_KEY": os.getenv("CODEX_GATEWAY_KEY") or credential,
        }
        thread_id = self.journal.data["rooms"].get(event["room_id"])
        text, image = event["body"], None
        if event["kind"] != "text":
            text, attachment = materialize_attachment(
                event,
                self.matrix,
                self.workspace,
                int(
                    os.getenv(
                        "AGENTTEAMS_MATRIX_ATTACHMENT_MAX_BYTES", str(25 * 1024 * 1024)
                    )
                ),
            )
            if event["body"] != event.get("filename"):
                text += "\nUser request: " + event["body"]
            if event["kind"] == "image":
                image = attachment
        with Client(self.workspace, env=environment) as client:
            thread = client.open_thread(thread_id, self.workspace, model, instructions)
            thread_id = thread["id"]
            self.journal.data["rooms"][event["room_id"]] = thread_id
            record["thread_id"] = thread_id
            self.journal.save()
            recovered = completed_answer(thread, event["event_id"])
            if recovered is not None:
                return recovered
            if record.get("status") == "running":
                # The original start may have reached the model before a crash.
                # Do not repeat shell/MCP side effects when history is uncertain.
                return "Codex execution was interrupted before its result was saved; it was not replayed. Send a new message to continue."
            record["status"] = "running"
            self.journal.save()
            turn = client.start(
                thread_id,
                f"[AgentTeams event {event['event_id']}]\nCurrent Matrix session: matrix:{event['room_id']}\nMatrix sender: {event['sender']}\n{text}",
                image,
            )
            record["turn_id"] = turn["id"]
            self.active = {
                "event": event,
                "thread_id": thread_id,
                "turn_id": turn["id"],
            }
            self.journal.save()
            answers = []
            deadline = time.monotonic() + int(
                os.getenv("CODEX_TASK_TIMEOUT_SECONDS", "3600")
            )
            last_progress = {}
            try:
                while not self.stop.is_set():
                    if time.monotonic() > deadline:
                        client.control("turn/interrupt", thread_id, turn["id"])
                        raise TimeoutError("Codex task timed out")
                    while not self.controls.empty():
                        source, verb, argument, target_thread, target_turn = (
                            self.controls.get_nowait()
                        )
                        try:
                            if (target_thread, target_turn) != (thread_id, turn["id"]):
                                raise ValueError(
                                    "The requested Codex turn has already ended"
                                )
                            if verb == "/cancel":
                                client.control("turn/interrupt", thread_id, turn["id"])
                            elif verb == "/steer":
                                client.control(
                                    "turn/steer", thread_id, turn["id"], argument
                                )
                            else:
                                request_id, _, answer = argument.partition(" ")
                                request = self.pending_requests.get(request_id)
                                if request is None:
                                    raise ValueError(
                                        "Approval/input request is missing or expired"
                                    )
                                method = request["method"]
                                if method == "item/tool/requestUserInput":
                                    if verb != "/answer":
                                        raise ValueError("Use /answer REQUEST_ID text")
                                    questions = request["params"].get("questions", [])
                                    result = {
                                        "answers": {
                                            question["id"]: {"answers": [answer]}
                                            for question in questions
                                        }
                                    }
                                elif method in {
                                    "item/commandExecution/requestApproval",
                                    "item/fileChange/requestApproval",
                                }:
                                    if verb not in {"/approve", "/reject"}:
                                        raise ValueError("Use /approve REQUEST_ID or /reject REQUEST_ID")
                                    result = {
                                        "decision": (
                                            "accept"
                                            if verb == "/approve"
                                            else "decline"
                                        )
                                    }
                                else:
                                    result = {"action": "decline", "content": None}
                                client.send({"id": request["id"], "result": result})
                                self.pending_requests.pop(request_id, None)
                                if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
                                    self.reply(event, f"Codex approval {request_id}: {result['decision']}.",
                                        f"approval:{request_id}:resolved", {
                                            "version": 1, "runtime": "codex", "request_id": request_id,
                                            "context_id": f"{thread_id}:{turn['id']}",
                                            "status": "approved" if result["decision"] == "accept" else "rejected",
                                        })
                            self.reply(source, "Codex control accepted.", "control")
                        except (ValueError, CodexError) as error:
                            self.reply(source, str(error), "control")
                    try:
                        message = client.next_event(timeout=1)
                    except TimeoutError:
                        continue
                    method, params = message.get("method", ""), message.get(
                        "params", {}
                    )
                    if "id" in message and method:
                        if method == "item/permissions/requestApproval":
                            client.send(
                                {"id": message["id"], "result": {"permissions": {}}}
                            )
                        elif method in {
                            "item/commandExecution/requestApproval",
                            "item/fileChange/requestApproval",
                            "item/tool/requestUserInput",
                            "mcpServer/elicitation/request",
                        }:
                            request_id = str(message["id"])
                            self.pending_requests[request_id] = message
                            summary = (
                                params.get("command")
                                or params.get("questions")
                                or params.get("message")
                                or params.get("reason")
                                or method
                            )
                            self.reply(
                                event,
                                f"Codex needs human input ({request_id}): {summary}\nUse /approve {request_id}, /reject {request_id}, or /answer {request_id} text.",
                                f"request:{request_id}",
                                {
                                    "version": 1, "runtime": "codex", "request_id": request_id,
                                    "context_id": f"{thread_id}:{turn['id']}",
                                    "status": "pending",
                                    "tool_name": "commandExecution" if method == "item/commandExecution/requestApproval" else "fileChange",
                                    "parameters": json.dumps(params, ensure_ascii=False),
                                } if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"} else None,
                            )
                        else:
                            client.send(
                                {
                                    "id": message["id"],
                                    "error": {
                                        "code": -32601,
                                        "message": "Unsupported client request",
                                    },
                                }
                            )
                    if method == "item/completed":
                        item = params.get("item", {})
                        if (
                            item.get("type") == "agentMessage"
                            and item.get("phase") != "commentary"
                        ):
                            answers.append(item.get("text", ""))
                    show_tools = (
                        not config.get("desired", {})
                        .get("channels", {})
                        .get("matrix", {})
                        .get("filter_tool_messages", False)
                    )
                    if show_tools and method == "item/started":
                        kind = params.get("item", {}).get("type", "work")
                        if (
                            kind in {"commandExecution", "mcpToolCall", "fileChange"}
                            and time.monotonic() - last_progress.get(kind, 0) > 10
                        ):
                            self.reply(
                                event,
                                f"Codex is working: {kind}.",
                                f"progress:{params.get('item', {}).get('id')}",
                            )
                            last_progress[kind] = time.monotonic()
                    if (
                        method == "turn/completed"
                        and params.get("turn", {}).get("id") == turn["id"]
                    ):
                        terminal = params["turn"]
                        if terminal["status"] == "completed":
                            return (
                                "\n".join(filter(None, answers))
                                or "Codex completed the task."
                            )
                        if terminal["status"] == "interrupted":
                            return "Codex execution was cancelled."
                        error = terminal.get("error") or {}
                        raise CodexError(error.get("message", "Codex turn failed"))
                client.control("turn/interrupt", thread_id, turn["id"])
                raise InterruptedError("Worker is shutting down")
            finally:
                self.active = None
                for request_id, request in list(self.pending_requests.items()):
                    if request["method"] in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
                        self.reply(event, f"Codex approval {request_id} expired.", f"approval:{request_id}:expired", {
                            "version": 1, "runtime": "codex", "request_id": request_id, "status": "expired",
                            "context_id": f"{thread_id}:{turn['id']}",
                        })
                self.pending_requests.clear()

    def process(self, event_id, record):
        event = record["event"]
        if event.get("control"):
            record["status"] = "completed"
            self.journal.save()
            return
        if "answer" not in record:
            record.setdefault("outbox_before", snapshot_outbox(self.workspace))
            self.journal.save()
            try:
                answer = self.run_turn(event, record)
            except Exception as error:
                # Preserve the terminal error, so Matrix delivery retries do not
                # execute the model/tool turn again.
                answer = f"Codex execution failed ({type(error).__name__}): {error}"
                record["error"] = type(error).__name__
            self.persist_codex()
            outputs = changed_outbox_files(self.workspace, record["outbox_before"])
            sync_output_paths(self.name, self.workspace, outputs)
            record.update(answer=answer, outputs=outputs, status="answer_ready")
            self.journal.save()
        restore_output_paths(self.name, self.workspace, record["outputs"])
        source = source_agent_reply_user_id(event, self.agent_ids, self.user_id)
        self.matrix.send_text(
            event["room_id"],
            record["answer"],
            event_id,
            f"codex:{event_id}:reply",
            reply_mention_user_ids(
                event, record["answer"], self.agent_ids, self.user_id
            ),
            bool(source),
        )
        send_output_paths(
            self.matrix,
            event["room_id"],
            event_id,
            self.workspace,
            record["outputs"],
            [source] if source else [],
            bool(source),
        )
        record["status"] = "completed"
        # Retain bounded replay protection; pending events are never pruned.
        completed = [
            key
            for key, value in self.journal.data["events"].items()
            if value["status"] == "completed"
        ]
        for old in completed[:-2000]:
            del self.journal.data["events"][old]
        self.journal.save()

    def run(self):
        threading.Thread(target=self.poll, daemon=True).start()
        while not self.stop.is_set():
            pending = [
                (key, value)
                for key, value in list(self.journal.data["events"].items())
                if value["status"] != "completed"
            ]
            for event_id, record in pending:
                if self.stop.is_set():
                    break
                try:
                    self.process(event_id, record)
                except Exception as error:
                    print(
                        f"[agentteams-codex] delivery pending: {type(error).__name__}",
                        flush=True,
                    )
                    self.stop.wait(2)
                    break
            self.stop.wait(0.2)


def main():
    if os.getenv("AGENTTEAMS_MATRIX_E2EE", "0").lower() in {"1", "true"}:
        raise RuntimeError("Codex bridge does not support Matrix E2EE")
    bridge = Bridge()

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            ready = bool(bridge.last_sync and time.time() - bridge.last_sync < 90)
            body = json.dumps(
                {"ok": ready, "runtime": "codex", "active": bridge.active is not None}
            ).encode()
            self.send_response(
                200
                if self.path == "/healthz" and ready
                else 503 if self.path == "/healthz" else 404
            )
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(
        ("0.0.0.0", int(os.getenv("AGENTTEAMS_CONSOLE_PORT", "8088"))), Health
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: bridge.stop.set())
    try:
        bridge.run()
    finally:
        server.shutdown()
