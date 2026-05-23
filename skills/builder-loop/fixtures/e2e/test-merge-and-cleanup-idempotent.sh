#!/usr/bin/env bash
# test-merge-and-cleanup-idempotent.sh — V3.0 merge-and-cleanup.sh cleanup_phase 幂等
#
# 验证：
#   Case A: cleanup_phase="ff_merged" 重跑 → 跳过 ff merge，直接 worktree remove + rm state
#   Case B: cleanup_phase="worktree_removed" 重跑 → 仅 rm state

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "merge-and-cleanup-idempotent"

# Helper: 准备一个已 ff merged 状态的 env，cleanup_phase 设到指定阶段
setup_post_pass() {
  local slug="$1" cleanup_phase="$2"
  local env
  env=$(create_test_env --slug "$slug" --worktree --phase "passed_pending_review" --no-state)
  local wt="$env/.claude/worktrees/$slug"
  local head1
  head1=$(git -C "$env" rev-parse --short HEAD)

  # worktree 内 commit 一个变化
  echo "feature" > "$wt/feature.txt"
  git -C "$wt" add -A >/dev/null
  git -C "$wt" -c core.hooksPath=/dev/null commit -q -m "chore(loop): [cr_id_skip] Auto-commit feature"
  local wt_head
  wt_head=$(git -C "$wt" rev-parse --short HEAD)

  # 模拟 ff merge 已发生
  if [ "$cleanup_phase" = "ff_merged" ] || [ "$cleanup_phase" = "worktree_removed" ]; then
    git -C "$env" merge --ff-only "loop/$slug" -q 2>/dev/null
  fi
  # 模拟 worktree_removed
  if [ "$cleanup_phase" = "worktree_removed" ]; then
    git -C "$env" worktree remove --force "$wt" 2>/dev/null || rm -rf "$wt"
  fi

  mkdir -p "$env/.claude/builder-loop/state"
  cat > "$env/.claude/builder-loop/state/$slug.yml" <<STEOF
active: true
phase: "passed_pending_review"
slug: "$slug"
owner_cwd: "$env"
iter: 1
max_iter: 5
project_root: "$wt"
main_repo_path: "$env"
start_head: "$head1"
last_iter_head: "$wt_head"
worktree_path: "$wt"
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
cleanup_phase: "$cleanup_phase"
created_at: "2026-05-09T00:00:00+08:00"
STEOF
  echo "$env"
}

# ============================================================
# Case A: cleanup_phase=ff_merged → 跳过 ff merge，直接 worktree remove + rm state
# ============================================================
section "Case A: cleanup_phase=ff_merged 重跑"
envA=$(setup_post_pass "idem-fixture-a" "ff_merged")
sfA=$(state_file "$envA" "idem-fixture-a")
wtA="$envA/.claude/worktrees/idem-fixture-a"

assert_file_exists "Case A worktree 重跑前存在" "$wtA"
mainHeadA=$(git -C "$envA" rev-parse HEAD)

resultA=$(run_merge_cleanup "$sfA")
actionA=$(result_last "$resultA" | awk '{print $1}')

assert "Case A 输出 MERGED" "[ '$actionA' = 'MERGED' ]"
mainHeadAAfter=$(git -C "$envA" rev-parse HEAD)
assert "Case A 主仓 HEAD 不再推进（ff merge 已跳过）" "[ '$mainHeadAAfter' = '$mainHeadA' ]"
assert_file_missing "Case A worktree 已删" "$wtA"
assert_file_missing "Case A state 已删" "$sfA"

# ============================================================
# Case B: cleanup_phase=worktree_removed → 仅 rm state
# ============================================================
section "Case B: cleanup_phase=worktree_removed 重跑"
envB=$(setup_post_pass "idem-fixture-b" "worktree_removed")
sfB=$(state_file "$envB" "idem-fixture-b")
wtB="$envB/.claude/worktrees/idem-fixture-b"

assert_file_missing "Case B worktree 重跑前已不存在" "$wtB"
mainHeadB=$(git -C "$envB" rev-parse HEAD)

resultB=$(run_merge_cleanup "$sfB")
actionB=$(result_last "$resultB" | awk '{print $1}')

assert "Case B 输出 MERGED" "[ '$actionB' = 'MERGED' ]"
mainHeadBAfter=$(git -C "$envB" rev-parse HEAD)
assert "Case B 主仓 HEAD 不再推进" "[ '$mainHeadBAfter' = '$mainHeadB' ]"
assert_file_missing "Case B state 已删" "$sfB"

harness_report
