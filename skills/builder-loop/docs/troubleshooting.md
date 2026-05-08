# 排查手册 — cc-builder-loop 已知问题

> 从 CLAUDE.md §7 外移。排查 stop hook / judge / worktree / state 相关问题时查阅。

## 7.1 Stop hook 未触发测试 — 僵尸 state 文件 bug（V1.8.1 修复）

**现象**：builder 回复"✅ loop 已活跃"，但 Stop hook 没跑测试，session 直接停下。

**根因**：Stop hook 读到 `active=false` 的僵尸 state 后正确放行——真正问题是僵尸 state 本身的存在。

**修复**（V1.8.1）：遇到 `active != true` 的 state → 归档到 `legacy/<ts>-zombie_inactive.bak` + 放行。

**排查步骤**：
1. `ls -la <project_root>/.claude/builder-loop/` 查看 `state/` 或 `legacy/` 目录
2. `tail -50 <project_root>/.claude/builder-loop/stop-hook-debug.log` 查最近 hook 触发决策点
3. 含 `phase=exit` + `reason=zombie_inactive` → V1.8.1 修复已生效
4. 其他退出原因按 `exit.reason` 字段定位（详见 §7.11）
5. 一键诊断：`bash ~/.claude/skills/builder-loop/scripts/diagnose-stop-hook.sh`

## 7.2 Commit-msg hook 拦截导致 auto-commit 失败

**现象**：loop 跑到 merge-worktree-back.sh 时失败，错误信息含 "commit message" 或 "cr_id_skip"。

**根因**：项目启用严格 commit-msg hook 时，auto-commit message 格式必须合规。

**修复**（V1.8.1）：message 改为 `"chore(loop): [cr_id_skip] Auto-commit iter N"`。

**排查**：检查 merge-worktree-back.sh 的 commit message 是否含 `[cr_id_skip]`。

**补充**（V2.7）：若 diagnose-stop-hook.sh 报「[1/6] verdict=ok 但只显示 5 条」→ 表示方案是 max，hook 注册正确，tester-write-guard.sh 不需要出现。

## 7.3 Judge agent 全部判定都被降级（V1.9+）

**现象**：`judge-trace.jsonl` 每行 `downgraded:true`，loop 退化为 PASS_CMD 二值判据。

**排查步骤**：

1. 跑 self-check：
   ```bash
   bash ~/.claude/skills/builder-loop/scripts/run-judge-agent.sh --self-check
   ```
   - `ERROR: missing credentials` → 检查 `ANTHROPIC_API_KEY` env
   - **正版 Max CC 用户**（V2.1+）：写 `~/.claude/skills/builder-loop/judge-env.sh`：
     ```bash
     # 方案 A：copilot-proxy
     export ANTHROPIC_API_KEY=sk-666
     export ANTHROPIC_BASE_URL=http://localhost:4142
     # 方案 B：独立 key
     # export ANTHROPIC_API_KEY=sk-ant-...
     ```

2. 看降级原因分布：
   ```bash
   cat <project_root>/.claude/builder-loop/judge-trace.jsonl | python3 -c "
   import json, sys
   from collections import Counter
   c = Counter()
   for line in sys.stdin:
       try:
           obj = json.loads(line)
           if obj.get('downgraded'):
               c[obj.get('downgrade_reason', '?')] += 1
       except: pass
   print(c.most_common(10))
   "
   ```

3. 常见原因：
   - `missing_credentials` → 见步骤 1
   - `timeout` → 检查 `ANTHROPIC_BASE_URL`
   - `http_401` / `http_403` → token 失效
   - `parse_error` → 查 `model_used` 字段
   - `low_confidence` → 调整阈值

4. 回退到 V1.8：`loop.yml` 加 `judge: { enabled: false }`

## 7.4 worktree 内 loop.yml 不存在（V2.0+）

**现象**：stderr 出现 `[run-pass-cmd] ⚠️ ... loop.yml 不存在 ... fallback 到主仓`。

