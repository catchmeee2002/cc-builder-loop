#!/usr/bin/env bash
# test-no-diff-silence.sh — V3.0 L2B 闸：worktree HEAD == last_iter_head + git status 空 → 静默
#
# 验证：
#   Case 1: state.last_iter_head == worktree HEAD + 工作树 clean → hook 静默 exit 0
#   Case 2: 在 worktree 内改一个文件（dirty 出现）→ hook 进 PASS_CMD 路径

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
  local cwd="$1" err_file="$2" ec=0
  printf '{"cwd": "%s"}' "$cwd" | bash "$HOOK_SCRIPT" 2>"$err_file" >/dev/null || ec=$?
  echo "$ec"
}

echo "=== V3.0 L2B 闸 — worktree 无改动 fixture ==="
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
# .gitignore 排除 hook / loop 副作用产物，避免 worktree git status 因这些文件非空触发误判
cat > .gitignore <<'GIT'
.claude/builder-loop/
.claude/loop-runs/
.claude/reviewer-*.txt
.claude/reviewer-*.json
.claude/review_reports/
GIT
git add -A
git commit -q -m "chore(test): [cr_id_skip] No-diff silence fixture seed"
HEAD1="$(git -C "$TMP" rev-parse --short HEAD)"

SLUG="nodiff-fixture"
WT="$TMP/.claude/worktrees/${SLUG}"
git -C "$TMP" worktree add -q "$WT" 2>/dev/null
WT_HEAD="$(git -C "$WT" rev-parse --short HEAD)"

mkdir -p "$TMP/.claude/builder-loop/state"
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
last_iter_head: "${WT_HEAD}"
worktree_path: "${WT}"
worktree_mode: "clean"
plan_file: ""
task_description: |
  no-diff-silence
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
# Case 1: HEAD == last_iter_head + git status 空 → 静默
# ============================================================
echo ""
echo "=== Case 1: worktree 无改动 ==="
ERR1="$(mktemp)"
EC1="$(run_hook "$WT" "$ERR1")"

assert "Case 1 hook EC=0（静默）" "[ '$EC1' -eq 0 ]"
assert "Case 1 PASS_CMD 未跑（stderr 不含 'iter ' + 跑 PASS_CMD）" "! grep -q '正在跑 PASS_CMD' '$ERR1'"
ITER1="$(grep -E '^iter:' "$TMP/.claude/builder-loop/state/${SLUG}.yml" | awk '{print $2}')"
assert "Case 1 iter 不变（=1）" "[ '$ITER1' = '1' ]"

# ============================================================
# Case 2: worktree 出现 dirty → 不再静默
# ============================================================
echo ""
echo "=== Case 2: worktree 内改文件 ==="
echo "new content" > "$WT/dirty-file.txt"

ERR2="$(mktemp)"
EC2="$(run_hook "$WT" "$ERR2")"

# Case 2 期望 hook 进 PASS_CMD 路径（不再因 L2B 静默）
assert "Case 2 hook 不再因 L2B 静默（stderr 有 PASS_CMD 跑或写入相关迹象）" \
  "grep -q '正在跑 PASS_CMD\|PASS_CMD\|phase=passed_pending_review\|已 commit' '$ERR2'"

echo ""
echo "=== 总计 ==="
echo "  ✅ PASS: $PASS"
echo "  ❌ FAIL: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
