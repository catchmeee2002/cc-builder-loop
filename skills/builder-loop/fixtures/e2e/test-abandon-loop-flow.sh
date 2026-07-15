#!/usr/bin/env bash
# test-abandon-loop-flow.sh — E2E：V2.6 abandon-loop.sh 主路径 + 边界 case
#
# V2.6 行为：
#   - abandon-loop.sh <state> <reason> → 归档 state + 还原 stash + 保留 worktree
#   - reason 必填、state.active=true 才允许 abandon
#   - 与 EARLY_STOP 路径对称：复用 V1.8.1 archive_to_legacy + V2.3 stash 还原 + V1.5 trace
#   - hook 自动放行：state 归档后 reviewer-timing-check.sh 找不到 active state → 放行
#
# 覆盖 case：
#   A1: reason 必填 — 无 reason / 空 reason → exit 2
#   A2: state 文件不存在 → exit 2
#   A3: reason 仅空白 → exit 2
#   A4: state.active=false（zombie）→ exit 2 拒绝（防二次归档）
#   A5: dirty 模式 + valid stash → 成功 abandon + state 归档 + stash 还原 + worktree 保留 + legacy info 字段
#   A6: clean 模式 → 成功 abandon + state 归档 + 不还原 stash
#   A7: bare 模式（worktree_path 空）→ 成功 abandon + stdout 显示 "<bare>"
#   A8: stash hash 无效 → STASH_RESTORED=conflict + stderr 提示 + 仍归档（不阻断）
#   A9: 重复调 abandon → 第二次 exit 2（state 已不在）
#   A10: hook 集成 — abandon 后 reviewer-timing-check.sh 找不到 active state → exit 0 放行
#
# 用法：bash test-abandon-loop-flow.sh
# 退出码：0=全部通过 / 1=有失败

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "V2.6 abandon-loop.sh"

ABANDON_SCRIPT="${HARNESS_REPO_ROOT}/skills/builder-loop/scripts/abandon-loop.sh"
REVIEWER_HOOK="${HARNESS_REPO_ROOT}/scripts/reviewer-timing-check.sh"

assert "abandon-loop.sh 存在且可执行" "[ -x '$ABANDON_SCRIPT' ]"
assert "reviewer-timing-check.sh 存在" "[ -f '$REVIEWER_HOOK' ]"

# 捕获命令的 stdout+stderr 与 exit code（不依赖 set -e 行为）
run_capture() {
  local _vout="$1" _vec="$2"; shift 2
  local _out _ec
  _out="$("$@" 2>&1)"; _ec=$?
  printf -v "$_vout" '%s' "$_out"
  printf -v "$_vec" '%s' "$_ec"
}

write_state() {
  # $1=state path, $2=phase, $3=worktree_mode, $4=worktree_path, $5=stash_ref, $6=main_repo
  mkdir -p "$(dirname "$1")"
  cat > "$1" <<EOF
phase: "$2"
slug: "test-abandon-fixture"
iter: 0
max_iter: 5
project_root: "${4:-$6}"
main_repo_path: "$6"
worktree_path: "$4"
worktree_mode: "$3"
pre_loop_stash_ref: "$5"
pre_loop_dirty_files: ""
task_description: |
  test fixture
source_dirs: ""
test_dirs: ""
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
stopped_reason: ""
created_at: "1970-01-01T00:00:00+00:00"
EOF
}

# ============================================================
# 准备：临时主仓（含 git init），用于跑各 case
# ============================================================
TMP="$(mktemp -d)"
_HARNESS_TMPDIRS+=("$TMP")
echo "    临时主仓：$TMP"
git -C "$TMP" init -q
git -C "$TMP" config user.email "e2e@test.local"
git -C "$TMP" config user.name "e2e-test"
mkdir -p "$TMP/src" "$TMP/.claude"
echo "init" > "$TMP/src/init.txt"
# locate-state.sh 策略 1 要求 .claude/loop.yml 锚定 PROJECT_ROOT
cat > "$TMP/.claude/loop.yml" <<'YML'
pass_cmd:
  - stage: smoke
    cmd: "true"
    timeout: 10
max_iterations: 3
YML
git -C "$TMP" add . && git -C "$TMP" -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Init"

