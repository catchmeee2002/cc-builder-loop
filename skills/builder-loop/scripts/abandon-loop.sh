#!/usr/bin/env bash
# abandon-loop.sh — V2.6 用户主动放弃本次 loop 的合法出口
#
# 用法：bash abandon-loop.sh <state_file> <reason>
#
# 参数：
#   $1 = state file 绝对路径（必填）
#   $2 = reason 自由文本（必填，建议 < 80 字符；写入 legacy info 用于审计）
#
# 退出码：
#   0 = 成功 abandon（state 已归档、stash 已还原、worktree 保留）
#   2 = 参数错误 / state 不存在 / reason 为空 / state 已 inactive
#
# 副作用：
#   - kill state.baseline_probe_pid（V2.7 + Phase 2 才会有，缺字段视为 0 跳过）
#   - git worktree remove --force state.baseline_probe_worktree（缺字段跳过）
#   - 还原主仓 stash（V2.3 路径，仅 worktree_mode=dirty）
#   - 写 trace event "ABANDON"
#   - archive_to_legacy state → legacy/<ts>-abandon_<reason>.bak
#   - 写 legacy/<ts>-abandon_<reason>.info（worktree path / stash hash / changed_files / reason）
#   - **不动** state.worktree_path 目录（保留 worktree + branch 让用户手动 cherry-pick）
#
# 设计：
#   - 本脚本与 EARLY_STOP 路径（builder-loop-stop.sh L840+）几乎对称——区别仅在
#     "归档原因前缀"（abandon vs early_stop）+ "是否提示 builder AskUserQuestion"
#     （abandon 是用户主动决策，已经走过 AskUserQuestion，不再注入）
#   - hook 自动放行：state 归档后下次 stop hook 找不到 active state → 走 bootstrap 路径
#     reviewer-timing-check.sh 也找不到 active state → 自动放行（不需单独的 user-override）
#
# 与现有机制的关系：
#   - 复用 V1.8.1 archive_to_legacy 的"mv 到 legacy/"语义
#   - 复用 V2.3 dirty stash 还原逻辑（stash apply 不 pop，副本保留待用户决定）
#   - 复用 V1.5 NDJSON trace（写 ABANDON event）

set -euo pipefail

# ---- 参数校验 ----
if [ "$#" -lt 2 ]; then
  cat >&2 <<USAGE
用法：bash abandon-loop.sh <state_file> <reason>

  state_file = 状态文件绝对路径，例如：
    <project_root>/.claude/builder-loop/state/<slug>.yml
  reason     = abandon 原因（必填，写入 legacy info 用于审计）
               建议 < 80 字符，例如：
                 "baseline broken not my fault"
                 "waiting upstream PR merge to master"

退出码：0=成功 / 2=参数错误或前置失败
USAGE
  exit 2
fi

STATE="$1"
REASON="$2"

# 空 reason / 仅空白 → 拒绝（防 builder 调用时把 "" 漏过去）
REASON_STRIPPED="$(printf '%s' "$REASON" | tr -d '[:space:]')"
if [ -z "$REASON_STRIPPED" ]; then
  echo "[abandon-loop] ❌ reason 必填且不能为纯空白。请传入有意义的 abandon 原因（写入 legacy/.info 审计）。" >&2
  exit 2
fi

if [ ! -f "$STATE" ]; then
  echo "[abandon-loop] ❌ state file 不存在：$STATE" >&2
  echo "                可能 loop 已 PASS 或已被 abandon；无需操作。" >&2
  exit 2
fi

# ---- 读 state 字段（容错：缺字段视为空，不杀脚本）----
read_field() {
  # 与 merge-worktree-back.sh 一致语义；末尾 || true 防 grep 未命中触发 set -e
  grep -E "^${1}:" "$STATE" 2>/dev/null | head -1 \
    | sed -E "s/^${1}:[[:space:]]*\"?([^\"]*)\"?[[:space:]]*\$/\1/" || true
}

