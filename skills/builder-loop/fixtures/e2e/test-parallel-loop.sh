#!/usr/bin/env bash
# test-parallel-loop.sh — 验证多状态并行 loop 互不干扰
#
# 场景：
#   1. 同一项目起 2 个 worktree loop：各自 state 独立、路径不重叠
#   2. locate-state.sh 从 worktree A cwd → 返回 state A；从 worktree B → state B
#   3. setup 同 slug 会被拒绝（exit 4）
#   4. 孤儿 state（worktree_path 失效）被 setup 启动时懒 gc 清理
#   5. bare loop + worktree loop 共存（slug=__main__ vs slug=<ts>-*）
#
# 用法：bash test-parallel-loop.sh
# 退出码：0=全部通过 / 1=有失败

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "多状态并行 loop"

SETUP_SCRIPT="${HARNESS_REPO_ROOT}/skills/builder-loop/scripts/setup-builder-loop.sh"
LOCATE_SCRIPT="${HARNESS_REPO_ROOT}/skills/builder-loop/scripts/locate-state.sh"

assert "setup 脚本存在" "[ -f '$SETUP_SCRIPT' ]"
assert "locate 脚本存在" "[ -f '$LOCATE_SCRIPT' ]"

TMP="$(mktemp -d -t builder-loop-parallel-XXXXXX)"
_HARNESS_TMPDIRS+=("$TMP")

# ---- 建测试仓 ----
(
  cd "$TMP"
  git init -q
  git config user.email "harness@test.local"
  git config user.name "harness"
  git -c core.hooksPath=/dev/null commit -q --allow-empty -m "chore(test): [cr_id_skip] Root"
  mkdir -p .claude src tests
  cat > .claude/loop.yml <<'YML'
pass_cmd:
  - { stage: test, cmd: "true", timeout: 30 }
max_iterations: 5
layout:
  source_dirs: [src]
  test_dirs: [tests]
worktree:
  enabled: true
YML
  git -c core.hooksPath=/dev/null add -A
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Bootstrap"
)

# ---- 场景 1: 起两个 worktree loop ----
section "场景 1: 起两个并行 worktree loop"
(cd "$TMP" && bash "$SETUP_SCRIPT" "task-alpha") > /tmp/setup-alpha.log 2>&1
sleep 1  # 保证 timestamp 不同
(cd "$TMP" && bash "$SETUP_SCRIPT" "task-beta") > /tmp/setup-beta.log 2>&1

STATE_DIR="$TMP/.claude/builder-loop/state"
STATE_CNT="$(ls -1 "$STATE_DIR"/*.yml 2>/dev/null | wc -l)"
assert "state 目录下有 2 个 .yml" "[ '$STATE_CNT' -eq 2 ]"

# 提取两个 state 文件
STATE_A="$(cd "$STATE_DIR" && ls -1 *alpha*.yml | head -1)"
STATE_A="${STATE_DIR}/${STATE_A}"
STATE_B="$(cd "$STATE_DIR" && ls -1 *beta*.yml | head -1)"
STATE_B="${STATE_DIR}/${STATE_B}"
assert "state A 存在且 alpha 对应" "[ -n '$STATE_A' ] && [ -f '$STATE_A' ]"
assert "state B 存在且 beta 对应" "[ -n '$STATE_B' ] && [ -f '$STATE_B' ]"
assert "两个 state 互不相同" "[ '$STATE_A' != '$STATE_B' ]"

WT_A="$(read_state_field "$STATE_A" "worktree_path")"
WT_B="$(read_state_field "$STATE_B" "worktree_path")"
assert "worktree A 目录存在" "[ -d '$WT_A' ]"
assert "worktree B 目录存在" "[ -d '$WT_B' ]"
assert "两 worktree 路径不同" "[ '$WT_A' != '$WT_B' ]"

# ---- 场景 2: locate-state.sh 按 CWD 正确定位 ----
section "场景 2: locate-state.sh CWD 定位"
LOC_A="$(bash "$LOCATE_SCRIPT" "$WT_A")"
LOC_B="$(bash "$LOCATE_SCRIPT" "$WT_B")"
assert "cwd=WT_A → 返回 state A" "[ '$LOC_A' = '$STATE_A' ]"
assert "cwd=WT_B → 返回 state B" "[ '$LOC_B' = '$STATE_B' ]"

# 从 worktree 的子目录也要能找到
mkdir -p "$WT_A/src/sub"
LOC_A_SUB="$(bash "$LOCATE_SCRIPT" "$WT_A/src/sub")"
assert "cwd=WT_A/src/sub → 仍返回 state A" "[ '$LOC_A_SUB' = '$STATE_A' ]"

# ---- 场景 3: 主目录 cwd → 无对应 state → 返回空或 __main__ ----
LOC_MAIN="$(bash "$LOCATE_SCRIPT" "$TMP" 2>/dev/null || echo "")"
assert "cwd=项目主目录且无 __main__.yml → 返回空" "[ -z '$LOC_MAIN' ]"

# ---- 场景 4: 再次 setup 同 slug 被拒绝 ----
section "场景 4: bare loop slug 冲突被拒"
(cd "$TMP" && bash "$SETUP_SCRIPT" --no-worktree "bare-task") > /tmp/setup-bare1.log 2>&1
assert "__main__ state 生成" "[ -f '$STATE_DIR/__main__.yml' ]"

SECOND_EC=0
(cd "$TMP" && bash "$SETUP_SCRIPT" --no-worktree "bare-task-2") > /tmp/setup-bare2.log 2>&1 || SECOND_EC=$?
assert "第二次 bare setup exit=4（被拒）" "[ '$SECOND_EC' -eq 4 ]"

# ---- 场景 4.5: bare setup 并发 flock 竞态 ----
section "场景 4.5: bare setup 并发竞态 (flock)"
# 先清掉场景 4 留下的 __main__，保证起点干净
rm -f "$STATE_DIR/__main__.yml"
# 两个 bare setup 同时发起
(cd "$TMP" && bash "$SETUP_SCRIPT" --no-worktree "race-a") > /tmp/setup-racea.log 2>&1 &
PID_A=$!
(cd "$TMP" && bash "$SETUP_SCRIPT" --no-worktree "race-b") > /tmp/setup-raceb.log 2>&1 &
PID_B=$!
EC_A=0; wait "$PID_A" || EC_A=$?
EC_B=0; wait "$PID_B" || EC_B=$?
# 一定有一个成功（exit 0）+ 一个失败（exit 4 被拒 / 5 lock 超时）
SUCCESS_CNT=0
[ "$EC_A" -eq 0 ] && SUCCESS_CNT=$((SUCCESS_CNT + 1))
[ "$EC_B" -eq 0 ] && SUCCESS_CNT=$((SUCCESS_CNT + 1))
assert "并发 bare setup 恰有 1 个成功（flock 串行化）" "[ '$SUCCESS_CNT' -eq 1 ]"
assert "并发 bare setup 结束后 __main__ state 唯一存在" "[ -f '$STATE_DIR/__main__.yml' ]"

# ---- 场景 5: 孤儿 state 自动 gc ----
section "场景 5: 孤儿 state gc"
# 删除 worktree A 的目录但保留 state A
rm -rf "$WT_A"
(cd "$TMP" && git worktree prune) 2>/dev/null || true
# 再 setup 一个新 loop，trigger gc
(cd "$TMP" && bash "$SETUP_SCRIPT" "task-gamma") > /tmp/setup-gamma.log 2>&1
assert "孤儿 state A 被 gc" "[ ! -f '$STATE_A' ]"
assert "state B 仍存在" "[ -f '$STATE_B' ]"

harness_report
