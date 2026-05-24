#!/usr/bin/env bash
# test-orphan-worktree-reuse.sh — V3.3 孤儿 worktree 检测与复用
#
# 覆盖 case：
#   A1: setup → abandon → re-setup → 检测到孤儿 → exit 6 + stderr 含 ORPHAN 信息
#   A2: setup → abandon → setup --reuse-worktree → 成功复用 → state 指向旧 worktree
#   A3: setup → abandon → setup --ignore-orphans → 新建 worktree，不报孤儿
#   A4: --reuse-worktree 路径无效 → exit 2
#   A5: 复用后 worktree 内旧改动仍在
#   A6: 多个孤儿 → 全部报出
#
# 退出码：0=全部通过 / 1=有失败

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "V3.3 orphan-worktree-reuse"

ABANDON_SCRIPT="${HARNESS_REPO_ROOT}/skills/builder-loop/scripts/abandon-loop.sh"

# ---- Helper: 建仓 + 跑一次 setup + abandon（留下孤儿 worktree）----
mk_orphan_repo() {
  local d
  d=$(mktemp -d -t "harness-orphan-XXXXXX")
  _HARNESS_TMPDIRS+=("$d")
  (
    cd "$d"
    git init -q
    git config user.email "harness@test.local"
    git config user.name "harness"
    mkdir -p .claude src tests
    cat > .claude/loop.yml <<'YML'
pass_cmd:
  - stage: smoke
    cmd: "true"
    timeout: 10
max_iterations: 3
worktree:
  enabled: true
YML
    echo "seed" > README.md
    echo "src" > src/main.py
    git add -A
    git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Initial seed"
  )
  echo "$d"
}

run_setup() {
  local cwd="$1"; shift
  local handle
  handle="$(mktemp -d -t harness-result-XXXXXX)"
  _HARNESS_TMPDIRS+=("$handle")
  local ec=0
  (cd "$cwd" && bash "$HARNESS_SETUP" "$@") \
    >"$handle/stdout" 2>"$handle/stderr" || ec=$?
  echo "$ec" > "$handle/ec"
  echo "$handle"
}

run_abandon() {
  local state_file="$1" reason="$2"
  local handle
  handle="$(mktemp -d -t harness-result-XXXXXX)"
  _HARNESS_TMPDIRS+=("$handle")
  local ec=0
  bash "$ABANDON_SCRIPT" "$state_file" "$reason" \
    >"$handle/stdout" 2>"$handle/stderr" || ec=$?
  echo "$ec" > "$handle/ec"
  echo "$handle"
}

# ============================================================
section "A1: setup → abandon → re-setup → exit 6（检测到孤儿）"
# ============================================================
envA1=$(mk_orphan_repo)

