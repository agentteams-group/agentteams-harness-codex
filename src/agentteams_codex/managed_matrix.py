"""Bind Codex Matrix admission to the Worker ledger and the current room membership."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

from agentteams_receipts import Receipts
from matrix_channel import matrix_transaction_id
from matrix_bridge import (changed_outbox_files, reply_mention_user_ids, snapshot_outbox,
                           source_agent_reply_user_id, workspace_output_path)
from task_recovery import Recovery, RecoveryConflict
from worker_matrix_delivery import confirm_bound
from worker_matrix_raw import RawWorkerSync, publish_raw_saved, raw_room_history, require_unencrypted_room
from .tool_progress import run_id
from worker_sessions import WorkerSessions
from worker_matrix_state import WorkerMatrix, matrix_event_key

from .native_input import commit_source, rollout_digest, rollout_path


class ManagedMatrix:
    def __init__(self, bridge, decode):
        self.bridge = bridge
        self.decode = decode
        self.matrix = WorkerMatrix("codex", device_delivery=True)
        self.receipts = Receipts(bridge.home.parent)
        self.joined = set()
        self.available = set()
        self.scan_confirmed = False
        self.raw = RawWorkerSync(self.matrix, bridge.matrix, bridge.user_id, self.classify, self.observe,
                                 event_types=("m.room.message", "org.agentteams.coordination"))

    def observe(self, payload):
        bridge = self.bridge
        dm = bridge.config.get("matrix", {}).get("transport", {}).get("dm") or {}
        direct_senders = set(dm.get("allowFrom") or []) if dm.get("policy") != "disabled" else set()
        bridge.join_invites(payload, bridge.agent_ids | direct_senders | {bridge.config.get("team", {}).get("admin", {}).get("matrixUserId", "")})
        self.joined = set(bridge.matrix.request("GET", "/_matrix/client/v3/joined_rooms")["joined_rooms"])
        for room in self.joined:
            bridge.rooms.setdefault(room, "team")
        for room, data in payload.get("rooms", {}).get("join", {}).items():
            if room not in self.joined:
                continue
            for event in data.get("timeline", {}).get("events", []):
                if event.get("type") == "m.room.message" and event.get("sender") != bridge.user_id:
                    self.receipts.enqueue(room, event.get("event_id", ""), "received")
        return self.joined

    def event(self, room, raw):
        if room not in self.joined:
            return None
        bridge = self.bridge
        policy = bridge.config.get("desired", {}).get("channelPolicy", {})
        personal = bridge.rooms[room] == "personal"
        deny_key = "dmDenyExtra" if personal else "groupDenyExtra"
        domain = os.environ["AGENTTEAMS_MATRIX_DOMAIN"]
        denied = {value if value == "*" or (value.startswith("@") and ":" in value)
                  else f"@{value.lstrip('@')}:{domain}" for value in policy.get(deny_key, [])}
        if "*" in denied or raw.get("sender") in denied:
            return None
        transport = bridge.config.get("matrix", {}).get("transport", {})
        dm = transport.get("dm") or {}
        sender_policy = dm.get("policy") if personal else transport.get("groupPolicy")
        allow = (dm.get("allowFrom") if personal else transport.get("groupAllowFrom")) or []
        allowed = None
        if sender_policy == "disabled":
            allowed = set()
        elif sender_policy and sender_policy != "open" and "*" not in allow:
            allowed = set(allow)
        payload = {"rooms": {"join": {room: {"timeline": {"events": [raw]}}}}}
        events = self.decode(payload, bridge.rooms, bridge.user_id, bridge.agent_ids, allowed)
        return events[0] if events else None

    def classify(self, room, raw):
        event = self.event(room, raw)
        if event:
            session = event.get("coordination_session", "")
            if session.startswith("coordination-task:"):
                return "task_notification", session.split(":", 2)[1]
            return "message", None
        return None

    def control(self, event):
        control = event.get("execution_control")
        verb = event["body"].partition(" ")[0]
        if control is None and "answer" not in event and verb not in {"/cancel", "/steer", "/approve", "/reject", "/answer"}:
            return False
        active = self.bridge.active or {}
        if (isinstance(control, dict) and control.get("action") == "steer"
                and control.get("run_id") != active.get("event", {}).get("event_id")):
            return False  # A late supplement remains ordinary work.
        key = event["shared_key"]
        claim = self.matrix.message_claim(key)
        if claim["recovery"] != "resume":
            return True
        identity = claim["identity"]
        call = {"id": event["event_id"], "name": "codex.control", "arguments": {
            "room": event["room_id"], "control": control, "verb": verb}}
        op = "tools:" + Recovery.digest([key, [call]])
        if op in ((claim["execution"].get("checkpoint") or {}).get("completed_steps") or []):
            return False
        op = self.matrix.message_begin_tools(identity, [call])
        event["control_execution"] = (identity, op)
        consumed = self.bridge.handle_control(event)
        if not event.pop("awaiting_native_control", False):
            self.confirm_control(event, "no native action", retire=consumed)
        return consumed

    def confirm_control(self, event, result, *, retire):
        identity, operation = event["control_execution"]
        self.matrix.message_finish_tools(identity, operation, [{"id": event["event_id"], "content": result}])
        if retire:
            self.matrix.message_cancel(identity)

    def poll(self):
        self.raw.client = self.bridge.matrix
        snapshot, failures = self.raw.poll()
        # The raw poll verified the bound Worker's account and device. Flush
        # through that same transport, including after credential rotation.
        self.receipts.flush(self.raw.client.request)
        events = []
        for key in snapshot["state"]["pending"]:
            item = self.matrix.input(key)
            if not item:
                raise RecoveryConflict("Missing retained Matrix input")
            room = item["room_id"]
            if room not in self.joined or snapshot["state"]["rooms"].get(room, {}).get("gaps"):
                continue
            self.raw.require_room(room)
            raw = {**item["input"], "room_id": room, "event_id": item["event_id"]}
            event = self.event(room, raw)
            if not event:
                continue
            if item["intent"] == "task_notification" and item.get("native_commit"):
                continue
            if item["intent"] == "message":
                execution = item.get("execution") or {}
                cp = execution.get("checkpoint") or {}
                if (execution.get("state") == "cancelled" or cp.get("delivery") == "delivered"
                        or (cp.get("operation") or {}).get("state") in {"unknown", "started"}):
                    continue
            if item["intent"] in {"message", "task_notification"}:
                event = {**event, "shared_key": key}
                if item["intent"] == "message" and self.control(event):
                    continue
                events.append(event)
        self.available = {event["event_id"] for event in events}
        if not failures and not any(room.get("gaps") for room in snapshot["state"]["rooms"].values()):
            self.scan_confirmed = True
        return events, failures

    def schedule(self, events):
        journal = self.bridge.journal
        with journal.lock:
            changed = False
            for event in events:
                key = event["event_id"]
                old = journal.data["events"].get(key)
                if old and old["event"].get("shared_key"):
                    continue
                record = {"event": event, "status": "queued"}
                if old and old["status"] != "queued":
                    # The retained legacy entry is evidence of a possible native
                    # effect. Never use its local completion as shared authority.
                    record["legacy_status"] = old["status"]
                journal.data["events"][key] = record
                changed = True
            if changed:
                journal.save()

    def commit_for_turn(self, client, thread_id, event, *, active_turn_id=None):
        matrix = self.matrix
        room = event["room_id"]
        if room not in self.joined:
            raise RecoveryConflict("Worker left the Matrix room")
        work = event.get("recovery")
        if work:
            snapshot = matrix.load()
            if snapshot["state"]["rooms"].get(room, {}).get("gaps"):
                return
            keys = snapshot["state"]["pending"]
        else:
            keys = [event["shared_key"]]
        for key in keys:
            view = matrix.native_input(key)
            item = view["input"]
            if item["room_id"] != room:
                continue
            if work:
                wanted = {"kind": "task", "team_id": work["team_id"], "task_id": work["task_id"]}
                if item.get("intent") != "task_notification" or item.get("work") != wanted or item.get("native_commit"):
                    continue
                source = {**item["input"], "room_id": room, "event_id": item["event_id"]}
                if self.classify(room, source) != ("task_notification", work["task_id"]):
                    continue
            elif key != matrix_event_key(room, event["event_id"]):
                raise RecoveryConflict("Retained Matrix input does not match Codex turn")
            commit_source(client, self.bridge.home, thread_id, matrix, key, view, self.bridge.persist_codex,
                          active_turn_id=active_turn_id)
            self.receipts.enqueue(room, item["event_id"], "received")
            self.receipts.committed([item["event_id"]])

    def remember_message(self, event, thread_id):
        identity = event["message_execution"]
        work = {"kind": "message", "event_key": identity["event_key"]}
        sessions = WorkerSessions("codex", recovery=self.matrix.recovery)
        view = sessions.load(work)
        if (view["checkpoint_digest"] == event["checkpoint_digest"]
                and view["config_generation"] == event["config_generation"]):
            digest = rollout_digest(self.bridge.home, thread_id)
            if digest:
                sessions.remember(work, view, identity, thread_id, digest, int(time.time()) + 86400)

    def save_outputs(self, identity, event, answer, outputs):
        """Save immutable bytes before the first upload. Ambiguous sends stay fenced."""
        bridge = self.bridge
        if not outputs:
            return self.matrix.message_save_reply(identity, answer)
        room, event_id = event["room_id"], event["event_id"]
        source = source_agent_reply_user_id(event, bridge.agent_ids, bridge.user_id)
        client = bridge.matrix
        account = client.request("GET", "/_matrix/client/v3/account/whoami")
        if account.get("user_id") != bridge.user_id or not account.get("device_id"):
            raise RecoveryConflict("Matrix attachment credential belongs to a different Worker")
        require_unencrypted_room(client, room)
        with tempfile.TemporaryDirectory() as directory:
            files, calls = [], []
            for relative in outputs:
                original = workspace_output_path(bridge.workspace, relative)
                path = Path(directory) / str(len(files)) / original.name
                path.parent.mkdir()
                shutil.copyfile(original, path)
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                files.append(path)
                calls.append({"id": relative, "name": "matrix.send_file", "arguments": {"room": room, "sha256": digest}})
            operation = self.matrix.message_begin_tools(identity, calls, reply=answer,
                                                        output_bytes=(p.read_bytes() for p in files))
            def check_room():
                self.matrix.message_pending_tools(identity, operation)
                require_unencrypted_room(client, room)
            results = []
            for call, path in zip(calls, files):
                check_room()
                sent = client.send_file(room, path, event_id, f"{event_id}:file:{call['id']}",
                                        [source] if source else [], bool(source), before_send=check_room)
                if not isinstance(sent, str) or not sent.startswith("$") or len(sent) == 1:
                    raise RecoveryConflict("Matrix media delivery requires verification")
                results.append({"id": call["id"], "content": {**call["arguments"], "event_id": sent}})
            return self.matrix.message_finish_tools(identity, operation, results, deliver_result=True)

    def task_room_session(self, room):
        # Only locally claimed Task assignments bind a room to a Worker thread.
        # Historical journals already contain these records.
        for record in reversed(list(self.bridge.journal.data["events"].values())):
            event = record["event"]
            work = event.get("recovery")
            if not work or event["room_id"] != room or event["sender"] != self.bridge.user_id:
                continue
            thread = record.get("thread_id")
            path = rollout_path(self.bridge.home, thread)
            if not path:
                return None
            with path.open("rb") as stream:
                cwd = Path(json.loads(stream.readline())["payload"]["cwd"])
            if cwd.is_dir() and any(p.name == work["task_id"] and p.parent.name == "tasks"
                                    for p in (cwd, *cwd.parents)):
                return thread, cwd
            return None

    def message_turn(self, record):
        matrix = self.matrix
        event = record["event"]
        key = event["shared_key"]
        entry = matrix.message(key)
        if entry["recovery"] in {"verify", "backfill"}:
            return
        claim = matrix.message_claim(key)
        identity = claim["identity"]
        if claim["recovery"] == "resume":
            if record.get("legacy_status") and not claim["execution"].get("checkpoint"):
                matrix.message_checkpoint(identity, {"version": 1, "operation": {"state": "unknown",
                    "id": "legacy-codex:" + key, "evidence": "Previous Codex admission: " + record["legacy_status"]}})
                return
            work = {"kind": "message", "event_key": key}
            sessions = WorkerSessions("codex", recovery=matrix.recovery)
            view = sessions.load(work)
            if view["generation"] != identity["generation"] or view["checkpoint_digest"] != claim["checkpoint_digest"]:
                raise RecoveryConflict("Matrix work changed before native session selection")
            ref = (view.get("cache") or {}).get("native_ref")
            if not sessions.usable(view, ref, rollout_digest(self.bridge.home, ref)):
                ref = None
            current = {**event, "message_execution": identity, "thread_id": ref,
                       "checkpoint_digest": claim["checkpoint_digest"], "config_generation": view["config_generation"]}
            if not event.get("coordination_session") and not event.get("_task_protocol_meta"):
                session = self.task_room_session(event["room_id"])
                if session:
                    task_ref, current["room_workdir"] = session
                    if ref is None and not claim["execution"].get("checkpoint"):
                        current["thread_id"] = task_ref
            if current["thread_id"] is None:
                history = raw_room_history(self.bridge.matrix, matrix, self.bridge.user_id, event["room_id"],
                                           event["event_id"], self.event)
                if history:
                    current["body"] += "\nEarlier Matrix messages:\n" + json.dumps(history, ensure_ascii=False)
            if claim["execution"].get("checkpoint"):
                current["body"] += "\n" + claim["resume_text"]
            matrix.message_current(identity)
            if "outbox_before" not in record:
                record["outbox_before"] = snapshot_outbox(self.bridge.workspace)
                self.bridge.journal.save()
            answer = self.bridge.run_turn(current, record)
            self.bridge.persist_codex()
            if record.get("stopped"):
                matrix.message_cancel(identity)
                record["status"] = "completed"
                self.bridge.journal.save()
                if record.get("direct"):
                    self.bridge.execution_status(event, "stopped")
                return
            if (event.get("_task_protocol_meta") or {}).get("teamharness_coordination", {}).get("kind") == "assistant_digest":
                matrix.message_cancel(identity)
                record["status"] = "completed"
                self.bridge.journal.save()
                return
            outputs = changed_outbox_files(self.bridge.workspace, record["outbox_before"])
            claim = self.save_outputs(identity, event, answer, outputs)
        if claim["recovery"] != "deliver":
            return
        if (event.get("_task_protocol_meta") or {}).get("teamharness_coordination", {}).get("kind") == "assistant_digest":
            matrix.message_cancel(identity)
            record["status"] = "completed"
            self.bridge.journal.save()
            return
        if record.get("task_presentation") == "":
            # The canonical receipt stays in durable execution/native history.
            # A silent control phase produces no Matrix chat event at all.
            matrix.message_complete(identity)
            record["status"] = "completed"
            self.bridge.journal.save()
            return
        cp = claim["execution"]["checkpoint"]
        def bind(next_cp):
            saved = matrix.message_checkpoint(identity, next_cp)
            confirm_bound(saved["execution"], identity, "codex", next_cp)
        room = event["room_id"]
        text = claim["saved_result"]
        content = {"m.relates_to": {"m.in_reply_to": {"event_id": event["event_id"]}},
                   "org.agentteams.run_id": run_id(event)}
        if isinstance(record.get("task_presentation"), str):
            # Business replies use their durable business body; control-only
            # phases have completed without a Matrix message.
            content["org.agentteams.progress_only"] = True
            content["org.agentteams.run"] = {"version": "1", "run_id": run_id(event),
                "blocks": [{"type": "text", "text": record["task_presentation"]}]}
        source = source_agent_reply_user_id(event, self.bridge.agent_ids, self.bridge.user_id)
        mentions = reply_mention_user_ids(event, text, self.bridge.agent_ids, self.bridge.user_id)
        # The registered Manager is an allowed private sender, but is not a
        # member of the execution Team roster. Direct replies still address it.
        sender = event.get("sender")
        allowed = self.bridge.config.get("matrix", {}).get("transport", {}).get("dm", {}).get("allowFrom", [])
        if (not source and self.bridge.rooms.get(room) == "personal" and sender != self.bridge.user_id
                and sender in allowed and event.get("auto_source_mention") is not True):
            source = sender
            mentions = sorted(set(mentions) | {sender})
        if source:
            content["com.agentteams.auto_source_mention"] = True
        if event.get("coordination_session"):
            content["org.agentteams.progress_only"] = True
        elif mentions:
            content["m.mentions"] = {"user_ids": mentions}
        message_id = publish_raw_saved(self.bridge.matrix, room, text, cp, self.bridge.user_id, bind,
                                       content=content, legacy_transaction_id=matrix_transaction_id)
        matrix.message_delivered(identity, message_id)
        if record.get("direct"):
            self.bridge.execution_status(event, "stopped" if record.get("stopped") else "completed", message_id)
        record["status"] = "completed"
        self.bridge.journal.save()
