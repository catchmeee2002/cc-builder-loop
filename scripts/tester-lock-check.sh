#!/usr/bin/env bash
# tester-lock-check.sh — PreToolUse hook (matcher=Read|Grep|Glob)
#
# tester subagent 上下文期间，物理拦截对 source_dirs 的读操作。
#
# 决策流：
#   1. 读 stdin JSON 拿 session_id + tool_name + tool_input.{file_path,path,pattern}
#   2. 读锁文件 ${ISOLATION_LOCK_DIR:-/tmp}/cc-subagent-{session_id}.lock
#      不存在 → 非 tester 上下文，放行（exit 0）
#   3. 锁过期（now - start_ts > ttl_min*60）→ 删锁 + 放行
#   4. 提取目标路径，转 abspath
#   5. 白名单优先：路径含 /test, /tests, /spec, /__tests__ 或以 .md 结尾 → 放行
#   6. 路径前缀匹配 source_dirs_abs → exit 2 + JSON deny
#   7. 否则放行
#
# 退出码：
#   0  = 放行
#   2  = 拒绝（CC 硬约定：PreToolUse exit 2 + stderr/stdout JSON deny → 阻断工具调用）

set -uo pipefail

LOCK_DIR="${ISOLATION_LOCK_DIR:-/tmp}"
LOG_FILE="${HOME}/.claude/logs/tester-lock-check.log"
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
log() { printf '[%s] %s\n' "$(date -Iseconds)" "$*" >> "$LOG_FILE" 2>/dev/null || true; }

INPUT="$(cat || echo '{}')"
SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || echo "")
TOOL_NAME=$(printf '%s' "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null || echo "")

[ -z "$SESSION_ID" ] && exit 0

LOCK_FILE="${LOCK_DIR}/cc-subagent-${SESSION_ID}.lock"
[ ! -f "$LOCK_FILE" ] && exit 0

# 锁是否 tester
AGENT_TYPE=$(grep -E '^agent_type:' "$LOCK_FILE" | sed -E 's/^agent_type:[[:space:]]*//' || echo "")
[ "$AGENT_TYPE" != "tester" ] && exit 0

# TTL 兜底
START_TS=$(grep -E '^start_ts:' "$LOCK_FILE" | sed -E 's/^start_ts:[[:space:]]*//' || echo 0)
TTL_MIN=$(grep -E '^ttl_min:' "$LOCK_FILE" | sed -E 's/^ttl_min:[[:space:]]*//' || echo 30)
NOW=$(date +%s)
AGE=$(( NOW - START_TS ))
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
  printf '%s\n' "{\"action\":\"deny\",\"message\":\"tester 禁读 source_dirs（命中 ${DENIED_BY}）。需要查接口请只读 interface_dirs/*.md/test 目录。如需详细规格请在 TESTER_SUMMARY 标注'规格不足'。\"}"
  exit 2
fi

exit 0
