# Changelog

## Unreleased

- 将全局 Plan mode 入口由强制加载 Planner 改为每次通过 `request_user_input` 选择 Codex 原生
  Plan 或 Builder-loop Planner；只有后者生成可由 `$builder` 执行的冻结契约。
- 将 `unit-test-spec` 与 `documentation-spec` 升级为 schema v2，并明确拒绝旧 v1 计划；非 L1
  计划在 `spec_head` 存在 `.claude/loop.yml` 时必须省略 `test_context.runner`，不存在时才由计划
  提供 runner，消除验证命令双来源。
- 为 `plan-validate` 增加仓库上下文和 `effective_verification_source`，与 `start` 共享只读
  preflight，在创建 run/worktree 前统一验证目标 HEAD、supersession、runner 安全、ownership
  与冻结依赖。
- 修复同一 Tester/Reviewer thread 的 follow-up 只有 `SubagentStop`、没有第二次
  `SubagentStart` 时 ledger 保留首次 turn 结果的问题。新增 `prepare-follow-up` dispatch 契约，
  在发送 follow-up 前失效旧 evidence、记录 pending turn，并由实际 terminal event 绑定新
  `turn_id`、结果、HEAD 与 Reviewer prerequisite snapshot。

## Codex-native 0.1.0 — 2026-07-19

- 从 `main` 建立长期独立的 `codex-native` 分支；Claude Code 版本继续由 `main` 维护。
- 将用户入口固定为 `/plan <需求>` 后显式调用 `$builder`，安装器为 Plan mode 注入
  Planner Skill 选择规则，不改变普通 Codex 改码行为。
- 以根线程作为 Builder；`parallel_ready=true` 时 Tester 与 Builder 从同一 `spec_head` 并行，
  串行计划则将精确公开文件发布为隔离 HEAD/manifest 后再启动 Tester，并冻结发布路径。
- 将测试目标、ownership、验收标准和迭代上限的修订统一为新 run：旧 run 先 abandon 保留
  现场，再经 `/plan` 提升 `plan_revision` 并重新调用 `$builder`。
- 新增确定性 runtime CLI、ledger、ownership gate、机器验证、agent lifecycle 记录、证据
  HEAD 失效、冲突安全停止和单次 squash commit。
- 对外只保留 L1 纯 Markdown 特例；其他任务统一进入 `L2/L3` 非 L1 交付路径，不再维护两套
  L2/L3 流程。
- Builder 继续负责文档维护，Reviewer 在 Phase D 按文档政策审计；未拆分独立 Doc Reviewer。
- 新增 Codex Skills、Tester/Reviewer custom agents、官方 lifecycle hooks 与隔离安装/卸载流程。
- 随安装部署默认文档政策；项目可提供更具体政策覆盖，避免 Reviewer Phase D 依赖作者机器路径。
- 要求所有非 L1 run 取得 Tester author `tests_ready` 和同 thread blackbox `pass` 证据。
- 将 Tester author turn、空 tree integration 和已完成 turn 防重放写入 ledger；Tester/Reviewer 新
  turn 会使本角色旧 evidence 失效。
- 将 blackbox evidence 绑定 candidate worktree、实际命令、returncode 和执行前后 HEAD，并按
  ownership 将测试修复路由回原 Tester。
- 为 L1 增加冻结 planning-time HEAD、revision 与精确文档写边界的 `documentation-spec`；修订
  计划通过旧 run id/plan digest 验证 abandoned supersession。
- 拒绝反向/恒真 runner，并保护 Makefile、pytest/ruff 配置和 package manifest 等可识别验证
  控制文件；repository wrapper 还要求在 `spec_head` 已存在且为仓库内普通文件，并拒绝 PATH
  override、symlink 与仓库外 target。
- 将 `max_iterations` 落入 ledger 状态机；机器验证改在 candidate 的临时干净 worktree 执行，
  每次 attempt 保留独立日志；最终提交改在临时 ref/worktree 运行目标仓库 hooks、验 tree 后再以
  expected-old compare-and-swap 更新目标分支，并以冻结 run mutation、重验 gate 的持久化
  finalize intent 恢复 CAS 前后的进程中断。
- 将 Reviewer pass 绑定到 turn 开始与完成时的 Tester integration、机器验证和 blackbox snapshot，
  防止先审查后补证据；L1 仍只绑定干净候选与冻结文档路径。
- 让 lifecycle Hook 能从 target、Builder、Tester worktree 定位同一 ledger，并为并发 start 增加
  repository 级锁；损坏 ledger 不再被解释为“无 active run”。
- 将安装器配置更新改为跨 `hooks.json`/`AGENTS.md` 的事务，保留同 entry 的外部 handler，且不再
  覆盖固定 `.bak` 文件。
- 移除 Tester/Reviewer custom agent 的固定模型配置，使其继承根线程模型和推理强度。
- 明确安装产物为指向 checkout 的符号链接，并补充自定义 `CODEX_HOME` 路径和移动约束。
- 将全局 AGENTS 托管块缩为 `/plan` 与显式 `$builder` 两条触发规则；非空
  `AGENTS.override.md` 会遮蔽该块，因此安装时 fail closed。
- 删除本分支中旧的 Claude Code commands、Markdown agents、Stop-hook 编排脚本和旧 fixture；
  同时移除旧 `.claude` plan、review report、trace、settings 与本地状态快照；这些实现及其完整历史
  仍保留在 `main` 和 Git 历史中。
- 建立不依赖真实模型的契约测试，覆盖计划、并行 ownership、Tester dirty state、agent
  continuity、evidence HEAD、Stop gate、schema、安装器、Git 冲突与 finalize 清理。
