#!/usr/bin/env bash
# test-pass-cmd-runs-worktree.sh — V2.0 E2E：PASS_CMD 跑 worktree（不再误跑主仓）
#
# 验证场景：
#   1. setup 创建 worktree 后，state 写入 main_repo_path 字段（V2.0 schema）
#   2. 主仓 loop.yml 与 worktree loop.yml 内容不同 → stop hook 跑 worktree 内的 loop.yml
#   3. 在 worktree 内改 loop.yml 加新 stage，同轮 PASS_CMD 跑到新 stage
#   4. 老 V1.x state（无 main_repo_path 字段）也享受新行为
#
# 用法：bash test-pass-cmd-runs-worktree.sh
# 退出码：0=全部通过 / 1=有失败

set -euo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$THIS_DIR/../../../.." && pwd)"
HOOK_SCRIPT="${REPO_ROOT}/scripts/builder-loop-stop.sh"
SETUP_SCRIPT="${REPO_ROOT}/skills/builder-loop/scripts/setup-builder-loop.sh"

PASS=0
FAIL=0

assert() {
  local desc="$1" cond="$2"
  if eval "$cond"; then echo "  ✅ $desc"; PASS=$(( PASS + 1 ));
  else echo "  ❌ $desc (cond: $cond)"; FAIL=$(( FAIL + 1 )); fi
}

call_stop_hook() {
  local proj="$1" err_file="$2" ec=0
  printf '{"cwd": "%s"}' "$proj" | bash "$HOOK_SCRIPT" 2>"$err_file" >/dev/null || ec=$?
  return "$ec"
}

echo "=== V2.0 E2E: PASS_CMD 跑 worktree（不误跑主仓）==="
assert "stop hook 存在" "[ -f '$HOOK_SCRIPT' ]"
assert "setup 脚本存在" "[ -f '$SETUP_SCRIPT' ]"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "    临时仓库：$TMP"

# ---- Step 1: 创建主仓 + 启用 worktree 的 loop.yml ----
echo "--- Step 1: 初始化主仓 + worktree 启用 ---"
cd "$TMP"
git init -q
git config user.email "e2e@test.local"
git config user.name "e2e-test"
mkdir -p .claude src
cat > .claude/loop.yml <<'YMLEOF'
pass_cmd:
  - stage: smoke
    cmd: "true"
    timeout: 10
max_iterations: 3
layout:
  source_dirs: [src]
  test_dirs: []
worktree:
  enabled: true
YMLEOF
echo "seed" > README.md
git add -A
git commit -q -m "chore(test): [cr_id_skip] Initial seed for v2.0 worktree pass-cmd test"
# loop.yml 必须在 worktree 创建之前 commit，否则 git worktree add 看不到（V2.0 PASS_CMD 跑 worktree）
# 注：上面 git add -A 已包含 .claude/loop.yml

# ---- Step 2: 调 setup 创建 worktree + state（V2.0 schema）----
echo "--- Step 2: setup-builder-loop.sh 启动 worktree ---"
SETUP_OUT="$(bash "$SETUP_SCRIPT" "v2-pass-cmd-test" 2>&1 || true)"
STATE_FILE_GLOB="$TMP/.claude/builder-loop/state/"*"-v2-pass-cmd-test.yml"
STATE_FILE="$(ls $STATE_FILE_GLOB 2>/dev/null | head -1 || echo "")"

assert "setup 创建了 state 文件" "[ -n '$STATE_FILE' ] && [ -f '$STATE_FILE' ]"
WORKTREE_PATH="$(grep -E '^worktree_path:' "$STATE_FILE" | head -1 | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/')"
PROJ_FIELD="$(grep -E '^project_root:' "$STATE_FILE" | head -1 | sed -E 's/^project_root:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/')"
MAIN_FIELD="$(grep -E '^main_repo_path:' "$STATE_FILE" | head -1 | sed -E 's/^main_repo_path:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/')"

assert "worktree 已创建" "[ -n '$WORKTREE_PATH' ] && [ -d '$WORKTREE_PATH' ]"
assert "state 写入 main_repo_path 字段（V2.0 schema）" "[ -n '$MAIN_FIELD' ]"
assert "main_repo_path == 主仓" "[ '$MAIN_FIELD' = '$TMP' ]"
assert "project_root == worktree（V2.0 语义）" "[ '$PROJ_FIELD' = '$WORKTREE_PATH' ]"

# ---- Step 3: 在 worktree 内改 loop.yml 加新 stage ----
echo "--- Step 3: worktree 内 loop.yml 加 stage=worktree_only ---"
cat > "$WORKTREE_PATH/.claude/loop.yml" <<'YMLEOF'
pass_cmd:
  - stage: smoke
    cmd: "true"
    timeout: 10
  - stage: worktree_only
    cmd: "echo MARKER_RAN_IN_WORKTREE"
    timeout: 10
