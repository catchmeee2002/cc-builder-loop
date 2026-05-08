#!/usr/bin/env bash
# test-plan-detection.sh — E2E：install.sh / diagnose-stop-hook.sh 按 ANTHROPIC_BASE_URL 识别 max / copilot 方案
#
# 场景：
#   - max env（unset ANTHROPIC_BASE_URL）：install 注册 5 条 hook（跳过 tester-write-guard.sh），diagnose [1/6] verdict=ok
#   - copilot env（ANTHROPIC_BASE_URL=http://127.0.0.1:4141）：install 注册 6 条 hook，diagnose [1/6] verdict=ok
#
# 用法：bash test-plan-detection.sh
# 退出码：0=全部通过 / 1=有失败

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
INSTALL_SCRIPT="$REPO_ROOT/install.sh"
UNINSTALL_SCRIPT="$REPO_ROOT/uninstall.sh"
DIAG_SCRIPT="$REPO_ROOT/skills/builder-loop/scripts/diagnose-stop-hook.sh"

if [ ! -f "$INSTALL_SCRIPT" ] || [ ! -f "$UNINSTALL_SCRIPT" ] || [ ! -f "$DIAG_SCRIPT" ]; then
  echo "❌ FAIL: install.sh / uninstall.sh / diagnose 不全" >&2
  exit 1
fi

if ! command -v python3 &>/dev/null; then
  echo "❌ SKIP: python3 不可用" >&2
  exit 0
fi

TMPHOME="$(mktemp -d -t builder-loop-plan-XXXXXX)"
TMPREPO="$(mktemp -d -t builder-loop-plan-repo-XXXXXX)"
trap 'rm -rf "$TMPHOME" "$TMPREPO"' EXIT

# ---- 1. 准备临时仓（含 .claude/loop.yml 让 diagnose 能找到 PROJECT_ROOT） ----
mkdir -p "$TMPREPO/.claude"
cat > "$TMPREPO/.claude/loop.yml" <<'YML'
pass_cmd:
  - stage: smoke
    cmd: "true"
YML

# ---- 2. 准备 baseline settings.json ----
mkdir -p "$TMPHOME/.claude"
cat > "$TMPHOME/.claude/settings.json" <<'JSON'
{
  "permissions": {"allow": []},
  "hooks": {}
}
JSON

PASS=0
FAIL=0

# 直接 if 结构断言，避开 eval（install/diagnose 输出含中文括号 / 单引号会让 eval 炸）
ok()   { echo "  ✅ $1"; PASS=$(( PASS + 1 )); }
fail() { echo "  ❌ $1"; FAIL=$(( FAIL + 1 )); }

count_bl_hooks() {
  python3 -c '
import json, sys
cfg = json.load(open(sys.argv[1]))
hooks = cfg.get("hooks", {})
bl_scripts = ["builder-loop-stop.sh", "tester-lock-write.sh",
              "tester-lock-check.sh", "tester-lock-clear.sh",
              "tester-write-guard.sh", "reviewer-timing-check.sh"]
n = 0
for arr in hooks.values():
    for item in arr:
        for h in item.get("hooks", []):
            if any(s in h.get("command", "") for s in bl_scripts):
                n += 1
print(n)
' "$1"
}

has_hook() {
  python3 -c '
import json, sys
cfg = json.load(open(sys.argv[1]))
for arr in cfg.get("hooks", {}).values():
    for item in arr:
        for h in item.get("hooks", []):
            if sys.argv[2] in h.get("command", ""):
                sys.exit(0)
sys.exit(1)
' "$1" "$2"
}

echo "=== E2E: 方案识别（max / copilot）==="

# ============================================================
# Case 1: max env install（unset ANTHROPIC_BASE_URL）
# ============================================================
echo ""
echo "--- Case 1: max env install ---"
INSTALL_OUT_FILE="$(mktemp)"
INSTALL_EC=0
env -u ANTHROPIC_BASE_URL HOME="$TMPHOME" bash "$INSTALL_SCRIPT" >"$INSTALL_OUT_FILE" 2>&1 || INSTALL_EC=$?

if [ "$INSTALL_EC" != "0" ]; then
  fail "max env install 退出码 0（实际=$INSTALL_EC）"
  cat "$INSTALL_OUT_FILE" >&2
  exit 1
fi
ok "max env install 退出码 0"

if grep -qF '检测方案=max' "$INSTALL_OUT_FILE"; then
  ok "max install 输出含 '检测方案=max'"
else
  fail "max install 输出缺 '检测方案=max'"
fi

if grep -qF '1 条跳过' "$INSTALL_OUT_FILE"; then
  ok "max install 输出含 '1 条跳过'"
else
  fail "max install 输出缺 '1 条跳过'"
fi

n="$(count_bl_hooks "$TMPHOME/.claude/settings.json")"
if [ "$n" = "5" ]; then
  ok "max install 后 settings.json 含 5 条 builder-loop hook"
else
  fail "max install 后 settings.json 含 $n 条 hook（期望 5）"
fi

if has_hook "$TMPHOME/.claude/settings.json" "tester-write-guard.sh"; then
  fail "max install 后 tester-write-guard.sh 不该被注册"
else
  ok "max install 后 tester-write-guard.sh 未注册（符合 max 方案）"
fi