ACTIVE="$(read_field active)"
SLUG="$(read_field slug)"
ITER="$(read_field iter)"
PROJECT_ROOT="$(read_field main_repo_path)"
[ -z "$PROJECT_ROOT" ] && PROJECT_ROOT="$(read_field project_root)"  # V1.x 老 state 兼容
WORKTREE_PATH="$(read_field worktree_path)"
WORKTREE_MODE="$(read_field worktree_mode)"
PRE_LOOP_STASH_REF="$(read_field pre_loop_stash_ref)"
# V2.6 Phase 2 字段（本期 abandon-loop.sh 已支持，缺字段当作空）
BASELINE_PROBE_PID="$(read_field baseline_probe_pid)"
BASELINE_PROBE_WORKTREE="$(read_field baseline_probe_worktree)"
BASELINE_PROBE_STATUS="$(read_field baseline_probe_status)"

# active 必须为 true 才允许 abandon（防 zombie 重复归档）
if [ "$ACTIVE" != "true" ]; then
  echo "[abandon-loop] ❌ state.active=${ACTIVE:-<空>}，loop 非活跃，不重复归档。" >&2
  echo "                state 文件：$STATE" >&2
  echo "                如已 PASS / 早停 / 之前 abandon 过，无需操作。" >&2
  exit 2
fi

# 主仓路径无效 → 拒绝（防写错位置）
if [ -z "$PROJECT_ROOT" ] || [ ! -d "$PROJECT_ROOT" ]; then
  echo "[abandon-loop] ❌ main_repo_path/project_root 字段缺失或目录不存在：${PROJECT_ROOT}" >&2
  echo "                state 文件可能损坏：$STATE" >&2
  exit 2
fi

echo "[abandon-loop] ⛔ 开始 abandon (slug=${SLUG:-<unknown>}, iter=${ITER:-?}, reason=${REASON})"

# ---- Step 1: V2.6 Phase 2 — kill baseline probe 进程 + 清临时 worktree（缺字段静默） ----
# 当前 Phase 1 这两个字段都是空，下方 if 不执行；Phase 2 后才生效
if [ -n "$BASELINE_PROBE_PID" ] && [ "$BASELINE_PROBE_PID" != "0" ]; then
  if kill -0 "$BASELINE_PROBE_PID" 2>/dev/null; then
    kill "$BASELINE_PROBE_PID" 2>/dev/null || true
    sleep 1
    kill -0 "$BASELINE_PROBE_PID" 2>/dev/null && kill -9 "$BASELINE_PROBE_PID" 2>/dev/null || true
    echo "[abandon-loop] 🛑 已停止 baseline probe 进程 (pid=${BASELINE_PROBE_PID})" >&2
  fi
fi

if [ -n "$BASELINE_PROBE_WORKTREE" ] && [ -d "$BASELINE_PROBE_WORKTREE" ]; then
  git -C "$PROJECT_ROOT" worktree remove --force "$BASELINE_PROBE_WORKTREE" 2>/dev/null || \
    rm -rf "$BASELINE_PROBE_WORKTREE" 2>/dev/null || true
  echo "[abandon-loop] 🧹 已清理 baseline probe 临时 worktree" >&2
fi

# ---- Step 2: V2.3 dirty 模式 → 还原主仓 stash（apply 不 pop，副本保留供事后查阅） ----
STASH_RESTORED="no"
if [ "$WORKTREE_MODE" = "dirty" ] && [ -n "$PRE_LOOP_STASH_REF" ]; then
  if git -C "$PROJECT_ROOT" stash apply "$PRE_LOOP_STASH_REF" 2>/dev/null; then
    STASH_RESTORED="yes"
    echo "[abandon-loop] 📦 主仓 stash 已 apply 还原 (hash=${PRE_LOOP_STASH_REF:0:8})" >&2
  else
    STASH_RESTORED="conflict"
    echo "[abandon-loop] ⚠️  stash apply 失败（可能主仓被中途修改），副本仍保留：${PRE_LOOP_STASH_REF:0:8}" >&2
    echo "                手动还原：git stash apply ${PRE_LOOP_STASH_REF:0:8}" >&2
  fi
fi

# ---- Step 3: 写 trace event "ABANDON"（与 PASS / FAIL / EARLY_STOP 同一份 jsonl） ----
TRACE_FILE="${PROJECT_ROOT}/.claude/loop-trace.jsonl"
mkdir -p "$(dirname "$TRACE_FILE")" 2>/dev/null || true
TS="$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)"
SLUG_J="$SLUG" ITER_J="${ITER:-0}" REASON_J="$REASON" TS_J="$TS" \
  python3 -c "
