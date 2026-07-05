[保质期: V6.0 规划启动, owner: hongyu, 正向归宿: CHANGELOG.md]

# cc-builder-loop 当前计划

> 覆写式维护（§8）：只保留当前态，历史进 CHANGELOG。

## 当前阶段：V5.8.1 已发布（2026-07-06）

V5.7 E2E framework redesign: verify + quality dual-track judge。tester 三层评估（L1 hard_rules → L2a verify → L2b quality），case schema 从 llm_judge 改为 judge:{verify, quality}。

**观察期**：
- tampering 检测迁移到 reviewer：**2026-07-15 前无漏判真篡改 → 关闭**

**阻塞项**：
- schema-out：等 CC Agent tool 支持 schema 参数（当前仍不支持，仅 Workflow agent() 有）
- spec contract / dashboard：大工程，需实战数据驱动

**下一步触发条件**：
- tampering 观察期 7/15 到期 → 评估关闭或加回轻量机器护栏
- CC Agent tool 支持 schema → 解封 schema-out（reviewer/tester 输出契约化）
- 积累 ≥3 个 V5.7 E2E 实战数据 → 评估 quality judge 准确率
