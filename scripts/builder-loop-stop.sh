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

# V1.8.1: state 归档到 legacy（替代"留着 active=false 僵尸"）
# 两个调用点：① 发现 active!=true 的僵尸 state；② EARLY_STOP 不再改字段，直接归档
archive_to_legacy() {
  local sf="$1" reason="$2"
  [ -f "$sf" ] || return 0
  local legacy_dir
  legacy_dir="$(dirname "$sf")/../legacy"
  mkdir -p "$legacy_dir" 2>/dev/null || true
  local ts reason_safe
  ts="$(date +%Y%m%d-%H%M%S)"
  reason_safe="$(printf '%s' "$reason" | tr -c 'a-zA-Z0-9_' '_')"
  mv "$sf" "${legacy_dir}/${ts}-${reason_safe}.bak" 2>/dev/null || true
}

# V1.8.2: 写"已处理 HEAD"游标 — 避免同一 commit 反复触发 bootstrap 兜底激活
# 调用点：PASS / 异常 merge / EARLY_STOP 三处"本轮 loop 结束"的出口
# 刻意不调用的路径：
#   ① zombie_inactive（非本轮 loop 归档，HEAD 可能未经处理，写游标会误阻塞下次合法激活）
#   ② NEED_ARBITRATION（state 未清，下次 Stop 命中 state 走正常流程，不进 bootstrap guard）
write_processed_cursor() {
  local proj_root="$1"
  local head_sha
  head_sha="$(git -C "$proj_root" rev-parse HEAD 2>/dev/null || echo "")"
  if [ -n "$head_sha" ]; then
    mkdir -p "${proj_root}/.claude/builder-loop" 2>/dev/null || true
    printf '%s\n' "$head_sha" > "${proj_root}/.claude/builder-loop/last_processed_head" 2>/dev/null || true
  fi
}

# ---- 解析 stdin ----
INPUT="$(cat)"
CWD="$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null || echo "")"
[ -z "$CWD" ] && CWD="$(pwd)"
# V1.9: transcript_path 给 judge agent 用
TRANSCRIPT_PATH="$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('transcript_path',''))" 2>/dev/null || echo "")"

# ---- 定位 state file + project_root（多状态并行模式）----
# 策略：
#   1. 用 locate-state.sh 按 CWD 找对应 state：命中 → 走正常流程
#   2. 未命中但向上 5 层能找到 .claude/loop.yml → 兜底激活（setup 一个 bare loop）
#   3. 都找不到 → exit 0（未接入场景）
LOCATE_SCRIPT="${SKILL_DIR}/locate-state.sh"
[ ! -f "$LOCATE_SCRIPT" ] && LOCATE_SCRIPT="$HOME/.claude/skills/builder-loop/scripts/locate-state.sh"

FOUND_LOOP_ONLY=false
PROJECT_ROOT=""
STATE_FILE=""

if [ -f "$LOCATE_SCRIPT" ]; then
  STATE_FILE="$(bash "$LOCATE_SCRIPT" "$CWD" 2>/dev/null || echo "")"
fi

if [ -n "$STATE_FILE" ]; then
  # 命中 state → 从 state 读 project_root
  PROJECT_ROOT="$(grep -E '^project_root:' "$STATE_FILE" | head -1 | sed -E 's/^project_root:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/')"
  # fallback：state 路径回溯 .../.claude/builder-loop/state/<slug>.yml → 上 3 层
  if [ -z "$PROJECT_ROOT" ] || [ ! -d "$PROJECT_ROOT" ]; then
    PROJECT_ROOT="$(cd "$(dirname "$STATE_FILE")/../../.." 2>/dev/null && pwd -P || echo "")"
  fi
else
  # 没 state，看能否向上找到 loop.yml（兜底激活前提）
  _d="$CWD"
  for _i in 1 2 3 4 5; do
    if [ -f "${_d}/.claude/loop.yml" ]; then
      PROJECT_ROOT="$_d"
      FOUND_LOOP_ONLY=true
      break
    fi
    [ "$_d" = "/" ] && break
    _d="$(dirname "$_d")"
  done
fi

# 未接入场景 → 静默放行
if [ -z "$PROJECT_ROOT" ]; then
  exit 0
fi

