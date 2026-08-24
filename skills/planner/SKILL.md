---
name: planner
description: 独立的通用规划层：冻结目标、范围、设计决策、验收标准和实施步骤，可交给 Codex 原生执行，也可由 Builder-loop adapter 投影为 Full Driver v4 contract。只规划，不实现、不启动 Builder run；普通原生计划和 Builder 实验必须保持显式分流。
---

# 通用 Planner

这是规划层，不是 Builder-loop runtime。它负责把用户目标整理成一份可执行、可审查的语义计划，
但不承担 Builder 的角色调度、独立证据、ledger、worktree finalize 或恢复编排。

## 适用范围

- 用户显式调用 `$planner`，或 Plan mode 选择「Planner + Codex 原生执行」时使用。
- 只规划和冻结，不直接修改代码、文档、配置，不启动 run，也不调用 `$builder`。
- Builder-loop 需要高保证执行时，由 `$builder-loop-planner` adapter 消费同一份语义计划；
  adapter 可以增加 v4 的 authority、assurance 和 execution 约束，但不能另写第二份产品方案。

## 建立计划

1. 读取项目 `AGENTS.md`、适用的设计哲学、文档政策、Git HEAD/branch、测试布局和可复制的验证入口。
2. 先给出用户可观察的结论、范围、成本和退路；只有重大、难逆或范式级的真实分叉才使用
   `request_user_input`，问题与选项使用白话，内部运行时术语只能在后面补充。
3. 从目标和公开行为推导精确的实现边界、只读验证输入、测试入口和需要用户参与的动作。
   不用目录级通配、未来猜测或“等实现失败再扩权”代替实现前的闭包。
4. 明确哪些属于稳定语义，哪些只是执行信息。目标、外部行为、接口、验收强度、信任边界和授权范围
   必须冻结；命令表达、fixture 和资源参数在不改变语义时可由执行阶段修正。
5. 让每个新增文件、接口、依赖和验证都能对应当前目标、明确演进压力或必要交付基础设施；
   未能对应的内容留到以后，不预置未知未来。

## 计划输出

计划只保留一份语义事实，至少包含：

- 目标与不做什么；
- 设计原则及其对本任务的具体推导；
- 变更文件、ownership 和依赖闭包；
- 用户可观察的验收场景、机器检查和测试策略；
- 风险、失败边界、回退路径和需要用户决定的事项；
- 明确的执行路线：`native_codex` 或 `builder_loop`。

### `native_codex`

- 输出普通 Codex 可直接实施的计划，然后停止规划。
- 不生成 `assurance-v4-contract`、`assurance-v4-decision` 或 `BUILDER_HANDOFF_READY`。
- 不调用 Builder Skill，不创建 Builder ledger；下一轮原生 `Implement the plan.` 仍由 Codex
  原生能力执行。
- 原生执行可以按项目规则创建开发 worktree、修改文件和运行测试，但不能声称拥有 Builder
  的独立 Tester/Reviewer/evidence/finalize 结论。

### `builder_loop`

- 只把同一份语义计划交给 `$builder-loop-planner` adapter。
- adapter 负责生成并验证唯一的 Assurance v4 contract；只有验证返回顶层 `READY` 且
  `admission.status=READY`，才允许输出 `BUILDER_HANDOFF_READY`。
- 普通计划执行失败不会自动升级为 Builder；需要切换路线时重新显式选择并由 adapter 重新冻结
  执行契约。

## 路线与连续性

- 路线选择改变的是执行承载，不应偷偷改变产品目标、授权范围或验收语义。
- 已接受的 `$planner` 计划切换到 Builder 时，adapter 应复用其语义内容，只补齐可验证的 v4
  执行事实；不要复制一份看似相同但可漂移的计划。
- 已接受的 Builder handoff 不能反向降级为普通原生执行；Builder runtime 的失败由其自身
  contract 和 ledger 处理。
