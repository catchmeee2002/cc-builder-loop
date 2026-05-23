#!/usr/bin/env bash
# test-askuserquestion-silence.sh — V3.0 L2A 闸：transcript 末是 pending AskUserQuestion → 静默
#
# Case 1: transcript 末尾是 AskUserQuestion tool_use 且无 tool_result → hook 静默 exit 0
# Case 2: AskUserQuestion 有 tool_result（已答）→ hook 不再因 L2A 静默

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "askuserquestion-silence"

# 需要 worktree + dirty 让 L2B 不命中，专测 L2A
env=$(create_test_env --slug "askuq-fixture" --worktree --phase active --wt-dirty "dirty.txt")
WT="$env/.claude/worktrees/askuq-fixture"

# ============================================================
# Case 1: pending AskUserQuestion → 静默
# ============================================================
section "Case 1: pending AskUserQuestion"
TP1="$(mktemp)"
_HARNESS_TMPDIRS+=("$(dirname "$TP1")")
cat > "$TP1" <<'TS'
{"type":"user","message":{"content":[{"type":"text","text":"hi"}]}}
{"type":"assistant","message":{"content":[{"type":"text","text":"想问你"},{"type":"tool_use","id":"toolu_pending_1","name":"AskUserQuestion","input":{"questions":[]}}]}}
TS

# run_hook 不支持 transcript_path，手动调
handle1="$(mktemp -d -t harness-result-XXXXXX)"
_HARNESS_TMPDIRS+=("$handle1")
ec1=0
printf '{"cwd": "%s", "transcript_path": "%s"}' "$WT" "$TP1" \
  | bash "$HARNESS_HOOK" >"$handle1/stdout" 2>"$handle1/stderr" || ec1=$?
echo "$ec1" > "$handle1/ec"

assert_ec "Case 1 hook EC=0（L2A 静默）" "$handle1" 0
assert "Case 1 PASS_CMD 未跑" "! grep -q '正在跑 PASS_CMD' '$handle1/stderr'"

# ============================================================
# Case 2: AskUserQuestion 已答（有 tool_result）→ 不静默
# ============================================================
section "Case 2: AskUserQuestion 已答"
TP2="$(mktemp)"
cat > "$TP2" <<'TS'
{"type":"user","message":{"content":[{"type":"text","text":"hi"}]}}
{"type":"assistant","message":{"content":[{"type":"tool_use","id":"toolu_answered_1","name":"AskUserQuestion","input":{"questions":[]}}]}}
{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_answered_1","content":"selected: 选项A"}]}}
{"type":"assistant","message":{"content":[{"type":"text","text":"好的"}]}}
TS

handle2="$(mktemp -d -t harness-result-XXXXXX)"
_HARNESS_TMPDIRS+=("$handle2")
ec2=0
printf '{"cwd": "%s", "transcript_path": "%s"}' "$WT" "$TP2" \
  | bash "$HARNESS_HOOK" >"$handle2/stdout" 2>"$handle2/stderr" || ec2=$?
echo "$ec2" > "$handle2/ec"

assert "Case 2 hook 不再因 L2A 静默" "! grep -q 'askuq_pending' '$handle2/stderr'"

harness_report
