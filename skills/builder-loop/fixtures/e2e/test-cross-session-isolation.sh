#!/usr/bin/env bash
# test-cross-session-isolation.sh — V3.0 跨 session 隔离
#
# 通过 cwd 推 slug 实现的隔离机制，验证两个并发"session"（用 cwd 区分）互不串扰：
#   Case A: 双 worktree 同时 active，从 worktree-A cwd 跑 hook → 只影响 state-A，state-B 不动
#   Case B: 从 worktree-B cwd 跑 hook → 只影响 state-B，state-A 不动
#   Case C: 从主仓 cwd 跑 hook → 多 active 不绑定（V2.4 策略 5），两 state 都不动

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

write_state() {
  local dir="$1" slug="$2" head="$3" wt="$4"
  cat > "$dir/.claude/builder-loop/state/${slug}.yml" <<EOF
active: true
phase: "active"
slug: "${slug}"
owner_cwd: "$dir"
iter: 0
max_iter: 5
project_root: "${wt}"
main_repo_path: "$dir"
start_head: "${head}"
last_iter_head: "${head}"
worktree_path: "${wt}"
worktree_mode: "clean"
plan_file: ""
task_description: |
  cross-session-test-${slug}
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

echo "=== V3.0 跨 session 隔离 fixture ==="
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
git commit -q -m "chore(test): [cr_id_skip] Cross-session fixture seed"
HEAD1="$(git -C "$TMP" rev-parse --short HEAD)"

# 建两个 worktree，模拟两个并发 session 各跑一个 builder-loop
SLUG_A="cs-fixture-a"
SLUG_B="cs-fixture-b"
WT_A="$TMP/.claude/worktrees/${SLUG_A}"
WT_B="$TMP/.claude/worktrees/${SLUG_B}"
git -C "$TMP" worktree add -q "$WT_A"
git -C "$TMP" worktree add -q "$WT_B"

# 让 worktree A 内有 dirty 改动（让 PASS 后能 commit）
echo "feature-a" > "$WT_A/feature.txt"
# B 同样 dirty（独立测试）
echo "feature-b" > "$WT_B/feature.txt"

mkdir -p "$TMP/.claude/builder-loop/state"
write_state "$TMP" "$SLUG_A" "$HEAD1" "$WT_A"
write_state "$TMP" "$SLUG_B" "$HEAD1" "$WT_B"

STATE_A="$TMP/.claude/builder-loop/state/${SLUG_A}.yml"
STATE_B="$TMP/.claude/builder-loop/state/${SLUG_B}.yml"

# ============================================================
# Case A: 从 worktree-A cwd 跑 hook → 只 state-A 被处理
# ============================================================
echo ""
echo "=== Case A: 从 worktree-A cwd 跑 hook ==="
# V3.2: local.md slug 指向 A，hook 只处理 A
cat > "$TMP/.claude/builder-loop.local.md" <<LOCALEOF
slug: "${SLUG_A}"
worktree_path: "${WT_A}"
state_file: "$TMP/.claude/builder-loop/state/${SLUG_A}.yml"
LOCALEOF

ERR_A="$(mktemp)"
EC_A="$(run_hook "$WT_A" "$ERR_A")"

PHASE_A="$(grep -E '^phase:' "$STATE_A" | head -1 | sed -E 's/^phase:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/')"
PHASE_B_AFTER_A="$(grep -E '^phase:' "$STATE_B" | head -1 | sed -E 's/^phase:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/')"

assert "Case A hook EC=2（PASS 续接）" "[ '$EC_A' -eq 2 ]"
assert "Case A state-A.phase = passed_pending_review" "[ '$PHASE_A' = 'passed_pending_review' ]"
assert "Case A state-B.phase 仍为 active（未被串扰）" "[ '$PHASE_B_AFTER_A' = 'active' ]"

# ============================================================
# Case B: 从 worktree-B cwd 跑 hook → 只 state-B 被处理
# ============================================================
echo ""
echo "=== Case B: 从 worktree-B cwd 跑 hook ==="
# V3.2: 切 local.md slug 到 B
cat > "$TMP/.claude/builder-loop.local.md" <<LOCALEOF
slug: "${SLUG_B}"
worktree_path: "${WT_B}"
state_file: "$TMP/.claude/builder-loop/state/${SLUG_B}.yml"
LOCALEOF

ERR_B="$(mktemp)"
EC_B="$(run_hook "$WT_B" "$ERR_B")"

PHASE_A_AFTER_B="$(grep -E '^phase:' "$STATE_A" | head -1 | sed -E 's/^phase:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/')"
PHASE_B="$(grep -E '^phase:' "$STATE_B" | head -1 | sed -E 's/^phase:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/')"

assert "Case B hook EC=2" "[ '$EC_B' -eq 2 ]"
assert "Case B state-B.phase = passed_pending_review" "[ '$PHASE_B' = 'passed_pending_review' ]"
# state-A 上一次 Case A 已是 passed_pending_review，不应被 Case B 触发自愈或改写
assert "Case B state-A 未被串扰" "[ '$PHASE_A_AFTER_B' = 'passed_pending_review' ]"

# ============================================================
# Case C: 无 local.md → hook 静默 exit 0，两 state 都不动
# V3.2: 不再靠 CWD 猜测 slug，无 local.md 就不处理任何 state
# ============================================================
echo ""
echo "=== Case C: 无 local.md → 两 state 都不动 ==="
# 重置 state-A / state-B 都为 active 测多 active 场景
write_state "$TMP" "$SLUG_A" "$HEAD1" "$WT_A"
write_state "$TMP" "$SLUG_B" "$HEAD1" "$WT_B"

# 删掉 local.md，模拟"没有绑定任何 session"
rm -f "$TMP/.claude/builder-loop.local.md"

ERR_C="$(mktemp)"
EC_C="$(run_hook "$TMP" "$ERR_C")"

PHASE_A_C="$(grep -E '^phase:' "$STATE_A" | head -1 | sed -E 's/^phase:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/')"
PHASE_B_C="$(grep -E '^phase:' "$STATE_B" | head -1 | sed -E 's/^phase:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/')"

# 无 local.md → hook exit 0 放行，两 state 都不受影响
assert "Case C hook EC=0（无 local.md 静默放行）" "[ '$EC_C' -eq 0 ]"
assert "Case C state-A 仍为 active" "[ '$PHASE_A_C' = 'active' ]"
assert "Case C state-B 仍为 active" "[ '$PHASE_B_C' = 'active' ]"

echo ""
echo "=== 总计 ==="
echo "  ✅ PASS: $PASS"
echo "  ❌ FAIL: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
