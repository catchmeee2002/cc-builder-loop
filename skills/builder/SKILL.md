---
name: builder
description: 执行已接受的 Builder-loop 实验方案，把同 session 紧邻的 BUILDER_HANDOFF_READY + 原生 Implement the plan.，或用户显式 $builder，桥接到 Full Driver v4。校验冻结的 Assurance v4 contract 后，使用原生 Tester/Reviewer custom agents、独立 proof、机器验证、blackbox 和安全 finalize。普通实施请求、原生 Plan 或缺少已验证 contract 时不要使用。
---

# Builder 实验入口

这是 Full Driver v4 的授权桥，不是 legacy v2/v3 Builder。不得调用公共 legacy `start`，不得恢复旧
Builder Skill 的第二套循环说明。

## 授权

只接受一种入口：

- 用户当前 turn 显式调用 `$builder`，且同一聊天中存在已接受、已验证的 Builder-loop 实验方案；或
- 同 session 紧邻上一轮由 `$builder-loop-planner` 输出 `BUILDER_HANDOFF_READY`，当前已在 Default
  mode，用户使用 Codex 原生 `Implement the plan.`／“实施计划”动作，期间没有插入消息或修订方案。

标记缺失、非紧邻、session 变化、仍在 Plan mode、来自 Codex 原生 Plan、contract 被修改或普通文字
“实现一下”都不构成授权。一次动作最多启动一个 run。

## 启动 Full Driver

1. 读取项目 `AGENTS.md`、设计哲学和文档政策。
2. 从已接受方案提取唯一 `assurance-v4-contract` marker；缺失、重复或 JSON 外还有第二份 contract 时停止。
3. 完整读取相邻 Skill `../full-driver-v4-experiment/SKILL.md`，后续循环、角色连续性、问题路由、用户
   中断边界和最终标记全部以它为准。本 Skill 不复制这些规则。
4. 再次把 contract 原样通过 stdin 交给
   `codex-builder-loop assurance --experimental-v4 validate --contract -`；只有 `status=READY` 才继续。
5. 从 SessionStart developer context 原样取得 `session_id`。生成唯一小写连字符 run id，通过 stdin 调用：

   `codex-builder-loop assurance --experimental-v4 start --repo <repo> --run <run-id> --session-id <session-id> --contract -`

6. start 成功后立即输出 `BUILDER_LOOP_RUN_ID:<run-id>`，随后严格按 Full Driver Skill 持续调用
   `driver-next`，每个 mutation 传入当前 `action_id`，直到 finalized 或冻结的用户决策边界。

legacy v2/v3 既有 ledger 的诊断、恢复或 cleanup 仍直接使用对应 CLI，不通过本实验入口创建 revision。
