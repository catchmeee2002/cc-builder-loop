#!/usr/bin/env bash
# test-subagent-identity.sh — V4.3 subagent identity tracking e2e fixture

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "test-subagent-identity"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
START_GUARD="${REPO_ROOT}/scripts/subagent-start-guard.sh"
LOCK_CLEAR="${REPO_ROOT}/scripts/subagent-lock-clear.sh"

# ---- Case 1: SubagentStart tester writes state ----
section "Case 1: SubagentStart tester writes state"
env1=$(create_test_env --slug "ident-1" --phase active)
state1="$(state_file "$env1" "ident-1")"

printf '{"session_id":"sess-1","cwd":"%s","subagent_type":"tester","agent_id":"test-id-123"}' "$env1" \
  | bash "$START_GUARD" >/dev/null 2>&1 || true

assert_contains "tester agent_id written" "$state1" 'agent_id: "test-id-123"'
assert_contains "tester status=running" "$state1" 'status: "running"'
assert_contains "subagents block created" "$state1" '^subagents:'

# ---- Case 2: SubagentStop tester updates status to idle ----
section "Case 2: SubagentStop tester updates status"

printf '{"session_id":"sess-1","cwd":"%s","subagent_type":"tester","agent_id":"test-id-123","agent_transcript_path":"/tmp/transcript.jsonl"}' "$env1" \
  | bash "$LOCK_CLEAR" >/dev/null 2>&1 || true

assert_contains "tester status=idle" "$state1" 'status: "idle"'
assert_contains "transcript_path written" "$state1" 'transcript_path: "/tmp/transcript.jsonl"'

# ---- Case 3: SubagentStart reviewer writes state (tester preserved) ----
section "Case 3: SubagentStart reviewer writes state"

printf '{"session_id":"sess-1","cwd":"%s","subagent_type":"reviewer","agent_id":"rev-id-456"}' "$env1" \
  | bash "$START_GUARD" >/dev/null 2>&1 || true

assert_contains "reviewer agent_id written" "$state1" 'agent_id: "rev-id-456"'
assert_contains "tester entry preserved" "$state1" 'agent_id: "test-id-123"'

# ---- Case 4: Second SubagentStart same type overwrites ----
section "Case 4: SubagentStart overwrite same type"

printf '{"session_id":"sess-1","cwd":"%s","subagent_type":"tester","agent_id":"test-id-789"}' "$env1" \
  | bash "$START_GUARD" >/dev/null 2>&1 || true

assert_contains "new tester agent_id" "$state1" 'agent_id: "test-id-789"'
assert "old tester id gone" "! grep -q 'test-id-123' '$state1'"

# ---- Case 5: Non-tracked type does NOT write subagents ----
section "Case 5: doc-maintainer NOT tracked"
env5=$(create_test_env --slug "ident-5" --phase active)
state5="$(state_file "$env5" "ident-5")"

printf '{"session_id":"sess-5","cwd":"%s","subagent_type":"doc-maintainer","agent_id":"doc-id-999"}' "$env5" \
  | bash "$START_GUARD" >/dev/null 2>&1 || true

assert "doc-maintainer not in state" "! grep -q 'doc-id-999' '$state5'"

# ---- Case 6: e2e inject with tester agent_id (resume path) ----
section "Case 6: e2e inject contains tester_agent_id"
env6=$(create_test_env --slug "ident-6" --phase active --pass-cmd "true" --dirty "change.txt")
state6="$(state_file "$env6" "ident-6")"

# Create a plan with e2e-cases tag
mkdir -p "$env6/.claude/plans"
cat > "$env6/.claude/plans/test-plan.md" <<'PLANEOF'
# Test plan
<!-- e2e-cases -->
### Case A: test something
- 断言：something works
<!-- /e2e-cases -->
PLANEOF
# Register plan_path + inject tester identity (idle)
python3 -c "
sf = '$state6'
text = open(sf).read()
text += 'plan_path: \".claude/plans/test-plan.md\"\n'
text += 'subagents:\n'
text += '  tester:\n'
text += '    agent_id: \"resume-tester-42\"\n'
text += '    started_at: \"2026-06-24T00:00:00\"\n'
text += '    status: \"idle\"\n'
text += '    transcript_path: \"\"\n'
open(sf, 'w').write(text)
"

result6=$(run_hook "$env6")
assert_stderr_contains "inject has SendMessage" "$result6" "SendMessage"
assert_stderr_contains "inject has tester_agent_id" "$result6" "resume-tester-42"

# ---- Case 7: e2e inject without agent_id (first spawn path) ----
section "Case 7: e2e inject without tester identity"
env7=$(create_test_env --slug "ident-7" --phase active --pass-cmd "true" --dirty "change.txt")
state7="$(state_file "$env7" "ident-7")"

mkdir -p "$env7/.claude/plans"
cat > "$env7/.claude/plans/test-plan.md" <<'PLANEOF'
# Test plan
<!-- e2e-cases -->
### Case A: test something
- 断言：something works
<!-- /e2e-cases -->
PLANEOF
python3 -c "
sf = '$state7'
text = open(sf).read()
text += 'plan_path: \".claude/plans/test-plan.md\"\n'
open(sf, 'w').write(text)
"

result7=$(run_hook "$env7")
assert_stderr_contains "inject says spawn" "$result7" "spawn tester"
assert "inject has no SendMessage" "! grep -q 'SendMessage' '$result7/stderr'"

# ---- Case 8: PASS message contains reviewer_agent_id ----
section "Case 8: PASS message with reviewer_agent_id"
env8=$(create_test_env --slug "ident-8" --phase active --pass-cmd "true" --dirty "change.txt")
state8="$(state_file "$env8" "ident-8")"

# Inject reviewer identity (idle), no plan_path (skip e2e)
python3 -c "
sf = '$state8'
text = open(sf).read()
text += 'subagents:\n'
text += '  reviewer:\n'
text += '    agent_id: \"rev-id-456\"\n'
text += '    started_at: \"2026-06-24T00:00:00\"\n'
text += '    status: \"idle\"\n'
text += '    transcript_path: \"\"\n'
open(sf, 'w').write(text)
"

result8=$(run_hook "$env8")
assert_stderr_contains "PASS has reviewer_agent_id" "$result8" "reviewer_agent_id=rev-id-456"

harness_report
