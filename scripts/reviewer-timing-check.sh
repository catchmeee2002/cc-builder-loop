#!/usr/bin/env bash
# reviewer-timing-check.sh — PreToolUse hook (matcher=Agent)
#
# 拦截 loop 活跃期间的 reviewer spawn，防止 reviewer 读到旧代码。
# 只检查 subagent_type=reviewer 的 Agent 调用，其他 subagent 放行。
#
# 退出码：
#   0 = 放行
#   2 = 拒绝（CC 硬约定：PreToolUse exit 2 → 阻断工具调用）

set -uo pipefail

INPUT="$(cat || echo '{}')"

# 只拦截 reviewer spawn
SUBAGENT_TYPE=$(printf '%s' "$INPUT" | jq -r '.tool_input.subagent_type // empty' 2>/dev/null || echo "")
[ -z "$SUBAGENT_TYPE" ] && exit 0
[ "$SUBAGENT_TYPE" != "reviewer" ] && exit 0

# 从 CWD 向上最多 5 层找 builder-loop.local.md
CWD="$(pwd)"
find_state() {
  local dir="$1" i=0
  while [ "$i" -lt 5 ]; do
    if [ -f "${dir}/.claude/builder-loop.local.md" ]; then
      echo "${dir}/.claude/builder-loop.local.md"
      return 0
    fi
    [ "$dir" = "/" ] && return 1
    dir="$(dirname "$dir")"
    i=$((i+1))
  done
  return 1
}

STATE_FILE="$(find_state "$CWD" || echo "")"
[ -z "$STATE_FILE" ] && exit 0

# 检查 loop 是否活跃
ACTIVE="$(grep -E '^active:' "$STATE_FILE" | head -1 | awk '{print $2}')"
[ "$ACTIVE" != "true" ] && exit 0

# Loop 活跃 → 拦截 reviewer spawn
printf '%s\n' '{"action":"deny","message":"⛔ [builder-loop] Reviewer spawn blocked: loop active (active=true). Wait for PASS_CMD."}'
exit 2
