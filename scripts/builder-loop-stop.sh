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

# V2.5: stop hook 可观测性 — debug log 写到 <P>/.claude/builder-loop/stop-hook-debug.log
# 目的：c1（loop 哑火）/ c5（自激空转）这类间歇性 stop hook 行为问题需要 forensic 数据
# 设计：
#   - 路径：<PROJECT_ROOT>/.claude/builder-loop/stop-hook-debug.log（多项目隔离 + .gitignore 排除）
#   - 格式：每行 1 条 NDJSON（ts/session/cwd/slug/phase/details）
#   - 滚动：默认 1 MB（BUILDER_LOOP_DEBUG_LOG_MAX_BYTES env 可调），保留 5 个 .1-.5
#   - IO 失败容忍：mkdir/写入末尾 || true，任何失败都不阻断 hook 主流程
#   - flock 之前也写：entry / locate_result phase 必须在 exec 200>... 之前完成
DEBUG_LOG_ROTATE_CHECKED=0
_DEBUG_PROBED_ROOT=""
debug_log() {
  local phase="$1" details="${2:-{\}}"
  local log_dir log_file root
  # 路径解析：PROJECT_ROOT 锚定后用之；否则用 CWD 向上 5 层探测 .claude/loop.yml
  # 防止 entry / locate_result phase 时（PROJECT_ROOT 未赋值）日志写到 CWD 子目录与
  # 后续 phase 写到 PROJECT_ROOT 不一致 → 同次触发日志分散在两个文件破坏 forensic
  if [ -n "${PROJECT_ROOT:-}" ]; then
    root="$PROJECT_ROOT"
  else
    if [ -z "$_DEBUG_PROBED_ROOT" ]; then
      local _d="${CWD:-$(pwd 2>/dev/null || echo .)}"
      for _ in 1 2 3 4 5; do
        if [ -f "${_d}/.claude/loop.yml" ]; then
          _DEBUG_PROBED_ROOT="$_d"
          break
        fi
        [ "$_d" = "/" ] && break
        _d="$(dirname "$_d")"
      done
    fi
    [ -z "$_DEBUG_PROBED_ROOT" ] && return 0
    root="$_DEBUG_PROBED_ROOT"
  fi
  log_dir="${root}/.claude/builder-loop"
  log_file="${log_dir}/stop-hook-debug.log"
  mkdir -p "$log_dir" 2>/dev/null || return 0
  # rotate（lazy check 避免每次 stat IO）
  if [ "$DEBUG_LOG_ROTATE_CHECKED" -eq 0 ] && [ -f "$log_file" ]; then
    local sz max
    max="${BUILDER_LOOP_DEBUG_LOG_MAX_BYTES:-1048576}"
    sz="$(stat -c%s "$log_file" 2>/dev/null || stat -f%z "$log_file" 2>/dev/null || echo 0)"
    if [ "${sz:-0}" -gt "$max" ]; then
      [ -f "${log_file}.5" ] && rm -f "${log_file}.5" 2>/dev/null || true
      for i in 4 3 2 1; do
        [ -f "${log_file}.$i" ] && mv "${log_file}.$i" "${log_file}.$((i+1))" 2>/dev/null || true
      done
      mv "$log_file" "${log_file}.1" 2>/dev/null || true
    fi
    DEBUG_LOG_ROTATE_CHECKED=1
  fi
  # 用 python3 拼 JSON 防引号 / 特殊字符破格式
  TS="$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)" \
  PHASE="$phase" DETAILS="$details" CWD="${CWD:-}" \
  SESSION="${HOOK_SESSION_ID:-}" SLUG="${SLUG:-}" \
  LOG_FILE="$log_file" \
    python3 -c "
import os, json
try:
    details = json.loads(os.environ.get('DETAILS') or '{}')
except Exception:
    details = {'raw': os.environ.get('DETAILS', '')}
line = {
    'ts': os.environ.get('TS',''),
    'session': os.environ.get('SESSION','')[:8],
    'cwd': os.environ.get('CWD',''),
    'slug': os.environ.get('SLUG',''),
    'phase': os.environ.get('PHASE',''),
    'details': details,
}
with open(os.environ['LOG_FILE'], 'a') as f:
    f.write(json.dumps(line, ensure_ascii=False) + '\n')
" 2>/dev/null || true
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
# V2.5: session_id 给 debug log 关联，便于跨 hook 触发追踪
HOOK_SESSION_ID="$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")"

