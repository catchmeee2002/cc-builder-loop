# V3.0 reviewer-as-gate — dotfiles `builder.md` 同步记录

> ✅ **已完成**：dotfiles `~/.hongyu.liao_debian12/my-dotfiles` commit `3349374`（feat(claude): Update builder.md for V3.0 reviewer-as-gate）已落实下述 4 处改动并 push origin/main。本文档转为历史档案，留作下次类似改动的样板。
>
> 历史背景（保留供回溯）：

> ⚠️ **POST-MERGE CRITICAL**：cc-builder-loop V3.0 主线 merge 后**必须立即**同步 dotfiles `builder.md`，否则线上 builder 行为会出问题：
> - hook 写 `state.phase=passed_pending_review` 但 `state.active` 仍是 `true`
> - 现版 builder.md 硬规则「自闭环活跃期间（active=true）绝对不 spawn reviewer」会**误判**
> - builder 收到 hook stderr「请继续 spawn reviewer」却被旧硬规则拦住 → 卡死流程
>
> 落实步骤见末尾「同步操作」段。**本仓 V3.0 主线 merge 后第一件事就是去 dotfiles 仓改并 commit，再开第二条 cc-builder-loop loop**。

> 本仓 V3.0 改造涉及 `~/.claude/commands/builder.md` 的 builder 行为约束更新。
> 因 cc-builder-loop loop 在 worktree 内运行、不能跨仓改动 dotfiles，本期 commit 后必须**单独**到 dotfiles 仓 `~/.hongyu.liao_debian12/my-dotfiles/claude/.claude/commands/builder.md` 落实下述改动并 commit。

---

## 改动 1：步骤"完成后触发 Reviewer Subagent" — 加 V3.0 worktree 模式分支

**位置**：步骤 3「收到通知后处理」之前的步骤 1-2 段。

**新增**：在「步骤 2：获取 diff + spawn reviewer」前加一道**自检前置**：

```markdown
**步骤 1.5（V3.0）：worktree 模式 builder 自检**

仅当 cwd 在 `<repo>/.claude/worktrees/<slug>/` 下时跑（普通对话不触发）：

```bash
case "$(pwd)" in
  */.claude/worktrees/*)
    slug=$(basename "$(pwd)")
    state=".claude/builder-loop/state/${slug}.yml"
    if [ -f "$state" ]; then
      phase=$(grep '^phase:' "$state" | head -1 | awk '{print $2}' | tr -d '"')
      [ "$phase" = "passed_pending_review" ] && echo "AUTO_SPAWN_REVIEWER"
    fi
    ;;
esac
```

输出 `AUTO_SPAWN_REVIEWER` → 命中 V3.0 reviewer-as-gate：
- Read 主仓 state.yml 拿 reviewer_pending 段（含 reviewer_files / report_path / diff_file）
- 直接 spawn reviewer（不再走 reviewer-params.json 路径）

**反馈处理分支**：

| reviewer 反馈 | 动作 |
|---|---|
| 0 🔴 通过 | `bash ~/.claude/skills/builder-loop/scripts/merge-and-cleanup.sh <state_file>` 合主线 + 删 worktree + 删 state |
| 🟡 / 🔵 非阻塞 | 在 worktree 内 Edit/Write 修复 → dirty 出现 → 下一轮 stop hook 触发 → L1 闸自愈回 phase=active → 重跑 PASS_CMD |
| 🔴 阻塞 | AskUserQuestion 让用户选 [继续修 / abandon-loop.sh] |
```

## 改动 2：长对话场景的 pause 用法

**位置**：步骤 1「先计划，后动手」段尾部。

**新增**：

```markdown
**[V3.0] 需要长对话静默 hook 时**：

```bash
# 暂停（hook 跑到此 slug 时静默 exit 0）
touch .claude/builder-loop/<slug>.pause

# 恢复
rm .claude/builder-loop/<slug>.pause
```

适用场景：跟用户深度讨论方案改造、长 PoC 排查、跨多个文件慢慢看代码。
不适用：放弃 loop（用 abandon-loop.sh）/ 单纯回避 PASS_CMD 失败（违反 reward hacking 防御）。
```

## 改动 3：老 state（缺 phase 字段）处理指引

**位置**：「检查 loop.yml」段「⛔ Reward hacking 警戒」之后。

**新增**：

```markdown
**[V3.0] 老 state 兼容**：hook 检测到 state 缺 phase / last_iter_head 字段 → stderr 注入「检测到老版 state X，无法走 V3.0 流程」警告。Builder 收到后 AskUserQuestion 让用户选：
- abandon-loop.sh 接手老 state（推荐，避免技术债）
- 手动 add `phase: active` + `last_iter_head: <start_head>` 到 state 文件（不推荐）
- 手动 spawn reviewer 跳过 hook 流程
```

## 改动 4：硬规则段更新 — 区分 active vs passed_pending_review

**位置**：「⛔ 硬规则：自闭环活跃期间 ...」段。

**改动**：

把「自闭环活跃期间」明确为「state.phase=active 期间」（不包括 passed_pending_review）。passed_pending_review 状态下**应该** spawn reviewer——这是 V3.0 reviewer-as-gate 的核心动作。

```markdown
> **⛔ 硬规则：state.phase=active 期间，绝对不 spawn reviewer/doc-maintainer/commit。**
> phase=passed_pending_review 时 builder 自检触发 spawn reviewer 是 V3.0 必经流程，不算违规。
```

---

## 同步操作

```bash
# 1. cc-builder-loop 主线 merge 完成（V3.0 进 main 后）
cd ~/.hongyu.liao_debian12/my-dotfiles

# 2. 按上述 4 处改动手动编辑 builder.md
$EDITOR claude/.claude/commands/builder.md

# 3. commit
git add claude/.claude/commands/builder.md
git commit -m "feat(claude): [cr_id_skip] Update builder.md for V3.0 reviewer-as-gate"

# 4. 软链已存在（~/.claude/commands/builder.md → my-dotfiles/...），改即时生效
```

## 同步前的兜底

V3.0 hook stderr 注入文案已经包含完整的 builder 后续流程指引（见 `scripts/builder-loop-stop.sh` PASS 路径的 `<<PASS_MSG`）。即便 builder.md 没及时同步，builder 收到 hook stderr 后仍能按指引走 reviewer-as-gate 流程。

文档同步是为了让 builder 在不读 hook 输出的场景下也能正确工作（例如：用户主动问 builder「现在该做什么」、builder 回顾流程等）。
