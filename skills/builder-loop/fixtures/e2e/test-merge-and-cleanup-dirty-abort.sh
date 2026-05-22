#!/usr/bin/env bash
# test-merge-and-cleanup-dirty-abort.sh — worktree dirty 时 merge-and-cleanup 必须 abort
#
# C1: worktree 有未提交改动 → ERROR + exit 3 + 现场保留
# C2: worktree 干净（已 commit）→ MERGED + exit 0

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "merge-and-cleanup dirty abort"

section "C1: worktree dirty → abort"
env1=$(create_test_env --slug "dirty-c1" --worktree --phase "passed_pending_review" --wt-dirty "feature.txt")
sf1=$(state_file "$env1" "dirty-c1")
r1=$(run_merge_cleanup "$sf1")
assert_ec "C1 exit 3" "$r1" 3
assert_stdout_contains "C1 报 uncommitted-changes" "$r1" "worktree-has-uncommitted-changes"
assert_stderr_contains "C1 stderr 有 FATAL" "$r1" "FATAL"
assert_file_exists "C1 worktree 保留" "$env1/.claude/worktrees/dirty-c1"
assert_file_exists "C1 state 保留" "$sf1"

section "C2: worktree clean → MERGED"
env2=$(create_test_env --slug "dirty-c2" --worktree --phase "passed_pending_review")
wt2="$env2/.claude/worktrees/dirty-c2"
echo "feature" > "$wt2/feature.txt"
git -C "$wt2" add -A >/dev/null
git -C "$wt2" commit -q -m "chore(loop): [cr_id_skip] Auto-commit feature"
sf2=$(state_file "$env2" "dirty-c2")
r2=$(run_merge_cleanup "$sf2")
assert_ec "C2 exit 0" "$r2" 0
assert_stdout_contains "C2 输出 MERGED" "$r2" "MERGED"

harness_report
