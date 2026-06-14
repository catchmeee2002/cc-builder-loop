#!/usr/bin/env bash
# test-subagent-identity-concurrent-locks.sh — V3.5 per-agent-type lock isolation
#
# Verifies that tester and doc-maintainer locks don't overwrite each other.

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "subagent-identity-concurrent-locks"

GUARD_SCRIPT="$HARNESS_REPO_ROOT/scripts/subagent-start-guard.sh"
CLEAR_SCRIPT="$HARNESS_REPO_ROOT/scripts/subagent-lock-clear.sh"
assert_file_exists "start-guard exists" "$GUARD_SCRIPT"
assert_file_exists "lock-clear exists" "$CLEAR_SCRIPT"

TMPDIR="$(mktemp -d)"
_HARNESS_TMPDIRS+=("$TMPDIR")

LOCK_DIR="$TMPDIR/locks"
mkdir -p "$LOCK_DIR"

# Create a minimal project with state
PROJECT="$TMPDIR/project"
mkdir -p "$PROJECT/.claude/builder-loop/state" "$PROJECT/.claude/worktrees/test-slug" "$PROJECT/src"
cat > "$PROJECT/.claude/loop.yml" <<'YML'
pass_cmd: "true"
YML
cat > "$PROJECT/.claude/builder-loop/state/test-slug.yml" <<STATEEOF
phase: active
project_root: "$PROJECT"
main_repo_path: "$PROJECT"
worktree_path: "$PROJECT/.claude/worktrees/test-slug"
source_dirs: "src"
active: true
STATEEOF

SESSION_ID="test-concurrent-$$"

make_start_input() {
  printf '{"session_id":"%s","cwd":"%s","subagent_type":"%s"}' "$SESSION_ID" "$PROJECT/.claude/worktrees/test-slug" "$1"
}

make_stop_input() {
  printf '{"session_id":"%s","subagent_type":"%s"}' "$SESSION_ID" "$1"
}

# ---- Case 1: Two different agent types create separate lock files ----
section "Case 1: tester + doc-maintainer create separate locks"
printf '%s' "$(make_start_input tester)" | ISOLATION_LOCK_DIR="$LOCK_DIR" bash "$GUARD_SCRIPT" >/dev/null 2>&1
TESTER_LOCK="$LOCK_DIR/cc-subagent-${SESSION_ID}-tester.lock"
assert_file_exists "tester lock created" "$TESTER_LOCK"

printf '%s' "$(make_start_input doc-maintainer)" | ISOLATION_LOCK_DIR="$LOCK_DIR" bash "$GUARD_SCRIPT" >/dev/null 2>&1
DM_LOCK="$LOCK_DIR/cc-subagent-${SESSION_ID}-doc-maintainer.lock"
assert_file_exists "doc-maintainer lock created" "$DM_LOCK"
assert_file_exists "tester lock still exists after doc-maintainer start" "$TESTER_LOCK"

# ---- Case 2: Each lock has correct agent_type ----
section "Case 2: Lock content correctness"
TESTER_TYPE="$(grep '^agent_type:' "$TESTER_LOCK" | sed 's/agent_type: //')"
DM_TYPE="$(grep '^agent_type:' "$DM_LOCK" | sed 's/agent_type: //')"
assert "tester lock has correct type" "[ '$TESTER_TYPE' = 'tester' ]"
assert "dm lock has correct type" "[ '$DM_TYPE' = 'doc-maintainer' ]"

# ---- Case 3: Clearing tester lock doesn't affect doc-maintainer ----
section "Case 3: Selective lock clear"
printf '%s' "$(make_stop_input tester)" | ISOLATION_LOCK_DIR="$LOCK_DIR" bash "$CLEAR_SCRIPT" >/dev/null 2>&1
assert_file_missing "tester lock cleared" "$TESTER_LOCK"
assert_file_exists "doc-maintainer lock untouched" "$DM_LOCK"

# ---- Case 4: Clear doc-maintainer ----
section "Case 4: Clear remaining lock"
printf '%s' "$(make_stop_input doc-maintainer)" | ISOLATION_LOCK_DIR="$LOCK_DIR" bash "$CLEAR_SCRIPT" >/dev/null 2>&1
assert_file_missing "doc-maintainer lock cleared" "$DM_LOCK"

# ---- Case 5: No old-format lock created ----
section "Case 5: No legacy lock created"
OLD_LOCK="$LOCK_DIR/cc-subagent-${SESSION_ID}.lock"
assert_file_missing "no legacy lock" "$OLD_LOCK"

harness_report
