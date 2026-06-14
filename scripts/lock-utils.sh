#!/usr/bin/env bash
# lock-utils.sh — subagent lock 公共函数库
#
# V3.5: 所有 hook 脚本 source 本文件，统一 lock 文件的路径规则 / 读写 / 查找 / 清理。
# 锁文件格式：cc-subagent-{session_id}-{agent_type}.lock（V3.5+）
# 旧格式兼容：cc-subagent-{session_id}.lock（V3.4-）
#
# 用法：
#   SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
#   source "${SCRIPT_DIR}/lock-utils.sh"

# ---- 常量 ----

BL_AGENT_WHITELIST="tester doc-maintainer arbiter reviewer"
BL_SYNC_AGENTS="tester doc-maintainer arbiter"

_BL_LOCK_DIR="${ISOLATION_LOCK_DIR:-/tmp}"

# ---- 公共函数 ----

bl_is_managed_agent() {
  local agent_type="${1:-}"
  [ -z "$agent_type" ] && return 1
  local a
  for a in $BL_AGENT_WHITELIST; do
    [ "$a" = "$agent_type" ] && return 0
  done
  return 1
}

bl_is_sync_agent() {
  local agent_type="${1:-}"
  [ -z "$agent_type" ] && return 1
  local a
  for a in $BL_SYNC_AGENTS; do
    [ "$a" = "$agent_type" ] && return 0
  done
  return 1
}

bl_lock_path() {
  local session_id="${1:?bl_lock_path: session_id required}"
  local agent_type="${2:?bl_lock_path: agent_type required}"
  printf '%s/cc-subagent-%s-%s.lock' "$_BL_LOCK_DIR" "$session_id" "$agent_type"
}

bl_legacy_lock_path() {
  local session_id="${1:?bl_legacy_lock_path: session_id required}"
  printf '%s/cc-subagent-%s.lock' "$_BL_LOCK_DIR" "$session_id"
}

bl_find_active_locks() {
  local session_id="${1:?bl_find_active_locks: session_id required}"
  local new_pattern="${_BL_LOCK_DIR}/cc-subagent-${session_id}-*.lock"
  local legacy="${_BL_LOCK_DIR}/cc-subagent-${session_id}.lock"
  local found=()

  local f
  for f in $new_pattern; do
    [ -f "$f" ] && found+=("$f")
  done

  [ -f "$legacy" ] && found+=("$legacy")

  if [ ${#found[@]} -gt 0 ]; then
    printf '%s\n' "${found[@]}"
  fi
}

bl_read_lock_field() {
  local lock_file="${1:?bl_read_lock_field: lock_file required}"
  local field="${2:?bl_read_lock_field: field required}"
  grep -E "^${field}:" "$lock_file" 2>/dev/null | head -1 | sed -E "s/^${field}:[[:space:]]*\"?([^\"]*)\"?[[:space:]]*$/\1/" || true
}

bl_cleanup_stale_locks() {
  local session_id="${1:?bl_cleanup_stale_locks: session_id required}"
  local ttl_sec="${2:-1800}"
  local now
  now="$(date +%s)"

  local f start_ts age
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    start_ts="$(bl_read_lock_field "$f" "start_ts")"
    [ -z "$start_ts" ] && continue
    age=$(( now - start_ts ))
    if [ "$age" -gt "$ttl_sec" ]; then
      rm -f "$f"
    fi
  done < <(bl_find_active_locks "$session_id")
}
