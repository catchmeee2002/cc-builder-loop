#!/usr/bin/env bash
# test-conflict.sh — 验证 merge-worktree-back.sh 冲突检测 + 仲裁标记 + 修复后合回
#
# 场景：
#   1. 创建临时 git 仓 + worktree 分支
#   2. 在 worktree 和主干分别改同一文件同一行（制造冲突）
#   3. merge-worktree-back.sh → NEED_ARBITRATION (exit 1)
#   4. 验证 state 写入 need_arbitration + conflict_files
#   5. mock arbiter 修复冲突
#   6. 再次 merge → MERGED (exit 0)
#
# 用法：bash test-conflict.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MERGE_SCRIPT="${SCRIPT_DIR}/../../scripts/merge-worktree-back.sh"

[ -f "$MERGE_SCRIPT" ] || { echo "❌ FAIL: merge-worktree-back.sh not found" >&2; exit 1; }

TMPDIR="$(mktemp -d -t builder-loop-conflict-XXXXXX)"
trap 'rm -rf "$TMPDIR"' EXIT

# === 构建临时仓 ===
cd "$TMPDIR"
git init -q
git -c core.hooksPath=/dev/null commit -q --allow-empty -m "root"
echo "line1-original" > shared.txt
git add shared.txt
git -c core.hooksPath=/dev/null commit -q -m "add shared.txt"
MAIN_HEAD="$(git rev-parse --short HEAD)"

# 创建 worktree 分支
mkdir -p .claude/worktrees
git worktree add -q .claude/worktrees/test-wt -b loop/test-wt

# === 在 worktree 改 shared.txt ===
echo "worktree-change" > .claude/worktrees/test-wt/shared.txt
git -C .claude/worktrees/test-wt add shared.txt
git -C .claude/worktrees/test-wt -c core.hooksPath=/dev/null commit -q -m "worktree edit"

# === 在主干也改 shared.txt（制造冲突）===
echo "main-change" > shared.txt
git add shared.txt
git -c core.hooksPath=/dev/null commit -q -m "main edit"

# === 写 state file ===
mkdir -p "$TMPDIR/.claude/builder-loop/state"
STATE="$TMPDIR/.claude/builder-loop/state/test-wt.yml"
cat > "$STATE" <<STEOF
active: true
slug: "test-wt"
iter: 1
max_iter: 3
project_root: "${TMPDIR}"
start_head: "${MAIN_HEAD}"
worktree_path: "${TMPDIR}/.claude/worktrees/test-wt"
plan_file: ""
task_description: "conflict test"
source_dirs: ""
test_dirs: ""
last_error_hash: ""
last_error_count: 0
stopped_reason: ""
STEOF

# === 阶段 1：merge → 期望 NEED_ARBITRATION ===
echo "--- 阶段 1：merge 期望 NEED_ARBITRATION ---"
EC=0
MERGE_OUT="$(bash "$MERGE_SCRIPT" "$STATE" 2>/dev/null)" || EC=$?
if [ "$EC" -ne 1 ]; then
  echo "❌ FAIL: 期望 exit 1（NEED_ARBITRATION），实际 exit $EC" >&2
  echo "输出: $MERGE_OUT" >&2
  exit 1
fi
if ! echo "$MERGE_OUT" | grep -q "NEED_ARBITRATION"; then
  echo "❌ FAIL: 输出不含 NEED_ARBITRATION" >&2
  exit 1
fi
echo "✓ 阶段 1: merge-worktree-back.sh 正确返回 NEED_ARBITRATION"

# 验证 state 写入冲突标记
if ! grep -q "need_arbitration: true" "$STATE"; then
  echo "❌ FAIL: state 缺少 need_arbitration: true" >&2
  exit 1
fi
if ! grep -q "conflict_files:" "$STATE"; then
  echo "❌ FAIL: state 缺少 conflict_files" >&2
  exit 1
fi
echo "✓ 阶段 1.5: state 正确写入 need_arbitration + conflict_files"

# 验证 their_commits 写入
if ! grep -q "their_commits:" "$STATE"; then
  echo "❌ FAIL: state 缺少 their_commits" >&2
  exit 1
fi
# 验证 JSON 含 "main edit" commit message
THEIR_JSON="$(grep -E '^their_commits:' "$STATE" | sed -E "s/^their_commits:[[:space:]]*//" | sed -E "s/^'//;s/'[[:space:]]*$//")"
if ! echo "$THEIR_JSON" | python3 -c "import sys,json; commits=json.load(sys.stdin); assert any('main edit' in c.get('message','') for c in commits), 'no main edit commit'" 2>/dev/null; then
  echo "❌ FAIL: their_commits JSON 不含 'main edit' commit" >&2
  echo "  JSON: $THEIR_JSON" >&2
  exit 1
fi
echo "✓ 阶段 1.6: state 正确写入 their_commits（含对方 commit 摘要）"

# === 阶段 2：mock arbiter 修复冲突（真 rebase + 解冲突，模拟 arbiter 行为）===
echo "--- 阶段 2：mock arbiter 修复 ---"
MAIN_BRANCH_NAME="$(git rev-parse --abbrev-ref HEAD)"
# 在 worktree 启动 rebase（会冲突）
git -C .claude/worktrees/test-wt rebase "$MAIN_BRANCH_NAME" 2>/dev/null || true
# 解冲突：写入合并后的内容
echo "resolved-content" > .claude/worktrees/test-wt/shared.txt
git -C .claude/worktrees/test-wt add shared.txt
# 继续 rebase
git -C .claude/worktrees/test-wt -c core.hooksPath=/dev/null rebase --continue 2>/dev/null || true

# 重置 state：去掉仲裁标记（不改 start_head，让 merge 走 ff 路径）
sed -i '/^need_arbitration:/d' "$STATE"
sed -i '/^conflict_files:/d' "$STATE"

# === 阶段 3：再次 merge → 期望 MERGED ===
echo "--- 阶段 3：merge 期望 MERGED ---"
EC=0
MERGE_OUT="$(bash "$MERGE_SCRIPT" "$STATE" 2>/dev/null)" || EC=$?
if [ "$EC" -ne 0 ]; then
  echo "❌ FAIL: 第二次 merge 期望 exit 0，实际 exit $EC" >&2
  echo "输出: $MERGE_OUT" >&2
  exit 1
fi
if ! echo "$MERGE_OUT" | grep -q "MERGED"; then
  echo "❌ FAIL: 输出不含 MERGED" >&2
  exit 1
fi
echo "✓ 阶段 3: merge-worktree-back.sh 正确返回 MERGED"

echo ""
echo "✅ PASS: rebase 冲突 → 仲裁标记 → mock 修复 → 合回主干 全流程通过"
exit 0
