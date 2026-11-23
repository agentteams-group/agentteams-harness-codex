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
