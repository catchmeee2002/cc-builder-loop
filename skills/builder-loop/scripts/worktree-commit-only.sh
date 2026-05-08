#!/usr/bin/env bash
# worktree-commit-only.sh — V3.0 reviewer-as-gate：PASS 后只在 worktree 内 commit
#
# 用法：bash worktree-commit-only.sh <state_file>
#
# 输出（stdout 最后一行为决定性结果）：
#   COMMIT_DONE <new_head>  ← 有改动并已 commit，输出新 HEAD short sha
#   NOOP                    ← worktree 内无改动（worktree 未启用或 git status 空）
#   ERROR <reason>          ← commit 失败 / state 字段缺失（exit 3）
#
# 退出码：0=COMMIT_DONE/NOOP  3=ERROR
#
# 副作用：
#   - 仅 `git -C $WORKTREE_PATH add -A && git -C $WORKTREE_PATH commit`
#   - **不动主仓**、**不 ff merge**、**不删 worktree**
#   - reviewer 通过后由 merge-and-cleanup.sh 处理 ff merge + cleanup
#
# 依赖 state 字段：worktree_path / slug / task_description / iter / pre_loop_dirty_files

set -euo pipefail

STATE="${1:?state file path required}"
[ -f "$STATE" ] || { echo "ERROR state-not-found"; exit 3; }

read_field() {
  grep -E "^${1}:" "$STATE" 2>/dev/null | head -1 | sed -E "s/^${1}:[[:space:]]*\"?([^\"]*)\"?[[:space:]]*\$/\1/" || true
}

WORKTREE_PATH="$(read_field worktree_path)"
SLUG_FIELD="$(read_field slug)"
PRE_LOOP_DIRTY_FILES="$(read_field pre_loop_dirty_files)"

# bare 模式或 worktree 未启用 → NOOP（bare 模式 commit 在 hook PASS 路径或 merge-worktree-back.sh 处理）
if [ -z "$WORKTREE_PATH" ] || [ ! -d "$WORKTREE_PATH" ]; then
  echo "NOOP"
  exit 0
fi

WT_STATUS="$(git -C "$WORKTREE_PATH" status --porcelain 2>/dev/null || echo "")"
if [ -z "$WT_STATUS" ]; then
  echo "NOOP"
  exit 0
fi

ITER_NUM="$(grep -E '^iter:' "$STATE" | head -1 | awk '{print $2}')"
ITER_NUM="${ITER_NUM:-0}"
TASK_DESC="$(awk '/^task_description: \|/{getline; sub(/^[[:space:]]+/, ""); print; exit}' "$STATE" 2>/dev/null || echo "")"
if [ -n "$TASK_DESC" ]; then
  COMMIT_SUBJECT="chore(loop): [cr_id_skip] Auto-commit ${TASK_DESC}"
else
  COMMIT_SUBJECT="chore(loop): [cr_id_skip] Auto-commit iter ${ITER_NUM}"
fi

# V2.3 dirty stash 兼容：subject 加 [+N main-dirty] 标记 + body 列文件清单
DIRTY_BODY=""
if [ -n "$PRE_LOOP_DIRTY_FILES" ]; then
  DIRTY_COUNT="$(printf '%s' "$PRE_LOOP_DIRTY_FILES" | tr ',' '\n' | grep -c . || true)"
  if [ "$DIRTY_COUNT" -gt 0 ]; then
    COMMIT_SUBJECT="${COMMIT_SUBJECT} [+${DIRTY_COUNT} main-dirty]"
    DIRTY_BODY=$'\n\n含主仓预存改动（V2.3 dirty stash apply）：'
    while IFS= read -r _f; do
      [ -n "$_f" ] && DIRTY_BODY="${DIRTY_BODY}"$'\n'"  - ${_f}"
    done < <(printf '%s' "$PRE_LOOP_DIRTY_FILES" | tr ',' '\n')
  fi
fi
COMMIT_MSG="${COMMIT_SUBJECT}${DIRTY_BODY}"

git -C "$WORKTREE_PATH" add -A >&2
if ! printf '%s\n' "$COMMIT_MSG" | git -C "$WORKTREE_PATH" commit -F - >&2; then
  echo "ERROR commit-failed"
  exit 3
fi

NEW_HEAD="$(git -C "$WORKTREE_PATH" rev-parse --short HEAD 2>/dev/null || echo "")"
echo "COMMIT_DONE ${NEW_HEAD}"
exit 0
