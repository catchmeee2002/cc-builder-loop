#!/usr/bin/env bash
# builder-loop-stop.sh — Stop hook 入口
#
# 触发方式：CC 在每次 Stop 事件时调用（settings.json hooks.Stop 注册）
# stdin：CC 提供的 Stop hook input JSON（含 session_id / cwd / transcript_path 等）
# stdout：
#   - 不再使用 stdout 输出 JSON（V1.4 及之前的 {"decision":"block"} 格式已废弃）
# stderr：
#   - 日志信息（ >&2 ）：调试和状态通知
# exit code：
#   - exit 0：不需要续接（CC 正常停止）
#   - exit 2：需要续接（CC 将 stderr 作为 user message 注入 LLM context，继续跑）
#     机制：CC query.ts 收到 blockingErrors → 追加到消息历史 → state machine continue
#
# NEED_ARBITRATION 行为（V1.1+）：
#   - PASS_CMD 通过但 worktree rebase 冲突 → 预读 state 提取 worktree_path /
#     conflict_files / task_context / main_branch，从 loop.yml 读 max_attempts，
#     输出结构化 block JSON（含 arbiter spawn 预填参数 + run-apply-arbitration.sh 路径）。
#     CC 只需：spawn arbiter → 保存输出到文件 → 调 run-apply-arbitration.sh → 根据退出码决策。
#
# 行为：
#   1. 读 stdin 拿 cwd（hook 可能在不同 CC 工作目录运行）
#   2. 检测 cwd/.claude/builder-loop.local.md 是否存在且 active=true
#      - 不存在或 active=false → exit 0 立即放行
#   3. 跑 run-pass-cmd.sh
#      - PASS → 删状态文件、exit 2 让 CC 继续执行 reviewer/commit pipeline
#      - FAIL → 调 extract-error.sh + early-stop-check.sh
#        - early-stop → 写 stopped_reason、删状态、exit 0（让 CC 停下，builder 自行 AskUserQuestion）
#        - 否则 → 更新 iter / hash / count，exit 2 让 CC 继续修复

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
FOUND_LOOP_ONLY=false
find_project_root() {
  local start="$1"
  local depth="${2:-5}"
  local dir="$start"
  local i=0
  # 第一轮：找 state file + loop.yml 都存在的目录（现有行为）
  while [ "$i" -lt "$depth" ]; do
    if [ -f "${dir}/.claude/builder-loop.local.md" ] && [ -f "${dir}/.claude/loop.yml" ]; then
      echo "$dir"
      return 0
    fi
    [ "$dir" = "/" ] && break
    dir="$(dirname "$dir")"
    i=$(( i + 1 ))
  done
  # 第二轮 fallback：找只有 loop.yml 的目录（兜底激活用）
  dir="$start"
  i=0
  while [ "$i" -lt "$depth" ]; do
    if [ -f "${dir}/.claude/loop.yml" ] && [ ! -f "${dir}/.claude/builder-loop.local.md" ]; then
      FOUND_LOOP_ONLY=true
      echo "$dir"
      return 0
    fi
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

# 未接入场景（PROJECT_ROOT 完全找不到）→ 静默放行
if [ -z "$PROJECT_ROOT" ]; then
  exit 0
fi

# ---- 兜底激活：loop.yml 存在但无状态文件 ----
if [ "$FOUND_LOOP_ONLY" = "true" ]; then
  # 检测是否有代码改动（未提交 或 近 30 分钟内的 commit）
  HAS_DIFF=""
  HAS_RECENT_COMMIT=""
  if ! git -C "$PROJECT_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    # 非 git 仓库 → 放行（无法判断改动）
    exit 0
  fi
  HAS_DIFF="$(git -C "$PROJECT_ROOT" diff --stat 2>/dev/null)" || true
  [ -z "$HAS_DIFF" ] && { HAS_DIFF="$(git -C "$PROJECT_ROOT" diff --cached --stat 2>/dev/null)" || true; }
  HAS_RECENT_COMMIT="$(git -C "$PROJECT_ROOT" log --since='30 minutes ago' --oneline 2>/dev/null | head -5)" || true
  # 无任何改动 → 放行（纯对话 stop）
  if [ -z "$HAS_DIFF" ] && [ -z "$HAS_RECENT_COMMIT" ]; then
    exit 0
  fi
  # 推断 task_description
  TASK_DESC="auto-activated-by-stop-hook"
  PLAN_DIR="${PROJECT_ROOT}/.claude/plans"
  if [ -d "$PLAN_DIR" ]; then
    LATEST_PLAN="$(ls -1t "$PLAN_DIR"/*.md 2>/dev/null | head -n 1 || true)"
    if [ -n "$LATEST_PLAN" ]; then
      TASK_DESC="$(head -5 "$LATEST_PLAN" | grep -E '^# ' | head -1 | sed 's/^#[[:space:]]*//' || echo "$TASK_DESC")"
      [ -z "$TASK_DESC" ] && TASK_DESC="auto-activated-by-stop-hook"
    fi
  fi
  if [ "$TASK_DESC" = "auto-activated-by-stop-hook" ] && [ -n "$HAS_RECENT_COMMIT" ]; then
    TASK_DESC="$(echo "$HAS_RECENT_COMMIT" | head -1 | sed 's/^[a-f0-9]\+[[:space:]]*//' || echo "$TASK_DESC")"
  fi
  echo "[builder-loop] ⚡ 兜底激活：检测到 loop.yml + 代码改动但无状态文件，自动启动 loop..." >&2
  if ! bash "$SKILL_DIR/setup-builder-loop.sh" --no-worktree "$TASK_DESC" >&2; then
    echo "[builder-loop] ⚠️  兜底激活 setup 失败，放行" >&2
    exit 0
  fi
  # setup 成功 → 状态文件已创建，继续走正常流程
fi

STATE_FILE="${PROJECT_ROOT}/.claude/builder-loop.local.md"

# state file 仍不存在（兜底激活 setup 可能写了不同路径）→ 放行
if [ ! -f "$STATE_FILE" ]; then
  echo "[builder-loop] ⚠️  兜底激活后状态文件未出现在预期路径：${STATE_FILE}，放行" >&2
  exit 0
fi

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

# ---- trace 初始化 ----
TRACE_FILE="${PROJECT_ROOT}/.claude/loop-trace.jsonl"
mkdir -p "$(dirname "$TRACE_FILE")" 2>/dev/null || true
TASK_DESC_SHORT="$(grep -E '^task_description:' "$STATE_FILE" | head -1 | sed -E 's/^task_description:[[:space:]]*//' | head -c 80)"
START_TS="$(date +%s%N 2>/dev/null || date +%s)"

# trace 写入函数
write_trace() {
  local result="$1" stage="${2:-}" error_hash="${3:-}" reason="${4:-}"
  local end_ts="$(date +%s%N 2>/dev/null || date +%s)"
  local duration_ms=$(( (end_ts - START_TS) / 1000000 )) 2>/dev/null || duration_ms=0
  TRACE_FILE="$TRACE_FILE" NEXT_ITER="$NEXT_ITER" RESULT="$result" STAGE="$stage" \
    ERROR_HASH="$error_hash" REASON="$reason" DURATION_MS="$duration_ms" TASK="$TASK_DESC_SHORT" \
    python3 -c "
import json, os, datetime
line = {
    'ts': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'iter': int(os.environ['NEXT_ITER']),
    'result': os.environ['RESULT'],
    'stage': os.environ.get('STAGE', ''),
    'duration_ms': int(os.environ.get('DURATION_MS', '0')),
    'error_hash': os.environ.get('ERROR_HASH', ''),
    'reason': os.environ.get('REASON', ''),
    'task': os.environ.get('TASK', ''),
}
line = {k: v for k, v in line.items() if v != '' and v != 0 or k in ('ts','iter','result')}
with open(os.environ['TRACE_FILE'], 'a') as f:
    f.write(json.dumps(line, ensure_ascii=False) + '\n')
" 2>/dev/null || true
}

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
      write_trace "PASS"
      # exit 2 让 CC 继续执行 reviewer/commit pipeline（stderr 作为 user message 注入 LLM）
      echo "[builder-loop] ✅ PASS_CMD 全部阶段通过（iter ${NEXT_ITER}）。状态文件已清理，循环结束。请继续执行 Builder 后续流程：触发 Reviewer Subagent → 文档更新评估 → 自动 commit → 改动汇总。" >&2
      exit 2
      ;;
    NEED_ARBITRATION)
      # state 里已被 merge-worktree-back.sh 标记 need_arbitration=true
      # 预读所有参数，输出结构化指令让 CC 只需 spawn + 调脚本
      WT_PATH="$(echo "$MERGE_LAST" | awk '{print $2}')"
      CONFLICT_FILES="$(grep -E '^conflict_files:' "$STATE_FILE" | head -1 | sed -E 's/^conflict_files:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/')"
      TASK_CTX="$(grep -E '^task_description:' "$STATE_FILE" | head -1 | sed -E 's/^task_description:[[:space:]]*//')"
      MAIN_BR="$(git -C "$PROJECT_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")"
      # 读对方 commits 上下文（merge-worktree-back.sh 已写入 state）
      THEIR_COMMITS="$(grep -E '^their_commits:' "$STATE_FILE" | head -1 | sed -E "s/^their_commits:[[:space:]]*'?(.*)'?[[:space:]]*$/\1/")"
      [ -z "$THEIR_COMMITS" ] && THEIR_COMMITS="[]"
      # 读 loop.yml 的 arbitration.max_attempts（默认 2）
      MAX_ATT="2"
      if [ -f "${PROJECT_ROOT}/.claude/loop.yml" ]; then
        MAX_ATT_RAW="$(LOOP_YML_PATH="${PROJECT_ROOT}/.claude/loop.yml" python3 -c "
