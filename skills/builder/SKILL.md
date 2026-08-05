---
name: builder
description: 执行已接受的 Builder-loop 实验方案，把同 session 紧邻的 BUILDER_HANDOFF_READY + 原生 Implement the plan.，或用户显式 $builder，桥接到 Full Driver v4。默认由 Native Driver 编排 Builder/Tester/Reviewer thread、独立 proof、机器验证、blackbox 和安全 finalize；普通实施请求、原生 Plan 或缺少已验证 contract 时不要使用。
---

# Builder 实验入口

这是 Full Driver v4 的授权桥，不是 legacy v2/v3 Builder。Native Driver 是 Full Driver 的原生承载，
不是第四个组件；不得调用公共 legacy `start`，不得恢复旧 Builder Skill 的第二套循环说明。

## 授权

只接受一种入口：

- 用户当前 turn 显式调用 `$builder`，且同一聊天中存在已接受、已验证的 Builder-loop 实验方案；或
- 同 session 紧邻上一轮由 `$builder-loop-planner` 输出 `BUILDER_HANDOFF_READY`，当前已在 Default
  mode，用户使用 Codex 原生 `Implement the plan.`／“实施计划”动作，期间没有插入消息或修订方案。

标记缺失、非紧邻、session 变化、仍在 Plan mode、来自 Codex 原生 Plan、contract 被修改或普通文字
“实现一下”都不构成授权。一次动作最多启动一个 run。

## 启动 Native Driver

1. 读取项目 `AGENTS.md`、设计哲学和文档政策。
2. 从已接受方案提取唯一 `assurance-v4-contract` marker；缺失、重复或 JSON 外还有第二份 contract 时停止。
3. 再次把 contract 原样通过 stdin 交给
   `codex-builder-loop assurance --experimental-v4 validate --contract -`；只有 `status=READY` 才继续。
4. 从 SessionStart developer context 原样取得 `session_id`。若方案含唯一 `assurance-v4-decision` marker，
   这是同 run contract decision，不得生成新 run：
   - marker 必须只含 schema_version/run_id/problem_key/action_id/facet/facet_digest，且当前
     `driver-next` 仍是同一 `contract_decision`；
   - 用 marker、当前 session_id 和完整 contract 重跑 `validate-decision`。任何 stale、session mismatch、
     隐藏 delta 或不受支持事务都停止，不能回退为 `start`；
   - `apply.command=update-facet` 时，从完整 contract 取对应 Authority/Assurance facet，带返回的精确
     authorization flags、`--resolve-plan-problem-key`、`--decision-action-id`、
     `--expected-facet-digest` 与 `--session-id` 原子更新；`apply.command=revise-mission` 时从完整 contract
     取 Mission 与 `execution.revision_transition`，同样携带全部 decision binding；
   - 更新成功后执行
     `codex-builder-loop native-driver resume --repo <repo> --run <run-id>`，并继续等待原 run 终态。
5. 没有 decision marker 时才生成唯一小写连字符 run id，把 contract 原样通过 stdin交给：

   `codex-builder-loop native-driver start --repo <repo> --run <run-id> --session-id <session-id> --contract -`

6. 新 run 看到 `event=native_driver_run_started` 后，或同 run decision 更新成功后，立即输出
   `BUILDER_LOOP_RUN_ID:<run-id>`，持续等待 Native
   Driver 到 `FINALIZED`、`FAILED` 或 `NEEDS_USER`。普通 revision、修复、Agent follow-up、target rematerialize
   和 finalize recovery 不交还用户。run 创建后若仅遇到 App Server disconnect/overload，用
   `native-driver resume --repo <repo> --run <run-id>` 自动续接；同一 transport signature 连续三次才按
   continuity failure 停止，不能改走另一控制器。未处理 FATAL 必须已由 Native Driver 写成
   `driver_failure` 并完成副作用恢复；若恢复仍需用户，保留 recovering 现场，不用 abandon 覆盖。
7. 只有 Native Driver 在创建 run 前返回 `NATIVE_DRIVER_CODEX_UNAVAILABLE`、
   `NATIVE_DRIVER_PROTOCOL_UNAVAILABLE` 或 `NATIVE_DRIVER_PROTOCOL_INCOMPATIBLE`，才完整读取相邻
   `../full-driver-v4-experiment/SKILL.md` 并由现有 Full Driver 承载同一 contract。run 一旦创建，禁止
   切换承载或让两个控制器同时 mutation。

legacy v2/v3 既有 ledger 的诊断、恢复或 cleanup 仍直接使用对应 CLI，不通过本实验入口创建 revision。

## 终态复盘

Native Driver 或 Full Driver 到达 `FINALIZED`、`NEEDS_USER`、`FATAL`、continuity failure 或 abandon
后，都只加载同一份
[交付后事故归属](references/post-delivery-retrospective.md)。复盘不重新打开 delivery gate，也不改写
finalized target 或原失败事实。`FINALIZED` 只有在复盘确认无事故，或事故已完成查重与授权分流后，
才输出 `FULL_DRIVER_V4_RESULT: finalized`；其他终态保留原状态和诊断，不用成功标记覆盖。
最终用户消息逐字包含 `retrospective-status.required_user_block`；完整 `required_block` 只留在结构化
审计结果，不复制到用户消息。
