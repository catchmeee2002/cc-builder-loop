#!/usr/bin/env bash
# harness.sh — fixture 共享测试框架
#
# 用法：在 fixture 顶部 source 本文件
#   source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
#   harness_init "test-my-feature"
#   env=$(create_test_env --slug "my-test" --worktree --phase active)
#   result=$(run_hook "$env")
#   assert_ec "hook EC=2" "$result" 2
#   harness_report

set -uo pipefail

# ---- 路径常量 ----
HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_REPO_ROOT="$(cd "$HARNESS_DIR/../../../.." && pwd)"
HARNESS_HOOK="${HARNESS_REPO_ROOT}/scripts/builder-loop-stop.sh"
HARNESS_SETUP="${HARNESS_REPO_ROOT}/skills/builder-loop/scripts/setup-builder-loop.sh"
HARNESS_MERGE_CLEANUP="${HARNESS_REPO_ROOT}/skills/builder-loop/scripts/merge-and-cleanup.sh"
HARNESS_LOCATE="${HARNESS_REPO_ROOT}/skills/builder-loop/scripts/locate-state.sh"

# ---- 全局状态 ----
_HARNESS_PASS=0
_HARNESS_FAIL=0
_HARNESS_NAME=""
_HARNESS_TMPDIRS=()

# ==================================================================
# harness_init NAME
# ==================================================================
harness_init() {
  _HARNESS_NAME="${1:-unnamed}"
  _HARNESS_PASS=0
  _HARNESS_FAIL=0
  _HARNESS_TMPDIRS=()
  trap '_harness_cleanup' EXIT
  echo "=== ${_HARNESS_NAME} ==="
}

