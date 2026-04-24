#!/usr/bin/env bash
# locate-state.sh — 从 CWD 找到对应的 builder-loop state 文件
#
# 用法：
#   STATE_FILE="$(bash locate-state.sh [cwd])"
#   [ -n "$STATE_FILE" ] || echo "未找到"
#
# 默认 cwd = $PWD。
#
# 定位策略（按优先级）：
#   1. 向上最多 5 层找 .claude/loop.yml → 锚定 PROJECT_ROOT
#   2. 若 cwd 在 <PROJECT_ROOT>/.claude/worktrees/<slug>/ 下 → 直接拼 state/<slug>.yml
#   3. 否则遍历 <PROJECT_ROOT>/.claude/builder-loop/state/*.yml，
#      比对 worktree_path 字段是否等于 cwd（或 cwd 落在其下）
#   4. 兜底 <PROJECT_ROOT>/.claude/builder-loop/state/__main__.yml（bare loop 场景）
#
# 输出：state 文件绝对路径（stdout，单行）；未找到 → 空 + exit 1。
#
# 静默错误（不向 stderr 喷）：所有 hook 要频繁调用，保持安静。

set -uo pipefail

CWD="${1:-$PWD}"
# 归一化 cwd
if [ -d "$CWD" ]; then
  CWD="$(cd "$CWD" && pwd -P)"
fi

# --- 1. 找 PROJECT_ROOT ---
# 注意：git worktree 会把 .claude/ 一并继承（包括 loop.yml），所以 worktree 目录里
# 也有 .claude/loop.yml。若 cwd 在 <P>/.claude/worktrees/<slug>/... 形态下，
# 先把 P 取出来作为真正的 PROJECT_ROOT，再 fallback 向上找 loop.yml。
find_project_root() {
  local dir="$1" i=0
  # ① cwd 路径里含 /.claude/worktrees/ → 截断到之前
  case "$dir" in
    */.claude/worktrees/*)
      # P = dir 截到 `/.claude/worktrees/` 之前
      local p="${dir%%/.claude/worktrees/*}"
      if [ -f "${p}/.claude/loop.yml" ]; then
        echo "$p"
        return 0
      fi
      ;;
  esac
  # ② 普通向上查（主项目内）
  while [ "$i" -lt 5 ]; do
    if [ -f "${dir}/.claude/loop.yml" ]; then
      echo "$dir"
      return 0
    fi
    [ "$dir" = "/" ] && return 1
    dir="$(dirname "$dir")"
    i=$(( i + 1 ))
  done
  return 1
}

PROJECT_ROOT="$(find_project_root "$CWD" || echo "")"
[ -z "$PROJECT_ROOT" ] && exit 1

STATE_DIR="${PROJECT_ROOT}/.claude/builder-loop/state"

# --- 2. cwd 在 <PROJECT_ROOT>/.claude/worktrees/<slug>/ 下时直接拼 ---
WT_ROOT="${PROJECT_ROOT}/.claude/worktrees"
case "$CWD" in
  "${WT_ROOT}"/*)
    # 提取 worktrees/ 之后的第一段作为 slug
    rel="${CWD#${WT_ROOT}/}"
    slug="${rel%%/*}"
    candidate="${STATE_DIR}/${slug}.yml"
    if [ -f "$candidate" ]; then
      echo "$candidate"
      exit 0
    fi
    ;;
esac

# --- 3. 遍历 state/*.yml 找 worktree_path 匹配 cwd ---
if [ -d "$STATE_DIR" ]; then
  for sf in "$STATE_DIR"/*.yml; do
    [ -e "$sf" ] || continue
    wt="$(grep -E '^worktree_path:' "$sf" 2>/dev/null | head -1 | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/')"
    [ -z "$wt" ] && continue
    # 归一化
    if [ -d "$wt" ]; then
      wt_abs="$(cd "$wt" && pwd -P 2>/dev/null || echo "$wt")"
    else
      wt_abs="$wt"
    fi
    # 精确匹配 or cwd 在 wt 下
    if [ "$CWD" = "$wt_abs" ] || case "$CWD" in "$wt_abs"/*) true;; *) false;; esac; then
      echo "$sf"
      exit 0
    fi
  done
fi

# --- 4. 兜底 __main__.yml（bare loop） ---
MAIN_STATE="${STATE_DIR}/__main__.yml"
if [ -f "$MAIN_STATE" ]; then
  echo "$MAIN_STATE"
  exit 0
fi

exit 1
