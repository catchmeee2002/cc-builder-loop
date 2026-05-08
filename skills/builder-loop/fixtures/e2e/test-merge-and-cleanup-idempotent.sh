#!/usr/bin/env bash
# test-merge-and-cleanup-idempotent.sh — V3.0 merge-and-cleanup.sh cleanup_phase 幂等
#
# 验证：
#   Case A: cleanup_phase="ff_merged" 重跑 → 跳过 ff merge，直接 worktree remove + rm state
#   Case B: cleanup_phase="worktree_removed" 重跑 → 仅 rm state

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

setup_post_pass_state() {
  # 准备一个已 ff merged 状态：worktree 内 commit 已 ff 到主仓，cleanup_phase 设到指定阶段
  local dir="$1" slug="$2" phase="$3"
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
  git commit -q -m "chore(test): [cr_id_skip] Idempotent fixture seed"

  WT="$dir/.claude/worktrees/${slug}"
  git -C "$dir" worktree add -q -b "loop/${slug}" "$WT"
  HEAD1="$(git -C "$dir" rev-parse --short HEAD)"

  # worktree 内 commit 一个变化（模拟 PASS 后 worktree 已 commit）
  echo "feature" > "$WT/feature.txt"
  git -C "$WT" add -A >/dev/null
  git -C "$WT" commit -q -m "chore(loop): [cr_id_skip] Auto-commit feature"
  WT_HEAD="$(git -C "$WT" rev-parse --short HEAD)"

  # 模拟 ff merge 已经发生（主仓推进到 worktree HEAD）
  if [ "$phase" = "ff_merged" ] || [ "$phase" = "worktree_removed" ]; then
    git -C "$dir" merge --ff-only "loop/${slug}" -q 2>/dev/null
  fi
  # 模拟 worktree_removed：物理删 worktree
  if [ "$phase" = "worktree_removed" ]; then
    git -C "$dir" worktree remove --force "$WT" 2>/dev/null || rm -rf "$WT"
  fi

  mkdir -p "$dir/.claude/builder-loop/state"
  cat > "$dir/.claude/builder-loop/state/${slug}.yml" <<EOF
active: true
phase: "passed_pending_review"
slug: "${slug}"
owner_cwd: "$dir"
iter: 1
max_iter: 5
project_root: "${WT}"
main_repo_path: "$dir"
start_head: "${HEAD1}"
last_iter_head: "${WT_HEAD}"
worktree_path: "${WT}"
worktree_mode: "clean"
plan_file: ""
task_description: |
  idempotent-test
source_dirs: ""
test_dirs: ""
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
stopped_reason: ""
cleanup_phase: "${phase}"
created_at: "2026-05-09T00:00:00+08:00"
EOF
}

echo "=== V3.0 merge-and-cleanup.sh 幂等 fixture ==="

# ============================================================
# Case A: cleanup_phase=ff_merged → 跳过 ff merge，直接 worktree remove + rm state
# ============================================================
echo ""
echo "=== Case A: cleanup_phase=ff_merged 重跑 ==="
TMPA="$(mktemp -d)"
trap 'rm -rf "${TMPA:-}" "${TMPB:-}"' EXIT
SLUG_A="idem-fixture-a"
setup_post_pass_state "$TMPA" "$SLUG_A" "ff_merged"

STATE_A="$TMPA/.claude/builder-loop/state/${SLUG_A}.yml"
WT_A="$TMPA/.claude/worktrees/${SLUG_A}"

assert "Case A worktree 重跑前存在" "[ -d '$WT_A' ]"
MAIN_HEAD_A_BEFORE="$(git -C "$TMPA" rev-parse HEAD)"

OUT_A="$(bash "$MERGE_CLEANUP" "$STATE_A" 2>&1 || true)"
LAST_A="$(echo "$OUT_A" | tail -1)"
ACTION_A="$(echo "$LAST_A" | awk '{print $1}')"

assert "Case A 输出 MERGED" "[ '$ACTION_A' = 'MERGED' ]"
assert "Case A 主仓 HEAD 不再推进（ff merge 已跳过）" \
  "[ \"\$(git -C $TMPA rev-parse HEAD)\" = '$MAIN_HEAD_A_BEFORE' ]"
assert "Case A worktree 已删" "[ ! -d '$WT_A' ]"
assert "Case A state 已删" "[ ! -f '$STATE_A' ]"

# ============================================================
# Case B: cleanup_phase=worktree_removed → 仅 rm state
# ============================================================
echo ""
echo "=== Case B: cleanup_phase=worktree_removed 重跑 ==="
TMPB="$(mktemp -d)"
SLUG_B="idem-fixture-b"
setup_post_pass_state "$TMPB" "$SLUG_B" "worktree_removed"

STATE_B="$TMPB/.claude/builder-loop/state/${SLUG_B}.yml"
WT_B="$TMPB/.claude/worktrees/${SLUG_B}"

assert "Case B worktree 重跑前已不存在" "[ ! -d '$WT_B' ]"
MAIN_HEAD_B_BEFORE="$(git -C "$TMPB" rev-parse HEAD)"

OUT_B="$(bash "$MERGE_CLEANUP" "$STATE_B" 2>&1 || true)"
LAST_B="$(echo "$OUT_B" | tail -1)"
ACTION_B="$(echo "$LAST_B" | awk '{print $1}')"

assert "Case B 输出 MERGED" "[ '$ACTION_B' = 'MERGED' ]"
assert "Case B 主仓 HEAD 不再推进" \
  "[ \"\$(git -C $TMPB rev-parse HEAD)\" = '$MAIN_HEAD_B_BEFORE' ]"
assert "Case B state 已删" "[ ! -f '$STATE_B' ]"

echo ""
echo "=== 总计 ==="
echo "  ✅ PASS: $PASS"
echo "  ❌ FAIL: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