# ---- V1.8.3: Stop hook 并发互斥（per-slug flock）----
# 根因：CC 可能并发触发 Stop hook，Hook A 跑 PASS_CMD 时 Hook B 已启动读 state，
#       A 完成后 rm state → B 内部 grep 踩空（复现 session d9ef1004 末尾 grep 报错）
# 策略：per-slug 粒度互斥，抢不到锁立即 exit 0 静默放行（让正在跑的 A 独占完成）
# bootstrap 场景（FOUND_LOOP_ONLY=true）固定用 __main__ slug 锁，天然互斥 setup race
SLUG="__main__"
if [ -n "$STATE_FILE" ]; then
  SLUG="$(basename "$STATE_FILE" .yml 2>/dev/null || echo "__main__")"
fi
LOCK_FILE="${PROJECT_ROOT}/.claude/builder-loop/stop-hook-${SLUG}.lock"
mkdir -p "$(dirname "$LOCK_FILE")" 2>/dev/null || true
# 注意：不能写 `exec 200>FILE 2>/dev/null`，bash 会把 `2>/dev/null` 视为"空 exec 全局 FD 重定向"
#       永久劫持主 shell 的 stderr，导致后续日志全部丢失
exec 200>"$LOCK_FILE"
if ! flock -n 200 2>/dev/null; then
  # 另一 Stop hook 正持本 slug 锁，本次静默放行
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
  # ---- 跨 session 污染守门（2026-04-24 新增）----
  # 已存在 loop/ 前缀的 worktree → 说明别的 session 正在用 loop 或留下孤儿
  # 兜底激活会误把当前 session 绑到别人的 plan / worktree 上，主仓 state file 被跨 session 污染
  # 此时宁可放行不续接，让用户人工判断
  EXISTING_LOOP_WORKTREES="$(git -C "$PROJECT_ROOT" worktree list 2>/dev/null | awk '{print $3}' | grep -c '^\[loop/' || true)"
  if [ "${EXISTING_LOOP_WORKTREES:-0}" -gt 0 ]; then
    echo "[builder-loop] ⚠️  检测到 ${EXISTING_LOOP_WORKTREES} 个已存在的 loop/ worktree，跳过兜底激活（避免跨 session 绑错 plan / worktree）" >&2
    echo "[builder-loop]    如需清理：git -C '$PROJECT_ROOT' worktree list 查看 → git worktree remove <path> 移除" >&2
    exit 0
  fi
  HAS_DIFF="$(git -C "$PROJECT_ROOT" diff --stat 2>/dev/null)" || true
  [ -z "$HAS_DIFF" ] && { HAS_DIFF="$(git -C "$PROJECT_ROOT" diff --cached --stat 2>/dev/null)" || true; }
  HAS_RECENT_COMMIT="$(git -C "$PROJECT_ROOT" log --since='30 minutes ago' --oneline 2>/dev/null | head -5)" || true
  # 无任何改动 → 放行（纯对话 stop）
  if [ -z "$HAS_DIFF" ] && [ -z "$HAS_RECENT_COMMIT" ]; then
    exit 0
  fi
  # V1.8.2: 已处理 HEAD 游标检查 — 同一 HEAD 不重复兜底激活
  # 修复场景：推完 commit 后 30 分钟窗口内每次对话都触发 NOOP 空转（session 3d62eb57 复现）
  # 仅当 HAS_DIFF 为空时游标生效；用户本地仍在改（HAS_DIFF 非空）时不受此限制
  if [ -z "$HAS_DIFF" ]; then
    CURSOR_FILE="${PROJECT_ROOT}/.claude/builder-loop/last_processed_head"
    CURRENT_HEAD="$(git -C "$PROJECT_ROOT" rev-parse HEAD 2>/dev/null || echo "")"
    if [ -f "$CURSOR_FILE" ] && [ -n "$CURRENT_HEAD" ]; then
      LAST_HEAD="$(cat "$CURSOR_FILE" 2>/dev/null | head -1 | tr -d '[:space:]')"
      if [ "$CURRENT_HEAD" = "$LAST_HEAD" ]; then
        exit 0
      fi
    fi
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
  # setup 成功 → state 文件已在新目录创建（bare 模式 slug=__main__）
  STATE_FILE="${PROJECT_ROOT}/.claude/builder-loop/state/__main__.yml"
