[保质期: V4.0 规划启动, owner: hongyu, 正向归宿: CHANGELOG.md]

# cc-builder-loop 当前计划

> 覆写式维护（§8）：只保留当前态，历史进 CHANGELOG。

## 当前阶段：V4.6 已发布，继续烘烤（2026-06-29 起）

V4.6 新增 CC 内置 worktree 干扰防御（setup 自动设 bgIsolation: none + SKILL.md 禁令）。

**观察期**：
- tester 写主仓：策略 5 phase 过滤已修，**2026-07-02 前无复现 → 确认关闭**

**待验证（等实战数据）**：
1. reviewer 轮次是否从 2-3 降到 1-2（V3.6 review_focus + 假设 frame）
2. 文件地图校验是否减少 reviewer 🔴

**已知中优 open**（不急）：
- --reuse-worktree state 路径错（创建在 worktree 内而非主仓）
- 步骤 5 /memory 重复确认

**阻塞项**：
- schema-out：等 CC Agent tool 支持 schema 参数（当前 v2.1.177 仍不支持）
- V4.0 三方向（spec contract / reviewer 并列收敛 / dashboard）均为大工程，需实战数据驱动优先级

**下一步触发条件**：
- tester 写主仓 7/2 前复现 → 重新立项
- 收集到 ≥3 个任务的 reviewer 轮次数据 → 评估 V3.6 效果
- CC Agent tool 支持 schema → 解封 schema-out
