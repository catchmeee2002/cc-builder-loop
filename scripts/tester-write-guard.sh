#!/usr/bin/env bash
# tester-write-guard.sh — PreToolUse hook (matcher=Write|Edit|MultiEdit)
#
# tester subagent 上下文期间，物理拦截把文件写到 worktree 之外的尝试。
#
# 决策流：
#   1. 读 stdin JSON 拿 session_id + tool_name + tool_input.file_path
#   2. 读锁文件 ${ISOLATION_LOCK_DIR:-/tmp}/cc-subagent-${session_id}.lock
#      不存在 → exit 0 放行（非 tester subagent）
#   3. agent_type != tester → exit 0 放行
#   4. 锁过期（now - start_ts > ttl_min*60）→ 删锁 + exit 0 放行
#   5. worktree_path 字段为空 → exit 0 放行（bare loop / V1.x 老锁）
#   6. file_path 转 abspath（realpath 解析 .. / symlink）
#   7. abspath 以 ${worktree_path}/ 开头（含尾斜杠防 prefix 误判）→ exit 0 放行
#   8. 否则 exit 2 + stderr 精确诊断（worktree_path / main_repo_path / 改用建议）
#
# 退出码：
#   0 = 放行
#   2 = 拒绝（CC 硬约定：PreToolUse exit 2 → 阻断工具调用 + stderr 注入 LLM context）

set -uo pipefail

LOCK_DIR="${ISOLATION_LOCK_DIR:-/tmp}"
LOG_FILE="${HOME}/.claude/logs/tester-write-guard.log"
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
log() { printf '[%s] %s\n' "$(date -Iseconds)" "$*" >> "$LOG_FILE" 2>/dev/null || true; }

INPUT="$(cat || echo '{}')"

# 解析 session_id / tool_name / file_path
parse_field() {
  local field="$1"
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$INPUT" | jq -r "$field // empty" 2>/dev/null || echo ""
  else
    printf '%s' "$INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    keys = '$field'.lstrip('.').split('.')
    v = d
    for k in keys:
        v = (v or {}).get(k, '')
    print(v if v is not None else '')
except Exception:
    print('')
" 2>/dev/null || echo ""
  fi
}

SESSION_ID="$(parse_field '.session_id')"
TOOL_NAME="$(parse_field '.tool_name')"
TARGET="$(parse_field '.tool_input.file_path')"

[ -z "$SESSION_ID" ] && exit 0
[ -z "$TOOL_NAME" ] && exit 0

case "$TOOL_NAME" in
  Write|Edit|MultiEdit) ;;
  *) exit 0 ;;
esac

LOCK_FILE="${LOCK_DIR}/cc-subagent-${SESSION_ID}.lock"
[ ! -f "$LOCK_FILE" ] && exit 0

AGENT_TYPE="$(grep -E '^agent_type:' "$LOCK_FILE" 2>/dev/null | head -1 | sed -E 's/^agent_type:[[:space:]]*//' || true)"
[ "$AGENT_TYPE" != "tester" ] && exit 0

# TTL 兜底：锁过期则删
START_TS="$(grep -E '^start_ts:' "$LOCK_FILE" 2>/dev/null | head -1 | sed -E 's/^start_ts:[[:space:]]*//' || echo 0)"
TTL_MIN="$(grep -E '^ttl_min:' "$LOCK_FILE" 2>/dev/null | head -1 | sed -E 's/^ttl_min:[[:space:]]*//' || echo 30)"
NOW="$(date +%s)"
AGE=$(( NOW - START_TS ))
TTL_SEC=$(( TTL_MIN * 60 ))
if [ "$AGE" -gt "$TTL_SEC" ]; then
  log "lock expired (age=${AGE}s ttl=${TTL_SEC}s), removing & passing"
  rm -f "$LOCK_FILE"
  exit 0
fi

WORKTREE_PATH="$(grep -E '^worktree_path:' "$LOCK_FILE" 2>/dev/null | head -1 | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || true)"
MAIN_REPO_PATH="$(grep -E '^main_repo_path:' "$LOCK_FILE" 2>/dev/null | head -1 | sed -E 's/^main_repo_path:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || true)"

# bare loop 或 V1.x 老锁缺字段 → 放行所有 Write/Edit
[ -z "$WORKTREE_PATH" ] && exit 0

[ -z "$TARGET" ] && exit 0

# realpath 解析 .. 与 symlink，防 path traversal 绕过
ABS_TARGET="$(readlink -f "$TARGET" 2>/dev/null || echo "$TARGET")"
ABS_WORKTREE="$(readlink -f "$WORKTREE_PATH" 2>/dev/null || echo "$WORKTREE_PATH")"

# 必须以 worktree/ 开头（尾斜杠防 /wt 与 /wt2 误匹配；也防 file_path 恰好等于 worktree 根）
case "$ABS_TARGET" in
  "$ABS_WORKTREE"/*)
    exit 0
    ;;
esac

log "DENY: $TOOL_NAME target=$ABS_TARGET worktree=$ABS_WORKTREE main=$MAIN_REPO_PATH"

cat >&2 <<DENY_MSG
⛔ [builder-loop] tester 跨目录写禁止：
   尝试写入: ${TARGET}
   解析路径: ${ABS_TARGET}
   允许根:   ${ABS_WORKTREE}
   主仓:     ${MAIN_REPO_PATH}（禁止跨界写）
   请改用 ${ABS_WORKTREE}/<相对路径>
DENY_MSG

exit 2
