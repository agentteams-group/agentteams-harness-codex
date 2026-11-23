# Managed direct conversation validation

The native Direct fixture now enters the Worker/Leader path: `WorkerMatrix.accept` retains each original event, `Bridge.process` calls `ManagedMatrix.message_turn`, and the synthetic HTTP Controller issues the execution identity only on `message_claim`. Controls use `ManagedMatrix.control` with a separate claim. Production managed PreToolUse/PostToolUse hooks remain enabled and are required by `run_turn`. Only the model and Matrix/Controller transports are synthetic. This is native integration evidence, not a real-provider collaboration certification.

Run in the packaged image with the fixed Core assets and its managed requirements:

```
python tests/direct_control_smoke.py steer worker
python tests/direct_control_smoke.py steer leader
python tests/direct_control_smoke.py stop worker
```

The Steer case asserts that the original tool survives, the same turn accepts the supplement, the next actual model request includes both sources, the native session is checkpointed, and the original shared reply reaches `delivered`.

## 2026-11-22 source and native evidence

With runtime `084cc70aea0ab5c52817370c0529dd09ddfee225`, public Core `663615d7e5d5a85887999b75b510347a1edfdbcc`, and SDK executable SHA256 `e47caa99cd7f65b874352d97e80d8a32b2200e7ec615aa59ab9e7009495b3338`, managed Steer failed: `Codex did not persist the original Matrix input`. Ordinary `thread/inject_items` queues during an active turn. The adapter now requests the SDK's existing `persistToActiveTurn` using the captured actual turn ID, checks the exact `persistedToActiveTurn` receipt, and independently verifies the native User row before publishing a shared input receipt. Initial pre-turn inputs retain their original path. No SDK rebuild or public Task semantics change is involved.

Worker and Leader Steer passed against the actual AppServer and actual managed hooks after this fix. Local validation used the existing Debian production OpenHuman base with read-only mounts for Codex source, fixed managed requirements, the exact SDK executable and Core assets; it does not substitute for the assembled Codex production image check. Local red/green logs are retained under `output/direct-managed-local/` in the isolated checkout.

## Open gates

- **Stop after starting an ordinary native tool:** the actual SDK interrupts the tool, but the original generic operation remains `started`/verification-required. `ManagedMatrix.message_save_reply` correctly rejects `A verified active message and a result are required`. Full stopped-result delivery remains unverified; the Stop fixture intentionally still fails. Do not erase the unknown operation or label it successfully delivered to pass the test.
- **Codex Manager ordinary management tools:** `task_recovery.enabled()` excludes Manager, so `Bridge` has no ManagedMatrix execution admission. `run_turn` still supplies a private native scope, and generic tool hooks correctly reject `Native tool requires a durable execution admission`. This affects native shell and noncanonical MCP tools in an ordinary Manager conversation. No-tool chat and the fixed canonical Task tool's separate common authority do not require that generic execution scope; Task-only planning/review/terminal phases are separate paths. This is a known scope gap, not an authorization request and not evidence of Manager operations passing. A focused test retains the fence. The packaged cloud failure was observed in run `44990094421`, `native-direct-steer.log`.

The current six-Harness collaboration experiment uses QwenPaw Manager. These direct-conversation gates must not be represented as completed certification or silently bypassed, but they do not require changing the public Task protocol for normal Worker/Leader Task trials.

## Production sandbox dependency preflight

The first real Worker business command found a separate packaging failure: the final Node22 image omitted system `bubblewrap`, and the custom SDK executable had no adjacent `codex-resources/bwrap`. The native launcher requires either a system `bwrap` supporting `--perms`, or its bundled helper. Installing Debian's `bubblewrap` in the final stage supplies the missing runtime dependency; it does not change sandbox policy or clear the unknown-effect fence from the failed attempt.

Before a new real task, run this in the actual candidate image with the same container security settings as a Worker:

```
python tests/native_tool_smoke.py allow workspace-write
```

This uses the actual AppServer and managed tool hooks. The command must create a file in its writable workspace and fail to create one outside it, while completing its owned tool checkpoint. Merely checking `bwrap --version` is insufficient. The original Direct fixture explicitly uses `danger-full-access` and therefore did not cover this dependency. If the workspace-write check instead fails at namespace creation, report the actual container user-namespace/seccomp capability failure; do not switch to an unrestricted sandbox or add privileged execution to make it pass. The earlier failed task remains verification-required and must not be silently replayed.