# V2.5: entry phase（在 locate / flock 之前）— 即使后续 exit 0 早退也能看到 hook 触发
debug_log "entry" "$(CWD_J="$CWD" TP="$TRANSCRIPT_PATH" python3 -c "
import os, json
print(json.dumps({'cwd': os.environ.get('CWD_J',''), 'transcript_path': os.environ.get('TP','')}))
" 2>/dev/null || echo '{}')"

# ---- V3.2: 读 local.md slug 精确定位 state（取代 CWD 猜测）----
# 从 CWD 向上找 .claude/builder-loop.local.md → 读 slug → 拼 state 路径
# 无 local.md / 无 slug / state 不存在 → exit 0 放行（不兜底激活）
PROJECT_ROOT=""
STATE_FILE=""
RUN_CWD=""
_d="$CWD"
for _i in 1 2 3 4 5; do
  if [ -f "${_d}/.claude/builder-loop.local.md" ]; then
    PROJECT_ROOT="$_d"
    break
  fi
  [ "$_d" = "/" ] && break
  _d="$(dirname "$_d")"
done

if [ -n "$PROJECT_ROOT" ]; then
  _LOCAL_MD="${PROJECT_ROOT}/.claude/builder-loop.local.md"
  _SLUG="$(grep -E '^slug:' "$_LOCAL_MD" 2>/dev/null | head -1 | sed -E 's/^slug:[[:space:]]*"?([^"]*)"?.*/\1/' || true)"
  if [ -n "$_SLUG" ]; then
    STATE_FILE="${PROJECT_ROOT}/.claude/builder-loop/state/${_SLUG}.yml"
  fi
fi

debug_log "locate_result" "$(SF="${STATE_FILE:-}" PR="${PROJECT_ROOT:-}" python3 -c "
import os, json
print(json.dumps({'state_file': os.environ.get('SF',''), 'project_root': os.environ.get('PR',''), 'method': 'local_md_slug'}))
" 2>/dev/null || echo '{}')"

if [ -z "$PROJECT_ROOT" ] || [ -z "$STATE_FILE" ] || [ ! -f "$STATE_FILE" ]; then
  debug_log "exit" '{"code":0,"reason":"no_local_md_or_no_state"}'
  exit 0
fi

# 从 state 提取字段
PROJECT_ROOT_FIELD="$(grep -E '^project_root:' "$STATE_FILE" 2>/dev/null | head -1 | sed -E 's/^project_root:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || true)"
MAIN_REPO_PATH_FIELD="$(grep -E '^main_repo_path:' "$STATE_FILE" 2>/dev/null | head -1 | sed -E 's/^main_repo_path:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || true)"
WORKTREE_PATH_FIELD="$(grep -E '^worktree_path:' "$STATE_FILE" 2>/dev/null | head -1 | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || true)"
if [ -n "$MAIN_REPO_PATH_FIELD" ]; then
  PROJECT_ROOT="$MAIN_REPO_PATH_FIELD"
  RUN_CWD="$PROJECT_ROOT_FIELD"
else
  PROJECT_ROOT="$PROJECT_ROOT_FIELD"
  if [ -n "$WORKTREE_PATH_FIELD" ] && [ -d "$WORKTREE_PATH_FIELD" ]; then
    RUN_CWD="$WORKTREE_PATH_FIELD"
  else
    RUN_CWD="$PROJECT_ROOT_FIELD"
  fi
fi
if [ -z "$PROJECT_ROOT" ] || [ ! -d "$PROJECT_ROOT" ]; then
  PROJECT_ROOT="$(cd "$(dirname "$STATE_FILE")/../../.." 2>/dev/null && pwd -P || echo "")"
fi
if [ -z "$RUN_CWD" ] || [ ! -d "$RUN_CWD" ]; then
  RUN_CWD="$PROJECT_ROOT"
fi
if [ -z "$PROJECT_ROOT" ]; then
  debug_log "exit" '{"code":0,"reason":"no_project_root"}'
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
  debug_log "flock_acquire" "$(LF="$LOCK_FILE" python3 -c "
import os, json
print(json.dumps({'lock_file': os.environ.get('LF',''), 'acquired': False}))
" 2>/dev/null || echo '{}')"
  debug_log "exit" '{"code":0,"reason":"lock_held_by_other"}'
  exit 0
fi
debug_log "flock_acquire" "$(LF="$LOCK_FILE" python3 -c "
import os, json
print(json.dumps({'lock_file': os.environ.get('LF',''), 'acquired': True}))
" 2>/dev/null || echo '{}')"

# V3.2: 兜底激活已移除（V3.0 要求 builder 先 setup 再写代码，不依赖 hook 自动启动）
RUN_CWD="${RUN_CWD:-$PROJECT_ROOT}"


# ---- 1. 状态文件不存在或非活跃 → 放行 ----
if [ ! -f "$STATE_FILE" ]; then
  debug_log "exit" '{"code":0,"reason":"state_file_missing"}'
  exit 0
fi
ACTIVE="$(grep -E '^active:' "$STATE_FILE" | head -1 | awk '{print $2}')"
if [ "$ACTIVE" != "true" ]; then
  # V1.8.1: 非活跃 state 视为僵尸（手动编辑 / 早停遗留），归档后放行
  # 防止下次 builder 进场误把僵尸当活跃 loop
  echo "[builder-loop] 🧟 state active='${ACTIVE}' (非 true)，归档到 legacy/ 后放行" >&2
  archive_to_legacy "$STATE_FILE" "zombie_inactive"
  debug_log "exit" "$(A="$ACTIVE" python3 -c "
import os, json
print(json.dumps({'code':0,'reason':'zombie_inactive','active': os.environ.get('A','')}))
" 2>/dev/null || echo '{}')"
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

# ---- V3.0: PASS_CMD 前多层闸（命中即静默 exit 0） ----
# 闸顺序（早闸优先，成本低到高）：
#   L1  phase=passed_pending_review → 不跑（牌子挂着等 reviewer 审）
#       特例：worktree 出现 dirty/新 commit → 自愈回 active 继续跑（reviewer 反馈修复路径）
#   L2A transcript 末是 pending AskUserQuestion → 不跑（builder 等用户答）
#   L2B worktree HEAD == last_iter_head + git status 空 → 不跑（无改动 thinking/讨论）
#       bare 模式（无 worktree_path）使用 PROJECT_ROOT 作 git 路径
#   L3  .claude/builder-loop/<slug>.pause 文件存在 → 不跑（builder 主动 pause）

# 老 state（V2.x 创建，无 phase 字段）兼容：stderr warning + 隐式升级（fall-through 到 PASS_CMD 路径，
# 跑完 PASS_CMD 后写 phase=passed_pending_review 自动升级 schema）。
# 设计偏离 spec：spec 要求 AskUserQuestion 阻断，实施改为"提示 + 隐式升级"——更平滑、跨 1-2 个版本周期所有
# 已接入项目自动升级到 V3.0 schema，不打断用户工作流。
PHASE_FIELD="$(grep -E '^phase:' "$STATE_FILE" 2>/dev/null | head -1 | sed -E 's/^phase:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || echo "")"
if [ -z "$PHASE_FIELD" ] && grep -qE '^active:' "$STATE_FILE" 2>/dev/null; then
  echo "[builder-loop] ⚠️  检测到 V2.x 老 state（无 phase 字段）：${STATE_FILE}" >&2
  echo "                本轮 hook 已自动升级到 V3.0 schema（PASS 后自动写 phase=passed_pending_review）" >&2
  debug_log "old_state_compat" '{"action":"warn_and_upgrade","missing_field":"phase"}'
fi
# L1: phase 闸 + worktree 改动兜底自愈
if [ "$PHASE_FIELD" = "passed_pending_review" ]; then
  WT_PATH_GATE="$(grep -E '^worktree_path:' "$STATE_FILE" 2>/dev/null | head -1 | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || echo "")"
  WT_HAS_CHANGES=0
  if [ -n "$WT_PATH_GATE" ] && [ -d "$WT_PATH_GATE" ]; then
    LIH_GATE="$(grep -E '^last_iter_head:' "$STATE_FILE" 2>/dev/null | head -1 | sed -E 's/^last_iter_head:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || echo "")"
    CUR_HEAD_GATE="$(git -C "$WT_PATH_GATE" rev-parse --short HEAD 2>/dev/null || echo "")"
    WT_STATUS_GATE="$(git -C "$WT_PATH_GATE" status --porcelain 2>/dev/null || echo "")"
    if [ -n "$WT_STATUS_GATE" ]; then
      WT_HAS_CHANGES=1
    elif [ -n "$LIH_GATE" ] && [ -n "$CUR_HEAD_GATE" ] && [ "$LIH_GATE" != "$CUR_HEAD_GATE" ]; then
      WT_HAS_CHANGES=1
    fi
  fi
  if [ "$WT_HAS_CHANGES" = "1" ]; then
    STATE_FILE="$STATE_FILE" python3 -c "
import os, re
sf = os.environ['STATE_FILE']
text = open(sf).read()
text = re.sub(r'^phase:.*\$', 'phase: \"active\"', text, flags=re.M)
open(sf, 'w').write(text)
" 2>/dev/null || true
    echo "[builder-loop] L1 phase 自愈：worktree 检测到改动，phase passed_pending_review → active" >&2
    debug_log "phase_self_heal" '{"from":"passed_pending_review","to":"active","reason":"worktree_changed"}'
  else
    debug_log "exit" '{"code":0,"reason":"l1_phase_passed_pending_review"}'
    exit 0
  fi
fi

# L2A: transcript 末尾 pending AskUserQuestion 静默
if [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ]; then
  ASKUQ_PENDING="$(TP="$TRANSCRIPT_PATH" python3 - <<'PY' 2>/dev/null || echo "false"
import os, json
tp = os.environ.get('TP', '')
try:
    with open(tp) as f:
        lines = [l for l in f if l.strip()]
except Exception:
    print('false'); raise SystemExit
if not lines:
    print('false'); raise SystemExit
last_id = None
last_idx = -1
for i, line in enumerate(lines):
    try:
        obj = json.loads(line)
    except Exception:
        continue
    if obj.get('type') == 'assistant':
        msg = obj.get('message', {})
        for blk in msg.get('content', []) or []:
            if blk.get('type') == 'tool_use' and blk.get('name') == 'AskUserQuestion':
                last_id = blk.get('id')
                last_idx = i
if not last_id:
    print('false'); raise SystemExit
for line in lines[last_idx+1:]:
    try:
        obj = json.loads(line)
    except Exception:
        continue
    if obj.get('type') == 'user':
        msg = obj.get('message', {})
        cont = msg.get('content', []) or []
        if isinstance(cont, list):
            for blk in cont:
                if isinstance(blk, dict) and blk.get('type') == 'tool_result' and blk.get('tool_use_id') == last_id:
                    print('false'); raise SystemExit
print('true')
PY
)"
  if [ "$ASKUQ_PENDING" = "true" ]; then
    debug_log "exit" '{"code":0,"reason":"l2a_askuq_pending"}'
    exit 0
  fi
fi

# L2B: HEAD == last_iter_head + git status 空 静默
# worktree 模式用 worktree_path；bare 模式（worktree_path 空）用 PROJECT_ROOT 主仓
WT_PATH_L2B="$(grep -E '^worktree_path:' "$STATE_FILE" 2>/dev/null | head -1 | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || echo "")"
if [ -n "$WT_PATH_L2B" ] && [ -d "$WT_PATH_L2B" ]; then
  GIT_PATH_L2B="$WT_PATH_L2B"
elif [ -n "${PROJECT_ROOT:-}" ] && [ -d "$PROJECT_ROOT" ]; then
  GIT_PATH_L2B="$PROJECT_ROOT"
else
  GIT_PATH_L2B=""
fi
if [ -n "$GIT_PATH_L2B" ]; then
  LIH_L2B="$(grep -E '^last_iter_head:' "$STATE_FILE" 2>/dev/null | head -1 | sed -E 's/^last_iter_head:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || echo "")"
  CUR_HEAD_L2B="$(git -C "$GIT_PATH_L2B" rev-parse --short HEAD 2>/dev/null || echo "")"
  WT_STATUS_L2B="$(git -C "$GIT_PATH_L2B" status --porcelain 2>/dev/null || echo "")"
  if [ -n "$LIH_L2B" ] && [ "$LIH_L2B" = "$CUR_HEAD_L2B" ] && [ -z "$WT_STATUS_L2B" ]; then
    debug_log "exit" '{"code":0,"reason":"l2b_no_diff"}'
    exit 0
  fi
fi

# L3: pause 文件 静默
SLUG_L3="$(grep -E '^slug:' "$STATE_FILE" 2>/dev/null | head -1 | sed -E 's/^slug:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || echo "")"
if [ -n "$SLUG_L3" ]; then
  PAUSE_FILE="${PROJECT_ROOT}/.claude/builder-loop/${SLUG_L3}.pause"
  if [ -f "$PAUSE_FILE" ]; then
    echo "[builder-loop] L3 pause 文件命中，hook 静默：${PAUSE_FILE}" >&2
    debug_log "exit" '{"code":0,"reason":"l3_pause_file"}'
    exit 0
  fi
fi
# ---- /V3.0 闸 ----

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
# V2.0：第一参 = 干活的地方（worktree 或主仓），LOOP_YML 从此读、PASS_CMD 在此跑；
#       第三参 = 主仓（日志归档目录基址）
echo "[builder-loop] 🔄 iter ${NEXT_ITER}: 正在跑 PASS_CMD（cwd=${RUN_CWD}）..." >&2
debug_log "pass_cmd_start" "$(IT="$NEXT_ITER" RC="$RUN_CWD" python3 -c "
import os, json
print(json.dumps({'iter': int(os.environ.get('IT','0') or 0), 'run_cwd': os.environ.get('RC','')}))
" 2>/dev/null || echo '{}')"
RESULT="$(bash "${SKILL_DIR}/run-pass-cmd.sh" "$RUN_CWD" "$NEXT_ITER" "$PROJECT_ROOT" || true)"
LAST_LINE="$(echo "$RESULT" | tail -1)"
debug_log "pass_cmd_result" "$(LL="$LAST_LINE" python3 -c "
import os, json
last = os.environ.get('LL','')
parts = last.split()
print(json.dumps({
  'last_line': last,
  'result': 'PASS' if last == 'PASS' else 'FAIL' if parts and parts[0] == 'FAIL' else 'UNKNOWN',
  'last_stage': parts[1] if len(parts) > 1 else '',
  'log_path': ' '.join(parts[2:]) if len(parts) > 2 else '',
}))
" 2>/dev/null || echo '{}')"

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
    # V2.0: --project-root 传 RUN_CWD（干活的地方），让 judge 读 worktree 内 loop.yml + git diff worktree
    JUDGE_RESULT="$(bash "${SKILL_DIR}/run-judge-agent.sh" \
        --state-file "$STATE_FILE" \
        --project-root "$RUN_CWD" \
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
    # V2.0：与 PASS_CMD 一致从 RUN_CWD（worktree）读 loop.yml，让 worktree 内改 judge 配置立即生效；
    # 文件缺失时（worktree 未 commit loop.yml 的极少数场景）fallback 主仓
    NUDGE_LOOP_YML="${RUN_CWD}/.claude/loop.yml"
    [ ! -f "$NUDGE_LOOP_YML" ] && NUDGE_LOOP_YML="${PROJECT_ROOT}/.claude/loop.yml"
    if [ -f "$NUDGE_LOOP_YML" ]; then
      MAX_NUDGE_RAW="$(grep -E '^[[:space:]]+max_consecutive_nudges:' "$NUDGE_LOOP_YML" 2>/dev/null | head -1 | awk '{print $2}' || echo "")"
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
      # V2.3: reward hacking 命中 → 加三选项二次确认提示
      case "$JUDGE_REASON_OUT" in
        *suspected_reward_hack*)
          cat >&2 <<'RH_MSG'