**根因**：git worktree add HEAD 只拷贝 tracked 文件——loop.yml 未 commit 则 worktree 看不到。

**处理**：写 loop.yml → `git add && git commit` → 再调 setup。已发生时 fallback 主仓不阻断。

## 7.5 PASS_CMD 跑了主仓而非 worktree（V2.0 已修）

**现象**：worktree 内改 loop.yml 加 stage，本轮 PASS_CMD 没跑新 stage。

**根因**（V1.7-V1.9.x）：`run-pass-cmd.sh` 死代码读旧路径 → RUN_CWD 永远是主仓。

**修复**（V2.0）：state schema 增 `main_repo_path`，`project_root` 改为"干活的地方"。

**自检**：state 应同时含 `project_root: <worktree>` + `main_repo_path: <主仓>`。

## 7.6 sonnet 降级到 haiku 后再不切（V2.1+）

**现象**：`judge-trace.jsonl` 的 `model_used` 一直是 `claude-haiku-4-5`。

**根因**：sonnet 连续失败 2 次后切 haiku，本 loop 内不再重试 sonnet。

**何时重置**：
- loop PASS 后自动重置（state 删除）
- 手动 rm state 文件后下次 setup 重建

**判断 sonnet 是否真不可用**：
```bash
curl -sS -X POST $ANTHROPIC_BASE_URL/v1/messages \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"claude-sonnet-4-6","max_tokens":10,"messages":[{"role":"user","content":"ping"}]}' \
  --max-time 10
```

**禁用降级链**：`loop.yml.judge` 设 `fallback_model: ""`。

## 7.7 用户主仓直接 commit 后 loop 没自动验证（V2.2+ 行为变更）

**现象**：主仓直接改代码 + commit + 关 CC，期望 stop hook 兜底但没触发。

**原因**：V2.2 砍了 `HAS_RECENT_COMMIT` 触发器，bootstrap 只看未提交工作树改动。

**处理**：
- commit 之前关 CC → 下次开 CC 仍会兜底（HAS_DIFF 非空）
- commit 后关 CC → 手动 `setup-builder-loop.sh "<task>"` 起 loop

## 7.8 主仓 dirty + 手动跑 setup 的预期行为（V2.3+）

**默认行为**：dirty + setup + worktree.enabled →
1. `git stash push -u` 主仓变 clean
2. worktree 创建 + `git stash apply` 还原到 worktree 内
3. PASS → commit 加 `[+N main-dirty]` + drop stash
4. EARLY_STOP → worktree 保留 + 主仓 stash apply 还原

**dirty 排除清单**：`.gitignore` / `.claude/builder-loop/` / `.claude/loop-runs/` / `.claude/loop-trace.jsonl` / `.claude/worktrees/`

**特殊状态拒绝**：rebase / merge / cherry-pick 进行中 → exit 2。加 `--no-stash` 跳过 stash。

**自检**：stderr 含 `📦 主仓 dirty 已 stash`；state 文件 `worktree_mode: "dirty"`。

## 7.9 Reward hacking 检测误触发 / 假阴性（V2.3+）

**误触发**：配置文件含关键词但有充分理由 → AskUserQuestion 第三选项「保留并给理由」。

**禁用**：`loop.yml.judge.reward_hacking_detection: false` 关 Layer 2 正则。

**假阴性盲区**：`--ignore-glob` / `--collect-only` / `pytest.mark.skipif` 等未覆盖（见 `known-risks.md` R6.5）。

**自检**：stop hook stderr 含 `[builder-loop reward-hack-guard]`；`judge-trace.jsonl` 含 `suspected_reward_hack`。

## 7.10 主仓 cwd 仍 setup 进 worktree → stop hook 不跟（V2.4）

**现象**：setup 后 worktree 创建成功，CC session cwd 仍在主仓，stop hook 没跑 PASS_CMD。

