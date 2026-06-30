#!/usr/bin/env bash
# test-early-stop-tampering.sh — V4.9 tampering 检测已迁移到 reviewer 语义判定
#
# 验证 early-stop-check.sh 不再因测试文件变更而输出 STOP：
#   Case 1: test_dirs 未配置 → CONTINUE
#   Case 2: 删测试文件 + 加 skip/xfail → CONTINUE（不再早停）
#   Case 3: 删 3 个测试文件 → CONTINUE（不再早停）
#   Case 4: 删 2 个测试文件 → CONTINUE
#   Case 5: 只改注释/格式 → CONTINUE
#
# 用法：bash test-early-stop-tampering.sh

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "early-stop-tampering"

EARLY_STOP="${HARNESS_REPO_ROOT}/skills/builder-loop/scripts/early-stop-check.sh"

# ==================================================================
# 辅助：建临时 git 仓，含 tests/ 和 src/ 目录 + 初始文件
# 返回：仓库根路径（stdout）
# ==================================================================
mk_test_repo() {
  local dir
  dir="$(mktemp -d -t "tampering-XXXXXX")"
  _HARNESS_TMPDIRS+=("$dir")
  (
    cd "$dir"
    git init -q
    git config user.email "harness@test.local"
    git config user.name "harness"
    mkdir -p tests src
    cat > tests/test_alpha.py <<'PY'
def test_add():
    assert 1 + 1 == 2

def test_sub():
    assert 3 - 1 == 2
PY
    cat > tests/test_beta.py <<'PY'
def test_mul():
    assert 2 * 3 == 6
PY
    cat > tests/test_gamma.py <<'PY'
def test_div():
    assert 10 / 2 == 5
PY
    cat > src/app.py <<'PY'
def hello():
    return "hello"
PY
    git add -A
    git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Init test files"
  )
  echo "$dir"
}

# ==================================================================
# 辅助：写 state 文件，返回 state 文件路径
# 参数：project_root  test_dirs（可为空字符串）
# ==================================================================
mk_state_file() {
  local project_root="$1"
  local test_dirs="$2"
  local state_dir
  state_dir="$(mktemp -d -t "state-XXXXXX")"
  _HARNESS_TMPDIRS+=("$state_dir")
  local state_file="${state_dir}/state.yml"
  cat > "$state_file" <<STEOF
iter: 0
max_iter: 5
project_root: "${project_root}"
test_dirs: "${test_dirs}"
last_error_hash: ""
last_error_count: 0
STEOF
  echo "$state_file"
}

# ==================================================================
# 辅助：写空日志文件（不含 FAILED/ERROR，确保 CUR_COUNT=0）
# ==================================================================
mk_empty_log() {
  local f
  f="$(mktemp -t "log-XXXXXX")"
  _HARNESS_TMPDIRS+=("$f")
  printf '' > "$f"
  echo "$f"
}

# ==============================================================
section "Case 1: test_dirs 未配置 → 跳过第 5 段 → CONTINUE"
# ==============================================================
# 场景：state 里 test_dirs="" 时，脚本第 5 段 [ -n "$TEST_DIRS_CSV" ] 为假，
#        直接输出 CONTINUE（测试 A+D 重构的 guard 条件）

repo1="$(mk_test_repo)"

# 工作区有测试文件改动，但 test_dirs 未配置，第 5 段不应触发
rm "$repo1/tests/test_alpha.py"
rm "$repo1/tests/test_beta.py"
rm "$repo1/tests/test_gamma.py"

state1="$(mk_state_file "$repo1" "")"
log1="$(mk_empty_log)"

out1="$(bash "$EARLY_STOP" "$state1" "$log1" 2>/dev/null)"
assert "Case 1 test_dirs 空 → 不触发 tampering 检测 → 输出 CONTINUE" \
  "echo '$out1' | grep -q '^CONTINUE'"

# ==============================================================
section "Case 2: 删 1 个测试文件 + 加 2 行 skip/xfail 标记 → CONTINUE（tampering 迁移到 reviewer）"
# ==============================================================
# 场景：删 test_gamma.py（信号+1），在 test_beta.py 加 pytest.mark.skip（信号+1），
#        在 test_alpha.py 加 pytest.mark.xfail（信号+1），总计 3 → 触发早停

repo2="$(mk_test_repo)"

# 删除 1 个测试文件
rm "$repo2/tests/test_gamma.py"

# 加 skip 标记（工作区改动，不 commit）
cat > "$repo2/tests/test_beta.py" <<'PY'
import pytest

@pytest.mark.skip(reason="builder skipped this")
def test_mul():
    assert 2 * 3 == 6
PY

# 加 xfail 标记
cat > "$repo2/tests/test_alpha.py" <<'PY'
import pytest

@pytest.mark.xfail
def test_add():
    assert 1 + 1 == 2

def test_sub():
    assert 3 - 1 == 2
PY

state2="$(mk_state_file "$repo2" "tests")"
log2="$(mk_empty_log)"

out2="$(bash "$EARLY_STOP" "$state2" "$log2" 2>/dev/null)"
assert "Case 2 测试变更不再触发早停 → 输出 CONTINUE" \
  "echo '$out2' | grep -q '^CONTINUE'"

# ==============================================================
section "Case 3: 删 3 个测试文件 → CONTINUE（tampering 迁移到 reviewer）"
# ==============================================================

repo3="$(mk_test_repo)"

rm "$repo3/tests/test_alpha.py"
rm "$repo3/tests/test_beta.py"
rm "$repo3/tests/test_gamma.py"

state3="$(mk_state_file "$repo3" "tests")"
log3="$(mk_empty_log)"

out3="$(bash "$EARLY_STOP" "$state3" "$log3" 2>/dev/null)"
assert "Case 3 删 3 个文件不再触发早停 → 输出 CONTINUE" \
  "echo '$out3' | grep -q '^CONTINUE'"

# ==============================================================
section "Case 4: 只删 2 个测试文件，信号=2 → 不触发早停 → CONTINUE"
# ==============================================================

repo4="$(mk_test_repo)"

rm "$repo4/tests/test_alpha.py"
rm "$repo4/tests/test_beta.py"

state4="$(mk_state_file "$repo4" "tests")"
log4="$(mk_empty_log)"

out4="$(bash "$EARLY_STOP" "$state4" "$log4" 2>/dev/null)"
assert "Case 4 信号=2 未达阈值 → 输出 CONTINUE" \
  "echo '$out4' | grep -q '^CONTINUE'"

# ==============================================================
section "Case 5: 只改注释/格式（diff 有内容但无 tampering 信号）→ CONTINUE"
# ==============================================================

repo5="$(mk_test_repo)"

# 只在 test_alpha.py 加一行注释（diff 有内容，但不含 tampering 信号）
cat > "$repo5/tests/test_alpha.py" <<'PY'
def test_add():
    # updated by builder: add tolerance note
    assert 1 + 1 == 2

def test_sub():
    assert 3 - 1 == 2
PY

state5="$(mk_state_file "$repo5" "tests")"
log5="$(mk_empty_log)"

out5="$(bash "$EARLY_STOP" "$state5" "$log5" 2>/dev/null)"
assert "Case 5 只改注释不弱化断言 → 输出 CONTINUE" \
  "echo '$out5' | grep -q '^CONTINUE'"

harness_report
