#!/usr/bin/env bash
# test-multi-worktree-feedback.sh — V3.0 同 session 串行多 worktree 反馈不丢失
#
# 模拟：同一 cwd 序列连续跑两个 worktree loop
#   1. setup worktree-A → PASS → reviewer-diff-A.txt + state-A.reviewer_pending
#   2. merge-and-cleanup A
#   3. setup worktree-B → PASS → reviewer-diff-B.txt + state-B.reviewer_pending
#
# 验证：第二轮 reviewer_pending 不丢消息、diff 文件按 slug 拆不撞

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "multi-worktree-feedback"

# ---- 初始化主仓（不用 create_test_env：需两轮手动 worktree 操作）----
TMP="$(mktemp -d)"
_HARNESS_TMPDIRS+=("$TMP")
(
  cd "$TMP"
  git init -q
  git config user.email "e2e@test.local"
  git config user.name "e2e-test"
  mkdir -p .claude
  cat > .claude/loop.yml <<'Y'
pass_cmd:
  - stage: smoke
    cmd: "true"
    timeout: 10
worktree:
  enabled: false
Y
  echo "seed" > README.md
  cat > .gitignore <<'G'
.claude/builder-loop/
.claude/loop-runs/
.claude/reviewer-*.txt
.claude/review_reports/
G
  git add -A
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Multi-worktree fixture seed"
)
HEAD1="$(git -C "$TMP" rev-parse --short HEAD)"
mkdir -p "$TMP/.claude/builder-loop/state"

write_mw_state() {
  local dir="$1" slug="$2" head="$3" wt="$4"
  cat > "$dir/.claude/builder-loop/state/${slug}.yml" <<EOF
active: true
phase: "active"
slug: "${slug}"
owner_cwd: "$dir"
iter: 0
max_iter: 5
project_root: "${wt}"
main_repo_path: "$dir"
start_head: "${head}"
last_iter_head: "${head}"
worktree_path: "${wt}"
worktree_mode: "clean"
plan_file: ""
task_description: |
  multi-wt-test-${slug}
source_dirs: ""
test_dirs: ""
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
stopped_reason: ""
cleanup_phase: ""
created_at: "2026-05-09T00:00:00+08:00"
EOF
}

# ============================================================
# 第一轮：worktree-A
# ============================================================
section "第一轮：worktree-A"
SLUG_A="mw-fixture-a"
WT_A="$TMP/.claude/worktrees/${SLUG_A}"
git -C "$TMP" worktree add -q "$WT_A"
echo "feature-a" > "$WT_A/feature.txt"
write_mw_state "$TMP" "$SLUG_A" "$HEAD1" "$WT_A"
STATE_A="$TMP/.claude/builder-loop/state/${SLUG_A}.yml"

r_a=$(run_hook "$WT_A")
assert_ec "第一轮 hook EC=2" "$r_a" 2
assert "第一轮 state-A 含 reviewer_pending 段" "grep -q '^reviewer_pending:' '$STATE_A'"
assert_file_exists "第一轮 reviewer-diff-A.txt 存在" "$TMP/.claude/reviewer-diff-${SLUG_A}.txt"
DIFF_A_IN_STATE="$(grep -E '^[[:space:]]+diff_file:' "$STATE_A" | head -1)"
assert "第一轮 diff_file 含 slug-A" "echo '$DIFF_A_IN_STATE' | grep -q '${SLUG_A}'"

# merge-and-cleanup A
r_mc=$(run_merge_cleanup "$STATE_A")
assert_file_missing "第一轮 merge 后 state-A 已删" "$STATE_A"
assert_file_missing "第一轮 worktree-A 已删" "$WT_A"

# ============================================================
# 第二轮：worktree-B
# ============================================================
section "第二轮：worktree-B"
HEAD2="$(git -C "$TMP" rev-parse --short HEAD)"
SLUG_B="mw-fixture-b"
WT_B="$TMP/.claude/worktrees/${SLUG_B}"
git -C "$TMP" worktree add -q "$WT_B"
echo "feature-b" > "$WT_B/feature.txt"
write_mw_state "$TMP" "$SLUG_B" "$HEAD2" "$WT_B"
STATE_B="$TMP/.claude/builder-loop/state/${SLUG_B}.yml"

r_b=$(run_hook "$WT_B")
assert_ec "第二轮 hook EC=2" "$r_b" 2
assert "第二轮 state-B 含 reviewer_pending（反馈不丢）" "grep -q '^reviewer_pending:' '$STATE_B'"
assert_file_exists "第二轮 reviewer-diff-B.txt 存在" "$TMP/.claude/reviewer-diff-${SLUG_B}.txt"
DIFF_B_IN_STATE="$(grep -E '^[[:space:]]+diff_file:' "$STATE_B" | head -1)"
assert "第二轮 diff_file 含 slug-B" "echo '$DIFF_B_IN_STATE' | grep -q '${SLUG_B}'"
assert "第二轮 diff_file 不含 slug-A" "! echo '$DIFF_B_IN_STATE' | grep -q '${SLUG_A}'"

harness_report
