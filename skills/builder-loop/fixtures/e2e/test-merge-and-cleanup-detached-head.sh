#!/usr/bin/env bash
# test-merge-and-cleanup-detached-head.sh — detached HEAD 防御
#
# 验证：
#   Case A: 主仓 detached HEAD → ERROR main-repo-detached-head (exit 3)，worktree/state 不被清理
#   Case B: 正常分支 ff merge → MERGED (exit 0)，新防御不破坏正常路径

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "merge-and-cleanup-detached-head"

# Helper: 建 worktree + commit 一个变化 + 写 state（phase=passed_pending_review）
setup_detached_env() {
  local slug="$1"
  local env
  env=$(create_test_env --slug "$slug" --worktree --phase "passed_pending_review" --no-state)
  local wt="$env/.claude/worktrees/$slug"
  local head1
  head1=$(git -C "$env" rev-parse --short HEAD)

  echo "feature" > "$wt/feature.txt"
  git -C "$wt" add -A >/dev/null
  git -C "$wt" -c core.hooksPath=/dev/null commit -q -m "chore(loop): [cr_id_skip] Auto-commit feature"

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
last_iter_head: "$(git -C "$wt" rev-parse --short HEAD)"
worktree_path: "$wt"
worktree_mode: "clean"
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
STEOF
  echo "$env"
}

# ============================================================
# Case A: 主仓 detached HEAD → ERROR main-repo-detached-head
# ============================================================
section "Case A: 主仓 detached HEAD → 拦截"
envA=$(setup_detached_env "detach-fixture-a")
sfA=$(state_file "$envA" "detach-fixture-a")

# detach 主仓 HEAD
detachHash=$(git -C "$envA" rev-parse HEAD)
git -C "$envA" checkout "$detachHash" -q 2>/dev/null

mainBr=$(git -C "$envA" rev-parse --abbrev-ref HEAD 2>/dev/null)
assert "Case A 主仓确认 detached（abbrev-ref = HEAD）" "[ '$mainBr' = 'HEAD' ]"

resultA=$(run_merge_cleanup "$sfA")
assert_stdout_contains "Case A stdout 含 ERROR main-repo-detached-head" "$resultA" "main-repo-detached-head"
assert_stderr_contains "Case A stderr 含 FATAL 提示" "$resultA" "FATAL.*detached HEAD"
assert_ec "Case A exit code = 3" "$resultA" 3
assert_file_exists "Case A worktree 未被删" "$envA/.claude/worktrees/detach-fixture-a"
assert_file_exists "Case A state 未被删" "$sfA"

# ============================================================
# Case B: 正常分支 ff merge → MERGED（sanity check）
# ============================================================
section "Case B: 正常分支 ff merge → MERGED"
envB=$(setup_detached_env "detach-fixture-b")
sfB=$(state_file "$envB" "detach-fixture-b")
mainHeadBefore=$(git -C "$envB" rev-parse HEAD)

resultB=$(run_merge_cleanup "$sfB")
actionB=$(result_last "$resultB" | awk '{print $1}')

assert "Case B 输出 MERGED" "[ '$actionB' = 'MERGED' ]"
assert_ec "Case B exit code = 0" "$resultB" 0
mainHeadAfterB=$(git -C "$envB" rev-parse HEAD)
assert "Case B 主仓 HEAD 已推进" "[ '$mainHeadBefore' != '$mainHeadAfterB' ]"
assert_file_missing "Case B state 已删" "$sfB"
assert_file_missing "Case B worktree 已删" "$envB/.claude/worktrees/detach-fixture-b"

harness_report
