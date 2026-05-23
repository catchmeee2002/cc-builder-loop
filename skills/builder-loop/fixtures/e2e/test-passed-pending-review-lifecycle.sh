#!/usr/bin/env bash
# test-passed-pending-review-lifecycle.sh — V3.0 phase=passed_pending_review 全生命周期
#
# 验证：
#   Case A: PASS → state.phase=passed_pending_review + reviewer_pending 段 + worktree 保留
#   Case B: passed_pending_review + 模拟 reviewer 通过 → merge-and-cleanup → 主仓 HEAD 推进 + worktree 删 + state 删
#   Case C: passed_pending_review + builder 在 worktree 改文件（dirty） → hook L1 自愈回 active

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "passed-pending-review-lifecycle"

# ============================================================
# Case A: PASS → phase=passed_pending_review
# ============================================================
section "Case A: PASS → phase=passed_pending_review"
envA=$(create_test_env --slug "lc-fixture-a" --worktree --phase active --wt-dirty "feature.txt")
sfA=$(state_file "$envA" "lc-fixture-a")
wtA="$envA/.claude/worktrees/lc-fixture-a"

resultA=$(run_hook "$wtA")
assert_ec "Case A hook EC=2（PASS 续接）" "$resultA" 2
assert_file_exists "Case A state 仍存在" "$sfA"
assert_file_exists "Case A worktree 仍存在" "$wtA"
phaseA=$(read_state_field "$sfA" "phase")
assert "Case A state.phase = passed_pending_review" "[ '$phaseA' = 'passed_pending_review' ]"
assert_contains "Case A state 含 reviewer_pending 段" "$sfA" "^reviewer_pending:"
assert_contains "Case A state 含 diff_file" "$sfA" "diff_file:"

# ============================================================
# Case B: reviewer 通过 → merge-and-cleanup
# ============================================================
section "Case B: reviewer 通过 → merge-and-cleanup"
envB=$(create_test_env --slug "lc-fixture-b" --worktree --phase active --wt-dirty "feature.txt")
sfB=$(state_file "$envB" "lc-fixture-b")
wtB="$envB/.claude/worktrees/lc-fixture-b"

# 先跑 hook 让 phase 变 passed_pending_review
run_hook "$wtB" >/dev/null

mainHeadBeforeB=$(git -C "$envB" rev-parse HEAD)

# 模拟 reviewer 通过 → builder 调 merge-and-cleanup
mergeResultB=$(run_merge_cleanup "$sfB")
mergeLast=$(result_last "$mergeResultB")
mergeAction=$(echo "$mergeLast" | awk '{print $1}')

assert "Case B merge-and-cleanup 输出 MERGED" "[ '$mergeAction' = 'MERGED' ]"
mainHeadAfterB=$(git -C "$envB" rev-parse HEAD)
assert "Case B 主仓 HEAD 已推进" "[ '$mainHeadBeforeB' != '$mainHeadAfterB' ]"
assert_file_missing "Case B state 文件已删除" "$sfB"
assert_file_missing "Case B worktree 目录已删除" "$wtB"

# ============================================================
# Case C: passed_pending_review + worktree dirty → hook L1 自愈回 active
# ============================================================
section "Case C: passed_pending_review + worktree dirty → 自愈"
envC=$(create_test_env --slug "lc-fixture-c" --worktree --phase active --wt-dirty "feature.txt")
sfC=$(state_file "$envC" "lc-fixture-c")
wtC="$envC/.claude/worktrees/lc-fixture-c"

# 先跑 hook 让 phase 变 passed_pending_review
run_hook "$wtC" >/dev/null

phaseC1=$(read_state_field "$sfC" "phase")
assert "Case C 第一轮后 state.phase = passed_pending_review" "[ '$phaseC1' = 'passed_pending_review' ]"

# builder 在 worktree 内改文件（模拟 reviewer 反馈修复）
echo "fix-from-reviewer" > "$wtC/fix.txt"

# 再跑 hook → L1 应触发自愈
resultC2=$(run_hook "$wtC")
assert_stderr_contains "Case C 第二轮 hook stderr 含 L1 phase 自愈" "$resultC2" "L1 phase 自愈"
assert_ec "Case C 第二轮 hook EC=2（自愈后跑 PASS_CMD 完成）" "$resultC2" 2

harness_report
