#!/usr/bin/env bash
# tester-lock-write.sh — SubagentStart hook (matcher=tester)
#
# 在 tester subagent 启动时落锁，写入：
#   - session_id（CC 提供）
#   - source_dirs 绝对路径列表（从项目根 .claude/builder-loop.local.md 读）
#   - 时间戳 + TTL（兜底锁遗留）
#
# 锁文件路径：${ISOLATION_LOCK_DIR:-/tmp}/cc-subagent-{session_id}.lock
# 内容：YAML（tester-lock-check.sh 用 grep 解析）
#
# 行为：
#   - 解析 stdin JSON 拿 session_id / cwd
#   - 找最近 builder-loop.local.md（向上查 5 级）
#   - 读 source_dirs（逗号分隔）+ project_root（state file 同级 .claude/.. 即项目根）
#   - 全部转 abspath 写入锁
#
# 退出码：始终 0（hook 失败不应阻断 subagent 启动）
# 调试日志：~/.claude/logs/tester-lock-write.log（可选）

set -uo pipefail

LOCK_DIR="${ISOLATION_LOCK_DIR:-/tmp}"
LOG_FILE="${HOME}/.claude/logs/tester-lock-write.log"
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true

log() { printf '[%s] %s\n' "$(date -Iseconds)" "$*" >> "$LOG_FILE" 2>/dev/null || true; }

# 读 stdin JSON
INPUT="$(cat || echo '{}')"
SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || echo "")
CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null || echo "")

if [ -z "$SESSION_ID" ]; then
  log "no session_id in stdin, skip"
  exit 0
fi

# cwd fallback
if [ -z "$CWD" ] || [ ! -d "$CWD" ]; then
  CWD="$(pwd)"
fi

# 向上找含 .claude/builder-loop.local.md 的目录（最多 5 级）
find_state_file() {
  local dir="$1"
  for _ in 1 2 3 4 5; do
    if [ -f "${dir}/.claude/builder-loop.local.md" ]; then
      echo "${dir}/.claude/builder-loop.local.md"
      return 0
    fi
    [ "$dir" = "/" ] && return 1
    dir="$(dirname "$dir")"
  done
  return 1
}

STATE_FILE="$(find_state_file "$CWD" || echo "")"
if [ -z "$STATE_FILE" ]; then
  log "no state file found from cwd=$CWD, skip"
  exit 0
fi

PROJECT_ROOT="$(dirname "$(dirname "$STATE_FILE")")"

# 解析 source_dirs（state file 中：source_dirs: "src,lib"）
SRC_RAW=$(grep -E '^source_dirs:' "$STATE_FILE" | sed -E 's/^source_dirs:[[:space:]]*"?([^"]*)"?$/\1/' || echo "")

# 转 abspath，每个一行
SRC_ABS=""
if [ -n "$SRC_RAW" ]; then
  IFS=',' read -ra DIRS <<< "$SRC_RAW"
  for d in "${DIRS[@]}"; do
    [ -z "$d" ] && continue
    abs="${PROJECT_ROOT}/${d}"
    SRC_ABS="${SRC_ABS}  - \"${abs}\"
"
  done
fi

# TTL 从 loop.yml 读（可选），默认 30 分钟
LOOP_YML="${PROJECT_ROOT}/.claude/loop.yml"
TTL_MIN=30
if [ -f "$LOOP_YML" ] && command -v python3 >/dev/null 2>&1; then
  TTL_FROM_YML=$(python3 -c "
import yaml, sys
try:
  d = yaml.safe_load(open('$LOOP_YML')) or {}
  print(d.get('isolation', {}).get('tester_lock_ttl_min', 30))
except Exception:
  print(30)
" 2>/dev/null || echo 30)
  if [ -n "$TTL_FROM_YML" ]; then
    TTL_MIN="$TTL_FROM_YML"
  fi
else
  log "loop.yml or python3 unavailable, TTL fallback=30min"
fi

LOCK_FILE="${LOCK_DIR}/cc-subagent-${SESSION_ID}.lock"

# 写锁（YAML，tester-lock-check.sh 用 grep 解析）
{
  echo "agent_type: tester"
  echo "session_id: ${SESSION_ID}"
  echo "project_root: \"${PROJECT_ROOT}\""
  echo "start_ts: $(date +%s)"
  echo "ttl_min: ${TTL_MIN}"
  echo "source_dirs_abs:"
  if [ -n "$SRC_ABS" ]; then
    printf '%s' "$SRC_ABS"
  else
    echo "  []"
  fi
} > "$LOCK_FILE"

log "lock written: $LOCK_FILE | source_dirs=$SRC_RAW"
exit 0
