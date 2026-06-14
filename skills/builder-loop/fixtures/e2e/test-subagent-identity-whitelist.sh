#!/usr/bin/env bash
# test-subagent-identity-whitelist.sh — V3.5 agent type whitelist + active state dual-condition
#
# Verifies that non-builder-loop agents (workflow, Explore) don't get lock files,
# and that whitelisted agents without active state also don't get locks.

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "subagent-identity-whitelist"

GUARD_SCRIPT="$HARNESS_REPO_ROOT/scripts/subagent-start-guard.sh"
assert_file_exists "start-guard exists" "$GUARD_SCRIPT"

TMPDIR="$(mktemp -d)"
_HARNESS_TMPDIRS+=("$TMPDIR")

LOCK_DIR="$TMPDIR/locks"
mkdir -p "$LOCK_DIR"

# Project with active state
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

SESSION_ID="test-whitelist-$$"
WT_CWD="$PROJECT/.claude/worktrees/test-slug"

make_input() {
  printf '{"session_id":"%s","cwd":"%s","subagent_type":"%s"}' "$SESSION_ID" "$WT_CWD" "$1"
}

# ---- Case 1: Non-whitelist agents don't produce locks ----
section "Case 1: workflow agent → no lock"
printf '%s' "$(make_input Explore)" | ISOLATION_LOCK_DIR="$LOCK_DIR" bash "$GUARD_SCRIPT" >/dev/null 2>&1
EXPLORE_LOCK="$LOCK_DIR/cc-subagent-${SESSION_ID}-Explore.lock"
assert_file_missing "Explore lock not created" "$EXPLORE_LOCK"

section "Case 2: general-purpose agent → no lock"
printf '%s' "$(make_input general-purpose)" | ISOLATION_LOCK_DIR="$LOCK_DIR" bash "$GUARD_SCRIPT" >/dev/null 2>&1
GP_LOCK="$LOCK_DIR/cc-subagent-${SESSION_ID}-general-purpose.lock"
assert_file_missing "general-purpose lock not created" "$GP_LOCK"

section "Case 3: claude agent → no lock"
printf '%s' "$(make_input claude)" | ISOLATION_LOCK_DIR="$LOCK_DIR" bash "$GUARD_SCRIPT" >/dev/null 2>&1
CL_LOCK="$LOCK_DIR/cc-subagent-${SESSION_ID}-claude.lock"
assert_file_missing "claude lock not created" "$CL_LOCK"

# ---- Case 4: Whitelisted agent with active state → lock created ----
section "Case 4: tester + active state → lock created"
printf '%s' "$(make_input tester)" | ISOLATION_LOCK_DIR="$LOCK_DIR" bash "$GUARD_SCRIPT" >/dev/null 2>&1
T_LOCK="$LOCK_DIR/cc-subagent-${SESSION_ID}-tester.lock"
assert_file_exists "tester lock created with active state" "$T_LOCK"
rm -f "$T_LOCK"

# ---- Case 5: Whitelisted agent without state → no lock (dual-condition) ----
section "Case 5: tester + no state → no lock"
SESSION_ID2="test-whitelist-nostate-$$"
# Use a CWD that has no state file
printf '%s' "$(printf '{"session_id":"%s","cwd":"/tmp","subagent_type":"tester"}' "$SESSION_ID2")" | ISOLATION_LOCK_DIR="$LOCK_DIR" bash "$GUARD_SCRIPT" >/dev/null 2>&1
NS_LOCK="$LOCK_DIR/cc-subagent-${SESSION_ID2}-tester.lock"
assert_file_missing "tester lock not created without state" "$NS_LOCK"

# ---- Case 6: No glob noise ----
section "Case 6: No stray lock files"
STRAY_COUNT="$(find "$LOCK_DIR" -maxdepth 1 -name 'cc-subagent-*.lock' 2>/dev/null | wc -l)"
assert "no stray locks" "[ '$STRAY_COUNT' -eq 0 ]"

harness_report