fi

# state file 仍不存在 → 放行
if [ ! -f "$STATE_FILE" ]; then
  echo "[builder-loop] ⚠️  兜底激活后状态文件未出现在预期路径：${STATE_FILE}，放行" >&2
  exit 0
fi


# ---- 1. 状态文件不存在或非活跃 → 放行 ----
if [ ! -f "$STATE_FILE" ]; then
  exit 0
fi
ACTIVE="$(grep -E '^active:' "$STATE_FILE" | head -1 | awk '{print $2}')"
if [ "$ACTIVE" != "true" ]; then
  # V1.8.1: 非活跃 state 视为僵尸（手动编辑 / 早停遗留），归档后放行
  # 防止下次 builder 进场误把僵尸当活跃 loop
  echo "[builder-loop] 🧟 state active='${ACTIVE}' (非 true)，归档到 legacy/ 后放行" >&2
  archive_to_legacy "$STATE_FILE" "zombie_inactive"
  exit 0
fi

# ---- V1.9: outcome 后置补标（回溯标注上一轮 judge 结果） ----
# 仅当上一轮 action=continue_nudge 时自动标 nudge_was_correct / nudge_likely_false_positive
# stop_done / retry_transient 类需要更复杂判据（或人工标），这里跳过
#
# 局限：本逻辑只在「同一 task 内多轮 loop」严格成立——start_head 与 jsonl 末尾的 nudge
#       同源（同一 setup-builder-loop.sh 调用）。跨 task 场景（上一 task PASS+stop_done
#       已 cleanup state，新 task setup 创建新 state.start_head）下，jsonl 末尾通常是
#       上 task 的 stop_done（不会触发 outcome 标记）；理论边界：上 task 末轮 nudge
#       后未到 stop_done 就被外部中断 → 新 task 进场可能误标。当前接受此小概率边界。
JUDGE_TRACE_FILE="${PROJECT_ROOT}/.claude/builder-loop/judge-trace.jsonl"
if [ -f "$JUDGE_TRACE_FILE" ]; then
  BACKFILL_START_HEAD="$(grep -E '^start_head:' "$STATE_FILE" 2>/dev/null | head -1 | sed -E 's/^start_head:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || echo "")"
  BACKFILL_DIFF_NE=""
  if [ -n "$BACKFILL_START_HEAD" ] && git -C "$PROJECT_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    if git -C "$PROJECT_ROOT" diff --quiet "${BACKFILL_START_HEAD}..HEAD" 2>/dev/null; then
      BACKFILL_DIFF_NE="false"
    else
      BACKFILL_DIFF_NE="true"
    fi
  fi
  TRACE_FILE="$JUDGE_TRACE_FILE" DIFF_NE="$BACKFILL_DIFF_NE" python3 - <<'PY' 2>/dev/null || true
import os, json
trace = os.environ['TRACE_FILE']
diff_ne = os.environ.get('DIFF_NE', '')
try:
    with open(trace) as f:
        lines = f.readlines()
except Exception:
    raise SystemExit
if not lines:
    raise SystemExit
idx = len(lines) - 1
while idx >= 0 and not lines[idx].strip():
    idx -= 1
if idx < 0:
    raise SystemExit
try:
    obj = json.loads(lines[idx])
except Exception:
    raise SystemExit
if obj.get('outcome') is not None:
    raise SystemExit
last_action = obj.get('judge', {}).get('action', '')
outcome = None
if last_action == 'continue_nudge':
    if diff_ne == 'true':
        outcome = 'nudge_was_correct'
    elif diff_ne == 'false':
        outcome = 'nudge_likely_false_positive'
if outcome is None:
    raise SystemExit
obj['outcome'] = outcome
lines[idx] = json.dumps(obj, ensure_ascii=False) + '\n'
with open(trace, 'w') as f:
    f.writelines(lines)
