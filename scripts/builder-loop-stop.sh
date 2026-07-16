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
#   2. 调 locate-state.sh 用 CWD→state 匹配定位 state 文件
#      - 未找到 → exit 0 立即放行
#   3. 跑 run-pass-cmd.sh
#      - PASS → 删状态文件、exit 2 让 CC 继续执行 reviewer/commit pipeline
#      - FAIL → 调 extract-error.sh + early-stop-check.sh
#        - early-stop → 写 stopped_reason、删状态、exit 0（让 CC 停下，builder 自行 AskUserQuestion）
#        - 否则 → 更新 iter / hash / count，exit 2 让 CC 继续修复

set -euo pipefail

# V4.5: SKILL_DIR 延迟解析 — no-op 路径不需要，省掉 cd+pwd 的 2 次 stat
_SCRIPT_DIR="${BASH_SOURCE[0]%/*}"
_resolve_skill_dir() {
  SKILL_DIR="$(cd "${_SCRIPT_DIR}/../skills/builder-loop/scripts" && pwd 2>/dev/null)" || \
    SKILL_DIR="$HOME/.claude/skills/builder-loop/scripts"
}
SKILL_DIR=""

# V1.8.1: state 归档到 legacy
# 两个调用点：① 发现无 phase 字段的僵尸 state；② EARLY_STOP 不再改字段，直接归档
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
#   ① zombie_no_phase（非本轮 loop 归档，HEAD 可能未经处理，写游标会误阻塞下次合法激活）
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

# ---- 解析 stdin（V4.5: 零子进程快速路径 — bash 内置解析 + 内联 locate）----
# V4.4 已消除 python3 冷启动；V4.5 进一步消除 sed 子进程 + bash locate-state.sh 子进程，
# 将 no-op 路径的 fork+exec 从 2 次降到 0 次，stat 从 ~20 次降到 ~3 次。
# NFS/IO 压力大时从分钟级降到秒级以内。
INPUT="$(cat)"
# bash 内置字符串操作提取 CWD（替代 sed 子进程）
_after_cwd="${INPUT#*\"cwd\"}"
if [ "$_after_cwd" != "$INPUT" ]; then
  _after_colon="${_after_cwd#*:}"
  _after_quote="${_after_colon#*\"}"
  CWD="${_after_quote%%\"*}"
else
  CWD=""
fi
[ -z "$CWD" ] && CWD="$(pwd 2>/dev/null || echo ".")"
CWD="${CWD%/}"

# bash 内置字符串操作提取 session_id（locate-state.sh 策略 1.5 需要）
_after_sid="${INPUT#*\"session_id\"}"
if [ "$_after_sid" != "$INPUT" ]; then
  _sid_colon="${_after_sid#*:}"
  _sid_quote="${_sid_colon#*\"}"
  HOOK_SESSION_ID="${_sid_quote%%\"*}"
else
  HOOK_SESSION_ID=""
fi

# ---- 内联 locate-state 快速探测（V4.5: 零子进程）----
# 先找 project root（loop.yml 所在目录），再检查 state 目录。
# 找到 loop.yml 但无 state → 写 no-op 日志后 exit 0。
# 找到 state → fall through 到完整的 locate-state.sh（需要精确的 worktree/bare/slug 匹配）。
_PROJECT_ROOT=""
_d="$CWD"
for _ in 1 2 3 4 5; do
  if [ -f "${_d}/.claude/loop.yml" ]; then
    _PROJECT_ROOT="$_d"
    break
  fi
  [ "$_d" = "/" ] && break
  _d="${_d%/*}"
  [ -z "$_d" ] && _d="/"
done

if [ -z "$_PROJECT_ROOT" ]; then
  # 无 loop.yml → 此项目未接入 builder-loop，直接退出（不写日志、不 spawn 子进程）
  exit 0
fi

