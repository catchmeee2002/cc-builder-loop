#!/usr/bin/env bash
# test-bare-reviewer-gate.sh — E2E：bare 模式 reviewer-as-gate 完整生命周期
#
# V4.1 新增。验证 bare 模式与 worktree 模式行为对齐：
#   Case 1: bare PASS → commit → phase=passed_pending_review → reviewer_pending 段完整
#   Case 2: bare L1 自愈 — phase=passed_pending_review 下出现 dirty → 自愈回 active
#   Case 3: bare reviewer pass → merge-and-cleanup bare 分支 → state 已删
#   Case 4: loop-commit.sh NOOP — project_root 无 dirty → NOOP（不报错）
#
# 用法：bash test-bare-reviewer-gate.sh
# 退出码：0=全部通过 / 1=有失败

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "bare-reviewer-gate"

LOOP_COMMIT="${HARNESS_REPO_ROOT}/skills/builder-loop/scripts/loop-commit.sh"
assert_file_exists "loop-commit.sh 存在" "$LOOP_COMMIT"
assert_file_exists "stop hook 存在" "$HARNESS_HOOK"

# ============================================================
# Case 1: bare PASS → commit → phase=passed_pending_review
# ============================================================
section "Case 1: bare PASS → commit → reviewer_pending 段"
env1=$(create_test_env --slug "__main__" --pass-cmd "true")
cat > "$env1/.claude/loop.yml" <<'YMLEOF'
pass_cmd:
  - stage: smoke
    cmd: "true"
    timeout: 10
max_iterations: 3
layout:
  source_dirs: [src]
worktree:
  enabled: false
YMLEOF
mkdir -p "$env1/src"
echo "new_code" > "$env1/src/app.py"

result1=$(run_hook "$env1")
sf1=$(state_file "$env1" "__main__")

assert_ec "Case 1 EC=2" "$result1" 2
assert_stderr_contains "Case 1 PASS 消息" "$result1" "phase=passed_pending_review"
assert_file_exists "Case 1 state 保留" "$sf1"

phase1=$(read_state_field "$sf1" "phase")
assert "Case 1 phase=passed_pending_review" "[ '$phase1' = 'passed_pending_review' ]"

lih1=$(read_state_field "$sf1" "last_iter_head")
assert "Case 1 last_iter_head 非空" "[ -n '$lih1' ]"

rp_psh=$(grep 'pass_start_head' "$sf1" 2>/dev/null | head -1 || true)
assert "Case 1 reviewer_pending.pass_start_head 存在" "[ -n '$rp_psh' ]"

slug_diff="$env1/.claude/reviewer-diff-__main__.txt"
assert_file_exists "Case 1 reviewer-diff 文件" "$slug_diff"

# ============================================================
# Case 2: bare L1 自愈 — passed_pending_review + dirty → active
# ============================================================
section "Case 2: bare L1 自愈"
env2=$(create_test_env --slug "__main__" --phase "passed_pending_review" --pass-cmd "false")
cat > "$env2/.claude/loop.yml" <<'YMLEOF'
pass_cmd:
  - stage: check
    cmd: "false"
    timeout: 10
worktree:
  enabled: false
YMLEOF
# 在主仓制造 dirty（模拟 builder 修复 reviewer 反馈）
echo "fix" > "$env2/fix.py"

result2=$(run_hook "$env2")
sf2=$(state_file "$env2" "__main__")

phase2=$(read_state_field "$sf2" "phase")
assert "Case 2 phase 自愈回 active（PASS_CMD 失败所以停在 active）" "[ '$phase2' = 'active' ]"
assert_stderr_contains "Case 2 stderr 含自愈信息" "$result2" "自愈"

# ============================================================
# Case 3: merge-and-cleanup bare 分支 — stash drop + rm state
# ============================================================
section "Case 3: merge-and-cleanup bare 分支"
env3=$(create_test_env --slug "__main__")
sf3=$(state_file "$env3" "__main__")

merge_result3=$(run_merge_cleanup "$sf3")
merge_ec3=$(result_ec "$merge_result3")
merge_last3=$(result_last "$merge_result3")

assert "Case 3 EC=0" "[ '$merge_ec3' -eq 0 ]"
assert "Case 3 输出 MERGED __main__" "echo '$merge_last3' | grep -q 'MERGED __main__'"
assert_file_missing "Case 3 state 已删" "$sf3"

# ============================================================
# Case 4: loop-commit.sh NOOP — project_root 无 dirty
# ============================================================
section "Case 4: loop-commit.sh NOOP（无 dirty）"
env4=$(create_test_env --slug "__main__")
sf4=$(state_file "$env4" "__main__")

commit_handle=$(mktemp -d -t harness-result-XXXXXX)
ec4=0
bash "$LOOP_COMMIT" "$sf4" >"$commit_handle/stdout" 2>"$commit_handle/stderr" || ec4=$?
commit_last4=$(tail -1 "$commit_handle/stdout" 2>/dev/null || echo "")

assert "Case 4 EC=0" "[ '$ec4' -eq 0 ]"
assert "Case 4 输出 NOOP" "[ '$commit_last4' = 'NOOP' ]"
rm -rf "$commit_handle"

harness_report
