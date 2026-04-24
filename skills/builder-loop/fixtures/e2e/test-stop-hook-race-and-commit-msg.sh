#!/usr/bin/env bash
# test-stop-hook-race-and-commit-msg.sh — E2E：V1.8.3 flock 互斥 + auto-commit message 语义化
#
# 覆盖场景：
#   A. flock 并发互斥：后台 subshell 持锁 → 前台 Stop hook 抢不到锁 → exit 0 静默
#   B. auto-commit message 从 state.task_description 构造
#   C. task_description 为空时降级为旧 Auto-commit iter N
#
# 用法：bash test-stop-hook-race-and-commit-msg.sh
# 退出码：0=全部通过 / 1=有失败

set -euo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$THIS_DIR/../../../.." && pwd)"
HOOK_SCRIPT="${REPO_ROOT}/scripts/builder-loop-stop.sh"
SKILL_SCRIPTS_DIR="${REPO_ROOT}/skills/builder-loop/scripts"
MERGE_SCRIPT="${SKILL_SCRIPTS_DIR}/merge-worktree-back.sh"

PASS=0
FAIL=0

assert() {
  local desc="$1" cond="$2"
  if eval "$cond"; then echo "  ✅ $desc"; PASS=$(( PASS + 1 ));
  else echo "  ❌ $desc (cond: $cond)"; FAIL=$(( FAIL + 1 )); fi
}

echo "=== E2E: V1.8.3 flock 互斥 + auto-commit message 语义化 ==="
assert "被测 Stop hook 存在" "[ -f '$HOOK_SCRIPT' ]"
assert "被测 merge 脚本存在" "[ -f '$MERGE_SCRIPT' ]"
assert "flock 工具可用" "command -v flock >/dev/null 2>&1"

# ==== 场景 A：flock 并发互斥 ====
echo ""
echo "--- 场景 A：flock 并发互斥 ---"

TMP_A="$(mktemp -d)"
trap 'rm -rf "$TMP_A" "${TMP_B:-}" "${TMP_C:-}"' EXIT

cd "$TMP_A"
git init -q
git config user.email "e2e@test.local"
git config user.name "e2e-test"
mkdir -p .claude src tests
cat > .claude/loop.yml <<'YMLEOF'
pass_cmd:
  - stage: smoke
    cmd: "sleep 5"
    timeout: 10
max_iterations: 3
layout:
  source_dirs: [src]
  test_dirs: [tests]
worktree:
  enabled: false
YMLEOF
echo seed > README.md
git add -A
git commit -q -m "chore(test): [cr_id_skip] Initial seed for race test"

# 手动建一个 __main__ state，模拟 bootstrap 后的活跃状态
mkdir -p .claude/builder-loop/state
cat > .claude/builder-loop/state/__main__.yml <<YMLEOF
active: true
iter: 0
max_iter: 5
project_root: "$TMP_A"
start_head: "$(git rev-parse --short HEAD)"
worktree_path: ""
plan_file: ""
task_description: |
  E2E race test task
source_dirs: "src"
test_dirs: "tests"
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
stopped_reason: ""
created_at: "$(date -Iseconds)"
YMLEOF

LOCK_FILE="$TMP_A/.claude/builder-loop/stop-hook-__main__.lock"

# 后台进程持锁 8 秒
(
  exec 201>"$LOCK_FILE"
  flock 201
  sleep 8
) &
BG_PID=$!
# 等后台进程抢到锁（一小段窗口）
sleep 1

# 前台调 Stop hook，期望抢不到锁 exit 0 静默
ERR_A="$(mktemp)"
EC_A=0
TS_BEFORE=$(date +%s)
printf '{"cwd": "%s"}' "$TMP_A" | bash "$HOOK_SCRIPT" 2>"$ERR_A" >/dev/null || EC_A=$?
TS_AFTER=$(date +%s)
ELAPSED=$(( TS_AFTER - TS_BEFORE ))

assert "前台 hook exit 0（抢不到锁静默放行）" "[ '$EC_A' -eq 0 ]"
assert "前台 hook 耗时 < 3s（说明没等待、快速放行）" "[ '$ELAPSED' -lt 3 ]"
assert "stderr 不含 'iter' 流程标志" "! grep -q 'iter' '$ERR_A'"
assert "stderr 不含 '正在跑 PASS_CMD'" "! grep -q '正在跑 PASS_CMD' '$ERR_A'"

# 等后台结束
wait "$BG_PID" 2>/dev/null || true

