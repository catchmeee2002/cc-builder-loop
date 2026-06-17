#!/usr/bin/env bash
# worktree-write-guard.sh — PreToolUse hook (matcher=Write|Edit|MultiEdit)
#
# V3.5: uses lock-utils.sh for per-agent-type lock files.
# Two modes based on subagent lock:
#
# SUBAGENT MODE (any sync agent lock exists for this session):
#   Strict whitelist — file_path must be in worktree or whitelisted paths.
#   Violation → exit 2 (block + stderr diagnosis).
#
# BUILDER MODE (no sync lock):
#   Always pass (exit 0). Writes outside worktree are logged for debugging.
#
# Exit codes:
#   0 = allow
#   2 = deny (CC: PreToolUse exit 2 → block tool + inject stderr into LLM context)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/lock-utils.sh"

LOG_FILE="${HOME}/.claude/logs/worktree-write-guard.log"
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
log() { printf '[%s] %s\n' "$(date -Iseconds)" "$*" >> "$LOG_FILE" 2>/dev/null || true; }

INPUT="$(cat || echo '{}')"

parse_field() {
  local field="$1"
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$INPUT" | jq -r "$field // empty" 2>/dev/null || echo ""
  else
    printf '%s' "$INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    keys = '$field'.lstrip('.').split('.')
    v = d
    for k in keys:
        v = (v or {}).get(k, '')
    print(v if v is not None else '')
except Exception:
    print('')
" 2>/dev/null || echo ""
  fi
}

SESSION_ID="$(parse_field '.session_id')"
TARGET="$(parse_field '.tool_input.file_path')"

[ -z "$SESSION_ID" ] && exit 0
[ -z "$TARGET" ] && exit 0

ABS_TARGET="$(readlink -f "$TARGET" 2>/dev/null || echo "$TARGET")"

# ---- V3.5: Check per-agent-type lock files → subagent or builder? ----
SYNC_LOCK=""
ACTIVE_AGENT_TYPE=""
_LOCK_COUNT=0

while IFS= read -r _lock; do
  [ -z "$_lock" ] && continue
  _LOCK_COUNT=$(( _LOCK_COUNT + 1 ))
  _atype="$(bl_read_lock_field "$_lock" "agent_type")"
  if bl_is_sync_agent "$_atype"; then
    # TTL check
    _start="$(bl_read_lock_field "$_lock" "start_ts")"
    _ttl_min="$(bl_read_lock_field "$_lock" "ttl_min")"
    [ -z "$_ttl_min" ] && _ttl_min=30
    _now="$(date +%s)"
    _age=$(( _now - ${_start:-0} ))
    _ttl_sec=$(( _ttl_min * 60 ))
    if [ "$_age" -gt "$_ttl_sec" ]; then
      log "lock expired (age=${_age}s ttl=${_ttl_sec}s), removing: $_lock"
      rm -f "$_lock"
      continue
    fi
    SYNC_LOCK="$_lock"
    ACTIVE_AGENT_TYPE="$_atype"
    break
  fi
done < <(bl_find_active_locks "$SESSION_ID")

# V3.7 diagnostic: log lock resolution for tester-write-to-main-repo investigation
log "lock-resolve: sid=${SESSION_ID:0:8} locks_found=${_LOCK_COUNT} sync_lock=${SYNC_LOCK:-NONE} agent=${ACTIVE_AGENT_TYPE:-NONE} target=${ABS_TARGET}"

if [ -n "$SYNC_LOCK" ]; then
  # ---- SUBAGENT STRICT MODE ----
  WORKTREE_PATH="$(bl_read_lock_field "$SYNC_LOCK" "worktree_path")"
  MAIN_REPO_PATH="$(bl_read_lock_field "$SYNC_LOCK" "main_repo_path")"

  log "strict-mode: wt=${WORKTREE_PATH:-EMPTY} main=${MAIN_REPO_PATH:-EMPTY}"
  [ -z "$WORKTREE_PATH" ] && { log "wt-empty→allow"; exit 0; }

  ABS_WORKTREE="$(readlink -f "$WORKTREE_PATH" 2>/dev/null || echo "$WORKTREE_PATH")"
  ABS_MAIN="$(readlink -f "$MAIN_REPO_PATH" 2>/dev/null || echo "$MAIN_REPO_PATH")"

  case "$ABS_TARGET" in
    "$ABS_WORKTREE"/*)                              exit 0 ;;
    "$ABS_MAIN"/.claude/builder-loop/state/*)        exit 0 ;;
    "$ABS_MAIN"/.claude/reviewer-diff-*)             exit 0 ;;
    "$ABS_MAIN"/.claude/review_reports/*)            exit 0 ;;
    "$ABS_MAIN"/.claude/builder-loop/*.pause)        exit 0 ;;
    "$ABS_MAIN"/*)                                   ;;
    /tmp/*)                                          exit 0 ;;
  esac

  log "DENY: $ACTIVE_AGENT_TYPE target=$ABS_TARGET worktree=$ABS_WORKTREE main=$ABS_MAIN"

  cat >&2 <<DENY_MSG
[builder-loop] worktree-write-guard: ${ACTIVE_AGENT_TYPE} write blocked
   target:  ${TARGET}
   resolved: ${ABS_TARGET}
   allowed: ${ABS_WORKTREE}/*
   main:    ${ABS_MAIN} (subagent writes outside worktree are blocked)
   fix:     use ${ABS_WORKTREE}/<relative-path> instead
DENY_MSG
  exit 2
fi

# ---- BUILDER MODE (no lock) ----
# Always pass. Log writes outside worktree for debugging.

SKILL_DIR="${HOME}/.claude/skills/builder-loop/scripts"
LOCATE_SCRIPT="${SKILL_DIR}/locate-state.sh"
if [ ! -f "$LOCATE_SCRIPT" ]; then
  for _cand in \
    "$(dirname "$0")/../skills/builder-loop/scripts/locate-state.sh" \
    "$(pwd)/skills/builder-loop/scripts/locate-state.sh"; do
    [ -f "$_cand" ] && { LOCATE_SCRIPT="$_cand"; break; }
  done
fi

[ ! -f "$LOCATE_SCRIPT" ] && exit 0

STATE_FILE="$(bash "$LOCATE_SCRIPT" 2>/dev/null || echo "")"
[ -z "$STATE_FILE" ] && exit 0

PHASE="$(grep -E '^phase:' "$STATE_FILE" 2>/dev/null | head -1 | sed -E 's/^phase:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || true)"
[ "$PHASE" != "active" ] && exit 0

WORKTREE_PATH="$(grep -E '^worktree_path:' "$STATE_FILE" 2>/dev/null | head -1 | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || true)"
[ -z "$WORKTREE_PATH" ] && exit 0

ABS_WORKTREE="$(readlink -f "$WORKTREE_PATH" 2>/dev/null || echo "$WORKTREE_PATH")"

case "$ABS_TARGET" in
  "$ABS_WORKTREE"/*)
    ;;
  *)
    log "builder-outside-worktree: target=$ABS_TARGET worktree=$ABS_WORKTREE"
    ;;
esac

exit 0
