#!/usr/bin/env bash
# test-tester-write-guard.sh — V2.2 E2E：tester subagent 跨目录写防御
#
# 覆盖 11 个 case（A 段路径白名单 + bare loop / 老锁兼容 + MultiEdit 拦截实证）：
#   A1  lock 含 worktree_path=/wt + Write file_path=主仓 → exit 2 + stderr 含 worktree/main
#   A2  lock 含 worktree_path=/wt + Edit  file_path=/wt/sub/foo.py → exit 0
#   A3  lock 含 worktree_path=/wt + MultiEdit file_path=/wt/foo.py → exit 0（放行场景）
#   A4  无 lock → exit 0（非 tester subagent 放行）
#   A5  lock worktree_path 字段为空字符串 → exit 0（bare loop 兼容）
#   A5b lock **完全不含** worktree_path 行（V1.x 老锁文件格式）→ exit 0
#   A6  lock 含 worktree_path=/wt + Write file_path=/wt（恰好等于 worktree 根，无文件名）→ exit 2
#   A7  lock 含 worktree_path=/wt + Write file_path=/wt2/foo.py（前缀部分匹配）→ exit 2
#   A8  lock 含 worktree_path=/wt + Write file_path=/wt/../main/foo.py（path traversal）→ exit 2
#   A9  lock 含 agent_type=reviewer（非 tester）+ Write file_path=主仓 → exit 0
#   A10 lock 含 worktree_path=/wt + MultiEdit file_path=主仓 → exit 2（MultiEdit 拦截实证）
#
# 用法：bash test-tester-write-guard.sh
# 退出码：0=全部通过 / 1=有失败

set -euo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$THIS_DIR/../../../.." && pwd)"
GUARD_SCRIPT="${REPO_ROOT}/scripts/tester-write-guard.sh"

PASS=0
FAIL=0

assert_exit() {
  local desc="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    echo "  ✅ $desc (exit=$actual)"
    PASS=$(( PASS + 1 ))
  else
    echo "  ❌ $desc (expected exit=$expected, got=$actual)"
    FAIL=$(( FAIL + 1 ))
  fi
}

assert_stderr_contains() {
  local desc="$1" needle="$2" haystack="$3"
  if printf '%s' "$haystack" | grep -q -- "$needle"; then
    echo "  ✅ $desc (stderr contains '$needle')"
    PASS=$(( PASS + 1 ))
  else
    echo "  ❌ $desc (stderr missing '$needle')"
    echo "     stderr: $(printf '%s' "$haystack" | head -3)"
    FAIL=$(( FAIL + 1 ))
  fi
}

echo "=== V2.2 E2E: tester-write-guard.sh ==="
[ -f "$GUARD_SCRIPT" ] || { echo "❌ guard script 不存在: $GUARD_SCRIPT"; exit 1; }
echo "  ✅ guard script 存在"
PASS=$(( PASS + 1 ))

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

LOCK_DIR="$TMP/lock"
mkdir -p "$LOCK_DIR"

# 模拟 worktree 与主仓目录（hook 会 readlink -f，必须真实存在）
WT="$TMP/wt"
WT2="$TMP/wt2"
MAIN="$TMP/main"
mkdir -p "$WT/sub" "$WT2" "$MAIN"

# helper：构造锁文件
write_lock() {
  local sid="$1" agent_type="$2" worktree_path="$3"
  local lock="$LOCK_DIR/cc-subagent-${sid}.lock"
  {
    echo "agent_type: ${agent_type}"
    echo "session_id: ${sid}"
    echo "project_root: \"${worktree_path:-$MAIN}\""
    echo "main_repo_path: \"${MAIN}\""
    echo "worktree_path: \"${worktree_path}\""
    echo "slug: \"test-slug\""
    echo "start_ts: $(date +%s)"
    echo "ttl_min: 30"
    echo "source_dirs_abs:"
    echo "  []"
  } > "$lock"
  echo "$lock"
}

# helper：构造 PreToolUse 输入 JSON
make_input() {
  local sid="$1" tool_name="$2" file_path="$3"
  printf '{"session_id":"%s","tool_name":"%s","tool_input":{"file_path":"%s"}}' \
    "$sid" "$tool_name" "$file_path"
}

# helper：跑 hook 拿 stderr + exit code
run_guard() {
  local input="$1"
  local stderr_file="$TMP/stderr.$$"
  set +e
  # env var 必须在管道下游 bash 进程上设置（不能在上游 printf）
  printf '%s' "$input" \
    | ISOLATION_LOCK_DIR="$LOCK_DIR" bash "$GUARD_SCRIPT" 2>"$stderr_file"
  local rc=$?
  set -e
  GUARD_RC="$rc"
  GUARD_STDERR="$(cat "$stderr_file")"
  rm -f "$stderr_file"
}

