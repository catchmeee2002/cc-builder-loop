#!/usr/bin/env bash
# test-locate-state-strategy5.sh — E2E：V3.2 策略 5 已删除 + locate-state 回退行为验证
#
# V3.2 变更：
#   - locate-state.sh 策略 5（V2.4 唯一 active worktree 自动绑定）已删除
#   - stop hook 改用 local.md slug 精确定位，不再 CWD 猜测
#
# 覆盖 case：
#   A1: 主仓 cwd + 1 active worktree state（目录存活）→ locate 返回空（策略 5 已删）
#   A2: 主仓 cwd + 2 active worktree state → locate 返回空（同 V2.4 行为，不变）
#   A3: worktree cwd → locate 策略 2 命中（不受策略 5 删除影响）
#   A4: __main__.yml 存在 → locate 策略 4 兜底命中（不受策略 5 删除影响）
#   A5: 策略 3 worktree_path 匹配 cwd → 命中（不受策略 5 删除影响）
#
# 用法：bash test-locate-state-strategy5.sh
# 退出码：0=全部通过 / 1=有失败

set -euo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# .../skills/builder-loop/fixtures/e2e → 仓库根
REPO_ROOT="$(cd "$THIS_DIR/../../../.." && pwd)"
LOCATE_SCRIPT="${REPO_ROOT}/skills/builder-loop/scripts/locate-state.sh"

PASS=0
FAIL=0

assert() {
  local desc="$1" cond="$2"
  if eval "$cond"; then echo "  ✅ $desc"; PASS=$(( PASS + 1 ));
  else echo "  ❌ $desc (cond: $cond)"; FAIL=$(( FAIL + 1 )); fi
}

write_state_yml() {
  # $1 = state file path, $2 = active (true/false), $3 = worktree_path, $4 = slug
  mkdir -p "$(dirname "$1")"
  cat > "$1" <<EOF
active: $2
slug: "$4"
phase: "active"
iter: 0
max_iter: 5
project_root: "$3"
main_repo_path: "${TMP}"
worktree_path: "$3"
worktree_mode: "clean"
start_head: "fakehead"
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
created_at: "1970-01-01T00:00:00+00:00"
EOF
}

echo "=== E2E: V3.2 策略 5 已删除 — locate-state 回退行为 ==="
echo "    locate 脚本：$LOCATE_SCRIPT"
assert "locate-state.sh 存在" "[ -f '$LOCATE_SCRIPT' ]"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "    临时仓库：$TMP"

# ---- 初始化最小仓库 + loop.yml ----
echo "--- 初始化仓库 + loop.yml ---"
cd "$TMP"
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
  test_dirs: []
worktree:
  enabled: true
YMLEOF
echo "seed" > README.md
git add -A
git commit -q -m "chore(test): [cr_id_skip] Initial seed for e2e locate-state strategy5"

STATE_DIR="${TMP}/.claude/builder-loop/state"
mkdir -p "$STATE_DIR"

# ============================================================
# A1: 主仓 cwd + 1 active worktree state（目录存活）→ 返回空
#     V3.2 删除策略 5 后，主仓 CWD 不再自动绑定唯一 active worktree
# ============================================================
echo ""
echo "--- A1: 主仓 cwd + 1 active worktree state → 返回空（策略 5 已删）---"
WT_A1="${TMP}/.claude/worktrees/1700000001-foo"
mkdir -p "$WT_A1"
write_state_yml "${STATE_DIR}/1700000001-foo.yml" "true" "$WT_A1" "1700000001-foo"

A1_OUT="$(bash "$LOCATE_SCRIPT" "$TMP" 2>/dev/null || echo "")"
A1_EXIT=0
bash "$LOCATE_SCRIPT" "$TMP" >/dev/null 2>&1 || A1_EXIT=$?

assert "A1 locate exit 1（策略 5 已删，主仓 cwd 不再自动绑）" "[ $A1_EXIT -eq 1 ]"
assert "A1 stdout 空" "[ -z '$A1_OUT' ]"

# A1 顺验：locate 静默契约（不能往 stderr 喷）
A1_STDERR="$(bash "$LOCATE_SCRIPT" "$TMP" 2>&1 >/dev/null || true)"
assert "A1 stderr 为空（locate 静默契约）" "[ -z '$A1_STDERR' ]"

