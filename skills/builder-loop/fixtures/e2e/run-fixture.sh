#!/usr/bin/env bash
# run-fixture.sh — e2e 验证：小 py 项目完整循环（setup → FAIL → fix → PASS）
#
# 场景：
#   - 复制 sample-project 到临时 git 仓
#   - 故意引入 bug（add 返回 a-b）
#   - 跑 PASS_CMD → FAIL
#   - 修复 bug
#   - 跑 PASS_CMD → PASS
#
# 用法：bash run-fixture.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS="${SCRIPT_DIR}/../../scripts"
SAMPLE="${SCRIPT_DIR}/sample-project"

[ -d "$SAMPLE" ] || { echo "❌ FAIL: sample-project 不存在: $SAMPLE" >&2; exit 1; }
[ -f "$SCRIPTS/setup-builder-loop.sh" ] || { echo "❌ FAIL: setup-builder-loop.sh 不存在" >&2; exit 1; }
[ -f "$SCRIPTS/run-pass-cmd.sh" ] || { echo "❌ FAIL: run-pass-cmd.sh 不存在" >&2; exit 1; }

TMPDIR="$(mktemp -d -t builder-loop-e2e-XXXXXX)"
trap 'rm -rf "$TMPDIR"' EXIT

# 构建临时仓
cp -r "$SAMPLE"/* "$TMPDIR/"
cp -r "$SAMPLE"/.claude "$TMPDIR/"
cd "$TMPDIR"
git init -q
git -c core.hooksPath=/dev/null add -A
git -c core.hooksPath=/dev/null commit -q -m "init sample project"

# === 阶段 1：验证正常状态 PASS ===
echo "--- 阶段 1：验证正常状态 PASS ---"
if ! bash "$SCRIPTS/run-pass-cmd.sh" "$TMPDIR" 0 > /dev/null 2>&1; then
  echo "❌ FAIL: 正常 sample-project PASS_CMD 失败（fixture 自身有问题）" >&2
  exit 1
fi
echo "✓ 阶段 1 通过：sample-project 正常状态 PASS"

# === 阶段 2：引入 bug ===
echo "--- 阶段 2：引入 bug ---"
sed -i 's/return a + b/return a - b/' src/foo.py
git -c core.hooksPath=/dev/null add -A
git -c core.hooksPath=/dev/null commit -q -m "introduce bug"

# === 阶段 3：setup + 跑 PASS_CMD，期望 FAIL ===
echo "--- 阶段 3：setup + PASS_CMD 期望 FAIL ---"
if ! bash "$SCRIPTS/setup-builder-loop.sh" "e2e fixture test" > /dev/null 2>&1; then
  echo "❌ FAIL: setup-builder-loop.sh 失败" >&2
  exit 1
fi

STATE=".claude/builder-loop.local.md"
[ -f "$STATE" ] || { echo "❌ FAIL: state file not created" >&2; exit 1; }

if bash "$SCRIPTS/run-pass-cmd.sh" "$TMPDIR" 1 > /dev/null 2>&1; then
  echo "❌ FAIL: PASS_CMD 期望失败但成功了" >&2
  exit 1
fi
echo "✓ 阶段 3 通过：PASS_CMD 正确失败（bug 在 src/foo.py）"

# === 阶段 4：修复 bug + 跑 PASS_CMD，期望 PASS ===
echo "--- 阶段 4：修复 bug + PASS_CMD 期望 PASS ---"
sed -i 's/return a - b/return a + b/' src/foo.py

if ! bash "$SCRIPTS/run-pass-cmd.sh" "$TMPDIR" 2 > /dev/null 2>&1; then
  echo "❌ FAIL: PASS_CMD 期望成功但失败了" >&2
  exit 1
fi
echo "✓ 阶段 4 通过：PASS_CMD 正确成功（bug 已修复）"

echo ""
echo "✅ PASS: e2e 完整循环验证通过（setup → FAIL → fix → PASS）"
exit 0
