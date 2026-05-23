#!/usr/bin/env bash
# test-worktree-commit-only.sh — V3.0 worktree-commit-only.sh 单元测
#
# Case A: worktree 有 dirty 改动 → COMMIT_DONE <new_head> + worktree HEAD 推进
# Case B: worktree 干净 → NOOP
# Case C: bare 模式（worktree_path 空）→ NOOP
# Case D: state 文件不存在 → ERROR exit 3

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "worktree-commit-only"

COMMIT_ONLY="${HARNESS_REPO_ROOT}/skills/builder-loop/scripts/worktree-commit-only.sh"

# ============================================================
# Case A: worktree dirty → COMMIT_DONE
# ============================================================
section "Case A: worktree dirty → COMMIT_DONE"
envA=$(create_test_env --slug "commit-a" --worktree --phase active --wt-dirty "feature.txt")
STATE_A="$(state_file "$envA" "commit-a")"
WT_A="$envA/.claude/worktrees/commit-a"
HEAD_BEFORE_A="$(git -C "$WT_A" rev-parse --short HEAD)"

OUT_A="$(bash "$COMMIT_ONLY" "$STATE_A" 2>&1 || true)"
LAST_A="$(echo "$OUT_A" | tail -1)"
ACTION_A="$(echo "$LAST_A" | awk '{print $1}')"
NEW_HEAD_A="$(echo "$LAST_A" | awk '{print $2}')"

assert "Case A 输出 COMMIT_DONE" "[ '$ACTION_A' = 'COMMIT_DONE' ]"
assert "Case A new_head 非空" "[ -n '$NEW_HEAD_A' ]"
assert "Case A worktree HEAD 推进" \
  "[ \"\$(git -C '$WT_A' rev-parse --short HEAD)\" != '$HEAD_BEFORE_A' ]"
assert "Case A 主仓 HEAD 未变" \
  "[ \"\$(git -C '$envA' rev-parse --short HEAD)\" = '$HEAD_BEFORE_A' ]"
assert_file_exists "Case A worktree 仍存在" "$WT_A"

# ============================================================
# Case B: worktree 干净 → NOOP
# ============================================================
section "Case B: worktree 干净 → NOOP"
envB=$(create_test_env --slug "commit-b" --worktree --phase active)
STATE_B="$(state_file "$envB" "commit-b")"

OUT_B="$(bash "$COMMIT_ONLY" "$STATE_B" 2>&1 || true)"
LAST_B="$(echo "$OUT_B" | tail -1)"
assert "Case B 输出 NOOP" "[ '$LAST_B' = 'NOOP' ]"

# ============================================================
# Case C: bare 模式 → NOOP
# ============================================================
section "Case C: bare 模式 → NOOP"
envC=$(create_test_env --slug "__main__" --phase active)
STATE_C="$(state_file "$envC" "__main__")"

OUT_C="$(bash "$COMMIT_ONLY" "$STATE_C" 2>&1 || true)"
LAST_C="$(echo "$OUT_C" | tail -1)"
assert "Case C bare 模式输出 NOOP" "[ '$LAST_C' = 'NOOP' ]"

# ============================================================
# Case D: state 不存在 → ERROR exit 3
# ============================================================
section "Case D: state 不存在 → ERROR"
TMPD="$(mktemp -d)"
_HARNESS_TMPDIRS+=("$TMPD")
EC_D=0
OUT_D="$(bash "$COMMIT_ONLY" "$TMPD/nonexistent.yml" 2>&1)" || EC_D=$?
LAST_D="$(echo "$OUT_D" | tail -1)"

assert "Case D 输出含 ERROR" "echo '$LAST_D' | grep -q '^ERROR'"
assert "Case D exit 3" "[ '$EC_D' -eq 3 ]"

harness_report
