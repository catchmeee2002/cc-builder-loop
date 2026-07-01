#!/usr/bin/env bash
# test-fork-aware-stop-hook.sh — V4.10 fork-aware stop hook L2C gate
#
# Case 1: fork 锁存在 → stop hook exit 0 静默（L2C gate）
# Case 2: fork 锁不存在 → stop hook 正常跑 PASS_CMD
# Case 3: 非 loop 状态 → fork 锁不影响（无 state 直接 exit 0）
#
# 用法：bash test-fork-aware-stop-hook.sh

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "fork-aware-stop-hook"

LOCK_UTILS="${HARNESS_REPO_ROOT}/scripts/lock-utils.sh"

# ==============================================================
section "Case 1: fork 锁存在 → L2C gate 静默 exit 0"
# ==============================================================

env1="$(create_test_env --slug "fork-gate-1" --phase active --pass-cmd "echo PASS_RAN")"
sid1="test-session-fork-1"

source "$LOCK_UTILS"
_BL_LOCK_DIR="/tmp"
FORK_LOCK_1="$(bl_lock_path "$sid1" "fork")"
cat > "$FORK_LOCK_1" <<LOCKEOF
agent_type: fork
session_id: ${sid1}
agent_id: "fake-fork-id-001"
start_ts: $(date +%s)
LOCKEOF

r1="$(run_hook "$env1" "$sid1")"
ec1="$(result_ec "$r1")"
stderr1="$(result_stderr "$r1")"
stdout1="$(result_stdout "$r1")"

assert "Case 1 fork 锁存在 → exit 0" \
  "[ '$ec1' = '0' ]"
assert "Case 1 PASS_CMD 未执行（stdout 不含 PASS_RAN）" \
  "! echo '$stdout1' | grep -q 'PASS_RAN'"

rm -f "$FORK_LOCK_1"

# ==============================================================
section "Case 2: fork 锁不存在 → 正常跑 PASS_CMD"
# ==============================================================

env2="$(create_test_env --slug "fork-gate-2" --phase active --pass-cmd "echo PASS_RAN" --dirty "changed.txt")"
sid2="test-session-fork-2"

r2="$(run_hook "$env2" "$sid2")"
ec2="$(result_ec "$r2")"
stderr2="$(result_stderr "$r2")"

assert "Case 2 无 fork 锁 → exit 2（PASS_CMD 跑了）" \
  "[ '$ec2' = '2' ]"

# ==============================================================
section "Case 3: fork 在白名单中"
# ==============================================================

source "$LOCK_UTILS"
assert "Case 3 fork 在 BL_AGENT_WHITELIST 中" \
  "bl_is_managed_agent fork"

assert "Case 3 fork 不在 BL_SYNC_AGENTS 中（fork 是异步的）" \
  "! bl_is_sync_agent fork"

harness_report
