#!/usr/bin/env bash
# tester-lock-check.sh — PreToolUse hook (matcher=Read|Grep|Glob)
#
# V3.5: uses lock-utils.sh for per-agent-type lock lookup.
# Only activates when a tester lock exists for this session.
#
# Exit codes:
#   0  = allow
#   2  = deny (CC: PreToolUse exit 2 → block tool call)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/lock-utils.sh"

LOG_FILE="${HOME}/.claude/logs/tester-lock-check.log"
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
log() { printf '[%s] %s\n' "$(date -Iseconds)" "$*" >> "$LOG_FILE" 2>/dev/null || true; }

INPUT="$(cat || echo '{}')"
SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || echo "")
TOOL_NAME=$(printf '%s' "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null || echo "")

[ -z "$SESSION_ID" ] && exit 0

# V3.5: find tester-specific lock (new format first, legacy fallback)
LOCK_FILE="$(bl_lock_path "$SESSION_ID" "tester")"
if [ ! -f "$LOCK_FILE" ]; then
  # Legacy fallback: old single-file format
  LOCK_FILE="$(bl_legacy_lock_path "$SESSION_ID")"
  if [ ! -f "$LOCK_FILE" ]; then
    exit 0
  fi
  _atype="$(bl_read_lock_field "$LOCK_FILE" "agent_type")"
  [ "$_atype" != "tester" ] && exit 0
fi

# TTL check
START_TS="$(bl_read_lock_field "$LOCK_FILE" "start_ts")"
TTL_MIN="$(bl_read_lock_field "$LOCK_FILE" "ttl_min")"
[ -z "$TTL_MIN" ] && TTL_MIN=30
NOW=$(date +%s)
AGE=$(( NOW - ${START_TS:-0} ))
TTL_SEC=$(( TTL_MIN * 60 ))
if [ "$AGE" -gt "$TTL_SEC" ]; then
  log "lock expired (age=${AGE}s ttl=${TTL_SEC}s), removing & passing"
  rm -f "$LOCK_FILE"
  exit 0
fi

# 提取目标路径
case "$TOOL_NAME" in
  Read)
    TARGET=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null || echo "")
    ;;
  Glob)
    TARGET=$(printf '%s' "$INPUT" | jq -r '(.tool_input.path // "") + "/" + (.tool_input.pattern // "")' 2>/dev/null || echo "")
    ;;
  Grep)
    TARGET=$(printf '%s' "$INPUT" | jq -r '.tool_input.path // ""' 2>/dev/null || echo "")
    [ -z "$TARGET" ] && TARGET=$(pwd)
    ;;
  *)
    exit 0
    ;;
esac

[ -z "$TARGET" ] && exit 0

# 转 abspath（不存在的路径 readlink 会失败，退而 fallback）
ABS_TARGET="$(readlink -f "$TARGET" 2>/dev/null || echo "$TARGET")"

# 白名单：路径含测试目录关键字 / Markdown 文档 / 配置 → 放行
# 注：CLAUDE.md / README.md 已被 *.md 覆盖，这里不重复列
case "$ABS_TARGET" in
  *.md|*.MD|*.markdown)
    log "WHITELIST(md): $ABS_TARGET"; exit 0 ;;
  *"/tests"|*"/tests/"*|*"/test"|*"/test/"*|*"/spec"|*"/spec/"*|*"/__tests__"|*"/__tests__/"*)
    log "WHITELIST(testdir): $ABS_TARGET"; exit 0 ;;
esac

# 黑名单：source_dirs_abs 前缀匹配 → 拒绝
DENIED_BY=""
while IFS= read -r line; do
  [ -z "$line" ] && continue
  src_dir=$(echo "$line" | sed -E 's/^[[:space:]]*-[[:space:]]*"?([^"]*)"?$/\1/')
  [ -z "$src_dir" ] && continue
  case "$ABS_TARGET" in
    "$src_dir"|"$src_dir"/*)
      DENIED_BY="$src_dir"; break ;;
  esac
done < <(awk '/^source_dirs_abs:/{flag=1; next} flag && /^[a-z_]+:/{flag=0} flag' "$LOCK_FILE")

if [ -n "$DENIED_BY" ]; then
  log "DENY: $TOOL_NAME target=$ABS_TARGET hit=$DENIED_BY"
  echo "[builder-loop] tester-lock-check: blocked ${TOOL_NAME} on ${ABS_TARGET} (hit ${DENIED_BY})" >&2
  printf '%s\n' "{\"action\":\"deny\",\"message\":\"tester 禁读 source_dirs（命中 ${DENIED_BY}）。需要查接口请只读 interface_dirs/*.md/test 目录。如需详细规格请在 TESTER_SUMMARY 标注'规格不足'。\"}"
  exit 2
fi

exit 0