[builder-loop reward-hack-guard] PASS_CMD 配置改动疑似 reward hacking。
立即用 AskUserQuestion 列三选项让用户决策：
  ① quarantine 该测试（pytest.mark.skip / --ignore=... / xfail）+ 留 followup issue
  ② 修测试根因（race / sleep / 共享状态 改成同步原语）
  ③ 保留 cmd 改动（需在回复中给出必要性理由）
禁止单方面继续完成 commit 流程。
RH_MSG
          ;;
      esac
      debug_log "judge_result" "$(JA="continue_nudge" JD="false" JC="$JUDGE_CONF_OUT" JR="$JUDGE_REASON_OUT" python3 -c "
import os, json
print(json.dumps({'action': os.environ.get('JA',''), 'downgraded': os.environ.get('JD','false') == 'true', 'confidence': float(os.environ.get('JC','0') or 0), 'reason': os.environ.get('JR','')[:200]}))
" 2>/dev/null || echo '{}')"
      debug_log "exit" '{"code":2,"reason":"judge_continue_nudge"}'
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

  # ---- V3.0 reviewer-as-gate: worktree 模式分支 ----
  # 判定 worktree 模式 → 调 worktree-commit-only.sh 只 commit 不 merge
  # bare 模式（worktree_path 空）fall-through 到下面 V2.x 路径（merge-worktree-back NOOP）
  WORKTREE_PATH_PASS="$(grep -E '^worktree_path:' "$STATE_FILE" 2>/dev/null | head -1 | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || echo "")"
  if [ -n "$WORKTREE_PATH_PASS" ] && [ -d "$WORKTREE_PATH_PASS" ]; then
    COMMIT_OUT="$(bash "${SKILL_DIR}/worktree-commit-only.sh" "$STATE_FILE" 2>&1 || true)"
    COMMIT_LAST="$(echo "$COMMIT_OUT" | tail -1)"
    COMMIT_ACTION="$(echo "$COMMIT_LAST" | awk '{print $1}')"
    debug_log "commit_only_result" "$(CA="$COMMIT_ACTION" CL="$COMMIT_LAST" python3 -c "
