#!/usr/bin/env bash
# test-locate-state-strategy5.sh — E2E：locate-state 全策略验证
#
# V3.4 变更：
#   - 策略 5（唯一 active worktree 自动绑定）已恢复（V3.2 曾删除）
#   - local.md 已移除，locate-state.sh 是唯一定位机制
#
# 覆盖 case：
#   A1: 主仓 cwd + 1 active worktree state（目录存活）→ 策略 5 命中
#   A2: 主仓 cwd + 2 active worktree state → 返回空（保隔离）
#   A3: worktree cwd → 策略 2 命中
#   A4: __main__.yml 存在 → 策略 4 兜底命中
#   A5: 策略 3 worktree_path 匹配 cwd → 命中

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "locate-state-strategy5"

assert_file_exists "locate-state.sh 存在" "$HARNESS_LOCATE"

# 构建共享测试仓库
TMP=$(mktemp -d -t "harness-locate-XXXXXX")
_HARNESS_TMPDIRS+=("$TMP")
(
  cd "$TMP"
  git init -q
  git config user.email "harness@test.local"
  git config user.name "harness"
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
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Locate-state seed"
)

STATE_DIR="$TMP/.claude/builder-loop/state"
mkdir -p "$STATE_DIR"

# Helper: 写 minimal state
write_ls_state() {
  local sf="$1" active="$2" wt_path="$3" slug="$4"
  mkdir -p "$(dirname "$sf")"
  cat > "$sf" <<STEOF
active: $active
slug: "$slug"
phase: "active"
iter: 0
max_iter: 5
project_root: "$wt_path"
main_repo_path: "$TMP"
worktree_path: "$wt_path"
worktree_mode: "clean"
start_head: "fakehead"
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
created_at: "1970-01-01T00:00:00+00:00"
STEOF
}

# ============================================================
# A1: 主仓 cwd + 1 active worktree state → 返回空（策略 5 已删）
# ============================================================
section "A1: 主仓 cwd + 1 active → 策略 5 命中"
WT_A1="$TMP/.claude/worktrees/1700000001-foo"
mkdir -p "$WT_A1"
write_ls_state "$STATE_DIR/1700000001-foo.yml" "true" "$WT_A1" "1700000001-foo"

resultA1=$(run_locate "$TMP")
assert_ec "A1 locate exit 0（策略 5 恢复）" "$resultA1" 0
a1out=$(result_stdout "$resultA1")
assert "A1 stdout = state 文件路径" "[ '$a1out' = '$STATE_DIR/1700000001-foo.yml' ]"
# locate 静默契约
a1err=$(result_stderr "$resultA1")
assert "A1 stderr 为空（静默契约）" "[ -z '$a1err' ]"

rm -f "$STATE_DIR"/*.yml; rm -rf "$TMP/.claude/worktrees"

# ============================================================
# A2: 主仓 cwd + 2 active worktree state → 返回空
# ============================================================
section "A2: 主仓 cwd + 2 active → 返回空"
WT_A2_1="$TMP/.claude/worktrees/1700000002-bar"
WT_A2_2="$TMP/.claude/worktrees/1700000003-baz"
mkdir -p "$WT_A2_1" "$WT_A2_2"
write_ls_state "$STATE_DIR/1700000002-bar.yml" "true" "$WT_A2_1" "1700000002-bar"
write_ls_state "$STATE_DIR/1700000003-baz.yml" "true" "$WT_A2_2" "1700000003-baz"

resultA2=$(run_locate "$TMP")
assert_ec "A2 locate exit 1（多 active 不绑）" "$resultA2" 1
a2out=$(result_stdout "$resultA2")
assert "A2 stdout 空" "[ -z '$a2out' ]"

rm -f "$STATE_DIR"/*.yml; rm -rf "$TMP/.claude/worktrees"

# ============================================================
# A3: cwd 在 worktree 内 → 策略 2 命中
# ============================================================
section "A3: worktree cwd → 策略 2 命中"
WT_A3="$TMP/.claude/worktrees/1700000004-qux"
mkdir -p "$WT_A3"
write_ls_state "$STATE_DIR/1700000004-qux.yml" "true" "$WT_A3" "1700000004-qux"

resultA3=$(run_locate "$WT_A3")
assert_ec "A3 locate exit 0（策略 2 命中）" "$resultA3" 0
a3out=$(result_stdout "$resultA3")
assert "A3 stdout = state 文件路径" "[ '$a3out' = '$STATE_DIR/1700000004-qux.yml' ]"

rm -f "$STATE_DIR"/*.yml; rm -rf "$TMP/.claude/worktrees"

# ============================================================
# A4: __main__.yml 存在 → 策略 4 兜底命中
# ============================================================
section "A4: __main__.yml → 策略 4 兜底"
write_ls_state "$STATE_DIR/__main__.yml" "true" "$TMP" "__main__"

resultA4=$(run_locate "$TMP")
assert_ec "A4 locate exit 0（策略 4 命中）" "$resultA4" 0
a4out=$(result_stdout "$resultA4")
assert "A4 stdout = __main__.yml" "[ '$a4out' = '$STATE_DIR/__main__.yml' ]"

rm -f "$STATE_DIR"/*.yml

# ============================================================
# A5: 策略 3 — worktree_path 匹配 cwd → 命中
# ============================================================
section "A5: worktree_path 匹配 cwd → 策略 3 命中"
WT_A5="$TMP/external-worktree-dir"
mkdir -p "$WT_A5"
write_ls_state "$STATE_DIR/1700000005-ext.yml" "true" "$WT_A5" "1700000005-ext"

resultA5=$(run_locate "$WT_A5")
assert_ec "A5 locate exit 0（策略 3 命中）" "$resultA5" 0
a5out=$(result_stdout "$resultA5")
assert "A5 stdout = state 文件路径" "[ '$a5out' = '$STATE_DIR/1700000005-ext.yml' ]"

rm -f "$STATE_DIR"/*.yml; rm -rf "$WT_A5"

harness_report
