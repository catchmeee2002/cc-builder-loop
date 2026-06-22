# 修复 Builder-Loop 三大严重 Bug

<!-- role:shared -->

## 背景 & 目标

Builder-Loop 在 worktree 模式下存在三个严重 bug，导致：
1. 代码丢失（worktree 未提交改动被 cleanup 删除）
2. Reviewer 误报（读主目录旧代码）
3. MERGED 假阳性（merge 实际是 no-op）

**成功标准**：worktree 模式下代码不丢失 + reviewer 时序正确 + merge 输出真实。

## 预估改动级别

**L2 实现改动** — 现有脚本/prompt 内部逻辑修改，不新增对外接口。

## 约束 & 边界

- 不改 CC 源码，只用 hook/skill/agent 扩展机制
- 兼容非 worktree 模式（worktree.enabled=false 不受影响）
- 兼容已接入项目的 loop.yml（无需用户改配置）

## 技术选型

| 方案 | 描述 | 推荐 |
|------|------|------|
| A: merge 前 auto-commit | merge-worktree-back.sh 在 merge 前自动 `git add -A && git commit` | ✅ 推荐 |
| B: builder prompt 要求 commit | 在 builder.md 中要求 builder 每轮都 commit | ❌ LLM 软约束，不可靠 |
| C: PASS_CMD 中加 commit | loop.yml pass_cmd 末尾加 commit 阶段 | ❌ 侵入用户配置 |

选 A：脚本层硬保证，不依赖 LLM 行为。

## 方案设计

### Fix 1: merge-worktree-back.sh — 合并前 auto-commit + 空合并检测

**问题**：worktree 内改动未 commit → `git merge --ff-only` 是 no-op → `cleanup_worktree` 丢数据

**修复**：在 merge 前插入 auto-commit 步骤：

```
1. 检查 worktree 是否有未提交改动（git -C $WT status --porcelain）
2. 有 → git -C $WT add -A && git -C $WT commit -m "chore: auto-commit by builder-loop"
3. 无 → 继续
4. 检查 worktree 分支是否有新 commit（相对 start_head）
5. 无新 commit → 输出 "NOOP" 而非 "MERGED"（防假阳性）
6. 有新 commit → 正常 merge --ff-only
```

### Fix 2: builder-loop-stop.sh — reviewer 时序硬门禁

**问题**：Builder 在 loop 活跃期提前 spawn reviewer，无硬约束

**修复**：两层防护
- **层 1（prompt 强化）**：builder.md 中明确标注流程顺序，L3 从 "tester→loop" 改为 "tester→等待 loop PASS→reviewer"
- **层 2（stop hook 输出强化）**：PASS 消息中加入 "注意：如果之前已 spawn 过 reviewer，其结果无效（基于旧代码），请忽略并重新 spawn"

### Fix 3: builder.md — reviewer diff 修正

**问题**：合并后 `git diff HEAD` 为空（所有改动已 commit），reviewer 无法审查

**修复**：builder.md 中 reviewer diff 获取改为：
- 从 stop hook PASS 消息中解析 `start_head`
- 用 `git diff <start_head>..HEAD` 代替 `git diff HEAD`
- 或用 `git log -p <start_head>..HEAD` 获取完整改动

## 风险 & 应对

| 风险 | 应对 |
|------|------|
| auto-commit 消息污染 git log | 用特殊前缀 `chore(loop):` 标记，后续可 squash |
| auto-commit 时 worktree 有冲突文件 | 检测冲突状态，有冲突则走 NEED_ARBITRATION |
| builder 仍提前 spawn reviewer（prompt 层面） | stop hook PASS 消息中加 start_head，让 builder 能重新 spawn 正确的 reviewer |

<!-- /role -->

<!-- role:builder -->

## 文件地图

| 文件 | 改动点 |
|------|--------|
| `skills/builder-loop/scripts/merge-worktree-back.sh` | 插入 auto-commit 步骤（核心修复） |
| `scripts/builder-loop-stop.sh` | PASS 消息加 start_head + reviewer 警告 |
| `~/.claude/commands/builder.md`（dotfiles 仓库） | 修正 reviewer diff 命令 + L3 流程时序 |

## 执行任务列表

### Task 1: merge-worktree-back.sh — auto-commit + 空合并检测
1. 在 `read_field` 之后、路径 A 之前，插入 auto-commit 块
2. 检查 `git -C "$WORKTREE_PATH" status --porcelain`
3. 有未提交 → `git -C "$WORKTREE_PATH" add -A && git -C "$WORKTREE_PATH" commit -m "chore(loop): auto-commit iter N"`
4. 检查 `git -C "$WORKTREE_PATH" rev-parse HEAD` vs `START_HEAD`
5. 相同（无新 commit）→ cleanup + 输出 `NOOP`（不输出 MERGED）

### Task 2: builder-loop-stop.sh — PASS 消息增强
1. 在 MERGED case 的 stderr 消息中加入 `start_head=<值>`
2. 加入警告："如果之前已有 reviewer 在后台运行，其基于旧代码，结果无效。请基于当前 HEAD 重新 spawn reviewer。"

### Task 3: builder.md — reviewer diff + 时序修正
1. "完成后触发 Reviewer" 步骤 2 改为：
   - 检查是否来自 loop PASS（stop hook 消息中有 start_head）
   - 有 → `git diff <start_head>..HEAD` 取 diff
   - 无 → 保持 `git diff HEAD`
2. L3 改动分级说明改为："先 spawn tester → **等待 loop PASS 后**再 spawn reviewer"
3. "自闭环活跃期间" 规则加粗强调 + 加具体场景举例

<!-- /role -->

<!-- role:tester -->

## 验收标准

1. **worktree 模式不丢数据**：builder 在 worktree 中改文件但不 commit → loop PASS → 改动出现在主分支 HEAD
2. **MERGED 输出准确**：无新 commit 时输出 NOOP 而非 MERGED
3. **reviewer 收到正确 diff**：reviewer diff_summary 包含实际改动而非空
4. **非 worktree 模式不受影响**：worktree.enabled=false 时所有行为不变

## 测试计划

- 测试深度：深度
- 关键场景：
  1. worktree 有未提交改动 → merge-worktree-back.sh 自动 commit 并 merge 成功
  2. worktree 无改动 → 输出 NOOP
  3. worktree 有 commit 无未提交 → 正常 merge（回归测试）
  4. 非 worktree 模式 → NOOP（回归）
  5. stop hook PASS 消息包含 start_head

<!-- /role -->
