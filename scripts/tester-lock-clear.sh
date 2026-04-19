#!/usr/bin/env bash
# tester-lock-clear.sh — SubagentStop hook (matcher=tester)
#
# tester subagent 结束时清锁。
# 锁路径：${ISOLATION_LOCK_DIR:-/tmp}/cc-subagent-{session_id}.lock
#
# 退出码：始终 0

set -uo pipefail

LOCK_DIR="${ISOLATION_LOCK_DIR:-/tmp}"
LOG_FILE="${HOME}/.claude/logs/tester-lock-clear.log"
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true

INPUT="$(cat || echo '{}')"
SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || echo "")

[ -z "$SESSION_ID" ] && exit 0

LOCK_FILE="${LOCK_DIR}/cc-subagent-${SESSION_ID}.lock"
if [ -f "$LOCK_FILE" ]; then
  rm -f "$LOCK_FILE"
  printf '[%s] cleared: %s\n' "$(date -Iseconds)" "$LOCK_FILE" >> "$LOG_FILE" 2>/dev/null || true
fi

exit 0