PY
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
  # V1.8.3 hotfix: 预读 start_head — merge-worktree-back.sh 的 cleanup_worktree 会 rm state，
  # 后续再 grep state 会抛 `No such file` 到用户屏幕（复现 session d9ef1004 `grep: .../state.yml`）
  # 安全性：进入此分支前 STATE_FILE 已通过 L200 + L203 的 `[ -f "$STATE_FILE" ]` 检查，`set -u` 不会抢先触发
  PASS_START_HEAD_PREREAD="$(grep -E '^start_head:' "$STATE_FILE" 2>/dev/null | head -1 | sed -E 's/^start_head:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || echo "")"

  # ---- V1.9: judge agent 调用（PASS_CMD 通过后语义判定） ----
  # 任何故障路径（脚本缺失 / API 失败 / JSON 解析失败 / confidence 低）都通过 downgraded=true 表达
  # 降级时本段不阻断，fall through 走原 PASS 路径（merge-worktree-back + reviewer）
  if [ -f "${SKILL_DIR}/run-judge-agent.sh" ]; then
    JUDGE_RESULT="$(bash "${SKILL_DIR}/run-judge-agent.sh" \
        --state-file "$STATE_FILE" \
        --project-root "$PROJECT_ROOT" \
        --transcript-path "$TRANSCRIPT_PATH" \
        --pass-cmd-status "PASS" 2>/dev/null || echo '{"action":"stop_done","downgraded":true,"downgrade_reason":"script_error","confidence":0.0,"reason":"","model_used":"","credential_path":"none"}')"
  else
    JUDGE_RESULT='{"action":"stop_done","downgraded":true,"downgrade_reason":"script_missing","confidence":0.0,"reason":"","model_used":"","credential_path":"none"}'
  fi
  JUDGE_ACTION="$(echo "$JUDGE_RESULT" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('action','stop_done'))" 2>/dev/null || echo "stop_done")"
  JUDGE_DOWNGRADED="$(echo "$JUDGE_RESULT" | python3 -c "import sys,json; print(str(json.loads(sys.stdin.read()).get('downgraded',False)).lower())" 2>/dev/null || echo "true")"
  JUDGE_CONF_OUT="$(echo "$JUDGE_RESULT" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('confidence',0))" 2>/dev/null || echo "0")"
  JUDGE_REASON_OUT="$(echo "$JUDGE_RESULT" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('reason',''))" 2>/dev/null || echo "")"

  # 仅在 PASS 分支才会到这里，run-judge-agent.sh 的 FAIL→PASS 错调用由本块所在的 PASS 段位置保证；
  # 这里不再检查 pass_cmd_status——纯 action 路由
  if [ "$JUDGE_ACTION" = "continue_nudge" ] && [ "$JUDGE_DOWNGRADED" = "false" ]; then
    # 连续 nudge 上限保护（防 LLM 判据脱缰）
    CUR_NUDGE="$(grep -E '^consecutive_nudge_count:' "$STATE_FILE" 2>/dev/null | head -1 | awk '{print $2}')"
    CUR_NUDGE="${CUR_NUDGE:-0}"
    MAX_NUDGE="2"
    if [ -f "${PROJECT_ROOT}/.claude/loop.yml" ]; then
      MAX_NUDGE_RAW="$(grep -E '^[[:space:]]+max_consecutive_nudges:' "${PROJECT_ROOT}/.claude/loop.yml" 2>/dev/null | head -1 | awk '{print $2}' || echo "")"
      [ -n "$MAX_NUDGE_RAW" ] && MAX_NUDGE="$MAX_NUDGE_RAW"
    fi
    MAX_ITER_FOR_MSG="$(grep -E '^max_iter:' "$STATE_FILE" 2>/dev/null | head -1 | awk '{print $2}')"
    MAX_ITER_FOR_MSG="${MAX_ITER_FOR_MSG:-5}"
    if [ "$CUR_NUDGE" -lt "$MAX_NUDGE" ]; then
      NEW_NUDGE=$((CUR_NUDGE + 1))
      STATE_FILE="$STATE_FILE" NEXT_ITER="$NEXT_ITER" \
        JUDGE_CF="$JUDGE_CONF_OUT" JUDGE_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo '1970-01-01T00:00:00Z')" \
        NUDGE_CNT="$NEW_NUDGE" python3 - <<'PY'
import os, re
sf = os.environ['STATE_FILE']
text = open(sf).read()
text = re.sub(r'^iter:.*$', f'iter: {os.environ["NEXT_ITER"]}', text, flags=re.M)
def upsert(text, key, value):
    pat = re.compile(rf'^{key}:.*$', re.M)
    if pat.search(text):
        return pat.sub(f'{key}: {value}', text)
    if not text.endswith('\n'):
        text += '\n'
    return text + f'{key}: {value}\n'
