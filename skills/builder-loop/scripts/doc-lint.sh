#!/usr/bin/env bash
# doc-lint.sh — 检测 diff 中删除的文件/函数是否在 .md 中有过时引用
#
# 用法：bash doc-lint.sh [project_root] [diff_base]
#   project_root: 项目根目录（默认 CWD）
#   diff_base:    git diff 基准（默认 HEAD~1）
#
# 退出码：0=无过时引用  1=有过时引用  2=参数错误

set -uo pipefail

PROJECT_ROOT="${1:-$(pwd)}"
DIFF_BASE="${2:-HEAD~1}"

if [ ! -d "$PROJECT_ROOT/.git" ] && ! git -C "$PROJECT_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "[doc-lint] 非 git 仓库，跳过" >&2
  exit 0
fi

# ---- 1. 提取删除的文件 ----
DELETED_FILES="$(git -C "$PROJECT_ROOT" diff "$DIFF_BASE" --diff-filter=D --name-only 2>/dev/null || true)"

# ---- 2. 提取删除的函数/方法名 ----
DELETED_SYMBOLS=""
DIFF_OUTPUT="$(git -C "$PROJECT_ROOT" diff "$DIFF_BASE" -U0 2>/dev/null || true)"
if [ -n "$DIFF_OUTPUT" ]; then
  DELETED_SYMBOLS="$(echo "$DIFF_OUTPUT" \
    | grep -E '^-' \
    | grep -v '^---' \
    | sed -n \
        -e 's/^-[[:space:]]*def \([a-zA-Z_][a-zA-Z0-9_]*\).*/\1/p' \
        -e 's/^-[[:space:]]*class \([a-zA-Z_][a-zA-Z0-9_]*\).*/\1/p' \
        -e 's/^-\([a-zA-Z_][a-zA-Z0-9_]*\)()[[:space:]]*{.*/\1/p' \
        -e 's/^-[[:space:]]*function \([a-zA-Z_][a-zA-Z0-9_]*\).*/\1/p' \
    | sort -u || true)"
fi

# ---- 3. 过滤太短/太通用的符号（≤3 字符大概率误报）----
PATTERNS=""
if [ -n "$DELETED_FILES" ]; then
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    base="$(basename "$f")"
    [ ${#base} -le 3 ] && continue
    PATTERNS="${PATTERNS}${base}\n"
  done <<< "$DELETED_FILES"
fi
if [ -n "$DELETED_SYMBOLS" ]; then
  while IFS= read -r s; do
    [ -z "$s" ] && continue
    [ ${#s} -le 3 ] && continue
    # 排除太通用的名字
    case "$s" in main|test|setup|init|run|get|set|put|delete|update|read|write|open|close|help|info|data|args|self|name|path|file|line|node|item|list|text|body|head|tail|keys|sort|load|save|send|recv|exec|call|done|next|step|stop|exit|true|pass|fail|skip|type|mode|size|code|hash|push|pull|drop|copy|move|find|show|hide|make|each|join|trim|lock|free|wait|kill) continue ;; esac
    PATTERNS="${PATTERNS}${s}\n"
  done <<< "$DELETED_SYMBOLS"
fi

if [ -z "$PATTERNS" ]; then
  echo "[doc-lint] 无删除的文件或函数，跳过"
  exit 0
fi

# ---- 4. 在 .md 文件中搜索过时引用 ----
MD_FILES="$(find "$PROJECT_ROOT" -name '*.md' -type f \
  -not -path '*/.git/*' \
  -not -path '*/node_modules/*' \
  -not -path '*/.claude/plans/*' \
  -not -path '*/.claude/builder-loop/legacy/*' \
  2>/dev/null || true)"

if [ -z "$MD_FILES" ]; then
  echo "[doc-lint] 无 .md 文件，跳过"
  exit 0
fi

HITS=""
HIT_COUNT=0

while IFS= read -r pattern; do
  [ -z "$pattern" ] && continue
  while IFS= read -r md; do
    [ -z "$md" ] && continue
    MATCHES="$(grep -n "$pattern" "$md" 2>/dev/null || true)"
    if [ -n "$MATCHES" ]; then
      REL="$(echo "$md" | sed "s|^${PROJECT_ROOT}/||")"
      while IFS= read -r match_line; do
        [ -z "$match_line" ] && continue
        LINE_NUM="$(echo "$match_line" | cut -d: -f1)"
        LINE_CONTENT="$(echo "$match_line" | cut -d: -f2- | sed 's/^[[:space:]]*//' | head -c 120)"
        HITS="${HITS}  ${REL}:${LINE_NUM}: 提到已删除的 \`${pattern}\` — ${LINE_CONTENT}\n"
        HIT_COUNT=$((HIT_COUNT + 1))
      done <<< "$MATCHES"
    fi
  done <<< "$MD_FILES"
done < <(printf '%b' "$PATTERNS")

# ---- 5. 输出结果 ----
if [ "$HIT_COUNT" -gt 0 ]; then
  echo "[doc-lint] 检测到 ${HIT_COUNT} 处过时文档引用："
  printf '%b' "$HITS"
  echo "[doc-lint] 请更新文档后重试"
  exit 1
fi

echo "[doc-lint] 无过时文档引用"
exit 0
