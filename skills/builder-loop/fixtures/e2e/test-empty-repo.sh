#!/usr/bin/env bash
# test-empty-repo.sh — 验证 setup-builder-loop.sh 在空仓不被 set -e 杀
#
# 场景：
#   - 临时 git 仓，无 commit、无 src/lib/app/pkg、无 tests/test/spec
#   - 仅含最小 .claude/loop.yml
#
# 期望：
#   - setup-builder-loop.sh exit 0
#   - state file .claude/builder-loop/state/__main__.yml 被生成（bare 模式，无 git commit 走 no-git 路径）
#   - source_dirs / test_dirs 字段值为空字符串（不报错）

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "empty-repo"

section "初始化空仓（无 commit、无标准目录结构）"
TMP="$(mktemp -d -t builder-loop-empty-XXXXXX)"
_HARNESS_TMPDIRS+=("$TMP")
(
  cd "$TMP"
  git init -q
  mkdir -p .claude
  cat > .claude/loop.yml <<'EOF'
pass_cmd:
  - stage: test
    cmd: "true"
EOF
)

section "setup-builder-loop.sh exit 0"
SETUP_EC=0
(cd "$TMP" && bash "$HARNESS_SETUP" "test-empty-repo-task" >/dev/null 2>&1) || SETUP_EC=$?
assert "setup exit 0" "[ '$SETUP_EC' -eq 0 ]"

STATE_FILE="$TMP/.claude/builder-loop/state/__main__.yml"
assert_file_exists "state file 已生成" "$STATE_FILE"

section "source_dirs / test_dirs 字段为空字符串"
src_val="$(read_state_field "$STATE_FILE" source_dirs)"
test_val="$(read_state_field "$STATE_FILE" test_dirs)"
assert "source_dirs 含空引号" "grep -q '\"\"' <<< '$(grep -E '^source_dirs:' "$STATE_FILE")'"
assert "test_dirs 含空引号" "grep -q '\"\"' <<< '$(grep -E '^test_dirs:' "$STATE_FILE")'"

harness_report
