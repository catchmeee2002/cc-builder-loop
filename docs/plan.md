[保质期: V4.0 规划启动, owner: hongyu, 正向归宿: CHANGELOG.md]

# cc-builder-loop 当前计划

> 覆写式维护（§8）：只保留当前态，历史进 CHANGELOG。

## 当前阶段：V3.7 热修 + 继续烘烤（2026-06-17 起）

V3.6 烘烤首日即触发两条并发 session 越界 case → 热修 owner_session_id 机制（V3.7）。烘烤继续。

**V3.7 已落地**：
- stop hook 加 `owner_session_id`：首次绑定写入 session_id，后续校验匹配，不匹配 → stderr 警告 + skip。消化 2 条并发 session 越界 case
- dirty-stash-flow fixture timeout 60s→120s（仍挂，需修清理逻辑）
- write-guard 加诊断日志（排查 tester 写主仓断裂点）

**待验证（等实战数据）**：
1. reviewer 轮次是否从 2-3 降到 1-2（V3.6 review_focus + 假设 frame）
2. 文件地图校验是否减少 reviewer 🔴
3. tester 写主仓断裂点是哪个（诊断日志已加，等下次复现）

**当前高优 open**：
- tester subagent 在 worktree 模式下写文件到主仓（3 次复现，诊断日志已加等复现定位）

**已知中优 open**（不急）：
- fixture 清理挂起（rm -rf 含 worktree 临时仓卡住）
- --reuse-worktree state 路径错（创建在 worktree 内而非主仓）
- 步骤 5 /memory 重复确认

**阻塞项**：
- schema-out：等 CC Agent tool 支持 schema 参数（当前 v2.1.177 仍不支持）
- V4.0 三方向（spec contract / reviewer 并列收敛 / dashboard）均为大工程，需实战数据驱动优先级

**下一步触发条件**：
- tester 写主仓再次复现 → 读诊断日志定位断裂点 → 修
- 收集到 ≥3 个任务的 reviewer 轮次数据 → 评估 V3.6 效果
- CC Agent tool 支持 schema → 解封 schema-out
