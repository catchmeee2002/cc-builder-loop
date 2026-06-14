#!/usr/bin/env bash
# test-subagent-identity-ttl-expiry.sh — V3.5 TTL expiry of per-agent-type locks
#
# Verifies that expired locks are ignored/removed by worktree-write-guard.

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "subagent-identity-ttl-expiry"

GUARD_SCRIPT="$HARNESS_REPO_ROOT/scripts/worktree-write-guard.sh"
assert_file_exists "write-guard exists" "$GUARD_SCRIPT"

TMPDIR="$(mktemp -d)"
_HARNESS_TMPDIRS+=("$TMPDIR")

LOCK_DIR="$TMPDIR/locks"
WORKTREE="$TMPDIR/worktree"
MAIN_REPO="$TMPDIR/main"
mkdir -p "$LOCK_DIR" "$WORKTREE/src" "$MAIN_REPO/.claude/builder-loop/state"

SESSION_ID="test-ttl-$$"

run_guard() {
  local ec=0
  printf '%s' "$1" | ISOLATION_LOCK_DIR="$LOCK_DIR" bash "$GUARD_SCRIPT" >/dev/null 2>/dev/null || ec=$?
  echo "$ec"
}

make_input() {
  printf '{"session_id":"%s","tool_name":"Write","tool_input":{"file_path":"%s"}}' "$SESSION_ID" "$1"
}

# ---- Case 1: Fresh lock → deny write to main repo ----
section "Case 1: Fresh tester lock denies main repo write"
LOCK_FILE="$LOCK_DIR/cc-subagent-${SESSION_ID}-tester.lock"
cat > "$LOCK_FILE" <<EOF
agent_type: tester
session_id: $SESSION_ID
project_root: "$WORKTREE"
main_repo_path: "$MAIN_REPO"
worktree_path: "$WORKTREE"
slug: "test-slug"
start_ts: $(date +%s)
ttl_min: 1
source_dirs_abs:
  - "$WORKTREE/src"
EOF

EC=$(run_guard "$(make_input "$MAIN_REPO/src/bad.py")")
assert "fresh lock → deny main repo write" "[ '$EC' -eq 2 ]"

# ---- Case 2: Expired lock → allow write (lock removed) ----
section "Case 2: Expired lock allows write"
EXPIRED_TS=$(( $(date +%s) - 120 ))
cat > "$LOCK_FILE" <<EOF
agent_type: tester
session_id: $SESSION_ID
project_root: "$WORKTREE"
main_repo_path: "$MAIN_REPO"
worktree_path: "$WORKTREE"
slug: "test-slug"
start_ts: $EXPIRED_TS
ttl_min: 1
source_dirs_abs:
  - "$WORKTREE/src"
EOF

EC=$(run_guard "$(make_input "$MAIN_REPO/src/ok.py")")
assert "expired lock → pass" "[ '$EC' -eq 0 ]"
assert_file_missing "expired lock removed" "$LOCK_FILE"

# ---- Case 3: Write to worktree always passes regardless of TTL ----
section "Case 3: Worktree write always passes"
cat > "$LOCK_FILE" <<EOF
agent_type: tester
session_id: $SESSION_ID
project_root: "$WORKTREE"
main_repo_path: "$MAIN_REPO"
worktree_path: "$WORKTREE"
slug: "test-slug"
start_ts: $(date +%s)
ttl_min: 1
source_dirs_abs: []
EOF

EC=$(run_guard "$(make_input "$WORKTREE/src/good.py")")
assert "worktree write → pass" "[ '$EC' -eq 0 ]"
rm -f "$LOCK_FILE"

harness_report
