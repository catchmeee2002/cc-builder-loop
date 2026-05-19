#!/usr/bin/env bash
# worktree-write-guard.sh — PreToolUse hook (matcher=Write|Edit|MultiEdit)
#
# V3.1 unified write boundary guard. Replaces tester-write-guard.sh.
# Two modes based on subagent lock:
#
# SUBAGENT MODE (lock exists, sync agent type: tester/doc-maintainer/arbiter):
#   Strict whitelist — file_path must be in worktree or whitelisted paths.
#   Violation → exit 2 (block + stderr diagnosis).
#
# BUILDER MODE (no lock, or background agent like reviewer):
#   Always pass (exit 0). Writes outside worktree are logged for debugging.
#
# Exit codes:
#   0 = allow
#   2 = deny (CC: PreToolUse exit 2 → block tool + inject stderr into LLM context)
#
# Performance: subagent mode reads lock file only (fast). Builder mode calls
# locate-state.sh (slower, but builder Write/Edit calls are infrequent).

set -uo pipefail

LOCK_DIR="${ISOLATION_LOCK_DIR:-/tmp}"
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

# ---- Check lock file → subagent or builder? ----
LOCK_FILE="${LOCK_DIR}/cc-subagent-${SESSION_ID}.lock"

if [ -f "$LOCK_FILE" ]; then
  AGENT_TYPE="$(grep -E '^agent_type:' "$LOCK_FILE" 2>/dev/null | head -1 | sed -E 's/^agent_type:[[:space:]]*//' || true)"

  # Sync agents block the builder → all writes are from the subagent → strict mode.
  # Background agents (reviewer): builder continues working → pass through.
  case "$AGENT_TYPE" in
    tester|doc-maintainer|arbiter)
      # ---- SUBAGENT STRICT MODE ----

      START_TS="$(grep -E '^start_ts:' "$LOCK_FILE" 2>/dev/null | head -1 | sed -E 's/^start_ts:[[:space:]]*//' || echo 0)"
      TTL_MIN="$(grep -E '^ttl_min:' "$LOCK_FILE" 2>/dev/null | head -1 | sed -E 's/^ttl_min:[[:space:]]*//' || echo 30)"
      NOW="$(date +%s)"
      AGE=$(( NOW - START_TS ))
      TTL_SEC=$(( TTL_MIN * 60 ))
      if [ "$AGE" -gt "$TTL_SEC" ]; then
        log "lock expired (age=${AGE}s ttl=${TTL_SEC}s), removing & passing"
        rm -f "$LOCK_FILE"
        exit 0
      fi

      WORKTREE_PATH="$(grep -E '^worktree_path:' "$LOCK_FILE" 2>/dev/null | head -1 | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || true)"
      MAIN_REPO_PATH="$(grep -E '^main_repo_path:' "$LOCK_FILE" 2>/dev/null | head -1 | sed -E 's/^main_repo_path:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || true)"

      [ -z "$WORKTREE_PATH" ] && exit 0

      ABS_WORKTREE="$(readlink -f "$WORKTREE_PATH" 2>/dev/null || echo "$WORKTREE_PATH")"
      ABS_MAIN="$(readlink -f "$MAIN_REPO_PATH" 2>/dev/null || echo "$MAIN_REPO_PATH")"

      # Whitelist: paths subagents may write outside worktree.
      # Order matters: worktree first, then main_repo whitelisted sub-paths,
      # then main_repo catch-all (fall through to deny), then /tmp last
      # (prevents /tmp/* from falsely matching when main_repo is under /tmp).
      case "$ABS_TARGET" in
        "$ABS_WORKTREE"/*)                              exit 0 ;;
        "$ABS_MAIN"/.claude/builder-loop/state/*)        exit 0 ;;
        "$ABS_MAIN"/.claude/reviewer-diff-*)             exit 0 ;;
        "$ABS_MAIN"/.claude/review_reports/*)            exit 0 ;;
        "$ABS_MAIN"/.claude/builder-loop/*.pause)        exit 0 ;;
        "$ABS_MAIN"/*)                                   ;;
        /tmp/*)                                          exit 0 ;;
      esac

      log "DENY: $AGENT_TYPE target=$ABS_TARGET worktree=$ABS_WORKTREE main=$ABS_MAIN"

      cat >&2 <<DENY_MSG
[builder-loop] worktree-write-guard: ${AGENT_TYPE} write blocked
   target:  ${TARGET}
   resolved: ${ABS_TARGET}
   allowed: ${ABS_WORKTREE}/*
   main:    ${ABS_MAIN} (subagent writes outside worktree are blocked)
   fix:     use ${ABS_WORKTREE}/<relative-path> instead
DENY_MSG
      exit 2
      ;;
    *)
      log "background-agent lock ($AGENT_TYPE), pass: $ABS_TARGET"
      exit 0
      ;;
  esac
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
