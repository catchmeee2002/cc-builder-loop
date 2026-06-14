#!/usr/bin/env bash
# subagent-lock-clear.sh — SubagentStop hook (no matcher = all subagents)
#
# V3.5: replaces tester-lock-clear.sh. Clears lock for any managed agent type.
# Lock path: cc-subagent-{session_id}-{agent_type}.lock
# Fallback: if agent_type is empty, glob-delete all locks for this session.
#
# Exit code: always 0

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/lock-utils.sh"

LOG_FILE="${HOME}/.claude/logs/subagent-lock-clear.log"
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true

log() { printf '[%s] %s\n' "$(date -Iseconds)" "$*" >> "$LOG_FILE" 2>/dev/null || true; }

INPUT="$(cat || echo '{}')"
SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || echo "")
SUBAGENT_TYPE=$(printf '%s' "$INPUT" | jq -r '.subagent_type // empty' 2>/dev/null || echo "")

[ -z "$SESSION_ID" ] && exit 0

if [ -n "$SUBAGENT_TYPE" ]; then
  LOCK_FILE="$(bl_lock_path "$SESSION_ID" "$SUBAGENT_TYPE")"
  if [ -f "$LOCK_FILE" ]; then
    rm -f "$LOCK_FILE"
    log "cleared: $LOCK_FILE"
  fi
  # Also clean legacy lock if it matches this agent_type
  LEGACY="$(bl_legacy_lock_path "$SESSION_ID")"
  if [ -f "$LEGACY" ]; then
    LEGACY_TYPE="$(bl_read_lock_field "$LEGACY" "agent_type")"
    if [ "$LEGACY_TYPE" = "$SUBAGENT_TYPE" ]; then
      rm -f "$LEGACY"
      log "cleared legacy: $LEGACY (type=$LEGACY_TYPE)"
    fi
  fi
else
  # Fallback: agent_type unknown, clean all locks for this session
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    rm -f "$f"
    log "cleared (fallback): $f"
  done < <(bl_find_active_locks "$SESSION_ID")
fi

exit 0
