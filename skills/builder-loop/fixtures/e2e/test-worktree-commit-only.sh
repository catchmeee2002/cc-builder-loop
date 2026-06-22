#!/usr/bin/env bash
# test-loop-commit.sh — loop-commit.sh 单元测试（V4.1 替代 worktree-commit-only.sh）
#
# Case A: worktree dirty → COMMIT_DONE + worktree HEAD 推进，主仓 HEAD 不变
# Case B: worktree 干净 → NOOP
# Case C: bare 模式干净 → NOOP
# Case D: bare 模式 dirty → COMMIT_DONE + 主仓 HEAD 推进
# Case E: state 不存在 → ERROR exit 3

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "loop-commit"

LOOP_COMMIT="${HARNESS_REPO_ROOT}/skills/builder-loop/scripts/loop-commit.sh"

# ============================================================
# Case A: worktree dirty → COMMIT_DONE
# ============================================================
section "Case A: worktree dirty → COMMIT_DONE"
envA=$(create_test_env --slug "commit-a" --worktree --phase active --wt-dirty "feature.txt")
STATE_A="$(state_file "$envA" "commit-a")"
WT_A="$envA/.claude/worktrees/commit-a"
HEAD_BEFORE_A="$(git -C "$WT_A" rev-parse --short HEAD)"

OUT_A="$(bash "$LOOP_COMMIT" "$STATE_A" 2>&1 || true)"
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

OUT_B="$(bash "$LOOP_COMMIT" "$STATE_B" 2>&1 || true)"
LAST_B="$(echo "$OUT_B" | tail -1)"
assert "Case B 输出 NOOP" "[ '$LAST_B' = 'NOOP' ]"

# ============================================================
# Case C: bare 模式干净 → NOOP
# ============================================================
section "Case C: bare 模式干净 → NOOP"
envC=$(create_test_env --slug "__main__" --phase active)
STATE_C="$(state_file "$envC" "__main__")"

OUT_C="$(bash "$LOOP_COMMIT" "$STATE_C" 2>&1 || true)"
LAST_C="$(echo "$OUT_C" | tail -1)"
assert "Case C bare 干净输出 NOOP" "[ '$LAST_C' = 'NOOP' ]"

# ============================================================
# Case D: bare 模式 dirty → COMMIT_DONE
# ============================================================
section "Case D: bare 模式 dirty → COMMIT_DONE"
envD=$(create_test_env --slug "__main__" --phase active)
STATE_D="$(state_file "$envD" "__main__")"
HEAD_BEFORE_D="$(git -C "$envD" rev-parse --short HEAD)"
echo "new_code" > "$envD/dirty_file.py"

OUT_D="$(bash "$LOOP_COMMIT" "$STATE_D" 2>&1 || true)"
LAST_D="$(echo "$OUT_D" | tail -1)"
ACTION_D="$(echo "$LAST_D" | awk '{print $1}')"

assert "Case D 输出 COMMIT_DONE" "[ '$ACTION_D' = 'COMMIT_DONE' ]"
assert "Case D 主仓 HEAD 推进" \
  "[ \"\$(git -C '$envD' rev-parse --short HEAD)\" != '$HEAD_BEFORE_D' ]"

# ============================================================
# Case E: state 不存在 → ERROR exit 3
# ============================================================
section "Case E: state 不存在 → ERROR"
TMPD="$(mktemp -d)"
_HARNESS_TMPDIRS+=("$TMPD")
EC_E=0
OUT_E="$(bash "$LOOP_COMMIT" "$TMPD/nonexistent.yml" 2>&1)" || EC_E=$?
LAST_E="$(echo "$OUT_E" | tail -1)"

assert "Case E 输出含 ERROR" "echo '$LAST_E' | grep -q '^ERROR'"
assert "Case E exit 3" "[ '$EC_E' -eq 3 ]"

harness_report