import os, json
print(json.dumps({'commit_action': os.environ.get('CA',''), 'commit_last_line': os.environ.get('CL','')[:200]}))
" 2>/dev/null || echo '{}')"

    case "$COMMIT_ACTION" in
      COMMIT_DONE|NOOP)
        NEW_HEAD_SHORT="$(echo "$COMMIT_LAST" | awk '{print $2}')"
        [ -z "$NEW_HEAD_SHORT" ] && NEW_HEAD_SHORT="$(git -C "$WORKTREE_PATH_PASS" rev-parse --short HEAD 2>/dev/null || echo "")"
        SLUG_PASS="$(grep -E '^slug:' "$STATE_FILE" 2>/dev/null | head -1 | sed -E 's/^slug:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || echo "")"
        DIFF_FILE_PASS="${PROJECT_ROOT}/.claude/reviewer-diff-${SLUG_PASS}.txt"
        PROJ_NAME_PASS="$(basename "$PROJECT_ROOT")"
        mkdir -p "${PROJECT_ROOT}/.claude/review_reports" 2>/dev/null || true
        REPORT_TS_PASS="$(date +%Y%m%d_%H%M%S)"
        REPORT_PATH_PASS="${PROJECT_ROOT}/.claude/review_reports/${PROJ_NAME_PASS}_${SLUG_PASS}_${REPORT_TS_PASS}.md"
        REVIEWER_FILES_PASS="$(git -C "$WORKTREE_PATH_PASS" diff --name-only "${PASS_START_HEAD_PREREAD}..HEAD" 2>/dev/null | tr '\n' ',' | sed 's/,$//' || echo "")"
        git -C "$WORKTREE_PATH_PASS" diff "${PASS_START_HEAD_PREREAD}..HEAD" > "$DIFF_FILE_PASS" 2>/dev/null || echo "" > "$DIFF_FILE_PASS"

        # 写 state：phase=passed_pending_review + last_iter_head + reviewer_pending 段
        STATE_FILE="$STATE_FILE" NEW_HEAD="$NEW_HEAD_SHORT" \
          PASS_SH_PASS="$PASS_START_HEAD_PREREAD" RFILES_PASS="$REVIEWER_FILES_PASS" \
          DFILE_PASS="$DIFF_FILE_PASS" RPATH_PASS="$REPORT_PATH_PASS" \
          WAT_PASS="$(date -Iseconds 2>/dev/null || date +%s)" python3 - <<'PY' 2>/dev/null || true
