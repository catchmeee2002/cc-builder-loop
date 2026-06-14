#!/usr/bin/env bash
# test-subagent-identity-clear-all-types.sh — V3.5 lock clear for all managed agent types
#
# Verifies subagent-lock-clear.sh correctly removes locks per agent_type.

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "subagent-identity-clear-all-types"

CLEAR_SCRIPT="$HARNESS_REPO_ROOT/scripts/subagent-lock-clear.sh"
assert_file_exists "lock-clear exists" "$CLEAR_SCRIPT"

TMPDIR="$(mktemp -d)"
_HARNESS_TMPDIRS+=("$TMPDIR")

LOCK_DIR="$TMPDIR/locks"
mkdir -p "$LOCK_DIR"

SESSION_ID="test-clear-$$"

write_lock() {
  local atype="$1"
  local lock_file="$LOCK_DIR/cc-subagent-${SESSION_ID}-${atype}.lock"
  cat > "$lock_file" <<EOF
agent_type: $atype
session_id: $SESSION_ID
start_ts: $(date +%s)
ttl_min: 30
EOF
}

make_stop() {
  printf '{"session_id":"%s","subagent_type":"%s"}' "$SESSION_ID" "$1"
}

# ---- Case 1: Clear each type individually ----
for atype in tester doc-maintainer arbiter reviewer; do
  section "Case: Clear $atype"
  write_lock tester
  write_lock doc-maintainer
  write_lock arbiter
  write_lock reviewer

  printf '%s' "$(make_stop "$atype")" | ISOLATION_LOCK_DIR="$LOCK_DIR" bash "$CLEAR_SCRIPT" >/dev/null 2>&1

  CLEARED="$LOCK_DIR/cc-subagent-${SESSION_ID}-${atype}.lock"
  assert_file_missing "$atype lock cleared" "$CLEARED"

  # Others should still exist
  for other in tester doc-maintainer arbiter reviewer; do
    [ "$other" = "$atype" ] && continue
    OTHER_LOCK="$LOCK_DIR/cc-subagent-${SESSION_ID}-${other}.lock"
    assert_file_exists "$other lock still exists after clearing $atype" "$OTHER_LOCK"
  done

  # Cleanup for next iteration
  rm -f "$LOCK_DIR"/cc-subagent-*.lock
done

# ---- Case 2: Fallback — empty agent_type clears all ----
section "Case: Empty agent_type fallback clears all"
write_lock tester
write_lock doc-maintainer

printf '%s' "$(printf '{"session_id":"%s"}' "$SESSION_ID")" | ISOLATION_LOCK_DIR="$LOCK_DIR" bash "$CLEAR_SCRIPT" >/dev/null 2>&1

assert_file_missing "tester cleared by fallback" "$LOCK_DIR/cc-subagent-${SESSION_ID}-tester.lock"
assert_file_missing "doc-maintainer cleared by fallback" "$LOCK_DIR/cc-subagent-${SESSION_ID}-doc-maintainer.lock"

harness_report
