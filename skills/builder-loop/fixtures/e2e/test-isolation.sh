#!/usr/bin/env bash
# test-isolation.sh — 验证 tester-lock-check.sh 正确拦截 source_dirs 读操作
#
# 场景：
#   1. Read src/ 路径 + 锁存在 → exit 2（被拦）
#   2. Read tests/ 路径 + 锁存在 → exit 0（放行）
#   3. Read *.md + 锁存在 → exit 0（白名单放行）
#   4. 无锁文件 → exit 0（放行）

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "tester-isolation"

HOOK_SCRIPT="${HOME}/.claude/scripts/tester-lock-check.sh"
if [ ! -f "$HOOK_SCRIPT" ]; then
  echo "SKIP: tester-lock-check.sh not found (run install.sh first)" >&2
  exit 0
fi

# ---- 搭建 mock 目录 ----
TMPDIR="$(mktemp -d -t builder-loop-iso-XXXXXX)"
_HARNESS_TMPDIRS+=("$TMPDIR")
LOCK_DIR="$TMPDIR/locks"
mkdir -p "$LOCK_DIR" "$TMPDIR/project/src" "$TMPDIR/project/tests"
echo "source code" > "$TMPDIR/project/src/foo.py"
echo "test code" > "$TMPDIR/project/tests/test_foo.py"
echo "# README" > "$TMPDIR/project/README.md"

SESSION_ID="test-iso-session"
LOCK_FILE="${LOCK_DIR}/cc-subagent-${SESSION_ID}.lock"

write_lock() {
  cat > "$LOCK_FILE" <<LOCKEOF
agent_type: tester
session_id: ${SESSION_ID}
start_ts: $(date +%s)
ttl_min: 30
source_dirs_abs:
  - "${TMPDIR}/project/src"
LOCKEOF
}

run_iso_hook() {
  local tool_name="$1" file_path="$2" ec=0
  local input="{\"session_id\":\"${SESSION_ID}\",\"tool_name\":\"${tool_name}\",\"tool_input\":{\"file_path\":\"${file_path}\"}}"
  printf '%s' "$input" | ISOLATION_LOCK_DIR="$LOCK_DIR" bash "$HOOK_SCRIPT" > /dev/null 2>&1 || ec=$?
  echo "$ec"
}

section "Case 1: Read src/foo.py + 锁 → exit 2"
write_lock
EC=$(run_iso_hook Read "${TMPDIR}/project/src/foo.py")
assert "Read src/ 被拦截 (exit 2)" "[ '$EC' -eq 2 ]"

section "Case 2: Read tests/ + 锁 → exit 0"
EC=$(run_iso_hook Read "${TMPDIR}/project/tests/test_foo.py")
assert "Read tests/ 放行 (exit 0)" "[ '$EC' -eq 0 ]"

section "Case 3: Read *.md + 锁 → exit 0"
EC=$(run_iso_hook Read "${TMPDIR}/project/README.md")
assert "Read *.md 白名单放行 (exit 0)" "[ '$EC' -eq 0 ]"

section "Case 4: 无锁 → exit 0"
rm -f "$LOCK_FILE"
EC=$(run_iso_hook Read "${TMPDIR}/project/src/foo.py")
assert "无锁文件放行 (exit 0)" "[ '$EC' -eq 0 ]"

harness_report