STATE_DIR="$TMP/.claude/builder-loop/state"
LEGACY_DIR="$TMP/.claude/builder-loop/legacy"
mkdir -p "$STATE_DIR"

# ============================================================
# A1: reason 必填
# ============================================================
section "A1: reason 必填"
write_state "$STATE_DIR/a1.yml" "active" "clean" "" "" "$TMP"
run_capture A1_OUT A1_EC bash "$ABANDON_SCRIPT"
assert "无参数 → exit 2" "[ '$A1_EC' -eq 2 ]"
assert "无参数 stderr 含用法提示" "echo '$A1_OUT' | grep -q '用法：'"
run_capture A1B_OUT A1B_EC bash "$ABANDON_SCRIPT" "$STATE_DIR/a1.yml"
assert "仅 state 无 reason → exit 2" "[ '$A1B_EC' -eq 2 ]"
assert "仅 state stderr 含用法提示" "echo '$A1B_OUT' | grep -q '用法：'"

# ============================================================
# A2: state 不存在
# ============================================================
section "A2: state 文件不存在"
run_capture A2_OUT A2_EC bash "$ABANDON_SCRIPT" "$STATE_DIR/nonexistent.yml" "test"
assert "不存在 state → exit 2" "[ '$A2_EC' -eq 2 ]"
assert "stderr 含 state file 不存在" "echo '$A2_OUT' | grep -q 'state file 不存在'"

# ============================================================
# A3: reason 仅空白
# ============================================================
section "A3: reason 仅空白"
write_state "$STATE_DIR/a3.yml" "active" "clean" "" "" "$TMP"
run_capture A3_OUT A3_EC bash "$ABANDON_SCRIPT" "$STATE_DIR/a3.yml" "   "
assert "纯空白 reason → exit 2" "[ '$A3_EC' -eq 2 ]"
assert "stderr 含 reason 必填" "echo '$A3_OUT' | grep -q 'reason 必填'"
assert "state 仍未归档" "[ -f '$STATE_DIR/a3.yml' ]"

# ============================================================
# A4: state.phase=空 拒绝
# ============================================================
section "A4: state.phase=空 拒绝"
write_state "$STATE_DIR/a4.yml" "" "clean" "" "" "$TMP"
run_capture A4_OUT A4_EC bash "$ABANDON_SCRIPT" "$STATE_DIR/a4.yml" "test inactive"
assert "phase=空 → exit 2" "[ '$A4_EC' -eq 2 ]"
assert "stderr 含 非活跃" "echo '$A4_OUT' | grep -q '非活跃'"
assert "inactive state 未归档" "[ -f '$STATE_DIR/a4.yml' ]"

# ============================================================
# A5: dirty + valid stash → 成功 abandon
# ============================================================
section "A5: dirty 模式 + 有效 stash"
echo "dirty content" > "$TMP/src/dirty.txt"
git -C "$TMP" add "src/dirty.txt"
git -C "$TMP" stash push -u -m 'builder-loop:auto:slug=test:ts=1' >/dev/null 2>&1
STASH_REF="$(git -C "$TMP" rev-parse 'stash@{0}^{commit}' 2>/dev/null)"
assert "stash 创建成功" "[ -n '$STASH_REF' ]"

# 制造 worktree（独立 git 仓模拟）
WT_PATH_A5="$TMP/.claude/worktrees/1777647748-test-a5"
mkdir -p "$WT_PATH_A5"
git -C "$WT_PATH_A5" init -q
git -C "$WT_PATH_A5" config user.email "e2e@test.local"
git -C "$WT_PATH_A5" config user.name "e2e-test"
echo "wt content" > "$WT_PATH_A5/wt-file.txt"
git -C "$WT_PATH_A5" add . && git -C "$WT_PATH_A5" -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Wt init"

write_state "$STATE_DIR/a5.yml" "active" "dirty" "$WT_PATH_A5" "$STASH_REF" "$TMP"