**V2.4 缓解**：
1. setup 末尾打 stderr 警告 + cd 建议
2. 唯一 active worktree → locate-state.sh 策略 5 自动绑定
3. 多 active worktree → 不绑，stop hook stderr 列候选

**何时仍哑火**：≥2 个 active state + 主仓 cwd → 必须 cd 到目标 worktree。

**排查**：
```bash
for f in <repo>/.claude/builder-loop/state/*.yml; do
  echo "=== $(basename "$f") ==="
  grep -E '^(active|worktree_path):' "$f"
done
```

## 7.11 stop hook debug log 格式 + 排查指南（V2.5）

**位置**：`<project_root>/.claude/builder-loop/stop-hook-debug.log`

**格式**：每行 NDJSON `{"ts","session","cwd","slug","phase","details"}`

**滚动**：1 MB（`BUILDER_LOOP_DEBUG_LOG_MAX_BYTES` env 可调），保留 5 个 `.1-.5`

**Phase 表**（10 种）：

| phase | 时机 | 关键 details |
|-------|------|-------------|
| `entry` | hook 入口 | `cwd`, `transcript_path` |
| `locate_result` | locate-state.sh 后 | `state_file`, `matched` |
| `flock_acquire` | flock 结果 | `lock_file`, `acquired` |
| `bootstrap_check` | 触发条件判定 | `decision`, `changed_files_count`, `doc_only` |
| `pass_cmd_start` | 跑 PASS_CMD 前 | `iter`, `run_cwd` |
| `pass_cmd_result` | PASS_CMD 完 | `result`, `last_stage`, `log_path` |
| `merge_result` | merge 后 | `merge_action`, `merge_last_line` |
| `judge_result` | judge 后 | `action`, `downgraded`, `confidence` |
| `early_stop` | 早停时 | `iter`, `reason` |
| `exit` | 退出前 | `code`, `reason` |

**`exit.reason` 常见取值**：

| reason | 含义 |
|--------|------|
| `no_project_root` | 项目未接入 loop |
| `lock_held_by_other` | 另一 hook 在跑 |
| `no_diff` / `doc_only` | 无改动 / 纯文档 |
| `zombie_inactive` | 僵尸归档 |
| `pass_done` | PASS 正常退出 |
| `need_arbitration` | rebase 冲突 |
| `fail_continue` | FAIL 注入 feedback |
| `judge_continue_nudge` | judge nudge |
| `early_stop_<reason>` | 早停 |

**典型排查**：

1. **hook 没触发**：`tail -10 debug.log` 看有无 `entry`；无 → `diagnose-stop-hook.sh`
2. **反复 NOOP**：`grep bootstrap_check debug.log | tail -10` 看 `decision`
3. **某 session 完整流程**：`grep '"session":"<8chars>"' debug.log`

## 7.12 用户主动 abandon loop（V2.6 Phase 1）

**何时使用**：loop 进行中 PASS_CMD fail 与本期改动无关 → 想停掉 loop。

**触发方式**：

1. **自然语言**：收到 `[builder-loop ...]` 注入后说「停下loop / 停掉loop / 停止loop / 中止loop / abandon loop」
2. **直接调脚本**：
   ```bash
   bash ~/.claude/skills/builder-loop/scripts/abandon-loop.sh <state_file> "<reason>"
   ```

**abandon 后的状态**：
- state 归档到 `legacy/<ts>-abandon_<reason>.bak`
- trace event "ABANDON" 写入 `loop-trace.jsonl`
- dirty 模式 → 主仓 stash apply 还原
- worktree + branch 保留

**abandon 后选项**：
```bash
# cherry-pick 本期改动
git -C <project_root> cherry-pick <commits>
# 丢弃
git -C <project_root> worktree remove --force <worktree_path>
git -C <project_root> branch -D loop/<slug>
# 重新进 loop
bash ~/.claude/skills/builder-loop/scripts/setup-builder-loop.sh "<task>"
```

**拒绝条件**：reason 缺失 / state 不存在 / active != true / 字段损坏。
