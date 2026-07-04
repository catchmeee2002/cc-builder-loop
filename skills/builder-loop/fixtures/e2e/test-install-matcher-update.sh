#!/usr/bin/env bash
# test-install-matcher-update.sh — 验证 install.sh 检测到 matcher 字面变化时会先删旧条目再写新条目
#
# 场景：
#   - 临时 HOME + 临时 install.sh 副本
#   - 第一次 install：reviewer-timing-check.sh matcher = "Agent"
#   - sed 改 install.sh 副本，让 reviewer-timing-check.sh 那行的 matcher 变 "Agent|Workflow"
#   - 第二次 install：应识别为 stale → 删旧 + 写新
#
# 期望：
#   - 第二次 install 后 settings.json 里 reviewer-timing-check.sh 这条 matcher = "Agent|Workflow"
#   - PreToolUse 数组里只有一条 reviewer-timing-check.sh（不是 stale + new 两条）
#   - install.sh 输出含 "条写入"（V5.3 幂等覆盖不再区分新增/更新）
#
# 用法：bash test-install-matcher-update.sh

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "install-matcher-update"

INSTALL_SCRIPT="$HARNESS_REPO_ROOT/install.sh"

assert "install.sh 存在" "[ -f '$INSTALL_SCRIPT' ]"

if ! command -v python3 &>/dev/null; then
  echo "SKIP: python3 不可用"
  exit 0
fi

TMPHOME="$(mktemp -d -t builder-loop-matcher-home-XXXXXX)"
TMPREPO="$(mktemp -d -t builder-loop-matcher-repo-XXXXXX)"
_HARNESS_TMPDIRS+=("$TMPHOME" "$TMPREPO")

# ---- 1. 准备临时仓：复制 install.sh 副本 ----
cp "$INSTALL_SCRIPT" "$TMPREPO/install.sh"
chmod +x "$TMPREPO/install.sh"

# ---- 2. 准备 TMPHOME/.claude/settings.json ----
mkdir -p "$TMPHOME/.claude"
cat > "$TMPHOME/.claude/settings.json" <<'JSON'
{
  "permissions": {"allow": []},
  "hooks": {}
}
JSON

# ---- 3. 第一次 install ----
section "第一次 install"
INSTALL1_LOG="$(mktemp)"
_HARNESS_TMPDIRS+=("$INSTALL1_LOG")
INSTALL1_EC=0
HOME="$TMPHOME" bash "$TMPREPO/install.sh" >"$INSTALL1_LOG" 2>&1 || INSTALL1_EC=$?
assert "第一次 install 退出码=0" "[ '$INSTALL1_EC' -eq 0 ]"

# ---- 4. 断言：matcher = "Agent" ----
matcher_before=$(python3 -c '
import json, sys
cfg = json.load(open(sys.argv[1]))
for item in cfg.get("hooks", {}).get("PreToolUse", []):
    for h in item.get("hooks", []):
        if "reviewer-timing-check.sh" in h.get("command", ""):
            print(item.get("matcher", ""))
            sys.exit(0)
sys.exit(1)
' "$TMPHOME/.claude/settings.json")
assert "第一次 install 后 matcher=Agent" "[ '$matcher_before' = 'Agent' ]"

# ---- 5. sed 改临时副本：matcher 加 |Workflow ----
section "sed 改 matcher 后第二次 install"
sed -i 's#"reviewer-timing-check.sh",  "Agent"#"reviewer-timing-check.sh",  "Agent|Workflow"#' "$TMPREPO/install.sh"
assert "sed 改成功" "grep -q 'Agent|Workflow' '$TMPREPO/install.sh'"

# ---- 6. 第二次 install ----
INSTALL2_LOG="$(mktemp)"
_HARNESS_TMPDIRS+=("$INSTALL2_LOG")
INSTALL2_EC=0
HOME="$TMPHOME" bash "$TMPREPO/install.sh" >"$INSTALL2_LOG" 2>&1 || INSTALL2_EC=$?
assert "第二次 install 退出码=0" "[ '$INSTALL2_EC' -eq 0 ]"

# ---- 7. 断言：install 输出含 "写入"（V5.3 幂等覆盖不再区分新增/更新）----
assert_contains "install 输出含 '条写入'" "$INSTALL2_LOG" '条写入'

# ---- 8. 断言：matcher 已更新 + 只有一条 ----
section "验证 matcher 更新结果"
matcher_check_ok=0
python3 -c '
import json, sys
cfg = json.load(open(sys.argv[1]))
arr = cfg.get("hooks", {}).get("PreToolUse", [])
matched = []
for item in arr:
    for h in item.get("hooks", []):
        if "reviewer-timing-check.sh" in h.get("command", ""):
            matched.append(item.get("matcher", ""))
assert len(matched) == 1, f"expected 1 entry, got {len(matched)}"
assert matched[0] == "Agent|Workflow", f"unexpected matcher: {matched[0]}"
' "$TMPHOME/.claude/settings.json" && matcher_check_ok=1
assert "stale 条目被删 + 新条目写入（count=1, matcher=Agent|Workflow）" "[ '$matcher_check_ok' -eq 1 ]"

harness_report