import os, re
sf = os.environ['STATE_FILE']
text = open(sf).read()

def upsert(text, key, val):
    pat = re.compile(rf'^{key}:.*$', re.M)
    if pat.search(text):
        return pat.sub(f'{key}: {val}', text)
    if not text.endswith('\n'):
        text += '\n'
    return text + f'{key}: {val}\n'

text = upsert(text, 'phase', '"passed_pending_review"')
text = upsert(text, 'last_iter_head', f'"{os.environ["NEW_HEAD"]}"')

# 删除老 reviewer_pending 段（块内每行以 2 空格缩进）
text = re.sub(r'^reviewer_pending:\n(?:  .+\n)*', '', text, flags=re.M)
if not text.endswith('\n'):
    text += '\n'
pending_block = (
    'reviewer_pending:\n'
    f'  pass_start_head: "{os.environ["PASS_SH_PASS"]}"\n'
    f'  reviewer_files: "{os.environ["RFILES_PASS"]}"\n'
    f'  diff_file: "{os.environ["DFILE_PASS"]}"\n'
    f'  report_path: "{os.environ["RPATH_PASS"]}"\n'
    f'  written_at: "{os.environ["WAT_PASS"]}"\n'
)
text += pending_block
open(sf, 'w').write(text)
PY

        write_processed_cursor "$PROJECT_ROOT"
        echo "[builder-loop] ✅ PASS at iter ${NEXT_ITER} (worktree commit, phase=passed_pending_review)" >&2
        write_trace "PASS"

        cat >&2 <<PASS_MSG
