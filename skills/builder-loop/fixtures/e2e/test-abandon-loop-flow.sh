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

set -uo pipefail   # 注：本 fixture 用 run_capture 显式捕获非 0 退出码，不开 -e 防 case 间串味

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# .../skills/builder-loop/fixtures/e2e → 仓库根（或 worktree 根）
REPO_ROOT="$(cd "$THIS_DIR/../../../.." && pwd)"
ABANDON_SCRIPT="${REPO_ROOT}/skills/builder-loop/scripts/abandon-loop.sh"
REVIEWER_HOOK="${REPO_ROOT}/scripts/reviewer-timing-check.sh"

PASS=0
FAIL=0

assert() {
  local desc="$1" cond="$2"
  if eval "$cond"; then echo "  ✅ $desc"; PASS=$(( PASS + 1 ));
  else echo "  ❌ $desc (cond: $cond)"; FAIL=$(( FAIL + 1 )); fi
}

# 捕获命令的 stdout+stderr 与 exit code（不依赖 set -e 行为）
# 用法：run_capture VAR_OUT VAR_EC cmd args...
# 注：assert "echo '$VAR' | grep -q ..." 的 eval 路径在 VAR 含单引号时会 syntax error。
#     当前 abandon-loop.sh 输出不含单引号；若未来改输出含单引号，断言改为 grep -q ... <<< "$VAR"
run_capture() {
  local _vout="$1" _vec="$2"; shift 2
  local _out _ec
  _out="$("$@" 2>&1)"; _ec=$?
  printf -v "$_vout" '%s' "$_out"
  printf -v "$_vec" '%s' "$_ec"
}

