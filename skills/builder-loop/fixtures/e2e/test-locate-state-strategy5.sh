#!/usr/bin/env bash
# test-locate-state-strategy5.sh — E2E：V2.4 locate-state.sh 策略 5 + setup cwd 警告 + stop hook 诊断
#
# V2.4 行为：
#   - locate-state.sh 主仓 cwd + 唯一 active worktree state（worktree_path 目录存活）→ 自动绑定
#   - 多 active / 死 worktree → 仍返回空（保 V1.8 多状态隔离精神）
#   - stop hook 未命中 + PROJECT_ROOT + STATE_DIR 有 active 候选 → 打 stderr 诊断
#   - setup-builder-loop.sh worktree 模式 + OWNER_CWD = 主仓 → 末尾打 cwd 不一致警告
#
# 覆盖 case：
#   A1: 主仓 cwd + 1 active worktree state（目录存活）→ locate 命中策略 5
#   A2: 主仓 cwd + 2 active worktree state → locate 不绑 + stop hook stderr 含诊断
#   A3: 主仓 cwd + 1 active state 但 worktree 目录已删 → locate 不绑（孤儿排除）
#   A4: 真跑 setup-builder-loop.sh + 主仓 cwd → setup stderr 含 cwd 警告 + locate 命中
#
# 用法：bash test-locate-state-strategy5.sh
# 退出码：0=全部通过 / 1=有失败

set -euo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# .../skills/builder-loop/fixtures/e2e → 仓库根
REPO_ROOT="$(cd "$THIS_DIR/../../../.." && pwd)"
LOCATE_SCRIPT="${REPO_ROOT}/skills/builder-loop/scripts/locate-state.sh"
SETUP_SCRIPT="${REPO_ROOT}/skills/builder-loop/scripts/setup-builder-loop.sh"
HOOK_SCRIPT="${REPO_ROOT}/scripts/builder-loop-stop.sh"

PASS=0
FAIL=0

assert() {
  local desc="$1" cond="$2"
  if eval "$cond"; then echo "  ✅ $desc"; PASS=$(( PASS + 1 ));
  else echo "  ❌ $desc (cond: $cond)"; FAIL=$(( FAIL + 1 )); fi
}

write_state_yml() {
  # $1 = state file path, $2 = active (true/false), $3 = worktree_path, $4 = slug
  cat > "$1" <<EOF
active: $2
slug: "$4"
iter: 0
max_iter: 5
project_root: "$3"
main_repo_path: "${TMP}"
worktree_path: "$3"
worktree_mode: "clean"
start_head: "fakehead"
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
created_at: "1970-01-01T00:00:00+00:00"
EOF
}

echo "=== E2E: V2.4 locate-state.sh 策略 5 ==="
echo "    locate 脚本：$LOCATE_SCRIPT"
echo "    setup 脚本：$SETUP_SCRIPT"
echo "    stop hook：$HOOK_SCRIPT"
assert "locate-state.sh 存在" "[ -f '$LOCATE_SCRIPT' ]"
assert "setup-builder-loop.sh 存在" "[ -f '$SETUP_SCRIPT' ]"
assert "builder-loop-stop.sh 存在" "[ -f '$HOOK_SCRIPT' ]"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "    临时仓库：$TMP"

# ---- 初始化最小仓库 + loop.yml ----
echo "--- 初始化仓库 + loop.yml（pass_cmd=true 让 stop hook 后续路径快速 PASS）---"
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
git commit -q -m "chore(test): [cr_id_skip] Initial seed for e2e locate-state strategy5"

STATE_DIR="${TMP}/.claude/builder-loop/state"
mkdir -p "$STATE_DIR"

# ============================================================
# A1: 主仓 cwd + 1 active worktree state（worktree_path 目录存活）→ locate 命中
# ============================================================
echo ""
echo "--- A1: 主仓 cwd + 1 active worktree state（目录存活）→ 命中策略 5 ---"
WT_A1="${TMP}/.claude/worktrees/1700000001-foo"
mkdir -p "$WT_A1"
write_state_yml "${STATE_DIR}/1700000001-foo.yml" "true" "$WT_A1" "1700000001-foo"

