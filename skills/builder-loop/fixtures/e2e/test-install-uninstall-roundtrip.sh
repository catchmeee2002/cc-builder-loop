#!/usr/bin/env bash
# test-install-uninstall-roundtrip.sh — 验证 install → uninstall 后 settings.json 与 install 前 byte-equal
#
# 场景：
#   - 临时 HOME，模拟 ~/.claude/ 含 baseline settings.json（含若干非 builder-loop 条目）
#   - 跑 install.sh → 跑 uninstall.sh
#
# 期望：
#   - install 后 settings.json 含 6 条 builder-loop hook 条目
#   - uninstall 后 settings.json 与 baseline byte-equal（diff 为空）
#
# 用法：bash test-install-uninstall-roundtrip.sh

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "install-uninstall-roundtrip"

INSTALL_SCRIPT="$HARNESS_REPO_ROOT/install.sh"
UNINSTALL_SCRIPT="$HARNESS_REPO_ROOT/uninstall.sh"

assert "install.sh 存在" "[ -f '$INSTALL_SCRIPT' ]"
assert "uninstall.sh 存在" "[ -f '$UNINSTALL_SCRIPT' ]"

if ! command -v python3 &>/dev/null; then
  echo "SKIP: python3 不可用"
  exit 0
fi

TMPHOME="$(mktemp -d -t builder-loop-roundtrip-XXXXXX)"
_HARNESS_TMPDIRS+=("$TMPHOME")

mkdir -p "$TMPHOME/.claude"
SETTINGS="$TMPHOME/.claude/settings.json"

# ---- 1. 写 baseline settings.json ----
section "写 baseline + install"
cat > "$SETTINGS" <<'JSON'
{
  "permissions": {
    "allow": ["Bash(ls *)", "Bash(git status:*)"]
  },
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "/some/user/script.sh"}]
      }
    ]
  }
}
JSON
cp "$SETTINGS" "$TMPHOME/baseline.json"

# ---- 2. 跑 install.sh ----
INSTALL_LOG="$(mktemp)"
_HARNESS_TMPDIRS+=("$INSTALL_LOG")
INSTALL_EC=0
ANTHROPIC_BASE_URL=http://127.0.0.1:4141 HOME="$TMPHOME" bash "$INSTALL_SCRIPT" >"$INSTALL_LOG" 2>&1 || INSTALL_EC=$?
assert "install 退出码=0" "[ '$INSTALL_EC' -eq 0 ]"

# ---- 3. 断言：install 后含 6 条 builder-loop hook ----
section "验证 install 后 hook 数量"
bl_count=$(python3 -c '
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
' "$SETTINGS")
assert "install 后 builder-loop hook 数=6" "[ '$bl_count' = '6' ]"

# ---- 4. 断言：原 user hook 仍在 ----
user_hook_ok=0
python3 -c '
import json, sys
cfg = json.load(open(sys.argv[1]))
arr = cfg.get("hooks", {}).get("PreToolUse", [])
found = any(
    "/some/user/script.sh" in h.get("command", "")
    for item in arr for h in item.get("hooks", [])
)
sys.exit(0 if found else 1)
' "$SETTINGS" && user_hook_ok=1
assert "install 后用户原 hook 仍在" "[ '$user_hook_ok' -eq 1 ]"

# ---- 5. 跑 uninstall.sh ----
section "uninstall + 还原验证"
UNINSTALL_LOG="$(mktemp)"
_HARNESS_TMPDIRS+=("$UNINSTALL_LOG")
UNINSTALL_EC=0
HOME="$TMPHOME" bash "$UNINSTALL_SCRIPT" >"$UNINSTALL_LOG" 2>&1 || UNINSTALL_EC=$?
assert "uninstall 退出码=0" "[ '$UNINSTALL_EC' -eq 0 ]"

# ---- 6. 断言：uninstall 后 settings.json 与 baseline 语义等价 ----
deep_equal_ok=0
python3 -c '
import json, sys
a = json.load(open(sys.argv[1]))
b = json.load(open(sys.argv[2]))
sys.exit(0 if a == b else 1)
' "$SETTINGS" "$TMPHOME/baseline.json" && deep_equal_ok=1
assert "uninstall 后 settings.json 与 baseline 语义等价" "[ '$deep_equal_ok' -eq 1 ]"

harness_report
