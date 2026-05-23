#!/usr/bin/env bash
# test-pause-file.sh — V3.0 L3 闸：pause 文件存在时 hook 静默
#
# 验证：
#   Case 1: 创建 pause 文件 → hook exit 0 + 不跑 PASS_CMD
#   Case 2: rm pause 文件 → hook 进 PASS_CMD 路径

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "pause-file"

SLUG="pause-fixture-slug"
env=$(create_test_env --slug "$SLUG" --worktree --phase active --wt-dirty "dirty.txt")
sf=$(state_file "$env" "$SLUG")
wt="$env/.claude/worktrees/$SLUG"

# ============================================================
# Case 1: pause 文件存在 → hook 静默 exit 0
# ============================================================
section "Case 1: pause 文件存在"
mkdir -p "$env/.claude/builder-loop"
touch "$env/.claude/builder-loop/${SLUG}.pause"

result1=$(run_hook "$wt")
assert_ec "Case 1 hook EC=0（静默）" "$result1" 0
assert_stderr_contains "Case 1 stderr 含 L3 pause 命中" "$result1" "L3 pause"
assert_file_exists "Case 1 state 仍存在（未被处理）" "$sf"
iter1=$(read_state_field "$sf" "iter")
assert "Case 1 iter 不变（PASS_CMD 未跑）" "[ '$iter1' = '0' ]"

# ============================================================
# Case 2: rm pause 文件 → hook 进 PASS_CMD 路径
# ============================================================
section "Case 2: rm pause 文件"
rm -f "$env/.claude/builder-loop/${SLUG}.pause"

result2=$(run_hook "$wt")
assert "Case 2 hook 不再因 pause 静默" "! grep -q 'L3 pause' '$result2/stderr' 2>/dev/null"

harness_report
