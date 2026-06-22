#!/usr/bin/env bash
# loop-commit.sh — PASS 后在 project_root 内 commit（统一 bare/worktree 两种模式）
#
# 用法：bash loop-commit.sh <state_file>
#
# 输出（stdout 最后一行为决定性结果）：
#   COMMIT_DONE <new_head>  ← 有改动并已 commit，输出新 HEAD short sha
#   NOOP                    ← project_root 内无改动（git status 空）
#   ERROR <reason>          ← commit 失败 / state 字段缺失（exit 3）
#
# 退出码：0=COMMIT_DONE/NOOP  3=ERROR
#
# 副作用：
#   - 仅 `git -C $PROJECT_ROOT add -A && git -C $PROJECT_ROOT commit`
#   - **不 ff merge**、**不删 worktree**
#   - reviewer 通过后由 merge-and-cleanup.sh 处理后续
#
# 依赖 state 字段：project_root / slug / task_description / iter / pre_loop_dirty_files

set -euo pipefail

STATE="${1:?state file path required}"
[ -f "$STATE" ] || { echo "ERROR state-not-found"; exit 3; }

read_field() {
  grep -E "^${1}:" "$STATE" 2>/dev/null | head -1 | sed -E "s/^${1}:[[:space:]]*\"?([^\"]*)\"?[[:space:]]*\$/\1/" || true
}

PROJECT_ROOT="$(read_field project_root)"
if [ -z "$PROJECT_ROOT" ] || [ ! -d "$PROJECT_ROOT" ]; then
  echo "ERROR project-root-missing"
  exit 3
fi

PRE_LOOP_DIRTY_FILES="$(read_field pre_loop_dirty_files)"

STATUS="$(git -C "$PROJECT_ROOT" status --porcelain 2>/dev/null || echo "")"
if [ -z "$STATUS" ]; then
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

git -C "$PROJECT_ROOT" add -A >&2
if ! printf '%s\n' "$COMMIT_MSG" | git -C "$PROJECT_ROOT" commit -F - >&2; then
  echo "ERROR commit-failed"
  exit 3
fi

NEW_HEAD="$(git -C "$PROJECT_ROOT" rev-parse --short HEAD 2>/dev/null || echo "")"
echo "COMMIT_DONE ${NEW_HEAD}"
exit 0
