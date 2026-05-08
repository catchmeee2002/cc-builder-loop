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

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
INSTALL_SCRIPT="$REPO_ROOT/install.sh"
UNINSTALL_SCRIPT="$REPO_ROOT/uninstall.sh"

if [ ! -f "$INSTALL_SCRIPT" ] || [ ! -f "$UNINSTALL_SCRIPT" ]; then
  echo "❌ FAIL: install.sh / uninstall.sh 不存在" >&2
  exit 1
fi

if ! command -v python3 &>/dev/null; then
  echo "❌ SKIP: python3 不可用，跳过 fixture" >&2
  exit 0
fi

TMPHOME="$(mktemp -d -t builder-loop-roundtrip-XXXXXX)"
trap 'rm -rf "$TMPHOME"' EXIT

mkdir -p "$TMPHOME/.claude"
SETTINGS="$TMPHOME/.claude/settings.json"

# ---- 1. 写 baseline settings.json：含 1 条非 builder-loop hook + 1 个 permissions 字段 ----
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

# ---- 2. 跑 install.sh（强制 copilot 方案保证装 6 条 hook，roundtrip 测主旨是装→卸还原 baseline，不是方案识别） ----
if ! ANTHROPIC_BASE_URL=http://127.0.0.1:4141 HOME="$TMPHOME" bash "$INSTALL_SCRIPT" >/tmp/install-roundtrip.log 2>&1; then
  ec=$?
  echo "❌ FAIL: install.sh exit=$ec" >&2
  cat /tmp/install-roundtrip.log >&2
  exit 1
fi

# ---- 3. 断言：install 后含 6 条 builder-loop hook ----
bl_count=$(python3 -c '
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
' "$SETTINGS")

if [ "$bl_count" != "6" ]; then
  echo "❌ FAIL: install 后 builder-loop hook 数=$bl_count，期望 6" >&2
  cat "$SETTINGS" >&2
  exit 1
fi

# ---- 4. 断言：原 user hook 仍在 ----
if ! python3 -c '
import json, sys
cfg = json.load(open(sys.argv[1]))
arr = cfg.get("hooks", {}).get("PreToolUse", [])
found = any(
    "/some/user/script.sh" in h.get("command", "")
    for item in arr for h in item.get("hooks", [])
)
sys.exit(0 if found else 1)
' "$SETTINGS"; then
  echo "❌ FAIL: install 后用户原 hook 丢失" >&2
  cat "$SETTINGS" >&2
  exit 1
fi

# ---- 5. 跑 uninstall.sh ----
if ! HOME="$TMPHOME" bash "$UNINSTALL_SCRIPT" >/tmp/uninstall-roundtrip.log 2>&1; then
  ec=$?
  echo "❌ FAIL: uninstall.sh exit=$ec" >&2
  cat /tmp/uninstall-roundtrip.log >&2
  exit 1
fi

# ---- 6. 断言：uninstall 后 settings.json 与 baseline 语义等价 ----
# uninstall.sh 会用 python3 重写 settings.json（json.dump），key 顺序可能与 baseline 不同。
# 用 python3 解析后做 deep-equal 比较，而不是字节比较。
if ! python3 -c '
import json, sys
a = json.load(open(sys.argv[1]))
b = json.load(open(sys.argv[2]))
sys.exit(0 if a == b else 1)
' "$SETTINGS" "$TMPHOME/baseline.json"; then
  echo "❌ FAIL: uninstall 后 settings.json 与 baseline 不一致" >&2
  echo "--- baseline ---" >&2
  cat "$TMPHOME/baseline.json" >&2
  echo "--- after uninstall ---" >&2
  cat "$SETTINGS" >&2
  exit 1
fi

echo "✅ PASS: install→uninstall 往返后 settings.json 还原到 baseline"
exit 0
