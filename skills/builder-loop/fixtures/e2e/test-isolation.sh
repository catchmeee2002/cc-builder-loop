#!/usr/bin/env bash
# test-isolation.sh — 验证 tester-lock-check.sh 正确拦截 source_dirs 读操作
#
# 场景：
#   1. 创建 mock 锁文件（模拟 tester subagent 活跃）
#   2. Read src/ 路径 → 期望 exit 2（被拦）
#   3. Read tests/ 路径 → 期望 exit 0（放行）
#   4. Read *.md → 期望 exit 0（白名单放行）
#   5. 无锁文件时 → 期望 exit 0（放行）
#
# 用法：bash test-isolation.sh

set -euo pipefail

HOOK_SCRIPT="${HOME}/.claude/scripts/tester-lock-check.sh"
if [ ! -f "$HOOK_SCRIPT" ]; then
  echo "⚠️  SKIP: tester-lock-check.sh not found at $HOOK_SCRIPT" >&2
  echo "   请先跑 install.sh 或手动 ln -sf 补软链" >&2
  exit 0
fi

TMPDIR="$(mktemp -d -t builder-loop-iso-XXXXXX)"
LOCK_DIR="$TMPDIR/locks"
mkdir -p "$LOCK_DIR" "$TMPDIR/project/src" "$TMPDIR/project/tests"
echo "source code" > "$TMPDIR/project/src/foo.py"
echo "test code" > "$TMPDIR/project/tests/test_foo.py"
echo "# README" > "$TMPDIR/project/README.md"

SESSION_ID="test-iso-session"
LOCK_FILE="${LOCK_DIR}/cc-subagent-${SESSION_ID}.lock"

# shellcheck disable=SC2317
cleanup() { rm -rf "$TMPDIR"; }
trap cleanup EXIT

# 写 mock 锁文件
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

run_hook() {
  local tool_name="$1" file_path="$2"
  local input="{\"session_id\":\"${SESSION_ID}\",\"tool_name\":\"${tool_name}\",\"tool_input\":{\"file_path\":\"${file_path}\"}}"
  local ec=0
  printf '%s' "$input" | ISOLATION_LOCK_DIR="$LOCK_DIR" bash "$HOOK_SCRIPT" > /dev/null 2>&1 || ec=$?
  echo "$ec"
}

PASSED=0
TOTAL=0

# case 1: Read src/foo.py → 期望 exit 2（拦截）
write_lock
TOTAL=$((TOTAL + 1))
EC=$(run_hook Read "${TMPDIR}/project/src/foo.py")
if [ "$EC" -eq 2 ]; then
  echo "✓ case 1: Read src/foo.py → exit 2（拦截）"
  PASSED=$((PASSED + 1))
else
  echo "✗ case 1: Read src/foo.py 期望 exit 2，实际 exit $EC"
fi

# case 2: Read tests/test_foo.py → 期望 exit 0（放行）
TOTAL=$((TOTAL + 1))
EC=$(run_hook Read "${TMPDIR}/project/tests/test_foo.py")
if [ "$EC" -eq 0 ]; then
  echo "✓ case 2: Read tests/test_foo.py → exit 0（放行）"
  PASSED=$((PASSED + 1))
else
  echo "✗ case 2: Read tests/test_foo.py 期望 exit 0，实际 exit $EC"
fi

# case 3: Read README.md → 期望 exit 0（白名单 *.md）
TOTAL=$((TOTAL + 1))
EC=$(run_hook Read "${TMPDIR}/project/README.md")
if [ "$EC" -eq 0 ]; then
  echo "✓ case 3: Read README.md → exit 0（白名单 *.md）"
  PASSED=$((PASSED + 1))
else
  echo "✗ case 3: Read README.md 期望 exit 0，实际 exit $EC"
fi

# case 4: 无锁文件时放行
rm -f "$LOCK_FILE"
TOTAL=$((TOTAL + 1))
EC=$(run_hook Read "${TMPDIR}/project/src/foo.py")
if [ "$EC" -eq 0 ]; then
  echo "✓ case 4: 无锁文件 Read src/foo.py → exit 0（放行）"
  PASSED=$((PASSED + 1))
else
  echo "✗ case 4: 无锁文件期望 exit 0，实际 exit $EC"
fi

echo ""
if [ "$PASSED" -eq "$TOTAL" ]; then
  echo "✅ PASS: tester 隔离测试 ${PASSED}/${TOTAL} 全部通过"
  exit 0
else
  echo "❌ FAIL: tester 隔离测试 ${PASSED}/${TOTAL} 通过"
  exit 1
fi