[builder-loop] ✅ PASS_CMD 全部阶段通过（iter ${NEXT_ITER}）。已在 worktree 内 commit，等待 reviewer 通过后才合主线。
phase=passed_pending_review
slug=${SLUG_PASS}
state_file=${STATE_FILE}
worktree_path=${WORKTREE_PATH_PASS}

请继续 Builder 后续流程：
1. Read state.yml 拿 reviewer_pending 段（含 reviewer_files / report_path / diff_file）
2. spawn reviewer subagent 评审
3. 反馈分支：
   - 0🔴 通过       → bash ${SKILL_DIR}/merge-and-cleanup.sh ${STATE_FILE}
   - 🟡/🔵 非阻塞   → 在 worktree 内 Edit/Write 修复 → 下一轮 PASS_CMD（hook 自动检测 dirty 改回 active）
   - 🔴 阻塞        → AskUserQuestion 让用户选 [继续修 / abandon-loop.sh]
PASS_MSG

        debug_log "exit" "$(IT="$NEXT_ITER" python3 -c "
import os, json
print(json.dumps({'code':2,'reason':'pass_done_v3','iter': int(os.environ.get('IT','0') or 0), 'phase': 'passed_pending_review'}))
" 2>/dev/null || echo '{}')"
        exit 2
        ;;
      *)
        echo "[builder-loop] ⚠️  worktree-commit-only.sh 失败：${COMMIT_LAST}" >&2
        debug_log "exit" '{"code":2,"reason":"worktree_commit_only_error"}'
        cat >&2 <<COMMIT_ERR
