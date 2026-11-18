"""Bind Codex Matrix admission to the Worker ledger and the current room membership."""
import hashlib
import json
import os
import time
from urllib.parse import quote

from agentteams_receipts import Receipts
from matrix_channel import matrix_transaction_id
from task_recovery import Recovery, RecoveryConflict
from worker_matrix_delivery import confirm_bound
from worker_matrix_raw import publish_raw_saved
from worker_sessions import WorkerSessions
from worker_matrix_raw import RawWorkerSync
from worker_matrix_state import WorkerMatrix, matrix_event_key

from .native_input import commit_source, rollout_digest


class ManagedMatrix:
    def __init__(self, bridge, decode):
        self.bridge = bridge
        self.decode = decode
        self.matrix = WorkerMatrix("codex", device_delivery=True)
        self.receipts = Receipts(bridge.home.parent)
        self.joined = set()
        self.available = set()
        self.scan_confirmed = False
        self.raw = RawWorkerSync(self.matrix, bridge.matrix, bridge.user_id, self.classify, self.observe)

    def observe(self, payload):
        bridge = self.bridge
        bridge.join_invites(payload, bridge.agent_ids | {bridge.config.get("team", {}).get("admin", {}).get("matrixUserId", "")})
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
        deny_key = "groupDenyExtra" if bridge.rooms[room] == "team" else "dmDenyExtra"
        domain = os.environ["AGENTTEAMS_MATRIX_DOMAIN"]
        denied = {value if value == "*" or (value.startswith("@") and ":" in value)
                  else f"@{value.lstrip('@')}:{domain}" for value in policy.get(deny_key, [])}
        if "*" in denied or raw.get("sender") in denied:
            return None
        payload = {"rooms": {"join": {room: {"timeline": {"events": [raw]}}}}}
        events = self.decode(payload, bridge.rooms, bridge.user_id, bridge.agent_ids)
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

    def commit_for_turn(self, client, thread_id, event):
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
            commit_source(client, self.bridge.home, thread_id, matrix, key, view, self.bridge.persist_codex)
            self.receipts.enqueue(room, item["event_id"], "received")
            self.receipts.committed([item["event_id"]])

    def history(self, room, source):
        path = f"/_matrix/client/v3/rooms/{quote(room, safe='')}/context/{quote(source, safe='')}?limit=20"
        page = self.bridge.matrix.request("GET", path)
        if (page.get("event") or {}).get("event_id") != source:
            raise RecoveryConflict("Cannot establish Codex room context at the source event")
        history = []
        for event in reversed(page.get("events_before", [])):
            if event.get("room_id", room) != room or event.get("type") != "m.room.message":
                continue
            content = event.get("content", {})
            if content.get("msgtype") != "m.text":
                continue
            if event.get("sender") == self.bridge.user_id:
                parent = content.get("m.relates_to", {}).get("m.in_reply_to", {}).get("event_id")
                prior = self.matrix.input(matrix_event_key(room, parent)) if parent else None
                cp = ((prior or {}).get("execution") or {}).get("checkpoint") or {}
                if (cp.get("reply_event") != event["event_id"]
                        or (cp.get("result") or {}).get("sha256") != hashlib.sha256(content["body"].encode()).hexdigest()):
                    continue
                source_event = {**prior["input"], "event_id": parent, "room_id": room}
            else:
                prior = self.matrix.input(matrix_event_key(room, event["event_id"]))
                if (not prior or prior["intent"] != "message" or prior["input"]["sender"] != event.get("sender")
                        or prior["input"]["content"] != content):
                    continue
                source_event = event
            if self.event(room, source_event):
                history.append({"sender": event["sender"], "event_id": event["event_id"], "text": content["body"]})
        return history

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
            if ref is None:
                history = self.history(event["room_id"], event["event_id"])
                if history:
                    current["body"] += "\nEarlier Matrix messages:\n" + json.dumps(history, ensure_ascii=False)
            if claim["execution"].get("checkpoint"):
                current["body"] += "\n" + claim["resume_text"]
            matrix.message_current(identity)
            answer = self.bridge.run_turn(current, record)
            self.bridge.persist_codex()
            claim = matrix.message_save_reply(identity, answer)
        if claim["recovery"] != "deliver":
            return
        cp = claim["execution"]["checkpoint"]
        def bind(next_cp):
            saved = matrix.message_checkpoint(identity, next_cp)
            confirm_bound(saved["execution"], identity, "codex", next_cp)
        room = event["room_id"]
        text = claim["saved_result"]
        content = {"m.relates_to": {"m.in_reply_to": {"event_id": event["event_id"]}}}
        message_id = publish_raw_saved(self.bridge.matrix, room, text, cp, self.bridge.user_id, bind,
                                       content=content, legacy_transaction_id=matrix_transaction_id)
        matrix.message_delivered(identity, message_id)
        record["status"] = "completed"
        self.bridge.journal.save()