import os, json
print(json.dumps({
    'ts': os.environ.get('TS_J',''),
    'event': 'ABANDON',
    'slug': os.environ.get('SLUG_J',''),
    'iter': int(os.environ.get('ITER_J','0') or 0),
    'reason': os.environ.get('REASON_J','')[:200],
}))
" >> "$TRACE_FILE" 2>/dev/null || true

# ---- Step 4: archive_to_legacy state → legacy/<ts>-abandon_<reason>.bak ----
LEGACY_DIR="${PROJECT_ROOT}/.claude/builder-loop/legacy"
mkdir -p "$LEGACY_DIR" 2>/dev/null || true
TS_FN="$(date +%Y%m%d-%H%M%S)"
# reason → 文件名安全字符（与 stop hook archive_to_legacy 同规则）
REASON_SAFE="$(printf '%s' "$REASON" | tr -c 'a-zA-Z0-9_' '_' | head -c 80)"
LEGACY_BAK="${LEGACY_DIR}/${TS_FN}-abandon_${REASON_SAFE}.bak"
LEGACY_INFO="${LEGACY_DIR}/${TS_FN}-abandon_${REASON_SAFE}.info"

# 先写 info（独立文件），再 mv state（防 mv 后再读丢字段）
{
  echo "reason: abandon_${REASON_SAFE}"
  echo "reason_raw: ${REASON}"
  echo "slug: ${SLUG}"
  echo "iter: ${ITER:-0}"
  echo "worktree_path: ${WORKTREE_PATH}"
  echo "worktree_mode: ${WORKTREE_MODE}"
  echo "pre_loop_stash_ref: ${PRE_LOOP_STASH_REF}"
  echo "stash_restored: ${STASH_RESTORED}"
  echo "baseline_probe_status: ${BASELINE_PROBE_STATUS}"
  echo "ts: $(date -Iseconds 2>/dev/null || date '+%Y-%m-%dT%H:%M:%S')"
} > "$LEGACY_INFO" 2>/dev/null || true

if ! mv "$STATE" "$LEGACY_BAK" 2>/dev/null; then
  echo "[abandon-loop] ❌ state 归档失败：$STATE → $LEGACY_BAK" >&2
  echo "                可能权限问题；手动 mv 即可。" >&2
  exit 2
fi

# ---- Step 5: stdout 输出供 builder 解析 ----
echo "[abandon-loop] ✅ Loop abandoned"
echo "  slug:          ${SLUG}"
echo "  reason:        ${REASON}"
echo "  state archived to: ${LEGACY_BAK}"
echo "  legacy info:   ${LEGACY_INFO}"
if [ -n "$WORKTREE_PATH" ] && [ -d "$WORKTREE_PATH" ]; then
  BRANCH="$(git -C "$WORKTREE_PATH" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "<unknown>")"
  echo "  worktree kept: ${WORKTREE_PATH}"
  echo "  branch kept:   ${BRANCH}"
  echo ""
  echo "下一步建议（任选）："
  echo "  - 等外部修复后 cherry-pick 本期改动:"
  echo "      git -C ${PROJECT_ROOT} cherry-pick <commits-on-${BRANCH}>"
  echo "  - 直接丢弃本期改动:"
  echo "      git -C ${PROJECT_ROOT} worktree remove --force ${WORKTREE_PATH}"
  echo "      git -C ${PROJECT_ROOT} branch -D ${BRANCH}"
  echo "  - 重新进 loop（外部条件就绪后）:"
  echo "      bash ~/.claude/skills/builder-loop/scripts/setup-builder-loop.sh \"<task>\""
else
  echo "  worktree:      <bare 模式 / 已不存在>"
fi

if [ "$STASH_RESTORED" = "yes" ]; then
  echo ""
  echo "  主仓 dirty 已从 stash 还原（副本仍在 stash list，事后清理：git stash drop ${PRE_LOOP_STASH_REF:0:8}）"
elif [ "$STASH_RESTORED" = "conflict" ]; then
  echo ""
  echo "  ⚠️  主仓 stash apply 撞冲突，未还原。手动处理：git stash apply ${PRE_LOOP_STASH_REF:0:8}"
fi

exit 0
