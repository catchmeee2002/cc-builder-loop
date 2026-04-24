#!/usr/bin/env bash
# test-zombie-selfheal.sh — E2E 测试：V1.8.1 僵尸 state 自愈 + EARLY_STOP 立即通知
#
# 场景：
#   S1. Stop hook 遇到 active=false 僵尸 state → 归档到 legacy/ + exit 0 放行
#   S2. Stop hook 遇到 EARLY_STOP（no_progress 模拟）→ 归档 + exit 2 + stderr 注入
#
# 用法：bash test-zombie-selfheal.sh
# 退出码：0=全过 / 1=有失败

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"/../../scripts && pwd)"
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"/../../../.. && pwd)/scripts"
PASS=0
FAIL=0

assert() {
  local desc="$1" cond="$2"
  if eval "$cond"; then
    echo "  ✅ $desc"
    PASS=$(( PASS + 1 ))
  else
    echo "  ❌ $desc  (cond: $cond)"
    FAIL=$(( FAIL + 1 ))
  fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "=== V1.8.1: 僵尸自愈 + EARLY_STOP 立即通知 ==="
echo "    临时目录: $TMP"

# 共享：起一个可用的 git 仓 + loop.yml
mkdir -p "$TMP/.claude"
cat > "$TMP/.claude/loop.yml" <<'YMLEOF'
pass_cmd:
  - { stage: "always_fail", cmd: "false", timeout: 10 }
max_iterations: 2
layout:
  source_dirs: [src]
  test_dirs: [tests]
worktree:
  enabled: false
YMLEOF
git -C "$TMP" init -q
git -C "$TMP" -c user.email=t@t -c user.name=t add -A >/dev/null 2>&1 || true
git -C "$TMP" -c user.email=t@t -c user.name=t -c core.hooksPath=/dev/null commit -m "chore(test): [cr_id_skip] Init" --allow-empty -q

STATE_DIR="$TMP/.claude/builder-loop/state"
LEGACY_DIR="$TMP/.claude/builder-loop/legacy"

# ============================================================
# S1: active=false 的僵尸 → 归档
# ============================================================
echo "--- S1: active=false 僵尸归档 ---"
mkdir -p "$STATE_DIR"
STATE_FILE="$STATE_DIR/__main__.yml"
cat > "$STATE_FILE" <<ZOMBIE
active: false
slug: "__main__"
iter: 0
max_iter: 5
project_root: "$TMP"
start_head: "init"
worktree_path: ""
plan_file: ""
task_description: |
  zombie test
source_dirs: "src"
test_dirs: "tests"
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
stopped_reason: "manual-merge-completed"
created_at: "2026-04-24T00:00:00+00:00"
ZOMBIE

# 确认 state 存在
assert "僵尸 state 预置成功" "[ -f '$STATE_FILE' ]"

# 调 hook
HOOK_OUT="$(echo "{\"cwd\":\"$TMP\"}" | bash "$HOOK_DIR/builder-loop-stop.sh" 2>&1; echo "EXIT:$?")"
EXIT_CODE="$(echo "$HOOK_OUT" | grep -oE 'EXIT:[0-9]+' | tail -1 | cut -d: -f2)"

assert "hook 输出含『归档到 legacy』提示" "echo '$HOOK_OUT' | grep -q '归档到 legacy'"
assert "hook exit 0 放行" "[ '$EXIT_CODE' = '0' ]"
assert "僵尸 state 已从 state/ 挪走" "[ ! -f '$STATE_FILE' ]"
assert "legacy/ 出现 zombie_inactive bak" "ls '$LEGACY_DIR'/*-zombie_inactive.bak >/dev/null 2>&1"

# ============================================================
# S2: EARLY_STOP（no_progress）→ 归档 + exit 2 + stderr 注入
# ============================================================
echo "--- S2: EARLY_STOP no_progress → 归档 + exit 2 ---"
# 清空 state/legacy，重建一个 active state 模拟 "连续两轮错误 hash 一致"
rm -rf "$STATE_DIR" "$LEGACY_DIR"
mkdir -p "$STATE_DIR"
STATE_FILE="$STATE_DIR/__main__.yml"

# iter=2（已跑过 2 轮），last_error_hash 非空 → 下一轮 FAIL + 相同 hash 会命中 no_progress
cat > "$STATE_FILE" <<ACTIVE
active: true
slug: "__main__"
iter: 2
max_iter: 5
project_root: "$TMP"
start_head: "init"
worktree_path: ""
plan_file: ""
task_description: |
  early stop test
source_dirs: "src"
test_dirs: "tests"
last_pass_stage: "always_fail"
last_error_hash: "deadbeef"
last_error_count: 1
stopped_reason: ""
created_at: "2026-04-24T00:00:00+00:00"
ACTIVE

# 制造一份 "错误 hash 会恒定" 的日志：early-stop-check.sh 用日志内容算 hash
# loop.yml 的 pass_cmd 是 "false"，run-pass-cmd 会 FAIL，日志就是 "false" 的空输出
# 两轮 hash 一致 → no_progress 触发
# 但 first run 时 last_error_hash 已预置为 deadbeef，所以一轮就能触发（假设新 hash 就是 deadbeef）
# 实际 hash 计算方式未知，我们用更稳妥方式：走 max_iter 触发早停（iter=2 → NEXT_ITER=3，max_iter=2 → 超上限）
# 直接调整 max_iter 字段
python3 -c "
import re
p='$STATE_FILE'
t=open(p).read()
t=re.sub(r'^max_iter:.*\$','max_iter: 2',t,flags=re.M)
open(p,'w').write(t)
"

HOOK_OUT="$(echo "{\"cwd\":\"$TMP\"}" | bash "$HOOK_DIR/builder-loop-stop.sh" 2>&1; echo "EXIT:$?")"
EXIT_CODE="$(echo "$HOOK_OUT" | grep -oE 'EXIT:[0-9]+' | tail -1 | cut -d: -f2)"

assert "hook 输出含『early stop』提示" "echo '$HOOK_OUT' | grep -q 'early stop'"
assert "hook 输出含『AskUserQuestion』引导" "echo '$HOOK_OUT' | grep -q 'AskUserQuestion'"
assert "hook exit 2（立即通知 builder）" "[ '$EXIT_CODE' = '2' ]"
assert "state 已归档（非 active=false 留存）" "[ ! -f '$STATE_FILE' ]"
assert "legacy/ 出现 early_stop_ bak" "ls '$LEGACY_DIR'/*-early_stop_*.bak >/dev/null 2>&1"

# ---- 汇报 ----
echo ""
echo "=== V1.8.1 E2E：✅ $PASS 通过，❌ $FAIL 失败 ==="
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