# 有 loop.yml → 检查 state 目录是否有 .yml 文件
_STATE_DIR="${_PROJECT_ROOT}/.claude/builder-loop/state"
_HAS_STATE=0
if [ -d "$_STATE_DIR" ]; then
  for _sf in "$_STATE_DIR"/*.yml; do
    [ -e "$_sf" ] && _HAS_STATE=1 && break
  done
fi

if [ "$_HAS_STATE" -eq 0 ]; then
  # 有 loop.yml 但无 state → no-op 日志后退出
  _noop_log="${_PROJECT_ROOT}/.claude/builder-loop/stop-hook-debug.log"
  mkdir -p "${_PROJECT_ROOT}/.claude/builder-loop" 2>/dev/null || true
  printf '{"ts":"%s","phase":"no_op","cwd":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)" \
    "$CWD" >> "$_noop_log" 2>/dev/null || true
  exit 0
fi

# 有 state 文件 → 需要精确匹配，调完整的 locate-state.sh（此路径本来就要跑完整流程，子进程开销可接受）
[ -z "$SKILL_DIR" ] && _resolve_skill_dir
STATE_FILE="$(bash "$SKILL_DIR/locate-state.sh" "$CWD" "$HOOK_SESSION_ID" 2>/dev/null || echo "")"
if [ -z "$STATE_FILE" ] || [ ! -f "$STATE_FILE" ]; then
  # locate-state 精确匹配失败（state 存在但不属于当前 CWD）→ no-op
  _noop_log="${_PROJECT_ROOT}/.claude/builder-loop/stop-hook-debug.log"
  printf '{"ts":"%s","phase":"no_op","cwd":"%s","note":"state_exists_but_no_match"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)" \
    "$CWD" >> "$_noop_log" 2>/dev/null || true
  exit 0
fi

# ---- 找到 state：解析剩余 stdin 字段 ----
TRANSCRIPT_PATH="$(printf '%s' "$INPUT" | sed -n 's/.*"transcript_path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
# HOOK_SESSION_ID 已在 locate-state 之前用 bash 内置提取

PROJECT_ROOT=""
RUN_CWD=""

debug_log "entry" "{\"cwd\":\"${CWD}\",\"transcript_path\":\"${TRANSCRIPT_PATH}\"}"
debug_log "locate_result" "{\"state_file\":\"${STATE_FILE}\",\"method\":\"locate_state_sh\"}"

# ---- V3.7: owner_session_id 校验（防并发 session 越界）----
# 首次绑定时写入 session_id；后续校验匹配，不匹配 → 警告 + skip
STATE_OWNER="$(grep -E '^owner_session_id:' "$STATE_FILE" 2>/dev/null | head -1 | sed -E 's/^owner_session_id:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || echo "")"
if [ -z "$STATE_OWNER" ] && [ -n "$HOOK_SESSION_ID" ]; then
  # 首次绑定：写入 owner_session_id
  python3 -c "
import sys
text = open(sys.argv[1]).read()
# 插在 phase: 行之后
lines = text.split('\n')
out = []
for l in lines:
    out.append(l)
    if l.startswith('phase:'):
        out.append('owner_session_id: \"' + sys.argv[2] + '\"')
open(sys.argv[1], 'w').write('\n'.join(out))
" "$STATE_FILE" "$HOOK_SESSION_ID" 2>/dev/null || true
  debug_log "owner_bind" "$(SID="$HOOK_SESSION_ID" python3 -c "
import os, json; print(json.dumps({'session_id': os.environ['SID']}))" 2>/dev/null || echo '{}')"
elif [ -n "$STATE_OWNER" ] && [ -n "$HOOK_SESSION_ID" ] && [ "$STATE_OWNER" != "$HOOK_SESSION_ID" ]; then
  echo "[builder-loop] ⚠️ session mismatch: state owner=${STATE_OWNER:0:8} current=${HOOK_SESSION_ID:0:8}, skip（非本 session 的 state）" >&2
  debug_log "exit" "$(SO="$STATE_OWNER" CS="$HOOK_SESSION_ID" python3 -c "
import os, json; print(json.dumps({'code':0,'reason':'session_mismatch','owner':os.environ['SO'][:8],'current':os.environ['CS'][:8]}))" 2>/dev/null || echo '{}')"
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
PHASE_PRE="$(grep -E '^phase:' "$STATE_FILE" 2>/dev/null | head -1 | sed -E 's/^phase:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || echo "")"
if [ -z "$PHASE_PRE" ]; then
  echo "[builder-loop] 🧟 state 无 phase 字段（僵尸 / 损坏），归档到 legacy/ 后放行" >&2
  archive_to_legacy "$STATE_FILE" "zombie_no_phase"
  debug_log "exit" '{"code":0,"reason":"zombie_no_phase"}'
  exit 0
fi

# ---- (V4.0 removed) judge outcome backfill — judge 已被 reviewer Phase 0 吸收 ----

# ---- 2. 取当前 iter ----
ITER=$(grep -E '^iter:' "$STATE_FILE" | head -1 | awk '{print $2}')
ITER=${ITER:-0}
NEXT_ITER=$(( ITER + 1 ))

# ---- V3.0: PASS_CMD 前多层闸（命中即静默 exit 0） ----
# 闸顺序（早闸优先，成本低到高）：
#   L1  phase=passed_pending_review|e2e_pending → 不跑（牌子挂着等 reviewer 审 / tester 跑 e2e）
#       自愈：dirty/新 commit → active（修复路径）；e2e_pending + e2e_verified_head is-ancestor + diff 全非源码 → active（e2e 完成）
#   L2A transcript 末是 pending AskUserQuestion → 不跑（builder 等用户答）
#   L2B worktree HEAD == last_iter_head + git status 空 → 不跑（无改动 thinking/讨论）
#       bare 模式（无 worktree_path）使用 PROJECT_ROOT 作 git 路径
#   L3  .claude/builder-loop/<slug>.pause 文件存在 → 不跑（builder 主动 pause）

PHASE_FIELD="$PHASE_PRE"
# L1: phase 闸 + 改动/e2e完成 自愈
if [ "$PHASE_FIELD" = "passed_pending_review" ] || [ "$PHASE_FIELD" = "e2e_pending" ]; then
  WT_PATH_GATE="$(grep -E '^worktree_path:' "$STATE_FILE" 2>/dev/null | head -1 | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || echo "")"
  # bare 模式 fallback：worktree_path 空时用 PROJECT_ROOT（主仓）做 dirty 检测
  CHECK_PATH_GATE="${WT_PATH_GATE:-$PROJECT_ROOT}"
  SHOULD_HEAL=0
  HEAL_REASON=""
  if [ -n "$CHECK_PATH_GATE" ] && [ -d "$CHECK_PATH_GATE" ]; then
    LIH_GATE="$(grep -E '^last_iter_head:' "$STATE_FILE" 2>/dev/null | head -1 | sed -E 's/^last_iter_head:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || echo "")"
    CUR_HEAD_GATE="$(git -C "$CHECK_PATH_GATE" rev-parse --short HEAD 2>/dev/null || echo "")"
    STATUS_GATE="$(git -C "$CHECK_PATH_GATE" status --porcelain 2>/dev/null || echo "")"
    if [ -n "$STATUS_GATE" ] && [ "$PHASE_FIELD" != "e2e_pending" ]; then
      SHOULD_HEAL=1
      HEAL_REASON="dirty_changes"
    elif [ -n "$LIH_GATE" ] && [ -n "$CUR_HEAD_GATE" ] && [ "$LIH_GATE" != "$CUR_HEAD_GATE" ]; then
      SHOULD_HEAL=1
      HEAL_REASON="new_commit"
    fi
  fi
  # e2e_pending 额外自愈（V5.6）：e2e_verified_head 是 HEAD 祖先 + 中间 diff 全是非行为文件 → e2e 仍有效
  if [ "$PHASE_FIELD" = "e2e_pending" ] && [ "$SHOULD_HEAL" = "0" ]; then
    E2E_VH_L1="$(grep -E '^e2e_verified_head:' "$STATE_FILE" 2>/dev/null | head -1 | sed -E 's/^e2e_verified_head:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || echo "")"
    if [ -n "$E2E_VH_L1" ]; then
      if git -C "$CHECK_PATH_GATE" merge-base --is-ancestor "$E2E_VH_L1" HEAD 2>/dev/null; then
        E2E_DIFF_FILES="$(git -C "$CHECK_PATH_GATE" diff --name-only "$E2E_VH_L1"..HEAD 2>/dev/null || echo "")"
        if [ -z "$E2E_DIFF_FILES" ]; then
          SHOULD_HEAL=1
          HEAL_REASON="e2e_verified"
        else
          E2E_HAS_SOURCE=0
          while IFS= read -r _ef; do
            case "$_ef" in
              *.md|*.txt|docs/*|.claude/*) ;;
              *) E2E_HAS_SOURCE=1; break ;;
            esac
          done <<< "$E2E_DIFF_FILES"
          if [ "$E2E_HAS_SOURCE" = "0" ]; then
            SHOULD_HEAL=1
            HEAL_REASON="e2e_verified_ancestor_safe"
          fi
        fi
      fi
    fi
  fi
  if [ "$SHOULD_HEAL" = "1" ]; then
    STATE_FILE="$STATE_FILE" python3 -c "
import os, re
sf = os.environ['STATE_FILE']
text = open(sf).read()
text = re.sub(r'^phase:.*\$', 'phase: \"active\"', text, flags=re.M)
open(sf, 'w').write(text)
" 2>/dev/null || true
    echo "[builder-loop] L1 phase 自愈：${PHASE_FIELD} → active (reason=${HEAL_REASON})" >&2
    debug_log "phase_self_heal" "{\"from\":\"${PHASE_FIELD}\",\"to\":\"active\",\"reason\":\"${HEAL_REASON}\"}"
  else
    echo "[builder-loop] L1 静默：phase=${PHASE_FIELD}，等待代码变更后自动恢复" >&2
    debug_log "exit" "{\"code\":0,\"reason\":\"l1_phase_${PHASE_FIELD}\"}"
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
    echo "[builder-loop] L2A 静默：等待用户回答 AskUserQuestion" >&2
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
    echo "[builder-loop] L2B 静默：HEAD=${CUR_HEAD_L2B} 未变且无 dirty，等待代码改动" >&2
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
  'result': 'PASS' if last == 'PASS' else parts[0] if parts and parts[0] in ('FAIL', 'FATAL') else 'UNKNOWN',
  'last_stage': parts[1] if len(parts) > 1 else '',
  'log_path': ' '.join(parts[2:]) if len(parts) > 2 else '',
}))
" 2>/dev/null || echo '{}')"

# ---- 3a. PASS → 调 handle-pass-result.sh 统一处理（V5.4 提取） ----
if [ "$LAST_LINE" = "PASS" ]; then
  _resolve_skill_dir
  set +e
  HANDLE_PASS_OUT="$(bash "${SKILL_DIR}/handle-pass-result.sh" "$STATE_FILE" "$NEXT_ITER" "$RUN_CWD" "$PROJECT_ROOT" 2>/dev/null)"
  HANDLE_PASS_EC=$?
  set -e
  HANDLE_PASS_TYPE="$(echo "$HANDLE_PASS_OUT" | tail -1 | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('type',''))" 2>/dev/null || echo "")"

  # ---- V5.4: dispatch by handle-pass-result.sh exit code ----
  debug_log "handle_pass_result" "$(HP_EC="$HANDLE_PASS_EC" HP_TYPE="$HANDLE_PASS_TYPE" python3 -c "
import os, json
print(json.dumps({'exit_code': int(os.environ.get('HP_EC','0') or 0), 'type': os.environ.get('HP_TYPE','')}))
" 2>/dev/null || echo '{}')"

  case "$HANDLE_PASS_TYPE" in
    e2e_needed)
      # e2e 验证请求 — 从 JSON 提取字段，构造 inject 消息
      debug_log "e2e_inject" "cases found via handle-pass-result (iter=$NEXT_ITER)"
      _E2E_JSON="$(echo "$HANDLE_PASS_OUT" | tail -1)"
      _E2E_WTP="$(echo "$_E2E_JSON" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('worktree_path',''))" 2>/dev/null || echo "")"
      _E2E_CP="$(echo "$_E2E_JSON" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('e2e_cases_path',''))" 2>/dev/null || echo "")"
      _E2E_LVL="$(echo "$_E2E_JSON" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('e2e_level','full'))" 2>/dev/null || echo "full")"
      _E2E_CASES="$(echo "$_E2E_JSON" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('e2e_cases',''))" 2>/dev/null || echo "")"
      _E2E_TAID="$(echo "$_E2E_JSON" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('tester_agent_id',''))" 2>/dev/null || echo "")"
      _E2E_CH="$(echo "$_E2E_JSON" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('current_head',''))" 2>/dev/null || echo "")"
      if [ -n "$_E2E_TAID" ]; then
        cat >&2 <<E2EEOF

[builder-loop] PASS_CMD 全过（iter ${NEXT_ITER}）。检测到端到端验收用例。
tester_agent_id=${_E2E_TAID}

续接 tester 执行端到端验收：
1. SendMessage(to: "${_E2E_TAID}", summary: "rerun e2e")，传入失败用例
   - 如果 SendMessage 失败，fallback 到 Agent(subagent_type: "tester") 全量重跑
   - worktree_path: ${_E2E_WTP}
   - e2e_cases_path: ${_E2E_CP}
   - e2e_level: ${_E2E_LVL}
2. tester 报 E2E_SUMMARY: all_pass → 用 python3 写 e2e_verified_head 到 state 文件：
   STATE_FILE=${STATE_FILE}
   写入字段：e2e_verified_head: "${_E2E_CH}"
3. tester 报 E2E_SUMMARY: has_failure → 修改代码后用 python3 写 phase: "active" 到 state 触发 PASS_CMD 重跑：
   STATE_FILE=${STATE_FILE}

端到端验收用例：
${_E2E_CASES}
E2EEOF
      else
        cat >&2 <<E2EEOF

[builder-loop] PASS_CMD 全过（iter ${NEXT_ITER}）。检测到端到端验收用例。

请执行端到端验收：
1. spawn tester subagent（e2e 模式），传入：
   - e2e_cases（以下用例文本）
   - worktree_path: ${_E2E_WTP}
   - e2e_cases_path: ${_E2E_CP}
   - e2e_level: ${_E2E_LVL}
2. tester 报 E2E_SUMMARY: all_pass → 用 python3 写 e2e_verified_head 到 state 文件：
   STATE_FILE=${STATE_FILE}
   写入字段：e2e_verified_head: "${_E2E_CH}"
3. tester 报 E2E_SUMMARY: has_failure → 修改代码后用 python3 写 phase: "active" 到 state 触发 PASS_CMD 重跑：
   STATE_FILE=${STATE_FILE}

端到端验收用例：
${_E2E_CASES}
E2EEOF
      fi
      debug_log "e2e_phase" '{"phase":"e2e_pending"}'
      exit 2
      ;;
    reward_hack)
      _RH_FILE="$(echo "$HANDLE_PASS_OUT" | tail -1 | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('file',''))" 2>/dev/null || echo "")"
      _RH_KW="$(echo "$HANDLE_PASS_OUT" | tail -1 | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('keyword',''))" 2>/dev/null || echo "")"
      debug_log "reward_hack_guard" "file=$_RH_FILE keyword=$_RH_KW"
      cat >&2 <<RH_MSG
[builder-loop reward-hack-guard] PASS_CMD 配置改动疑似 reward hacking（file=$_RH_FILE, keyword=$_RH_KW）。
立即用 AskUserQuestion 列三选项让用户决策：
  1. quarantine 该测试（pytest.mark.skip / --ignore=... / xfail）+ 留 followup issue
  2. 修测试根因（race / sleep / 共享状态 改成同步原语）
  3. 保留 cmd 改动（需在回复中给出必要性理由）
禁止单方面继续完成 commit 流程。
RH_MSG
      exit 2
      ;;
    pass)
      debug_log "commit_only_result" "$(echo "$HANDLE_PASS_OUT" | tail -1)"
      write_trace "PASS"
      _PASS_JSON="$(echo "$HANDLE_PASS_OUT" | tail -1)"
      _SLUG="$(echo "$_PASS_JSON" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('slug',''))" 2>/dev/null || echo "")"
      _WTP="$(echo "$_PASS_JSON" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('worktree_path',''))" 2>/dev/null || echo "")"
      _RAID="$(echo "$_PASS_JSON" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('reviewer_agent_id',''))" 2>/dev/null || echo "")"
      _WT_LINE=""
      [ -n "$_WTP" ] && _WT_LINE="worktree_path=${_WTP}"
      _REV_LINE=""
      [ -n "$_RAID" ] && _REV_LINE="reviewer_agent_id=${_RAID}"
      echo "[builder-loop] PASS at iter ${NEXT_ITER} (commit, phase=passed_pending_review)" >&2
      cat >&2 <<PASS_MSG
[builder-loop] PASS_CMD 全部阶段通过（iter ${NEXT_ITER}）。
phase=passed_pending_review
slug=${_SLUG}
state_file=${STATE_FILE}
${_WT_LINE}
${_REV_LINE}
PASS_MSG
      debug_log "exit" "$(IT="$NEXT_ITER" python3 -c "
import os, json
print(json.dumps({'code':2,'reason':'pass_done_v3','iter': int(os.environ.get('IT','0') or 0), 'phase': 'passed_pending_review'}))
" 2>/dev/null || echo '{}')"
      exit 2
      ;;
    commit_error)
      _CE_JSON="$(echo "$HANDLE_PASS_OUT" | tail -1)"
      _CE_DETAIL="$(echo "$_CE_JSON" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('detail',''))" 2>/dev/null || echo "")"
      _CE_LOG="$(echo "$_CE_JSON" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('log_file',''))" 2>/dev/null || echo "")"
      echo "[builder-loop] loop-commit.sh 失败：${_CE_DETAIL}" >&2
      debug_log "exit" '{"code":2,"reason":"loop_commit_error"}'
      cat >&2 <<COMMIT_ERR
[builder-loop] commit 失败（iter ${NEXT_ITER}）
${_CE_DETAIL}
完整日志：${_CE_LOG}
请检查工作目录状态后重试。
COMMIT_ERR
      exit 2
      ;;
    *)
      echo "[builder-loop] handle-pass-result.sh 返回未知 type: ${HANDLE_PASS_TYPE} (ec=${HANDLE_PASS_EC})" >&2
      debug_log "exit" '{"code":2,"reason":"handle_pass_unknown"}'
      exit 2
      ;;
  esac
fi

# (Old inline PASS path removed in V5.4 — now handled by handle-pass-result.sh above)
# ---- 3a'. FATAL → 判据没跑起来，和「测试没过」是两回事 ----
# 不落到 3b：3b 按 `FAIL <stage> <log>` 分词，会把 `FATAL <reason>` 的 reason 当 stage
# 塞进日志路径提示，把 builder 引向「改代码」——而 FATAL 要改的是参数或 loop.yml 配置。
case "$LAST_LINE" in
  FATAL*)
    echo "[builder-loop] ⛔ iter ${NEXT_ITER}: PASS_CMD 未能执行 — ${LAST_LINE}" >&2
    debug_log "exit" '{"code":2,"reason":"pass_cmd_fatal"}'
    cat >&2 <<FATAL_MSG
[builder-loop] PASS_CMD 未能执行（iter ${NEXT_ITER}）——判据一个都没跑，不是测试失败。
${LAST_LINE}
run_cwd=${RUN_CWD}
main_repo=${PROJECT_ROOT}
不要改代码。核对 loop.yml 是否存在、pass_cmd 是否非空、yaml 是否合法后重试。
FATAL_MSG
    exit 2
    ;;
esac

# ---- 3b. FAIL → 处理反馈 ----
echo "[builder-loop] ❌ iter ${NEXT_ITER}: PASS_CMD 在 stage=$(echo "$LAST_LINE" | awk '{print $2}') 失败，分析中..." >&2
STAGE="$(echo "$LAST_LINE" | awk '{print $2}')"
LOG_PATH="$(echo "$LAST_LINE" | awk '{print $3}')"

# ---- V4.0: retry_transient 机械检测（FAIL 分支，原 judge Layer 简化） ----
# grep pass_cmd 错误日志中的瞬态关键词，命中则重跑（不喂回 builder）
if [ -f "$LOG_PATH" ]; then
  RETRY_HIT=""
  for rt_kw in 'API truncation' 'connection reset' 'ETIMEDOUT' 'socket hang up' 'ECONNRESET' 'ENOTFOUND'; do
    if grep -qi "$rt_kw" "$LOG_PATH" 2>/dev/null; then
      RETRY_HIT="$rt_kw"
      break
    fi
  done
  if [ -n "$RETRY_HIT" ]; then
    debug_log "retry_transient" "keyword=$RETRY_HIT"
    write_trace "RETRY_TRANSIENT" "$STAGE" "" "$RETRY_HIT"
    cat >&2 <<RETRY_MSG
[builder-loop | iter=${NEXT_ITER} | retry_transient]
原因：pass_cmd 日志含瞬态关键词「${RETRY_HIT}」，疑似 API 中断 / 网络抖动。
请重新执行同一任务，不要重做已经完成的部分。
RETRY_MSG
    debug_log "exit" '{"code":2,"reason":"retry_transient"}'
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

  # V1.8.1: 直接归档 + exit 2 注入让 builder 立即 AskUserQuestion
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
worktree=${WORKTREE_PATH_ES:-<bare>}
stash=${PRE_LOOP_STASH_REF_ES:0:8} (restored=${STASH_RESTORED})
请立即用 AskUserQuestion 询问用户下一步：继续手动调试 / 放弃 / 重新进 loop。
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