[builder-loop] ⚠️  worktree commit 失败（iter ${NEXT_ITER}）
${COMMIT_OUT}
请检查 worktree 状态后重试。
COMMIT_ERR
        exit 2
        ;;
    esac
  fi
  # ---- /V3.0 worktree 模式分支结束；以下走 bare 模式 V2.x 路径 ----

  # T2.7：worktree 启用时先合回主干（fast-forward / rebase / 标记仲裁）
  MERGE_OUT="$(bash "${SKILL_DIR}/merge-worktree-back.sh" "$STATE_FILE" 2>&1 || true)"
  MERGE_LAST="$(echo "$MERGE_OUT" | tail -1)"
  MERGE_ACTION="$(echo "$MERGE_LAST" | awk '{print $1}')"
  debug_log "merge_result" "$(MA="$MERGE_ACTION" ML="$MERGE_LAST" python3 -c "
import os, json
print(json.dumps({'merge_action': os.environ.get('MA',''), 'merge_last_line': os.environ.get('ML','')[:200]}))
" 2>/dev/null || echo '{}')"
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
      debug_log "exit" "$(IT="$NEXT_ITER" MA="$MERGE_ACTION" python3 -c "
import os, json
print(json.dumps({'code':2,'reason':'pass_done','iter': int(os.environ.get('IT','0') or 0), 'merge_action': os.environ.get('MA','')}))
" 2>/dev/null || echo '{}')"
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
      debug_log "exit" '{"code":2,"reason":"need_arbitration"}'
      exit 2
      ;;
    *)
      # M5: 不再静默删 state；显式把 merge 完整输出抛给 builder + exit 2 让 builder 收到通知
      # 历史教训：V1.9.1 之前 merge-worktree-back.sh 因 grep+pipefail 静默退出导致 MERGE_OUT 为空，
      # 走到这里 echo 一行后 exit 0 + rm state，state 丢了下一轮进 builder 找不着→数据丢失
      cat >&2 <<MERGE_FAIL_MSG
[builder-loop] ❌ merge-worktree-back.sh 未识别结果（MERGE_LAST="${MERGE_LAST}"），不删除 state 也不走 reviewer。
完整输出：
${MERGE_OUT}
请检查 worktree=$(grep -E '^worktree_path:' "$STATE_FILE" 2>/dev/null | head -1) 与主仓状态后手动决定下一步。
MERGE_FAIL_MSG
      debug_log "exit" "$(MA="$MERGE_ACTION" python3 -c "
import os, json
print(json.dumps({'code':2,'reason':'merge_failed','merge_action':os.environ.get('MA','')}))
" 2>/dev/null || echo '{}')"
      exit 2
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
      --project-root "$RUN_CWD" \
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
    debug_log "judge_result" "$(JA='retry_transient' JD='false' JC="$JUDGE_CONF_FAIL" JR="$JUDGE_REASON_FAIL" python3 -c "
import os, json
print(json.dumps({'action': os.environ.get('JA',''), 'downgraded': os.environ.get('JD','false') == 'true', 'confidence': float(os.environ.get('JC','0') or 0), 'reason': os.environ.get('JR','')[:200]}))
" 2>/dev/null || echo '{}')"
    debug_log "exit" '{"code":2,"reason":"judge_retry_transient"}'
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
  debug_log "early_stop" "$(IT="$NEXT_ITER" R="$REASON" python3 -c "
