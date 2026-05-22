#!/usr/bin/env bash
# test-stop-hook-slug-binding.sh — V3.2 stop hook 读 local.md slug 精确绑定
#
# A1: local.md + active state → EC=2
# A2: 无 local.md → exit 0
# A3: local.md → 不存在的 state → exit 0
# D1: loop.yml + dirty + 无 local.md → exit 0（不兜底激活）

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "V3.2 stop hook slug 绑定"

section "A1: local.md + active state → EC=2"
env1=$(create_test_env --slug "slug-a1" --dirty "README.md")
r1=$(run_hook "$env1")
assert_ec "A1 hook 正常操作" "$r1" 2

section "A2: 无 local.md → exit 0"
env2=$(create_test_env --slug "slug-a2" --no-local-md --dirty "README.md")
r2=$(run_hook "$env2")
assert_ec "A2 放行" "$r2" 0

section "A3: local.md → 不存在的 state → exit 0"
env3=$(create_test_env --slug "slug-a3" --no-state)
cat > "$env3/.claude/builder-loop.local.md" <<'EOF'
slug: "nonexistent-slug"
worktree_path: ""
EOF
r3=$(run_hook "$env3")
assert_ec "A3 放行" "$r3" 0

section "D1: loop.yml + dirty + 无 local.md → 不兜底激活"
env4=$(create_test_env --slug "slug-d1" --no-local-md --no-state --dirty "README.md")
r4=$(run_hook "$env4")
assert_ec "D1 放行" "$r4" 0
assert_file_missing "D1 state 未被自动创建" "$env4/.claude/builder-loop/state"

harness_report
