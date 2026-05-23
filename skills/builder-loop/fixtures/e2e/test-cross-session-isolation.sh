#!/usr/bin/env bash
# test-cross-session-isolation.sh — V3.0 跨 session 隔离
#
# 通过 local.md slug 实现的隔离机制，验证两个并发"session"互不串扰：
#   Case A: 双 worktree 同时 active，local.md 指向 A → hook 只影响 state-A
#   Case B: local.md 切到 B → hook 只影响 state-B
#   Case C: 无 local.md → hook 静默 exit 0，两 state 都不动

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "cross-session-isolation"

SLUG_A="cs-fixture-a"
SLUG_B="cs-fixture-b"

# 手工构建共享主仓 + 双 worktree（create_test_env 只支持单 slug，这里需要同仓双 state）
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
.claude/builder-loop.local.md
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

# Helper: 写 state
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
plan_file: ""
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
# Case A: local.md 指向 A → 只 state-A 被处理
# ============================================================
section "Case A: local.md 指向 A"
cat > "$TMP/.claude/builder-loop.local.md" <<LOCALEOF
slug: "$SLUG_A"
worktree_path: "$WT_A"
state_file: $STATE_A
LOCALEOF

# run_hook 需要 cwd 参数但 hook 实际靠 local.md 定位
resultA=$(run_hook "$WT_A")
phaseA=$(read_state_field "$STATE_A" "phase")
phaseBafterA=$(read_state_field "$STATE_B" "phase")

assert_ec "Case A hook EC=2（PASS 续接）" "$resultA" 2
assert "Case A state-A.phase = passed_pending_review" "[ '$phaseA' = 'passed_pending_review' ]"
assert "Case A state-B.phase 仍为 active（未被串扰）" "[ '$phaseBafterA' = 'active' ]"

# ============================================================
# Case B: local.md 切到 B → 只 state-B 被处理
# ============================================================
section "Case B: local.md 切到 B"
cat > "$TMP/.claude/builder-loop.local.md" <<LOCALEOF
slug: "$SLUG_B"
worktree_path: "$WT_B"
state_file: $STATE_B
LOCALEOF

resultB=$(run_hook "$WT_B")
phaseAafterB=$(read_state_field "$STATE_A" "phase")
phaseB=$(read_state_field "$STATE_B" "phase")

assert_ec "Case B hook EC=2" "$resultB" 2
assert "Case B state-B.phase = passed_pending_review" "[ '$phaseB' = 'passed_pending_review' ]"
assert "Case B state-A 未被串扰" "[ '$phaseAafterB' = 'passed_pending_review' ]"

# ============================================================
# Case C: 无 local.md → 两 state 都不动
# ============================================================
section "Case C: 无 local.md → 两 state 都不动"
# 重置两 state 为 active
write_cs_state "$SLUG_A" "$WT_A"
write_cs_state "$SLUG_B" "$WT_B"

rm -f "$TMP/.claude/builder-loop.local.md"

resultC=$(run_hook "$TMP")
phaseAC=$(read_state_field "$STATE_A" "phase")
phaseBC=$(read_state_field "$STATE_B" "phase")

assert_ec "Case C hook EC=0（无 local.md 静默放行）" "$resultC" 0
assert "Case C state-A 仍为 active" "[ '$phaseAC' = 'active' ]"
assert "Case C state-B 仍为 active" "[ '$phaseBC' = 'active' ]"

harness_report
