#!/usr/bin/env bash
# test-old-state-compat.sh — 无 phase 字段的 state 处理
#
# 验证：
#   Case A: state 缺 phase 字段 → 当僵尸归档（exit 0）
#   Case B: state 含 phase=active（标准）→ 正常走 PASS_CMD

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "old-state-compat"

# ============================================================
# Case A: 无 phase 字段 → 僵尸归档
# ============================================================
section "Case A: 无 phase 字段 → 僵尸归档"
envA=$(create_test_env --slug "old-state-fixture-a" --worktree --no-state --wt-dirty "feature.txt")
sfA=$(state_file "$envA" "old-state-fixture-a")
wtA="$envA/.claude/worktrees/old-state-fixture-a"
headA=$(git -C "$envA" rev-parse --short HEAD)

mkdir -p "$envA/.claude/builder-loop/state"
cat > "$sfA" <<STEOF
slug: "old-state-fixture-a"
owner_cwd: "$envA"
iter: 0
max_iter: 5
project_root: "$wtA"
main_repo_path: "$envA"
start_head: "$headA"
worktree_path: "$wtA"
worktree_mode: "clean"
task_description: |
  old-state-test
source_dirs: ""
test_dirs: ""
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
stopped_reason: ""
created_at: "2026-04-15T00:00:00+08:00"
STEOF

resultA=$(run_hook "$wtA")
assert_ec "Case A hook exit 0（僵尸归档放行）" "$resultA" 0
assert_stderr_contains "Case A stderr 含僵尸归档提示" "$resultA" "僵尸"
assert_file_missing "Case A state 已从 state/ 挪走" "$sfA"

# ============================================================
# Case B: V3.0 标准 state（含 phase 字段）→ 无重复 warning
# ============================================================
section "Case B: V3.0 标准 state（含 phase 字段）"
envB=$(create_test_env --slug "old-state-fixture-b" --worktree --phase active --wt-dirty "feature.txt")
sfB=$(state_file "$envB" "old-state-fixture-b")
wtB="$envB/.claude/worktrees/old-state-fixture-b"

resultB=$(run_hook "$wtB")
assert_ec "Case B hook 正常（EC=2）" "$resultB" 2
assert "Case B stderr 不含老 state warning" \
  "! grep -q '检测到 V2.x 老 state' '$resultB/stderr' 2>/dev/null"

harness_report
