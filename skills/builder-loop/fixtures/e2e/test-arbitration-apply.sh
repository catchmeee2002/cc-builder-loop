#!/usr/bin/env bash
# test-arbitration-apply.sh — 验证 run-apply-arbitration.sh 三种场景
#
# 场景：
#   1. 信心 high + 有效 patch → APPLIED (exit 0)
#   2. 信心 low  → LOW_CONFIDENCE (exit 1)
#   3. 信心 high + 坏 patch → APPLY_FAILED (exit 2)
#
# 用法：bash test-arbitration-apply.sh

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "run-apply-arbitration 三种场景"

APPLY_SCRIPT="${HARNESS_REPO_ROOT}/skills/builder-loop/scripts/run-apply-arbitration.sh"
assert "run-apply-arbitration.sh 存在" "[ -f '$APPLY_SCRIPT' ]"

TMPDIR="$(mktemp -d -t builder-loop-arb-XXXXXX)"
_HARNESS_TMPDIRS+=("$TMPDIR")

# === 构建基础临时仓（三个场景共享） ===
setup_repo() {
  local repo="$1"
  mkdir -p "$repo"
  (
    cd "$repo"
    git init -q
    git config user.email "harness@test.local"
    git config user.name "harness"
    git -c core.hooksPath=/dev/null commit -q --allow-empty -m "chore(test): [cr_id_skip] Root"
    echo "line1-original" > shared.txt
    git add shared.txt
    git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Add shared.txt"
  )
  MAIN_HEAD="$(git -C "$repo" rev-parse --short HEAD)"

  # 创建 worktree
  mkdir -p "$repo/.claude/worktrees"
  git -C "$repo" worktree add -q "$repo/.claude/worktrees/test-wt" -b loop/test-wt

  # worktree 改文件
  echo "worktree-change" > "$repo/.claude/worktrees/test-wt/shared.txt"
  git -C "$repo/.claude/worktrees/test-wt" add shared.txt
  git -C "$repo/.claude/worktrees/test-wt" -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Wt edit"

  # 主干也改文件（制造冲突）
  echo "main-change" > "$repo/shared.txt"
  git -C "$repo" add shared.txt
  git -C "$repo" -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Main edit"

  # 写 loop.yml
  mkdir -p "$repo/.claude"
  cat > "$repo/.claude/loop.yml" <<'LYML'
pass_cmd:
  - { stage: test, cmd: "echo ok", timeout: 30 }
arbitration:
  auto_apply_confidence: medium
  max_attempts: 2
LYML

  # 写 state file
  mkdir -p "$repo/.claude/builder-loop/state"
  cat > "$repo/.claude/builder-loop/state/test-wt.yml" <<STEOF
slug: "test-wt"
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
section "场景 1: 信心 high + 有效 patch"
REPO1="${TMPDIR}/repo1"
setup_repo "$REPO1" > /dev/null
STATE1="${REPO1}/.claude/builder-loop/state/test-wt.yml"
ARB_OUT1="${TMPDIR}/arb-out-1.txt"

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
assert "场景1: exit=0, result=APPLIED" "[ '$EC' -eq 0 ] && [ '$LAST' = 'APPLIED' ]"

# =============================================
# 场景 2：信心 low → LOW_CONFIDENCE
# =============================================
section "场景 2: 信心 low"
REPO2="${TMPDIR}/repo2"
setup_repo "$REPO2" > /dev/null
STATE2="${REPO2}/.claude/builder-loop/state/test-wt.yml"
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
assert "场景2: exit=1, result=LOW_CONFIDENCE" "[ '$EC' -eq 1 ] && [ '$LAST' = 'LOW_CONFIDENCE' ]"

# =============================================
# 场景 3：信心 high + 坏 patch → APPLY_FAILED
# =============================================
section "场景 3: 信心 high + 坏 patch"
REPO3="${TMPDIR}/repo3"
setup_repo "$REPO3" > /dev/null
STATE3="${REPO3}/.claude/builder-loop/state/test-wt.yml"
ARB_OUT3="${TMPDIR}/arb-out-3.txt"

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
assert "场景3: exit=2, result=APPLY_FAILED" "[ '$EC' -eq 2 ] && [ '$LAST' = 'APPLY_FAILED' ]"

harness_report