write_state() {
  # $1=state path, $2=active, $3=worktree_mode, $4=worktree_path, $5=stash_ref, $6=main_repo
  mkdir -p "$(dirname "$1")"
  cat > "$1" <<EOF
active: $2
slug: "test-abandon-fixture"
iter: 0
max_iter: 5
project_root: "${4:-$6}"
main_repo_path: "$6"
worktree_path: "$4"
worktree_mode: "$3"
pre_loop_stash_ref: "$5"
pre_loop_dirty_files: ""
plan_file: ""
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

echo "=== E2E: V2.6 abandon-loop.sh ==="
echo "    abandon 脚本：$ABANDON_SCRIPT"
echo "    reviewer hook：$REVIEWER_HOOK"
assert "abandon-loop.sh 存在且可执行" "[ -x '$ABANDON_SCRIPT' ]"
assert "reviewer-timing-check.sh 存在" "[ -f '$REVIEWER_HOOK' ]"

# ============================================================
# 准备：临时主仓（含 git init），用于跑各 case
# ============================================================
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "    临时主仓：$TMP"
git -C "$TMP" init -q
git -C "$TMP" config user.email "e2e@test.local"
git -C "$TMP" config user.name "e2e-test"
mkdir -p "$TMP/src" "$TMP/.claude"
echo "init" > "$TMP/src/init.txt"
# locate-state.sh 策略 1 要求 .claude/loop.yml 锚定 PROJECT_ROOT（A10 reviewer hook 集成必需）
cat > "$TMP/.claude/loop.yml" <<'YML'
pass_cmd:
  - stage: smoke
    cmd: "true"
    timeout: 10
max_iterations: 3
YML
git -C "$TMP" add . && git -C "$TMP" commit -q -m "chore(test): [cr_id_skip] Init"

STATE_DIR="$TMP/.claude/builder-loop/state"
LEGACY_DIR="$TMP/.claude/builder-loop/legacy"
mkdir -p "$STATE_DIR"

# ============================================================
# A1: reason 必填 — 无参数 / 仅传 state
# ============================================================
echo "--- A1: reason 必填 ---"
write_state "$STATE_DIR/a1.yml" "true" "clean" "" "" "$TMP"
run_capture A1_OUT A1_EC bash "$ABANDON_SCRIPT"
assert "无参数 → exit 2" "[ '$A1_EC' -eq 2 ]"
assert "无参数 stderr 含用法提示" "echo '$A1_OUT' | grep -q '用法：'"
run_capture A1B_OUT A1B_EC bash "$ABANDON_SCRIPT" "$STATE_DIR/a1.yml"
assert "仅 state 无 reason → exit 2" "[ '$A1B_EC' -eq 2 ]"
assert "仅 state stderr 含用法提示" "echo '$A1B_OUT' | grep -q '用法：'"

# ============================================================
# A2: state 不存在
# ============================================================
echo "--- A2: state 文件不存在 ---"
run_capture A2_OUT A2_EC bash "$ABANDON_SCRIPT" "$STATE_DIR/nonexistent.yml" "test"
assert "不存在 state → exit 2" "[ '$A2_EC' -eq 2 ]"
assert "stderr 含 'state file 不存在'" "echo '$A2_OUT' | grep -q 'state file 不存在'"

# ============================================================
# A3: reason 仅空白
# ============================================================
echo "--- A3: reason 仅空白 ---"
write_state "$STATE_DIR/a3.yml" "true" "clean" "" "" "$TMP"
run_capture A3_OUT A3_EC bash "$ABANDON_SCRIPT" "$STATE_DIR/a3.yml" "   "
assert "纯空白 reason → exit 2" "[ '$A3_EC' -eq 2 ]"
assert "stderr 含 'reason 必填'" "echo '$A3_OUT' | grep -q 'reason 必填'"
assert "state 仍未归档" "[ -f '$STATE_DIR/a3.yml' ]"

# ============================================================
# A4: state.active=false 拒绝
# ============================================================
echo "--- A4: state.active=false 拒绝 ---"
write_state "$STATE_DIR/a4.yml" "false" "clean" "" "" "$TMP"
run_capture A4_OUT A4_EC bash "$ABANDON_SCRIPT" "$STATE_DIR/a4.yml" "test inactive"
assert "active=false → exit 2" "[ '$A4_EC' -eq 2 ]"
assert "stderr 含 '非活跃'" "echo '$A4_OUT' | grep -q '非活跃'"
assert "inactive state 未归档" "[ -f '$STATE_DIR/a4.yml' ]"

# ============================================================
# A5: dirty + valid stash → 成功 abandon
# ============================================================
echo "--- A5: dirty 模式 + 有效 stash → 成功 abandon ---"
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
git -C "$WT_PATH_A5" add . && git -C "$WT_PATH_A5" commit -q -m "chore(test): [cr_id_skip] Wt init"

write_state "$STATE_DIR/a5.yml" "true" "dirty" "$WT_PATH_A5" "$STASH_REF" "$TMP"

run_capture A5_OUT A5_EC bash "$ABANDON_SCRIPT" "$STATE_DIR/a5.yml" "baseline broken not my fault"
assert "dirty abandon → exit 0" "[ '$A5_EC' -eq 0 ]"
assert "stdout 含 'Loop abandoned'" "echo '$A5_OUT' | grep -q 'Loop abandoned'"
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
assert "stash 已 apply（src/dirty.txt 在 unstaged）" "git -C '$TMP' status --porcelain 2>/dev/null | grep -q 'dirty.txt'"
assert "loop-trace.jsonl 含 ABANDON event" "grep -q '\"event\": \"ABANDON\"' '$TMP/.claude/loop-trace.jsonl'"

# 清理 stash 副本 + 主仓 dirty（避免后续 case 串味）
git -C "$TMP" stash drop stash@{0} 2>/dev/null || true
git -C "$TMP" reset --hard HEAD 2>/dev/null >/dev/null
git -C "$TMP" clean -fd 2>/dev/null >/dev/null

# ============================================================
# A6: clean 模式 → 成功 abandon + 不还原 stash
# ============================================================
echo "--- A6: clean 模式 → 不还原 stash ---"
WT_PATH_A6="$TMP/.claude/worktrees/1777647748-test-a6"
mkdir -p "$WT_PATH_A6"
write_state "$STATE_DIR/a6.yml" "true" "clean" "$WT_PATH_A6" "" "$TMP"
run_capture A6_OUT A6_EC bash "$ABANDON_SCRIPT" "$STATE_DIR/a6.yml" "clean test"
assert "clean abandon → exit 0" "[ '$A6_EC' -eq 0 ]"
assert "state 已归档" "[ ! -f '$STATE_DIR/a6.yml' ]"
LEGACY_INFO_A6="$(ls "$LEGACY_DIR"/*-abandon_clean_test.info 2>/dev/null | head -1)"
assert "info 含 stash_restored: no" "grep -q 'stash_restored: no' '$LEGACY_INFO_A6'"
assert "stdout 不含 stash 还原行" "! echo '$A6_OUT' | grep -q '主仓 dirty 已从 stash 还原'"

# ============================================================
# A7: bare 模式（worktree_path 空）
# ============================================================
echo "--- A7: bare 模式 ---"
write_state "$STATE_DIR/a7.yml" "true" "bare" "" "" "$TMP"
run_capture A7_OUT A7_EC bash "$ABANDON_SCRIPT" "$STATE_DIR/a7.yml" "bare test"
assert "bare abandon → exit 0" "[ '$A7_EC' -eq 0 ]"
assert "stdout 显示 worktree: <bare 模式>" "echo '$A7_OUT' | grep -q '<bare 模式 / 已不存在>'"
assert "state 已归档" "[ ! -f '$STATE_DIR/a7.yml' ]"

# ============================================================
# A8: stash hash 无效 → conflict 提示 + 仍归档
# ============================================================
echo "--- A8: stash hash 无效 → conflict 但仍归档 ---"
WT_PATH_A8="$TMP/.claude/worktrees/1777647748-test-a8"
mkdir -p "$WT_PATH_A8"
write_state "$STATE_DIR/a8.yml" "true" "dirty" "$WT_PATH_A8" "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef" "$TMP"
run_capture A8_OUT A8_EC bash "$ABANDON_SCRIPT" "$STATE_DIR/a8.yml" "invalid stash"
assert "invalid stash → 仍 exit 0（不阻断）" "[ '$A8_EC' -eq 0 ]"
assert "stderr 含 stash apply 失败提示" "echo '$A8_OUT' | grep -q 'stash apply 失败'"
LEGACY_INFO_A8="$(ls "$LEGACY_DIR"/*-abandon_invalid_stash.info 2>/dev/null | head -1)"
assert "info 含 stash_restored: conflict" "grep -q 'stash_restored: conflict' '$LEGACY_INFO_A8'"
assert "state 已归档" "[ ! -f '$STATE_DIR/a8.yml' ]"

# ============================================================
# A9: 重复调 abandon
# ============================================================
echo "--- A9: 重复调 abandon ---"
write_state "$STATE_DIR/a9.yml" "true" "clean" "" "" "$TMP"
bash "$ABANDON_SCRIPT" "$STATE_DIR/a9.yml" "first call" >/dev/null 2>&1
run_capture A9_2_OUT A9_2_EC bash "$ABANDON_SCRIPT" "$STATE_DIR/a9.yml" "second call"
assert "重复调 → exit 2（state 已不在）" "[ '$A9_2_EC' -eq 2 ]"
assert "stderr 含 'state file 不存在'" "echo '$A9_2_OUT' | grep -q 'state file 不存在'"

# ============================================================
# A10: hook 集成 — abandon 后 reviewer-timing-check.sh 应放行
# ============================================================
echo "--- A10: abandon 后 reviewer hook 自动放行 ---"
WT_PATH_A10="$TMP/.claude/worktrees/1777647748-test-a10"
mkdir -p "$WT_PATH_A10"
git -C "$WT_PATH_A10" init -q
git -C "$WT_PATH_A10" config user.email "e2e@test.local"
git -C "$WT_PATH_A10" config user.name "e2e-test"
echo "wt-a10" > "$WT_PATH_A10/wt.txt"
git -C "$WT_PATH_A10" add . && git -C "$WT_PATH_A10" commit -q -m "chore(test): [cr_id_skip] Wt-a10 init"

# A10 reviewer hook 集成验证。依赖 locate-state.sh 路径解析的两个前提：
#   1. cwd（$TMP）必须含 .claude/loop.yml — 策略 1 锚定 PROJECT_ROOT
#      （fixture 顶部 init 段已写入；漏写则 hook fallthrough exit 0 → A10 BEFORE 假阳性 flaky）
#   2. 临时仓 .claude/builder-loop/state/<slug>.yml 必须 active=true + worktree_path 目录存活
#      — 策略 5 唯一 active worktree 自动绑定（V2.4）
# 任一前提失效则 hook 走 exit 0 放行 → BEFORE 断言 flaky。下方 write_state + WT_PATH_A10 mkdir
# 是处理前提 (2) 的实现。
write_state "$STATE_DIR/a10.yml" "true" "clean" "$WT_PATH_A10" "" "$TMP"

# 在 worktree 的 .claude/builder-loop/state 下也放一份（locate-state 策略 1 查的是 CWD）
mkdir -p "$WT_PATH_A10/.claude/builder-loop/state"

# Step 1: abandon 之前 reviewer hook 拦截
HOOK_INPUT='{"tool_input":{"subagent_type":"reviewer"}}'
run_capture HOOK_BEFORE_OUT HOOK_BEFORE_EC bash -c "cd '$TMP' && echo '$HOOK_INPUT' | bash '$REVIEWER_HOOK'"
assert "abandon 之前 reviewer hook 拦截 (exit 2)" "[ '$HOOK_BEFORE_EC' -eq 2 ]"

# Step 2: abandon
bash "$ABANDON_SCRIPT" "$STATE_DIR/a10.yml" "hook integration test" >/dev/null 2>&1

# Step 3: abandon 后再调 reviewer hook → 应放行
run_capture HOOK_AFTER_OUT HOOK_AFTER_EC bash -c "cd '$TMP' && echo '$HOOK_INPUT' | bash '$REVIEWER_HOOK'"
assert "abandon 之后 reviewer hook 放行 (exit 0)" "[ '$HOOK_AFTER_EC' -eq 0 ]"

# ============================================================
# A11: reason 含换行符 → .info 的 reason_raw 单行（\n flatten 成空格）
# ============================================================
echo "--- A11: reason 含换行符 → info reason_raw 不被换行破坏 ---"
WT_PATH_A11="$TMP/.claude/worktrees/1777647748-test-a11"
mkdir -p "$WT_PATH_A11"
write_state "$STATE_DIR/a11.yml" "true" "clean" "$WT_PATH_A11" "" "$TMP"
# 用 printf 构造含真实换行的 reason（不走 bash $'...' 以免单引号与 assert eval 冲突）
REASON_A11="$(printf 'line1\nline2')"
run_capture A11_OUT A11_EC bash "$ABANDON_SCRIPT" "$STATE_DIR/a11.yml" "$REASON_A11"
assert "换行 reason → exit 0" "[ '$A11_EC' -eq 0 ]"
assert "换行 reason → state 已归档" "[ ! -f '$STATE_DIR/a11.yml' ]"
LEGACY_INFO_A11="$(ls "$LEGACY_DIR"/*-abandon_line1_line2.info 2>/dev/null | head -1)"
assert "换行 reason → legacy .info 存在（spaces flatten）" "[ -f '$LEGACY_INFO_A11' ]"
# reason_raw 字段必须在同一行（不能有裸换行导致字段值溢出到下一行）
# 用 grep -c 确保只有一行匹配（多行说明 flatten 失效）
REASON_RAW_LINES="$(grep -c 'reason_raw:' "$LEGACY_INFO_A11" 2>/dev/null || echo 0)"
assert "info reason_raw 只出现一行（无多行溢出）" "[ '$REASON_RAW_LINES' -eq 1 ]"

# ============================================================
# A12: reason 含单引号/反引号/$ → trace NDJSON 合法 + info 不被 shell 展开
# ============================================================
echo "--- A12: reason 含特殊字符 → trace JSON 合法 + info reason_raw 安全 ---"
WT_PATH_A12="$TMP/.claude/worktrees/1777647748-test-a12"
mkdir -p "$WT_PATH_A12"
write_state "$STATE_DIR/a12.yml" "true" "clean" "$WT_PATH_A12" "" "$TMP"
# 故意使用含 单引号/反引号/dollar 的 reason（全部是原始字面量，不展开）
REASON_A12="it's done: backtick-cmd and dollar-sign"
run_capture A12_OUT A12_EC bash "$ABANDON_SCRIPT" "$STATE_DIR/a12.yml" "$REASON_A12"
assert "特殊字符 reason → exit 0" "[ '$A12_EC' -eq 0 ]"
assert "特殊字符 reason → state 已归档" "[ ! -f '$STATE_DIR/a12.yml' ]"
# 验证 trace jsonl 最后一条 ABANDON event 是合法 JSON
TRACE_VALID="$(tail -1 "$TMP/.claude/loop-trace.jsonl" 2>/dev/null | python3 -c "import json,sys; json.load(sys.stdin); print('ok')" 2>/dev/null || echo "invalid")"
assert "trace jsonl ABANDON event 是合法 JSON" "[ '$TRACE_VALID' = 'ok' ]"
# 文件名 sanitize：单引号→'_', 冒号→'__', 空格→'_'
# "it's done: backtick-cmd and dollar-sign" → "it_s_done__backtick_cmd_and_dollar_sign"
LEGACY_INFO_A12="$(ls "$LEGACY_DIR"/*-abandon_it_s_done__backtick_cmd_and_dollar_sign.info 2>/dev/null | head -1)"
assert "特殊字符 reason → legacy .info 存在" "[ -f '$LEGACY_INFO_A12' ]"

# ============================================================
# A13: stash_ref 含尾部空格 → read_field strip 生效 → stash apply 成功
# ============================================================
echo "--- A13: stash_ref 含尾部空格 → strip 后 stash apply 成功 ---"
# 创建真实 stash（需要有未 commit 改动）
echo "trailing-space dirty" > "$TMP/src/trailing-dirty.txt"
git -C "$TMP" add "src/trailing-dirty.txt"
git -C "$TMP" stash push -u -m 'builder-loop:auto:slug=a13:ts=9999' >/dev/null 2>&1
STASH_REF_A13="$(git -C "$TMP" rev-parse 'stash@{0}^{commit}' 2>/dev/null)"
assert "A13 stash 创建成功（有有效 hash）" "[ -n '$STASH_REF_A13' ]"

WT_PATH_A13="$TMP/.claude/worktrees/1777647748-test-a13"
mkdir -p "$WT_PATH_A13"
# 关键：state 里 stash_ref 写带尾部空格（手动写 cat，绕过 write_state 的 shellvar 展开）
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
plan_file: ""
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
assert "尾部空格 stash_ref → exit 0（strip 后 apply 成功）" "[ '$A13_EC' -eq 0 ]"
assert "state 已归档" "[ ! -f '$STATE_DIR/a13.yml' ]"
LEGACY_INFO_A13="$(ls "$LEGACY_DIR"/*-abandon_trailing_stash_ref_test.info 2>/dev/null | head -1)"
assert "info 含 stash_restored: yes（strip 后 hash 有效）" "grep -q 'stash_restored: yes' '$LEGACY_INFO_A13'"
# stash apply 后主仓工作树应能看到 trailing-dirty.txt
assert "stash apply 还原 dirty 文件可见" "git -C '$TMP' status --porcelain 2>/dev/null | grep -q 'trailing-dirty.txt'"

# 清理 A13 残留（避免后续影响）
git -C "$TMP" stash drop stash@{0} 2>/dev/null || true
git -C "$TMP" reset --hard HEAD 2>/dev/null >/dev/null
git -C "$TMP" clean -fd 2>/dev/null >/dev/null

# ============================================================
# 汇总
# ============================================================
echo ""
echo "=== 结果：PASS=$PASS / FAIL=$FAIL ==="
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