_harness_cleanup() {
  for _d in "${_HARNESS_TMPDIRS[@]+"${_HARNESS_TMPDIRS[@]}"}"; do
    # V3.7: prune git worktrees before rm to avoid hanging on NFS/lock
    if [ -d "$_d/.git" ] || [ -f "$_d/.git" ]; then
      git -C "$_d" worktree prune 2>/dev/null || true
      for _wt in "$_d"/.claude/worktrees/*/; do
        [ -d "$_wt" ] && git -C "$_d" worktree remove --force "$_wt" 2>/dev/null || true
      done
    fi
    rm -rf "$_d" 2>/dev/null || true
  done
}

# ==================================================================
# create_test_env [flags] — 建临时 git 仓，stdout 输出 project_root
#
# Flags:
#   --slug NAME          slug 名（必填）
#   --worktree           创建 worktree（默认 bare）
#   --phase PHASE        state.phase（默认 active）
#   --dirty FILE1,FILE2  在 worktree/主仓创建 dirty 文件
#   --no-state           不创建 state 文件
#   --pass-cmd CMD       loop.yml pass_cmd（默认 true）
#   --wt-dirty FILE      在 worktree 内创建 dirty 文件（不 commit）
# ==================================================================
create_test_env() {
  local slug="" worktree=0 phase="active" dirty="" no_state=0
  local pass_cmd="true" wt_dirty=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --slug)        slug="$2"; shift 2 ;;
      --worktree)    worktree=1; shift ;;
      --phase)       phase="$2"; shift 2 ;;
      --dirty)       dirty="$2"; shift 2 ;;
      --no-local-md) shift ;;  # V3.4: deprecated no-op, kept for old callers
      --no-state)    no_state=1; shift ;;
      --pass-cmd)    pass_cmd="$2"; shift 2 ;;
      --wt-dirty)    wt_dirty="$2"; shift 2 ;;
      *) echo "harness: unknown flag: $1" >&2; return 1 ;;
    esac
  done
  [ -z "$slug" ] && { echo "harness: --slug required" >&2; return 1; }

  local dir
  dir="$(mktemp -d -t "harness-${slug}-XXXXXX")"
  _HARNESS_TMPDIRS+=("$dir")

  (
    cd "$dir"
    git init -q
    git config user.email "harness@test.local"
    git config user.name "harness"
    mkdir -p .claude
    cat > .claude/loop.yml <<YMLEOF
pass_cmd:
  - stage: smoke
    cmd: "${pass_cmd}"
    timeout: 10
worktree:
  enabled: $([ "$worktree" -eq 1 ] && echo "true" || echo "false")
YMLEOF
    echo "seed" > README.md
    cat > .gitignore <<'GIEOF'
.claude/builder-loop/
.claude/loop-runs/
GIEOF
    git add -A
    git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Harness seed"
  )

  local wt_path=""
  local head1
  head1="$(git -C "$dir" rev-parse --short HEAD)"

  if [ "$worktree" -eq 1 ]; then
    wt_path="$dir/.claude/worktrees/${slug}"
    git -C "$dir" worktree add -q -b "loop/${slug}" "$wt_path" 2>/dev/null
    if [ -n "$wt_dirty" ]; then
      echo "wt-dirty-content" > "$wt_path/$wt_dirty"
    fi
  fi

  if [ -n "$dirty" ]; then
    local IFS=','
    for f in $dirty; do
      echo "dirty-content" >> "$dir/$f"
    done
    unset IFS
  fi

  if [ "$no_state" -eq 0 ]; then
    mkdir -p "$dir/.claude/builder-loop/state"
    local runtime_root="$dir"
    [ -n "$wt_path" ] && runtime_root="$wt_path"
    local wt_head="$head1"
    [ -n "$wt_path" ] && wt_head="$(git -C "$wt_path" rev-parse --short HEAD 2>/dev/null || echo "$head1")"
    cat > "$dir/.claude/builder-loop/state/${slug}.yml" <<STEOF
active: true
phase: "${phase}"
slug: "${slug}"
owner_cwd: "$dir"
iter: 0
max_iter: 5
project_root: "${runtime_root}"
main_repo_path: "$dir"
start_head: "${head1}"
last_iter_head: "${wt_head}"
worktree_path: "${wt_path}"
worktree_mode: "$([ -n "$wt_path" ] && echo "clean" || echo "bare")"
task_description: |
  harness-test
source_dirs: ""
test_dirs: ""
pre_loop_stash_ref: ""
pre_loop_dirty_files: ""
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
stopped_reason: ""
cleanup_phase: ""
created_at: "$(date -Iseconds)"
STEOF
  fi

  echo "$dir"
}

# ==================================================================
# run_hook DIR — 调 stop hook，返回 result handle（路径前缀）
# ==================================================================
run_hook() {
  local cwd="$1"
  local session_id="${2:-}"
  local handle
  handle="$(mktemp -d -t harness-result-XXXXXX)"
  _HARNESS_TMPDIRS+=("$handle")

  local input_json
  if [ -n "$session_id" ]; then
    input_json="{\"cwd\": \"$cwd\", \"session_id\": \"$session_id\"}"
  else
    input_json="{\"cwd\": \"$cwd\"}"
  fi

  local ec=0
  printf '%s' "$input_json" \
    | bash "$HARNESS_HOOK" \
    >"$handle/stdout" 2>"$handle/stderr" || ec=$?
  echo "$ec" > "$handle/ec"
  echo "$handle"
}

# ==================================================================
# run_merge_cleanup STATE_FILE — 调 merge-and-cleanup.sh
# ==================================================================
run_merge_cleanup() {
  local state_file="$1"
  local handle
  handle="$(mktemp -d -t harness-result-XXXXXX)"
  _HARNESS_TMPDIRS+=("$handle")

  local ec=0
  bash "$HARNESS_MERGE_CLEANUP" "$state_file" \
    >"$handle/stdout" 2>"$handle/stderr" || ec=$?
  echo "$ec" > "$handle/ec"
  echo "$handle"
}

# ==================================================================
# run_locate CWD — 调 locate-state.sh
# ==================================================================
run_locate() {
  local cwd="$1"
  local handle
  handle="$(mktemp -d -t harness-result-XXXXXX)"
  _HARNESS_TMPDIRS+=("$handle")

  local ec=0
  bash "$HARNESS_LOCATE" "$cwd" \
    >"$handle/stdout" 2>"$handle/stderr" || ec=$?
  echo "$ec" > "$handle/ec"
  echo "$handle"
}

# ==================================================================
# result 读取函数
# ==================================================================
result_ec()     { cat "$1/ec" 2>/dev/null || echo "999"; }
result_stdout() { cat "$1/stdout" 2>/dev/null || echo ""; }
result_stderr() { cat "$1/stderr" 2>/dev/null || echo ""; }
result_last()   { tail -1 "$1/stdout" 2>/dev/null || echo ""; }

# ==================================================================
# 断言函数
# ==================================================================
assert() {
  local desc="$1" cond="$2"
  if eval "$cond"; then
    echo "  ✅ $desc"
    _HARNESS_PASS=$(( _HARNESS_PASS + 1 ))
  else
    echo "  ❌ $desc"
    echo "     cond: $cond"
    _HARNESS_FAIL=$(( _HARNESS_FAIL + 1 ))
  fi
}

assert_ec() {
  local desc="$1" handle="$2" expected="$3"
  local actual
  actual="$(result_ec "$handle")"
  assert "$desc (ec=$actual expect=$expected)" "[ '$actual' -eq '$expected' ]"
}

assert_contains() {
  local desc="$1" file="$2" pattern="$3"
  assert "$desc" "grep -q '$pattern' '$file' 2>/dev/null"
}

assert_stdout_contains() {
  local desc="$1" handle="$2" pattern="$3"
  assert "$desc" "grep -q '$pattern' '$handle/stdout' 2>/dev/null"
}

assert_stderr_contains() {
  local desc="$1" handle="$2" pattern="$3"
  assert "$desc" "grep -q '$pattern' '$handle/stderr' 2>/dev/null"
}

assert_file_exists() {
  local desc="$1" path="$2"
  assert "$desc" "[ -e '$path' ]"
}

assert_file_missing() {
  local desc="$1" path="$2"
  assert "$desc" "[ ! -e '$path' ]"
}

# ==================================================================
# state_file ENV SLUG — 返回 state 文件路径
# ==================================================================
state_file() { echo "$1/.claude/builder-loop/state/$2.yml"; }

# ==================================================================
# read_state_field STATE_FILE FIELD — 读 state 字段值
# ==================================================================
read_state_field() {
  grep -E "^${2}:" "$1" 2>/dev/null | head -1 \
    | sed -E "s/^${2}:[[:space:]]*\"?([^\"]*)\"?[[:space:]]*\$/\1/" || true
}

# ==================================================================
# section NAME — 分节标记
# ==================================================================
section() { echo ""; echo "--- $1 ---"; }

# ==================================================================
# harness_report — 汇总 + exit code
# ==================================================================
harness_report() {
  echo ""
  echo "=== ${_HARNESS_NAME}: PASS=${_HARNESS_PASS} FAIL=${_HARNESS_FAIL} ==="
  [ "$_HARNESS_FAIL" -eq 0 ] || exit 1
  exit 0
}
