#!/usr/bin/env bash
# test-bare-loop-merge.sh — E2E：bare loop 完整 stop hook 流程
#
# 防回归：
#   V1.9.1 之前 merge-worktree-back.sh::read_field 在 bare loop 场景（state 无 worktree_path 字段）
#   下因 grep 未命中 + pipefail + set -e 静默退出 → MERGE_OUT 为空 → MERGE_ACTION 空 → case *
#   静默 exit 0 + rm state，state 丢失。本测试覆盖：
#   1. bare loop（worktree.enabled=false）一轮完整 PASS 路径
#   2. case * 默认分支（M5）若被命中要 echo 详细错误 + exit 2，不再静默删 state
#
# 用法：bash test-bare-loop-merge.sh
# 退出码：0=全部通过 / 1=有失败

set -euo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$THIS_DIR/../../../.." && pwd)"
HOOK_SCRIPT="${REPO_ROOT}/scripts/builder-loop-stop.sh"
MERGE_SCRIPT="${REPO_ROOT}/skills/builder-loop/scripts/merge-worktree-back.sh"

PASS=0
FAIL=0

assert() {
  local desc="$1" cond="$2"
  if eval "$cond"; then echo "  ✅ $desc"; PASS=$(( PASS + 1 ));
  else echo "  ❌ $desc (cond: $cond)"; FAIL=$(( FAIL + 1 )); fi
}

call_stop_hook() {
  local proj="$1" err_file="$2" ec=0
  printf '{"cwd": "%s"}' "$proj" | bash "$HOOK_SCRIPT" 2>"$err_file" >/dev/null || ec=$?
  return "$ec"
}

echo "=== E2E: Bare loop 完整 stop hook + merge 路径回归测试 ==="
assert "stop hook 存在" "[ -f '$HOOK_SCRIPT' ]"
assert "merge 脚本存在" "[ -f '$MERGE_SCRIPT' ]"

# ============================================================
# Case 1: bare loop 完整 PASS 路径（locate-state.sh slug=__main__ 兜底）
# ============================================================
echo ""
echo "=== Case 1: bare loop 完整 PASS 路径 ==="
TMP1="$(mktemp -d)"
# TMP1/TMP2 都加 :- 兜底，case 1 异常退出时 TMP2 可能未赋值，避免 unset var 杀 trap
trap 'rm -rf "${TMP1:-}" "${TMP2:-}"' EXIT
cd "$TMP1"
git init -q
git config user.email "e2e@test.local"
git config user.name "e2e-test"
mkdir -p .claude src
cat > .claude/loop.yml <<'YMLEOF'
pass_cmd:
  - stage: smoke
    cmd: "true"
    timeout: 10
max_iterations: 3
layout:
  source_dirs: [src]
worktree:
  enabled: false
YMLEOF
echo "seed" > README.md
git add -A
git commit -q -m "chore(test): [cr_id_skip] Bare loop seed"

mkdir -p .claude/builder-loop/state
HEAD1="$(git rev-parse --short HEAD)"
# 写 bare loop state（slug=__main__，无 worktree_path）
cat > .claude/builder-loop/state/__main__.yml <<EOF
# builder-loop state file (do NOT manually edit while loop is active)
active: true
slug: "__main__"
owner_cwd: "$TMP1"
iter: 0
max_iter: 5
project_root: "$TMP1"
main_repo_path: "$TMP1"
start_head: "$HEAD1"
worktree_path: ""
plan_file: ""
task_description: |
  bare-loop-test
source_dirs: "src"
test_dirs: ""
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
stopped_reason: ""
created_at: "2026-04-01T00:00:00+08:00"
EOF

ERR1="$(mktemp)"
EC1=0
call_stop_hook "$TMP1" "$ERR1" || EC1=$?

assert "Case 1 stop hook EC=2（PASS 续接）" "[ '$EC1' -eq 2 ]"
assert "Case 1 stderr 含 PASS_CMD 全部阶段通过" "grep -q 'PASS_CMD 全部阶段通过' '$ERR1'"
assert "Case 1 state 已被 rm（PASS 后 cleanup）" "[ ! -f '$TMP1/.claude/builder-loop/state/__main__.yml' ]"
assert "Case 1 cursor 已写" "[ -f '$TMP1/.claude/builder-loop/last_processed_head' ]"

# ============================================================
# Case 2: 直接调 merge-worktree-back.sh，bare loop state（无 worktree_path）→ NOOP
# 防 V1.9.1 修过的 read_field 静默退出回归
# ============================================================
echo ""
echo "=== Case 2: merge-worktree-back.sh 在 bare state 下应输出 NOOP ==="
TMP2="$(mktemp -d)"
mkdir -p "$TMP2/.claude/builder-loop/state"
cat > "$TMP2/.claude/builder-loop/state/__main__.yml" <<EOF
active: true
slug: "__main__"
project_root: "$TMP2"
main_repo_path: "$TMP2"
start_head: "deadbeef"
worktree_path: ""
EOF

MERGE_OUT="$(bash "$MERGE_SCRIPT" "$TMP2/.claude/builder-loop/state/__main__.yml" 2>&1 || true)"
MERGE_LAST="$(echo "$MERGE_OUT" | tail -1)"

assert "Case 2 merge 输出非空（防 grep 静默退出回归）" "[ -n '$MERGE_LAST' ]"
assert "Case 2 merge 末行 = NOOP" "[ '$MERGE_LAST' = 'NOOP' ]"

# ============================================================
# Case 3: 老 V1.x bare state（无 main_repo_path 字段，project_root=主仓）
# merge-worktree-back.sh 兼容性
# ============================================================
echo ""
echo "=== Case 3: 老 V1.x bare state（缺 main_repo_path）兼容 ==="
cat > "$TMP2/.claude/builder-loop/state/__main__.yml" <<EOF
active: true
slug: "__main__"
project_root: "$TMP2"
start_head: "deadbeef"
worktree_path: ""
EOF

MERGE_OUT3="$(bash "$MERGE_SCRIPT" "$TMP2/.claude/builder-loop/state/__main__.yml" 2>&1 || true)"
MERGE_LAST3="$(echo "$MERGE_OUT3" | tail -1)"

assert "Case 3 merge 输出非空" "[ -n '$MERGE_LAST3' ]"
assert "Case 3 merge 末行 = NOOP（用 project_root 兜底为主仓）" "[ '$MERGE_LAST3' = 'NOOP' ]"

# ============================================================
# 总结
# ============================================================
echo ""
echo "=== 总计 ==="
echo "  ✅ PASS: $PASS"
echo "  ❌ FAIL: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
