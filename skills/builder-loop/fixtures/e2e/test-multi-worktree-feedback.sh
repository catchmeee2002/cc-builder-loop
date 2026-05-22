#!/usr/bin/env bash
# test-multi-worktree-feedback.sh — V3.0 同 session 串行多 worktree 反馈不丢失
#
# 模拟：同一 cwd 序列连续跑两个 worktree loop
#   1. setup worktree-A → PASS → reviewer-diff-A.txt + state-A.reviewer_pending
#   2. merge-and-cleanup A
#   3. setup worktree-B → PASS → reviewer-diff-B.txt + state-B.reviewer_pending
#
# 验证：第二轮 reviewer_pending 不丢消息、diff 文件按 slug 拆不撞

set -uo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$THIS_DIR/../../../.." && pwd)"
HOOK_SCRIPT="${REPO_ROOT}/scripts/builder-loop-stop.sh"
MERGE_CLEANUP="${REPO_ROOT}/skills/builder-loop/scripts/merge-and-cleanup.sh"

PASS=0
FAIL=0
assert() {
  local desc="$1" cond="$2"
  if eval "$cond"; then echo "  ✅ $desc"; PASS=$(( PASS + 1 ))
  else echo "  ❌ $desc (cond: $cond)"; FAIL=$(( FAIL + 1 )); fi
}

run_hook() {
  local cwd="$1" err_file="$2" ec=0
  printf '{"cwd": "%s"}' "$cwd" | bash "$HOOK_SCRIPT" 2>"$err_file" >/dev/null || ec=$?
  echo "$ec"
}

write_state() {
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

echo "=== V3.0 同 session 多 worktree 反馈不丢失 fixture ==="
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP:-}"' EXIT

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
git commit -q -m "chore(test): [cr_id_skip] Multi-worktree fixture seed"
HEAD1="$(git -C "$TMP" rev-parse --short HEAD)"

mkdir -p "$TMP/.claude/builder-loop/state"

# ============================================================
# 第一轮：worktree-A
# ============================================================
echo ""
echo "=== 第一轮：worktree-A ==="
SLUG_A="mw-fixture-a"
WT_A="$TMP/.claude/worktrees/${SLUG_A}"
git -C "$TMP" worktree add -q "$WT_A"
echo "feature-a" > "$WT_A/feature.txt"
write_state "$TMP" "$SLUG_A" "$HEAD1" "$WT_A"
STATE_A="$TMP/.claude/builder-loop/state/${SLUG_A}.yml"

# V3.2: local.md 指向 slug-A
cat > "$TMP/.claude/builder-loop.local.md" <<LOCALEOF
slug: "${SLUG_A}"
worktree_path: "${WT_A}"
state_file: "$TMP/.claude/builder-loop/state/${SLUG_A}.yml"
LOCALEOF

ERR_A="$(mktemp)"
EC_A="$(run_hook "$WT_A" "$ERR_A")"

assert "第一轮 hook EC=2" "[ '$EC_A' -eq 2 ]"
assert "第一轮 state-A 含 reviewer_pending 段" "grep -q '^reviewer_pending:' '$STATE_A'"
assert "第一轮 reviewer-diff-A.txt 存在" "[ -f '$TMP/.claude/reviewer-diff-${SLUG_A}.txt' ]"
DIFF_A_IN_STATE="$(grep -E '^[[:space:]]+diff_file:' "$STATE_A" | head -1)"
assert "第一轮 state.reviewer_pending.diff_file 含 slug-A" \
  "echo '$DIFF_A_IN_STATE' | grep -q '${SLUG_A}'"

# 模拟 reviewer 通过 → merge-and-cleanup
bash "$MERGE_CLEANUP" "$STATE_A" 2>/dev/null >/dev/null

assert "第一轮 merge-and-cleanup 后 state-A 已删" "[ ! -f '$STATE_A' ]"
assert "第一轮 worktree-A 已删" "[ ! -d '$WT_A' ]"

# ============================================================
# 第二轮：同 session（同 cwd）setup worktree-B
# ============================================================
echo ""
echo "=== 第二轮：worktree-B ==="
HEAD2="$(git -C "$TMP" rev-parse --short HEAD)"  # 第一轮 merge 后主仓 HEAD 已推进
SLUG_B="mw-fixture-b"
WT_B="$TMP/.claude/worktrees/${SLUG_B}"
git -C "$TMP" worktree add -q "$WT_B"
echo "feature-b" > "$WT_B/feature.txt"
write_state "$TMP" "$SLUG_B" "$HEAD2" "$WT_B"
STATE_B="$TMP/.claude/builder-loop/state/${SLUG_B}.yml"

# V3.2: 切 local.md 到 slug-B
cat > "$TMP/.claude/builder-loop.local.md" <<LOCALEOF
slug: "${SLUG_B}"
worktree_path: "${WT_B}"
state_file: "$TMP/.claude/builder-loop/state/${SLUG_B}.yml"
LOCALEOF

ERR_B="$(mktemp)"
EC_B="$(run_hook "$WT_B" "$ERR_B")"

assert "第二轮 hook EC=2" "[ '$EC_B' -eq 2 ]"
assert "第二轮 state-B 含 reviewer_pending 段（反馈不丢）" \
  "grep -q '^reviewer_pending:' '$STATE_B'"
assert "第二轮 reviewer-diff-B.txt 存在" "[ -f '$TMP/.claude/reviewer-diff-${SLUG_B}.txt' ]"
DIFF_B_IN_STATE="$(grep -E '^[[:space:]]+diff_file:' "$STATE_B" | head -1)"
assert "第二轮 state.reviewer_pending.diff_file 含 slug-B（不是 slug-A 残留）" \
  "echo '$DIFF_B_IN_STATE' | grep -q '${SLUG_B}'"
assert "第二轮 state.reviewer_pending.diff_file 不含 slug-A（按 slug 拆生效）" \
  "! echo '$DIFF_B_IN_STATE' | grep -q '${SLUG_A}'"

echo ""
echo "=== 总计 ==="
echo "  ✅ PASS: $PASS"
echo "  ❌ FAIL: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
