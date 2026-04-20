#!/usr/bin/env bash
# builder-loop-stop.sh — Stop hook 入口
#
# 触发方式：CC 在每次 Stop 事件时调用（settings.json hooks.Stop 注册）
# stdin：CC 提供的 Stop hook input JSON（含 session_id / cwd / transcript_path 等）
# stdout：
#   - 如果不需要继续循环 → 无输出，exit 0（CC 正常停止）
#   - 如果需要继续循环 → 输出 JSON {"decision":"block","reason":"<feedback>"} 让 CC 继续跑下一轮
#
# 行为：
#   1. 读 stdin 拿 cwd（hook 可能在不同 CC 工作目录运行）
#   2. 检测 cwd/.claude/builder-loop.local.md 是否存在且 active=true
#      - 不存在或 active=false → exit 0 立即放行
#   3. 跑 run-pass-cmd.sh
#      - PASS → 删状态文件、输出 block JSON 让 CC 继续执行 reviewer/commit pipeline
#      - FAIL → 调 extract-error.sh + early-stop-check.sh
#        - early-stop → 写 stopped_reason、删状态、exit 0（让 CC 停下，builder 自行 AskUserQuestion）
#        - 否则 → 更新 iter / hash / count，输出 block JSON 让 CC 继续

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../skills/builder-loop/scripts" && pwd 2>/dev/null)" || \
  SKILL_DIR="$HOME/.claude/skills/builder-loop/scripts"

# ---- 解析 stdin ----
INPUT="$(cat)"
CWD="$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null || echo "")"
[ -z "$CWD" ] && CWD="$(pwd)"

# ---- 定位 state file + project_root ----
# 优先级：
#   1. cwd/.claude/builder-loop.local.md 直接命中
#   2. 从 cwd 向上最多 5 层找含 .claude/loop.yml 的目录作为 PROJECT_ROOT
#   3. 都找不到 → exit 0（未接入场景）
# 命中后再读 state 里的 project_root 字段作为后续路径锚点（worktree / log 等）。
find_project_root() {
  local start="$1"
  local depth="${2:-5}"
  local dir="$start"
  local i=0
  while [ "$i" -lt "$depth" ]; do
    if [ -f "${dir}/.claude/builder-loop.local.md" ] && [ -f "${dir}/.claude/loop.yml" ]; then
      echo "$dir"
      return 0
    fi
    # 到根了
    [ "$dir" = "/" ] && return 1
    dir="$(dirname "$dir")"
    i=$(( i + 1 ))
  done
  return 1
}

PROJECT_ROOT=""
if [ -f "${CWD}/.claude/builder-loop.local.md" ]; then
  PROJECT_ROOT="$CWD"
else
  PROJECT_ROOT="$(find_project_root "$CWD" 5 || true)"
fi

# 未接入场景（没找到 state file）→ 静默放行
if [ -z "$PROJECT_ROOT" ] || [ ! -f "${PROJECT_ROOT}/.claude/builder-loop.local.md" ]; then
  exit 0
fi

STATE_FILE="${PROJECT_ROOT}/.claude/builder-loop.local.md"

# 优先用 state 里写的 project_root（绝对路径），向后兼容 state 无该字段的旧版本
STATE_PROJECT_ROOT="$(grep -E '^project_root:' "$STATE_FILE" | head -1 | sed -E 's/^project_root:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/')"
if [ -n "$STATE_PROJECT_ROOT" ] && [ -d "$STATE_PROJECT_ROOT" ]; then
  PROJECT_ROOT="$STATE_PROJECT_ROOT"
  STATE_FILE="${PROJECT_ROOT}/.claude/builder-loop.local.md"
fi


# ---- 1. 状态文件不存在或非活跃 → 放行 ----
if [ ! -f "$STATE_FILE" ]; then
  exit 0
fi
ACTIVE="$(grep -E '^active:' "$STATE_FILE" | head -1 | awk '{print $2}')"
if [ "$ACTIVE" != "true" ]; then
  exit 0
fi

# ---- 2. 取当前 iter ----
ITER=$(grep -E '^iter:' "$STATE_FILE" | head -1 | awk '{print $2}')
ITER=${ITER:-0}
NEXT_ITER=$(( ITER + 1 ))

# ---- 3. 跑 PASS_CMD ----
echo "[builder-loop] 🔄 iter ${NEXT_ITER}: 正在跑 PASS_CMD..." >&2
RESULT="$(bash "${SKILL_DIR}/run-pass-cmd.sh" "$PROJECT_ROOT" "$NEXT_ITER" || true)"
LAST_LINE="$(echo "$RESULT" | tail -1)"

# ---- 3a. PASS → merge worktree 回主干 / 删状态、放行 ----
if [ "$LAST_LINE" = "PASS" ]; then
  # T2.7：worktree 启用时先合回主干（fast-forward / rebase / 标记仲裁）
  MERGE_OUT="$(bash "${SKILL_DIR}/merge-worktree-back.sh" "$STATE_FILE" 2>&1 || true)"
  MERGE_LAST="$(echo "$MERGE_OUT" | tail -1)"
  MERGE_ACTION="$(echo "$MERGE_LAST" | awk '{print $1}')"
  case "$MERGE_ACTION" in
    MERGED|NOOP)
      rm -f "$STATE_FILE"
      echo "[builder-loop] ✅ PASS at iter ${NEXT_ITER} (${MERGE_ACTION})" >&2
      # 输出 block JSON 让 CC 继续执行 reviewer/commit pipeline，而不是停下来等用户输入
      python3 <<PY
