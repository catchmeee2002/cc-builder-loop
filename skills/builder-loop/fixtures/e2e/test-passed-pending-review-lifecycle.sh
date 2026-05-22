#!/usr/bin/env bash
# test-passed-pending-review-lifecycle.sh — V3.0 phase=passed_pending_review 全生命周期
#
# 验证：
#   Case A: PASS → state.phase=passed_pending_review + reviewer_pending 段 + worktree 保留
#   Case B: passed_pending_review + 模拟 reviewer 通过 → merge-and-cleanup → 主仓 HEAD 推进 + worktree 删 + state 删
#   Case C: passed_pending_review + builder 在 worktree 改文件（dirty） → hook L1 自愈回 phase=active

set -uo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$THIS_DIR/../../../.." && pwd)"
HOOK_SCRIPT="${REPO_ROOT}/scripts/builder-loop-stop.sh"
COMMIT_ONLY="${REPO_ROOT}/skills/builder-loop/scripts/worktree-commit-only.sh"
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

setup_repo() {
  local dir="$1" slug="$2"
  cd "$dir"
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
  git commit -q -m "chore(test): [cr_id_skip] Lifecycle fixture seed"

  WT="$dir/.claude/worktrees/${slug}"
  git -C "$dir" worktree add -q "$WT" 2>/dev/null
  HEAD1="$(git -C "$dir" rev-parse --short HEAD)"

  mkdir -p "$dir/.claude/builder-loop/state"
  cat > "$dir/.claude/builder-loop/state/${slug}.yml" <<EOF
active: true
phase: "active"
slug: "${slug}"
owner_cwd: "$dir"
iter: 0
max_iter: 5
project_root: "${WT}"
main_repo_path: "$dir"
start_head: "${HEAD1}"
last_iter_head: "${HEAD1}"
worktree_path: "${WT}"
worktree_mode: "clean"
plan_file: ""
task_description: |
  lifecycle-test
source_dirs: ""
test_dirs: ""
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
stopped_reason: ""
cleanup_phase: ""
created_at: "2026-05-09T00:00:00+08:00"
EOF
  # V3.2: 创建 local.md 让 stop hook 能通过 slug 精确定位 state
  cat > "$dir/.claude/builder-loop.local.md" <<LOCALEOF
slug: "${slug}"
worktree_path: "${WT}"
state_file: "$dir/.claude/builder-loop/state/${slug}.yml"
LOCALEOF

  # 让 worktree 内有 dirty 改动让 PASS_CMD 后 commit-only 能 commit 出 new HEAD
  echo "feature change" > "$WT/feature.txt"
}

echo "=== V3.0 phase=passed_pending_review 全生命周期 fixture ==="

# ============================================================
# Case A: PASS → phase=passed_pending_review
# ============================================================
echo ""
echo "=== Case A: PASS → phase=passed_pending_review ==="
TMPA="$(mktemp -d)"
trap 'rm -rf "${TMPA:-}" "${TMPB:-}" "${TMPC:-}"' EXIT
SLUG_A="lc-fixture-a"
setup_repo "$TMPA" "$SLUG_A"
STATE_A="$TMPA/.claude/builder-loop/state/${SLUG_A}.yml"
WT_A="$TMPA/.claude/worktrees/${SLUG_A}"

ERR_A="$(mktemp)"
EC_A="$(run_hook "$WT_A" "$ERR_A")"

assert "Case A hook EC=2（PASS 续接）" "[ '$EC_A' -eq 2 ]"
assert "Case A state 仍存在（不删 state）" "[ -f '$STATE_A' ]"
assert "Case A worktree 仍存在（不删 worktree）" "[ -d '$WT_A' ]"
PHASE_A="$(grep -E '^phase:' "$STATE_A" | head -1 | sed -E 's/^phase:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/')"
assert "Case A state.phase = passed_pending_review" "[ '$PHASE_A' = 'passed_pending_review' ]"
assert "Case A state 含 reviewer_pending 段" "grep -q '^reviewer_pending:' '$STATE_A'"
assert "Case A state 含 reviewer_pending.diff_file" "grep -q 'diff_file:' '$STATE_A'"

