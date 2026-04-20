#!/usr/bin/env bash
# test-arbitration-apply.sh — 验证 run-apply-arbitration.sh 三种场景
#
# 场景：
#   1. 信心 high + 有效 patch → APPLIED (exit 0)
#   2. 信心 low  → LOW_CONFIDENCE (exit 1)
#   3. 信心 high + 坏 patch → APPLY_FAILED (exit 2)
#
# 用法：bash test-arbitration-apply.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPLY_SCRIPT="${SCRIPT_DIR}/../../scripts/run-apply-arbitration.sh"

[ -f "$APPLY_SCRIPT" ] || { echo "FAIL: run-apply-arbitration.sh not found" >&2; exit 1; }

TMPDIR="$(mktemp -d -t builder-loop-arb-XXXXXX)"
trap 'rm -rf "$TMPDIR"' EXIT

PASS_COUNT=0
FAIL_COUNT=0

# === 构建基础临时仓（三个场景共享） ===
setup_repo() {
  local repo="$1"
  mkdir -p "$repo"
  cd "$repo"
  git init -q
  git -c core.hooksPath=/dev/null commit -q --allow-empty -m "root"
  echo "line1-original" > shared.txt
  git add shared.txt
  git -c core.hooksPath=/dev/null commit -q -m "add shared.txt"
  MAIN_HEAD="$(git rev-parse --short HEAD)"

  # 创建 worktree
  mkdir -p .claude/worktrees
  git worktree add -q .claude/worktrees/test-wt -b loop/test-wt

  # worktree 改文件
  echo "worktree-change" > .claude/worktrees/test-wt/shared.txt
  git -C .claude/worktrees/test-wt add shared.txt
  git -C .claude/worktrees/test-wt -c core.hooksPath=/dev/null commit -q -m "wt edit"

  # 主干也改文件（制造冲突）
  echo "main-change" > shared.txt
  git add shared.txt
  git -c core.hooksPath=/dev/null commit -q -m "main edit"

  # 写 loop.yml（auto_apply_confidence: medium）
  mkdir -p .claude
  cat > .claude/loop.yml <<'LYML'
pass_cmd:
  - { stage: test, cmd: "echo ok", timeout: 30 }
arbitration:
  auto_apply_confidence: medium
  max_attempts: 2
LYML

  # 写 state file
  cat > .claude/builder-loop.local.md <<STEOF
active: true
iter: 1
max_iter: 3
project_root: "${repo}"
start_head: "${MAIN_HEAD}"
worktree_path: "${repo}/.claude/worktrees/test-wt"
need_arbitration: true
conflict_files: "shared.txt"
task_description: "arbitration test"
source_dirs: ""
test_dirs: ""
last_error_hash: ""
last_error_count: 0
stopped_reason: ""
STEOF
  echo "$MAIN_HEAD"
}

# =============================================
# 场景 1：信心 high + 有效 patch → APPLIED
# =============================================
echo "=== 场景 1：信心 high + 有效 patch ==="
REPO1="${TMPDIR}/repo1"
setup_repo "$REPO1" > /dev/null
STATE1="${REPO1}/.claude/builder-loop.local.md"
ARB_OUT1="${TMPDIR}/arb-out-1.txt"

# 构造 mock arbiter 输出：信心 high + 有效 patch
# patch 内容：把 shared.txt 从冲突态改为 resolved
cat > "$ARB_OUT1" <<'ARBEOF'
# 仲裁报告

## 冲突概览
- 文件数：1
- 冲突块：1
- 总体信心：high

## Patch
ARBITER_PATCH_BEGIN
--- a/shared.txt
+++ b/shared.txt
@@ -1 +1 @@
-main-change
+resolved-by-arbiter
ARBITER_PATCH_END

ARBITER_SUMMARY: 已解决 1 处冲突 | 关键决策: 合并两侧改动 | 信心: high
ARBEOF

