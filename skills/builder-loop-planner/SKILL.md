---
name: builder-loop-planner
description: 在 Codex Plan mode 中为显式选择的 Builder-loop 实验路线生成并验证 Assurance v4 四事实面 contract，冻结目标、权限、独立验收与执行入口，并为紧邻的原生 Implement the plan. 输出一次性 Builder handoff。仅在用户通过 Plan 选项卡选择 Builder-loop 实验，或显式调用 $builder-loop-planner 时使用；普通原生 Plan、问答和只读分析不要使用。
---

# Builder-loop 实验 Planner

只规划和验证，不实现、不启动 run。该路线由 `/plan` 中的一次选项进入，验证后交给 `$builder`；
公共 legacy v2/v3 `start` 保持关闭。

## 建立方案

1. 读取项目 `AGENTS.md`、设计哲学、文档政策、Git HEAD/branch、测试布局和可复制执行的验证命令。
2. 只在答案会改变目标、权限或验收强度时使用 `request_user_input`。不要恢复固定问卷。
   只有存在真实、重大、难逆或范式级设计分叉时，才读取
   [方案取舍与演进](references/design-decisions.md)；局部可逆任务不虚构备选方案。
3. target dirty 默认不进入任务。任务确实依赖 dirty 时，先用选项卡取得 exact-path 授权，再计算每个
   普通文件当前内容的 SHA-256；不得 stash、复制、清理或授权目录/glob。
4. 按可观察行为写人类可读方案，同时生成一份 schema v4 contract。完整读取本 Skill 相对路径
   `../../schema/assurance-v4-contract.schema.json`，不要复制或猜测 schema。

## Contract 约束

- `mission` 只放目标、行为、接口、验收场景和信任边界；只有这些语义变化才提升 revision。
- `authority` 冻结 target branch、Builder/Tester 精确写边界、dirty intake、串行公开前置产物和受保护
  support path。权限扩大必须重新交给用户决定。
- 代码任务的 `assurance.required` 默认精确包含 `tester`、`proof`、`machine`、`blackbox`、`reviewer`；
  纯 Markdown L1 只要求 `reviewer`，不得伪造 Tester、machine 或 blackbox。
- `execution` 初始固定为 `version: 1`、`driver_enforced: true`、`candidate_head: null`、空的
  `builder_files/tester_files/dirty_snapshot/agents`、`tester_source: null`。Agent identity 只能在真实 spawn
  后由专用事务写入，Planner 不预填。
- `assurance.machine_commands` 冻结机器命令；`execution.commands` 冻结独立 blackbox 命令。argv 必须是
  字符串数组，命令必须真实失败时返回非零，不使用 shell 恒真包装。
- 单仓本地代码或 L1 文档属于当前实验范围；外部设备、远程部署和多仓原子事务选择 Codex 原生 Plan。

## 验证与交接

1. 把 contract 的规范 JSON 通过 stdin 交给：

   `codex-builder-loop assurance --experimental-v4 validate --contract -`

2. 只有返回 `status=READY` 才输出最终方案。最终方案必须包含唯一、完整且与已验证字节语义相同的：

~~~~markdown
<!-- assurance-v4-contract -->
```json
{ "schema_version": 4 }
```
<!-- /assurance-v4-contract -->
~~~~

3. contract marker 位于方案正文内；不要再输出 legacy `unit-test-spec`、`documentation-spec`、
   `workspace-intake` 或第二份计划状态。
4. 在方案正文之外输出独立行：

`BUILDER_HANDOFF_READY`

验证失败、用户选择原生 Plan、任务超出实验范围、方案仍有未决产品取舍时不得输出该标记。
