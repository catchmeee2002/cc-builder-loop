---
name: builder
description: 执行已接受并验证的 Builder-loop 实验方案。Codex 主 Agent 原生协调 Builder、Tester、Reviewer；runtime 只负责角色调用适配、独立判据和事务。普通原生计划或缺少已验证 contract 时不使用。
---

# Builder 实验入口

主 Agent 负责理解任务、安排角色、澄清和正常迭代。判据与事务由 runtime 验证，不能用 Agent 自述代替。
不得调用公共 legacy `start`，不得手改 ledger。

## 授权与启动

只接受用户显式 `$builder` 且同一聊天中已有接受并验证的实验方案，或同 session 紧邻的
`BUILDER_HANDOFF_READY` 后在 Default mode 提交的原生 `Implement the plan.`。普通原生计划不隐式切换路线。

创建 run 前完成身份、方案和准入核对：

1. 读项目 AGENTS、设计哲学和文档政策。从方案取唯一 `assurance-v4-contract`；不得重写为另一份计划。
2. 通过 `assurance --experimental-v4 validate --repo <repo> --contract -` 重新验证，要求顶层和 admission 均 READY。
3. 用 `native-driver session-identity` 读取宿主身份；不得设置 `CODEX_THREAD_ID`、借用历史身份或让用户填写身份。
4. 有 `assurance-v4-decision` 时，绑定原 run/problem/facet 执行 `validate-decision` 及其返回的原子修订入口，
   不新开 run。产品选择回答先通过正式入口记为精确 delta；不得把回答当成已应用变更。
5. 新任务用 `native-agent start --repo <repo> --run <run> --contract -`。一次授权最多创建一个 run。
   原样报告 `BUILDER_LOOP_RUN_ID:<run>`。Builder 默认使用当前主 session；仅冻结方案明确指定 `native_thread` 时调用独立 Builder thread。

已有任务按冻结 runtime 和原控制器继续；不能用新入口接管旧任务。`full-driver-v4-experiment` 只属于
冻结旧版本，不作为新任务的自动回退。宿主能力不足时保留已接受方案和现场，不能降低独立判据。

## 原生协调

- `native-agent status` 提供真实证据、问题、角色和事务，以及可执行操作。它不替你选择工作。
- 根据任务和事实选择操作，用 `native-agent inspect --action <operation>` 获取输入绑定，再调用
  `native-agent invoke --action <operation> --expected-operation-id <id>`。这些命令均带 `--repo` 和 `--run`。
  多个合法操作由你安排；输入变化后重新核对，不能照抄过期凭据。
- Builder 操作返回 `BUILDER_HANDOFF` 后，在候选 worktree 实现并提交；通过
  `assurance --experimental-v4 apply-root-builder-result` 绑定 action 和 owner 提交结果。
  已接收结果不得重写；应用中断续接原 artifact，不能再次实现或重派角色。
- Tester/Reviewer 调用使用独立身份；普通后续工作续接原 thread。调用层只执行本次选择的角色操作，
  不循环决定下一个角色。测试问题回原 Tester，实现问题由 Builder 修，审查回原 Reviewer。
- 角色输入只从 `role-context` 获取。消息是通知；角色不得以主 Agent 的转述、候选实现或预期结果代替
  冻结输入。大输入用 `--index`、`--pointer` 分段读取并核对 input digest；压缩后重新获取。
- 机器验证、proof、blackbox 和最终审查必须保留；缺失或过期证据不能通过口头解释跳过。
  `READY` 只表示命令成功或条件成立，不表示业务交付完成。
- 普通实现、测试纠错、输入重新获取和通信恢复由你持续处理。只有目标、授权、验收强度改变、明确
  执行额度耗尽，或无法保持安全边界时才交给用户决定；框架缺少路径不得伪装成新的产品选择。
- 通信额度耗尽后，只有显式恢复授权才可在原操作调用中增加 `--renew-reason`；普通继续不携带该参数。
- 对未知执行结果先核对原 thread/turn 和事务，不直接重发。恢复只使用状态指向的持久化事务与正式入口。
  不手改 ledger、不清理授权 dirty 输入、不默默替换角色或控制器。
- 外部副作用始终绑定授权目标、probe 和可恢复 intent；结束前释放 lease 并证明恢复。
- 全部判据齐备后显式执行 finalize，携带合规 commit message；finalize 成功才汇报交付。
  用户要求暂停时使用正式 hold，恢复绑定同一 hold，暂停不是完成。

## 收尾

遇到 FINALIZED、NEEDS_USER、FATAL、continuity failure 或 abandon 后，读取
[交付后事故归属](references/post-delivery-retrospective.md)。保留原始事实和失败事实，不用成功标记覆盖阻塞。
只有 finalized 且复盘完成才输出 `FULL_DRIVER_V4_RESULT: finalized`；保留该结果 marker 的消费兼容。
最终消息包含 `retrospective-status.required_user_block`；完整审计块不复制给用户。
