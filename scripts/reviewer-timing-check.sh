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

# 只拦截 reviewer spawn（jq 优先，python3 fallback）
SUBAGENT_TYPE=""
if command -v jq &>/dev/null; then
  SUBAGENT_TYPE=$(printf '%s' "$INPUT" | jq -r '.tool_input.subagent_type // empty' 2>/dev/null || echo "")
fi
if [ -z "$SUBAGENT_TYPE" ]; then
  SUBAGENT_TYPE=$(printf '%s' "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('subagent_type',''))" 2>/dev/null || echo "")
fi
[ -z "$SUBAGENT_TYPE" ] && exit 0
[ "$SUBAGENT_TYPE" != "reviewer" ] && exit 0

# 用 locate-state.sh 按 CWD 定位对应的 state（多状态并行模式）
# 关键点：只拦截"当前 cwd 所属 worktree 的 state"，不拦截同项目内其他 worktree 的 loop
SKILL_DIR="${HOME}/.claude/skills/builder-loop/scripts"
LOCATE_SCRIPT="${SKILL_DIR}/locate-state.sh"

# 本地部署 fallback（用户直接跑 cc-builder-loop 仓库场景）
if [ ! -f "$LOCATE_SCRIPT" ]; then
  for _cand in \
    "$(dirname "$0")/../skills/builder-loop/scripts/locate-state.sh" \
    "$(pwd)/skills/builder-loop/scripts/locate-state.sh"; do
    [ -f "$_cand" ] && { LOCATE_SCRIPT="$_cand"; break; }
  done
fi
[ ! -f "$LOCATE_SCRIPT" ] && exit 0  # 找不到 locate 脚本 → 放行

STATE_FILE="$(bash "$LOCATE_SCRIPT" "$(pwd)" 2>/dev/null || echo "")"
[ -z "$STATE_FILE" ] && exit 0

# 检查 loop 是否活跃
ACTIVE="$(grep -E '^active:' "$STATE_FILE" | head -1 | awk '{print $2}')"
[ "$ACTIVE" != "true" ] && exit 0

# Loop 活跃 → 拦截 reviewer spawn
printf '%s\n' '{"action":"deny","message":"⛔ [builder-loop] Reviewer spawn blocked: loop active (active=true). Wait for PASS_CMD."}'
exit 2
