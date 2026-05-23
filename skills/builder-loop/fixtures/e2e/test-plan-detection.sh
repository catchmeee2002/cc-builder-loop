#!/usr/bin/env bash
# test-plan-detection.sh — E2E：install.sh / diagnose-stop-hook.sh 按 ANTHROPIC_BASE_URL 识别 max / copilot 方案
#
# 场景：
#   - max env（unset ANTHROPIC_BASE_URL）：install 注册 6 条 hook，diagnose [1/6] verdict=ok
#   - copilot env（ANTHROPIC_BASE_URL=http://127.0.0.1:4141）：install 注册 6 条 hook，diagnose [1/6] verdict=ok
#
# 用法：bash test-plan-detection.sh
# 退出码：0=全部通过 / 1=有失败

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "plan-detection"

INSTALL_SCRIPT="$HARNESS_REPO_ROOT/install.sh"
UNINSTALL_SCRIPT="$HARNESS_REPO_ROOT/uninstall.sh"
DIAG_SCRIPT="$HARNESS_REPO_ROOT/skills/builder-loop/scripts/diagnose-stop-hook.sh"

assert "install.sh 存在" "[ -f '$INSTALL_SCRIPT' ]"
assert "uninstall.sh 存在" "[ -f '$UNINSTALL_SCRIPT' ]"
assert "diagnose 脚本存在" "[ -f '$DIAG_SCRIPT' ]"

if ! command -v python3 &>/dev/null; then
  echo "SKIP: python3 不可用"
  exit 0
fi

TMPHOME="$(mktemp -d -t builder-loop-plan-XXXXXX)"
TMPREPO="$(mktemp -d -t builder-loop-plan-repo-XXXXXX)"
_HARNESS_TMPDIRS+=("$TMPHOME" "$TMPREPO")

# ---- 准备临时仓（含 .claude/loop.yml 让 diagnose 能找到 PROJECT_ROOT） ----
mkdir -p "$TMPREPO/.claude"
cat > "$TMPREPO/.claude/loop.yml" <<'YML'
pass_cmd:
  - stage: smoke
    cmd: "true"
YML

# ---- 准备 baseline settings.json ----
mkdir -p "$TMPHOME/.claude"
cat > "$TMPHOME/.claude/settings.json" <<'JSON'
{
  "permissions": {"allow": []},
  "hooks": {}
}
JSON

# 辅助：统计 builder-loop hook 条数
count_bl_hooks() {
  python3 -c '
import json, sys
cfg = json.load(open(sys.argv[1]))
hooks = cfg.get("hooks", {})
bl_scripts = ["builder-loop-stop.sh", "subagent-start-guard.sh",
              "tester-lock-check.sh", "tester-lock-clear.sh",
              "worktree-write-guard.sh", "reviewer-timing-check.sh"]
n = 0
for arr in hooks.values():
    for item in arr:
        for h in item.get("hooks", []):
            if any(s in h.get("command", "") for s in bl_scripts):
                n += 1
print(n)
' "$1"
}

# 辅助：检查某 hook 是否已注册
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

# 辅助：跑命令返回 handle（通用，传入完整命令参数）
# 用法：run_cmd_with_env env_args... -- cmd args...
run_cmd() {
  local handle
  handle="$(mktemp -d -t harness-result-XXXXXX)"
  _HARNESS_TMPDIRS+=("$handle")
  local ec=0
  "$@" >"$handle/stdout" 2>"$handle/stderr" || ec=$?
  echo "$ec" > "$handle/ec"
  echo "$handle"
}

# ============================================================
# Case 1: max env install（unset ANTHROPIC_BASE_URL）
# ============================================================
section "Case 1: max env install"
RES_I1="$(run_cmd env -u ANTHROPIC_BASE_URL HOME="$TMPHOME" bash "$INSTALL_SCRIPT")"
assert_ec "max env install 退出码 0" "$RES_I1" 0
assert_stdout_contains "max install 输出含 '检测方案=max'" "$RES_I1" '检测方案=max'
assert_stdout_contains "max install 输出含 '0 条跳过'" "$RES_I1" '0 条跳过'

n="$(count_bl_hooks "$TMPHOME/.claude/settings.json")"
assert "max install 后 settings.json 含 6 条 builder-loop hook（实际=$n）" "[ '$n' = '6' ]"
if has_hook "$TMPHOME/.claude/settings.json" "worktree-write-guard.sh"; then
  assert "max install 后 worktree-write-guard.sh 已注册" "true"
else
  assert "max install 后 worktree-write-guard.sh 已注册" "false"
fi

# ============================================================
# Case 2: max env diagnose
# ============================================================
section "Case 2: max env diagnose"
RES_D1="$(run_cmd env -u ANTHROPIC_BASE_URL HOME="$TMPHOME" bash "$DIAG_SCRIPT" "$TMPREPO")"
assert_stdout_contains "max diagnose 输出含 'plan: max'" "$RES_D1" 'plan: max'

