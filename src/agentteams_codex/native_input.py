"""Codex rollout and input persistence at the Worker recovery boundary."""
import hashlib
import json
import time
import uuid

from task_recovery import RecoveryConflict, RecoveryUnavailable


def rollout_path(home, thread_id):
    """Resolve an actual Codex rollout with its native session identity."""
    if not thread_id:
        return None
    try:
        if str(uuid.UUID(thread_id)) != thread_id:
            return None
    except (ValueError, TypeError, AttributeError):
        return None
    files = list((home / "sessions").rglob(f"rollout-*-{thread_id}.jsonl"))
    if len(files) != 1:
        return None
    try:
        with files[0].open("rb") as rollout:
            meta = json.loads(rollout.readline())
        if meta.get("type") == "session_meta" and meta.get("payload", {}).get("id") == thread_id:
            return files[0]
    except (OSError, ValueError):
        pass
    return None


def rollout_digest(home, thread_id):
    path = rollout_path(home, thread_id)
    if path:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    return None


def saved_message(home, thread_id, role, text):
    path = rollout_path(home, thread_id)
    if path:
        with path.open("rb") as stream:
            for line in stream:
                if not line.endswith(b"\n"):
                    continue  # The native writer has not completed this row.
                row = json.loads(line)
                item = row.get("payload", {})
                if (row.get("type") == "response_item" and item.get("type") == "message"
                        and item.get("role") == role and item.get("content") == [{"type": "input_text", "text": text}]):
                    return hashlib.sha256(line).hexdigest()
    return None


def saved_input(home, thread_id, text):
    return saved_message(home, thread_id, "user", text)


def commit_source(client, home, thread_id, matrix, key, view, persist):
    """Persist the exact original source without requesting an extra model turn."""
    entry = view["input"]
    source = {"room_id": entry["room_id"], "event_id": entry["event_id"], **entry["input"]}
    text = "Original Matrix input\n" + json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = saved_input(home, thread_id, text)
    if not digest:
        client.request("thread/inject_items", {"threadId": thread_id, "items": [{"type": "message", "role": "user",
                           "content": [{"type": "input_text", "text": text}]}]})
        # The RPC accepted the item. Wait for Codex's native writer before
        # publishing a receipt or admitting inference.
        for _ in range(40):
            digest = saved_input(home, thread_id, text)
            if digest:
                break
            time.sleep(0.05)
        if not digest:
            raise RecoveryUnavailable("Codex did not persist the original Matrix input")
    if not matrix.native_current(view, thread_id, digest):
        persist(include_workspace=False)
        view = matrix.native_committed(key, view, thread_id, digest)
        if not matrix.native_current(view, thread_id, digest):
            raise RecoveryConflict("Codex native input belongs to a different Worker start")
    return digest


def checkpoint_rows_present(home, thread_id, digests):
    """Verify owned native row receipts without interpreting User message bodies."""
    if not digests:
        return False
    path = rollout_path(home, thread_id)
    if path is None:
        return False
    with path.open('rb') as stream:
        present = {hashlib.sha256(row).hexdigest() for row in stream if row.endswith(b'\n')}
    return set(digests).issubset(present)
