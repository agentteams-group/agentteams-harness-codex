"""Live Project/Task acceptance after tests/e2e.py creates the test Team."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

controller = os.environ["AT_TEST_CONTROLLER"]
assert controller.startswith("codex-poc-")
sys.path.insert(0, str(Path(os.environ["AT_SOURCE"]) / "deepseek-harness/scripts"))
from matrix_channel import MatrixClient


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True)


def agt(*args):
    return json.loads(docker("exec", controller, "agt", *args, "-o", "json"))


env = dict(
    item.split("=", 1)
    for item in json.loads(docker("inspect", controller))[0]["Config"]["Env"]
)
matrix = MatrixClient(os.getenv("AT_TEST_MATRIX_URL", "http://127.0.0.1:38080"), "")
login = matrix.request(
    "POST",
    "/_matrix/client/v3/login",
    {
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": env["AGENTTEAMS_ADMIN_USER"]},
        "password": env["AGENTTEAMS_ADMIN_PASSWORD"],
    },
)
matrix.token = login["access_token"]
team = agt("get", "teams", "codex-team")
leader = agt("get", "workers", "codex-a")["matrixUserID"]
worker = agt("get", "workers", "codex-b")["matrixUserID"]
room = team["teamRoomID"]
existing_project = os.getenv("AT_TEST_PROJECT_ID")
project_id = existing_project or "codex-dag-" + str(int(time.time()))
marker = "DAG_ACCEPTED_" + project_id
text = (
    f"Complete this small durable Project Work end to end using TeamHarness MCP tools. "
    f"Use project ID {project_id}. Create a DAG with one task assigned to {worker}: "
    f"Use unique task ID {project_id}-calc. "
    "calculate 17 * 19, write the result in a task workspace result.txt, and submit a structured result with artifact evidence. "
    "Delegate to that Worker, wait for its submission, inspect it and accept it as Leader, then complete the project. "
    "Read the relevant TeamHarness Skills before using their tools. Include in the task spec that the Worker must call ack_task and verify synced=true before submit_task, and must verify its submission synced=true. End your turn after delegation so incoming results can be processed. "
    "Do not edit task/project metadata files directly. This test authorizes the complete workflow. "
    f"Requester is {login['user_id']}; replyRoute.channel=matrix, replyRoute.target=room:{room}, "
    f"replyRoute.targetUser={login['user_id']}, replyRoute.targetSession=matrix:{room}. "
    f"Create a task room and self-trigger there if your role instructions require it. "
    f"After acceptance and project completion, report back here with {marker}."
)
if not existing_project:
    matrix.send_text(room, text, mentioned_user_ids=[leader])
artifacts = Path(os.getenv("AT_TEST_ARTIFACTS", "artifacts"))
artifacts.mkdir(parents=True, exist_ok=True)
deadline = time.monotonic() + 600
seen = set()
snapshot = None
while time.monotonic() < deadline:
    try:
        snapshot = agt(
            "get", "projects", project_id, "--team", "codex-team", "--include-tasks"
        )
        (artifacts / "project-workflow.json").write_text(
            json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n"
        )
        if snapshot.get("status") == "completed":
            tasks = snapshot.get("tasks_detail", [])
            assert len(tasks) == 1, "completed project must have the delegated task"
            task = tasks[0]
            assert task["status"] == "completed" and task["assigned_to"] == worker
            assert task.get("submission_id") and task.get("deliverables")
            deliverable = task["deliverables"][0]
            artifact_path = (
                deliverable.get("path")
                if isinstance(deliverable, dict)
                else deliverable
            )
            assert artifact_path.startswith(f"shared/tasks/{task['task_id']}/")
            artifact = docker(
                "exec",
                controller,
                "bash",
                "-c",
                'source /opt/agentteams/scripts/lib/agentteams-env.sh; mc cat "${AGENTTEAMS_STORAGE_PREFIX}/teams/codex-team/$1"',
                "_",
                artifact_path,
            )
            assert "323" in artifact, "accepted result file must contain 17 * 19"
            result = {
                "name": "durable-project-task-delegate-submit-accept",
                "status": "passed",
                "project_id": project_id,
                "workflow_status": snapshot["status"],
                "task_id": task["task_id"],
                "submission_id": task["submission_id"],
                "artifact_verified": True,
            }
            (artifacts / "project-e2e.json").write_text(
                json.dumps(result, indent=2) + "\n"
            )
            print(json.dumps(result), flush=True)
            raise SystemExit(0)
    except subprocess.CalledProcessError:
        pass
    payload = matrix.sync(None, 0)
    for room_id, data in payload.get("rooms", {}).get("join", {}).items():
        for event in data.get("timeline", {}).get("events", []):
            if event.get("event_id") in seen or event.get("sender") not in {
                leader,
                worker,
            }:
                continue
            seen.add(event.get("event_id"))
            body = event.get("content", {}).get("body", "")
            if body:
                print(
                    json.dumps(
                        {
                            "room": room_id,
                            "sender": event.get("sender"),
                            "text": body[:240],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
    time.sleep(3)
result = {
    "name": "durable-project-task-delegate-submit-accept",
    "status": "failed",
    "project_id": project_id,
    "workflow_status": snapshot.get("status") if snapshot else None,
}
(artifacts / "project-e2e.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result), flush=True)
raise SystemExit(1)