text = upsert(text, 'last_judge_action', '"continue_nudge"')
text = upsert(text, 'last_judge_confidence', os.environ['JUDGE_CF'])
text = upsert(text, 'last_judge_ts', f'"{os.environ["JUDGE_TS"]}"')
text = upsert(text, 'consecutive_nudge_count', os.environ['NUDGE_CNT'])
open(sf, 'w').write(text)
PY
      write_trace "JUDGE_NUDGE" "judge" "" "$JUDGE_REASON_OUT"
      cat >&2 <<NUDGE_MSG
[builder-loop judge | iter=${NEXT_ITER}/${MAX_ITER_FOR_MSG} | judge=continue_nudge | conf=${JUDGE_CONF_OUT}]
原因：${JUDGE_REASON_OUT}
请确认：是确实完成了无需更多改动，还是漏了什么？

(PASS_CMD 状态：通过)
本消息来自 builder-loop 自动判定 agent，非用户输入。如果你认为判定错误，请在回复中说明理由继续操作。
NUDGE_MSG
      exit 2
    else
      echo "[builder-loop judge | iter=${NEXT_ITER}] consecutive_nudge_count=${CUR_NUDGE} >= max=${MAX_NUDGE}，强制 stop_done（防脱缰）" >&2
      # V1.9 fix: 强制 stop_done 也要写 telemetry，标记 max_nudge_reached（reviewer 反馈）
      MAX_NUDGE_TRACE="${PROJECT_ROOT}/.claude/builder-loop/judge-trace.jsonl"
      MAX_NUDGE_SLUG="$(basename "$STATE_FILE" .yml 2>/dev/null || echo "")"
      TRACE_FILE="$MAX_NUDGE_TRACE" SLUG="$MAX_NUDGE_SLUG" NEXT_ITER="$NEXT_ITER" \
        CUR_NUDGE="$CUR_NUDGE" MAX_NUDGE="$MAX_NUDGE" JUDGE_CONF_OUT="$JUDGE_CONF_OUT" \
        python3 - <<'PY' 2>/dev/null || true
import os, json, datetime
line = {
    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "slug": os.environ.get('SLUG', ''),
    "iter": int(os.environ.get('NEXT_ITER') or 0),
    "action": "stop_done",
    "judge": {
        "action": "stop_done",
        "confidence": float(os.environ.get('JUDGE_CONF_OUT') or 0),
        "reason": f"max_nudge_reached: {os.environ.get('CUR_NUDGE','')} >= {os.environ.get('MAX_NUDGE','')}",
    },
    "downgraded": True,
    "downgrade_reason": "max_nudge_reached",
    "outcome": None,
}
try:
    with open(os.environ['TRACE_FILE'], 'a') as f:
        f.write(json.dumps(line, ensure_ascii=False) + '\n')
except Exception:
    pass
PY
    fi
  fi

  # T2.7：worktree 启用时先合回主干（fast-forward / rebase / 标记仲裁）
  MERGE_OUT="$(bash "${SKILL_DIR}/merge-worktree-back.sh" "$STATE_FILE" 2>&1 || true)"
  MERGE_LAST="$(echo "$MERGE_OUT" | tail -1)"
  MERGE_ACTION="$(echo "$MERGE_LAST" | awk '{print $1}')"
  case "$MERGE_ACTION" in
    MERGED|NOOP)
      # 用 merge 前预读的 start_head（cleanup_worktree 可能已把 state 删了）
      PASS_START_HEAD="$PASS_START_HEAD_PREREAD"
      # fallback：旧 state 文件可能无 start_head 字段
      if [ -z "$PASS_START_HEAD" ]; then
        PASS_START_HEAD="$(git -C "$PROJECT_ROOT" rev-parse --short HEAD 2>/dev/null || echo "")"
      fi
      write_processed_cursor "$PROJECT_ROOT"
      rm -f "$STATE_FILE"

      # ---- 预计算 reviewer 参数 → 写入文件，builder 直接消费 ----
      PARAMS_FILE="${PROJECT_ROOT}/.claude/reviewer-params.json"
      DIFF_FILE="${PROJECT_ROOT}/.claude/reviewer-diff.txt"
      REVIEWER_FILES="$(git -C "$PROJECT_ROOT" diff --name-only "${PASS_START_HEAD}..HEAD" 2>/dev/null | tr '\n' ',' | sed 's/,$//' || echo "")"
      PROJ_NAME="$(basename "$PROJECT_ROOT")"
      mkdir -p "${PROJECT_ROOT}/.claude/review_reports" 2>/dev/null || true
      REPORT_TS="$(date +%Y%m%d_%H%M%S)"
      REPORT_PATH="${PROJECT_ROOT}/.claude/review_reports/${PROJ_NAME}_${REPORT_TS}.md"
      git -C "$PROJECT_ROOT" diff "${PASS_START_HEAD}..HEAD" > "$DIFF_FILE" 2>/dev/null || echo "" > "$DIFF_FILE"
      PARAMS_FILE="$PARAMS_FILE" PASS_START_HEAD="$PASS_START_HEAD" REVIEWER_FILES="$REVIEWER_FILES" \
        REPORT_PATH="$REPORT_PATH" DIFF_FILE="$DIFF_FILE" python3 -c "
