#!/usr/bin/env bash
# test-pause-file.sh — V3.0 L3 闸：pause 文件存在时 hook 静默
#
# 验证：
#   Case 1: 创建 pause 文件 → hook exit 0 + 不跑 PASS_CMD
#   Case 2: rm pause 文件 → hook 进 PASS_CMD 路径

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
max_iterations: 3
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
  git commit -q -m "chore(test): [cr_id_skip] Pause file fixture seed"
}

echo "=== V3.0 L3 闸 — pause 文件 fixture ==="
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP:-}"' EXIT
setup_repo "$TMP"

mkdir -p "$TMP/.claude/builder-loop/state"
HEAD1="$(git -C "$TMP" rev-parse --short HEAD)"
SLUG="pause-fixture-slug"

# 写 worktree 模式 state（V3.0 含 phase + last_iter_head）
mkdir -p "$TMP/.claude/worktrees/${SLUG}"
cat > "$TMP/.claude/builder-loop/state/${SLUG}.yml" <<EOF
active: true
phase: "active"
slug: "${SLUG}"
owner_cwd: "$TMP"
iter: 0
max_iter: 5
project_root: "$TMP/.claude/worktrees/${SLUG}"
main_repo_path: "$TMP"
start_head: "${HEAD1}"
last_iter_head: "${HEAD1}"
worktree_path: "$TMP/.claude/worktrees/${SLUG}"
worktree_mode: "clean"
plan_file: ""
task_description: |
  pause-file-test
source_dirs: ""
test_dirs: ""
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
stopped_reason: ""
cleanup_phase: ""
created_at: "2026-05-09T00:00:00+08:00"
EOF

# V3.2: 创建 local.md 让 stop hook 能通过 slug 精确定位 state
cat > "$TMP/.claude/builder-loop.local.md" <<LOCALEOF
slug: "${SLUG}"
worktree_path: "$TMP/.claude/worktrees/${SLUG}"
state_file: "$TMP/.claude/builder-loop/state/${SLUG}.yml"
LOCALEOF

# 实际让 worktree 是个 git worktree（hook 内的 git -C 调用需要它是 git 仓）
git -C "$TMP" worktree add -q "$TMP/.claude/worktrees/${SLUG}" 2>/dev/null || true
# 让 worktree 内有 dirty 改动，确保 L2B 闸不命中（专测 L3 pause）
echo "dirty-for-pause-test" > "$TMP/.claude/worktrees/${SLUG}/dirty.txt"

# ============================================================
# Case 1: pause 文件存在 → hook 静默 exit 0
# ============================================================
echo ""
echo "=== Case 1: pause 文件存在 ==="
mkdir -p "$TMP/.claude/builder-loop"
touch "$TMP/.claude/builder-loop/${SLUG}.pause"

ERR1="$(mktemp)"
EC1="$(run_hook "$TMP/.claude/worktrees/${SLUG}" "$ERR1")"

assert "Case 1 hook EC=0（静默）" "[ '$EC1' -eq 0 ]"
assert "Case 1 stderr 含 L3 pause 命中" "grep -q 'L3 pause' '$ERR1'"
assert "Case 1 state 仍存在（未被处理）" "[ -f '$TMP/.claude/builder-loop/state/${SLUG}.yml' ]"
ITER1="$(grep -E '^iter:' "$TMP/.claude/builder-loop/state/${SLUG}.yml" | awk '{print $2}')"
assert "Case 1 iter 不变（PASS_CMD 未跑）" "[ '$ITER1' = '0' ]"

# ============================================================
# Case 2: rm pause 文件 → hook 进 PASS_CMD 路径
# ============================================================
echo ""
echo "=== Case 2: rm pause 文件 ==="
rm -f "$TMP/.claude/builder-loop/${SLUG}.pause"

ERR2="$(mktemp)"
EC2="$(run_hook "$TMP/.claude/worktrees/${SLUG}" "$ERR2")"

# 跑 PASS_CMD 后期望 EC=2（PASS 续接），或 EC=0（无变化静默）；不该是因 pause 静默
assert "Case 2 hook 不再因 pause 静默" "! grep -q 'L3 pause' '$ERR2'"

echo ""
echo "=== 总计 ==="
echo "  ✅ PASS: $PASS"
echo "  ❌ FAIL: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
