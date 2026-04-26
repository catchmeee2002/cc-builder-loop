# cc-builder-loop 待固化改进

> 时间倒序。每条按 builder.md 步骤 5 模板（触发上下文 / 建议方向 / 优先级）。
> 立项不等于本期实施——A 类候选清单，等独立任务挑出来落地。

## 2026-04-26 V1.8 多状态并行 + worktree 启用时 stop hook 用主仓 cwd 找不到 state

- **触发上下文**：V2.2 落地 session（1781a3be）实测——setup-builder-loop.sh 创建 worktree slug `1777216199-v2-2-tester` 后，CC session cwd 仍是主仓 `/mnt/hongyu.liao_docker/cc-builder-loop`。stop hook 触发时把主仓 cwd 传给 locate-state.sh，策略 1-4 全部不命中（worktree 在 `.claude/worktrees/<slug>/` 下、cwd 不在；策略 3 比对 worktree_path 字段也不匹配主仓 cwd；策略 4 兜底 `__main__.yml` 在 worktree 模式不存在）→ 返回空 → stop hook 走 bootstrap → 跨 session 守门检测到 worktree 存在 → 静默放行 → **永远不跑 PASS_CMD**。绕过靠手动 `cd` 到 worktree。
- **建议方向**：
  1. **setup-builder-loop.sh** 输出醒目提示「⚠️ CC session cwd 仍在主仓，stop hook 找不到 state，请手动 `cd <worktree>` 或在新 CC session 用 `--cwd` 启动」
  2. **locate-state.sh** 加策略 5：主仓 cwd + 同项目下仅 1 个 active worktree state → 自动绑定（多 active 时仍返回空，避免错绑）
  3. e2e fixture 加 case：模拟主仓 cwd + active worktree state，验证 stop hook 能正确命中
- **优先级**：高（工作树启用是 builder-loop 主推荐路径，这个盲区让 stop hook 全程哑火）

## 2026-04-27 install.sh has_entry() 仅比脚本名不比 matcher

- **触发上下文**：V2.2 收尾时整理「改动同步 checklist」（CLAUDE.md §3 末尾）发现：`install.sh` L82 的 `has_entry(arr, cmd_name)` 只检查脚本名是否在 settings.json 任一条目，**不比对 matcher 字段**。后果：把 hook matcher 从 `Read|Grep|Glob` 改成 `Read|Grep|Glob|WebFetch` 后重跑 install.sh，`has_entry` 看到脚本名已存在直接跳过 → settings.json 仍是旧 matcher → 新增的 WebFetch 永远不被拦截。
- **建议方向**：
  1. `install.sh` `has_entry(arr, cmd_name, matcher)` 加 matcher 参数：脚本名匹配且 matcher 也匹配才视为已存在；matcher 不同视为"需更新"（先删旧条目再 append 新条目）
  2. e2e fixture：`test-install-matcher-update.sh` —— install 一次（matcher=A）→ 改 matcher=B → 再 install → 断言 settings.json 该条目 matcher=B
- **优先级**：中（V2.2 没改 matcher，未触发；未来改 matcher 时会静默失效）

## 2026-04-26 uninstall.sh bl_scripts 列表漏 reviewer-timing-check.sh

- **触发上下文**：V2.2 reviewer 审查发现（pre-existing 老 bug，本期未修按"bug fix 不带周边清理"原则留作 A2 候选）。`uninstall.sh` L49 的 `bl_scripts = ["builder-loop-stop.sh", "tester-lock-write.sh", "tester-lock-check.sh", "tester-lock-clear.sh", "tester-write-guard.sh"]` 列表漏 `reviewer-timing-check.sh`，uninstall 后 settings.json 里该 hook 条目残留，下次 install 重复合并造成 hook 执行多次。
- **建议方向**：
  1. `uninstall.sh` L49 的 `bl_scripts` 加 `"reviewer-timing-check.sh"` 一项
  2. 加 e2e fixture：`test-install-uninstall-roundtrip.sh`——install 后 uninstall 应让 settings.json 完全等于 install 前的状态（diff 必须为空）
- **优先级**：中（uninstall 不彻底导致冗余执行，但不影响功能正确性）
