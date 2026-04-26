#!/usr/bin/env bash
# early-stop-check.sh — 早停判据
#
# 用法：bash early-stop-check.sh <state_file> <current_log_file>
#
# 输出（stdout）：
#   CONTINUE                  ← 继续循环
#   STOP <reason>             ← 早停，reason ∈ {max_iter, no_progress, error_growth, suspected_test_tampering}
#
# 退出码：始终 0（判据本身不算失败）

set -euo pipefail

STATE_FILE="${1:?state file required}"
CUR_LOG="${2:?current log required}"

# ---- 解析 state 文件中的关键字段 ----
# 函数内 "$1" 引用语义说明：
#   - bash 函数会把位置参数 $1/$2/... 局部覆盖为函数自己的入参
#   - 因此本函数体内的 "$1" 是字段名（如 iter / project_root），而非脚本入参 $1
#   - python3 -c '... sys.argv[2]' 拿到的就是 "$1"（字段名）
#   - sys.argv[1] 用 "$STATE_FILE" 而非 "$1"，避免位置参数歧义、显式说明本意是读 state 文件
get_field() {
  # 优先 python3 yaml 真解析，fallback grep+sed（无 python3/yaml 环境）
  python3 -c "
import yaml, sys
with open(sys.argv[1]) as f:
    d = yaml.safe_load(f) or {}
v = d.get(sys.argv[2], '')
print('' if v is None else str(v))
" "$STATE_FILE" "$1" 2>/dev/null && return 0
  # Fallback
  grep -E "^${1}:" "$STATE_FILE" | head -1 | sed -E "s/^${1}:[ \t]*//; s/^\"//; s/\"$//"
}

ITER=$(get_field iter)
MAX_ITER=$(get_field max_iter)
LAST_HASH=$(get_field last_error_hash)
LAST_COUNT=$(get_field last_error_count)
# V2.0: state.project_root 在新 schema 下 = 干活的地方（worktree 或主仓）
# 旧 V1.8 路径："dirname dirname state" 只回 2 层得到 .claude/builder-loop，git diff 相对基址错
# git diff 用 project_root 才能看到 builder 当前实际改的文件（worktree 改动主仓未合时不可见）
PROJECT_ROOT="$(get_field project_root)"
if [ -z "$PROJECT_ROOT" ] || [ ! -d "$PROJECT_ROOT" ]; then
  # 兜底：state 路径 = <P>/.claude/builder-loop/state/<slug>.yml → 向上 4 层
  PROJECT_ROOT="$(cd "$(dirname "$STATE_FILE")/../../.." 2>/dev/null && pwd -P || echo "")"
fi

# ---- 1. 硬上限 ----
if [ "${ITER:-0}" -ge "${MAX_ITER:-5}" ]; then
  echo "STOP max_iter"
  exit 0
fi

# ---- 2. 当前轮错误特征 ----
CUR_HASH="$(sha1sum "$CUR_LOG" 2>/dev/null | awk '{print $1}' | cut -c1-12)"
CUR_COUNT="$(grep -ciE 'FAILED|ERROR|error:' "$CUR_LOG" 2>/dev/null || echo 0)"

# ---- 3. 无进展早停（连续 2 轮 hash 一致）----
if [ -n "$LAST_HASH" ] && [ "$LAST_HASH" = "$CUR_HASH" ]; then
  echo "STOP no_progress"
  exit 0
fi

# ---- 4. 反增长早停（错误数 ≥ 1.5x 上轮）----
if [ -n "${LAST_COUNT:-}" ] && [ "${LAST_COUNT:-0}" -gt 0 ]; then
  THRESHOLD=$(( LAST_COUNT * 3 / 2 ))
  if [ "$CUR_COUNT" -gt "$THRESHOLD" ]; then
    echo "STOP error_growth"
    exit 0
  fi
fi

# ---- 5. 保护路径作弊检测 ----
# 检查本轮 git diff 是否大量修改了测试文件
TEST_DIRS_CSV="$(get_field test_dirs || echo '')"
if [ -n "$TEST_DIRS_CSV" ]; then
  IFS=',' read -ra TEST_DIRS <<< "$TEST_DIRS_CSV"
  CHANGED_TESTS=0
  for d in "${TEST_DIRS[@]}"; do
    [ -z "$d" ] && continue
    n=$(git -C "$PROJECT_ROOT" diff --name-only HEAD -- "$d" 2>/dev/null | wc -l)
    CHANGED_TESTS=$(( CHANGED_TESTS + n ))
  done
  if [ "$CHANGED_TESTS" -ge 3 ]; then
    echo "STOP suspected_test_tampering"
    exit 0
  fi
fi

# ---- 输出 hash 和 count 给主控更新 state ----
echo "CONTINUE hash=${CUR_HASH} count=${CUR_COUNT}"
exit 0