# 后台释放锁后，再调 hook 应能正常进入流程（证明锁不永久持有）
sleep 1
ERR_A2="$(mktemp)"
EC_A2=0
printf '{"cwd": "%s"}' "$TMP_A" | bash "$HOOK_SCRIPT" 2>"$ERR_A2" >/dev/null || EC_A2=$?
# 此时应进入正常流程（可能 PASS 也可能 FAIL，取决于 sleep 5 能否完成）
# 关键断言：stderr 里能看到 'iter' 关键字，说明进入了流程
assert "锁释放后 stderr 含 'iter'（说明进入流程）" "grep -q 'iter' '$ERR_A2'"

# ==== 场景 B：auto-commit message 从 task_description 构造 ====
echo ""
echo "--- 场景 B：auto-commit message 语义化 ---"

TMP_B="$(mktemp -d)"
cd "$TMP_B"
git init -q
git config user.email "e2e@test.local"
git config user.name "e2e-test"
echo base > README.md
git add -A
git commit -q -m "chore(test): [cr_id_skip] Base commit for merge test"
START_HEAD_B="$(git rev-parse --short HEAD)"

# 建 worktree
BRANCH_B="loop/e2e-msg-test"
WT_PATH_B="$TMP_B/.wt"
git worktree add -q -b "$BRANCH_B" "$WT_PATH_B" HEAD

# 准备 state，含明确的 task_description
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
plan_file: ""
task_description: |
  $TASK_DESC_B
source_dirs: "src"
test_dirs: "tests"
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
stopped_reason: ""
created_at: "$(date -Iseconds)"
YMLEOF

# 在 worktree 里造一个未 commit 的改动
echo "changed" > "$WT_PATH_B/CHANGES.md"

# 跑 merge-worktree-back.sh
MERGE_OUT_B="$(bash "$MERGE_SCRIPT" "$STATE_B" 2>&1 || true)"
MERGE_LAST_B="$(echo "$MERGE_OUT_B" | tail -1)"

assert "merge 输出最后一行是 MERGED" "[ \"\$(echo '$MERGE_LAST_B' | awk '{print \$1}')\" = 'MERGED' ]"

# 检查主干最新 commit message 是否含 task_description
# merge-worktree-back.sh 会把 worktree 的 auto-commit 合回主干（ff-only），所以主干 HEAD 就是 auto-commit 那个
cd "$TMP_B"
COMMIT_MSG_B="$(git log -1 --pretty=%s)"
echo "    主干最新 commit message: $COMMIT_MSG_B"

assert "commit msg 含 'Auto-commit'" "echo '$COMMIT_MSG_B' | grep -q 'Auto-commit'"
assert "commit msg 含 task_description 内容" "echo '$COMMIT_MSG_B' | grep -q 'E2E test auto-commit message propagation'"
assert "commit msg 不是旧的 'Auto-commit iter' 格式" "! echo '$COMMIT_MSG_B' | grep -qE 'Auto-commit iter [0-9]+$'"
assert "commit msg 合规（有 [cr_id_skip] 和 chore(loop)）" "echo '$COMMIT_MSG_B' | grep -q 'chore(loop): \[cr_id_skip\] Auto-commit'"

# ==== 场景 C：task_description 为空时降级 ====
echo ""
echo "--- 场景 C：task_description 为空时降级到 iter N ---"

TMP_C="$(mktemp -d)"
cd "$TMP_C"
git init -q
git config user.email "e2e@test.local"
git config user.name "e2e-test"
echo base > README.md
git add -A
git commit -q -m "chore(test): [cr_id_skip] Base for empty-task test"
START_HEAD_C="$(git rev-parse --short HEAD)"

BRANCH_C="loop/e2e-empty-task"
WT_PATH_C="$TMP_C/.wt"
git worktree add -q -b "$BRANCH_C" "$WT_PATH_C" HEAD

mkdir -p "$TMP_C/.claude/builder-loop/state"
STATE_C="$TMP_C/.claude/builder-loop/state/e2e-empty-task.yml"
# 故意写一个空 task_description（block scalar 下只有一个空白缩进行）
cat > "$STATE_C" <<YMLEOF
active: true
iter: 2
max_iter: 5
project_root: "$TMP_C"
start_head: "$START_HEAD_C"
worktree_path: "$WT_PATH_C"
plan_file: ""
task_description: |

source_dirs: "src"
test_dirs: "tests"
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
stopped_reason: ""
created_at: "$(date -Iseconds)"
YMLEOF

