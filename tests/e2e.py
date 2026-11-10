"""Live acceptance against an explicitly selected disposable AT deployment.

Never discovers or mutates the user's default AgentTeams installation.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.parse
import struct
import zlib

source = Path(os.environ["AT_SOURCE"])
sys.path.insert(0, str(source / "deepseek-harness/scripts"))
from matrix_channel import MatrixClient

controller = os.environ["AT_TEST_CONTROLLER"]
assert controller.startswith("codex-poc-"), "use an isolated codex-poc deployment"
image = os.environ["AT_CODEX_IMAGE"]
worker_prefix = "codex-poc-worker-"
results = []
artifact_dir = Path(os.getenv("AT_TEST_ARTIFACTS", "artifacts"))
artifact_dir.mkdir(parents=True, exist_ok=True)


def docker(*arguments, text=None):
    return subprocess.run(
        ["docker", *arguments], input=text, text=True, capture_output=True, check=True
    ).stdout


def agt(*arguments, text=None):
    return docker("exec", "-i", controller, "agt", *arguments, text=text)


def wait_for(function, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = function()
        if result:
            return result
        time.sleep(1)
    raise TimeoutError("acceptance condition did not become true")


def worker(name):
    return json.loads(agt("get", "workers", name, "-o", "json"))


def ready(name):
    data = worker(name)
    if data.get("phase") != "Running":
        return False
    try:
        health = json.loads(
            docker(
                "exec",
                worker_prefix + name,
                "curl",
                "-fsS",
                "http://127.0.0.1:8088/healthz",
            )
        )
    except subprocess.CalledProcessError:
        return False
    return data if health.get("ok") else False


def create(name):
    manifest = {
        "apiVersion": "agentteams.io/v1beta1",
        "kind": "Worker",
        "metadata": {"name": name},
        "spec": {
            "runtime": "codex",
            "model": "qwen3.5-plus",
            "image": image,
            "env": {"CODEX_SANDBOX_MODE": "danger-full-access"},
            "channels": {"matrix": {"showToolCalls": True, "showThinking": False}},
        },
    }
    agt("apply", "-f", "/dev/stdin", text=json.dumps(manifest))
    return wait_for(lambda: ready(name))


def messages(room):
    path = urllib.parse.quote(room, safe="")
    return matrix.request(
        "GET", f"/_matrix/client/v3/rooms/{path}/messages?dir=b&limit=100"
    ).get("chunk", [])


def reply(room, event_id, marker, sender):
    for event in messages(room):
        content = event.get("content", {})
        if (
            event.get("sender") == sender
            and content.get("m.relates_to", {}).get("m.in_reply_to", {}).get("event_id")
            == event_id
        ):
            if content.get("body", "").startswith("Codex execution failed"):
                raise AssertionError(content["body"])
            if marker in content.get("body", ""):
                return event
    return False


def ask(room, text, marker, sender, timeout=240, mentions=None):
    event_id = matrix.send_text(room, text, mentioned_user_ids=mentions)
    return wait_for(lambda: reply(room, event_id, marker, sender), timeout)


def check(name, function):
    selected = os.getenv("AT_TEST_CASES", "").split(",")
    if (
        selected != [""]
        and name not in selected
        and name != "controller-created-worker-and-health"
    ):
        return True
    started = time.monotonic()
    try:
        details = function()
        result = {
            "name": name,
            "status": "passed",
            "seconds": round(time.monotonic() - started, 2),
            "details": details,
        }
    except Exception as error:
        result = {
            "name": name,
            "status": "failed",
            "seconds": round(time.monotonic() - started, 2),
            "error": f"{type(error).__name__}: {error}",
        }
    results.append(result)
    (artifact_dir / "e2e.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False) + "\n"
    )
    print(json.dumps(result), flush=True)
    return result["status"] == "passed"


environment = dict(
    value.split("=", 1)
    for value in json.loads(docker("inspect", controller))[0]["Config"]["Env"]
)
matrix = MatrixClient(os.getenv("AT_TEST_MATRIX_URL", "http://127.0.0.1:38080"), "")
login = matrix.request(
    "POST",
    "/_matrix/client/v3/login",
    {
        "type": "m.login.password",
        "identifier": {
            "type": "m.id.user",
            "user": environment["AGENTTEAMS_ADMIN_USER"],
        },
        "password": environment["AGENTTEAMS_ADMIN_PASSWORD"],
    },
)
matrix.token = login["access_token"]
admin_id = login["user_id"]
primary = None


def provision():
    global primary
    primary = create("codex-a")
    return {
        "name": primary["name"],
        "runtime": primary["runtime"],
        "phase": primary["phase"],
        "image": image,
        "codex": docker(
            "exec", worker_prefix + "codex-a", "codex", "--version"
        ).strip(),
    }


if not check("controller-created-worker-and-health", provision):
    raise SystemExit(1)
room, sender = primary["roomID"], primary["matrixUserID"]
memory = "AT_MEMORY_" + str(int(time.time()))

check(
    "matrix-model-mcp-health",
    lambda: ask(
        room,
        "Call the TeamHarness health MCP tool, then reply exactly MCP_HEALTH_OK.",
        "MCP_HEALTH_OK",
        sender,
    )["event_id"],
)
check(
    "conversation-memory-first-turn",
    lambda: ask(
        room,
        f"Remember this token for our conversation: {memory}. Reply exactly MEMORY_SAVED.",
        "MEMORY_SAVED",
        sender,
    )["event_id"],
)
check(
    "conversation-memory-next-turn",
    lambda: ask(
        room,
        "What token did I ask you to remember? Reply with only that token.",
        memory,
        sender,
    )["event_id"],
)


def file_roundtrip():
    filename = f"codex-result-{int(time.time())}.txt"
    prompt = f"Use your shell tool to create outbox/{filename} containing exactly FILE_ROUNDTRIP_OK, also create durable.txt containing WORKSPACE_PERSISTED, and return the result file. Reply FILE_CREATED."
    event_id = matrix.send_text(room, prompt)
    wait_for(lambda: reply(room, event_id, "FILE_CREATED", sender), 240)

    def attachment():
        return next(
            (
                item
                for item in messages(room)
                if item.get("sender") == sender
                and item.get("content", {}).get("msgtype") == "m.file"
                and item["content"]
                .get("m.relates_to", {})
                .get("m.in_reply_to", {})
                .get("event_id")
                == event_id
            ),
            None,
        )

    result = wait_for(attachment)
    data, _ = matrix.download_media(result["content"]["url"], 1024)
    assert data.strip() == b"FILE_ROUNDTRIP_OK", "file bytes differ"
    return {"event_id": result["event_id"], "bytes": len(data)}


check("shell-outbox-matrix-file-roundtrip", file_roundtrip)


def inbound_file():
    path = artifact_dir / "codex-input.txt"
    path.write_text("Read this file and reply exactly FILE_INPUT_OK.\n")
    event_id = matrix.send_file(
        room, path, None, "codex-inbound-file-" + str(time.time_ns())
    )
    return wait_for(lambda: reply(room, event_id, "FILE_INPUT_OK", sender), 240)[
        "event_id"
    ]


check("matrix-inbox-file-read", inbound_file)


def inbound_image():
    path = artifact_dir / "codex-red.png"

    def chunk(kind, data):
        return (
            struct.pack("!I", len(data))
            + kind
            + data
            + struct.pack("!I", zlib.crc32(kind + data))
        )

    pixels = (b"\x00" + b"\xff\x00\x00" * 32) * 32
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!2I5B", 32, 32, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )
    event_id = matrix.send_file(room, path, None, "codex-image-" + str(time.time_ns()))
    result = wait_for(
        lambda: next(
            (
                item
                for item in messages(room)
                if item.get("sender") == sender
                and item.get("content", {})
                .get("m.relates_to", {})
                .get("m.in_reply_to", {})
                .get("event_id")
                == event_id
                and any(
                    color in item.get("content", {}).get("body", "").lower()
                    for color in ("red", "红")
                )
            ),
            None,
        ),
        240,
    )
    return result["event_id"]


check("matrix-image-native-vision", inbound_image)


def recreate():
    previous = json.loads(docker("inspect", worker_prefix + "codex-a"))[0]["Id"]
    docker("rm", "-f", worker_prefix + "codex-a")
    # Embedded Docker reconciliation is periodic (5 minutes in AT main).
    wait_for(lambda: ready("codex-a"), 420)
    current = json.loads(docker("inspect", worker_prefix + "codex-a"))[0]["Id"]
    assert current != previous
    result = ask(
        room,
        "Repeat the token I asked you to remember earlier. Reply with only the token.",
        memory,
        sender,
    )
    ask(
        room,
        "Read durable.txt using your shell tool and reply with its contents.",
        "WORKSPACE_PERSISTED",
        sender,
    )
    return {"recreated": True, "memory_reply": result["event_id"]}


check("container-recreation-native-session-recovery", recreate)


def cancel():
    event_id = matrix.send_text(
        room,
        "Run sleep 45 in your shell tool, then reply SLEEP_FINISHED. Do not skip the shell call.",
    )
    wait_for(lambda: reply(room, event_id, "commandExecution", sender), 120)
    control = matrix.send_text(room, "/cancel")
    wait_for(lambda: reply(room, control, "control accepted", sender), 30)
    result = wait_for(lambda: reply(room, event_id, "cancelled", sender), 30)
    return result["event_id"]


check("human-cancels-active-turn", cancel)


def steer():
    event_id = matrix.send_text(
        room,
        "Run sleep 12 in your shell tool, then reply ORIGINAL_FINISH. Do not skip the shell call.",
    )
    wait_for(lambda: reply(room, event_id, "commandExecution", sender), 120)
    control = matrix.send_text(
        room, "/steer Change the final answer to STEERED_FINISH."
    )
    wait_for(lambda: reply(room, control, "control accepted", sender), 30)
    return wait_for(lambda: reply(room, event_id, "STEERED_FINISH", sender), 120)[
        "event_id"
    ]


check("human-steers-active-turn", steer)


def team():
    secondary = create("codex-b")
    try:
        existing = json.loads(agt("get", "teams", "codex-team", "-o", "json"))
        assert (
            existing["leaderName"] == "codex-a" and "codex-b" in existing["workerNames"]
        )
    except subprocess.CalledProcessError:
        agt(
            "create",
            "team",
            "--name",
            "codex-team",
            "--leader-name",
            "codex-a",
            "--workers",
            "codex-b",
            "--admin-matrix-id",
            admin_id,
        )

    def team_ready():
        data = json.loads(agt("get", "teams", "codex-team", "-o", "json"))
        return (
            data if data.get("phase") == "Active" and data.get("teamRoomID") else False
        )

    data = wait_for(team_ready)
    # Wait for the bridges to load the Controller-projected role context.
    time.sleep(4)
    result = ask(
        data["teamRoomID"],
        "State your current role and call TeamHarness health. End with LEADER_ROLE_OK.",
        "LEADER_ROLE_OK",
        sender,
        mentions=[sender],
    )
    assert "leader" in result["content"]["body"].lower()
    member_id = secondary["matrixUserID"]
    ask(
        data["teamRoomID"],
        "State your current role and call TeamHarness health. End with WORKER_ROLE_OK.",
        "WORKER_ROLE_OK",
        member_id,
        mentions=[member_id],
    )
    return {"team": data["name"], "leader_event": result["event_id"]}


check("codex-leader-and-worker-team-roles", team)


def collaborate():
    data = json.loads(agt("get", "teams", "codex-team", "-o", "json"))
    target = worker("codex-b")["matrixUserID"]
    marker = "TEAM_COLLAB_OK_" + str(time.time_ns())
    worker_marker = "WORKER_RESULT_323_" + str(time.time_ns())
    matrix.send_text(
        data["teamRoomID"],
        f"This is a lightweight collaboration check. Use your TeamHarness message tool to ask {target} in this room to calculate 17 * 19 and reply {worker_marker}. Mention that member explicitly. When the member's answer arrives, reply exactly {marker}. Do not create a project or task for this check.",
        mentioned_user_ids=[sender],
    )
    result = wait_for(
        lambda: next(
            (
                item["event_id"]
                for item in messages(data["teamRoomID"])
                if item.get("sender") == sender
                and marker in item.get("content", {}).get("body", "")
            ),
            None,
        ),
        240,
    )
    assert any(
        item.get("sender") == target
        and worker_marker in item.get("content", {}).get("body", "")
        for item in messages(data["teamRoomID"])
    )
    return result


check("leader-delegates-worker-replies-leader-aggregates", collaborate)
raise SystemExit(0 if all(result["status"] == "passed" for result in results) else 1)
