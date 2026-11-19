"""Run inside the packaged image. Persist original Matrix input without model inference."""
import os
from pathlib import Path
import tempfile
from unittest.mock import Mock

from agentteams_codex.client import Client
from agentteams_codex.native_input import commit_source, rollout_digest
from agentteams_codex.tool_hook import require_hooks


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    home = root / ".codex"
    home.mkdir()
    (home / "config.toml").write_text('model = "fixture"\nmodel_provider = "fixture"\n'
        '[model_providers.fixture]\nname = "offline fixture"\nbase_url = "http://127.0.0.1:1/v1"\n'
        'env_key = "CODEX_GATEWAY_KEY"\nwire_api = "responses"\n')
    matrix = Mock()
    input_ = {"room_id": "!room:test", "event_id": "$late", "input": {
        "sender": "@leader:test", "type": "m.room.message", "origin_server_ts": 10,
        "content": {"body": "Continue the same task"}}}
    view = {"input": input_}
    persist = Mock()
    matrix.native_current.side_effect = lambda state, ref, digest: state.get("proof") == (ref, digest)
    matrix.native_committed.side_effect = lambda key, state, ref, digest: {**state, "proof": (ref, digest)}
    env = {**os.environ, "CODEX_HOME": str(home), "CODEX_GATEWAY_KEY": "offline-fixture"}
    with Client(root, env=env) as client:
        require_hooks(client, root)
        thread_id = client.open_thread(None, root, "fixture", "Fixture")["id"]
        digest = commit_source(client, home, thread_id, matrix, "key", view, persist)
        persisted = rollout_digest(home, thread_id)
        assert persisted and len(digest) == 64
        assert commit_source(client, home, thread_id, matrix, "key", {**view, "proof": (thread_id, digest)}, persist) == digest
        assert rollout_digest(home, thread_id) == persisted
        assert client.request("thread/read", {"threadId": thread_id, "includeTurns": True})["thread"]["turns"] == []
    persist.assert_called_once_with(include_workspace=False)
    matrix.native_committed.assert_called_once()
    print("PASS: saved original Matrix row and native receipt once without a model turn")