# [1/6] + [2/6] verdict=ok 需要在合并的 stdout+stderr 中找
DIAG_OUT1="$(cat "$RES_D1/stdout" "$RES_D1/stderr" 2>/dev/null)"
if echo "$DIAG_OUT1" | grep -E '^\[1/6\]' | grep -qF 'ok'; then
  assert "max diagnose [1/6] verdict=ok" "true"
else
  assert "max diagnose [1/6] verdict=ok" "false"
fi

if echo "$DIAG_OUT1" | grep -E '^\[2/6\]' | grep -qF 'ok'; then
  assert "max diagnose [2/6] verdict=ok" "true"
else
  assert "max diagnose [2/6] verdict=ok" "false"
fi

DIAG_EC1="$(result_ec "$RES_D1")"
assert "max diagnose 退出码 != 2（实际=$DIAG_EC1）" "[ '$DIAG_EC1' -ne 2 ]"

# --json 模式
DIAG_JSON_OUT="$(env -u ANTHROPIC_BASE_URL HOME="$TMPHOME" bash "$DIAG_SCRIPT" "$TMPREPO" --json 2>/dev/null || true)"
JSON_PLAN_OK=0
echo "$DIAG_JSON_OUT" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    sys.exit(0 if d.get("plan") == "max" else 1)
except Exception:
    sys.exit(1)
' 2>/dev/null && JSON_PLAN_OK=1
assert "max diagnose --json 输出 plan='max'" "[ '$JSON_PLAN_OK' -eq 1 ]"

# ============================================================
# Case 3: uninstall
# ============================================================
section "Case 3: uninstall"
HOME="$TMPHOME" bash "$UNINSTALL_SCRIPT" >/dev/null 2>&1 || true
n="$(count_bl_hooks "$TMPHOME/.claude/settings.json")"
assert "uninstall 后 settings.json 含 0 条 builder-loop hook" "[ '$n' = '0' ]"

# ============================================================
# Case 4: copilot env install
# ============================================================
section "Case 4: copilot env install"
RES_I2="$(run_cmd env ANTHROPIC_BASE_URL=http://127.0.0.1:4141 HOME="$TMPHOME" bash "$INSTALL_SCRIPT")"
assert_ec "copilot env install 退出码 0" "$RES_I2" 0
assert_stdout_contains "copilot install 输出含 '检测方案=copilot'" "$RES_I2" '检测方案=copilot'
assert_stdout_contains "copilot install 输出含 '0 条跳过'" "$RES_I2" '0 条跳过'

n="$(count_bl_hooks "$TMPHOME/.claude/settings.json")"
assert "copilot install 后 settings.json 含 6 条 builder-loop hook（实际=$n）" "[ '$n' = '6' ]"
if has_hook "$TMPHOME/.claude/settings.json" "worktree-write-guard.sh"; then
  assert "copilot install 后 worktree-write-guard.sh 已注册" "true"
else
  assert "copilot install 后 worktree-write-guard.sh 已注册" "false"
fi

# ============================================================
# Case 5: copilot env diagnose
# ============================================================
section "Case 5: copilot env diagnose"
RES_D2="$(run_cmd env ANTHROPIC_BASE_URL=http://127.0.0.1:4141 HOME="$TMPHOME" bash "$DIAG_SCRIPT" "$TMPREPO")"
assert_stdout_contains "copilot diagnose 输出含 'plan: copilot'" "$RES_D2" 'plan: copilot'

DIAG_OUT2="$(cat "$RES_D2/stdout" "$RES_D2/stderr" 2>/dev/null)"
if echo "$DIAG_OUT2" | grep -E '^\[1/6\]' | grep -qF 'ok'; then
  assert "copilot diagnose [1/6] verdict=ok" "true"
else
  assert "copilot diagnose [1/6] verdict=ok" "false"
fi

if echo "$DIAG_OUT2" | grep -E '^\[2/6\]' | grep -qF 'ok'; then
  assert "copilot diagnose [2/6] verdict=ok" "true"
else
  assert "copilot diagnose [2/6] verdict=ok" "false"
fi

DIAG_EC2="$(result_ec "$RES_D2")"
assert "copilot diagnose 退出码 != 2（实际=$DIAG_EC2）" "[ '$DIAG_EC2' -ne 2 ]"

# --json 模式
DIAG_JSON_OUT2="$(env ANTHROPIC_BASE_URL=http://127.0.0.1:4141 HOME="$TMPHOME" bash "$DIAG_SCRIPT" "$TMPREPO" --json 2>/dev/null || true)"
JSON_PLAN_OK2=0
echo "$DIAG_JSON_OUT2" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    sys.exit(0 if d.get("plan") == "copilot" else 1)
except Exception:
    sys.exit(1)
' 2>/dev/null && JSON_PLAN_OK2=1
assert "copilot diagnose --json 输出 plan='copilot'" "[ '$JSON_PLAN_OK2' -eq 1 ]"

harness_report