import re, os
text = open(os.environ['LOOP_YML_PATH']).read()
m = re.search(r'max_attempts:\s*(\d+)', text)
print(m.group(1) if m else '2')
" 2>/dev/null || echo "2")"
        [ -n "$MAX_ATT_RAW" ] && MAX_ATT="$MAX_ATT_RAW"
      fi
      # 格式化对方 commits 为可读形式
      THEIR_COMMITS_TEXT="$(THEIR_COMMITS_RAW="$THEIR_COMMITS" python3 -c "
import json, os
raw = os.environ.get('THEIR_COMMITS_RAW', '[]')
try:
    tc_list = json.loads(raw)
    if tc_list:
        lines = []
        for c in tc_list[:20]:
            lines.append(f'  - {c.get(\"hash\",\"?\")}: {c.get(\"message\",\"\")}')
            for f in c.get('files', []):
                lines.append(f'    {f}')
        print('\n'.join(lines))
    else:
        print('(no opponent commits)')
except Exception:
    print('(parse failed)')
" 2>/dev/null || echo "(parse failed)")"
      # exit 2 让 CC 继续，stderr 注入仲裁指令
      cat >&2 <<ARBITER_MSG
[builder-loop] PASS_CMD 通过，但 worktree rebase 主干时发生冲突。

