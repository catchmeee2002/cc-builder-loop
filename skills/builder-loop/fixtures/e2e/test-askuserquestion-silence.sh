#!/usr/bin/env bash
# test-askuserquestion-silence.sh — V3.0 L2A 闸：transcript 末是 pending AskUserQuestion → 静默
#
# 验证：
#   Case 1: transcript 末尾是 AskUserQuestion tool_use 且无 tool_result → hook 静默 exit 0
#   Case 2: AskUserQuestion 有 tool_result（已答）→ hook 不再因 L2A 静默

set -uo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$THIS_DIR/../../../.." && pwd)"
HOOK_SCRIPT="${REPO_ROOT}/scripts/builder-loop-stop.sh"

PASS=0
FAIL=0
assert() {
  local desc="$1" cond="$2"
  if eval "$cond"; then echo "  ✅ $desc"; PASS=$(( PASS + 1 ))
  else echo "  ❌ $desc (cond: $cond)"; FAIL=$(( FAIL + 1 )); fi
}

run_hook() {
  local cwd="$1" tp="$2" err_file="$3" ec=0
  printf '{"cwd": "%s", "transcript_path": "%s"}' "$cwd" "$tp" \
    | bash "$HOOK_SCRIPT" 2>"$err_file" >/dev/null || ec=$?
  echo "$ec"
}

echo "=== V3.0 L2A 闸 — AskUserQuestion 静默 fixture ==="
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP:-}"' EXIT

cd "$TMP"
git init -q
git config user.email "e2e@test.local"
git config user.name "e2e-test"
mkdir -p .claude
cat > .claude/loop.yml <<'Y'
pass_cmd:
  - stage: smoke
    cmd: "true"
    timeout: 10
worktree:
  enabled: false
Y
echo "seed" > README.md
cat > .gitignore <<'G'
.claude/builder-loop/
.claude/loop-runs/
.claude/reviewer-*.txt
.claude/review_reports/
G
git add -A
git commit -q -m "chore(test): [cr_id_skip] AskUQ fixture seed"

# 让 worktree 处于 dirty 状态（让 L2B 不会静默；专测 L2A）
SLUG="askuq-fixture"
WT="$TMP/.claude/worktrees/${SLUG}"
git -C "$TMP" worktree add -q "$WT" 2>/dev/null
echo "dirty" > "$WT/dirty.txt"  # 让 L2B 不命中

mkdir -p "$TMP/.claude/builder-loop/state"
HEAD1="$(git -C "$TMP" rev-parse --short HEAD)"
cat > "$TMP/.claude/builder-loop/state/${SLUG}.yml" <<EOF
active: true
phase: "active"
slug: "${SLUG}"
owner_cwd: "$TMP"
iter: 1
max_iter: 5
project_root: "${WT}"
main_repo_path: "$TMP"
start_head: "${HEAD1}"
last_iter_head: "${HEAD1}"
worktree_path: "${WT}"
worktree_mode: "clean"
plan_file: ""
task_description: |
  askuq-test
source_dirs: ""
test_dirs: ""
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
stopped_reason: ""
cleanup_phase: ""
created_at: "2026-05-09T00:00:00+08:00"
EOF

# ============================================================
# Case 1: transcript 末是 pending AskUserQuestion → 静默
# ============================================================
echo ""
echo "=== Case 1: pending AskUserQuestion ==="
TP1="$(mktemp)"
cat > "$TP1" <<'TS'
{"type":"user","message":{"content":[{"type":"text","text":"hi"}]}}
{"type":"assistant","message":{"content":[{"type":"text","text":"想问你"},{"type":"tool_use","id":"toolu_pending_1","name":"AskUserQuestion","input":{"questions":[]}}]}}
TS

ERR1="$(mktemp)"
EC1="$(run_hook "$WT" "$TP1" "$ERR1")"

assert "Case 1 hook EC=0（L2A 静默）" "[ '$EC1' -eq 0 ]"
assert "Case 1 PASS_CMD 未跑" "! grep -q '正在跑 PASS_CMD' '$ERR1'"

# ============================================================
# Case 2: AskUserQuestion 有 tool_result（已答）→ 不静默
# ============================================================
echo ""
echo "=== Case 2: AskUserQuestion 已答（有 tool_result） ==="
TP2="$(mktemp)"
cat > "$TP2" <<'TS'
{"type":"user","message":{"content":[{"type":"text","text":"hi"}]}}
{"type":"assistant","message":{"content":[{"type":"tool_use","id":"toolu_answered_1","name":"AskUserQuestion","input":{"questions":[]}}]}}
{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_answered_1","content":"selected: 选项A"}]}}
{"type":"assistant","message":{"content":[{"type":"text","text":"好的"}]}}
TS

ERR2="$(mktemp)"
EC2="$(run_hook "$WT" "$TP2" "$ERR2")"

assert "Case 2 hook 不再因 L2A 静默（stderr 没有 askuq_pending 退出）" \
  "! grep -q 'askuq_pending' '$ERR2'"

echo ""
echo "=== 总计 ==="
echo "  ✅ PASS: $PASS"
echo "  ❌ FAIL: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
