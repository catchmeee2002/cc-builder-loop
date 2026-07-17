# 已知环境问题

[保质期: Codex multi-agent child 事件完整性修复并通过 live smoke, owner: codex-builder-loop, 正向归宿: tests/live/agent-events 报告]

## Child spawn 假通过

当前验证过的 Codex CLI alpha 构建中，`codex exec` 可能在没有生成 child thread 的情况下反复等待空 `receiver_thread_ids`，随后仍由主 agent 返回看似成功的结果。进程 exit code 和最终文本因此不能证明 subagent 真正执行。

发布级 live smoke 必须同时验证：

- spawn 事件成功且包含非空 child thread id；
- child 进入 completed；
- follow-up 发送到同一个 child id；
- 根线程最终结果发生在 child completion 之后；
- stderr 不包含 spawn、sandbox 或 routing 错误。

任一条件失败时，`$builder` 必须返回 continuity/capability failure，不得让根 agent 自己补做后声称通过。

## Linux read-only sandbox

[保质期: Codex read-only sandbox 在受限容器通过 live smoke, owner: codex-builder-loop, 正向归宿: tests/live/sandbox-doctor 报告]

部分容器内核不允许 bubblewrap 创建 user namespace，Codex read-only sandbox 会在读取 worktree
元数据时失败。live smoke 应先运行 `codex doctor`；不满足 sandbox 前提时标记环境阻塞，不降低
权限后静默重试。