run_capture A5_OUT A5_EC bash "$ABANDON_SCRIPT" "$STATE_DIR/a5.yml" "baseline broken not my fault"
assert "dirty abandon → exit 0" "[ '$A5_EC' -eq 0 ]"
assert "stdout 含 Loop abandoned" "echo '$A5_OUT' | grep -q 'Loop abandoned'"
assert "stdout 含 worktree kept 路径" "echo '$A5_OUT' | grep -q 'worktree kept:'"
assert "state 已 mv 走" "[ ! -f '$STATE_DIR/a5.yml' ]"
LEGACY_BAK_A5="$(ls "$LEGACY_DIR"/*-abandon_baseline_broken_not_my_fault.bak 2>/dev/null | head -1)"
assert "legacy .bak 存在" "[ -f '$LEGACY_BAK_A5' ]"
LEGACY_INFO_A5="$(ls "$LEGACY_DIR"/*-abandon_baseline_broken_not_my_fault.info 2>/dev/null | head -1)"
assert "legacy .info 存在" "[ -f '$LEGACY_INFO_A5' ]"
assert "info 含 reason_raw" "grep -q 'reason_raw: baseline broken not my fault' '$LEGACY_INFO_A5'"
assert "info 含 stash_restored: yes" "grep -q 'stash_restored: yes' '$LEGACY_INFO_A5'"
assert "info 含 worktree_mode: dirty" "grep -q 'worktree_mode: dirty' '$LEGACY_INFO_A5'"
assert "worktree 目录保留" "[ -d '$WT_PATH_A5' ]"
assert "stash 已 apply（dirty.txt 可见）" "git -C '$TMP' status --porcelain 2>/dev/null | grep -q 'dirty.txt'"
assert "loop-trace.jsonl 含 ABANDON event" "grep -q '\"event\": \"ABANDON\"' '$TMP/.claude/loop-trace.jsonl'"

# 清理 stash 副本 + 主仓 dirty
git -C "$TMP" stash drop stash@{0} 2>/dev/null || true
git -C "$TMP" reset --hard HEAD 2>/dev/null >/dev/null
git -C "$TMP" clean -fd 2>/dev/null >/dev/null

# ============================================================
# A6: clean 模式
# ============================================================
section "A6: clean 模式"
WT_PATH_A6="$TMP/.claude/worktrees/1777647748-test-a6"
mkdir -p "$WT_PATH_A6"
write_state "$STATE_DIR/a6.yml" "active" "clean" "$WT_PATH_A6" "" "$TMP"
run_capture A6_OUT A6_EC bash "$ABANDON_SCRIPT" "$STATE_DIR/a6.yml" "clean test"
assert "clean abandon → exit 0" "[ '$A6_EC' -eq 0 ]"
assert "state 已归档" "[ ! -f '$STATE_DIR/a6.yml' ]"
LEGACY_INFO_A6="$(ls "$LEGACY_DIR"/*-abandon_clean_test.info 2>/dev/null | head -1)"
assert "info 含 stash_restored: no" "grep -q 'stash_restored: no' '$LEGACY_INFO_A6'"
assert "stdout 不含 stash 还原行" "! echo '$A6_OUT' | grep -q '主仓 dirty 已从 stash 还原'"

# ============================================================
# A7: bare 模式（worktree_path 空）
# ============================================================
section "A7: bare 模式"
write_state "$STATE_DIR/a7.yml" "active" "bare" "" "" "$TMP"
run_capture A7_OUT A7_EC bash "$ABANDON_SCRIPT" "$STATE_DIR/a7.yml" "bare test"
assert "bare abandon → exit 0" "[ '$A7_EC' -eq 0 ]"
assert "stdout 显示 <bare 模式>" "echo '$A7_OUT' | grep -q '<bare 模式 / 已不存在>'"
assert "state 已归档" "[ ! -f '$STATE_DIR/a7.yml' ]"

# ============================================================
# A8: stash hash 无效
# ============================================================
section "A8: stash hash 无效 → conflict 但仍归档"
WT_PATH_A8="$TMP/.claude/worktrees/1777647748-test-a8"
mkdir -p "$WT_PATH_A8"
write_state "$STATE_DIR/a8.yml" "active" "dirty" "$WT_PATH_A8" "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef" "$TMP"
run_capture A8_OUT A8_EC bash "$ABANDON_SCRIPT" "$STATE_DIR/a8.yml" "invalid stash"
assert "invalid stash → 仍 exit 0（不阻断）" "[ '$A8_EC' -eq 0 ]"
assert "stderr 含 stash apply 失败提示" "echo '$A8_OUT' | grep -q 'stash apply 失败'"
LEGACY_INFO_A8="$(ls "$LEGACY_DIR"/*-abandon_invalid_stash.info 2>/dev/null | head -1)"
assert "info 含 stash_restored: conflict" "grep -q 'stash_restored: conflict' '$LEGACY_INFO_A8'"
assert "state 已归档" "[ ! -f '$STATE_DIR/a8.yml' ]"

