#!/usr/bin/env bash
# test-subagent-identity-old-lock-compat.sh — V3.5 backward compat with V3.4 lock format
#
# Verifies that old-format locks (cc-subagent-{sid}.lock) are still handled by
# worktree-write-guard.sh and tester-lock-check.sh.

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "subagent-identity-old-lock-compat"

GUARD_SCRIPT="$HARNESS_REPO_ROOT/scripts/worktree-write-guard.sh"
ISO_SCRIPT="$HARNESS_REPO_ROOT/scripts/tester-lock-check.sh"
CLEAR_SCRIPT="$HARNESS_REPO_ROOT/scripts/subagent-lock-clear.sh"
assert_file_exists "write-guard exists" "$GUARD_SCRIPT"
assert_file_exists "tester-lock-check exists" "$ISO_SCRIPT"
assert_file_exists "lock-clear exists" "$CLEAR_SCRIPT"

TMPDIR="$(mktemp -d)"
_HARNESS_TMPDIRS+=("$TMPDIR")

LOCK_DIR="$TMPDIR/locks"
WORKTREE="$TMPDIR/worktree"
MAIN_REPO="$TMPDIR/main"
mkdir -p "$LOCK_DIR" "$WORKTREE/src" "$MAIN_REPO/.claude/builder-loop/state" "$TMPDIR/project/src" "$TMPDIR/project/tests"

SESSION_ID="test-compat-$$"

# Write old-format lock (V3.4 style)
LEGACY_LOCK="$LOCK_DIR/cc-subagent-${SESSION_ID}.lock"
cat > "$LEGACY_LOCK" <<EOF
agent_type: tester
session_id: $SESSION_ID
project_root: "$WORKTREE"
main_repo_path: "$MAIN_REPO"
worktree_path: "$WORKTREE"
slug: "test-slug"
start_ts: $(date +%s)
ttl_min: 30
source_dirs_abs:
  - "$TMPDIR/project/src"
EOF

# ---- Case 1: worktree-write-guard still enforces old lock ----
section "Case 1: Old lock triggers write guard"
run_guard() {
  local ec=0
  printf '%s' "$1" | ISOLATION_LOCK_DIR="$LOCK_DIR" bash "$GUARD_SCRIPT" >/dev/null 2>/dev/null || ec=$?
  echo "$ec"
}
make_write_input() {
  printf '{"session_id":"%s","tool_name":"Write","tool_input":{"file_path":"%s"}}' "$SESSION_ID" "$1"
}

EC=$(run_guard "$(make_write_input "$MAIN_REPO/src/bad.py")")
assert "old lock → deny main repo write" "[ '$EC' -eq 2 ]"

EC=$(run_guard "$(make_write_input "$WORKTREE/src/ok.py")")
assert "old lock → allow worktree write" "[ '$EC' -eq 0 ]"

# ---- Case 2: tester-lock-check still reads old lock ----
section "Case 2: Old lock triggers read isolation"
run_iso() {
  local ec=0
  local input="{\"session_id\":\"${SESSION_ID}\",\"tool_name\":\"Read\",\"tool_input\":{\"file_path\":\"$1\"}}"
  printf '%s' "$input" | ISOLATION_LOCK_DIR="$LOCK_DIR" bash "$ISO_SCRIPT" >/dev/null 2>&1 || ec=$?
  echo "$ec"
}

EC=$(run_iso "$TMPDIR/project/src/foo.py")
assert "old lock → deny source read" "[ '$EC' -eq 2 ]"

EC=$(run_iso "$TMPDIR/project/tests/test_foo.py")
assert "old lock → allow test read" "[ '$EC' -eq 0 ]"

# ---- Case 3: subagent-lock-clear cleans legacy lock when type matches ----
section "Case 3: Clear script handles legacy lock"
printf '%s' "$(printf '{"session_id":"%s","subagent_type":"tester"}' "$SESSION_ID")" | ISOLATION_LOCK_DIR="$LOCK_DIR" bash "$CLEAR_SCRIPT" >/dev/null 2>&1
assert_file_missing "legacy lock cleaned by clear script" "$LEGACY_LOCK"

harness_report
