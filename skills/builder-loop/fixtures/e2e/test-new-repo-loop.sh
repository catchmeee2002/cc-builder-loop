#!/usr/bin/env bash
# test-new-repo-loop.sh — E2E 测试：全新仓库 builder-loop 全流程
#
# 覆盖：空目录 → git init → loop-init → setup → worktree → FAIL → 修复 → PASS → trace
#
# 用法：bash test-new-repo-loop.sh
# 退出码：0=全部通过 / 1=有失败

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"/../../scripts && pwd)"
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"/../../../.. && pwd)/scripts"
PASS=0
FAIL=0

assert() {
  local desc="$1" cond="$2"
  if eval "$cond"; then
    echo "  ✅ $desc"
    PASS=$(( PASS + 1 ))
  else
    echo "  ❌ $desc"
    FAIL=$(( FAIL + 1 ))
  fi
}

# ---- Setup: 创建临时目录 ----
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "=== E2E: 全新仓库 builder-loop ==="
echo "    临时目录: $TMP"

# ---- Step 1: 创建有语法错误的 Python 文件 ----
echo "--- Step 1: 创建项目文件（含语法错误）---"
mkdir -p "$TMP/src" "$TMP/tests"

# Python 文件 — 有意的语法错误（缺少冒号）
cat > "$TMP/src/main.py" <<'PYEOF'
def hello()
    return "hello"
PYEOF

cat > "$TMP/tests/test_main.py" <<'PYEOF'
def test_hello():
    from src.main import hello
    assert hello() == "hello"
PYEOF

# ---- Step 2: loop-init（自动 git init + 生成 loop.yml）----
echo "--- Step 2: 一键 loop-init + 覆盖 pass_cmd ---"
cd "$TMP"
bash "$SCRIPT_DIR/loop-init.sh" "$TMP" 2>&1 | tail -5

# 覆盖 pass_cmd 为 py_compile（确保语法错误被检测到）
cat > "$TMP/.claude/loop.yml" <<'YMLEOF'
pass_cmd:
  - stage: syntax
    cmd: "python3 -m py_compile src/main.py"
    timeout: 30
max_iterations: 5
layout:
  source_dirs: [src]
  test_dirs: [tests]
worktree:
  enabled: false
YMLEOF
git -C "$TMP" add -A && git -C "$TMP" commit -m "chore: override loop.yml for E2E" --allow-empty-message >/dev/null 2>&1 || true

assert "loop.yml 存在" "[ -f '$TMP/.claude/loop.yml' ]"
assert "git 仓库已初始化" "git -C '$TMP' rev-parse --is-inside-work-tree >/dev/null 2>&1"
assert ".gitignore 包含 loop-runs" "grep -q 'loop-runs' '$TMP/.gitignore' 2>/dev/null"

# ---- Step 3: setup-builder-loop（不用 worktree，模拟兜底场景）----
echo "--- Step 3: setup-builder-loop ---"
cd "$TMP"
bash "$SCRIPT_DIR/setup-builder-loop.sh" --no-worktree "E2E test task" 2>&1 | tail -3

assert "状态文件存在" "[ -f '$TMP/.claude/builder-loop/state/__main__.yml' ]"
assert "active=true" "grep -q 'active: true' '$TMP/.claude/builder-loop/state/__main__.yml'"

# ---- Step 4: run-pass-cmd → 预期 FAIL（语法错误）----
echo "--- Step 4: run-pass-cmd (预期 FAIL) ---"
RESULT="$(bash "$SCRIPT_DIR/run-pass-cmd.sh" "$TMP" 1 2>/dev/null || true)"
LAST="$(echo "$RESULT" | tail -1)"

assert "PASS_CMD 返回 FAIL" "echo '$LAST' | grep -q 'FAIL'"

# ---- Step 5: 修复语法错误 → PASS ----
echo "--- Step 5: 修复语法 → 再跑 PASS_CMD ---"
cat > "$TMP/src/main.py" <<'PYEOF'
def hello():
    return "hello"
PYEOF

RESULT2="$(bash "$SCRIPT_DIR/run-pass-cmd.sh" "$TMP" 2 2>/dev/null || true)"
LAST2="$(echo "$RESULT2" | tail -1)"

assert "修复后 PASS_CMD 返回 PASS" "echo '$LAST2' | grep -q 'PASS'"

# ---- Step 6: 验证 trace（需先模拟 stop hook 的 write_trace 调用）----
echo "--- Step 6: 验证 trace 文件 ---"
# 直接验证 loop-runs 日志存在
assert "日志目录存在" "[ -d '$TMP/.claude/loop-runs' ]"
assert "iter-1 日志存在" "ls '$TMP/.claude/loop-runs/iter-1-syntax.log' >/dev/null 2>&1"
assert "iter-2 日志存在" "ls '$TMP/.claude/loop-runs/iter-2-syntax.log' >/dev/null 2>&1"

# ---- Step 7: stop hook 路径查找测试 ----
echo "--- Step 7: stop hook 路径查找 ---"
# 模拟 stop hook 接收到 CWD=$TMP 的 stdin
HOOK_OUT="$(echo "{\"cwd\":\"$TMP\"}" | bash "$HOOK_DIR/builder-loop-stop.sh" 2>&1; echo "EXIT:$?")"
# 预期：stop hook 应该找到状态文件并跑 PASS_CMD（因为语法已修复，应 PASS）
assert "stop hook 执行完成" "echo '$HOOK_OUT' | grep -qE 'PASS|EXIT:2'"

# ---- 汇报 ----
echo ""
echo "=== E2E 结果：✅ $PASS 通过，❌ $FAIL 失败 ==="
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
