#!/usr/bin/env bash
# test-old-state-compat.sh — V3.0 老 state（V2.x 创建，无 phase 字段）兼容
#
# 验证：
#   Case A: state 含 active 但缺 phase 字段 → hook stderr warning + 不阻断 + PASS 后自动写 phase
#   Case B: state 含 active + phase=active（V3.0 标准）→ 无 warning（不重复提示）

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "old-state-compat"

# ============================================================
# Case A: V2.x 老 state（无 phase 字段）
# ============================================================
section "Case A: V2.x 老 state（无 phase 字段）"
envA=$(create_test_env --slug "old-state-fixture-a" --worktree --no-state --wt-dirty "feature.txt")
sfA=$(state_file "$envA" "old-state-fixture-a")
wtA="$envA/.claude/worktrees/old-state-fixture-a"
headA=$(git -C "$envA" rev-parse --short HEAD)

# 手工写 V2.x 格式 state（无 phase/last_iter_head/cleanup_phase）
mkdir -p "$envA/.claude/builder-loop/state"
cat > "$sfA" <<STEOF
active: true
slug: "old-state-fixture-a"
owner_cwd: "$envA"
iter: 0
max_iter: 5
project_root: "$wtA"
main_repo_path: "$envA"
start_head: "$headA"
worktree_path: "$wtA"
worktree_mode: "clean"
plan_file: ""
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
assert_ec "Case A hook 不阻断（EC=2 PASS 续接）" "$resultA" 2
assert_stderr_contains "Case A stderr 含老 state warning" "$resultA" "检测到 V2.x 老 state"
assert_stderr_contains "Case A stderr 含自动升级提示" "$resultA" "已自动升级到 V3.0 schema"
phaseAfter=$(read_state_field "$sfA" "phase")
assert "Case A PASS 后 state 已自动升级（phase=passed_pending_review）" \
  "[ '$phaseAfter' = 'passed_pending_review' ]"

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
