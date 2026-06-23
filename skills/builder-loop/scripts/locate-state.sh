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
#      [V3.0 reviewer-as-gate 主信号源] cwd 含 worktrees/<slug> 时 slug 推断 100% 准确，
#      不依赖 session_id / 不需要遍历 state，是跨 session 隔离的核心机制：
#      builder cwd 在 worktree 内 → 推出本 session 自己的 slug → 找对应 state；
#      其他 session cwd 不在 worktrees 子目录 → 走策略 3-5，自然不会绑到本 slug。
#   3. 否则遍历 <PROJECT_ROOT>/.claude/builder-loop/state/*.yml，
#      比对 worktree_path 字段是否等于 cwd（或 cwd 落在其下）
#   4. V3.4: bare 模式 owner_cwd 精确匹配（worktree_path 空 + owner_cwd == cwd）
#   4b. 兜底 <PROJECT_ROOT>/.claude/builder-loop/state/__main__.yml（bare loop）
#   5. V3.4 恢复：cwd 不在 worktree 子目录 → 扫 state/*.yml 找
#      phase=active 且 worktree_path 目录存活的 state，**恰好 1 个时**自动绑定；
#      ≥2 个时返回空（保多状态隔离）
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

# --- 4. V3.4: bare 模式 owner_cwd 精确匹配（优先于 __main__.yml 兜底）---
# worktree_path 为空 + owner_cwd == CWD → bare 模式的 state
if [ -d "$STATE_DIR" ]; then
  for sf in "$STATE_DIR"/*.yml; do
    [ -e "$sf" ] || continue
    _wt="$(grep -E '^worktree_path:' "$sf" 2>/dev/null | head -1 | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?.*/\1/' || true)"
    [ -n "$_wt" ] && continue
    _oc="$(grep -E '^owner_cwd:' "$sf" 2>/dev/null | head -1 | sed -E 's/^owner_cwd:[[:space:]]*"?([^"]*)"?.*/\1/' || true)"
    if [ "$_oc" = "$CWD" ]; then
      echo "$sf"
      exit 0
    fi
  done
fi

# --- 4b. 兜底 __main__.yml（bare loop，owner_cwd 无匹配时）---
MAIN_STATE="${STATE_DIR}/__main__.yml"
if [ -f "$MAIN_STATE" ]; then
  echo "$MAIN_STATE"
  exit 0
fi

# --- 5. V3.4 恢复：唯一 active/pending worktree 自动绑定 ---
# CWD 在主仓但有 worktree loop 时兜底。恰好 1 个 → 绑定；≥2 个 → 不绑（保隔离）
# V3.7: 匹配 active + e2e_pending + passed_pending_review（均为存活 loop，subagent 需受约束）
if [ -d "$STATE_DIR" ]; then
  _active_count=0
  _active_sf=""
  for sf in "$STATE_DIR"/*.yml; do
    [ -e "$sf" ] || continue
    _phase="$(grep -E '^phase:' "$sf" 2>/dev/null | head -1 | sed -E 's/^phase:[[:space:]]*"?([^"]*)"?.*/\1/' || true)"
    case "$_phase" in active|e2e_pending|passed_pending_review) ;; *) continue ;; esac
    _wt="$(grep -E '^worktree_path:' "$sf" 2>/dev/null | head -1 | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?.*/\1/' || true)"
    [ -z "$_wt" ] && continue
    [ ! -d "$_wt" ] && continue
    _active_count=$(( _active_count + 1 ))
    _active_sf="$sf"
  done
  if [ "$_active_count" -eq 1 ]; then
    echo "$_active_sf"
    exit 0
  fi
fi

exit 1
