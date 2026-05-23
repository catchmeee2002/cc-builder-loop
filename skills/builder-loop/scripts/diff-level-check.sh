#!/usr/bin/env bash
# diff-level-check.sh — 扫 diff 中新增的函数/类/文件，输出 L3 信号供 builder 逐项回应
#
# 用法：bash diff-level-check.sh [project_root] [diff_base]
# 退出码：0=无 L3 信号  1=有 L3 信号需要 builder 回应

set -uo pipefail

PROJECT_ROOT="${1:-$(pwd)}"
DIFF_BASE="${2:-HEAD~1}"

if ! git -C "$PROJECT_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo '{"level_signals":[],"count":0}'
  exit 0
fi

SIGNALS=""
COUNT=0

add_signal() {
  local sig="$1"
  if [ -z "$SIGNALS" ]; then
    SIGNALS="\"$sig\""
  else
    SIGNALS="${SIGNALS},\"$sig\""
  fi
  COUNT=$((COUNT + 1))
}

# ---- 1. 新增文件（排除测试）----
NEW_FILES="$(git -C "$PROJECT_ROOT" diff "$DIFF_BASE" --diff-filter=A --name-only 2>/dev/null || true)"
if [ -n "$NEW_FILES" ]; then
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    case "$f" in
      test_*|tests/*|**/test_*|*_test.*|*.test.*) continue ;;
      *__init__*) continue ;;
    esac
    add_signal "new file: $f"
  done <<< "$NEW_FILES"
fi

# ---- 2. 新增的函数/类定义（排除测试文件）----
DIFF_OUTPUT="$(git -C "$PROJECT_ROOT" diff "$DIFF_BASE" -U0 2>/dev/null || true)"
if [ -n "$DIFF_OUTPUT" ]; then
  CURRENT_FILE=""
  while IFS= read -r line; do
    case "$line" in
      "diff --git a/"*)
        CURRENT_FILE="$(echo "$line" | sed 's|^diff --git a/.* b/||')"
        case "$CURRENT_FILE" in
          test_*|tests/*|**/test_*|*_test.*|*.test.*) CURRENT_FILE="" ;;
        esac
        ;;
      "+def "*)
        [ -z "$CURRENT_FILE" ] && continue
        FNAME="$(echo "$line" | sed -n 's/^+[[:space:]]*def \([a-zA-Z_][a-zA-Z0-9_]*\).*/\1/p')"
        [ -z "$FNAME" ] && continue
        [ "$FNAME" = "__init__" ] && continue
        [ "$FNAME" = "__str__" ] && continue
        [ "$FNAME" = "__repr__" ] && continue
        add_signal "def $FNAME ($CURRENT_FILE)"
        ;;
      "+class "*)
        [ -z "$CURRENT_FILE" ] && continue
        CNAME="$(echo "$line" | sed -n 's/^+[[:space:]]*class \([a-zA-Z_][a-zA-Z0-9_]*\).*/\1/p')"
        [ -z "$CNAME" ] && continue
        add_signal "class $CNAME ($CURRENT_FILE)"
        ;;
      "+function "*)
        [ -z "$CURRENT_FILE" ] && continue
        FNAME="$(echo "$line" | sed -n 's/^+[[:space:]]*function \([a-zA-Z_][a-zA-Z0-9_]*\).*/\1/p')"
        [ -z "$FNAME" ] && continue
        add_signal "function $FNAME ($CURRENT_FILE)"
        ;;
      "+"*"() {"*)
        [ -z "$CURRENT_FILE" ] && continue
        FNAME="$(echo "$line" | sed -n 's/^+\([a-zA-Z_][a-zA-Z0-9_]*\)()[[:space:]]*{.*/\1/p')"
        [ -z "$FNAME" ] && continue
        add_signal "$FNAME() ($CURRENT_FILE)"
        ;;
    esac
  done <<< "$DIFF_OUTPUT"
fi

echo "{\"level_signals\":[${SIGNALS}],\"count\":${COUNT}}"

if [ "$COUNT" -gt 0 ]; then
  exit 1
fi
exit 0