import os, json
print(json.dumps({'iter': int(os.environ.get('IT','0') or 0), 'reason': os.environ.get('R','')}))
" 2>/dev/null || echo '{}')"

  # V2.3: dirty 模式 → 还原主仓 stash（apply 不 pop，副本保留供事后查阅 / 用户决定要不要 drop）
  # 防御性 trim：sed 已剥引号，这里再 tr 删 [:space:] 防极端 corner case 路径前后空白污染 git 命令
  PRE_LOOP_STASH_REF_ES="$(grep -E '^pre_loop_stash_ref:' "$STATE_FILE" 2>/dev/null | head -1 | sed -E 's/^pre_loop_stash_ref:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' | tr -d '[:space:]' || true)"
  WORKTREE_MODE_ES="$(grep -E '^worktree_mode:' "$STATE_FILE" 2>/dev/null | head -1 | sed -E 's/^worktree_mode:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' | tr -d '[:space:]' || true)"
  WORKTREE_PATH_ES="$(grep -E '^worktree_path:' "$STATE_FILE" 2>/dev/null | head -1 | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || true)"
  STASH_RESTORED="no"
  if [ "$WORKTREE_MODE_ES" = "dirty" ] && [ -n "$PRE_LOOP_STASH_REF_ES" ]; then
    if git -C "$PROJECT_ROOT" stash apply "$PRE_LOOP_STASH_REF_ES" 2>/dev/null; then
      STASH_RESTORED="yes"
    else
      echo "[builder-loop] ⚠️  EARLY_STOP 还原 stash 失败（可能主仓被中途修改），stash 副本仍保留：${PRE_LOOP_STASH_REF_ES:0:8}" >&2
      STASH_RESTORED="conflict"
    fi
  fi

  # V1.8.1: 不再"改 active=false 留僵尸"，直接归档 + exit 2 注入让 builder 立即 AskUserQuestion
  # 原行为 exit 0 需要 builder 在下一轮 user prompt 时才发现早停；新行为 builder 当场反应
  write_processed_cursor "$PROJECT_ROOT"
  archive_to_legacy "$STATE_FILE" "early_stop_${REASON}"

  # V2.3: legacy 补一个 .info 文件记录现场上下文（worktree path / stash hash / 还原结果）
  LEGACY_INFO="${PROJECT_ROOT}/.claude/builder-loop/legacy/$(date +%Y%m%d-%H%M%S)-early_stop_${REASON}.info"
  mkdir -p "$(dirname "$LEGACY_INFO")" 2>/dev/null || true
  {
    echo "reason: early_stop_${REASON}"
    echo "iter: ${NEXT_ITER}"
    echo "worktree_path: ${WORKTREE_PATH_ES}"
    echo "worktree_mode: ${WORKTREE_MODE_ES}"
    echo "pre_loop_stash_ref: ${PRE_LOOP_STASH_REF_ES}"
    echo "stash_restored: ${STASH_RESTORED}"
    echo "ts: $(date -Iseconds 2>/dev/null || date '+%Y-%m-%dT%H:%M:%S')"
  } > "$LEGACY_INFO" 2>/dev/null || true

  cat >&2 <<EARLY_STOP_MSG
[builder-loop] ⛔ Auto-loop 早停 (iter=${NEXT_ITER}, reason=${REASON})。状态已归档到 legacy/。
现场保留：worktree=${WORKTREE_PATH_ES:-<bare>} | dirty stash 副本=${PRE_LOOP_STASH_REF_ES:0:8}（restored=${STASH_RESTORED}）
注：dirty 模式下主仓 stash 副本不会自动 drop（仅 PASS 路径 drop）。继续 / 放弃 / 重新进 loop 后请视情况手动 git stash drop 清理。
请立即用 AskUserQuestion 询问用户下一步：
  - 继续手动调试（loop 已停，代码仍在当前 worktree，主仓 dirty 已还原；事后清理 git stash drop）
  - 放弃本次任务（后续可 git worktree remove + git stash drop）
  - 重新进 loop（调 setup-builder-loop.sh 起新 slug）
早停原因说明：
  max_iter                 — 达最大迭代上限
  no_progress              — 连续多轮错误 hash 完全一致，builder 无进展
  error_growth             — 错误数持续增长
  suspected_test_tampering — 疑似修改测试绕 PASS_CMD
EARLY_STOP_MSG
  debug_log "exit" "$(R="$REASON" python3 -c "
import os, json
print(json.dumps({'code':2,'reason':'early_stop_'+os.environ.get('R','unknown')}))
" 2>/dev/null || echo '{}')"
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
debug_log "exit" "$(IT="$NEXT_ITER" S="$STAGE" python3 -c "
import os, json
print(json.dumps({'code':2,'reason':'fail_continue','iter': int(os.environ.get('IT','0') or 0), 'stage': os.environ.get('S','')}))
" 2>/dev/null || echo '{}')"
exit 2
