#!/usr/bin/env bash
# test-stop-hook-slug-binding.sh — V3.4 stop hook CWD→state 定位
#
# A1: active state → locate-state.sh 找到 → EC=2
# A2: state 存在但无 local.md → 仍能找到（V3.4 不依赖 local.md）→ EC=2
# A3: 无 state 文件 → exit 0
# D1: loop.yml + dirty + 无 state → exit 0（不兜底激活）

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "V3.4 stop hook CWD→state 定位"

section "A1: active state → EC=2"
env1=$(create_test_env --slug "slug-a1" --dirty "README.md")
r1=$(run_hook "$env1")
assert_ec "A1 hook 正常操作" "$r1" 2

section "A2: state 存在 + 无 local.md → EC=2（V3.4 不依赖 local.md）"
env2=$(create_test_env --slug "slug-a2" --dirty "README.md")
r2=$(run_hook "$env2")
assert_ec "A2 locate-state.sh 找到 state" "$r2" 2

section "A3: 无 state 文件 → exit 0"
env3=$(create_test_env --slug "slug-a3" --no-state)
r3=$(run_hook "$env3")
assert_ec "A3 放行" "$r3" 0

section "D1: loop.yml + dirty + 无 state → 不兜底激活"
env4=$(create_test_env --slug "slug-d1" --no-state --dirty "README.md")
r4=$(run_hook "$env4")
assert_ec "D1 放行" "$r4" 0
assert_file_missing "D1 state 未被自动创建" "$env4/.claude/builder-loop/state"

harness_report
