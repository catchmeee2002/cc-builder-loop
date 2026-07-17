[保质期: #69 判据接回完成, owner: hongyu, 正向归宿: CHANGELOG.md]

# cc-builder-loop 当前计划

> 覆写式维护（§8）：只保留当前态，历史进 CHANGELOG。

## 当前主线：#69 判据接回（进行中）

本仓自身的纯机器判据层是空的——`.claude/loop.yml` 的 `pass_cmd` 是 `cmd: "true"`（恒真），fixture 无聚合 runner 且不被 loop 引用，导致红断言可以穿过任意多轮 loop / reviewer / merge 不被发现（违反原则七 dogfooding + 原则一 判据分层）。

**详细剩余清单以 GitHub issue #69 的 comment 为唯一来源**，本节只留导航。

- **已完成**：worktree Stop-hook 探针 no-op 修复（commit `3a1c9e2`，已进 main）。原 V4.5 内联探针在 cwd 落 `.claude/worktrees/` 时恒判 no-op → Stop hook 在 worktree 模式全失效（PASS_CMD safety net / e2e 注入 / reviewer 触发均不发生），被空判据长期掩盖。8 个 fixture 红→绿，6 个原绿零回归。
- **剩余四步**：① 清 7 个红 fixture ② 写 fixture 聚合 runner（`run-fixture.sh` 是单场景脚本，非套件 runner）③ `pass_cmd` 从 `"true"` 接回 runner（撞 reward hacking 警戒线，需用户授权）④ 补「pass_cmd 不许恒真」元判据（`true` / `:` / `echo` 形式合法，#67 的 FATAL 拦不住）
- **接手触发器**：
  - 改 hook 或跑 e2e fixture → 用 bare 模式（`setup-builder-loop.sh --no-worktree "<task>"`，flag 必须在任务描述前，见 #54），避免「用 worktree loop 修 worktree 相关代码」的自指
  - 判 fixture 删除集 / 文件依赖关系 → 先 `ls` 确认文件真实存在再下结论，不采信二手分类
  - 动 #69 标注的疑似 REAL_BUG（老 V1.x state 的 run_cwd 解析、`abandon-loop.sh` 的 stash_ref 尾随空格）→ 改代码前先自行复现验证

## 观察期

- tampering 检测迁移到 reviewer（#18）：**2026-07-15 已到期**，待关闭或延期

## 阻塞项

- schema-out：等 CC Agent tool 支持 schema 参数（当前仍不支持，仅 Workflow agent() 有）
- spec contract / dashboard：大工程，需实战数据驱动

## 下一步触发条件

- #18 观察期已过期 → 评估关闭或加回轻量机器护栏
- CC Agent tool 支持 schema → 解封 schema-out（reviewer/tester 输出契约化）
- 积累 ≥3 个 V5.7 E2E 实战数据 → 评估 quality judge 准确率
