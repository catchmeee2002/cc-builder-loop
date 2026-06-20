#!/usr/bin/env bash
# test-e2e-verification-stage.sh — fixture for stop hook e2e verification branch
source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "test-e2e-verification-stage"

# ---- 1. PASS + e2e_plan_path → e2e message injected, exit 2 ----
section "Case 1: e2e cases trigger verification request"
env1=$(create_test_env --slug "e2e-trigger" --worktree --phase active --wt-dirty "feature.txt")
wt1="$env1/.claude/worktrees/e2e-trigger"
sf1=$(state_file "$env1" "e2e-trigger")

mkdir -p "$env1/.claude/plans"
cat > "$env1/.claude/plans/test-plan.md" <<'EOF'
## Plan
<!-- e2e-cases -->
- 启动 app
- 检查首页
<!-- /e2e-cases -->
EOF
printf '\ne2e_plan_path: ".claude/plans/test-plan.md"\n' >> "$sf1"

result1=$(run_hook "$wt1")
assert_ec "e2e cases present → exit 2" "$result1" 2
assert_stderr_contains "injects e2e message" "$result1" "端到端验收用例"
assert_stderr_contains "includes case 1" "$result1" "启动 app"
assert_stderr_contains "includes case 2" "$result1" "检查首页"

# ---- 2. PASS + e2e_plan_path + verified HEAD → skip e2e ----
section "Case 2: verified HEAD skips e2e"
env2=$(create_test_env --slug "e2e-skip" --worktree --phase active --wt-dirty "feature.txt")
wt2="$env2/.claude/worktrees/e2e-skip"
sf2=$(state_file "$env2" "e2e-skip")

mkdir -p "$env2/.claude/plans"
cat > "$env2/.claude/plans/test-plan.md" <<'EOF'
<!-- e2e-cases -->
- 启动 app
<!-- /e2e-cases -->
EOF
CURRENT_HEAD2="$(git -C "$wt2" rev-parse HEAD)"
printf '\ne2e_plan_path: ".claude/plans/test-plan.md"\n' >> "$sf2"
printf 'e2e_verified_head: "%s"\n' "$CURRENT_HEAD2" >> "$sf2"

result2=$(run_hook "$wt2")
assert "verified HEAD → no e2e injection" "! grep -q '端到端验收用例' '$result2/stderr' 2>/dev/null"

# ---- 3. PASS + no e2e_plan_path → normal flow ----
section "Case 3: no e2e_plan_path → normal"
env3=$(create_test_env --slug "e2e-none" --worktree --phase active --wt-dirty "feature.txt")
wt3="$env3/.claude/worktrees/e2e-none"

result3=$(run_hook "$wt3")
assert "no e2e_plan_path → no e2e injection" "! grep -q '端到端验收用例' '$result3/stderr' 2>/dev/null"

# ---- 4. PASS + e2e_plan_path but plan file missing → skip ----
section "Case 4: missing plan file → skip"
env4=$(create_test_env --slug "e2e-nofile" --worktree --phase active --wt-dirty "feature.txt")
wt4="$env4/.claude/worktrees/e2e-nofile"
sf4=$(state_file "$env4" "e2e-nofile")
printf '\ne2e_plan_path: ".claude/plans/nonexistent.md"\n' >> "$sf4"

result4=$(run_hook "$wt4")
assert "missing plan file → no e2e injection" "! grep -q '端到端验收用例' '$result4/stderr' 2>/dev/null"

# ---- 5. PASS + e2e_plan_path but no e2e-cases tags → skip ----
section "Case 5: no tags in plan → skip"
env5=$(create_test_env --slug "e2e-notag" --worktree --phase active --wt-dirty "feature.txt")
wt5="$env5/.claude/worktrees/e2e-notag"
sf5=$(state_file "$env5" "e2e-notag")

mkdir -p "$env5/.claude/plans"
cat > "$env5/.claude/plans/test-plan.md" <<'EOF'
## Plan
No e2e cases here.
EOF
printf '\ne2e_plan_path: ".claude/plans/test-plan.md"\n' >> "$sf5"

result5=$(run_hook "$wt5")
assert "no tags → no e2e injection" "! grep -q '端到端验收用例' '$result5/stderr' 2>/dev/null"

harness_report