# ---- A1: tester + worktree_path 非空 + 写主仓 → exit 2 ----
echo ""
echo "[A1] tester + worktree_path 非空 + 写主仓 → 拒绝"
write_lock "sid-a1" "tester" "$WT" >/dev/null
run_guard "$(make_input "sid-a1" "Write" "$MAIN/tests/foo.py")"
assert_exit "A1 exit code" "2" "$GUARD_RC"
assert_stderr_contains "A1 stderr 含禁止字样" "tester 跨目录写禁止" "$GUARD_STDERR"
assert_stderr_contains "A1 stderr 含 worktree_path" "$WT" "$GUARD_STDERR"
assert_stderr_contains "A1 stderr 含 main_repo_path" "$MAIN" "$GUARD_STDERR"

# ---- A2: tester + 写 worktree 内 → exit 0 ----
echo ""
echo "[A2] tester + 写 worktree 内 → 放行"
run_guard "$(make_input "sid-a1" "Edit" "$WT/sub/foo.py")"
assert_exit "A2 exit code" "0" "$GUARD_RC"

# ---- A3: tester + MultiEdit worktree 内 → exit 0 ----
echo ""
echo "[A3] tester + MultiEdit worktree 内 → 放行"
run_guard "$(make_input "sid-a1" "MultiEdit" "$WT/foo.py")"
assert_exit "A3 exit code" "0" "$GUARD_RC"

# ---- A4: 无 lock → exit 0 ----
echo ""
echo "[A4] 无 lock → 放行（非 tester subagent）"
run_guard "$(make_input "sid-no-lock" "Write" "$MAIN/foo.py")"
assert_exit "A4 exit code" "0" "$GUARD_RC"

# ---- A5: lock 无 worktree_path 字段 → exit 0（bare loop / V1.x 老锁）----
echo ""
echo "[A5] lock 无 worktree_path 字段 → 放行（bare loop 兼容）"
write_lock "sid-a5" "tester" "" >/dev/null  # worktree_path 写空字符串
run_guard "$(make_input "sid-a5" "Write" "$MAIN/foo.py")"
assert_exit "A5 exit code" "0" "$GUARD_RC"

# ---- A6: file_path 等于 worktree 根（无尾斜杠）→ exit 2 ----
echo ""
echo "[A6] file_path 恰好等于 worktree 根 → 拒绝（无文件名）"
write_lock "sid-a6" "tester" "$WT" >/dev/null
run_guard "$(make_input "sid-a6" "Write" "$WT")"
assert_exit "A6 exit code" "2" "$GUARD_RC"

# ---- A7: 前缀部分匹配（/wt 与 /wt2）→ exit 2 ----
echo ""
echo "[A7] 前缀部分匹配 → 拒绝（必须含尾斜杠才匹配）"
run_guard "$(make_input "sid-a6" "Write" "$WT2/foo.py")"
assert_exit "A7 exit code" "2" "$GUARD_RC"

# ---- A8: path traversal → exit 2 ----
echo ""
echo "[A8] path traversal /wt/../main/foo.py → 拒绝（realpath 解析后越界）"
# 准备：/wt/../main = /tmp/xxx/main，是 main 目录，不在 wt 内
mkdir -p "$MAIN/sub"
run_guard "$(make_input "sid-a6" "Write" "$WT/../main/sub/foo.py")"
assert_exit "A8 exit code" "2" "$GUARD_RC"

# ---- A9: agent_type=reviewer（非 tester）→ exit 0 ----
echo ""
echo "[A9] agent_type=reviewer → 放行（仅拦 tester）"
write_lock "sid-a9" "reviewer" "$WT" >/dev/null
run_guard "$(make_input "sid-a9" "Write" "$MAIN/foo.py")"
assert_exit "A9 exit code" "0" "$GUARD_RC"

# ---- A5b: lock 完全不含 worktree_path 行（V1.x 老锁文件格式）→ exit 0 ----
echo ""
echo "[A5b] lock 完全不含 worktree_path 行（V1.x 老锁）→ 放行"
LOCK_A5B="$LOCK_DIR/cc-subagent-sid-a5b.lock"
{
  echo "agent_type: tester"
  echo "session_id: sid-a5b"
  echo "project_root: \"$WT\""
  echo "start_ts: $(date +%s)"
  echo "ttl_min: 30"
  echo "source_dirs_abs:"
  echo "  []"
} > "$LOCK_A5B"
run_guard "$(make_input "sid-a5b" "Write" "$MAIN/foo.py")"
assert_exit "A5b exit code（V1.x 老锁兼容）" "0" "$GUARD_RC"

# ---- A10: tester + MultiEdit 跨界写主仓 → exit 2（MultiEdit 拦截实证）----
echo ""
echo "[A10] tester + MultiEdit file_path=主仓 → 拒绝（验证 MultiEdit 真能拦）"
write_lock "sid-a10" "tester" "$WT" >/dev/null
# 完整 MultiEdit 输入：含顶层 file_path + edits 数组（MultiEdit 真实结构）
INPUT_A10="$(printf '{"session_id":"sid-a10","tool_name":"MultiEdit","tool_input":{"file_path":"%s/tests/foo.py","edits":[{"old_string":"a","new_string":"b"}]}}' "$MAIN")"
run_guard "$INPUT_A10"
assert_exit "A10 exit code（MultiEdit 拦截）" "2" "$GUARD_RC"
assert_stderr_contains "A10 stderr 含禁止字样" "tester 跨目录写禁止" "$GUARD_STDERR"

echo ""
echo "============================================="
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