EC=0
RESULT="$(bash "$APPLY_SCRIPT" "$STATE1" "$ARB_OUT1" 2>/dev/null)" || EC=$?
LAST="$(echo "$RESULT" | tail -1)"
if [ "$EC" -eq 0 ] && [ "$LAST" = "APPLIED" ]; then
  echo "  ✓ PASS: exit=$EC, result=$LAST"
  PASS_COUNT=$((PASS_COUNT + 1))
else
  echo "  ✗ FAIL: 期望 exit=0/APPLIED，实际 exit=$EC/$LAST"
  FAIL_COUNT=$((FAIL_COUNT + 1))
fi

# =============================================
# 场景 2：信心 low → LOW_CONFIDENCE
# =============================================
echo "=== 场景 2：信心 low → LOW_CONFIDENCE ==="
REPO2="${TMPDIR}/repo2"
setup_repo "$REPO2" > /dev/null
STATE2="${REPO2}/.claude/builder-loop.local.md"
ARB_OUT2="${TMPDIR}/arb-out-2.txt"

cat > "$ARB_OUT2" <<'ARBEOF'
# 仲裁报告

## 冲突概览
- 文件数：1
- 冲突块：1
- 总体信心：low

ARBITER_SUMMARY: 无法自动仲裁，原因: 接口签名冲突 | 信心: low
ARBEOF

EC=0
RESULT="$(bash "$APPLY_SCRIPT" "$STATE2" "$ARB_OUT2" 2>/dev/null)" || EC=$?
LAST="$(echo "$RESULT" | tail -1)"
if [ "$EC" -eq 1 ] && [ "$LAST" = "LOW_CONFIDENCE" ]; then
  echo "  ✓ PASS: exit=$EC, result=$LAST"
  PASS_COUNT=$((PASS_COUNT + 1))
else
  echo "  ✗ FAIL: 期望 exit=1/LOW_CONFIDENCE，实际 exit=$EC/$LAST"
  FAIL_COUNT=$((FAIL_COUNT + 1))
fi

# =============================================
# 场景 3：信心 high + 坏 patch → APPLY_FAILED
# =============================================
echo "=== 场景 3：信心 high + 坏 patch ==="
REPO3="${TMPDIR}/repo3"
setup_repo "$REPO3" > /dev/null
STATE3="${REPO3}/.claude/builder-loop.local.md"
ARB_OUT3="${TMPDIR}/arb-out-3.txt"

# 构造无效 patch（引用不存在的文件）
cat > "$ARB_OUT3" <<'ARBEOF'
# 仲裁报告
ARBITER_PATCH_BEGIN
--- a/nonexistent-file.txt
+++ b/nonexistent-file.txt
@@ -1 +1 @@
-old
+new
ARBITER_PATCH_END

ARBITER_SUMMARY: 已解决 1 处冲突 | 信心: high
ARBEOF

EC=0
RESULT="$(bash "$APPLY_SCRIPT" "$STATE3" "$ARB_OUT3" 2>/dev/null)" || EC=$?
LAST="$(echo "$RESULT" | tail -1)"
if [ "$EC" -eq 2 ] && [ "$LAST" = "APPLY_FAILED" ]; then
  echo "  ✓ PASS: exit=$EC, result=$LAST"
  PASS_COUNT=$((PASS_COUNT + 1))
else
  echo "  ✗ FAIL: 期望 exit=2/APPLY_FAILED，实际 exit=$EC/$LAST"
  FAIL_COUNT=$((FAIL_COUNT + 1))
fi

# =============================================
# 汇总
# =============================================
echo ""
echo "--- 汇总: ${PASS_COUNT} PASS / ${FAIL_COUNT} FAIL ---"
if [ "$FAIL_COUNT" -gt 0 ]; then
  echo "❌ FAIL: 有 ${FAIL_COUNT} 个场景未通过"
  exit 1
fi
echo "✅ PASS: 全部 3 个场景通过"
exit 0