A1_OUT="$(bash "$LOCATE_SCRIPT" "$TMP" 2>/dev/null || echo "")"
A1_EXIT=0
bash "$LOCATE_SCRIPT" "$TMP" >/dev/null 2>&1 || A1_EXIT=$?

assert "A1 locate exit 0" "[ $A1_EXIT -eq 0 ]"
assert "A1 stdout = state 文件路径" "[ '$A1_OUT' = '${STATE_DIR}/1700000001-foo.yml' ]"

# A1 顺验：locate 静默契约（不能往 stderr 喷）
A1_STDERR="$(bash "$LOCATE_SCRIPT" "$TMP" 2>&1 >/dev/null || true)"
assert "A1 stderr 为空（locate 静默契约）" "[ -z '$A1_STDERR' ]"

# 清理 A1
rm -f "${STATE_DIR}"/*.yml
rm -rf "${TMP}/.claude/worktrees"

# ============================================================
# A2: 主仓 cwd + 2 active worktree state → 不绑（多 active 保隔离）
#     + stop hook stderr 含诊断段
# ============================================================
echo ""
echo "--- A2: 主仓 cwd + 2 active worktree state → 不绑 + stop hook 诊断 ---"
WT_A2_1="${TMP}/.claude/worktrees/1700000002-bar"
WT_A2_2="${TMP}/.claude/worktrees/1700000003-baz"
mkdir -p "$WT_A2_1" "$WT_A2_2"
write_state_yml "${STATE_DIR}/1700000002-bar.yml" "true" "$WT_A2_1" "1700000002-bar"
write_state_yml "${STATE_DIR}/1700000003-baz.yml" "true" "$WT_A2_2" "1700000003-baz"

A2_OUT="$(bash "$LOCATE_SCRIPT" "$TMP" 2>/dev/null || echo "")"
A2_EXIT=0
bash "$LOCATE_SCRIPT" "$TMP" >/dev/null 2>&1 || A2_EXIT=$?

assert "A2 locate exit 1（多 active 不绑）" "[ $A2_EXIT -eq 1 ]"
assert "A2 stdout 空" "[ -z '$A2_OUT' ]"

# A2 stop hook 诊断：让 hook 跑、抓 stderr 前几行（hook 后续会走 bootstrap → setup，
# 不影响诊断段已经先打出）。timeout 防 setup 卡住整个 fixture。
A2_STDERR_FILE="$(mktemp)"
A2_HOOK_INPUT="$(printf '{"cwd":"%s","transcript_path":""}' "$TMP")"
# timeout 60s：hook 内部还要跑 bootstrap → setup-builder-loop（含 git stash 检测 / worktree add /
# .gitignore 自愈），慢机器 / NFS 上 30s 偏紧（reviewer 反馈对齐其他 stop hook fixture 风格）
timeout 60 bash -c 'printf "%s" "$1" | bash "$2" 2>"$3" >/dev/null || true' \
  _ "$A2_HOOK_INPUT" "$HOOK_SCRIPT" "$A2_STDERR_FILE" || true

assert "A2 stop hook stderr 含诊断 ⚠️ cwd= 段" "grep -qF '⚠️  cwd=' '$A2_STDERR_FILE'"
assert "A2 stop hook stderr 含 worktree path A2_1" "grep -qF '$WT_A2_1' '$A2_STDERR_FILE'"
assert "A2 stop hook stderr 含 worktree path A2_2" "grep -qF '$WT_A2_2' '$A2_STDERR_FILE'"
assert "A2 stop hook stderr 含 cd 提示" "grep -qF '请 cd 到对应 worktree' '$A2_STDERR_FILE'"

rm -f "$A2_STDERR_FILE"
# 清理 A2（含 stop hook 走 bootstrap 可能写入的 state / lock / trace）
rm -rf "${TMP}/.claude/worktrees" "${TMP}/.claude/builder-loop" "${TMP}/.claude/loop-runs" \
       "${TMP}/.claude/loop-trace.jsonl" "${TMP}/.claude/reviewer-params.json" \
       "${TMP}/.claude/reviewer-diff.txt" "${TMP}/.claude/review_reports"
mkdir -p "$STATE_DIR"

# ============================================================
# A3: 主仓 cwd + 1 active state 但 worktree 目录已删 → 不绑（孤儿排除）
# ============================================================
echo ""
echo "--- A3: 1 active state + 死 worktree → 不绑 ---"
WT_A3_DEAD="${TMP}/.claude/worktrees/1700000004-dead-not-created"
write_state_yml "${STATE_DIR}/1700000004-dead.yml" "true" "$WT_A3_DEAD" "1700000004-dead"
# 故意不 mkdir WT_A3_DEAD

A3_OUT="$(bash "$LOCATE_SCRIPT" "$TMP" 2>/dev/null || echo "")"
A3_EXIT=0
bash "$LOCATE_SCRIPT" "$TMP" >/dev/null 2>&1 || A3_EXIT=$?

assert "A3 locate exit 1（死 worktree 不绑）" "[ $A3_EXIT -eq 1 ]"
assert "A3 stdout 空" "[ -z '$A3_OUT' ]"

# A3 顺验：1 active 死 worktree + 1 active 活 worktree → 命中活的（验候选筛选只数活的）
WT_A3_LIVE="${TMP}/.claude/worktrees/1700000005-live"
mkdir -p "$WT_A3_LIVE"
write_state_yml "${STATE_DIR}/1700000005-live.yml" "true" "$WT_A3_LIVE" "1700000005-live"
A3B_OUT="$(bash "$LOCATE_SCRIPT" "$TMP" 2>/dev/null || echo "")"
assert "A3.b 1 死 + 1 活 → 命中活的（死的不计入候选数）" "[ '$A3B_OUT' = '${STATE_DIR}/1700000005-live.yml' ]"

# A3 顺验：active=false 不计入候选
write_state_yml "${STATE_DIR}/1700000006-inactive.yml" "false" "$WT_A3_LIVE" "1700000006-inactive"
A3C_OUT="$(bash "$LOCATE_SCRIPT" "$TMP" 2>/dev/null || echo "")"
assert "A3.c 1 active 活 + 1 inactive → 仍命中 active（inactive 不参与）" "[ '$A3C_OUT' = '${STATE_DIR}/1700000005-live.yml' ]"

# 清理 A3
rm -f "${STATE_DIR}"/*.yml
rm -rf "${TMP}/.claude/worktrees"

# ============================================================
# A4: 真跑 setup-builder-loop.sh + 主仓 cwd → setup stderr 含 cwd 警告 + locate 命中
# ============================================================
echo ""
echo "--- A4: 真跑 setup-builder-loop.sh（worktree 模式）→ 端到端集成 ---"
A4_SETUP_LOG="$(mktemp)"
# setup 调用 cwd = 主仓（模拟 builder 在主仓 cwd 调 setup）
cd "$TMP"
bash "$SETUP_SCRIPT" "test-task-strategy5" >"$A4_SETUP_LOG" 2>>"$A4_SETUP_LOG" || true

assert "A4 setup stderr 含 cwd 警告" "grep -qF 'CC session cwd 仍在主仓' '$A4_SETUP_LOG'"
assert "A4 setup stderr 含「请 cd」提示" "grep -qF 'cd ' '$A4_SETUP_LOG'"
assert "A4 setup 创建了 worktree" "[ -d '${TMP}/.claude/worktrees' ]"

# locate 在主仓 cwd 应能命中策略 5 拿到 setup 创建的 state
A4_LOCATE_OUT="$(bash "$LOCATE_SCRIPT" "$TMP" 2>/dev/null || echo "")"
assert "A4 locate 命中 setup 创建的 state" "[ -n '$A4_LOCATE_OUT' ] && [ -f '$A4_LOCATE_OUT' ]"

# 验证 state 文件是 worktree state（slug 不是 __main__）
A4_SLUG="$(basename "$A4_LOCATE_OUT" .yml)"
assert "A4 state slug 不是 __main__（worktree 模式）" "[ '$A4_SLUG' != '__main__' ]"

rm -f "$A4_SETUP_LOG"

# ============================================================
# 汇总
# ============================================================
echo ""
echo "=========================================="
echo "PASS: $PASS  FAIL: $FAIL"
echo "=========================================="
[ "$FAIL" -eq 0 ]