r1=$(run_setup "$envA1" "a1-first-setup")
assert_ec "A1 首次 setup 成功" "$r1" 0
stateA1=$(ls "$envA1"/.claude/builder-loop/state/*.yml 2>/dev/null | head -1)
assert_file_exists "A1 state 存在" "$stateA1"
wtA1=$(grep -E '^worktree_path:' "$stateA1" | head -1 | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?.*/\1/')
assert_file_exists "A1 worktree 存在" "$wtA1"

r1a=$(run_abandon "$stateA1" "test orphan scenario")
assert_ec "A1 abandon 成功" "$r1a" 0
assert_file_missing "A1 state 已归档" "$stateA1"
assert_file_exists "A1 worktree 仍保留" "$wtA1"

r1b=$(run_setup "$envA1" "a1-second-setup")
assert_ec "A1 re-setup exit 6（孤儿检测）" "$r1b" 6
assert_stderr_contains "A1 stderr 含 ORPHAN" "$r1b" "ORPHAN:"
assert_stderr_contains "A1 stderr 含复用提示" "$r1b" "reuse-worktree"
assert_stderr_contains "A1 stderr 含忽略提示" "$r1b" "ignore-orphans"

# ============================================================
section "A2: --reuse-worktree → 成功复用"
# ============================================================
r2=$(run_setup "$envA1" --reuse-worktree "$wtA1" "a2-reuse-task")
assert_ec "A2 reuse setup 成功" "$r2" 0
assert_stdout_contains "A2 输出含复用模式" "$r2" "复用已有"

stateA2=$(ls "$envA1"/.claude/builder-loop/state/*.yml 2>/dev/null | head -1)
assert_file_exists "A2 新 state 存在" "$stateA2"
stateA2_wt=$(grep -E '^worktree_path:' "$stateA2" | head -1 | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?.*/\1/')
assert "A2 state 指向旧 worktree" "[ '$stateA2_wt' = '$wtA1' ]"
stateA2_mode=$(grep -E '^worktree_mode:' "$stateA2" | head -1 | sed -E 's/^worktree_mode:[[:space:]]*"?([^"]*)"?.*/\1/')
assert "A2 worktree_mode=reuse" "[ '$stateA2_mode' = 'reuse' ]"
stateA2_iter=$(grep -E '^iter:' "$stateA2" | head -1 | awk '{print $2}')
assert "A2 iter=0（重置）" "[ '$stateA2_iter' = '0' ]"

# ============================================================
section "A3: --ignore-orphans → 新建 worktree"
# ============================================================
envA3=$(mk_orphan_repo)
r3_first=$(run_setup "$envA3" "a3-first-setup")
assert_ec "A3 首次 setup 成功" "$r3_first" 0
stateA3_1=$(ls "$envA3"/.claude/builder-loop/state/*.yml 2>/dev/null | head -1)
wtA3=$(grep -E '^worktree_path:' "$stateA3_1" | head -1 | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?.*/\1/')
r3a=$(run_abandon "$stateA3_1" "test ignore-orphans")
assert_ec "A3 abandon 成功" "$r3a" 0

r3b=$(run_setup "$envA3" --ignore-orphans "a3-ignore-orphans-task")
assert_ec "A3 ignore-orphans setup 成功" "$r3b" 0
stateA3_2=$(ls "$envA3"/.claude/builder-loop/state/*.yml 2>/dev/null | head -1)
wtA3_new=$(grep -E '^worktree_path:' "$stateA3_2" | head -1 | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?.*/\1/')
assert "A3 新 worktree 路径 ≠ 旧" "[ '$wtA3_new' != '$wtA3' ]"
assert_file_exists "A3 新 worktree 存在" "$wtA3_new"

# ============================================================
section "A4: --reuse-worktree 路径无效 → exit 2"
# ============================================================
envA4=$(mk_orphan_repo)
r4=$(run_setup "$envA4" --reuse-worktree "/tmp/nonexistent-worktree-path-xxxxx" "a4-invalid-path")
assert_ec "A4 无效路径 exit 2" "$r4" 2
assert_stderr_contains "A4 stderr 含错误提示" "$r4" "路径无效"

# ============================================================
section "A5: 复用后 worktree 内旧改动仍在"
# ============================================================
envA5=$(mk_orphan_repo)
r5_first=$(run_setup "$envA5" "a5-dirty-reuse")
assert_ec "A5 首次 setup 成功" "$r5_first" 0
stateA5=$(ls "$envA5"/.claude/builder-loop/state/*.yml 2>/dev/null | head -1)
wtA5=$(grep -E '^worktree_path:' "$stateA5" | head -1 | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?.*/\1/')

echo "dirty change in worktree" > "$wtA5/dirty-file.txt"
r5a=$(run_abandon "$stateA5" "test dirty reuse")
assert_ec "A5 abandon 成功" "$r5a" 0
assert_file_exists "A5 dirty 文件仍在 worktree" "$wtA5/dirty-file.txt"

r5b=$(run_setup "$envA5" --reuse-worktree "$wtA5" "a5-reuse-dirty")
assert_ec "A5 reuse dirty worktree 成功" "$r5b" 0
assert_file_exists "A5 dirty 文件在 reuse 后仍存在" "$wtA5/dirty-file.txt"

# ============================================================
section "A6: 多个孤儿 → 全部报出"
# ============================================================
envA6=$(mk_orphan_repo)

r6_1=$(run_setup "$envA6" "a6-first-orphan")
assert_ec "A6 setup-1 成功" "$r6_1" 0
stateA6_1=$(ls "$envA6"/.claude/builder-loop/state/*.yml 2>/dev/null | head -1)
r6a=$(run_abandon "$stateA6_1" "orphan 1")
assert_ec "A6 abandon-1 成功" "$r6a" 0

r6_2=$(run_setup "$envA6" --ignore-orphans "a6-second-orphan")
assert_ec "A6 setup-2（ignore-orphans）成功" "$r6_2" 0
stateA6_2=$(ls "$envA6"/.claude/builder-loop/state/*.yml 2>/dev/null | head -1)
r6b=$(run_abandon "$stateA6_2" "orphan 2")
assert_ec "A6 abandon-2 成功" "$r6b" 0

r6_detect=$(run_setup "$envA6" "a6-detect-multiple")
assert_ec "A6 检测到多孤儿 exit 6" "$r6_detect" 6
_a6_orphan_count=$(grep -c "ORPHAN:" "$r6_detect/stderr" || echo "0")
assert "A6 stderr 报出 2 个 ORPHAN" "[ '$_a6_orphan_count' -ge 2 ]"

# ============================================================
section "A7: --reuse-worktree 相对路径 → exit 2"
# ============================================================
envA7=$(mk_orphan_repo)
r7=$(run_setup "$envA7" --reuse-worktree "relative/path" "a7-relative-path")
assert_ec "A7 相对路径 exit 2" "$r7" 2
assert_stderr_contains "A7 stderr 含绝对路径提示" "$r7" "绝对路径"

# ============================================================
section "A8: --reuse-worktree + --no-worktree 互斥 → exit 2"
# ============================================================
envA8=$(mk_orphan_repo)
r8=$(run_setup "$envA8" --reuse-worktree "/tmp/whatever" --no-worktree "a8-mutex")
assert_ec "A8 互斥 flag exit 2" "$r8" 2
assert_stderr_contains "A8 stderr 含互斥提示" "$r8" "互斥"

harness_report
