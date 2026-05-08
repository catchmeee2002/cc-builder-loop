#!/usr/bin/env bash
# test-install-matcher-update.sh — 验证 install.sh 检测到 matcher 字面变化时会先删旧条目再写新条目
#
# 场景：
#   - 临时 HOME + 临时 install.sh 副本
#   - 第一次 install：matcher = "Read|Grep|Glob"
#   - sed 改 install.sh 副本，让 tester-lock-check.sh 那行的 matcher 变 "Read|Grep|Glob|WebFetch"
#   - 第二次 install：应识别为 stale → 删旧 + 写新
#
# 期望：
#   - 第二次 install 后 settings.json 里 tester-lock-check.sh 这条 matcher = "Read|Grep|Glob|WebFetch"
#   - PreToolUse 数组里只有一条 tester-lock-check.sh（不是 stale + new 两条）
#   - install.sh 输出含 "1 条更新"
#
# 用法：bash test-install-matcher-update.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
INSTALL_SCRIPT="$REPO_ROOT/install.sh"

if [ ! -f "$INSTALL_SCRIPT" ]; then
  echo "❌ FAIL: install.sh 不存在: $INSTALL_SCRIPT" >&2
  exit 1
fi

if ! command -v python3 &>/dev/null; then
  echo "❌ SKIP: python3 不可用，跳过 fixture" >&2
  exit 0
fi

TMPHOME="$(mktemp -d -t builder-loop-matcher-home-XXXXXX)"
TMPREPO="$(mktemp -d -t builder-loop-matcher-repo-XXXXXX)"
trap 'rm -rf "$TMPHOME" "$TMPREPO"' EXIT

# ---- 1. 准备临时仓：复制 install.sh 副本（其它目录留空，软链段会跳过空目录） ----
cp "$INSTALL_SCRIPT" "$TMPREPO/install.sh"
chmod +x "$TMPREPO/install.sh"

# ---- 2. 准备 TMPHOME/.claude/settings.json（空 hooks，允许 install.sh 注册） ----
mkdir -p "$TMPHOME/.claude"
cat > "$TMPHOME/.claude/settings.json" <<'JSON'
{
  "permissions": {"allow": []},
  "hooks": {}
}
JSON

# ---- 3. 第一次 install ----
if ! HOME="$TMPHOME" bash "$TMPREPO/install.sh" >/tmp/install-matcher-1.log 2>&1; then
  ec=$?
  echo "❌ FAIL: 第一次 install exit=$ec" >&2
  cat /tmp/install-matcher-1.log >&2
  exit 1
fi

# ---- 4. 断言：tester-lock-check.sh 的 matcher = "Read|Grep|Glob" ----
matcher_before=$(python3 -c '
import json, sys
cfg = json.load(open(sys.argv[1]))
for item in cfg.get("hooks", {}).get("PreToolUse", []):
    for h in item.get("hooks", []):
        if "tester-lock-check.sh" in h.get("command", ""):
            print(item.get("matcher", ""))
            sys.exit(0)
sys.exit(1)
' "$TMPHOME/.claude/settings.json")

if [ "$matcher_before" != "Read|Grep|Glob" ]; then
  echo "❌ FAIL: 第一次 install 后 tester-lock-check.sh matcher='$matcher_before'，期望 'Read|Grep|Glob'" >&2
  exit 1
fi

# ---- 5. sed 改临时副本：tester-lock-check.sh matcher 加 |WebFetch ----
sed -i 's#"tester-lock-check.sh",      "Read|Grep|Glob"#"tester-lock-check.sh",      "Read|Grep|Glob|WebFetch"#' "$TMPREPO/install.sh"

# 验证 sed 真改成了
if ! grep -q 'Read|Grep|Glob|WebFetch' "$TMPREPO/install.sh"; then
  echo "❌ FAIL: sed 未能改 install.sh 副本的 matcher" >&2
  grep -n 'tester-lock-check' "$TMPREPO/install.sh" >&2 || true
  exit 1
fi

# ---- 6. 第二次 install ----
if ! HOME="$TMPHOME" bash "$TMPREPO/install.sh" >/tmp/install-matcher-2.log 2>&1; then
  ec=$?
  echo "❌ FAIL: 第二次 install exit=$ec" >&2
  cat /tmp/install-matcher-2.log >&2
  exit 1
fi

# ---- 7. 断言：install 输出含 "1 条更新" ----
if ! grep -q '1 条更新' /tmp/install-matcher-2.log; then
  echo "❌ FAIL: 第二次 install 输出未含 '1 条更新'" >&2
  cat /tmp/install-matcher-2.log >&2
  exit 1
fi

# ---- 8. 断言：tester-lock-check.sh 的 matcher 已变成新值 + 数组里只有一条 ----
result=$(python3 -c '
import json, sys
cfg = json.load(open(sys.argv[1]))
arr = cfg.get("hooks", {}).get("PreToolUse", [])
matched = []
for item in arr:
    for h in item.get("hooks", []):
        if "tester-lock-check.sh" in h.get("command", ""):
            matched.append(item.get("matcher", ""))
print(f"count={len(matched)} matchers={matched}")
' "$TMPHOME/.claude/settings.json")

expected='count=1 matchers=['"'"'Read|Grep|Glob|WebFetch'"'"']'
if [ "$result" != "$expected" ]; then
  echo "❌ FAIL: 期望 $expected" >&2
  echo "  实际: $result" >&2
  cat "$TMPHOME/.claude/settings.json" >&2
  exit 1
fi

echo "✅ PASS: install.sh 识别 matcher 字面变化，stale 条目被删 + 新条目写入"
exit 0
