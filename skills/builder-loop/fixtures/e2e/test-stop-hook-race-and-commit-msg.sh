#!/usr/bin/env bash
# test-stop-hook-race-and-commit-msg.sh — E2E：V1.8.3 flock 互斥 + auto-commit message 语义化
#
# 场景：
#   A. flock 并发互斥：后台 subshell 持锁 → 前台 Stop hook 抢不到锁 → exit 0 静默
#   B. auto-commit message 从 state.task_description 构造
#   C. task_description 为空时降级为旧 Auto-commit iter N
#   D. stop hook PASS 路径 cleanup_worktree rm state 后不报 grep 错

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "race-and-commit-msg"

MERGE_SCRIPT="${HARNESS_REPO_ROOT}/skills/builder-loop/scripts/merge-worktree-back.sh"
SKILL_SCRIPTS_DIR="${HARNESS_REPO_ROOT}/skills/builder-loop/scripts"

assert_file_exists "被测 Stop hook 存在" "$HARNESS_HOOK"
assert_file_exists "被测 merge 脚本存在" "$MERGE_SCRIPT"
assert "flock 工具可用" "command -v flock >/dev/null 2>&1"

# ==== 场景 A：flock 并发互斥 ====
section "场景 A：flock 并发互斥"

envA=$(create_test_env --slug "__main__" --phase active --pass-cmd "sleep 5")
STATE_DIR_A="$envA/.claude/builder-loop/state"

LOCK_FILE="$envA/.claude/builder-loop/stop-hook-__main__.lock"

# 后台进程持锁 8 秒
(
  exec 201>"$LOCK_FILE"
  flock 201
  sleep 8
) &
BG_PID=$!
sleep 1  # 等后台抢到锁

# 前台调 Stop hook，期望抢不到锁 exit 0 静默
TS_BEFORE=$(date +%s)
rA=$(run_hook "$envA")
TS_AFTER=$(date +%s)
ELAPSED=$(( TS_AFTER - TS_BEFORE ))

assert_ec "前台 hook exit 0（抢不到锁静默放行）" "$rA" 0
assert "前台 hook 耗时 < 3s（快速放行）" "[ '$ELAPSED' -lt 3 ]"
assert "stderr 不含 iter 流程标志" "! grep -q 'iter' '$(echo "$rA")/stderr'"
assert "stderr 不含 '正在跑 PASS_CMD'" "! grep -q '正在跑 PASS_CMD' '$(echo "$rA")/stderr'"

wait "$BG_PID" 2>/dev/null || true
sleep 1

# 锁释放后，造一个 dirty 文件（防 L2B 静默：HEAD==last_iter_head + 空 status）
echo "dirty" >> "$envA/README.md"
rA2=$(run_hook "$envA")
assert_stderr_contains "锁释放后 stderr 含 iter" "$rA2" "iter"

# ==== 场景 B：auto-commit message 从 task_description 构造 ====
section "场景 B：auto-commit message 语义化"

TMP_B="$(mktemp -d)"
_HARNESS_TMPDIRS+=("$TMP_B")
(
  cd "$TMP_B"
  git init -q
  git config user.email "e2e@test.local"
  git config user.name "e2e-test"
  echo base > README.md
  git add -A
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Base commit for merge test"
)
START_HEAD_B="$(git -C "$TMP_B" rev-parse --short HEAD)"

BRANCH_B="loop/e2e-msg-test"
WT_PATH_B="$TMP_B/.wt"
git -C "$TMP_B" worktree add -q -b "$BRANCH_B" "$WT_PATH_B" HEAD

mkdir -p "$TMP_B/.claude/builder-loop/state"
STATE_B="$TMP_B/.claude/builder-loop/state/e2e-msg-test.yml"
TASK_DESC_B="E2E test auto-commit message propagation"
cat > "$STATE_B" <<YMLEOF
active: true
iter: 0
max_iter: 5
project_root: "$TMP_B"
start_head: "$START_HEAD_B"
worktree_path: "$WT_PATH_B"
task_description: |
  $TASK_DESC_B
source_dirs: "src"
test_dirs: "tests"
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
stopped_reason: ""
created_at: "2026-05-09T00:00:00+08:00"
YMLEOF

echo "changed" > "$WT_PATH_B/CHANGES.md"
MERGE_OUT_B="$(bash "$MERGE_SCRIPT" "$STATE_B" 2>&1 || true)"
MERGE_LAST_B="$(echo "$MERGE_OUT_B" | tail -1)"

assert "merge 输出最后一行是 MERGED" "[ \"\$(echo '$MERGE_LAST_B' | awk '{print \$1}')\" = 'MERGED' ]"

COMMIT_MSG_B="$(git -C "$TMP_B" log -1 --pretty=%s)"
assert "commit msg 含 Auto-commit" "echo '$COMMIT_MSG_B' | grep -q 'Auto-commit'"
assert "commit msg 含 task_description 内容" "echo '$COMMIT_MSG_B' | grep -q 'E2E test auto-commit message propagation'"
assert "commit msg 不是旧格式" "! echo '$COMMIT_MSG_B' | grep -qE 'Auto-commit iter [0-9]+\$'"
assert "commit msg 合规" "echo '$COMMIT_MSG_B' | grep -q 'chore(loop): \[cr_id_skip\] Auto-commit'"