# ============================================================
# Case B: 模拟 reviewer 通过 → merge-and-cleanup
# ============================================================
echo ""
echo "=== Case B: reviewer 通过 → merge-and-cleanup ==="
TMPB="$(mktemp -d)"
SLUG_B="lc-fixture-b"
setup_repo "$TMPB" "$SLUG_B"
STATE_B="$TMPB/.claude/builder-loop/state/${SLUG_B}.yml"
WT_B="$TMPB/.claude/worktrees/${SLUG_B}"

# 先跑 hook 让 phase 变 passed_pending_review
ERR_B1="$(mktemp)"
run_hook "$WT_B" "$ERR_B1" >/dev/null

MAIN_HEAD_BEFORE_B="$(git -C "$TMPB" rev-parse HEAD)"

# 模拟 reviewer 通过 → builder 调 merge-and-cleanup
MERGE_OUT="$(bash "$MERGE_CLEANUP" "$STATE_B" 2>&1 || true)"
MERGE_LAST="$(echo "$MERGE_OUT" | tail -1)"
MERGE_ACTION="$(echo "$MERGE_LAST" | awk '{print $1}')"

assert "Case B merge-and-cleanup 输出 MERGED" "[ '$MERGE_ACTION' = 'MERGED' ]"
assert "Case B 主仓 HEAD 已推进" "[ \"$MAIN_HEAD_BEFORE_B\" != \"$(git -C \"$TMPB\" rev-parse HEAD)\" ]"
assert "Case B state 文件已删除" "[ ! -f '$STATE_B' ]"
assert "Case B worktree 目录已删除" "[ ! -d '$WT_B' ]"

# ============================================================
# Case C: passed_pending_review + worktree dirty → hook L1 自愈回 active
# ============================================================
echo ""
echo "=== Case C: passed_pending_review + worktree dirty → 自愈 ==="
TMPC="$(mktemp -d)"
SLUG_C="lc-fixture-c"
setup_repo "$TMPC" "$SLUG_C"
STATE_C="$TMPC/.claude/builder-loop/state/${SLUG_C}.yml"
WT_C="$TMPC/.claude/worktrees/${SLUG_C}"

# 先跑 hook 让 phase 变 passed_pending_review
ERR_C1="$(mktemp)"
run_hook "$WT_C" "$ERR_C1" >/dev/null

# 验证当前 phase = passed_pending_review
PHASE_C1="$(grep -E '^phase:' "$STATE_C" | head -1 | sed -E 's/^phase:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/')"
assert "Case C 第一轮后 state.phase = passed_pending_review" "[ '$PHASE_C1' = 'passed_pending_review' ]"

# builder 在 worktree 内改文件（模拟 reviewer 反馈修复）
echo "fix-from-reviewer" > "$WT_C/fix.txt"

# 再跑 hook → L1 应触发自愈
ERR_C2="$(mktemp)"
EC_C2="$(run_hook "$WT_C" "$ERR_C2")"

PHASE_C2="$(grep -E '^phase:' "$STATE_C" | head -1 | sed -E 's/^phase:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/')"
# 触发自愈后 hook 会再次跑 PASS_CMD → 又变成 passed_pending_review；但中间过程经过了 active
# 验证 stderr 含 phase 自愈日志
assert "Case C 第二轮 hook stderr 含 L1 phase 自愈" "grep -q 'L1 phase 自愈' '$ERR_C2'"
assert "Case C 第二轮 hook EC=2（自愈后跑 PASS_CMD 完成）" "[ '$EC_C2' -eq 2 ]"

echo ""
echo "=== 总计 ==="
echo "  ✅ PASS: $PASS"
echo "  ❌ FAIL: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
