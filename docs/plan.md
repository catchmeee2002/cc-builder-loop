[保质期: V4.0 规划启动, owner: hongyu, 正向归宿: CHANGELOG.md]

# cc-builder-loop 当前计划

> 覆写式维护（§8）：只保留当前态，历史进 CHANGELOG。

## 当前阶段：V3.6 实战烘烤（2026-06-16 起）

**决策**：V3.6 已发布，不急于修补中优 case 或规划 V4.0。让 V3.6 在业务项目中跑 3-5 个任务，收集实战数据后再决定下一步。

**等什么数据**：
1. reviewer 轮次是否从 2-3 降到 1-2（review_focus + 假设 frame 的效果）
2. 文件地图校验是否减少了 reviewer 🔴（plan 精确度提升）
3. 有没有新的高频痛点从业务侧写入 improvements.md

**阻塞项**：
- schema-out：等 CC Agent tool 支持 schema 参数（当前 v2.1.177 仍不支持）
- V4.0 三方向（spec contract / reviewer 并列收敛 / dashboard）均为大工程，需实战数据驱动优先级

**下一步触发条件**：
- 收集到 ≥3 个任务的 reviewer 轮次数据 → 评估 V3.6 效果
- improvements.md 出现新高优 → 立即处理
- CC Agent tool 支持 schema → 解封 schema-out，启动 V3.7 或 V4.0
