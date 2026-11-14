# AgentTeams Codex Harness

Run the unmodified Codex App Server as an AgentTeams-managed runtime. AT owns the member lifecycle, Matrix identity and storage; this adapter owns the Codex process, configuration, event journal and native session snapshots. Manager, Leader and Worker are roles of the same runtime.

Managers load the shared TeamHarness Manager prompts, resource-management and diagnosis Skills without requiring Team membership. Your Matrix adapter uses the Controller-owned Manager sender allow-list, requires mentions in group rooms and reports readiness after a successful Matrix sync. Select `runtime: codex` and an image containing this support on your Manager resource; the older v1.2.7 image supports Worker/Leader only. Your gateway route must support the Responses API.

Select your Harness at launch. Cross-Harness hot switching does not transfer Matrix cursors and can replay messages; that behavior is tracked separately in [Core #1631](https://github.com/agentscope-ai/AgentTeams/issues/1631). Same-Harness restarts retain the existing durable journal recovery.

Container launches use the shared bounded runtime logger. Inspect `status.json`, `events.jsonl` and `runtime.log` under `/var/log/agentteams/manager/` or `/var/log/agentteams/workers/<resource-name>/`. Tool failures retain native error details, exit codes and item IDs; model-turn failures retain their concrete reason. Credentials are redacted. Mount the directory explicitly if you need logs after container replacement. See the [Core logging contract](https://github.com/agentscope-ai/AgentTeams/blob/main/docs/usage/runtime-logging.md).

Native `requestUserInput` requests render as inline Dashboard question cards.
Structured answers preserve each question ID and selected or custom answer;
only the requesting human or team admin can answer the active request. An old
card cannot answer a later turn that reuses an App Server request ID.

Status: experimental PoC. The integration requires the AT `codex` runtime registration change. Codex CLI is pinned to `0.143.0`; AT transport and TeamHarness build inputs are pinned to `41ef2358abc2af7519356d1cd5b9f72392f70af6`.

## Build independently

```sh
make build VERSION=v0.1.0
# Test against a local AT checkout instead of the pinned remote input:
make build AT_CONTEXT=../AgentTeams VERSION=local-YYYYmmddHHMMSS
```

BuildKit supplies the `at` named context. There is no dependency on an adjacent checkout in the default build. The image consumes the existing Matrix transport/file helpers, shared shell libraries and TeamHarness from that immutable AT commit. It does not fork or patch Codex. `make push` publishes amd64/arm64 images; set `IMAGE` to your registry. Pin an independently released image when configuring AT.

## Create a member

```yaml
apiVersion: agentteams.io/v1beta1
kind: Worker
metadata:
  name: codex-dev
spec:
  runtime: codex
  model: gpt-5.3-codex
  image: ghcr.io/agentteams-group/agentteams-harness-codex:v0.1.0
```

The image above is the intended publication location, not a claim that v0.1.0 has been published. A Team can reference this resource as its Leader or Worker. The Controller projects `runtime.yaml` and issues Matrix, storage and model-gateway credentials. The model endpoint must implement the Responses API, including streamed tool calls; Chat Completions compatibility alone is insufficient.

The adapter reads the projected model, gateway route, inline identity/instructions, role and MCP servers before each turn. TeamHarness Skills are filtered by role. Normal replies return directly to the current Matrix room; the TeamHarness MCP server enforces its existing role permissions. Team task rooms and valid `PROJECT_REQUESTED` self triggers are supported.

## Execution and recovery

- Each Matrix room maps to a separate persistent Codex thread.
- Incoming events and the Matrix cursor are journaled before acknowledgement. Delivery retries reuse a deterministic Matrix transaction key and a saved answer, without repeating the model/tool turn.
- Incoming files/images are saved in `workspace/inbox/`. Changed files under `workspace/outbox/` are uploaded to Matrix and object storage.
- Native sessions and consistent SQLite backups are checkpointed after the App Server exits. Authentication files are excluded. The image restores snapshots on container replacement.
- A turn interrupted before its result is safely saved is not automatically replayed. Its requester receives a recovery message and can send new input. This avoids claiming exactly-once shell/MCP execution across crashes.
- `/healthz` reports recent Matrix sync health and whether a turn is active.

## Human control

In the active room, the requesting human or Team admin can send:

```text
/cancel
/steer Additional instructions for the current turn
/approve REQUEST_ID
/reject REQUEST_ID
/answer REQUEST_ID Your answer
```

Dashboard cards append a `THREAD_ID:TURN_ID` context token to `/approve` and
`/reject`. The bridge checks it before answering App Server, so an old card
cannot approve a new turn that reused the same request ID. Bare manual commands
remain supported for the currently active turn.

Control messages from other Agents are rejected. Pending approval IDs expire when the turn or App Server ends. New messages without a control command are queued as new turns. Use `CODEX_APPROVAL_POLICY=on-request` for interactive approvals; the default `never` executes only within the configured sandbox and cannot escalate it. The default sandbox is `workspace-write`. On a dedicated disposable container without nested sandbox support, explicitly set `CODEX_SANDBOX_MODE=danger-full-access` to rely on container isolation; do not mount the Docker socket or unrestricted host directories into that container.

## Model configuration

By default, the adapter uses the Controller-projected gateway URL and gateway key. `CODEX_BASE_URL` and `CODEX_GATEWAY_KEY` are explicit deployment overrides. Keep real keys in deployment secrets, not this repository.

For a trusted developer-only ChatGPT-account test, set `CODEX_AUTH_MODE=chatgpt` and mount a credentials file at `CODEX_AUTH_FILE`. The entrypoint copies it to the private Codex state directory; snapshots exclude it. Model catalog entries do not establish account entitlement: a completed inference turn does.

## Current limits

- Matrix E2EE, DingTalk transport, AgentSpec packages and legacy SSE MCP are not supported. Unsupported package/SSE configuration fails explicitly.
- Execution is sequential per member. Use multiple members for parallel work.
- Codex-specific session/history UI remains outside this adapter. Command and file approvals publish `org.agentteams.approval` Matrix metadata for Dashboard cards, with request and thread/turn IDs. Dashboard sends the existing `/approve ID` and `/reject ID` controls; accepted controls publish a runtime receipt. Questions keep `/answer ID text`, and pending approvals expire when the turn ends. Matching Dashboard support is required; this does not change the default approval or sandbox policies.
- Skills from the TeamHarness bundle are available. Additional AT Skill registry/package distribution needs a separate compatibility implementation.
- App Server is experimental upstream; re-run compatibility tests before changing the pinned Codex version.

## Tests

```sh
python3 -m pip install -e .
make test AT_SOURCE=../AgentTeams

# Only against a disposable codex-poc-* deployment:
AT_SOURCE=../AgentTeams \
AT_TEST_CONTROLLER=codex-poc-controller \
AT_CODEX_IMAGE=agentteams/codex-harness:local-YYYYmmddHHMMSS \
python3 tests/e2e.py

# Additional durable Project/Task check after the Team is ready:
AT_SOURCE=../AgentTeams AT_TEST_CONTROLLER=codex-poc-controller \
python3 tests/project_e2e.py
```

The live suite covers Controller provisioning, Matrix/model/MCP calls, multi-turn context, file ingress/egress, container replacement, cancellation, steering and Leader/Worker roles. It writes results to the ignored `artifacts/` directory. See `docs/acceptance.md` for measured results and remaining verification gaps.
