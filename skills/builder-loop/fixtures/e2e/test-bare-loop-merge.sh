#!/usr/bin/env bash
# test-bare-loop-merge.sh — E2E：bare loop 完整 stop hook 流程
#
# V4.1 更新：bare 模式升级到 reviewer-as-gate，行为对齐 worktree 模式：
#   1. bare loop PASS → loop-commit.sh commit → phase=passed_pending_review（state 保留）
#   2. merge-and-cleanup.sh 接受 bare mode → stash drop + rm state → MERGED __main__
#
# 用法：bash test-bare-loop-merge.sh
# 退出码：0=全部通过 / 1=有失败

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "bare-loop-merge"

assert_file_exists "stop hook 存在" "$HARNESS_HOOK"

# ============================================================
# Case 1: bare loop 完整 PASS 路径 → phase=passed_pending_review（state 保留）
# ============================================================
section "Case 1: bare loop PASS → phase=passed_pending_review"
env1=$(create_test_env --slug "__main__" --pass-cmd "true")
cat > "$env1/.claude/loop.yml" <<'YMLEOF'
pass_cmd:
  - stage: smoke
    cmd: "true"
    timeout: 10
max_iterations: 3
layout:
  source_dirs: [src]
worktree:
  enabled: false
YMLEOF
mkdir -p "$env1/src"
echo "change" > "$env1/src/test.py"
git -C "$env1" add -A >/dev/null 2>&1 && git -C "$env1" commit -m "chore: [cr_id_skip] add test file" >/dev/null 2>&1
echo "modified" > "$env1/src/test.py"

result1=$(run_hook "$env1")
assert_ec "Case 1 stop hook EC=2（PASS 续接）" "$result1" 2
assert_stderr_contains "Case 1 stderr 含 PASS_CMD 全部阶段通过" "$result1" "PASS_CMD 全部阶段通过"
sf1=$(state_file "$env1" "__main__")
assert_file_exists "Case 1 state 保留（reviewer-as-gate）" "$sf1"
phase1=$(read_state_field "$sf1" "phase")
assert "Case 1 phase=passed_pending_review" "[ '$phase1' = 'passed_pending_review' ]"
assert_file_exists "Case 1 cursor 已写" "$env1/.claude/builder-loop/last_processed_head"

# ============================================================
# Case 2: merge-and-cleanup.sh 接受 bare state → MERGED __main__
# ============================================================
section "Case 2: merge-and-cleanup.sh 在 bare state 下 → MERGED __main__"
env2=$(create_test_env --slug "__main__")
merge_result2=$(run_merge_cleanup "$(state_file "$env2" "__main__")")
merge_last2=$(result_last "$merge_result2")
merge_ec2=$(result_ec "$merge_result2")

assert "Case 2 merge EC=0" "[ '$merge_ec2' -eq 0 ]"
assert "Case 2 merge 输出 MERGED __main__" "echo '$merge_last2' | grep -q 'MERGED __main__'"
sf2=$(state_file "$env2" "__main__")
assert_file_missing "Case 2 state 已删" "$sf2"

# ============================================================
# Case 3: 老 V1.x bare state（无 main_repo_path 字段）兼容
# ============================================================
section "Case 3: 老 V1.x bare state（缺 main_repo_path）兼容"
env3=$(create_test_env --slug "__main__" --no-state)
mkdir -p "$env3/.claude/builder-loop/state"
cat > "$env3/.claude/builder-loop/state/__main__.yml" <<EOF
active: true
slug: "__main__"
project_root: "$env3"
start_head: "deadbeef"
worktree_path: ""
EOF

merge_result3=$(run_merge_cleanup "$env3/.claude/builder-loop/state/__main__.yml")
merge_last3=$(result_last "$merge_result3")
merge_ec3=$(result_ec "$merge_result3")

assert "Case 3 merge EC=0" "[ '$merge_ec3' -eq 0 ]"
assert "Case 3 merge 输出 MERGED __main__" "echo '$merge_last3' | grep -q 'MERGED __main__'"

harness_report