import json
msg = "[builder-loop] ✅ PASS_CMD 全部阶段通过（iter ${NEXT_ITER}）。状态文件已清理，循环结束。请继续执行 Builder 后续流程：触发 Reviewer Subagent → 文档更新评估 → 自动 commit → 改动汇总。"
print(json.dumps({"decision": "block", "reason": msg}))
PY
      exit 0
      ;;
    NEED_ARBITRATION)
      # state 里已被 merge-worktree-back.sh 标记 need_arbitration=true；让 builder 下轮 spawn arbiter
      WT_PATH="$(echo "$MERGE_LAST" | awk '{print $2}')"
      python3 <<PY
import json
msg = f"""[builder-loop] ⚠️  PASS_CMD 通过，但 worktree rebase 主干时发生冲突。
worktree 路径：${WT_PATH}
请按 commands/builder.md 的「仲裁分支」流程 spawn arbiter subagent 解冲突，
解完后再跑 bash ~/.claude/skills/builder-loop/scripts/merge-worktree-back.sh <state_file> 重试。
"""
print(json.dumps({"decision": "block", "reason": msg}))
PY
      exit 0
      ;;
    *)
      echo "[builder-loop] ❌ merge-worktree-back.sh 未知结果：${MERGE_OUT}" >&2
      rm -f "$STATE_FILE"
      exit 0
      ;;
  esac
fi

# ---- 3b. FAIL → 处理反馈 ----
echo "[builder-loop] ❌ iter ${NEXT_ITER}: PASS_CMD 在 stage=$(echo "$LAST_LINE" | awk '{print $2}') 失败，分析中..." >&2
STAGE="$(echo "$LAST_LINE" | awk '{print $2}')"
LOG_PATH="$(echo "$LAST_LINE" | awk '{print $3}')"

# 早停判断
ESTOP="$(bash "${SKILL_DIR}/early-stop-check.sh" "$STATE_FILE" "$LOG_PATH")"
ESTOP_ACTION="$(echo "$ESTOP" | awk '{print $1}')"

if [ "$ESTOP_ACTION" = "STOP" ]; then
  REASON="$(echo "$ESTOP" | awk '{print $2}')"
  # 用 python 安全更新 yaml 字段（防特殊字符破坏）
  STATE_FILE="$STATE_FILE" REASON="$REASON" python3 - <<'PY'
import os, re
sf = os.environ['STATE_FILE']
reason = os.environ['REASON']
text = open(sf).read()
text = re.sub(r'^stopped_reason:.*$', f'stopped_reason: "{reason}"', text, flags=re.M)
text = re.sub(r'^active:.*$', 'active: false', text, flags=re.M)
open(sf, 'w').write(text)
PY
  echo "[builder-loop] ⛔ early stop at iter ${NEXT_ITER}, reason=${REASON}" >&2
  # 不阻断 CC：让 builder 在下一次 user prompt 时 AskUserQuestion
  exit 0
fi

# CONTINUE → 更新 state，注入反馈
NEW_HASH="$(echo "$ESTOP" | grep -oE 'hash=[a-f0-9]+' | cut -d= -f2 || echo '')"
NEW_COUNT="$(echo "$ESTOP" | grep -oE 'count=[0-9]+' | cut -d= -f2 || echo 0)"
STATE_FILE="$STATE_FILE" NEXT_ITER="$NEXT_ITER" STAGE="$STAGE" NEW_HASH="$NEW_HASH" NEW_COUNT="$NEW_COUNT" python3 - <<'PY'
import os, re
sf = os.environ['STATE_FILE']
text = open(sf).read()
text = re.sub(r'^iter:.*$', f'iter: {os.environ["NEXT_ITER"]}', text, flags=re.M)
text = re.sub(r'^last_pass_stage:.*$', f'last_pass_stage: "{os.environ["STAGE"]}"', text, flags=re.M)
text = re.sub(r'^last_error_hash:.*$', f'last_error_hash: "{os.environ["NEW_HASH"]}"', text, flags=re.M)
text = re.sub(r'^last_error_count:.*$', f'last_error_count: {os.environ["NEW_COUNT"]}', text, flags=re.M)
open(sf, 'w').write(text)
PY

FEEDBACK="$(bash "${SKILL_DIR}/extract-error.sh" "$LOG_PATH" "$STAGE" "$PROJECT_ROOT")"

# ---- 输出 block JSON 让 CC 自动继续 ----
python3 <<PY
import json, sys
fb = """${FEEDBACK//\"/\\\"}"""
msg = f"""[builder-loop iter ${NEXT_ITER}/${ITER}+1] PASS_CMD failed at stage='${STAGE}'.
请根据下面的失败信息修复代码。修复完成后会自动再跑一轮 PASS_CMD。

{fb}
"""
print(json.dumps({"decision": "block", "reason": msg}))
PY
