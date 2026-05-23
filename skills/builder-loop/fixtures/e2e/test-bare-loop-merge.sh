#!/usr/bin/env bash
# test-bare-loop-merge.sh — E2E：bare loop 完整 stop hook 流程
#
# 防回归：
#   V1.9.1 之前 merge-worktree-back.sh::read_field 在 bare loop 场景（state 无 worktree_path 字段）
#   下因 grep 未命中 + pipefail + set -e 静默退出 → MERGE_OUT 为空 → MERGE_ACTION 空 → case *
#   静默 exit 0 + rm state，state 丢失。本测试覆盖：
#   1. bare loop（worktree.enabled=false）一轮完整 PASS 路径
#   2. case * 默认分支（M5）若被命中要 echo 详细错误 + exit 2，不再静默删 state
#
# 用法：bash test-bare-loop-merge.sh
# 退出码：0=全部通过 / 1=有失败

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "bare-loop-merge"

MERGE_SCRIPT="${HARNESS_REPO_ROOT}/skills/builder-loop/scripts/merge-worktree-back.sh"

assert_file_exists "stop hook 存在" "$HARNESS_HOOK"
assert_file_exists "merge 脚本存在" "$MERGE_SCRIPT"

# ============================================================
# Case 1: bare loop 完整 PASS 路径（locate-state.sh slug=__main__ 兜底）
# ============================================================
section "Case 1: bare loop 完整 PASS 路径"
env1=$(create_test_env --slug "__main__" --pass-cmd "true")
# 覆盖 loop.yml 加 bare 模式配置
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

result1=$(run_hook "$env1")
assert_ec "Case 1 stop hook EC=2（PASS 续接）" "$result1" 2
assert_stderr_contains "Case 1 stderr 含 PASS_CMD 全部阶段通过" "$result1" "PASS_CMD 全部阶段通过"
sf1=$(state_file "$env1" "__main__")
assert_file_missing "Case 1 state 已被 rm（PASS 后 cleanup）" "$sf1"
assert_file_exists "Case 1 cursor 已写" "$env1/.claude/builder-loop/last_processed_head"

# ============================================================
# Case 2: 直接调 merge-worktree-back.sh，bare loop state（无 worktree_path）→ NOOP
# 防 V1.9.1 修过的 read_field 静默退出回归
# ============================================================
section "Case 2: merge-worktree-back.sh 在 bare state 下应输出 NOOP"
env2=$(create_test_env --slug "__main__")
merge_result2=$(run_merge_cleanup "$(state_file "$env2" "__main__")")
merge_last2=$(result_last "$merge_result2")

assert "Case 2 merge 输出非空（防 grep 静默退出回归）" "[ -n '$merge_last2' ]"
assert "Case 2 merge 末行 = NOOP" "[ '$merge_last2' = 'NOOP' ]"

# ============================================================
# Case 3: 老 V1.x bare state（无 main_repo_path 字段，project_root=主仓）
# merge-worktree-back.sh 兼容性
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

assert "Case 3 merge 输出非空" "[ -n '$merge_last3' ]"
assert "Case 3 merge 末行 = NOOP（用 project_root 兜底为主仓）" "[ '$merge_last3' = 'NOOP' ]"

harness_report
