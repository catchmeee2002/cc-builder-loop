#!/usr/bin/env bash
# test-old-state-compat.sh — V3.0 老 state（V2.x 创建，无 phase 字段）兼容
#
# 验证：
#   Case A: state 含 active 但缺 phase 字段 → hook stderr warning + 不阻断 + PASS 后自动写 phase
#   Case B: state 含 active + phase=active（V3.0 标准）→ 无 warning（不重复提示）

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

setup_repo() {
  local dir="$1"
  cd "$dir"
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
  git commit -q -m "chore(test): [cr_id_skip] Old-state compat fixture seed"
}

echo "=== V3.0 老 state（无 phase 字段）兼容 fixture ==="

# ============================================================
# Case A: state 缺 phase 字段 → warning + 不阻断 + 自动升级
# ============================================================
echo ""
echo "=== Case A: V2.x 老 state（无 phase 字段） ==="
TMPA="$(mktemp -d)"
trap 'rm -rf "${TMPA:-}" "${TMPB:-}"' EXIT
setup_repo "$TMPA"
HEAD1="$(git -C "$TMPA" rev-parse --short HEAD)"

SLUG_A="old-state-fixture-a"
WT_A="$TMPA/.claude/worktrees/${SLUG_A}"
git -C "$TMPA" worktree add -q "$WT_A"
echo "feature" > "$WT_A/feature.txt"

mkdir -p "$TMPA/.claude/builder-loop/state"
# V2.x state — 无 phase / last_iter_head / cleanup_phase 字段
cat > "$TMPA/.claude/builder-loop/state/${SLUG_A}.yml" <<EOF
active: true
slug: "${SLUG_A}"
owner_cwd: "$TMPA"
iter: 0
max_iter: 5
project_root: "${WT_A}"
main_repo_path: "$TMPA"
start_head: "${HEAD1}"
worktree_path: "${WT_A}"
worktree_mode: "clean"
plan_file: ""
task_description: |
  old-state-test
source_dirs: ""
test_dirs: ""
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
stopped_reason: ""
created_at: "2026-04-15T00:00:00+08:00"
EOF
STATE_A="$TMPA/.claude/builder-loop/state/${SLUG_A}.yml"

ERR_A="$(mktemp)"
EC_A="$(run_hook "$WT_A" "$ERR_A")"

assert "Case A hook 不阻断（EC=2 PASS 续接）" "[ '$EC_A' -eq 2 ]"
assert "Case A stderr 含老 state warning" "grep -q '检测到 V2.x 老 state' '$ERR_A'"
assert "Case A stderr 含自动升级提示" "grep -q '已自动升级到 V3.0 schema' '$ERR_A'"
# PASS 跑完后 hook 进 V3.0 worktree 模式分支自动写 phase
PHASE_AFTER="$(grep -E '^phase:' "$STATE_A" | head -1 | sed -E 's/^phase:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/')"
assert "Case A PASS 后 state 已自动升级（phase=passed_pending_review）" \
  "[ '$PHASE_AFTER' = 'passed_pending_review' ]"

# ============================================================
# Case B: state 含 phase 字段 → 无重复 warning
# ============================================================
echo ""
echo "=== Case B: V3.0 标准 state（含 phase 字段） ==="
TMPB="$(mktemp -d)"
setup_repo "$TMPB"
HEAD1B="$(git -C "$TMPB" rev-parse --short HEAD)"

SLUG_B="old-state-fixture-b"
WT_B="$TMPB/.claude/worktrees/${SLUG_B}"
git -C "$TMPB" worktree add -q "$WT_B"
echo "feature" > "$WT_B/feature.txt"

mkdir -p "$TMPB/.claude/builder-loop/state"
cat > "$TMPB/.claude/builder-loop/state/${SLUG_B}.yml" <<EOF
active: true
phase: "active"
slug: "${SLUG_B}"
owner_cwd: "$TMPB"
iter: 0
max_iter: 5
project_root: "${WT_B}"
main_repo_path: "$TMPB"
start_head: "${HEAD1B}"
last_iter_head: "${HEAD1B}"
worktree_path: "${WT_B}"
worktree_mode: "clean"
plan_file: ""
task_description: |
  v3-state-test
source_dirs: ""
test_dirs: ""
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
stopped_reason: ""
cleanup_phase: ""
created_at: "2026-05-09T00:00:00+08:00"
EOF

ERR_B="$(mktemp)"
EC_B="$(run_hook "$WT_B" "$ERR_B")"

assert "Case B hook 正常（EC=2）" "[ '$EC_B' -eq 2 ]"
assert "Case B stderr 不含老 state warning（V3.0 标准 state 不重复提示）" \
  "! grep -q '检测到 V2.x 老 state' '$ERR_B'"

echo ""
echo "=== 总计 ==="
echo "  ✅ PASS: $PASS"
echo "  ❌ FAIL: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