# ==== 场景 C：task_description 为空时降级 ====
section "场景 C：task_description 为空时降级到 iter N"

TMP_C="$(mktemp -d)"
_HARNESS_TMPDIRS+=("$TMP_C")
(
  cd "$TMP_C"
  git init -q
  git config user.email "e2e@test.local"
  git config user.name "e2e-test"
  echo base > README.md
  git add -A
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Base for empty-task test"
)
START_HEAD_C="$(git -C "$TMP_C" rev-parse --short HEAD)"

BRANCH_C="loop/e2e-empty-task"
WT_PATH_C="$TMP_C/.wt"
git -C "$TMP_C" worktree add -q -b "$BRANCH_C" "$WT_PATH_C" HEAD

mkdir -p "$TMP_C/.claude/builder-loop/state"
STATE_C="$TMP_C/.claude/builder-loop/state/e2e-empty-task.yml"
cat > "$STATE_C" <<YMLEOF
active: true
iter: 2
max_iter: 5
project_root: "$TMP_C"
start_head: "$START_HEAD_C"
worktree_path: "$WT_PATH_C"
task_description: |

source_dirs: "src"
test_dirs: "tests"
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
stopped_reason: ""
created_at: "2026-05-09T00:00:00+08:00"
YMLEOF

echo "changed-c" > "$WT_PATH_C/CHANGES.md"
MERGE_OUT_C="$(bash "$MERGE_SCRIPT" "$STATE_C" 2>&1 || true)"
MERGE_LAST_C="$(echo "$MERGE_OUT_C" | tail -1)"

assert "场景 C merge 输出 MERGED" "[ \"\$(echo '$MERGE_LAST_C' | awk '{print \$1}')\" = 'MERGED' ]"

COMMIT_MSG_C="$(git -C "$TMP_C" log -1 --pretty=%s)"
assert "空 task 降级为 Auto-commit iter 2" "echo '$COMMIT_MSG_C' | grep -qE 'Auto-commit iter 2\$'"
assert "降级 msg 合规" "echo '$COMMIT_MSG_C' | grep -q 'chore(loop): \[cr_id_skip\]'"

# ==== 场景 D：stop hook PASS 路径下 state 被 cleanup rm 后不报错 ====
section "场景 D：PASS 路径 cleanup rm state 后不报错"

TMP_D="$(mktemp -d)"
_HARNESS_TMPDIRS+=("$TMP_D")
(
  cd "$TMP_D"
  git init -q
  git config user.email "e2e@test.local"
  git config user.name "e2e-test"
  mkdir -p src tests .claude
  echo seed > README.md
  cat > .claude/loop.yml <<'YMLEOF'
pass_cmd:
  - stage: smoke
    cmd: "true"
    timeout: 10
max_iterations: 3
layout:
  source_dirs: [src]
  test_dirs: [tests]
worktree:
  enabled: true
YMLEOF
  git add -A
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Base for stop-hook PASS test"
  git add .claude/loop.yml 2>/dev/null || true
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Add loop.yml" 2>/dev/null || true
)

(cd "$TMP_D" && bash "$SKILL_SCRIPTS_DIR/setup-builder-loop.sh" "E2E stop hook PASS with cleanup" >/dev/null 2>&1)

WT_PATH_D="$(ls -d "$TMP_D/.claude/worktrees/"*/ 2>/dev/null | head -1)"
[ -n "$WT_PATH_D" ] && WT_PATH_D="${WT_PATH_D%/}"
STATE_D="$(ls "$TMP_D/.claude/builder-loop/state/"*.yml 2>/dev/null | head -1)"

assert "worktree 已创建" "[ -n '$WT_PATH_D' ] && [ -d '$WT_PATH_D' ]"
assert "state 文件已创建" "[ -n '$STATE_D' ] && [ -f '$STATE_D' ]"

# 在 worktree 里造新 commit
mkdir -p "$WT_PATH_D/src"
echo "feature" > "$WT_PATH_D/src/feature.txt"
git -C "$WT_PATH_D" add -A
git -C "$WT_PATH_D" -c core.hooksPath=/dev/null commit -q -m "feat(test): [cr_id_skip] Add feature in worktree"

rD=$(run_hook "$WT_PATH_D")
assert_ec "场景 D Stop hook exit 2（PASS 续接）" "$rD" 2
assert "场景 D stderr 不含 No such file" "! grep -q 'No such file' '$(echo "$rD")/stderr'"
assert_stderr_contains "场景 D stderr 含 PASS_CMD 全部阶段通过" "$rD" "PASS_CMD 全部阶段通过"
assert_file_exists "场景 D state 仍存在（V3.0 不删）" "$STATE_D"
assert "场景 D state 含 reviewer_pending 段" "grep -q '^reviewer_pending:' '$STATE_D'"
assert "场景 D phase = passed_pending_review" \
  "grep -E '^phase:' '$STATE_D' | head -1 | grep -q 'passed_pending_review'"

harness_report
