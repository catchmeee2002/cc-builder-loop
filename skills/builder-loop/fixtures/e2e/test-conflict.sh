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

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "merge-worktree-back 冲突检测 + 仲裁 + 合回"

MERGE_SCRIPT="${HARNESS_REPO_ROOT}/skills/builder-loop/scripts/merge-worktree-back.sh"
assert "merge-worktree-back.sh 存在" "[ -f '$MERGE_SCRIPT' ]"

# === 构建临时仓 ===
TMPDIR="$(mktemp -d -t builder-loop-conflict-XXXXXX)"
_HARNESS_TMPDIRS+=("$TMPDIR")

(
  cd "$TMPDIR"
  git init -q
  git config user.email "harness@test.local"
  git config user.name "harness"
  git -c core.hooksPath=/dev/null commit -q --allow-empty -m "chore(test): [cr_id_skip] Root"
  echo "line1-original" > shared.txt
  git add shared.txt
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Add shared.txt"
)
MAIN_HEAD="$(git -C "$TMPDIR" rev-parse --short HEAD)"

# 创建 worktree 分支
mkdir -p "$TMPDIR/.claude/worktrees"
git -C "$TMPDIR" worktree add -q "$TMPDIR/.claude/worktrees/test-wt" -b loop/test-wt

# === 在 worktree 改 shared.txt ===
echo "worktree-change" > "$TMPDIR/.claude/worktrees/test-wt/shared.txt"
git -C "$TMPDIR/.claude/worktrees/test-wt" add shared.txt
git -C "$TMPDIR/.claude/worktrees/test-wt" -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Worktree edit"

# === 在主干也改 shared.txt（制造冲突）===
echo "main-change" > "$TMPDIR/shared.txt"
git -C "$TMPDIR" add shared.txt
git -C "$TMPDIR" -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Main edit"

# === 写 state file ===
mkdir -p "$TMPDIR/.claude/builder-loop/state"
STATE="$TMPDIR/.claude/builder-loop/state/test-wt.yml"
cat > "$STATE" <<STEOF
slug: "test-wt"
iter: 1
max_iter: 3
project_root: "${TMPDIR}"
start_head: "${MAIN_HEAD}"
worktree_path: "${TMPDIR}/.claude/worktrees/test-wt"
task_description: "conflict test"
source_dirs: ""
test_dirs: ""
last_error_hash: ""
last_error_count: 0
stopped_reason: ""
STEOF

# === 阶段 1：merge → 期望 NEED_ARBITRATION ===
section "阶段 1: merge 期望 NEED_ARBITRATION"
MERGE_RESULT="$(run_merge_cleanup "$STATE")"
assert_ec "merge 返回 exit 1 (NEED_ARBITRATION)" "$MERGE_RESULT" 1
assert_stdout_contains "输出含 NEED_ARBITRATION" "$MERGE_RESULT" "NEED_ARBITRATION"

# 验证 state 写入冲突标记
assert_contains "state 含 need_arbitration: true" "$STATE" "need_arbitration: true"
assert_contains "state 含 conflict_files" "$STATE" "conflict_files:"

# 验证 their_commits 写入
assert_contains "state 含 their_commits" "$STATE" "their_commits:"

# 验证 JSON 含 "main edit" commit message
THEIR_JSON="$(grep -E '^their_commits:' "$STATE" | sed -E "s/^their_commits:[[:space:]]*//" | sed -E "s/^'//;s/'[[:space:]]*$//")"
assert "their_commits JSON 含 Main edit commit" \
  "echo '$THEIR_JSON' | python3 -c \"import sys,json; commits=json.load(sys.stdin); assert any('Main edit' in c.get('message','') for c in commits)\" 2>/dev/null"

# === 阶段 2：mock arbiter 修复冲突 ===
section "阶段 2: mock arbiter 修复"
MAIN_BRANCH_NAME="$(git -C "$TMPDIR" rev-parse --abbrev-ref HEAD)"
# 在 worktree 启动 rebase（会冲突）
git -C "$TMPDIR/.claude/worktrees/test-wt" rebase "$MAIN_BRANCH_NAME" 2>/dev/null || true
# 解冲突：写入合并后的内容
echo "resolved-content" > "$TMPDIR/.claude/worktrees/test-wt/shared.txt"
git -C "$TMPDIR/.claude/worktrees/test-wt" add shared.txt
# 继续 rebase
git -C "$TMPDIR/.claude/worktrees/test-wt" -c core.hooksPath=/dev/null rebase --continue 2>/dev/null || true

# 重置 state：去掉仲裁标记
sed -i '/^need_arbitration:/d' "$STATE"
sed -i '/^conflict_files:/d' "$STATE"

# === 阶段 3：再次 merge → 期望 MERGED ===
section "阶段 3: merge 期望 MERGED"
MERGE_RESULT2="$(run_merge_cleanup "$STATE")"
assert_ec "第二次 merge 返回 exit 0" "$MERGE_RESULT2" 0
assert_stdout_contains "输出含 MERGED" "$MERGE_RESULT2" "MERGED"

harness_report
