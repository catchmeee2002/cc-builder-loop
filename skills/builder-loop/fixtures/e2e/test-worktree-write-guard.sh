#!/usr/bin/env bash
# test-worktree-write-guard.sh — e2e fixture for worktree-write-guard.sh
#
# Tests the V3.1 unified write boundary guard:
# - Subagent strict mode (tester, doc-maintainer)
# - Background agent passthrough (reviewer)
# - Whitelist paths (/tmp, review_reports, reviewer-diff, state, pause)
# - TTL expiry, bare mode, builder mode (no lock)

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "worktree-write-guard"

GUARD_SCRIPT="$HARNESS_REPO_ROOT/scripts/worktree-write-guard.sh"
assert_file_exists "guard script exists" "$GUARD_SCRIPT"

TMPDIR="$(mktemp -d)"
_HARNESS_TMPDIRS+=("$TMPDIR")

WORKTREE="$TMPDIR/worktree"
MAIN_REPO="$TMPDIR/main"
mkdir -p "$WORKTREE/src" "$MAIN_REPO/.claude/builder-loop/state" "$MAIN_REPO/.claude/review_reports"

SESSION_ID="test-guard-$$"
LOCK_DIR="$TMPDIR/locks"
mkdir -p "$LOCK_DIR"
LOCK_FILE="$LOCK_DIR/cc-subagent-${SESSION_ID}.lock"

run_guard() {
  local ec=0
  printf '%s' "$1" | ISOLATION_LOCK_DIR="$LOCK_DIR" bash "$GUARD_SCRIPT" >/dev/null 2>/dev/null || ec=$?
  echo "$ec"
}

write_lock() {
  local agent_type="$1"
  cat > "$LOCK_FILE" <<EOF
agent_type: $agent_type
session_id: $SESSION_ID
project_root: "$WORKTREE"
main_repo_path: "$MAIN_REPO"
worktree_path: "$WORKTREE"
slug: "test-slug"
start_ts: $(date +%s)
ttl_min: 30
source_dirs_abs:
  - "$WORKTREE/src"
EOF
}

make_input() {
  printf '{"session_id":"%s","tool_name":"%s","tool_input":{"file_path":"%s"}}' "$SESSION_ID" "$1" "$2"
}

section "Case 1: No lock → pass"
rm -f "$LOCK_FILE"
EC=$(run_guard "$(make_input Write /some/random/path.txt)")
assert "no lock → pass" "[ '$EC' -eq 0 ]"

section "Case 2-3: Tester in/out worktree"
write_lock "tester"
EC=$(run_guard "$(make_input Write "$WORKTREE/src/foo.py")")
assert "tester inside worktree → pass" "[ '$EC' -eq 0 ]"
EC=$(run_guard "$(make_input Edit "$MAIN_REPO/src/bar.py")")
assert "tester outside worktree → deny" "[ '$EC' -eq 2 ]"

section "Case 4-8: Tester whitelist paths"
write_lock "tester"
EC=$(run_guard "$(make_input Write /tmp/some-temp.txt)")
assert "tester /tmp → pass" "[ '$EC' -eq 0 ]"
EC=$(run_guard "$(make_input Write "$MAIN_REPO/.claude/review_reports/report.md")")
assert "tester review_reports → pass" "[ '$EC' -eq 0 ]"
EC=$(run_guard "$(make_input Write "$MAIN_REPO/.claude/reviewer-diff-some-slug.txt")")
assert "tester reviewer-diff → pass" "[ '$EC' -eq 0 ]"
EC=$(run_guard "$(make_input Write "$MAIN_REPO/.claude/builder-loop/state/test.yml")")
assert "tester state file → pass" "[ '$EC' -eq 0 ]"
EC=$(run_guard "$(make_input Write "$MAIN_REPO/.claude/builder-loop/test-slug.pause")")
assert "tester pause file → pass" "[ '$EC' -eq 0 ]"

section "Case 9: Reviewer (background agent) → pass"
write_lock "reviewer"
EC=$(run_guard "$(make_input Write "$MAIN_REPO/outside.txt")")
assert "reviewer outside → pass (background)" "[ '$EC' -eq 0 ]"

section "Case 10-11: Sync agents → deny"
write_lock "doc-maintainer"
EC=$(run_guard "$(make_input Write "$MAIN_REPO/docs/readme.md")")
assert "doc-maintainer outside → deny" "[ '$EC' -eq 2 ]"
write_lock "arbiter"
EC=$(run_guard "$(make_input Edit "$MAIN_REPO/conflict.txt")")
assert "arbiter outside → deny" "[ '$EC' -eq 2 ]"

section "Case 12: Expired lock → pass"
cat > "$LOCK_FILE" <<EOF
agent_type: tester
session_id: $SESSION_ID
worktree_path: "$WORKTREE"
main_repo_path: "$MAIN_REPO"
start_ts: 1000000000
ttl_min: 1
source_dirs_abs: []
EOF
EC=$(run_guard "$(make_input Write "$MAIN_REPO/outside.txt")")
assert "expired lock → pass" "[ '$EC' -eq 0 ]"

section "Case 13: Bare mode → pass"
cat > "$LOCK_FILE" <<EOF
agent_type: tester
session_id: $SESSION_ID
worktree_path: ""
main_repo_path: "$MAIN_REPO"
start_ts: $(date +%s)
ttl_min: 30
source_dirs_abs: []
EOF
EC=$(run_guard "$(make_input Write "$MAIN_REPO/outside.txt")")
assert "bare mode → pass" "[ '$EC' -eq 0 ]"

section "Case 14-15: Edge cases"
rm -f "$LOCK_FILE"
EC=$(run_guard '{"session_id":"","tool_name":"Write","tool_input":{"file_path":"/x"}}')
assert "empty session_id → pass" "[ '$EC' -eq 0 ]"
write_lock "tester"
EC=$(run_guard "{\"session_id\":\"$SESSION_ID\",\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"\"}}")
assert "empty file_path → pass" "[ '$EC' -eq 0 ]"

harness_report
