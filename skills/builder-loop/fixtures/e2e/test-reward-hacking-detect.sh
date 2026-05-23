#!/usr/bin/env bash
# test-reward-hacking-detect.sh — V2.3 reward hacking 关键词检测
#
# 覆盖：
#   配置 lint：judge-system.md / run-judge-agent.sh / builder-loop-stop.sh 含必要段
#   B1: .claude/loop.yml 加 --reruns 2 → 文件 + 内容双命中
#   B2: pyproject.toml 加 xfail_strict → 命中
#   B3: tests/test_foo.py 加 @pytest.mark.flaky → 命中
#   B4: src/foo.py 普通实现改动 → 不命中（控制组）
#
# 注：本 fixture 不真实跑 judge agent（避免依赖 ANTHROPIC API 凭证）。
#     检测算法核心是 grep 关键词清单，本 fixture 复用相同清单做单元验证。
#
# 用法：bash test-reward-hacking-detect.sh

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "reward-hacking-detect"

JUDGE_SCRIPT="${HARNESS_REPO_ROOT}/skills/builder-loop/scripts/run-judge-agent.sh"
JUDGE_PROMPT="${HARNESS_REPO_ROOT}/skills/builder-loop/prompts/judge-system.md"
STOP_HOOK="${HARNESS_REPO_ROOT}/scripts/builder-loop-stop.sh"

# ---- 配置 lint ----
section "配置 lint：关键代码段未被误删"
assert "judge-system.md 存在" "[ -f '$JUDGE_PROMPT' ]"
assert "judge-system.md 含 V2.3 reward hacking 段" "grep -q 'V2.3 reward hacking' '$JUDGE_PROMPT'"
assert "judge-system.md 含 suspected_reward_hack 关键字" "grep -q 'suspected_reward_hack' '$JUDGE_PROMPT'"
assert "judge-system.md 含三选项关键字 quarantine" "grep -q 'quarantine' '$JUDGE_PROMPT'"

assert "run-judge-agent.sh 存在" "[ -f '$JUDGE_SCRIPT' ]"
assert "run-judge-agent.sh 含 reward_hacking_detection 配置字段" "grep -q 'reward_hacking_detection' '$JUDGE_SCRIPT'"
assert "run-judge-agent.sh 含 RH_FILE_PATTERN" "grep -q 'RH_FILE_PATTERN' '$JUDGE_SCRIPT'"
assert "run-judge-agent.sh 含 RH_DIFF_PATTERN" "grep -q 'RH_DIFF_PATTERN' '$JUDGE_SCRIPT'"
assert "run-judge-agent.sh 写 reason=suspected_reward_hack" "grep -q 'suspected_reward_hack' '$JUDGE_SCRIPT'"

assert "stop hook 存在" "[ -f '$STOP_HOOK' ]"
assert "stop hook 含 reward-hack-guard 注入文案" "grep -q 'reward-hack-guard' '$STOP_HOOK'"
assert "stop hook 含三选项内容（quarantine）" "grep -q 'quarantine' '$STOP_HOOK'"
assert "stop hook 注入文案识别 suspected_reward_hack reason" "grep -q 'suspected_reward_hack' '$STOP_HOOK'"

# ---- 关键词检测算法验证 ----
# 使用与 run-judge-agent.sh 完全一致的两个正则
section "关键词检测算法验证（与 run-judge-agent.sh 同清单）"
RH_FILE_PATTERN='\.claude/loop\.yml$|pyproject\.toml$|pytest\.ini$|setup\.cfg$|conftest\.py$|^tests?/.*\.py$|/tests?/.*\.py$'
RH_DIFF_PATTERN='--reruns|@pytest\.mark\.flaky|@flaky|xfail|pytest\.skip|@unittest\.skip|-k +['"'"'"]not '

# B1: .claude/loop.yml 加 --reruns 2
section "B1: .claude/loop.yml 加 --reruns 2"
B1_FILE='.claude/loop.yml'
B1_DIFF='+  - { stage: "test", cmd: "pytest --reruns 2 tests/", timeout: 600 }'
assert "B1 文件命中（.claude/loop.yml）" "echo '$B1_FILE' | grep -qE '$RH_FILE_PATTERN'"
assert "B1 内容命中（--reruns）" "echo '$B1_DIFF' | grep -qE -- \"\$RH_DIFF_PATTERN\""

# B2: pyproject.toml 加 xfail
section "B2: pyproject.toml 加 xfail"
B2_FILE='pyproject.toml'
B2_DIFF='+xfail_strict = true'
assert "B2 文件命中（pyproject.toml）" "echo '$B2_FILE' | grep -qE '$RH_FILE_PATTERN'"
assert "B2 内容命中（xfail）" "echo '$B2_DIFF' | grep -qE -- \"\$RH_DIFF_PATTERN\""

# B3: tests/test_foo.py 加 @pytest.mark.flaky
section "B3: tests/test_foo.py 加 @pytest.mark.flaky"
B3_FILE='tests/test_foo.py'
B3_DIFF='+@pytest.mark.flaky(reruns=3)'
assert "B3 文件命中（tests/...py）" "echo '$B3_FILE' | grep -qE '$RH_FILE_PATTERN'"
assert "B3 内容命中（@pytest.mark.flaky）" "echo '$B3_DIFF' | grep -qE -- \"\$RH_DIFF_PATTERN\""
B3B_FILE='backend/tests/test_bar.py'
assert "B3 嵌套 tests 路径命中（backend/tests/...py）" "echo '$B3B_FILE' | grep -qE '$RH_FILE_PATTERN'"

# B5: -k 'not slow' 类 pytest 排除
section "B5: -k 'not slow' 排除模式"
B5_FILE='conftest.py'
B5_DIFF="+pytest_args = \"-k 'not slow'\""
assert "B5 文件命中（conftest.py）" "echo '$B5_FILE' | grep -qE -- \"\$RH_FILE_PATTERN\""
assert "B5 内容命中（-k 'not X'）" "echo \"\$B5_DIFF\" | grep -qE -- \"\$RH_DIFF_PATTERN\""

# B4: 普通实现代码改动（控制组）
section "B4: src/foo.py 普通实现改动（控制组）"
B4_FILE='src/foo.py'
B4_DIFF='+def hello(): return 42'
assert "B4 文件不命中（src/）" "! echo '$B4_FILE' | grep -qE '$RH_FILE_PATTERN'"
assert "B4 内容也不含 hacking 关键词" "! echo '$B4_DIFF' | grep -qE -- \"\$RH_DIFF_PATTERN\""
B4B_FILE='tests/test_clean.py'
B4B_DIFF='+def test_simple(): assert 1 + 1 == 2'
assert "B4 仅文件命中但内容无关键词" "echo '$B4B_FILE' | grep -qE '$RH_FILE_PATTERN' && ! echo '$B4B_DIFF' | grep -qE -- \"\$RH_DIFF_PATTERN\""

harness_report