# ============================================================
# A9: 重复调 abandon
# ============================================================
section "A9: 重复调 abandon"
write_state "$STATE_DIR/a9.yml" "active" "clean" "" "" "$TMP"
bash "$ABANDON_SCRIPT" "$STATE_DIR/a9.yml" "first call" >/dev/null 2>&1
run_capture A9_2_OUT A9_2_EC bash "$ABANDON_SCRIPT" "$STATE_DIR/a9.yml" "second call"
assert "重复调 → exit 2（state 已不在）" "[ '$A9_2_EC' -eq 2 ]"
assert "stderr 含 state file 不存在" "echo '$A9_2_OUT' | grep -q 'state file 不存在'"

# ============================================================
# A10: hook 集成
# ============================================================
section "A10: abandon 后 reviewer hook 自动放行"
WT_PATH_A10="$TMP/.claude/worktrees/1777647748-test-a10"
mkdir -p "$WT_PATH_A10"
git -C "$WT_PATH_A10" init -q
git -C "$WT_PATH_A10" config user.email "e2e@test.local"
git -C "$WT_PATH_A10" config user.name "e2e-test"
echo "wt-a10" > "$WT_PATH_A10/wt.txt"
git -C "$WT_PATH_A10" add . && git -C "$WT_PATH_A10" -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Wt-a10 init"

A10_SLUG="1777647748-test-a10"
write_state "$STATE_DIR/${A10_SLUG}.yml" "active" "clean" "$WT_PATH_A10" "" "$TMP"

# Step 1: abandon 之前 reviewer hook 拦截
HOOK_INPUT='{"tool_input":{"subagent_type":"reviewer"}}'
run_capture HOOK_BEFORE_OUT HOOK_BEFORE_EC bash -c "cd '$WT_PATH_A10' && echo '$HOOK_INPUT' | bash '$REVIEWER_HOOK'"
assert "abandon 之前 reviewer hook 拦截 (exit 2)" "[ '$HOOK_BEFORE_EC' -eq 2 ]"

# Step 2: abandon
bash "$ABANDON_SCRIPT" "$STATE_DIR/${A10_SLUG}.yml" "hook integration test" >/dev/null 2>&1

# Step 3: abandon 后再调 reviewer hook → 应放行
run_capture HOOK_AFTER_OUT HOOK_AFTER_EC bash -c "cd '$WT_PATH_A10' && echo '$HOOK_INPUT' | bash '$REVIEWER_HOOK'"
assert "abandon 之后 reviewer hook 放行 (exit 0)" "[ '$HOOK_AFTER_EC' -eq 0 ]"

# ============================================================
# A11: reason 含换行符
# ============================================================
section "A11: reason 含换行符 → info reason_raw 不被换行破坏"
WT_PATH_A11="$TMP/.claude/worktrees/1777647748-test-a11"
mkdir -p "$WT_PATH_A11"
write_state "$STATE_DIR/a11.yml" "active" "clean" "$WT_PATH_A11" "" "$TMP"
REASON_A11="$(printf 'line1\nline2')"
run_capture A11_OUT A11_EC bash "$ABANDON_SCRIPT" "$STATE_DIR/a11.yml" "$REASON_A11"
assert "换行 reason → exit 0" "[ '$A11_EC' -eq 0 ]"
assert "换行 reason → state 已归档" "[ ! -f '$STATE_DIR/a11.yml' ]"
LEGACY_INFO_A11="$(ls "$LEGACY_DIR"/*-abandon_line1_line2.info 2>/dev/null | head -1)"
assert "换行 reason → legacy .info 存在" "[ -f '$LEGACY_INFO_A11' ]"
REASON_RAW_LINES="$(grep -c 'reason_raw:' "$LEGACY_INFO_A11" 2>/dev/null || echo 0)"
assert "info reason_raw 只出现一行" "[ '$REASON_RAW_LINES' -eq 1 ]"