# 清理 A1
rm -f "${STATE_DIR}"/*.yml
rm -rf "${TMP}/.claude/worktrees"

# ============================================================
# A2: 主仓 cwd + 2 active worktree state → 返回空
#     多 active 场景，V2.4 就不绑，V3.2 删策略 5 后更不会绑
# ============================================================
echo ""
echo "--- A2: 主仓 cwd + 2 active worktree state → 返回空 ---"
WT_A2_1="${TMP}/.claude/worktrees/1700000002-bar"
WT_A2_2="${TMP}/.claude/worktrees/1700000003-baz"
mkdir -p "$WT_A2_1" "$WT_A2_2"
write_state_yml "${STATE_DIR}/1700000002-bar.yml" "true" "$WT_A2_1" "1700000002-bar"
write_state_yml "${STATE_DIR}/1700000003-baz.yml" "true" "$WT_A2_2" "1700000003-baz"

A2_OUT="$(bash "$LOCATE_SCRIPT" "$TMP" 2>/dev/null || echo "")"
A2_EXIT=0
bash "$LOCATE_SCRIPT" "$TMP" >/dev/null 2>&1 || A2_EXIT=$?

assert "A2 locate exit 1（多 active 不绑）" "[ $A2_EXIT -eq 1 ]"
assert "A2 stdout 空" "[ -z '$A2_OUT' ]"

# 清理 A2
rm -f "${STATE_DIR}"/*.yml
rm -rf "${TMP}/.claude/worktrees"

# ============================================================
# A3: cwd 在 worktree 内 → 策略 2 命中（不受策略 5 删除影响）
# ============================================================
echo ""
echo "--- A3: worktree cwd → 策略 2 命中 ---"
WT_A3="${TMP}/.claude/worktrees/1700000004-qux"
mkdir -p "$WT_A3"
write_state_yml "${STATE_DIR}/1700000004-qux.yml" "true" "$WT_A3" "1700000004-qux"

A3_OUT="$(bash "$LOCATE_SCRIPT" "$WT_A3" 2>/dev/null || echo "")"
A3_EXIT=0
bash "$LOCATE_SCRIPT" "$WT_A3" >/dev/null 2>&1 || A3_EXIT=$?

assert "A3 locate exit 0（策略 2 命中）" "[ $A3_EXIT -eq 0 ]"
assert "A3 stdout = state 文件路径" "[ '$A3_OUT' = '${STATE_DIR}/1700000004-qux.yml' ]"

# 清理 A3
rm -f "${STATE_DIR}"/*.yml
rm -rf "${TMP}/.claude/worktrees"

# ============================================================
# A4: __main__.yml 存在 → 策略 4 兜底命中
# ============================================================
echo ""
echo "--- A4: __main__.yml 存在 → 策略 4 兜底命中 ---"
write_state_yml "${STATE_DIR}/__main__.yml" "true" "$TMP" "__main__"

A4_OUT="$(bash "$LOCATE_SCRIPT" "$TMP" 2>/dev/null || echo "")"
A4_EXIT=0
bash "$LOCATE_SCRIPT" "$TMP" >/dev/null 2>&1 || A4_EXIT=$?

assert "A4 locate exit 0（策略 4 命中）" "[ $A4_EXIT -eq 0 ]"
assert "A4 stdout = __main__.yml" "[ '$A4_OUT' = '${STATE_DIR}/__main__.yml' ]"

# 清理 A4
rm -f "${STATE_DIR}"/*.yml

# ============================================================
# A5: 策略 3 — worktree_path 匹配 cwd → 命中
# ============================================================
echo ""
echo "--- A5: worktree_path 匹配 cwd → 策略 3 命中 ---"
WT_A5="${TMP}/external-worktree-dir"
mkdir -p "$WT_A5"
write_state_yml "${STATE_DIR}/1700000005-ext.yml" "true" "$WT_A5" "1700000005-ext"

A5_OUT="$(bash "$LOCATE_SCRIPT" "$WT_A5" 2>/dev/null || echo "")"
A5_EXIT=0
bash "$LOCATE_SCRIPT" "$WT_A5" >/dev/null 2>&1 || A5_EXIT=$?

# 策略 3 要求 cwd 在 PROJECT_ROOT 内（从 cwd 向上找 loop.yml）
# external-worktree-dir 在 $TMP 下，而 $TMP 有 .claude/loop.yml
assert "A5 locate exit 0（策略 3 命中）" "[ $A5_EXIT -eq 0 ]"
assert "A5 stdout = state 文件路径" "[ '$A5_OUT' = '${STATE_DIR}/1700000005-ext.yml' ]"

# 清理 A5
rm -f "${STATE_DIR}"/*.yml
rm -rf "$WT_A5"

# ============================================================
# 汇总
# ============================================================
echo ""
echo "=========================================="
echo "PASS: $PASS  FAIL: $FAIL"
echo "=========================================="
[ "$FAIL" -eq 0 ]