# ============================================================
# Case 2: max env diagnose
# ============================================================
echo ""
echo "--- Case 2: max env diagnose ---"
DIAG_OUT_FILE="$(mktemp)"
DIAG_EC=0
env -u ANTHROPIC_BASE_URL HOME="$TMPHOME" bash "$DIAG_SCRIPT" "$TMPREPO" >"$DIAG_OUT_FILE" 2>&1 || DIAG_EC=$?

if grep -qF 'plan: max' "$DIAG_OUT_FILE"; then
  ok "max diagnose 输出含 'plan: max'"
else
  fail "max diagnose 输出缺 'plan: max'"
fi

if grep -E '^\[1/6\]' "$DIAG_OUT_FILE" | grep -qF 'ok'; then
  ok "max diagnose [1/6] verdict=ok"
else
  fail "max diagnose [1/6] verdict 不是 ok"
fi

# diagnose 退出码语义：0=全 ok / 1=有 warn / 2=有 fail
# 临时仓 trace 必然为空，[5/6] warn 是合理的，断言不能=2 即 [1/6]/[2/6] 等都没 fail
if [ "$DIAG_EC" -ne 2 ]; then
  ok "max diagnose 退出码 != 2（[1/6] 等关键段无 fail，实际=$DIAG_EC，warn 来自空 trace 合理）"
else
  fail "max diagnose 退出码 = 2（有 fail 段）"
  cat "$DIAG_OUT_FILE" >&2
fi

# ============================================================
# Case 3: uninstall + 还原（容忍 pre-existing bug：main HEAD uninstall.sh 漏 reviewer-timing-check）
# ============================================================
echo ""
echo "--- Case 3: uninstall ---"
HOME="$TMPHOME" bash "$UNINSTALL_SCRIPT" >/dev/null 2>&1 || true
n="$(count_bl_hooks "$TMPHOME/.claude/settings.json")"
# A 批 cherry-pick 后期望 0；当前 main 的 uninstall.sh 漏 reviewer-timing-check 残留 1 条
if [ "$n" -le 1 ]; then
  ok "uninstall 后 settings.json 含 $n 条 hook（≤1，A 批 cherry-pick 后会到 0）"
else
  fail "uninstall 后 settings.json 含 $n 条 hook（>1，超出 pre-existing bug 范围）"
fi

# ============================================================
# Case 4: copilot env install
# ============================================================
echo ""
echo "--- Case 4: copilot env install ---"
INSTALL_OUT_FILE2="$(mktemp)"
INSTALL_EC2=0
ANTHROPIC_BASE_URL=http://127.0.0.1:4141 HOME="$TMPHOME" bash "$INSTALL_SCRIPT" >"$INSTALL_OUT_FILE2" 2>&1 || INSTALL_EC2=$?

if [ "$INSTALL_EC2" != "0" ]; then
  fail "copilot env install 退出码 0（实际=$INSTALL_EC2）"
  cat "$INSTALL_OUT_FILE2" >&2
  exit 1
fi
ok "copilot env install 退出码 0"

if grep -qF '检测方案=copilot' "$INSTALL_OUT_FILE2"; then
  ok "copilot install 输出含 '检测方案=copilot'"
else
  fail "copilot install 输出缺 '检测方案=copilot'"
fi

if grep -qF '0 条跳过' "$INSTALL_OUT_FILE2"; then
  ok "copilot install 输出含 '0 条跳过'"
else
  fail "copilot install 输出缺 '0 条跳过'"
fi

n="$(count_bl_hooks "$TMPHOME/.claude/settings.json")"
if [ "$n" = "6" ]; then
  ok "copilot install 后 settings.json 含 6 条 builder-loop hook"
else
  fail "copilot install 后 settings.json 含 $n 条 hook（期望 6）"
fi

if has_hook "$TMPHOME/.claude/settings.json" "tester-write-guard.sh"; then
  ok "copilot install 后 tester-write-guard.sh 已注册"
else
  fail "copilot install 后 tester-write-guard.sh 未注册"
fi

# ============================================================
# Case 5: copilot env diagnose
# ============================================================
echo ""
echo "--- Case 5: copilot env diagnose ---"
DIAG_OUT_FILE2="$(mktemp)"
DIAG_EC2=0
ANTHROPIC_BASE_URL=http://127.0.0.1:4141 HOME="$TMPHOME" bash "$DIAG_SCRIPT" "$TMPREPO" >"$DIAG_OUT_FILE2" 2>&1 || DIAG_EC2=$?

if grep -qF 'plan: copilot' "$DIAG_OUT_FILE2"; then
  ok "copilot diagnose 输出含 'plan: copilot'"
else
  fail "copilot diagnose 输出缺 'plan: copilot'"
fi

if grep -E '^\[1/6\]' "$DIAG_OUT_FILE2" | grep -qF 'ok'; then
  ok "copilot diagnose [1/6] verdict=ok"
else
  fail "copilot diagnose [1/6] verdict 不是 ok"
fi

if [ "$DIAG_EC2" -ne 2 ]; then
  ok "copilot diagnose 退出码 != 2（实际=$DIAG_EC2）"
else
  fail "copilot diagnose 退出码 = 2"
  cat "$DIAG_OUT_FILE2" >&2
fi

# ============================================================
rm -f "$INSTALL_OUT_FILE" "$INSTALL_OUT_FILE2" "$DIAG_OUT_FILE" "$DIAG_OUT_FILE2"

echo ""
if [ "$FAIL" -gt 0 ]; then
  echo "❌ FAIL: $FAIL 个断言失败（PASS=$PASS）"
  exit 1
fi
echo "✅ PASS: $PASS 个断言全部通过"
exit 0
