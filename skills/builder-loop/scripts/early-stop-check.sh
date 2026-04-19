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
PROJECT_ROOT="$(dirname "$(dirname "$STATE_FILE")")"  # state 在 .claude/ 下，向上两级

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
