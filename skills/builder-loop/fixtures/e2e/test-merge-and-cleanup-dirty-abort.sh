#!/usr/bin/env bash
# test-merge-and-cleanup-dirty-abort.sh — worktree dirty 时 merge-and-cleanup 必须 abort
#
# 验证：
#   Case A: worktree 有未提交改动 → ERROR worktree-has-uncommitted-changes (exit 3) + 现场保留
#   Case B: worktree 干净（已 commit）→ MERGED (exit 0)

set -uo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$THIS_DIR/../../../.." && pwd)"
MERGE_CLEANUP="${REPO_ROOT}/skills/builder-loop/scripts/merge-and-cleanup.sh"

PASS=0
FAIL=0
assert() {
  local desc="$1" cond="$2"
  if eval "$cond"; then echo "  ✅ $desc"; PASS=$(( PASS + 1 ))
  else echo "  ❌ $desc (cond: $cond)"; FAIL=$(( FAIL + 1 )); fi
}

setup_repo_with_worktree() {
  local dir="$1" slug="$2" commit_wt="$3"
  cd "$dir"
  git init -q
  git config user.email "e2e@test.local"
  git config user.name "e2e-test"
  echo "seed" > README.md
  cat > .gitignore <<'G'
.claude/builder-loop/
.claude/loop-runs/
G
  git add -A
  git commit -q -m "chore(test): [cr_id_skip] Dirty abort fixture seed"

  local wt="$dir/.claude/worktrees/${slug}"
  git -C "$dir" worktree add -q -b "loop/${slug}" "$wt"
  local head1
  head1="$(git -C "$dir" rev-parse --short HEAD)"

  echo "feature" > "$wt/feature.txt"

  if [ "$commit_wt" = "yes" ]; then
    git -C "$wt" add -A >/dev/null
    git -C "$wt" commit -q -m "chore(loop): [cr_id_skip] Auto-commit feature"
  fi

  mkdir -p "$dir/.claude/builder-loop/state"
  cat > "$dir/.claude/builder-loop/state/${slug}.yml" <<EOF
active: true
phase: "passed_pending_review"
slug: "${slug}"
owner_cwd: "$dir"
iter: 1
max_iter: 5
project_root: "${wt}"
main_repo_path: "$dir"
start_head: "${head1}"
last_iter_head: "$(git -C "$wt" rev-parse --short HEAD 2>/dev/null || echo "$head1")"
worktree_path: "${wt}"
worktree_mode: "clean"
plan_file: ""
task_description: |
  dirty-abort-test
source_dirs: ""
test_dirs: ""
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
stopped_reason: ""
cleanup_phase: ""
created_at: "2026-05-23T00:00:00+08:00"
EOF
}

echo "=== merge-and-cleanup.sh worktree dirty abort fixture ==="

# ============================================================
# Case A: worktree 有未提交改动 → abort
# ============================================================
echo ""
echo "=== Case A: worktree dirty → abort ==="
TMPA="$(mktemp -d)"
trap 'rm -rf "${TMPA:-}" "${TMPB:-}"' EXIT
SLUG_A="dirty-abort-a"
setup_repo_with_worktree "$TMPA" "$SLUG_A" "no"
STATE_A="$TMPA/.claude/builder-loop/state/${SLUG_A}.yml"
WT_A="$TMPA/.claude/worktrees/${SLUG_A}"

WT_STATUS="$(git -C "$WT_A" status --porcelain 2>/dev/null)"
assert "Case A worktree 确认有 dirty 文件" "[ -n '$WT_STATUS' ]"

ERR_A="$(mktemp)"
OUT_A="$(bash "$MERGE_CLEANUP" "$STATE_A" 2>"$ERR_A")"
EC_A=$?

LAST_A="$(echo "$OUT_A" | tail -1)"

assert "Case A stdout 含 worktree-has-uncommitted-changes" "echo '$LAST_A' | grep -q 'worktree-has-uncommitted-changes'"
assert "Case A stderr 含 FATAL 提示" "grep -q 'FATAL.*未提交改动' '$ERR_A'"
assert "Case A exit code = 3" "[ '$EC_A' -eq 3 ]"
assert "Case A worktree 保留" "[ -d '$WT_A' ]"
assert "Case A state 保留" "[ -f '$STATE_A' ]"
assert "Case A dirty 文件仍在" "[ -f '$WT_A/feature.txt' ]"

# ============================================================
# Case B: worktree 干净 → MERGED
# ============================================================
echo ""
echo "=== Case B: worktree clean → MERGED ==="
TMPB="$(mktemp -d)"
SLUG_B="dirty-abort-b"
setup_repo_with_worktree "$TMPB" "$SLUG_B" "yes"
STATE_B="$TMPB/.claude/builder-loop/state/${SLUG_B}.yml"

ERR_B="$(mktemp)"
OUT_B="$(bash "$MERGE_CLEANUP" "$STATE_B" 2>"$ERR_B")"
EC_B=$?

LAST_B="$(echo "$OUT_B" | tail -1)"
ACTION_B="$(echo "$LAST_B" | awk '{print $1}')"

assert "Case B 输出 MERGED" "[ '$ACTION_B' = 'MERGED' ]"
assert "Case B exit code = 0" "[ '$EC_B' -eq 0 ]"

echo ""
echo "=== 总计 ==="
echo "  ✅ PASS: $PASS"
echo "  ❌ FAIL: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
