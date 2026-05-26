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

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "pass-cmd-runs-worktree"

assert "stop hook 存在" "[ -f '$HARNESS_HOOK' ]"
assert "setup 脚本存在" "[ -f '$HARNESS_SETUP' ]"

TMP="$(mktemp -d)"
_HARNESS_TMPDIRS+=("$TMP")

# ---- Step 1: 创建主仓 + 启用 worktree 的 loop.yml ----
section "Step 1: 初始化主仓 + worktree 启用"
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
git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Initial seed for v2.0 worktree pass-cmd test"

# ---- Step 2: 调 setup 创建 worktree + state（V2.0 schema）----
section "Step 2: setup-builder-loop.sh 启动 worktree"
SETUP_OUT="$(bash "$HARNESS_SETUP" "v2-pass-cmd-test" 2>&1 || true)"
STATE_FILE="$(find "$TMP/.claude/builder-loop/state" -maxdepth 1 -name '*-v2-pass-cmd-test.yml' 2>/dev/null | head -1)"

assert "setup 创建了 state 文件" "[ -n '$STATE_FILE' ] && [ -f '$STATE_FILE' ]"
WORKTREE_PATH="$(read_state_field "$STATE_FILE" worktree_path)"
PROJ_FIELD="$(read_state_field "$STATE_FILE" project_root)"
MAIN_FIELD="$(read_state_field "$STATE_FILE" main_repo_path)"

assert "worktree 已创建" "[ -n '$WORKTREE_PATH' ] && [ -d '$WORKTREE_PATH' ]"
assert "state 写入 main_repo_path 字段（V2.0 schema）" "[ -n '$MAIN_FIELD' ]"
assert "main_repo_path == 主仓" "[ '$MAIN_FIELD' = '$TMP' ]"
assert "project_root == worktree（V2.0 语义）" "[ '$PROJ_FIELD' = '$WORKTREE_PATH' ]"

# ---- Step 3: 在 worktree 内改 loop.yml 加新 stage ----
section "Step 3: worktree 内 loop.yml 加 stage=worktree_only"
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
assert "主仓 loop.yml 未变（仍单 stage）" "! grep -q 'worktree_only' '$TMP/.claude/loop.yml'"
assert "worktree loop.yml 已加 stage" "grep -q 'worktree_only' '$WORKTREE_PATH/.claude/loop.yml'"

# ---- Step 4: 触发 stop hook（cwd = worktree）----
section "Step 4: 触发 stop hook（cwd=worktree）→ 期望 PASS_CMD 跑 worktree 配置"
RES1="$(run_hook "$WORKTREE_PATH")"

assert_ec "stop hook 退出码 = 2（PASS 续接）" "$RES1" 2
assert_stderr_contains "stderr 含 PASS_CMD 全部阶段通过" "$RES1" 'PASS_CMD 全部阶段通过'

LOG_DIR="$TMP/.claude/loop-runs"
SMOKE_LOG="$LOG_DIR/iter-1-smoke.log"
WT_ONLY_LOG="$LOG_DIR/iter-1-worktree_only.log"

assert_file_exists "smoke stage 日志存在" "$SMOKE_LOG"
assert_file_exists "worktree_only stage 日志存在" "$WT_ONLY_LOG"
assert_contains "worktree_only 日志含 MARKER_RAN_IN_WORKTREE" "$WT_ONLY_LOG" 'MARKER_RAN_IN_WORKTREE'

# ---- Step 5: 老 V1.x state（无 main_repo_path 字段）兼容验证 ----
section "Step 5: 老 state 兼容（删 main_repo_path / project_root 设为主仓）"
rm -rf "$TMP/.claude/builder-loop"
mkdir -p "$TMP/.claude/builder-loop/state"

LEGACY_WT="$TMP/.claude/worktrees/legacy-task"
git -C "$TMP" worktree add -b "loop/legacy-task" "$LEGACY_WT" HEAD >/dev/null 2>&1

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

RES2="$(run_hook "$LEGACY_WT")"

LEGACY_LOG="$TMP/.claude/loop-runs/iter-1-legacy_marker.log"
assert_ec "老 state stop hook 退出码 = 2" "$RES2" 2
assert_file_exists "老 state PASS_CMD 跑了 worktree 配置" "$LEGACY_LOG"
assert_contains "老 state worktree 内命令真跑了" "$LEGACY_LOG" 'LEGACY_RAN_IN_WORKTREE'

# ---- Step 6: 含空格路径鲁棒性 ----
section "Step 6: 含空格的 mktemp 路径下 setup + state 定位仍正常"
SPACE_TMP="$(mktemp -d)/dir with space"
_HARNESS_TMPDIRS+=("$(dirname "$SPACE_TMP")")
mkdir -p "$SPACE_TMP"
cd "$SPACE_TMP"
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
worktree:
  enabled: true
YMLEOF
echo "seed" > README.md
git add -A
git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Init in spaced path"
SETUP_OUT_S="$(bash "$HARNESS_SETUP" "spaced-path-test" 2>&1 || true)"
STATE_FILE_S="$(find "$SPACE_TMP/.claude/builder-loop/state" -maxdepth 1 -name '*-spaced-path-test.yml' 2>/dev/null | head -1)"

assert "含空格路径 state 能定位到" "[ -n '$STATE_FILE_S' ] && [ -f '$STATE_FILE_S' ]"
WT_PATH_S="$(read_state_field "$STATE_FILE_S" worktree_path)"
assert "含空格 worktree 已创建" "[ -n '$WT_PATH_S' ] && [ -d '$WT_PATH_S' ]"

harness_report
