---
name: planner
description: 统一规划入口，明确目标、范围、设计与验收。先由用户选择 Codex 原生执行或 Builder-loop；仅 Builder 路线按需读取契约指导。只规划，不实施或启动 run。
---

# 通用 Planner

用于 `/plan`、显式 `$planner` 和已选路线的规划。两条路线共用一份语义计划。

## 路线准入

- 路线尚未确定时，第一条动作必须用 `request_user_input`，只询问「Planner + Codex 原生执行」或
  「Builder-loop 实验」；回答前不得读取、分析、输出结论或提出其他问题。显式调用 `$planner` 本身
  不代表选择任何路线。托管入口已经取得答案，或用户已明确指定本任务路线时沿用，不重复询问。
- 用户未回答、取消或答案缺失时停止本轮，不调用其他工具、不按推荐或默认项推进。
- 是否使用 Builder 全由用户决定，不按复杂度、风险或历史偏好代选；执行失败也不自动切换。
- `native_codex`：只使用通用规划指令，不读取 Builder 参考文件或 v4 schema，不生成 contract、
  `BUILDER_HANDOFF_READY` 或 ledger，不调用 Builder runtime。
- `builder_loop`：开始调查和规划前读取 [Builder 契约适配](references/builder-contract.md)，
  在同一份计划中补齐权限和独立验收。后续特殊场景按该参考的触发条件继续加载，不预读所有参考。

## 建立计划

1. 读取项目 AGENTS、适用设计哲学、文档政策、Git HEAD/branch、测试布局和可复制验证入口。
2. 先说明用户可观察的结果、范围、成本与退路。只有答案会改变目标、权限或验收强度时才提问；
   使用 `request_user_input` 时问题先写用户能观察到的行为、成本和退路，carryover、turn、supersession、actionable finding 等内部词不能单独
   作为用户标签。用户尚未回答时停止；推荐选项不等于决定。
3. 只有存在重大、难逆或范式级的真实分叉时，读取 [方案取舍与演进](references/design-decisions.md)，
   把价值取舍交给用户决定。只有一条可信路径时说明其他方向被什么约束排除；不存在真实分叉时
   不做方案比较，不把局部可逆改动变成固定问卷。
4. 从目标和公开行为推导实现边界、ownership、依赖闭包、只读验证输入和测试入口。
   不用目录通配、未来猜测或“等实现失败再扩权”代替精确边界。
5. 冻结目标、外部行为、接口、验收强度、信任边界与授权范围；命令表达、fixture、资源参数属于
   执行信息，允许执行阶段在不改变语义和授权的条件下修正。
6. 每个新增文件、接口、依赖和验证必须对应当前目标、明确演进压力或必要交付基础设施。
   不预置未知未来；删除测试说明保留或替代的行为覆盖，文档变更声明归宿。

## 计划与交接

输出一份目标、范围、设计原则及推导、实施步骤、ownership、验收、风险与回退完整的计划，
明确路线为 `native_codex` 或 `builder_loop`。只有重大未决选择才继续提问，不另建计划状态副本。

- 原生路线输出普通 Codex 实施计划后停止；下一轮原生 `Implement the plan.` 由 Codex 原生执行，
  不能声称已取得 Builder 独立 Tester、Reviewer 或 evidence 结论。
- Builder 路线由内部适配指导生成并验证唯一 contract；满足既有准入条件才交接给 `$builder`。
  Planner 不实现、不启动 run、不写 ledger。用户确认最终方案前完成契约验证。
- 用户显式把已接受原生计划切换到 Builder 时复用目标、范围、验收与授权，只补齐契约条件；
  已接受 Builder handoff 的失败按其 contract 和 ledger 处理，不自动降级为原生执行。
