"""Durable Matrix ingress and Codex execution, using pinned AT transport assets."""

import json
import hashlib
from contextlib import closing
import os
from pathlib import Path
import queue
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .client import Client, CodexError, completed_answer
from .native_input import rollout_digest, checkpoint_rows_present
from .input_barrier import InputBarrier, ENV as BARRIER_ENV
from .admission import Scope as NativeScope, ENV as SCOPE_ENV, COMMAND_ENV as ADMISSION_COMMAND_ENV
from .managed_matrix import ManagedMatrix
from .tool_hook import require_hooks
from .tool_progress import ToolProgress, run_id
from .config import managed_worker, prepare, read_runtime, sync_assigned_skills, _role
from .task_protocol import CONTEXT_ENV, prepare_turn, delivery_context, common
from .native_telemetry import trace_attributes
try:
    from runtime_observability import event as trace_event
except ImportError:
    def trace_event(*args, **kwargs):
        pass  # Standalone packages can run without container logging.

# The transport and file helpers are consumed unchanged from a pinned AT
# commit. Runtime execution and Codex state belong to this repository.
from agentteams_receipts import Receipts, completed_turns
from matrix_channel import MatrixClient, matrix_events, matrix_transaction_id
from task_recovery import RecoveryConflict, RecoveryUnavailable
from worker_matrix_delivery import confirm_bound
from worker_matrix_raw import publish_raw_saved
from worker_sessions import WorkerSessions
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
    manager_allowed_senders,
)


