#!/usr/bin/env bash
# test-handle-pass-agent-id.sh — handle-pass-result.sh agent_id 提取（去 idle 守卫后）
#
# Case 1: subagents.reviewer 有 agent_id + status=running → JSON 输出含 reviewer_agent_id
# Case 2: subagents.tester 有 agent_id + status=running → JSON 输出含 tester_agent_id
# Case 3: 无 subagents 段 → agent_id 均为空
# Case 4: subagents 段只有 agent_id 无 status → 仍输出 agent_id

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "test-handle-pass-agent-id"

HANDLE_PASS="${HARNESS_REPO_ROOT}/skills/builder-loop/scripts/handle-pass-result.sh"

# ---- Helper: 构造环境，跑 handle-pass-result.sh 到 pass 路径，提取 JSON ----
run_handle_pass() {
  local dir="$1"
  local slug="$2"
  local state_file="$dir/.claude/builder-loop/state/${slug}.yml"
  local run_cwd="$dir"
  local project_root="$dir"

  local handle
  handle="$(mktemp -d -t harness-result-XXXXXX)"
  _HARNESS_TMPDIRS+=("$handle")

  local ec=0
  bash "$HANDLE_PASS" "$state_file" "1" "$run_cwd" "$project_root" \
    >"$handle/stdout" 2>"$handle/stderr" || ec=$?
  echo "$ec" > "$handle/ec"
  echo "$handle"
}

# ---- Case 1: reviewer agent_id + status=running → pass JSON 含 reviewer_agent_id ----
echo "--- Case 1: reviewer agent_id with status=running ---"
env1=$(create_test_env --slug "aid-reviewer" --pass-cmd "true")
state1="$env1/.claude/builder-loop/state/aid-reviewer.yml"
# 追加 subagents 段
cat >> "$state1" <<'SUBEOF'
subagents:
  reviewer:
    agent_id: "rev-abc123"
    status: "running"
SUBEOF
# 在主仓创建一个新 commit（让 PASS_START_HEAD != HEAD，避免 NOOP）
(cd "$env1" && echo "change1" > file1.txt && git add -A && git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Change 1")
r1=$(run_handle_pass "$env1" "aid-reviewer")
assert_ec "Case1 handle-pass exit 0" "$r1" 0
last1=$(result_last "$r1")
assert "Case1 reviewer_agent_id=rev-abc123" "echo '$last1' | python3 -c \"import json,sys; d=json.load(sys.stdin); assert d['reviewer_agent_id']=='rev-abc123', d\""

# ---- Case 2: tester agent_id + status=running → e2e_needed JSON 含 tester_agent_id ----
echo "--- Case 2: tester agent_id with status=running ---"
env2=$(create_test_env --slug "aid-tester" --pass-cmd "true")
state2="$env2/.claude/builder-loop/state/aid-tester.yml"
# 写 plan_path + plan 文件含 e2e-cases
echo 'plan_path: "test-plan.md"' >> "$state2"
cat > "$env2/test-plan.md" <<'PLANEOF'
<!-- e2e-cases -->
- id: test-case-1
  input: "hello"
  hard_rules: {}
  judge:
    verify: "responds"
    quality: "ok"
  level: full
<!-- /e2e-cases -->
PLANEOF
# 追加 subagents.tester 段
cat >> "$state2" <<'SUBEOF'
subagents:
  tester:
    agent_id: "tst-xyz789"
    status: "running"
SUBEOF
(cd "$env2" && echo "change2" > file2.txt && git add -A && git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Change 2")
# 需要 extract-e2e-cases.sh 能提取出 cases
r2=$(run_handle_pass "$env2" "aid-tester")
assert_ec "Case2 handle-pass exit 2 (e2e_needed)" "$r2" 2
last2=$(result_last "$r2")
assert "Case2 tester_agent_id=tst-xyz789" "echo '$last2' | python3 -c \"import json,sys; d=json.load(sys.stdin); assert d['tester_agent_id']=='tst-xyz789', d\""

# ---- Case 3: 无 subagents 段 → agent_id 为空 ----
echo "--- Case 3: no subagents section ---"
env3=$(create_test_env --slug "aid-none" --pass-cmd "true")
(cd "$env3" && echo "change3" > file3.txt && git add -A && git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Change 3")
r3=$(run_handle_pass "$env3" "aid-none")
assert_ec "Case3 handle-pass exit 0" "$r3" 0
last3=$(result_last "$r3")
assert "Case3 reviewer_agent_id empty" "echo '$last3' | python3 -c \"import json,sys; d=json.load(sys.stdin); assert d['reviewer_agent_id']=='', d\""

# ---- Case 4: agent_id 存在但无 status 字段 → 仍输出 agent_id ----
echo "--- Case 4: agent_id without status field ---"
env4=$(create_test_env --slug "aid-nostatus" --pass-cmd "true")
state4="$env4/.claude/builder-loop/state/aid-nostatus.yml"
cat >> "$state4" <<'SUBEOF'
subagents:
  reviewer:
    agent_id: "rev-no-status"
SUBEOF
(cd "$env4" && echo "change4" > file4.txt && git add -A && git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Change 4")
r4=$(run_handle_pass "$env4" "aid-nostatus")
assert_ec "Case4 handle-pass exit 0" "$r4" 0
last4=$(result_last "$r4")
assert "Case4 reviewer_agent_id=rev-no-status" "echo '$last4' | python3 -c \"import json,sys; d=json.load(sys.stdin); assert d['reviewer_agent_id']=='rev-no-status', d\""

harness_report