# ============================================================
# A12: reason 含特殊字符
# ============================================================
section "A12: reason 含特殊字符 → trace JSON 合法"
WT_PATH_A12="$TMP/.claude/worktrees/1777647748-test-a12"
mkdir -p "$WT_PATH_A12"
write_state "$STATE_DIR/a12.yml" "active" "clean" "$WT_PATH_A12" "" "$TMP"
REASON_A12="it's done: backtick-cmd and dollar-sign"
run_capture A12_OUT A12_EC bash "$ABANDON_SCRIPT" "$STATE_DIR/a12.yml" "$REASON_A12"
assert "特殊字符 reason → exit 0" "[ '$A12_EC' -eq 0 ]"
assert "特殊字符 reason → state 已归档" "[ ! -f '$STATE_DIR/a12.yml' ]"
TRACE_VALID="$(tail -1 "$TMP/.claude/loop-trace.jsonl" 2>/dev/null | python3 -c "import json,sys; json.load(sys.stdin); print('ok')" 2>/dev/null || echo "invalid")"
assert "trace jsonl ABANDON event 是合法 JSON" "[ '$TRACE_VALID' = 'ok' ]"
LEGACY_INFO_A12="$(ls "$LEGACY_DIR"/*-abandon_it_s_done__backtick_cmd_and_dollar_sign.info 2>/dev/null | head -1)"
assert "特殊字符 reason → legacy .info 存在" "[ -f '$LEGACY_INFO_A12' ]"

# ============================================================
# A13: stash_ref 含尾部空格
# ============================================================
section "A13: stash_ref 含尾部空格 → strip 后 stash apply 成功"
echo "trailing-space dirty" > "$TMP/src/trailing-dirty.txt"
git -C "$TMP" add "src/trailing-dirty.txt"
git -C "$TMP" stash push -u -m 'builder-loop:auto:slug=a13:ts=9999' >/dev/null 2>&1
STASH_REF_A13="$(git -C "$TMP" rev-parse 'stash@{0}^{commit}' 2>/dev/null)"
assert "A13 stash 创建成功" "[ -n '$STASH_REF_A13' ]"

WT_PATH_A13="$TMP/.claude/worktrees/1777647748-test-a13"
mkdir -p "$WT_PATH_A13"
mkdir -p "$STATE_DIR"
cat > "$STATE_DIR/a13.yml" <<EOF
active: true
slug: "test-abandon-fixture"
iter: 0
max_iter: 5
project_root: "$WT_PATH_A13"
main_repo_path: "$TMP"
worktree_path: "$WT_PATH_A13"
worktree_mode: "dirty"
pre_loop_stash_ref: "${STASH_REF_A13}  "
pre_loop_dirty_files: "src/trailing-dirty.txt"
task_description: |
  trailing space stash ref test
source_dirs: ""
test_dirs: ""
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
stopped_reason: ""
created_at: "1970-01-01T00:00:00+00:00"
EOF
run_capture A13_OUT A13_EC bash "$ABANDON_SCRIPT" "$STATE_DIR/a13.yml" "trailing stash ref test"
assert "尾部空格 stash_ref → exit 0" "[ '$A13_EC' -eq 0 ]"
assert "state 已归档" "[ ! -f '$STATE_DIR/a13.yml' ]"
LEGACY_INFO_A13="$(ls "$LEGACY_DIR"/*-abandon_trailing_stash_ref_test.info 2>/dev/null | head -1)"
assert "info 含 stash_restored: yes" "grep -q 'stash_restored: yes' '$LEGACY_INFO_A13'"
assert "stash apply 还原 dirty 文件可见" "git -C '$TMP' status --porcelain 2>/dev/null | grep -q 'trailing-dirty.txt'"

# 清理 A13 残留
git -C "$TMP" stash drop stash@{0} 2>/dev/null || true
git -C "$TMP" reset --hard HEAD 2>/dev/null >/dev/null
git -C "$TMP" clean -fd 2>/dev/null >/dev/null

harness_report