def actionable_events(payload, rooms, user_id, agents, allowed_senders=None):
    events = matrix_events(payload, rooms, user_id, agents, allowed_senders)
    by_id = {event["event_id"]: event for event in events}
    content_by_id = {
        event.get("event_id"): event.get("content", {})
        for room in payload.get("rooms", {}).get("join", {}).values()
        for event in room.get("timeline", {}).get("events", [])
    }
    for event in events:
        control = content_by_id.get(event["event_id"], {}).get("com.agentteams.control")
        if control is not None:
            event["execution_control"] = control
    # TeamHarness uses an authenticated same-agent event to continue Project
    # Work in a task room. Ordinary self messages must remain suppressed.
    for room_id, room in payload.get("rooms", {}).get("join", {}).items():
        if room_id not in rooms:
            continue
        for event in room.get("timeline", {}).get("events", []):
            content = event.get("content", {})
            if event.get("event_id") in by_id and isinstance(content.get("com.agentteams.answer"), dict):
                from runtime_hitl import attention_answer
                if not attention_answer(content):
                    by_id[event["event_id"]]["answer"] = content["com.agentteams.answer"]
                else:
                    by_id[event["event_id"]].pop("answer", None)
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
            with temporary.open("w") as stream:
                stream.write(json.dumps(self.data, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            prefix = os.getenv("AGENTTEAMS_STORAGE_PREFIX")
            if prefix:
                mc(
                    "cp",
                    str(self.path),
                    f"{prefix}/agents/{os.environ['AGENTTEAMS_WORKER_NAME']}/runtime/{self.path.name}",
                )

    def ingest(self, events, cursor, completed=()):
        with self.lock:
            for room, event_id in completed:
                record = self.data["events"].get(event_id)
                if record and record["event"]["room_id"] == room:
                    record["status"] = "completed"
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


def task_thread(home, recovery, task, work):
    sessions = WorkerSessions("codex", recovery=recovery)
    view = sessions.load(work)
    execution = task["execution"]
    if (view["generation"] != execution["generation"] or view["instance_id"] != execution["instance_id"]
            or view["checkpoint_digest"] != recovery.digest(execution.get("checkpoint"))):
        raise RecoveryConflict("Task changed before Codex thread selection")
    ref = (view.get("cache") or {}).get("native_ref")
    return (ref if sessions.usable(view, ref, rollout_digest(home, ref)) else None), view


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
        self.stop = threading.Event()
        self.controls = queue.Queue()
        self.active = None
        self.pending_requests = {}
        self.last_sync = 0
        self.reported_ready = False
        self.config = read_runtime(self.runtime_path)
        self.refresh_context()
        self.managed = ManagedMatrix(self, actionable_events) if managed_worker() else None
        self.manager_receipts = (Receipts(self.home.parent)
            if self.config.get("member", {}).get("role") == "manager" else None)
        self.scan_ready = threading.Event()
        self.handoff_scanned = False

    def deliver_task(self, recovery, task_id, team, room, execution, text):
        def bind(cp):
            actual = recovery.checkpoint(task_id, team, cp)["execution"]
            confirm_bound(actual, execution, "codex", cp)
        identity = run_id({'recovery': {'team_id': team, 'task_id': task_id, 'generation': execution['generation']}})
        content = {"org.agentteams.progress_only": True, "org.agentteams.run_id": identity}
        task = recovery.current(task_id, team)
        if task and task.get("task_plan_id") and task.get("submission_id") and task.get("status") in {"submitted", "completed"}:
            # Keep the exact report as the durable delivery body; present the
            # verified phase receipt instead of repeating the report in chat.
            result = task.get("result_status")
            title = ("结果需要修正，已提交任务系统处理" if result == "REVISION_NEEDED" else
                     "执行受阻，已提交任务系统处理" if result in {"BLOCKED", "INTERRUPTED"} else
                     "节点结果已保存，后续推进由任务系统接管")
            content["org.agentteams.run"] = {"version": "1", "run_id": identity,
                                              "blocks": [{"type": "text", "text": title}]}
        return publish_raw_saved(self.matrix, room, text, execution["checkpoint"], self.user_id, bind,
            content=content, legacy_transaction_id=matrix_transaction_id)

    def recover_tasks(self, only_task=None):
        from task_recovery import Recovery, enabled
        if not enabled() or self.config.get("member", {}).get("role") == "manager":
            return
        from worker_matrix_state import WorkerMatrix
        recovery = Recovery(device_delivery=True)
        WorkerMatrix("codex", recovery=recovery).start()
        snapshot = recovery.discover()
        for entry in snapshot["tasks"]:
            task_id, team = entry["task"]["task_id"], entry["team_id"]
            if only_task is not None and task_id != only_task:
                continue
            try:
                claimed = recovery.claim(snapshot, entry)
            except RecoveryConflict:
                continue  # A changed assignment must not block an unrelated Task.
            task = claimed["task"]
            execution = task["execution"]
            if execution["state"] == "needs_verification":
                trace_event("task.recovery.verify", team_id=team, task_id=task_id, generation=execution["generation"])
                continue
            cp = execution.get("checkpoint") or {}
            room = task.get("room_id")
            if not room:
                raise ValueError("Recoverable Task has no Matrix room")
            if cp.get("delivery") == "pending":
                try:
                    event_id = self.deliver_task(recovery, task_id, team, room, execution, claimed["saved_result"])
                except (RecoveryConflict, RecoveryUnavailable) as error:
                    trace_event("task.delivery_pending", task_id=task_id, error_type=type(error).__name__)
                    continue
                recovery.delivered(task_id, team, event_id)
                task = recovery.current(task_id, team)
                if task is None:
                    continue
                execution = task["execution"]
                cp = execution.get("checkpoint") or {}
            if task["status"] == "submitted":
                continue
            key = team + "/" + task_id
            event_id = "recovery:" + key + ":" + str(execution["generation"])
            task = {**task, "team_id": team, "spec": claimed["spec"]}
            event = {"event_id": event_id, "room_id": room, "sender": self.user_id,
                     "kind": "text", "body": recovery.resume_text(task),
                     "session_source_event_ids": [],
                     "coordination_session": "recovery:" + key,
                     "recovery": {"task_id": task_id, "team_id": team, "thread_id": None, "spec": claimed["spec"],
                                  **{k: execution[k] for k in ("instance_id", "generation", "process_id")}}}
            if task.get("task_plan_id"):
                event["_task_protocol_meta"] = {"teamharness_coordination": {
                    "kind": "task_assigned", "task_id": task_id, "main_task_id": task.get("main_task_id", ""),
                    "project_id": task.get("project_id", ""), "delivery_id": task.get("notifyTxn")}}
            # Supersede only the same Task's local admission. Other Matrix
            # backlog stays queued and its cursor is never reset.
            for old_id, old in list(self.journal.data["events"].items()):
                source = old["event"]
                prior = source.get("recovery", {})
                if source.get("room_id") != room:
                    continue
                if (source.get("coordination_session", "").startswith("coordination-task:" + task_id + ":")
                        or (prior.get("task_id") == task_id and prior.get("team_id") == team)):
                    event["session_source_event_ids"].extend(
                        source_id for source_id in source.get("session_source_event_ids", [old_id])
                        if source_id.startswith("$"))
                    if old_id != event_id and not source.get("shared_key"):
                        old["status"] = "superseded"
            event["session_source_event_ids"] = list(dict.fromkeys(event["session_source_event_ids"]))
            self.journal.ingest([event], self.journal.data["cursor"])

    def refresh_context(self):
        self.user_id, self.rooms = runtime_matrix_context(
            self.runtime_path, self.name, os.environ["AGENTTEAMS_MATRIX_DOMAIN"]
        )
        self.agent_ids = runtime_agent_user_ids(self.runtime_path)
        if self.config.get("member", {}).get("role") == "manager":
            domain = os.environ["AGENTTEAMS_MATRIX_DOMAIN"]
            self.agent_ids = manager_allowed_senders(self.runtime_path, domain) - {f"@{os.getenv('AGENTTEAMS_ADMIN_USER', 'admin')}:{domain}"}
        if not self.rooms:
            raise ValueError("runtime.yaml has no Matrix rooms")
        if managed_worker():
            channel = self.config["matrix"]
            self.matrix = MatrixClient(channel["transport"]["homeserver"], channel["accessToken"])
            prepare(self.config, self.home, self.workspace, self.plugin)
        else:
            self.matrix = MatrixClient(os.environ["AGENTTEAMS_MATRIX_URL"], os.environ["AGENTTEAMS_WORKER_MATRIX_TOKEN"])
            sync_assigned_skills(self.config, self.home, self.plugin)

    def persist_codex(self, *, include_workspace=True):
        prefix = os.getenv("AGENTTEAMS_STORAGE_PREFIX")
        if prefix and include_workspace:
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

    def checkpoint_refreshed_input(self, record, frame):
        self.persist_codex(include_workspace=False)
        record["native_task_context"] = frame
        self.journal.save()

    def checkpoint_input(self, client, thread_id, turn_id, record):
        if record.get("input_checkpointed"):
            return
        try:
            thread = client.request("thread/read", {"threadId": thread_id, "includeTurns": True})["thread"]
            if not any(turn.get("id") == turn_id and any(item.get("type") == "userMessage" for item in turn.get("items", [])) for turn in thread.get("turns", [])):
                return
            self.persist_codex(include_workspace=False)
        except (CodexError, OSError, subprocess.SubprocessError, KeyError):
            return
        record["input_checkpointed"] = True
        acknowledgements = record.pop("delivery_context_ack", {})
        if acknowledgements:
            common(self.plugin, "task_receipt").ack_delivery_context(
                self.journal.data.setdefault("task_delivery_context", {}), acknowledgements)
        self.journal.save()

    def reply(self, event, text, suffix, approval=None):
        if not approval and (event.get('_task_protocol_meta') or {}).get('teamharness_coordination', {}).get('kind') == 'assistant_digest':
            return None
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

    def question_receipt(self, event, request, status):
        content = {
            "msgtype": "m.text", "body": f"Question {status}",
            "org.agentteams.run": {"version": "1", "blocks": [{
                "type": "confirmation", "tool_name": "ask_human",
                "confirmation_id": request["_confirmation_id"], "status": status,
            }]},
        }
        transaction = f"codex:{event['event_id']}:question:{request['_confirmation_id']}:{status}"
        with self.journal.lock:
            self.journal.data.setdefault("question_receipts", {})[transaction] = {"room": event["room_id"], "content": content}
            self.journal.save()
        self.flush_question_receipts()

    def flush_question_receipts(self):
        with self.journal.lock:
            pending = list(self.journal.data.get("question_receipts", {}).items())
        for transaction, receipt in pending:
            try:
                self.matrix.send_content(receipt["room"], receipt["content"], transaction)
            except Exception as error:
                trace_event("hitl.receipt_pending", error_type=type(error).__name__)
                continue
            with self.journal.lock:
                self.journal.data["question_receipts"].pop(transaction, None)
                self.journal.save()

    def handle_control(self, event):
        body = event["body"]
        answer = event.get("answer")
        if isinstance(answer, dict) and answer.get("kind") == "tool_approval":
            from runtime_hitl import approval_decision
            decision = approval_decision({"com.agentteams.answer": answer}, self.user_id)
            if not decision:
                return True
            request_id, context, approved = decision
            body = f"/{'approve' if approved else 'reject'} {request_id} {context}"
            answer = None
        if isinstance(answer, dict):
            if answer.get("kind") not in (None, "ask_user"):
                return True
            if answer.get("agent_id") and answer["agent_id"] != self.user_id:
                return True
            request_id = next((key for key, value in self.pending_requests.items()
                               if value.get("_confirmation_id") == answer.get("confirmation_id")), None)
            if request_id is None:
                return True
            body = f"/answer {request_id} " + json.dumps({"responses": answer.get("responses")}, ensure_ascii=False)
        verb, _, argument = body.partition(" ")
        control = event.get("execution_control")
        if control is not None:
            if not isinstance(control, dict) or control.get("action") not in {"stop", "steer"} or control.get("agent_id") != self.user_id:
                return True
            active = self.active
            if not active or control.get("run_id") != active["event"]["event_id"] or event["room_id"] != active["event"]["room_id"]:
                return control["action"] == "stop"
            if event["sender"] != active["event"]["sender"] or not self.direct_member(event):
                return True
            # Persist the supplementary event before steering: the poller
            # and native turn run on separate threads, and either may win.
            if control["action"] == "steer":
                if not body.strip():
                    return True
                self.journal.ingest([{**event, "control": True}], self.journal.data["cursor"])
            verb = ("/redirect" if event.get("kind") == "image" else "/steer") if control["action"] == "steer" else "/cancel"
            argument = body
        if verb not in {"/cancel", "/steer", "/approve", "/reject", "/answer"}:
            if verb != "/redirect" or control is None:
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
            if event.get("control_execution"):
                event["awaiting_native_control"] = True
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

    def direct_member(self, event):
        if event["sender"] in self.agent_ids or self.rooms.get(event["room_id"]) != "personal":
            return False
        try:
            members = self.matrix.request("GET", "/_matrix/client/v3/rooms/" + urllib.parse.quote(event["room_id"], safe="") + "/joined_members")
            return set(members.get("joined", {})) == {self.user_id, event["sender"]}
        except Exception:
            return False

    def execution_status(self, event, status, message_id=None):
        self.matrix.send_content(event["room_id"], {
            "msgtype": "m.notice", "body": {"running": "正在执行", "stopping": "正在停止", "stopped": "已停止", "completed": "本轮已结束", "failed": "执行失败"}[status],
            "com.agentteams.execution": {"run_id": event["event_id"], "status": status,
                "capabilities": ["stop", "steer"], "steering_mode": "native",
                **({"message_id": message_id} if message_id else {})},
        }, f"codex:{event['event_id']}:execution:{status}")

    def join_invites(self, payload, trusted):
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

    def poll(self):
        while not self.stop.is_set():
            stage = "config.refresh"
            try:
                self.flush_question_receipts()
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
                        warnings = self.home / "skill-warnings.json"
                        if warnings.is_file() and json.loads(warnings.read_text()):
                            sync_assigned_skills(self.config, self.home, self.plugin)
                if getattr(self, "managed", None):
                    stage = "matrix.shared_sync"
                    queued, failures = self.managed.poll()
                    for room, error in failures.items():
                        trace_event("matrix.history_pending", room_id=room, error_type=type(error).__name__)
                    # The local journal schedules native callbacks. Its cursor is never used to consume a managed batch.
                    self.managed.schedule(queued)
                    self.scan_ready.set()
                    if self.managed.scan_confirmed and not self.handoff_scanned:
                        from worker_handoff import recovery_scanned
                        recovery_scanned()
                        self.handoff_scanned = True
                else:
                    cursor = self.journal.data["cursor"]
                    stage = "matrix.sync"
                    payload = self.matrix.sync(cursor or None, 1000)
                    stage = "matrix.events"
                    allowed_senders = None
                    if self.config.get("member", {}).get("role") == "manager":
                        allowed_senders = manager_allowed_senders(self.runtime_path, os.environ["AGENTTEAMS_MATRIX_DOMAIN"])
                    trusted = allowed_senders if allowed_senders is not None else self.agent_ids | {
                        self.config.get("team", {}).get("admin", {}).get("matrixUserId", "")
                    }
                    self.join_invites(payload, trusted)
                    # Matrix membership is the access boundary. Task rooms created
                    # by TeamHarness may not appear in the initial runtime.yaml.
                    for room_id in payload.get("rooms", {}).get("join", {}):
                        self.rooms.setdefault(room_id, "team")
                    if cursor:
                        self.matrix.fill_timeline_gaps(payload, cursor, self.rooms)
                    events = (
                        actionable_events(payload, self.rooms, self.user_id, self.agent_ids, allowed_senders)
                        if cursor
                        else []
                    )
                    completed = completed_turns(payload, self.user_id) if self.manager_receipts else ()
                    queued = []
                    for event in events:
                        if ((event["room_id"], event["event_id"]) in completed
                                or event["event_id"] in self.journal.data["events"]):
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
                    self.journal.ingest(queued, payload.get("next_batch", cursor), completed)
                    self.scan_ready.set()
                for received in queued:
                    trace_event("message.received", room_id=received["room_id"], event_id=received["event_id"], sender=received["sender"])
                if not self.last_sync:
                    trace_event("runtime.ready", evidence="matrix_sync")
                self.last_sync = time.time()
                if os.getenv("AGENTTEAMS_CONTROLLER_URL") and not self.reported_ready:
                    try:
                        subprocess.run(["agt", "worker", "report-ready", "--name",
                                        os.getenv("AGENTTEAMS_WORKER_CR_NAME", self.name)], check=True, timeout=60)
                    except (OSError, subprocess.SubprocessError) as error:
                        trace_event("readiness.report_failed", error_type=type(error).__name__, error=str(error)[:4096])
                    else:
                        self.reported_ready = True
            except Exception as error:
                detail = error.stderr if isinstance(error, subprocess.CalledProcessError) and error.stderr else str(error)
                trace_event("poll.failed", stage=stage, error_type=type(error).__name__, error=detail[:4096])
                print(
                    f"[agentteams-codex] Matrix poll failed: {type(error).__name__}",
                    flush=True,
                )
                self.stop.wait(2)

    def run_turn(self, event, record):
        trace_event("turn.begin", room_id=event["room_id"], event_id=event["event_id"])
        config = read_runtime(self.runtime_path)
        task_context, task_facts, task_output = prepare_turn(self.plugin, _role(config), event)
        saved = record.get("native_task_context") or {}
        if (saved.get("sourceEvent") == event["event_id"]
                and checkpoint_rows_present(self.home, saved.get("threadId"), saved.get("rows"))):
            task_context.update(sources=saved["sources"], inputRefreshPending=False)
            task_facts += "\n[Task input references]\n" + json.dumps([
                {key: source[key] for key in ("inputId", "eventId")} for source in saved["sources"]])
        model, instructions = prepare(config, self.home, self.workspace, self.plugin,
            trace_attributes=trace_attributes(config, event, task_context), task_scope=task_context)
        if task_facts:
            instructions += "\n\n" + task_facts
        delivered, acknowledgements = delivery_context(self.plugin, _role(config), event["room_id"],
            self.journal.data.setdefault("task_delivery_context", {}))
        if delivered:
            instructions += "\n\n" + delivered
            record["delivery_context_ack"] = acknowledgements
        working_directory = task_output or self.workspace
        credential = os.getenv("AGENTTEAMS_WORKER_GATEWAY_KEY") or config.get(
            "desired", {}
        ).get("model", {}).get("gatewayKey", "")
        environment = {
            **os.environ,
            "CODEX_GATEWAY_KEY": (config["desired"]["model"]["gatewayKey"] if managed_worker()
                                  else os.getenv("CODEX_GATEWAY_KEY") or credential),
            CONTEXT_ENV: json.dumps(task_context, ensure_ascii=False, default=str),
        }
        native_scope = NativeScope(self.home, self.plugin, task_context,
            {'kind': 'task', 'identity': event['recovery']} if event.get('recovery') else
            {'kind': 'message', 'identity': event['message_execution']} if event.get('message_execution') else None)
        scratch = Path((task_context.get('context') or {}).get('scratchDirectory') or self.workspace / '.scratch')
        scratch.mkdir(parents=True, exist_ok=True)
        environment.update({SCOPE_ENV: str(native_scope.path),
                            ADMISSION_COMMAND_ENV: json.dumps([sys.executable, '-m', 'agentteams_codex.admission']),
                            'TMPDIR': str(scratch)})
        thread_id = self.journal.data["rooms"].get(event.get("coordination_session", event["room_id"]))
        if event.get("recovery"):
            thread_id = event["recovery"].get("thread_id")
        if event.get("message_execution"):
            environment["AGENTTEAMS_CODEX_MESSAGE_EXECUTION"] = json.dumps(event["message_execution"])
            thread_id = event["thread_id"]
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
        trace_event("inference.begin", model=model, room_id=event["room_id"], event_id=event["event_id"], layer="agent_turn")
        with InputBarrier() as input_barrier, Client(working_directory, env={**environment, BARRIER_ENV: input_barrier.path},
                trace_context=(event.get("_task_protocol_meta") or {}).get("otel_carrier")) as client:
            if event.get("message_execution") or event.get("recovery") or any(task_context.get(key) for key in ("planning", "review", "completion")):
                require_hooks(client, working_directory)
            try:
                thread = client.open_thread(thread_id, working_directory, model, instructions)
            except CodexError:
                if not (event.get("recovery") or event.get("message_execution")) or not thread_id:
                    raise
                # A matching marker is insufficient when the native archive
                # was lost or the SDK has expired the session.
                thread = client.open_thread(None, working_directory, model, instructions)
            thread_id = thread["id"]
            self.journal.data["rooms"][event.get("coordination_session", event["room_id"])] = thread_id
            record["thread_id"] = thread_id
            self.journal.save()
            if getattr(self, "managed", None) and (event.get("shared_key") or event.get("recovery")):
                self.managed.commit_for_turn(client, thread_id, event)
                if event.get("message_execution"):
                    self.managed.remember_message(event, thread_id)
            recovered = completed_answer(thread, event["event_id"], terminal_only=bool(event.get("message_execution") or event.get("recovery")))
            if recovered is not None:
                return native_scope.presentation() or record.get("task_presentation") or recovered
            if record.get("status") == "running" and not (event.get("recovery") or event.get("message_execution")):
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
            self.checkpoint_input(client, thread_id, turn["id"], record)
            self.active = {
                "event": event,
                "thread_id": thread_id,
                "turn_id": turn["id"],
            }
            direct = self.direct_member(event)
            record["direct"] = direct
            if direct:
                self.execution_status(event, "running")
            redirects = []
            self.journal.save()
            answers = []
            deadline = time.monotonic() + int(
                os.getenv("CODEX_TASK_TIMEOUT_SECONDS", "3600")
            )
            tool_progress = ToolProgress(self.matrix, event, run_id(event))
            try:
                while not self.stop.is_set():
                    input_barrier.serve(client, thread_id, turn["id"],
                        lambda frame: self.checkpoint_refreshed_input(record, frame),
                        context=task_context, home=self.home)
                    self.checkpoint_input(client, thread_id, turn["id"], record)
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
                            if source.get("control_execution"):
                                self.managed.commit_for_turn(client, thread_id, source, active_turn_id=turn["id"])
                            if verb in {"/cancel", "/redirect"}:
                                client.control("turn/interrupt", thread_id, turn["id"])
                                if direct and source.get("execution_control"):
                                    self.execution_status(event, "stopping")
                                if verb == "/redirect":
                                    redirects.append(source)
                            elif verb == "/steer":
                                client.control(
                                    "turn/steer", thread_id, turn["id"], argument
                                )
                                if source.get("execution_control"):
                                    self.journal.data["events"][source["event_id"]]["status"] = "completed"
                                    self.journal.save()
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
                                    if answer.startswith('{"responses":'):
                                        responses = json.loads(answer)["responses"]
                                        if not isinstance(responses, list) or len(responses) != len(questions):
                                            raise ValueError("Answer every requested question exactly once")
                                        structured = {}
                                        for question in questions:
                                            matches = [item for item in responses if isinstance(item, dict) and item.get("id") == question["id"]]
                                            if len(matches) != 1:
                                                raise ValueError("Question id is missing or duplicated")
                                            response = matches[0]
                                            selected = response.get("selected", [])
                                            labels = {option["label"] for option in question.get("options", [])}
                                            if not isinstance(selected, list) or len(selected) > 1 or any(not isinstance(value, str) or value not in labels for value in selected):
                                                raise ValueError("Unknown question option")
                                            custom = response.get("custom", "")
                                            if not isinstance(custom, str) or (not selected and not custom.strip()):
                                                raise ValueError("Question answer is empty")
                                            structured[question["id"]] = {"answers": selected + ([custom.strip()] if custom.strip() else [])}
                                        result = {"answers": structured}
                                elif method in {
                                    "item/commandExecution/requestApproval",
                                    "item/fileChange/requestApproval",
                                }:
                                    if verb not in {"/approve", "/reject"}:
                                        raise ValueError("Use /approve REQUEST_ID or /reject REQUEST_ID")
                                    if answer and answer != f"{thread_id}:{turn['id']}":
                                        raise ValueError("The requested Codex approval belongs to another turn")
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
                                if method == "item/tool/requestUserInput" and request.get("_confirmation_id"):
                                    self.question_receipt(event, request, "answered")
                                if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
                                    self.reply(event, f"Codex approval {request_id}: {result['decision']}.",
                                        f"approval:{request_id}:resolved", {
                                            "version": 1, "runtime": "codex", "request_id": request_id,
                                            "context_id": f"{thread_id}:{turn['id']}",
                                            "status": "approved" if result["decision"] == "accept" else "rejected",
                                        })
                            if source.get("control_execution"):
                                self.managed.confirm_control(source, "native control accepted", retire=verb != "/redirect")
                            self.reply(source, "Codex control accepted.", "control")
                        except (ValueError, CodexError) as error:
                            if verb == "/redirect" or (verb == "/steer" and source.get("execution_control")):
                                # Completion can win the race with steering.
                                # Keep the accepted supplement as a normal
                                # follow-up in the same native thread.
                                pending = self.journal.data["events"][source["event_id"]]
                                pending["event"]["control"] = False
                                pending["status"] = "queued"
                                self.journal.save()
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
                            if method == "item/tool/requestUserInput":
                                confirmation_id = "codex-input-" + hashlib.sha256(
                                    f"{thread_id}:{turn['id']}:{request_id}".encode()).hexdigest()
                                message["_confirmation_id"] = confirmation_id
                                questions = [{
                                    "id": question["id"], "question": question["question"],
                                    "options": [{"id": option["label"], "label": option["label"],
                                                 "description": option.get("description", "")}
                                                for option in question.get("options", [])],
                                } for question in params.get("questions", [])]
                                self.matrix.send_content(event["room_id"], {
                                    "msgtype": "m.text", "body": "\n".join(question["question"] for question in questions),
                                    "org.agentteams.run": {"version": "1", "blocks": [{
                                        "type": "confirmation", "tool_name": "ask_human",
                                        "confirmation_id": confirmation_id, "questions": questions,
                                        "reply_format": "com.agentteams.answer", "status": "pending",
                                        "requested_of": event["sender"],
                                        "expires_at": int((time.time() + max(0, deadline - time.monotonic())) * 1000),
                                    }]},
                                }, f"codex:{event['event_id']}:request:{request_id}")
                                continue
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
                                    "reply_format": "com.agentteams.answer",
                                    "context_id": f"{thread_id}:{turn['id']}",
                                    "status": "pending",
                                    "expires_at": int((time.time() + max(0, deadline - time.monotonic())) * 1000),
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
                    if method in {"item/started", "item/completed"}:
                        item = params.get("item", {})
                        if item.get("type") in {"commandExecution", "mcpToolCall", "fileChange"}:
                            failed = bool(item.get("error")) or item.get("exitCode") not in (None, 0) or item.get("status") == "failed"
                            trace_event("tool.begin" if method == "item/started" else "tool.end",
                                        event_id=event["event_id"], room_id=event["room_id"],
                                        tool_type=item.get("type"), item_id=item.get("id"), status="failed" if failed else item.get("status"),
                                        error=item.get("error") or ((item.get("aggregatedOutput") or "")[:4096] if failed else None), exit_code=item.get("exitCode"))
                    if method in {"item/started", "item/completed"} and (show_tools or params.get("item", {}).get("type") in {"reasoning", "agentMessage"}):
                        try:
                            tool_progress.update(method, params.get("item", {}))
                        except Exception as error:
                            trace_event("progress.delivery_failed", event_id=event["event_id"], error_type=type(error).__name__)
                    if (
                        method == "turn/completed"
                        and params.get("turn", {}).get("id") == turn["id"]
                    ):
                        terminal = params["turn"]
                        native_scope.require_admitted()
                        trace_event("inference.end", event_id=event["event_id"], status=terminal.get("status"), error=terminal.get("error"), layer="agent_turn")
                        if terminal["status"] == "completed":
                            presentation = native_scope.presentation()
                            if presentation is not None:
                                record["task_presentation"] = presentation
                                self.journal.save()
                                return presentation
                            return (
                                "\n".join(filter(None, answers))
                                or "Codex completed the task."
                            )
                        if terminal["status"] == "interrupted":
                            record["stopped"] = True
                            if redirects:
                                combined = "继续原任务，保留已有工具结果，不要重复已完成的操作。\n" + event["body"]
                                for source in redirects:
                                    combined += "\n用户补充：" + source["body"]
                                    self.journal.data["events"][source["event_id"]]["status"] = "completed"
                                last = self.journal.data["events"][redirects[-1]["event_id"]]
                                sources = [*event.get("session_source_event_ids", [event["event_id"]]), *[source["event_id"] for source in redirects]]
                                last["event"] = {**last["event"], "control": False, "body": combined, "session_source_event_ids": list(dict.fromkeys(sources))}
                                last["status"] = "queued"
                                self.journal.save()
                            return "Codex execution was cancelled."
                        error = terminal.get("error") or {}
                        raise CodexError(error.get("message", "Codex turn failed"))
                client.control("turn/interrupt", thread_id, turn["id"])
                raise InterruptedError("Worker is shutting down")
            finally:
                self.active = None
                for request_id, request in list(self.pending_requests.items()):
                    if request.get("_confirmation_id"):
                        self.question_receipt(event, request, "expired" if time.monotonic() >= deadline else "cancelled")
                    if request["method"] in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
                        self.reply(event, f"Codex approval {request_id} expired.", f"approval:{request_id}:expired", {
                            "version": 1, "runtime": "codex", "request_id": request_id, "status": "expired",
                            "context_id": f"{thread_id}:{turn['id']}",
                        })
                self.pending_requests.clear()

    def attach_late_assignment(self, event):
        """Attach an assignment delivered after self-discovery without making a second turn."""
        matrix = self.managed.matrix
        key = event["shared_key"]
        native = matrix.native_input(key)
        item = native["input"]
        work = item.get("work") or {}
        room = item["room_id"]
        if (item.get("intent") != "task_notification" or work.get("kind") != "task"
                or room != event["room_id"] or event["event_id"] != item["event_id"]):
            raise RecoveryConflict("Late Matrix input no longer identifies the same Task")
        raw = {**item["input"], "room_id": room, "event_id": item["event_id"]}
        if self.managed.classify(room, raw) != ("task_notification", work["task_id"]):
            return False
        recovery = matrix.recovery
        task = recovery.current(work["task_id"], work["team_id"])
        if not task or task.get("room_id") != room:
            return False
        execution = task.get("execution", {})
        if execution.get("process_id") != recovery.process or execution.get("state") != "active":
            return False
        admission = f"recovery:{work['team_id']}/{work['task_id']}:{execution['generation']}"
        turn = self.journal.data["events"].get(admission)
        if not turn or turn["status"] != "completed":
            return False
        thread, view = task_thread(self.home, recovery, task, work)
        if not thread:
            return False
        config = read_runtime(self.runtime_path)
        model, instructions = prepare(config, self.home, self.workspace, self.plugin)
        with Client(self.workspace, env={**os.environ, "CODEX_GATEWAY_KEY": config["desired"]["model"]["gatewayKey"]}) as client:
            actual = client.open_thread(thread, self.workspace, model, instructions)
            if actual["id"] != thread:
                raise RecoveryConflict("Late assignment resolved to a different Codex thread")
            self.managed.commit_for_turn(client, thread, {"room_id": room, "recovery": work})
        digest = rollout_digest(self.home, thread)
        if digest:
            WorkerSessions("codex", recovery=recovery).remember(work, view, execution, thread, digest, int(time.time()) + 86400)
        return True

    def process(self, event_id, record):
        event = record["event"]
        if event.get("recovery"):
            return self.process_recovered_task(event_id, record)
        if event.get("coordination_session", "").startswith("coordination-task:"):
            from task_recovery import enabled
            if enabled():
                self.recover_tasks(event["coordination_session"].split(":", 2)[1])
                if event.get("shared_key") and self.managed:
                    if not self.attach_late_assignment(event):
                        return
                record["status"] = "superseded"
                self.journal.save()
                return
        if event.get("control"):
            record["status"] = "completed"
            self.journal.save()
            return
        if event.get("shared_key") and getattr(self, "managed", None):
            return self.managed.message_turn(record)
        if "answer" not in record:
            record.setdefault("outbox_before", snapshot_outbox(self.workspace))
            self.journal.save()
            try:
                answer = self.run_turn(event, record)
            except Exception as error:
                # Preserve the terminal error, so Matrix delivery retries do not
                # execute the model/tool turn again.
                trace_event("turn.failed", event_id=event_id, error_type=type(error).__name__, error=str(error)[:4096])
                answer = f"Codex execution failed ({type(error).__name__}): {error}"
                record["error"] = type(error).__name__
            self.persist_codex()
            outputs = changed_outbox_files(self.workspace, record["outbox_before"])
            sync_output_paths(self.name, self.workspace, outputs)
            record.update(answer=answer, outputs=outputs, status="answer_ready")
            self.journal.save()
        restore_output_paths(self.name, self.workspace, record["outputs"])
        if (event.get("_task_protocol_meta") or {}).get("teamharness_coordination", {}).get("kind") == "assistant_digest":
            # Controller's sent/skipped receipt is the only user-facing outcome.
            record["status"] = "completed"
            self.journal.save()
            return
        source = source_agent_reply_user_id(event, self.agent_ids, self.user_id)
        content = {"msgtype": "m.text", "body": record["answer"],
                   "m.relates_to": {"m.in_reply_to": {"event_id": event_id}},
                   "org.agentteams.run_id": run_id(event)}
        if event.get("coordination_session"):
            content["org.agentteams.progress_only"] = True
        else:
            mentions = reply_mention_user_ids(event, record["answer"], self.agent_ids, self.user_id)
            if mentions:
                content["m.mentions"] = {"user_ids": mentions}
        if source:
            content["com.agentteams.auto_source_mention"] = True
        message_id = self.matrix.send_content(event["room_id"], content, f"codex:{event_id}:reply")
        if record.get("direct"):
            self.execution_status(event, "stopped" if record.get("stopped") else "failed" if record.get("error") else "completed", message_id)
        send_output_paths(
            self.matrix,
            event["room_id"],
            event_id,
            self.workspace,
            record["outputs"],
            [source] if source else [],
            bool(source),
            **({"progress_only": True} if event.get("coordination_session") else {}),
        )
        if getattr(self, "manager_receipts", None) and message_id and not record.get("error") and not record.get("stopped"):
            self.manager_receipts.enqueue(event["room_id"], event_id, "completed")
        record["status"] = "completed"
        trace_event("message.sent", room_id=event["room_id"], event_id=event_id, reply_event_id=message_id)
        # Retain bounded replay protection; pending events are never pruned.
        completed = [
            key
            for key, value in self.journal.data["events"].items()
            if value["status"] == "completed"
        ]
        for old in completed[:-2000]:
            del self.journal.data["events"][old]
        self.journal.save()

    def process_recovered_task(self, event_id, record):
        from task_recovery import Recovery, RecoveryConflict
        recovery = Recovery(device_delivery=True)
        event = record["event"]
        identity = event["recovery"]
        task_id, team = identity["task_id"], identity["team_id"]
        task = recovery.current(task_id, team)
        if task is None:
            record["status"] = "superseded"
            self.journal.save()
            return
        execution = task["execution"]
        if (execution["runtime"] != "codex" or execution["process_id"] != recovery.process
                or any(identity.get(k) != execution[k] for k in ("instance_id", "generation", "process_id"))):
            record["status"] = "superseded"
            self.journal.save()
            return
        if execution["state"] == "needs_verification":
            record["status"] = "superseded"
            self.journal.save()
            return
        cp = execution.get("checkpoint") or {}
        if cp.get("delivery") == "delivered" and (task["status"] == "submitted" or record.get("reply_txn") == cp.get("reply_txn")):
            record["status"] = "completed"
            self.journal.save()
            return
        work = {"kind": "task", "task_id": task_id, "team_id": team}
        selection = None
        if cp.get("delivery") != "pending":
            if execution["state"] != "active":
                raise RecoveryConflict("Codex Task execution is not active")
            if record.get("status") == "running":
                recovery.interrupted(task_id, team)
                return
            identity["thread_id"], selection = task_thread(self.home, recovery, task, work)
            event["body"] = recovery.resume_text({**task, "team_id": team, "spec": identity["spec"]})
            try:
                answer = self.run_turn(event, record)
            except BaseException:
                if record.get("status") == "running":
                    recovery.interrupted(task_id, team)
                raise
            self.persist_codex()
            saved, answer = recovery.prepare_reply(task_id, team, answer)
            cp = saved["execution"]["checkpoint"]
            if cp.get("delivery") == "delivered":
                record["status"] = "completed"
                self.journal.save()
                return
        else:
            # The Controller revalidates the saved bytes on a repeated claim.
            snapshot = recovery.discover()
            entry = next(e for e in snapshot["tasks"] if e["task"]["task_id"] == task_id and e["team_id"] == team)
            saved = recovery.claim(snapshot, entry)
            cp = saved["execution"]["checkpoint"]
            answer = saved["saved_result"]
        record["reply_txn"] = cp["reply_txn"]
        self.journal.save()
        message_id = self.deliver_task(recovery, task_id, team, event["room_id"], saved["execution"], answer)
        saved = recovery.delivered(task_id, team, message_id)
        record["status"] = "completed"
        self.journal.save()
        if selection and saved.get("task", {}).get("status") != "submitted":
            native_ref = record.get("thread_id")
            digest = rollout_digest(self.home, native_ref)
            if digest:
                try:
                    sessions = WorkerSessions("codex", recovery=recovery)
                    view = sessions.load(work)
                    if (view["config_generation"] == selection["config_generation"]
                            and view["checkpoint_digest"] == recovery.digest(saved["execution"]["checkpoint"])):
                        sessions.remember(work, view, identity, native_ref, digest, int(time.time()) + 86400)
                except (RecoveryConflict, RecoveryUnavailable) as exc:
                    trace_event("task.recovery.cache_skipped", task_id=task_id, error_type=type(exc).__name__)

    def run(self):
        # Discover/claim before polling or accepting new Matrix work. A failed
        # recovery leaves the process blocked and visible to its supervisor.
        self.recover_tasks()
        threading.Thread(target=self.poll, daemon=True).start()
        if self.managed or self.manager_receipts:
            while not self.stop.is_set() and not self.scan_ready.wait(0.5):
                continue
        next_discovery = time.monotonic() + 5
        while not self.stop.is_set():
            if self.managed and time.monotonic() >= next_discovery:
                try:
                    self.recover_tasks()
                except (RecoveryConflict, RecoveryUnavailable) as error:
                    trace_event("task.scan_pending", error_type=type(error).__name__)
                next_discovery = time.monotonic() + 5
            pending = [
                (key, value)
                for key, value in list(self.journal.data["events"].items())
                if value["status"] not in {"completed", "superseded"}
                and (not self.managed or value["event"].get("recovery") or key in self.managed.available)
            ]
            for event_id, record in pending:
                if self.stop.is_set():
                    break
                try:
                    self.process(event_id, record)
                except Exception as error:
                    detail = error.stderr if isinstance(error, subprocess.CalledProcessError) and error.stderr else str(error)
                    trace_event("delivery.failed", event_id=event_id, error_type=type(error).__name__, error=detail[:4096])
                    print(
                        f"[agentteams-codex] delivery pending: {type(error).__name__}",
                        flush=True,
                    )
                    self.stop.wait(2)
                    if not self.managed:
                        break
            self.stop.wait(0.2)


def main():
    if os.getenv("AGENTTEAMS_MATRIX_E2EE", "0").lower() in {"1", "true"}:
        raise RuntimeError("Codex bridge does not support Matrix E2EE")
    bridge = Bridge()
    if not bridge.managed:
        from agentteams_receipts import serve
        receipts = bridge.manager_receipts or Receipts(bridge.home.parent)
        threading.Thread(target=serve, args=(receipts, "codex"), daemon=True).start()

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
