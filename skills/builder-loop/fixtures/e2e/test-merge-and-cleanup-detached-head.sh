#!/usr/bin/env bash
# test-merge-and-cleanup-detached-head.sh — detached HEAD 防御
#
# 验证：
#   Case A: 主仓 detached HEAD → ERROR main-repo-detached-head (exit 3)，worktree/state 不被清理
#   Case B: 正常分支 ff merge → MERGED (exit 0)，新防御不破坏正常路径

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
  local dir="$1" slug="$2"
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
  git commit -q -m "chore(test): [cr_id_skip] Detached HEAD fixture seed"

  local wt="$dir/.claude/worktrees/${slug}"
  git -C "$dir" worktree add -q -b "loop/${slug}" "$wt"
  local head1
  head1="$(git -C "$dir" rev-parse --short HEAD)"

  echo "feature" > "$wt/feature.txt"
  git -C "$wt" add -A >/dev/null
  git -C "$wt" commit -q -m "chore(loop): [cr_id_skip] Auto-commit feature"

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
last_iter_head: "$(git -C "$wt" rev-parse --short HEAD)"
worktree_path: "${wt}"
worktree_mode: "clean"
plan_file: ""
task_description: |
  detached-head-test
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

echo "=== merge-and-cleanup.sh detached HEAD 防御 fixture ==="

# ============================================================
# Case A: 主仓 detached HEAD → ERROR main-repo-detached-head
# ============================================================
echo ""
echo "=== Case A: 主仓 detached HEAD → 拦截 ==="
TMPA="$(mktemp -d)"
trap 'rm -rf "${TMPA:-}" "${TMPB:-}"' EXIT
SLUG_A="detach-fixture-a"
setup_repo_with_worktree "$TMPA" "$SLUG_A"
STATE_A="$TMPA/.claude/builder-loop/state/${SLUG_A}.yml"

# detach 主仓 HEAD（checkout 到当前 commit hash）
DETACH_HASH="$(git -C "$TMPA" rev-parse HEAD)"
git -C "$TMPA" checkout "$DETACH_HASH" -q 2>/dev/null

MAIN_BR="$(git -C "$TMPA" rev-parse --abbrev-ref HEAD 2>/dev/null)"
assert "Case A 主仓确认 detached（abbrev-ref = HEAD）" "[ '$MAIN_BR' = 'HEAD' ]"

ERR_A="$(mktemp)"
OUT_A="$(bash "$MERGE_CLEANUP" "$STATE_A" 2>"$ERR_A")"
EC_A=$?

LAST_A="$(echo "$OUT_A" | tail -1)"

assert "Case A stdout 含 ERROR main-repo-detached-head" "echo '$LAST_A' | grep -q 'main-repo-detached-head'"
assert "Case A stderr 含 FATAL 提示" "grep -q 'FATAL.*detached HEAD' '$ERR_A'"
assert "Case A exit code = 3" "[ '$EC_A' -eq 3 ]"
assert "Case A worktree 未被删（阶段 1 拦截不清理）" "[ -d '$TMPA/.claude/worktrees/$SLUG_A' ]"
assert "Case A state 未被删" "[ -f '$STATE_A' ]"

# ============================================================
# Case B: 正常分支 ff merge → MERGED（sanity check）
# ============================================================
echo ""
echo "=== Case B: 正常分支 ff merge → MERGED ==="
TMPB="$(mktemp -d)"
SLUG_B="detach-fixture-b"
setup_repo_with_worktree "$TMPB" "$SLUG_B"
STATE_B="$TMPB/.claude/builder-loop/state/${SLUG_B}.yml"

MAIN_HEAD_BEFORE="$(git -C "$TMPB" rev-parse HEAD)"

ERR_B="$(mktemp)"
OUT_B="$(bash "$MERGE_CLEANUP" "$STATE_B" 2>"$ERR_B")"
EC_B=$?

LAST_B="$(echo "$OUT_B" | tail -1)"
ACTION_B="$(echo "$LAST_B" | awk '{print $1}')"

assert "Case B 输出 MERGED" "[ '$ACTION_B' = 'MERGED' ]"
assert "Case B exit code = 0" "[ '$EC_B' -eq 0 ]"
assert "Case B 主仓 HEAD 已推进" "[ \"$MAIN_HEAD_BEFORE\" != \"$(git -C \"$TMPB\" rev-parse HEAD)\" ]"
assert "Case B state 已删" "[ ! -f '$STATE_B' ]"
assert "Case B worktree 已删" "[ ! -d '$TMPB/.claude/worktrees/$SLUG_B' ]"

echo ""
echo "=== 总计 ==="
echo "  ✅ PASS: $PASS"
echo "  ❌ FAIL: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
