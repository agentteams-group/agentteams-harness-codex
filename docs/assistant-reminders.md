# Codex Teams 助手提醒（主线预览）

Refs https://github.com/agentscope-ai/AgentTeams/issues/1683

依赖公共层 https://github.com/agentscope-ai/AgentTeams/pull/1684 ，本 PR 的 AT_REF 指向该公共层提交。

Controller 保存角色／用户上下文、调度和发送回执，通过现有 Teams 消息／@ 唤醒协议触发。此 Harness 沿已有鉴权载体消费同一份规范输入；不增加私有 HTTP 唤醒、cron、heartbeat 或业务记忆实现。

本 PR 验证：实际 ManagedMatrix/actionable_events 入站桥接：规范输入、独立助手会话、按普通工作入站而非业务 Task、旧角色拒绝。

```sh
AT_SOURCE=<Core-checkout> make test
```

镜像还必须使用包含 `agt assistant context/receipt/send` 的匹配 Controller/CLI；仅更新公共源码 pin 并不升级旧镜像中的 CLI。现有发布镜像不能据此宣称已启用新能力。完整模型执行、发送结果和 Dashboard 通知需在任务协议最终版本上联合认证。

部署前核对锁定的公共 Core、Controller／CLI 和 Harness 镜像是否匹配。实跑证据及未覆盖场景见总 Issue；源码合并不代表已发布。
