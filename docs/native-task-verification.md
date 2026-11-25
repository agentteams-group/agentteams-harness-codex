The public Task business contract stays in the pinned AgentTeams Core context.
This adapter binds one dedicated native App Server/MCP process to a trusted turn.

The baseline native fixture uses the published Codex 0.143.0 binary and an own
local Responses endpoint, fake CLI executable and OTLP Collector. It exercises
real SDK tool discovery, source binding, framework receipts, no-effect validation
and content-free native telemetry. It is not a model/team certification run.

`patches/codex-durable-user-input.patch` targets the exact official SDK commit
`c4d748f586a84a3ed5b6aceb82e9a1db4abb1cda` (rust-v0.143.0). Its optional
`thread/inject_items.persistToActiveTurn` capability accepts only text User
messages for that exact active Regular turn, appends native history and flushes
rollout before returning `persistedToActiveTurn`. Default injection behavior is
unchanged. The private InputBarrier holds a Task ToolResult until this native
acknowledgement and runtime persistence complete. A stock SDK queueing ACK is
rejected and never clears `inputRefreshPending`.

Remaining gates before certification:

- Build the fixed SDK, generate/verify its app-server protocol schema fixtures,
  and run its exact-turn/history/no-duplicate-input tests.
- Run a real native refresh fixture proving the User item is durable before the
  held Task ToolResult returns and before every subsequent model request,
  including compaction and crash/restart boundaries.
- Update native trace attribution when new source inputs enter an existing turn;
  the initial process span attributes cannot describe later User inputs.
- Verify phase completion ends the native inference loop without extra work.
- Read native model/tool traces back through the experiment's Collector, then
  execute the frozen 20 scenarios and cross-Harness acceptance runs.

No native fixture or source patch is a substitute for these gates.


## Natural Task handoff and minimal context

Use the shared `task_context.model_context` projection for prepared Task evidence. Keep original requirements once, and pass versioned file references instead of previews and duplicate report bodies. The actual associated Skill contents remain available in native instructions.

A validated terminal Task result closes its native phase. Retain its identity/status and Task fact in the actual tool history; append confirmed sender-owned dispatch receipts through the SDK's fixed no-inference receipt callback. Preserve these receipts when restoring an admission scope. Do not dispatch a new model turn to explain them or acknowledge receipt in Matrix.

Keep business presentation separate from internal facts. Source intake returns one natural acceptance linking the Task room; full planning occurs there. Structured tool blocks expose real names, arguments, results, call IDs and status and coalesce with the final turn. A saved Task report uses the same run identity and retains the true report bytes; successful control receipts do not create an additional conversational answer. Recovery uses the retained presentation and exact pending delivery.

A verified completed phase without business presentation ends quietly. Preserve the empty presentation through scope restore and native answer recovery; do not display the internal receipt. Keep that validated receipt as the nonempty durable delivery body. Publish the shared `kind=quiet` run ending with the same RunId so tools remain available without a receipt bubble or another model turn.

Validate native persisted receipts and post-submit generation counts, not just sidecar state. Also verify no progress blocks or unaddressed peer messages enter another worker's model context. Local fixtures, deployment, mixed Task execution and full admission certification are separate proof layers. The Core integration guide's Task handoff section records the cross-Harness contract and remaining runtime acceptance gates.

The matched Core contract is pinned to `f08cd8291c4004479660addb16fc7ecd7d08c591` ([Core PR #1725](https://github.com/agentscope-ai/AgentTeams/pull/1725)); build the Controller/CLI and shared assets from that same revision. Local mixed normal and rework Tasks are separate from full admission certification.
