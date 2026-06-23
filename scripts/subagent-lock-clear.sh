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
AGENT_ID=$(printf '%s' "$INPUT" | jq -r '.agent_id // empty' 2>/dev/null || echo "")
TRANSCRIPT_PATH=$(printf '%s' "$INPUT" | jq -r '.agent_transcript_path // empty' 2>/dev/null || echo "")
CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null || echo "")

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

# V4.3: update agent identity in state (tester + reviewer only)
case "$SUBAGENT_TYPE" in tester|reviewer)
  [ -z "$CWD" ] || [ ! -d "$CWD" ] && CWD="$(pwd)"
  SKILL_DIR="${HOME}/.claude/skills/builder-loop/scripts"
  LOCATE_SCRIPT="${SKILL_DIR}/locate-state.sh"
  [ ! -f "$LOCATE_SCRIPT" ] && for _cand in \
    "$(dirname "$0")/../skills/builder-loop/scripts/locate-state.sh" \
    "$(pwd)/skills/builder-loop/scripts/locate-state.sh"; do
    [ -f "$_cand" ] && { LOCATE_SCRIPT="$_cand"; break; }
  done
  STATE_FILE=""
  [ -f "$LOCATE_SCRIPT" ] && STATE_FILE="$(bash "$LOCATE_SCRIPT" "$CWD" 2>/dev/null || echo "")"
  if [ -n "$STATE_FILE" ] && [ -f "$STATE_FILE" ]; then
    STATE_FILE="$STATE_FILE" AGENT_TYPE="$SUBAGENT_TYPE" \
      TRANSCRIPT_P="$TRANSCRIPT_PATH" python3 -c "
import os, re
sf = os.environ['STATE_FILE']
at = os.environ['AGENT_TYPE']
tp = os.environ.get('TRANSCRIPT_P', '')
text = open(sf).read()
pat = rf'^  {at}:\n(?:    .+\n)*'
m = re.search(pat, text, re.M)
if m:
    block = m.group(0)
    block = re.sub(r'status: \"[^\"]*\"', 'status: \"idle\"', block)
    block = re.sub(r'transcript_path: \"[^\"]*\"', f'transcript_path: \"{tp}\"', block)
    text = text[:m.start()] + block + text[m.end():]
    open(sf, 'w').write(text)
" 2>/dev/null || true
    log "identity updated in state: type=$SUBAGENT_TYPE status=idle"
  fi
  ;; esac

exit 0
