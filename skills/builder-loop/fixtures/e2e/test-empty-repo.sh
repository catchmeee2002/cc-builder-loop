#!/usr/bin/env bash
# test-empty-repo.sh — 验证 setup-builder-loop.sh 在空仓不被 set -e 杀
#
# 场景：
#   - 临时 git 仓，无 commit、无 src/lib/app/pkg、无 tests/test/spec
#   - 仅含最小 .claude/loop.yml
#
# 期望：
#   - setup-builder-loop.sh exit 0
#   - state file .claude/builder-loop.local.md 被生成
#   - source_dirs / test_dirs 字段值为空字符串（不报错）
#
# 用法：bash test-empty-repo.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETUP_SCRIPT="${SCRIPT_DIR}/../../scripts/setup-builder-loop.sh"

if [ ! -f "$SETUP_SCRIPT" ]; then
  echo "❌ FAIL: setup-builder-loop.sh 不存在: $SETUP_SCRIPT" >&2
  exit 1
fi

TMPDIR="$(mktemp -d -t builder-loop-empty-XXXXXX)"
trap 'rm -rf "$TMPDIR"' EXIT

cd "$TMPDIR"
git init -q
mkdir -p .claude
cat > .claude/loop.yml <<'EOF'
pass_cmd:
  - stage: test
    cmd: "true"
EOF

# 跑 setup-builder-loop.sh，捕获 exit code
if ! bash "$SETUP_SCRIPT" "test-empty-repo-task" >/tmp/setup-empty.log 2>&1; then
  ec=$?
  echo "❌ FAIL: setup-builder-loop.sh exit=$ec（期望 0）" >&2
  echo "--- 输出 ---" >&2
  cat /tmp/setup-empty.log >&2
  exit 1
fi

STATE_FILE=".claude/builder-loop.local.md"
if [ ! -f "$STATE_FILE" ]; then
  echo "❌ FAIL: state file 未生成: $STATE_FILE" >&2
  exit 1
fi

# 校验 source_dirs / test_dirs 字段为空字符串（不存在该字段也算失败）
src_line=$(grep -E '^source_dirs:' "$STATE_FILE" || echo "")
test_line=$(grep -E '^test_dirs:' "$STATE_FILE" || echo "")

if [ -z "$src_line" ] || [ -z "$test_line" ]; then
  echo "❌ FAIL: state file 缺 source_dirs/test_dirs 字段" >&2
  cat "$STATE_FILE" >&2
  exit 1
fi

# 字段值期望为 "" （引号内为空）
if ! echo "$src_line" | grep -q '""'; then
  echo "❌ FAIL: source_dirs 期望空字符串，实际：$src_line" >&2
  exit 1
fi
if ! echo "$test_line" | grep -q '""'; then
  echo "❌ FAIL: test_dirs 期望空字符串，实际：$test_line" >&2
  exit 1
fi

echo "✅ PASS: setup-builder-loop.sh 在空仓正常完成，state file 字段空值符合预期"
exit 0
