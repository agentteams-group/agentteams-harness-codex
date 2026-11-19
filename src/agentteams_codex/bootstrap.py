"""Fetch only Worker-owned startup assets and this adapter's native caches."""
import json
import os
from pathlib import Path
import subprocess
import tempfile


def listing(prefix):
    found = subprocess.run(["mc", "ls", "--json", prefix + "/"], check=True, capture_output=True, text=True)
    keys = set()
    for line in found.stdout.splitlines():
        item = json.loads(line)
        if item.get("status") != "success":
            raise ValueError("Worker storage listing failed")
        keys.add(item["key"])
    return keys


def restore(home, prefix):
    root_keys = listing(prefix)
    runtime_keys = listing(prefix + "/runtime")
    with tempfile.TemporaryDirectory(dir=home) as scratch:
        def pull(relative):
            local = Path(scratch) / Path(relative).name
            subprocess.run(["mc", "cp", prefix + "/" + relative, str(local)], check=True, stdout=subprocess.DEVNULL)
            local.replace(home / relative)

        pull("runtime/runtime.yaml")
        for name in ("codex-bridge-state.json", "codex-state.tar.gz"):
            if name in runtime_keys:
                pull("runtime/" + name)
        for name in ("IDENTITY.md", "SOUL.md", "AGENTS.md", "openclaw.json"):
            if name in root_keys:
                pull(name)
    journal = home / "runtime/codex-bridge-state.json"
    has_files = journal.is_file() and json.loads(journal.read_text()).get("workspace_has_files")
    if "workspace/" in root_keys or has_files:
        subprocess.run(["mc", "mirror", prefix + "/workspace/", str(home / "workspace") + "/", "--overwrite"],
                       check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    home = Path(os.environ["TEAMHARNESS_RUNTIME_CONFIG"]).parent.parent
    prefix = os.environ["AGENTTEAMS_STORAGE_PREFIX"].rstrip("/") + "/agents/" + os.environ["AGENTTEAMS_WORKER_NAME"]
    restore(home, prefix)
