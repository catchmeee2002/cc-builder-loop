#!/usr/bin/env bash
# subagent-start-guard.sh — SubagentStart hook (no matcher = all subagents)
#
# V3.5: per-agent-type lock files + whitelist + active state dual-condition.
#   1. Only write lock for managed agent types (whitelist) AND when active state exists
#   2. Inject worktree boundary context via additionalContext JSON (Layer 1: soft guidance)
#
# Lock file: ${ISOLATION_LOCK_DIR:-/tmp}/cc-subagent-{session_id}-{agent_type}.lock (V3.5+)
# Content: YAML (parsed by downstream hooks via lock-utils.sh)
#
# Exit code: always 0 (hook failure must not block subagent start)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/lock-utils.sh"

LOG_FILE="${HOME}/.claude/logs/subagent-start-guard.log"
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true

log() { printf '[%s] %s\n' "$(date -Iseconds)" "$*" >> "$LOG_FILE" 2>/dev/null || true; }

INPUT="$(cat || echo '{}')"
SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || echo "")
CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null || echo "")
SUBAGENT_TYPE=$(printf '%s' "$INPUT" | jq -r '.subagent_type // empty' 2>/dev/null || echo "")

if [ -z "$SESSION_ID" ]; then
  log "no session_id in stdin, skip"
  exit 0
fi

if [ -z "$CWD" ] || [ ! -d "$CWD" ]; then
  CWD="$(pwd)"
fi

# Locate state via locate-state.sh
SKILL_DIR="${HOME}/.claude/skills/builder-loop/scripts"
LOCATE_SCRIPT="${SKILL_DIR}/locate-state.sh"
if [ ! -f "$LOCATE_SCRIPT" ]; then
  for _cand in \
    "$(dirname "$0")/../skills/builder-loop/scripts/locate-state.sh" \
    "$(pwd)/skills/builder-loop/scripts/locate-state.sh"; do
    [ -f "$_cand" ] && { LOCATE_SCRIPT="$_cand"; break; }
  done
fi

STATE_FILE=""
if [ -f "$LOCATE_SCRIPT" ]; then
  STATE_FILE="$(bash "$LOCATE_SCRIPT" "$CWD" 2>/dev/null || echo "")"
fi

if [ -z "$STATE_FILE" ]; then
  log "no state file from cwd=$CWD, type=$SUBAGENT_TYPE, skip (no active loop)"
  exit 0
fi

# V3.5: whitelist check — only managed agent types get a lock
if ! bl_is_managed_agent "$SUBAGENT_TYPE"; then
  log "agent_type=$SUBAGENT_TYPE not in whitelist, skip lock"
  exit 0
fi

# Read state fields
PROJECT_ROOT="$(grep -E '^project_root:' "$STATE_FILE" 2>/dev/null | head -1 | sed -E 's/^project_root:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || true)"
MAIN_REPO_PATH="$(grep -E '^main_repo_path:' "$STATE_FILE" 2>/dev/null | head -1 | sed -E 's/^main_repo_path:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || true)"
WORKTREE_PATH="$(grep -E '^worktree_path:' "$STATE_FILE" 2>/dev/null | head -1 | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || true)"
PHASE="$(grep -E '^phase:' "$STATE_FILE" 2>/dev/null | head -1 | sed -E 's/^phase:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || true)"
SLUG="$(basename "$STATE_FILE" .yml 2>/dev/null || echo "")"

if [ -z "$PROJECT_ROOT" ] || [ ! -d "$PROJECT_ROOT" ]; then
  PROJECT_ROOT="$(cd "$(dirname "$STATE_FILE")/../../.." 2>/dev/null && pwd -P || echo "")"
fi
[ -z "$MAIN_REPO_PATH" ] && MAIN_REPO_PATH="$PROJECT_ROOT"

# source_dirs (tester-lock-check.sh reads this from lock)
SRC_RAW=$(grep -E '^source_dirs:' "$STATE_FILE" | sed -E 's/^source_dirs:[[:space:]]*"?([^"]*)"?$/\1/' || echo "")
SRC_ABS=""
if [ -n "$SRC_RAW" ]; then
  IFS=',' read -ra DIRS <<< "$SRC_RAW"
  for d in "${DIRS[@]}"; do
    [ -z "$d" ] && continue
    SRC_ABS="${SRC_ABS}  - \"${PROJECT_ROOT}/${d}\"
"
  done
fi

# TTL from loop.yml (optional, default 30 min)
LOOP_YML="${PROJECT_ROOT}/.claude/loop.yml"
TTL_MIN=30
if [ -f "$LOOP_YML" ] && command -v python3 >/dev/null 2>&1; then
  TTL_FROM_YML=$(python3 -c "
import yaml, sys
try:
  d = yaml.safe_load(open('$LOOP_YML')) or {}
  print(d.get('isolation', {}).get('tester_lock_ttl_min', 30))
except Exception:
  print(30)
" 2>/dev/null || echo 30)
  [ -n "$TTL_FROM_YML" ] && TTL_MIN="$TTL_FROM_YML"
fi

# V3.5: per-agent-type lock file
LOCK_FILE="$(bl_lock_path "$SESSION_ID" "$SUBAGENT_TYPE")"
{
  echo "agent_type: ${SUBAGENT_TYPE:-unknown}"
  echo "session_id: ${SESSION_ID}"
  echo "project_root: \"${PROJECT_ROOT}\""
  echo "main_repo_path: \"${MAIN_REPO_PATH}\""
  echo "worktree_path: \"${WORKTREE_PATH}\""
  echo "slug: \"${SLUG}\""
  echo "start_ts: $(date +%s)"
  echo "ttl_min: ${TTL_MIN}"
  echo "source_dirs_abs:"
  if [ -n "$SRC_ABS" ]; then
    printf '%s' "$SRC_ABS"
  else
    echo "  []"
  fi
} > "$LOCK_FILE"

log "lock written: $LOCK_FILE | type=$SUBAGENT_TYPE | worktree=$WORKTREE_PATH"

# Layer 1: Inject worktree boundary context into subagent via additionalContext
if [ -n "$WORKTREE_PATH" ] && [ -d "$WORKTREE_PATH" ] && { [ "$PHASE" = "active" ] || [ "$PHASE" = "passed_pending_review" ]; }; then
  CONTEXT_MSG="WORKTREE BOUNDARY: All file operations (Write/Edit) MUST target paths under ${WORKTREE_PATH}/. The main repo at ${MAIN_REPO_PATH} is read-only during active loop. Any write outside the worktree will be blocked by PreToolUse hook."
  if command -v jq >/dev/null 2>&1; then
    jq -n --arg ctx "$CONTEXT_MSG" '{"additionalContext": $ctx}'
  else
    CTX_MSG="$CONTEXT_MSG" python3 -c "import os,json; print(json.dumps({'additionalContext': os.environ['CTX_MSG']}))" 2>/dev/null || true
  fi
  log "injected worktree boundary context for $SUBAGENT_TYPE"
fi

exit 0