echo "changed-c" > "$WT_PATH_C/CHANGES.md"

MERGE_OUT_C="$(bash "$MERGE_SCRIPT" "$STATE_C" 2>&1 || true)"
MERGE_LAST_C="$(echo "$MERGE_OUT_C" | tail -1)"

assert "场景 C merge 输出 MERGED" "[ \"\$(echo '$MERGE_LAST_C' | awk '{print \$1}')\" = 'MERGED' ]"

cd "$TMP_C"
COMMIT_MSG_C="$(git log -1 --pretty=%s)"
echo "    降级 commit message: $COMMIT_MSG_C"

assert "空 task_description 降级为 'Auto-commit iter 2'" "echo '$COMMIT_MSG_C' | grep -qE 'Auto-commit iter 2$'"
assert "降级 msg 合规（有 [cr_id_skip]）" "echo '$COMMIT_MSG_C' | grep -q 'chore(loop): \[cr_id_skip\]'"

# ==== 场景 D：stop hook PASS 路径下，cleanup_worktree rm state 后不应 grep 报错 ====
# 复现 session d9ef1004 `grep: .../1777049006-stop-hook-flock.yml: No such file` 的根因场景
echo ""
echo "--- 场景 D：stop hook PASS 路径下 state 被 cleanup rm 后不报错 ---"

TMP_D="$(mktemp -d)"
trap 'rm -rf "$TMP_A" "${TMP_B:-}" "${TMP_C:-}" "${TMP_D:-}"' EXIT
cd "$TMP_D"
git init -q
git config user.email "e2e@test.local"
git config user.name "e2e-test"
mkdir -p src tests
echo seed > README.md
git add -A
git commit -q -m "chore(test): [cr_id_skip] Base for stop-hook PASS test"
START_HEAD_D="$(git rev-parse --short HEAD)"

# 用真的 setup-builder-loop.sh 创建 worktree + state（而非手写 state，更贴近真实场景）
# 但 setup 脚本依赖 loop-init 生成 loop.yml，我们直接手造最小 loop.yml
mkdir -p .claude
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

bash "$SKILL_SCRIPTS_DIR/setup-builder-loop.sh" "E2E stop hook PASS with cleanup" >/dev/null 2>&1

# 定位 setup 产出的 worktree + state
WT_PATH_D="$(ls -d "$TMP_D/.claude/worktrees/"*/ 2>/dev/null | head -1)"
[ -n "$WT_PATH_D" ] && WT_PATH_D="${WT_PATH_D%/}"
STATE_D="$(ls "$TMP_D/.claude/builder-loop/state/"*.yml 2>/dev/null | head -1)"

assert "场景 D worktree 已创建" "[ -n '$WT_PATH_D' ] && [ -d '$WT_PATH_D' ]"
assert "场景 D state 文件已创建" "[ -n '$STATE_D' ] && [ -f '$STATE_D' ]"

# 在 worktree 里造真实的新 commit（让 merge 走 MERGED 分支而非 NOOP）
mkdir -p "$WT_PATH_D/src"
echo "feature" > "$WT_PATH_D/src/feature.txt"
git -C "$WT_PATH_D" add -A
git -C "$WT_PATH_D" commit -q -m "feat(test): [cr_id_skip] Add feature in worktree"

# 调 Stop hook（cwd 设为主仓库，locate-state 会按 state 里的 worktree_path 匹配，但 cwd 不在 wt 下）
# 为让 locate 命中，传 cwd = worktree 路径（模拟 CC 在 worktree 里 stop）
ERR_D="$(mktemp)"
EC_D=0
printf '{"cwd": "%s"}' "$WT_PATH_D" | bash "$HOOK_SCRIPT" 2>"$ERR_D" >/dev/null || EC_D=$?

assert "场景 D Stop hook exit 2（PASS 续接）" "[ '$EC_D' -eq 2 ]"
assert "场景 D stderr 不含 'No such file'（修复关键断言）" "! grep -q 'No such file' '$ERR_D'"
assert "场景 D stderr 含 'PASS_CMD 全部阶段通过'" "grep -q 'PASS_CMD 全部阶段通过' '$ERR_D'"
assert "场景 D reviewer-params.json 已写入" "[ -f '$TMP_D/.claude/reviewer-params.json' ]"

# ==== 汇总 ====
echo ""
echo "=== 汇总: ✅ PASS=${PASS}  ❌ FAIL=${FAIL} ==="

if [ "$FAIL" -gt 0 ]; then exit 1; fi
exit 0
