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

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/../../scripts" && pwd)"
SETUP_SCRIPT="$SCRIPTS_DIR/setup-builder-loop.sh"
LOCATE_SCRIPT="$SCRIPTS_DIR/locate-state.sh"

for f in "$SETUP_SCRIPT" "$LOCATE_SCRIPT"; do
  [ -f "$f" ] || { echo "❌ missing $f" >&2; exit 1; }
done

PASS=0
FAIL=0

assert() {
  local desc="$1" cond="$2"
  if eval "$cond"; then
    echo "  ✅ $desc"
    PASS=$((PASS + 1))
  else
    echo "  ❌ $desc"
    FAIL=$((FAIL + 1))
  fi
}

TMP="$(mktemp -d -t builder-loop-parallel-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

echo "=== E2E: 多状态并行 loop ==="

# ---- 建测试仓 ----
cd "$TMP"
git init -q
git -c core.hooksPath=/dev/null commit -q --allow-empty -m "root"
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
git -c core.hooksPath=/dev/null commit -q -m "bootstrap"

# ---- 场景 1: 起两个 worktree loop ----
echo "--- 场景 1: 起两个并行 worktree loop ---"
bash "$SETUP_SCRIPT" "task-alpha" > /tmp/setup-alpha.log 2>&1
sleep 1  # 保证 timestamp 不同
bash "$SETUP_SCRIPT" "task-beta" > /tmp/setup-beta.log 2>&1

STATE_DIR=".claude/builder-loop/state"
STATE_CNT="$(ls -1 "$STATE_DIR"/*.yml 2>/dev/null | wc -l)"
assert "state 目录下有 2 个 .yml" "[ '$STATE_CNT' -eq 2 ]"

# 提取两个 state 文件（用绝对路径，locate-state.sh 返回的是绝对路径）
STATE_A="$(cd "$STATE_DIR" && ls -1 *alpha*.yml | head -1)"
STATE_A="${TMP}/${STATE_DIR}/${STATE_A}"
STATE_B="$(cd "$STATE_DIR" && ls -1 *beta*.yml | head -1)"
STATE_B="${TMP}/${STATE_DIR}/${STATE_B}"
assert "state A 存在且 alpha 对应" "[ -n '$STATE_A' ] && [ -f '$STATE_A' ]"
assert "state B 存在且 beta 对应" "[ -n '$STATE_B' ] && [ -f '$STATE_B' ]"
assert "两个 state 互不相同" "[ '$STATE_A' != '$STATE_B' ]"

WT_A="$(grep -E '^worktree_path:' "$STATE_A" | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?$/\1/')"
WT_B="$(grep -E '^worktree_path:' "$STATE_B" | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?$/\1/')"
assert "worktree A 目录存在" "[ -d '$WT_A' ]"
assert "worktree B 目录存在" "[ -d '$WT_B' ]"
assert "两 worktree 路径不同" "[ '$WT_A' != '$WT_B' ]"

# ---- 场景 2: locate-state.sh 按 CWD 正确定位 ----
echo "--- 场景 2: locate-state.sh CWD 定位 ---"
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
# setup 的 slug 生成规则是 <timestamp>-<slug>，timestamp 天然不同；真正会撞的是 __main__
# 所以单独测试 bare 模式的 slug 冲突
echo "--- 场景 4: bare loop slug 冲突被拒 ---"
bash "$SETUP_SCRIPT" --no-worktree "bare-task" > /tmp/setup-bare1.log 2>&1
assert "__main__ state 生成" "[ -f '$STATE_DIR/__main__.yml' ]"

SECOND_EC=0
bash "$SETUP_SCRIPT" --no-worktree "bare-task-2" > /tmp/setup-bare2.log 2>&1 || SECOND_EC=$?
assert "第二次 bare setup exit=4（被拒）" "[ '$SECOND_EC' -eq 4 ]"

# ---- 场景 4.5: bare setup 并发 flock 竞态 ----
# 两个进程同时抢 __main__，flock 串行化后：只有一个能写入 active state，另一个 exit 4/5
echo "--- 场景 4.5: bare setup 并发竞态（flock）---"
# 先清掉场景 4 留下的 __main__，保证起点干净
rm -f "$STATE_DIR/__main__.yml"
# 两个 bare setup 同时发起
bash "$SETUP_SCRIPT" --no-worktree "race-a" > /tmp/setup-racea.log 2>&1 &
PID_A=$!
bash "$SETUP_SCRIPT" --no-worktree "race-b" > /tmp/setup-raceb.log 2>&1 &
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
echo "--- 场景 5: 孤儿 state gc ---"
# 删除 worktree A 的目录但保留 state A
rm -rf "$WT_A"
git worktree prune 2>/dev/null || true
# 再 setup 一个新 loop，trigger gc
bash "$SETUP_SCRIPT" "task-gamma" > /tmp/setup-gamma.log 2>&1
assert "孤儿 state A 被 gc" "[ ! -f '$STATE_A' ]"
assert "state B 仍存在" "[ -f '$STATE_B' ]"

# ---- 汇总 ----
echo ""
echo "--- 汇总: $PASS PASS / $FAIL FAIL ---"
if [ "$FAIL" -gt 0 ]; then
  echo "❌ FAIL: 有 $FAIL 项未通过" >&2
  exit 1
fi
echo "✅ PASS: 所有并行场景通过"
exit 0
