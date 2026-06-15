#!/usr/bin/env bash
# test-cross-session-isolation.sh — V3.4 CWD→state 并发隔离
#
# 验证 locate-state.sh 的 CWD 匹配让多 session 互不串扰：
#   Case A: CWD=worktree-A → hook 只影响 state-A
#   Case B: CWD=worktree-B → hook 只影响 state-B（A 不被串扰）
#   Case C: CWD=主仓且两个 active → hook 不绑定任何一个（exit 0）
#   Case D: CWD=主仓且恰好 1 个 active → 策略 5 自动绑定
#   Case E: 旧 local.md 残留 → hook 忽略它，仍走 CWD 匹配

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "cross-session-isolation"

SLUG_A="cs-fixture-a"
SLUG_B="cs-fixture-b"

TMP=$(mktemp -d -t "harness-cross-XXXXXX")
_HARNESS_TMPDIRS+=("$TMP")
(
  cd "$TMP"
  git init -q
  git config user.email "harness@test.local"
  git config user.name "harness"
  mkdir -p .claude
  cat > .claude/loop.yml <<'YMLEOF'
pass_cmd:
  - stage: smoke
    cmd: "true"
    timeout: 10
worktree:
  enabled: false
YMLEOF
  echo "seed" > README.md
  cat > .gitignore <<'GIEOF'
.claude/builder-loop/
.claude/loop-runs/
GIEOF
  git add -A
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Cross-session seed"
)

HEAD1=$(git -C "$TMP" rev-parse --short HEAD)
WT_A="$TMP/.claude/worktrees/$SLUG_A"
WT_B="$TMP/.claude/worktrees/$SLUG_B"
git -C "$TMP" worktree add -q "$WT_A" 2>/dev/null
git -C "$TMP" worktree add -q "$WT_B" 2>/dev/null

echo "feature-a" > "$WT_A/feature.txt"
echo "feature-b" > "$WT_B/feature.txt"

write_cs_state() {
  local slug="$1" wt="$2"
  mkdir -p "$TMP/.claude/builder-loop/state"
  cat > "$TMP/.claude/builder-loop/state/$slug.yml" <<STEOF
active: true
phase: "active"
slug: "$slug"
owner_cwd: "$TMP"
iter: 0
max_iter: 5
project_root: "$wt"
main_repo_path: "$TMP"
start_head: "$HEAD1"
last_iter_head: "$HEAD1"
worktree_path: "$wt"
worktree_mode: "clean"
task_description: |
  cross-session-test-$slug
source_dirs: ""
test_dirs: ""
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
stopped_reason: ""
cleanup_phase: ""
created_at: "2026-05-09T00:00:00+08:00"
STEOF
}

write_cs_state "$SLUG_A" "$WT_A"
write_cs_state "$SLUG_B" "$WT_B"

STATE_A="$TMP/.claude/builder-loop/state/$SLUG_A.yml"
STATE_B="$TMP/.claude/builder-loop/state/$SLUG_B.yml"

# ============================================================
# Case A: CWD=worktree-A → 只 state-A 被处理
# ============================================================
section "Case A: CWD=worktree-A → 只 state-A 被处理"
resultA=$(run_hook "$WT_A")
phaseA=$(read_state_field "$STATE_A" "phase")
phaseBafterA=$(read_state_field "$STATE_B" "phase")

assert_ec "Case A hook EC=2（PASS 续接）" "$resultA" 2
assert "Case A state-A.phase = passed_pending_review" "[ '$phaseA' = 'passed_pending_review' ]"
assert "Case A state-B.phase 仍为 active（未被串扰）" "[ '$phaseBafterA' = 'active' ]"

# ============================================================
# Case B: CWD=worktree-B → 只 state-B 被处理
# ============================================================
section "Case B: CWD=worktree-B → 只 state-B 被处理"
resultB=$(run_hook "$WT_B")
phaseAafterB=$(read_state_field "$STATE_A" "phase")
phaseB=$(read_state_field "$STATE_B" "phase")

assert_ec "Case B hook EC=2" "$resultB" 2
assert "Case B state-B.phase = passed_pending_review" "[ '$phaseB' = 'passed_pending_review' ]"
assert "Case B state-A 未被串扰" "[ '$phaseAafterB' = 'passed_pending_review' ]"

# ============================================================
# Case C: CWD=主仓且两个 active → hook 不绑定（exit 0）
# ============================================================
section "Case C: CWD=主仓且两个 active → exit 0"
write_cs_state "$SLUG_A" "$WT_A"
write_cs_state "$SLUG_B" "$WT_B"

resultC=$(run_hook "$TMP")
phaseAC=$(read_state_field "$STATE_A" "phase")
phaseBC=$(read_state_field "$STATE_B" "phase")

assert_ec "Case C hook EC=0（主仓+多 active 不绑定）" "$resultC" 0
assert "Case C state-A 仍为 active" "[ '$phaseAC' = 'active' ]"
assert "Case C state-B 仍为 active" "[ '$phaseBC' = 'active' ]"

# ============================================================
# Case D: CWD=主仓且恰好 1 个 active → 策略 5 自动绑定
# ============================================================
section "Case D: CWD=主仓且 1 个 active → 策略 5 绑定"
rm -f "$STATE_B"

resultD=$(run_hook "$TMP")
phaseAD=$(read_state_field "$STATE_A" "phase")

assert_ec "Case D hook EC=2（唯一 active 自动绑定）" "$resultD" 2
assert "Case D state-A.phase = passed_pending_review" "[ '$phaseAD' = 'passed_pending_review' ]"

# ============================================================
# Case E: 旧 local.md 残留 → hook 忽略它，仍走 CWD 匹配
# ============================================================
section "Case E: 旧 local.md 残留 → 被忽略"
write_cs_state "$SLUG_A" "$WT_A"
write_cs_state "$SLUG_B" "$WT_B"

cat > "$TMP/.claude/builder-loop.local.md" <<LOCALEOF
slug: "$SLUG_A"
worktree_path: "$WT_A"
state_file: $STATE_A
LOCALEOF

resultE=$(run_hook "$WT_B")
phaseAE=$(read_state_field "$STATE_A" "phase")
phaseBE=$(read_state_field "$STATE_B" "phase")

assert_ec "Case E hook EC=2（CWD=B 则操作 B，忽略 local.md 指向 A）" "$resultE" 2
assert "Case E state-A 未被操作" "[ '$phaseAE' = 'active' ]"
assert "Case E state-B 被操作" "[ '$phaseBE' = 'passed_pending_review' ]"

rm -f "$TMP/.claude/builder-loop.local.md"

harness_report
