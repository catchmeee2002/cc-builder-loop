#!/usr/bin/env bash
# test-worktree-commit-only.sh — V3.0 worktree-commit-only.sh 单元测
#
# 验证：
#   Case A: worktree 有 dirty 改动 → COMMIT_DONE <new_head> + worktree HEAD 推进
#   Case B: worktree 干净（git status 空）→ NOOP
#   Case C: worktree_path 字段为空（bare 模式）→ NOOP
#   Case D: state 文件不存在 → ERROR exit 3

set -uo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$THIS_DIR/../../../.." && pwd)"
COMMIT_ONLY="${REPO_ROOT}/skills/builder-loop/scripts/worktree-commit-only.sh"

PASS=0
FAIL=0
assert() {
  local desc="$1" cond="$2"
  if eval "$cond"; then echo "  ✅ $desc"; PASS=$(( PASS + 1 ))
  else echo "  ❌ $desc (cond: $cond)"; FAIL=$(( FAIL + 1 )); fi
}

setup_repo() {
  local dir="$1" slug="$2"
  cd "$dir"
  git init -q
  git config user.email "e2e@test.local"
  git config user.name "e2e-test"
  echo "seed" > README.md
  git add -A
  git commit -q -m "chore(test): [cr_id_skip] Commit-only fixture"
  WT="$dir/.claude/worktrees/${slug}"
  git -C "$dir" worktree add -q "$WT"
  HEAD1="$(git -C "$dir" rev-parse --short HEAD)"

  mkdir -p "$dir/.claude/builder-loop/state"
  cat > "$dir/.claude/builder-loop/state/${slug}.yml" <<EOF
active: true
phase: "active"
slug: "${slug}"
owner_cwd: "$dir"
iter: 0
max_iter: 5
project_root: "${WT}"
main_repo_path: "$dir"
start_head: "${HEAD1}"
last_iter_head: "${HEAD1}"
worktree_path: "${WT}"
worktree_mode: "clean"
plan_file: ""
task_description: |
  commit-only-test
source_dirs: ""
test_dirs: ""
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
stopped_reason: ""
cleanup_phase: ""
created_at: "2026-05-09T00:00:00+08:00"
EOF
}

echo "=== V3.0 worktree-commit-only.sh 单点测试 ==="

# ============================================================
# Case A: worktree 有 dirty 改动 → COMMIT_DONE
# ============================================================
echo ""
echo "=== Case A: worktree dirty → COMMIT_DONE ==="
TMPA="$(mktemp -d)"
trap 'rm -rf "${TMPA:-}" "${TMPB:-}" "${TMPC:-}" "${TMPD:-}"' EXIT
SLUG_A="commit-fixture-a"
setup_repo "$TMPA" "$SLUG_A"
STATE_A="$TMPA/.claude/builder-loop/state/${SLUG_A}.yml"
WT_A="$TMPA/.claude/worktrees/${SLUG_A}"

echo "feature" > "$WT_A/feature.txt"
HEAD_BEFORE_A="$(git -C "$WT_A" rev-parse --short HEAD)"

OUT_A="$(bash "$COMMIT_ONLY" "$STATE_A" 2>&1 || true)"
LAST_A="$(echo "$OUT_A" | tail -1)"
ACTION_A="$(echo "$LAST_A" | awk '{print $1}')"
NEW_HEAD_A="$(echo "$LAST_A" | awk '{print $2}')"

assert "Case A 输出 COMMIT_DONE" "[ '$ACTION_A' = 'COMMIT_DONE' ]"
assert "Case A new_head 非空" "[ -n '$NEW_HEAD_A' ]"
assert "Case A worktree HEAD 推进" \
  "[ \"\$(git -C $WT_A rev-parse --short HEAD)\" != '$HEAD_BEFORE_A' ]"
assert "Case A 主仓 HEAD 未变（不该影响主仓）" \
  "[ \"\$(git -C $TMPA rev-parse --short HEAD)\" = '$HEAD_BEFORE_A' ]"
assert "Case A worktree 仍存在" "[ -d '$WT_A' ]"

# ============================================================
# Case B: worktree 干净 → NOOP
# ============================================================
echo ""
echo "=== Case B: worktree 干净 → NOOP ==="
TMPB="$(mktemp -d)"
SLUG_B="commit-fixture-b"
setup_repo "$TMPB" "$SLUG_B"
STATE_B="$TMPB/.claude/builder-loop/state/${SLUG_B}.yml"
WT_B="$TMPB/.claude/worktrees/${SLUG_B}"

OUT_B="$(bash "$COMMIT_ONLY" "$STATE_B" 2>&1 || true)"
LAST_B="$(echo "$OUT_B" | tail -1)"

assert "Case B 输出 NOOP" "[ '$LAST_B' = 'NOOP' ]"

# ============================================================
# Case C: bare 模式（worktree_path 空）→ NOOP
# ============================================================
echo ""
echo "=== Case C: bare 模式 → NOOP ==="
TMPC="$(mktemp -d)"
mkdir -p "$TMPC/.claude/builder-loop/state"
cat > "$TMPC/.claude/builder-loop/state/__main__.yml" <<EOF
active: true
phase: "active"
slug: "__main__"
worktree_path: ""
EOF

OUT_C="$(bash "$COMMIT_ONLY" "$TMPC/.claude/builder-loop/state/__main__.yml" 2>&1 || true)"
LAST_C="$(echo "$OUT_C" | tail -1)"

assert "Case C bare 模式输出 NOOP" "[ '$LAST_C' = 'NOOP' ]"

# ============================================================
# Case D: state 文件不存在 → ERROR exit 3
# ============================================================
echo ""
echo "=== Case D: state 不存在 → ERROR ==="
TMPD="$(mktemp -d)"
EC_D=0
OUT_D="$(bash "$COMMIT_ONLY" "$TMPD/nonexistent.yml" 2>&1)" || EC_D=$?
LAST_D="$(echo "$OUT_D" | tail -1)"

assert "Case D 输出含 ERROR" "echo '$LAST_D' | grep -q '^ERROR'"
assert "Case D exit 3" "[ '$EC_D' -eq 3 ]"

echo ""
echo "=== 总计 ==="
echo "  ✅ PASS: $PASS"
echo "  ❌ FAIL: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
