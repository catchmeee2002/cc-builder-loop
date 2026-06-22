#!/usr/bin/env bash
# test-e2e-default-bare.sh — E2E：plan 含 e2e-cases 标签时 builder 应传 --no-worktree
#
# V4.1 新增。验证 e2e 行为测试场景自动选择 bare 模式：
#   Case 1: setup --no-worktree → state.worktree_mode=bare + slug=__main__
#   Case 2: setup 不传 --no-worktree（对照组）→ state.worktree_mode!=bare
#
# 注意：本 fixture 只测 setup 脚本接受 --no-worktree 并正确生成 bare state，
#       不测 builder.md 的 e2e-cases 标签检测逻辑（那是 LLM prompt 层面）。
#
# 用法：bash test-e2e-default-bare.sh
# 退出码：0=全部通过 / 1=有失败

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "e2e-default-bare"

SETUP_SCRIPT="${HARNESS_REPO_ROOT}/skills/builder-loop/scripts/setup-builder-loop.sh"
assert_file_exists "setup 脚本存在" "$SETUP_SCRIPT"

# ============================================================
# Case 1: --no-worktree → bare state
# ============================================================
section "Case 1: setup --no-worktree → bare state"
env1=$(create_test_env --slug "__main__" --no-state)
cat > "$env1/.claude/loop.yml" <<'YMLEOF'
pass_cmd:
  - stage: smoke
    cmd: "true"
    timeout: 10
worktree:
  enabled: true
YMLEOF

setup_handle1=$(mktemp -d -t harness-result-XXXXXX)
ec1=0
cd "$env1" && bash "$SETUP_SCRIPT" --no-worktree "e2e test task" \
  >"$setup_handle1/stdout" 2>"$setup_handle1/stderr" || ec1=$?

assert "Case 1 setup EC=0" "[ '$ec1' -eq 0 ]"

sf1="$env1/.claude/builder-loop/state/__main__.yml"
assert_file_exists "Case 1 state 文件存在" "$sf1"

wt_mode1=$(read_state_field "$sf1" "worktree_mode")
assert "Case 1 worktree_mode=bare" "[ '$wt_mode1' = 'bare' ]"

slug1=$(read_state_field "$sf1" "slug")
assert "Case 1 slug=__main__" "[ '$slug1' = '__main__' ]"

wt_path1=$(read_state_field "$sf1" "worktree_path")
assert "Case 1 worktree_path 为空" "[ -z '$wt_path1' ]"

rm -rf "$setup_handle1"

# ============================================================
# Case 2: 对照组 — 不传 --no-worktree（worktree.enabled=true）→ worktree state
# ============================================================
section "Case 2: 对照组 — 默认 worktree state"
env2=$(create_test_env --slug "test-ctrl" --no-state)
cat > "$env2/.claude/loop.yml" <<'YMLEOF'
pass_cmd:
  - stage: smoke
    cmd: "true"
    timeout: 10
worktree:
  enabled: true
YMLEOF

setup_handle2=$(mktemp -d -t harness-result-XXXXXX)
ec2=0
cd "$env2" && bash "$SETUP_SCRIPT" "control task" \
  >"$setup_handle2/stdout" 2>"$setup_handle2/stderr" || ec2=$?

assert "Case 2 setup EC=0" "[ '$ec2' -eq 0 ]"

# 找到 state 文件（slug 含时间戳，用 glob）
sf2=$(find "$env2/.claude/builder-loop/state/" -name "*.yml" -not -name "__main__.yml" 2>/dev/null | head -1)
assert "Case 2 state 文件存在" "[ -n '$sf2' ] && [ -f '$sf2' ]"

if [ -n "$sf2" ] && [ -f "$sf2" ]; then
  wt_mode2=$(read_state_field "$sf2" "worktree_mode")
  assert "Case 2 worktree_mode!=bare" "[ '$wt_mode2' != 'bare' ]"

  wt_path2=$(read_state_field "$sf2" "worktree_path")
  assert "Case 2 worktree_path 非空" "[ -n '$wt_path2' ]"
fi

rm -rf "$setup_handle2"

harness_report
