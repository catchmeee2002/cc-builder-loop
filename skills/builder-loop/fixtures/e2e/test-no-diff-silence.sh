#!/usr/bin/env bash
# test-no-diff-silence.sh — V3.0 L2B 闸：worktree HEAD == last_iter_head + git status 空 → 静默
#
# 验证：
#   Case 1: state.last_iter_head == worktree HEAD + 工作树 clean → hook 静默 exit 0
#   Case 2: 在 worktree 内改一个文件（dirty 出现）→ hook 进 PASS_CMD 路径
#   Case 3: bare 模式 L2B（worktree_path 空，主仓 cwd，无改动）→ 静默

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "no-diff-silence"

# ============================================================
# Case 1: HEAD == last_iter_head + git status 空 → 静默
# ============================================================
section "Case 1: worktree 无改动"
# iter=1 让 last_iter_head 等于当前 HEAD（create_test_env 默认 iter=0 但 last_iter_head 已等于 HEAD）
env1=$(create_test_env --slug "nodiff-fixture" --worktree --phase active)
sf1=$(state_file "$env1" "nodiff-fixture")
wt1="$env1/.claude/worktrees/nodiff-fixture"
# 把 iter 改为 1（模拟非首轮，L2B 需要 iter>0 或 HEAD==last_iter_head）
sed -i 's/^iter: 0/iter: 1/' "$sf1"

result1=$(run_hook "$wt1")
assert_ec "Case 1 hook EC=0（静默）" "$result1" 0
assert "Case 1 PASS_CMD 未跑" "! grep -q '正在跑 PASS_CMD' '$result1/stderr' 2>/dev/null"
iter1=$(read_state_field "$sf1" "iter")
assert "Case 1 iter 不变（=1）" "[ '$iter1' = '1' ]"

# ============================================================
# Case 2: worktree 出现 dirty → 不再静默
# ============================================================
section "Case 2: worktree 内改文件"
echo "new content" > "$wt1/dirty-file.txt"

result2=$(run_hook "$wt1")
assert "Case 2 hook 不再因 L2B 静默" \
  "grep -q '正在跑 PASS_CMD\|PASS_CMD\|phase=passed_pending_review\|已 commit' '$result2/stderr' 2>/dev/null"

# ============================================================
# Case 3: bare 模式 L2B（worktree_path 空，主仓 cwd，无改动）
# ============================================================
section "Case 3: bare 模式 L2B 静默"
env3=$(create_test_env --slug "__main__" --phase active)
sf3=$(state_file "$env3" "__main__")
sed -i 's/^iter: 0/iter: 1/' "$sf3"

result3=$(run_hook "$env3")
assert_ec "Case 3 bare 模式 hook EC=0（L2B 静默）" "$result3" 0
assert "Case 3 bare 模式 PASS_CMD 未跑" "! grep -q '正在跑 PASS_CMD' '$result3/stderr' 2>/dev/null"

harness_report
