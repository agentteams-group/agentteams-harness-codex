# Codex Harness PoC acceptance

Run date: 2026-11-09–10, Asia/Shanghai. This is an experimental integration, not a production release.

## Inputs and deployment

- AT's actual default branch is `main` (there is no `master` branch). Base: `41ef2358abc2af7519356d1cd5b9f72392f70af6`, with the accompanying Codex runtime registration patch. The remote head was rechecked during acceptance.
- Unmodified `@openai/codex` / App Server `0.143.0`, Node 22, Debian Python 3.11.
- Independently built Linux ARM64 image: `agentteams/codex-harness:local-20261110003300`, image ID `sha256:22bf31c97e3cf3ba782e6faf18028a9ebd96e9e8ef136da577692aec4100e4f5`.
- Isolated Docker Controller, Higress, Tuwunel and MinIO deployment, with Controller-created Codex members. All resources use a `codex-poc-` prefix; the normal local installation was not changed.
- Actual inference used **Codex Harness + Qwen3.5-plus through Higress Responses streaming**. Available ChatGPT-account models returned `model_not_found` on inference. OpenAI-model inference is therefore an explicit remaining verification gap.
- The dedicated test containers used `CODEX_SANDBOX_MODE=danger-full-access` with no Docker socket or host source mount. The shipped default remains `workspace-write`.

## Live acceptance

The reproducible suite is `tests/e2e.py`: all 12 scenarios passed on the final image. The first 11 passed in a single full run; the collaboration scenario was rerun after replacing its repeated Worker result marker with a per-run unique marker. Earlier attempts correctly failed when the model claimed completion without fresh Worker evidence. The passing run verifies actual Matrix events, tool execution, file bytes and Controller recreation rather than substituting mocked model replies.

Coverage: provisioning/health, native MCP/model calls, two-turn memory, outbox upload/download, inbound file reading, native image understanding, forced container replacement with session and workspace recovery, cancellation, steering, Leader/Worker role projection and actual member-to-member delegation/result aggregation.

The forced replacement recovered both the remembered token and a workspace file in 321.69 seconds, including the Controller's normal five-minute reconciliation interval. The collaboration rerun completed in 16.67 seconds and verified a fresh Worker answer before counting the Leader result.

`tests/project_e2e.py` additionally checks durable Project/Task delegation, structured submission and Leader acceptance against the authoritative Controller workflow API. Project `codex-dag-1794242250` and task `task1` reached `completed`; submission `b6dc732ef4ed450dad8948fe2a97ea7d` was accepted and the stored result file contained `323`.

This supplemental workflow required **one explicit administrator correction**: the model skipped `ack_task`, so the Controller correctly refused its direct `assigned -> submitted` commit. After the administrator asked the Worker to read its task-execution Skill, acknowledge first and verify synchronization, the Worker committed `in_progress -> submitted` and the Leader accepted/completed the project. The final checker was also corrected to follow the actual deliverable path in task metadata instead of assuming a fixed filename. This proves the controlled workflow and its state/artifact results; it does **not** establish unattended project execution reliability for this model. The rerun prompt now states the acknowledgement requirement explicitly.

## Regression results

| Suite | Result |
| --- | --- |
| Harness unit/interaction/recovery tests | 18 passed on host and in the actual Debian image |
| Controller Go unit tests | All 29 tested packages passed |
| Kubernetes Controller envtest with race detector | 49 passed |
| QwenPaw Python suite, Linux, pristine 2.2.1 dependency | 439 passed, 3 subtests passed |
| Manager Python suite | 205 passed |
| Matrix channel Python suite | 112 passed |
| Plugin bundle contracts/install tests | 58 passed, 3 skipped |
| DeepSeek Harness transport/runtime tests | 47 Python tests and 2 Node tests passed |
| Manager MCP tests / shared file sync | 14 / 6 passed |
| Ruby taskflow smoke / real QwenPaw Matrix plugin loader | Passed |
| TeamHarness Python suite, Linux | 480 passed, 10 skipped, 5 failures also reproduced on clean main |
| Plugin CLI contracts | 5 passed; 1 failure also reproduced on clean main |
| CI plan/tools, mc installer, release metrics | 8 / 22 / 4 / 4 passed |
| Helm / CRD / shell contracts | Chart lint/render passed; missing independent image tag correctly rejected; CRDs identical; shell syntax and installer contracts passed |

Existing failures are retained rather than hidden:

- The five Linux TeamHarness failures are the status variants of `test_failed_pull_fails_closed_for_submit_and_accept_but_check_still_reads`: the fixture expects a stale-copy diagnostic but receives `task not found`. All five reproduce on the untouched base commit in the same Linux environment.
- `test_loongsuite_plugin_probe_definition_contract` expects a `claude` detection command while the existing definition contains only `qwenpaw`. It reproduces on untouched main.
- The Ruby transition-table smoke reports `failed to publish task files to shared storage` in both the modified checkout and clean main in the same Linux environment.
- On macOS, OpenClaw bundle syntax and four QwenPaw entrypoint tests failed in both relevant clean-base comparisons. Linux cleared those platform-dependent failures. An already patched QwenPaw package was replaced with pristine CI-pinned 2.2.1 before the final 439-test run.

These are local-environment regression results, including failures observed on the untouched base. The hosted AT TeamHarness, QwenPaw, Controller and other regression jobs subsequently passed; image build jobs were still running at the last check. The regression result is consequently **not an all-green claim for every local and hosted job**.

## Boundaries

- Kubernetes is covered by backend unit tests and real API-server envtest; real Kubernetes Pods, Sandbox service, cloud OSS/RRSA and multi-architecture registry pulls were not exercised.
- The image build and amd64/arm64 publication workflow are independent of AT releases. [Harness hosted CI](https://github.com/agentteams-group/agentteams-harness-codex/actions/runs/42656829492) passed its host tests and actual Linux AMD64 image tests; local live tests used ARM64. No multi-architecture registry manifest or public image release has been published.
- Approval acceptance/rejection, question responses, event correlation, unauthorized controls, duplicate delivery and uncertain-turn recovery have deterministic protocol tests. Live cancellation/steering are separate actual-model tests.
- Matrix E2EE, DingTalk, AgentSpec packages, extra AT Skill registry packages and legacy SSE MCP are unsupported and are not counted as accepted features. Dashboard-specific Codex history/approval UI is outside this PoC.
- Matrix delivery retries reuse the persisted answer. A crash during uncertain model/tool execution does not automatically replay the operation; this is not an exactly-once shell/MCP guarantee.

See the repository README for build, deployment, controls and rerun commands. Keep runtime logs, account credentials and deployment secrets outside git.
