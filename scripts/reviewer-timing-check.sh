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

# 仅 phase=active 时拦截 reviewer spawn。V3.0 PASS 后 phase=passed_pending_review，
# builder 主动 spawn reviewer 是 reviewer-as-gate 必经流程，必须放行。
PHASE_FIELD="$(grep -E '^phase:' "$STATE_FILE" 2>/dev/null | head -1 | sed -E 's/^phase:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || echo "")"
ACTIVE_FIELD="$(grep -E '^active:' "$STATE_FILE" 2>/dev/null | head -1 | awk '{print $2}' || echo "")"

if [ -n "$PHASE_FIELD" ]; then
  # V3.0+ state：以 phase 为准
  [ "$PHASE_FIELD" != "active" ] && exit 0
else
  # V2.x 老 state（无 phase 字段）兼容：fallback active=true 拦
  echo "[builder-loop] reviewer-timing-check: V2.x 老 state（无 phase 字段），fallback active=${ACTIVE_FIELD:-empty} 判定（state=${STATE_FILE}）" >&2
  [ "$ACTIVE_FIELD" != "true" ] && exit 0
fi

# Loop 真正在跑 PASS_CMD → 拦截 reviewer spawn。stderr 写一行明确原因，
# 避免 CC 把「exit 2 + 仅 stdout JSON」渲染成「No stderr output」误导排查。
echo "[builder-loop] reviewer-timing-check: blocked reviewer spawn (state=${STATE_FILE}, phase=${PHASE_FIELD:-empty}, active=${ACTIVE_FIELD:-empty})" >&2
printf '%s\n' '{"action":"deny","message":"⛔ [builder-loop] Reviewer spawn blocked: loop active (phase=active). Wait for PASS_CMD."}'
exit 2