max_iterations: 3
layout:
  source_dirs: [src]
  test_dirs: []
worktree:
  enabled: true
YMLEOF
# 主仓 loop.yml 仍然是单 stage（验证 stop hook 没误读主仓）
assert "主仓 loop.yml 未变（仍单 stage）" "! grep -q 'worktree_only' '$TMP/.claude/loop.yml'"
assert "worktree loop.yml 已加 stage" "grep -q 'worktree_only' '$WORKTREE_PATH/.claude/loop.yml'"

# ---- Step 4: 触发 stop hook（cwd = 主仓，模拟 builder 在主仓退出）----
echo "--- Step 4: 触发 stop hook（cwd=worktree）→ 期望 PASS_CMD 跑 worktree 配置 ---"
ERR1="$(mktemp)"
EC1=0
# cwd 给 worktree（locate-state.sh 通过路径反查 state）
call_stop_hook "$WORKTREE_PATH" "$ERR1" || EC1=$?

assert "stop hook 退出码 = 2（PASS 续接）" "[ '$EC1' -eq 2 ]"
assert "stderr 含 PASS_CMD 全部阶段通过" "grep -q 'PASS_CMD 全部阶段通过' '$ERR1'"

# 验证两个 stage 都跑过 → 日志目录有两份
LOG_DIR="$TMP/.claude/loop-runs"
SMOKE_LOG="$LOG_DIR/iter-1-smoke.log"
WT_ONLY_LOG="$LOG_DIR/iter-1-worktree_only.log"

assert "smoke stage 日志存在（基线 stage）" "[ -f '$SMOKE_LOG' ]"
assert "worktree_only stage 日志存在（验证读 worktree 内 loop.yml）" "[ -f '$WT_ONLY_LOG' ]"
assert "worktree_only 日志含 MARKER_RAN_IN_WORKTREE（命令真跑了）" "grep -q 'MARKER_RAN_IN_WORKTREE' '$WT_ONLY_LOG'"

# ---- Step 5: 老 V1.x state（无 main_repo_path 字段）兼容验证 ----
echo "--- Step 5: 老 state 兼容（删 main_repo_path / project_root 设为主仓）---"
# 重置：清掉游标，state 改为老 schema
rm -rf "$TMP/.claude/builder-loop"
mkdir -p "$TMP/.claude/builder-loop/state"

# 创建一个新 worktree（手动模拟，setup 已经会写 V2.0 schema）
LEGACY_WT="$TMP/.claude/worktrees/legacy-task"
git -C "$TMP" worktree add -b "loop/legacy-task" "$LEGACY_WT" HEAD >/dev/null 2>&1

# worktree 内 loop.yml 加 marker stage
cat > "$LEGACY_WT/.claude/loop.yml" <<'YMLEOF'
pass_cmd:
  - stage: legacy_marker
    cmd: "echo LEGACY_RAN_IN_WORKTREE"
    timeout: 10
max_iterations: 3
layout:
  source_dirs: [src]
worktree:
  enabled: true
YMLEOF

# 写 V1.x 老 state（无 main_repo_path 字段，project_root = 主仓）
LEGACY_HEAD="$(git -C "$LEGACY_WT" rev-parse --short HEAD)"
cat > "$TMP/.claude/builder-loop/state/legacy-task.yml" <<EOF
# builder-loop state file (do NOT manually edit while loop is active)
active: true
slug: "legacy-task"
owner_cwd: "$TMP"
iter: 0
max_iter: 5
project_root: "$TMP"
start_head: "$LEGACY_HEAD"
worktree_path: "$LEGACY_WT"
plan_file: ""
task_description: |
  legacy-v1-state-test
source_dirs: "src"
test_dirs: ""
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
stopped_reason: ""
created_at: "2026-04-01T00:00:00+08:00"
EOF

ERR2="$(mktemp)"
EC2=0
call_stop_hook "$LEGACY_WT" "$ERR2" || EC2=$?

LEGACY_LOG="$TMP/.claude/loop-runs/iter-1-legacy_marker.log"
assert "老 state stop hook 退出码 = 2" "[ '$EC2' -eq 2 ]"
assert "老 state PASS_CMD 跑了 worktree 配置" "[ -f '$LEGACY_LOG' ]"
assert "老 state worktree 内命令真跑了" "grep -q 'LEGACY_RAN_IN_WORKTREE' '$LEGACY_LOG' 2>/dev/null"

# ---- 总结 ----
echo ""
echo "=== 总计 ==="
echo "  ✅ PASS: $PASS"
echo "  ❌ FAIL: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