import json, os
params = {
    'start_head': os.environ['PASS_START_HEAD'],
    'changed_files': [f for f in os.environ['REVIEWER_FILES'].split(',') if f],
    'report_path': os.environ['REPORT_PATH'],
    'diff_file': os.environ['DIFF_FILE'],
}
with open(os.environ['PARAMS_FILE'], 'w') as f:
    json.dump(params, f, indent=2, ensure_ascii=False)
    f.write('\n')
" 2>/dev/null || true
      echo "[builder-loop] ✅ PASS at iter ${NEXT_ITER} (${MERGE_ACTION})" >&2
      write_trace "PASS"
      # exit 2 让 CC 继续执行 reviewer/commit pipeline（stderr 作为 user message 注入 LLM）
      cat >&2 <<PASS_MSG
[builder-loop] ✅ PASS_CMD 全部阶段通过（iter ${NEXT_ITER}）。状态文件已清理，循环结束。
start_head=${PASS_START_HEAD}
reviewer_params=${PARAMS_FILE}
请继续执行 Builder 后续流程：触发 Reviewer Subagent → 文档更新评估 → 自动 commit → 改动汇总。
⚠️ 重要：如果之前已有 reviewer 在后台运行，其结果基于旧代码（loop 运行前的快照），无效。请忽略旧 reviewer 结果，基于当前 HEAD 重新 spawn reviewer。
⚠️ Reviewer 参数已预计算到 ${PARAMS_FILE}（含 changed_files/report_path/diff_file），Read 后直接传给 reviewer。diff 用 git diff ${PASS_START_HEAD}..HEAD 或读 ${DIFF_FILE}。
PASS_MSG
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
      write_processed_cursor "$PROJECT_ROOT"
      rm -f "$STATE_FILE"
      exit 0
      ;;
  esac
fi

# ---- 3b. FAIL → 处理反馈 ----
echo "[builder-loop] ❌ iter ${NEXT_ITER}: PASS_CMD 在 stage=$(echo "$LAST_LINE" | awk '{print $2}') 失败，分析中..." >&2
STAGE="$(echo "$LAST_LINE" | awk '{print $2}')"
LOG_PATH="$(echo "$LAST_LINE" | awk '{print $3}')"

