#!/usr/bin/env bash
# test-zombie-selfheal.sh — E2E 测试：V1.8.1 僵尸 state 自愈 + EARLY_STOP 立即通知
#
# 场景：
#   S1. Stop hook 遇到 active=false 僵尸 state → 归档到 legacy/ + exit 0 放行
#   S2. Stop hook 遇到 EARLY_STOP（max_iter 触发）→ 归档 + exit 2 + stderr 注入

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "zombie-selfheal"

# ---- 初始化共享仓库 ----
env=$(create_test_env --slug "__main__" --pass-cmd "false" --no-state)
STATE_DIR="$env/.claude/builder-loop/state"
LEGACY_DIR="$env/.claude/builder-loop/legacy"
STATE_FILE="$STATE_DIR/__main__.yml"
mkdir -p "$STATE_DIR"

# ============================================================
# S1: phase 缺失的僵尸 → 归档
# ============================================================
section "S1: phase 缺失僵尸归档"
cat > "$STATE_FILE" <<ZOMBIE
slug: "__main__"
iter: 0
max_iter: 5
project_root: "$env"
main_repo_path: "$env"
start_head: "init"
worktree_path: ""
task_description: |
  zombie test
source_dirs: "src"
test_dirs: "tests"
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
stopped_reason: "manual-merge-completed"
created_at: "2026-04-24T00:00:00+00:00"
ZOMBIE

assert_file_exists "僵尸 state 预置成功" "$STATE_FILE"

r1=$(run_hook "$env")
assert_stderr_contains "hook 输出含归档提示" "$r1" "归档到 legacy"
assert_ec "hook exit 0 放行" "$r1" 0
assert_file_missing "僵尸 state 已从 state/ 挪走" "$STATE_FILE"
assert "legacy/ 出现 zombie_no_phase bak" "ls '$LEGACY_DIR'/*-zombie_no_phase.bak >/dev/null 2>&1"

# ============================================================
# S2: EARLY_STOP（max_iter 触发）→ 归档 + exit 2 + stderr 注入
# ============================================================
section "S2: EARLY_STOP max_iter → 归档 + exit 2"
rm -rf "$STATE_DIR" "$LEGACY_DIR"
mkdir -p "$STATE_DIR"

cat > "$STATE_FILE" <<ACTIVE
phase: "active"
slug: "__main__"
iter: 2
max_iter: 2
project_root: "$env"
main_repo_path: "$env"
start_head: "init"
worktree_path: ""
task_description: |
  early stop test
source_dirs: "src"
test_dirs: "tests"
last_pass_stage: "always_fail"
last_error_hash: "deadbeef"
last_error_count: 1
stopped_reason: ""
created_at: "2026-04-24T00:00:00+00:00"
ACTIVE

r2=$(run_hook "$env")
assert_stderr_contains "hook 输出含 early stop 提示" "$r2" "early stop"
assert_stderr_contains "hook 输出含 AskUserQuestion 引导" "$r2" "AskUserQuestion"
assert_ec "hook exit 2（立即通知 builder）" "$r2" 2
assert_file_missing "state 已归档" "$STATE_FILE"
assert "legacy/ 出现 early_stop_ bak" "ls '$LEGACY_DIR'/*-early_stop_*.bak >/dev/null 2>&1"

harness_report
