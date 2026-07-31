---
name: full-driver-v4-experiment
description: 在 Builder-loop v4 维护期内显式运行本地 Git Full Driver 实验，使用 Assurance Core v4 的四事实面、独立 Tester、机器验证、黑盒、Reviewer 与安全 finalize 持续收敛。仅用于已接受的内部 v4 实验或历史回放；不得作为公共 `$builder`、普通实施请求或自动发现入口。
---

# Full Driver v4 Experiment

只编排 Codex 原生工作；不把 Agent 循环、correction budget 或下一步状态写进 Core。

## 启动

1. 确认用户已接受内部 v4 实验，目标仓库是单一、本地 Git 代码交付；外部设备、远程部署、
   protected preparation 和多仓任务返回 unsupported。
2. 生成四事实面 contract，以实验标志运行
   `codex-builder-loop assurance --experimental-v4 validate/start`。
3. 仅在 Core 返回的 candidate worktree 中实现。主线程写业务实现，独立 Tester 只写其授权测试路径，
   Reviewer 只读。

## 持续收敛

重复调用 `assurance --experimental-v4 driver-next`，按 action 执行：

- `builder_implement` / `builder_fix`：在同一 Mission 和 candidate worktree 中实现或修复，提交后提升
  Execution Manifest version、绑定 live candidate HEAD 并完整分类 changed files。
- `tester_author` / `tester_fix`：先用 `prepare-tester` 创建独立 source worktree，再创建或续接 Tester
  thread；Tester 提交后调用 `integrate-tester`，以 source HEAD/blob manifest 更新 Execution，再记录
  `tester` evidence。普通 fixture 或测试实现修正不修改 Mission。
- `verify_machine`：调用 Core 的 `verify-machine`，不得手工伪造 machine evidence。
- `tester_blackbox`：续接同一 Tester thread，对当前 candidate 执行 Execution 中冻结的命令，并以
  candidate worktree、前后 HEAD 和逐命令 returncode 记录 `blackbox` evidence。
- `reviewer_final`：正式前置 evidence 齐全后，由独立 Reviewer 对最终 candidate 给出 evidence；文档门禁
  需要时同一审查同时记录 `doc_review`。
- `rematerialize_target`：调用 Core 的 target rematerialization；无冲突时重验 stale evidence，不修改
  Mission，冲突或覆盖风险才交还用户。
- Tester continuity 丢失：旧 source worktree/branch 未漂移时以 `prepare-tester --replace` 建立新身份并
  重新 author/integrate/evidence；不修改 Mission。旧现场 dirty 或漂移时保留并停止。
- `recover_finalize`：只重放 Core 已持久化的 finalize intent。
- `architecture_review`：同一失败签名达到三次时暂停普通重试，检查设计；不得自动创建新 Mission。
- `finalize`：调用 Core 原子收尾，不自行更新 target ref、stash、清理未知 worktree 或复制 ledger。

每轮都以 Core status/readiness 为事实源；不要从 transcript、Agent 自称完成或 Driver 文本推导 PASS。

## 变化边界

- 目标、外部行为/接口、验收场景或信任边界变化：显式 Semantic Revision，Mission revision 恰好加一。
- 写入/dirty intake 授权扩大：取得用户授权后只更新 Authority，不重定义 Mission。
- Assurance 降级：取得用户决定；增强只补新 gate。
- 实际文件、命令、fixture、timeout、Agent identity 和 candidate：在授权内更新 Execution，只重验依赖
  它的 evidence。

最终成功输出独立行 `FULL_DRIVER_V4_RESULT: finalized`。任何 unsupported、授权扩大、产品取舍、Git
冲突或无法恢复的 finalize intent 输出 `FULL_DRIVER_V4_RESULT: needs_user` 并保留现场。