# ---- V1.9: judge agent retry_transient 检测（FAIL 分支） ----
# 仅识别"上轮回复异常截断（API 抖动）"，其他 FAIL 全部走原路径（extract-error + early-stop）
if [ -f "${SKILL_DIR}/run-judge-agent.sh" ]; then
  JUDGE_RESULT_FAIL="$(bash "${SKILL_DIR}/run-judge-agent.sh" \
      --state-file "$STATE_FILE" \
      --project-root "$PROJECT_ROOT" \
      --transcript-path "$TRANSCRIPT_PATH" \
      --pass-cmd-status "FAIL" \
      --pass-cmd-stage "$STAGE" \
      --pass-cmd-log "$LOG_PATH" 2>/dev/null || echo '{"action":"continue_strict","downgraded":true,"confidence":0.0,"reason":""}')"
  JUDGE_ACTION_FAIL="$(echo "$JUDGE_RESULT_FAIL" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('action','continue_strict'))" 2>/dev/null || echo "continue_strict")"
  JUDGE_DOWNGRADED_FAIL="$(echo "$JUDGE_RESULT_FAIL" | python3 -c "import sys,json; print(str(json.loads(sys.stdin.read()).get('downgraded',False)).lower())" 2>/dev/null || echo "true")"
  JUDGE_CONF_FAIL="$(echo "$JUDGE_RESULT_FAIL" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('confidence',0))" 2>/dev/null || echo "0")"
  JUDGE_REASON_FAIL="$(echo "$JUDGE_RESULT_FAIL" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('reason',''))" 2>/dev/null || echo "")"
  if [ "$JUDGE_ACTION_FAIL" = "retry_transient" ] && [ "$JUDGE_DOWNGRADED_FAIL" = "false" ]; then
    STATE_FILE="$STATE_FILE" NEXT_ITER="$NEXT_ITER" \
      JUDGE_CF="$JUDGE_CONF_FAIL" JUDGE_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo '1970-01-01T00:00:00Z')" python3 - <<'PY'
import os, re
sf = os.environ['STATE_FILE']
text = open(sf).read()
text = re.sub(r'^iter:.*$', f'iter: {os.environ["NEXT_ITER"]}', text, flags=re.M)
def upsert(text, key, value):
    pat = re.compile(rf'^{key}:.*$', re.M)
    if pat.search(text):
        return pat.sub(f'{key}: {value}', text)
    if not text.endswith('\n'):
        text += '\n'
    return text + f'{key}: {value}\n'
text = upsert(text, 'last_judge_action', '"retry_transient"')
text = upsert(text, 'last_judge_confidence', os.environ['JUDGE_CF'])
text = upsert(text, 'last_judge_ts', f'"{os.environ["JUDGE_TS"]}"')
open(sf, 'w').write(text)
PY
    write_trace "JUDGE_RETRY" "judge" "" "$JUDGE_REASON_FAIL"
    cat >&2 <<RETRY_MSG
[builder-loop judge | iter=${NEXT_ITER} | judge=retry_transient | conf=${JUDGE_CONF_FAIL}]
原因：${JUDGE_REASON_FAIL}（疑似上轮 API 中断 / 网络抖动）
请重新执行同一任务，不要重做已经完成的部分。

本消息来自 builder-loop 自动判定 agent，非用户输入。
RETRY_MSG
    exit 2
  fi
fi

# 早停判断
ESTOP="$(bash "${SKILL_DIR}/early-stop-check.sh" "$STATE_FILE" "$LOG_PATH")"
ESTOP_ACTION="$(echo "$ESTOP" | awk '{print $1}')"

if [ "$ESTOP_ACTION" = "STOP" ]; then
  REASON="$(echo "$ESTOP" | awk '{print $2}')"
  echo "[builder-loop] ⛔ early stop at iter ${NEXT_ITER}, reason=${REASON}" >&2
  write_trace "EARLY_STOP" "" "" "$REASON"
  # V1.8.1: 不再"改 active=false 留僵尸"，直接归档 + exit 2 注入让 builder 立即 AskUserQuestion
  # 原行为 exit 0 需要 builder 在下一轮 user prompt 时才发现早停；新行为 builder 当场反应
  write_processed_cursor "$PROJECT_ROOT"
  archive_to_legacy "$STATE_FILE" "early_stop_${REASON}"
  cat >&2 <<EARLY_STOP_MSG
[builder-loop] ⛔ Auto-loop 早停 (iter=${NEXT_ITER}, reason=${REASON})。状态已归档到 legacy/。
请立即用 AskUserQuestion 询问用户下一步：
  - 继续手动调试（loop 已停，代码仍在当前 worktree）
  - 放弃本次任务（后续可 git worktree remove）
  - 重新进 loop（调 setup-builder-loop.sh 起新 slug）
早停原因说明：
  max_iter                 — 达最大迭代上限
  no_progress              — 连续多轮错误 hash 完全一致，builder 无进展
  error_growth             — 错误数持续增长
  suspected_test_tampering — 疑似修改测试绕 PASS_CMD
EARLY_STOP_MSG
  exit 2
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
