#!/usr/bin/env bash
# test-worktree-write-guard.sh — e2e fixture for worktree-write-guard.sh
#
# Tests the V3.1 unified write boundary guard:
# - Subagent strict mode (tester, doc-maintainer)
# - Background agent passthrough (reviewer)
# - Whitelist paths (/tmp, review_reports, reviewer-diff, state, pause)
# - TTL expiry
# - Bare mode (empty worktree_path)
# - Builder mode (no lock → always pass)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
GUARD_SCRIPT="$REPO_ROOT/scripts/worktree-write-guard.sh"

[ -f "$GUARD_SCRIPT" ] || { echo "FATAL: guard script not found: $GUARD_SCRIPT" >&2; exit 1; }

PASS=0; FAIL=0; TOTAL=0
assert_exit() {
  local desc="$1" expected="$2" actual="$3"
  TOTAL=$((TOTAL + 1))
  if [ "$actual" -eq "$expected" ]; then
    PASS=$((PASS + 1))
    echo "  ✅ $desc (exit=$actual)"
  else
    FAIL=$((FAIL + 1))
    echo "  ❌ $desc (expected=$expected got=$actual)"
  fi
}

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

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
  local tool="$1" path="$2"
  printf '{"session_id":"%s","tool_name":"%s","tool_input":{"file_path":"%s"}}' "$SESSION_ID" "$tool" "$path"
}

echo "=== worktree-write-guard.sh fixture ==="

# ---- Case 1: No lock, no state → pass ----
echo "Case 1: No lock, no state"
rm -f "$LOCK_FILE"
EC=$(run_guard "$(make_input Write /some/random/path.txt)")
assert_exit "no lock no state → pass" 0 "$EC"

# ---- Case 2: Tester lock, file in worktree → pass ----
echo "Case 2: Tester in worktree"
write_lock "tester"
EC=$(run_guard "$(make_input Write "$WORKTREE/src/foo.py")")
assert_exit "tester inside worktree → pass" 0 "$EC"

# ---- Case 3: Tester lock, file outside worktree → deny ----
echo "Case 3: Tester outside worktree"
write_lock "tester"
EC=$(run_guard "$(make_input Edit "$MAIN_REPO/src/bar.py")")
assert_exit "tester outside worktree → deny" 2 "$EC"

# ---- Case 4: Tester lock, /tmp → pass ----
echo "Case 4: Tester writes /tmp"
write_lock "tester"
EC=$(run_guard "$(make_input Write /tmp/some-temp.txt)")
assert_exit "tester /tmp → pass" 0 "$EC"

# ---- Case 5: Tester lock, whitelist review_reports → pass ----
echo "Case 5: Tester whitelist review_reports"
write_lock "tester"
EC=$(run_guard "$(make_input Write "$MAIN_REPO/.claude/review_reports/report.md")")
assert_exit "tester review_reports → pass" 0 "$EC"

# ---- Case 6: Tester lock, whitelist reviewer-diff → pass ----
echo "Case 6: Tester whitelist reviewer-diff"
write_lock "tester"
EC=$(run_guard "$(make_input Write "$MAIN_REPO/.claude/reviewer-diff-some-slug.txt")")
assert_exit "tester reviewer-diff → pass" 0 "$EC"

# ---- Case 7: Tester lock, whitelist state file → pass ----
echo "Case 7: Tester whitelist state"
write_lock "tester"
EC=$(run_guard "$(make_input Write "$MAIN_REPO/.claude/builder-loop/state/test.yml")")
assert_exit "tester state file → pass" 0 "$EC"

# ---- Case 8: Tester lock, whitelist pause file → pass ----
echo "Case 8: Tester whitelist pause"
write_lock "tester"
EC=$(run_guard "$(make_input Write "$MAIN_REPO/.claude/builder-loop/test-slug.pause")")
assert_exit "tester pause file → pass" 0 "$EC"

# ---- Case 9: Reviewer lock (background agent) → pass ----
echo "Case 9: Reviewer lock, outside worktree"
write_lock "reviewer"
EC=$(run_guard "$(make_input Write "$MAIN_REPO/outside.txt")")
assert_exit "reviewer outside → pass (background)" 0 "$EC"

# ---- Case 10: Doc-maintainer lock (sync agent) → deny ----
echo "Case 10: Doc-maintainer outside worktree"
write_lock "doc-maintainer"
EC=$(run_guard "$(make_input Write "$MAIN_REPO/docs/readme.md")")
assert_exit "doc-maintainer outside → deny" 2 "$EC"

# ---- Case 11: Arbiter lock (sync agent) → deny ----
echo "Case 11: Arbiter outside worktree"
write_lock "arbiter"
EC=$(run_guard "$(make_input Edit "$MAIN_REPO/conflict.txt")")
assert_exit "arbiter outside → deny" 2 "$EC"

# ---- Case 12: Expired lock → pass ----
echo "Case 12: Expired lock"
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
assert_exit "expired lock → pass" 0 "$EC"

# ---- Case 13: Bare mode (empty worktree_path) → pass ----
echo "Case 13: Bare mode"
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
assert_exit "bare mode → pass" 0 "$EC"

# ---- Case 14: Empty session_id → pass ----
echo "Case 14: Empty session_id"
rm -f "$LOCK_FILE"
EC=$(run_guard '{"session_id":"","tool_name":"Write","tool_input":{"file_path":"/x"}}')
assert_exit "empty session_id → pass" 0 "$EC"

# ---- Case 15: Empty file_path → pass ----
echo "Case 15: Empty file_path"
write_lock "tester"
EC=$(run_guard '{"session_id":"'"$SESSION_ID"'","tool_name":"Write","tool_input":{"file_path":""}}')
assert_exit "empty file_path → pass" 0 "$EC"

echo ""
echo "=== worktree-write-guard: $PASS/$TOTAL passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