请执行以下仲裁流程：
1. spawn arbiter subagent（同步），参数如下：
   subagent_type: arbiter
   worktree_path: ${WT_PATH}
   main_branch: ${MAIN_BR}
   conflict_files: ${CONFLICT_FILES}
   task_context: ${TASK_CTX}
   their_commits:
${THEIR_COMMITS_TEXT}

2. 保存 arbiter 输出到 /tmp/arbiter-output.txt

3. 调用后处理脚本：
   bash ${SKILL_DIR}/run-apply-arbitration.sh ${STATE_FILE} /tmp/arbiter-output.txt

4. 根据退出码决策：
   APPLIED (exit 0) → 继续 Reviewer/commit 流程
   LOW_CONFIDENCE (exit 1) → AskUserQuestion 让用户决策
   APPLY_FAILED (exit 2) → 重试（max_attempts=${MAX_ATT}）或交用户
   MERGE_FAILED (exit 3) → 同上
ARBITER_MSG
      exit 2
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
  write_trace "EARLY_STOP" "" "" "$REASON"
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
write_trace "FAIL" "$STAGE" "$NEW_HASH"

# ---- exit 2 让 CC 自动继续，stderr 注入修复指令 ----
cat >&2 <<FEEDBACK_MSG
[builder-loop iter ${NEXT_ITER}] PASS_CMD failed at stage='${STAGE}'.
请根据下面的失败信息修复代码。修复完成后会自动再跑一轮 PASS_CMD。

${FEEDBACK}
FEEDBACK_MSG
exit 2
