#!/usr/bin/env bash
# test-e2e-pending-dirty-guard.sh — V5.2 e2e_pending L1 dirty guard
#
# Case A: e2e_pending + dirty → L1 不自愈（防 tester 写文件触发无限循环）
# Case B: e2e_pending + 新 commit → L1 正常自愈回 active（builder 改完代码 commit 后恢复流程）
#
# 注意：CWD 用主仓（非 worktree），locate-state 走策略 5 自动绑定唯一 active state。
# 这避免了 CWD-in-worktree 的 pre-existing locate-state 问题。

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "e2e-pending-dirty-guard"

# ============================================================
# Case A: e2e_pending + dirty → exit 0，phase 不变
# ============================================================
section "Case A: e2e_pending + dirty → L1 不自愈"
envA=$(create_test_env --slug "e2e-dirty-a" --worktree --phase e2e_pending --wt-dirty "feature.txt")
sfA=$(state_file "$envA" "e2e-dirty-a")

# 用主仓 CWD 跑 hook（策略 5 唯一 active 绑定）
resultA=$(run_hook "$envA")
assert_ec "Case A hook EC=0（e2e_pending dirty 不自愈）" "$resultA" 0
phaseA=$(read_state_field "$sfA" "phase")
assert "Case A phase 仍为 e2e_pending" "[ '$phaseA' = 'e2e_pending' ]"

# ============================================================
# Case B: e2e_pending + 新 commit → 自愈回 active → PASS
# ============================================================
section "Case B: e2e_pending + 新 commit → L1 自愈"
envB=$(create_test_env --slug "e2e-dirty-b" --worktree --phase e2e_pending --wt-dirty "feature.txt")
sfB=$(state_file "$envB" "e2e-dirty-b")
wtB="$envB/.claude/worktrees/e2e-dirty-b"

# commit dirty 文件（HEAD 变化触发 new_commit 自愈）
git -C "$wtB" add feature.txt
git -C "$wtB" commit -m "fix(test): [cr_id_skip] Code fix" --quiet

# 用主仓 CWD 跑 hook
resultB=$(run_hook "$envB")
assert_stderr_contains "Case B hook stderr 含 L1 phase 自愈" "$resultB" "L1 phase 自愈"
assert_ec "Case B hook EC=2（自愈后跑 PASS_CMD）" "$resultB" 2

harness_report
