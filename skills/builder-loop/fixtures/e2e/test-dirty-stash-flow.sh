#!/usr/bin/env bash
# test-dirty-stash-flow.sh — V3.2 setup 默认干净 worktree + --touched-files 选择性带入
#
# 覆盖 4 个 case：
#   A1: clean 主仓 + setup → worktree_mode=clean
#   A2: dirty 主仓 + 默认调用 → worktree 干净，dirty 留主仓，stderr 有提示
#   A3: dirty 主仓 + --touched-files → 只带指定文件，其余留主仓
#   A4: rebase 进行中 + --touched-files → exit 2 拒绝

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "dirty-stash-flow"

assert_file_exists "被测脚本存在" "$HARNESS_SETUP"

# Helper: 建简单仓库（setup-builder-loop.sh 需要特定 loop.yml 格式）
mk_setup_repo() {
  local d
  d=$(mktemp -d -t "harness-stash-XXXXXX")
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

# ============================================================
# A1: clean 主仓 → worktree_mode=clean
# ============================================================
section "A1: clean 主仓 → worktree_mode=clean"
envA1=$(mk_setup_repo)
(cd "$envA1" && bash "$HARNESS_SETUP" "a1-clean-test" >/dev/null 2>&1) || true
stateA1=$(ls "$envA1"/.claude/builder-loop/state/*.yml 2>/dev/null | head -1)
assert_file_exists "A1 state 存在" "$stateA1"
assert_contains "A1 worktree_mode=clean" "$stateA1" 'worktree_mode: "clean"'
assert_contains "A1 pre_loop_stash_ref 空" "$stateA1" 'pre_loop_stash_ref: ""'

# ============================================================
# A2: dirty 主仓 + 默认调用 → worktree 干净，dirty 留主仓
# ============================================================
section "A2: dirty 主仓 + 默认 → worktree 干净"
envA2=$(mk_setup_repo)
(cd "$envA2" && echo "modified" >> README.md && echo "new" > untracked.py)
stderrA2=$(mktemp); _HARNESS_TMPDIRS+=("$(dirname "$stderrA2")")
(cd "$envA2" && bash "$HARNESS_SETUP" "a2-dirty-default-test" >/dev/null 2>"$stderrA2") || true
stateA2=$(ls "$envA2"/.claude/builder-loop/state/*.yml 2>/dev/null | head -1)
assert_file_exists "A2 state 存在" "$stateA2"
assert_contains "A2 worktree_mode=clean" "$stateA2" 'worktree_mode: "clean"'
assert_contains "A2 pre_loop_stash_ref 空" "$stateA2" 'pre_loop_stash_ref: ""'
mainDirtyA2=$(git -C "$envA2" status --porcelain 2>/dev/null | grep -E 'README|untracked' || true)
assert "A2 主仓 dirty 仍在" "[ -n '$mainDirtyA2' ]"
wtA2=$(grep -E '^worktree_path:' "$stateA2" | head -1 | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?.*/\1/')
wtDirtyA2=$(git -C "$wtA2" status --porcelain 2>/dev/null || true)
assert "A2 worktree 干净（无 dirty）" "[ -z '$wtDirtyA2' ]"
assert_contains "A2 stderr 有 dirty 提示" "$stderrA2" "未提交文件"

# ============================================================
# A3: dirty + --touched-files → 选择性带入
# ============================================================
section "A3: dirty + --touched-files → 选择性 stash"
envA3=$(mk_setup_repo)
(cd "$envA3" && echo "modified" >> README.md && echo "src-change" >> src/main.py && echo "new" > untracked.py)
(cd "$envA3" && bash "$HARNESS_SETUP" --touched-files "README.md,untracked.py" "a3-touched-test" >/dev/null 2>&1) || true
stateA3=$(ls "$envA3"/.claude/builder-loop/state/*.yml 2>/dev/null | head -1)
assert_file_exists "A3 state 存在" "$stateA3"
assert_contains "A3 worktree_mode=selective" "$stateA3" 'worktree_mode: "selective"'
assert "A3 pre_loop_stash_ref 非空" "grep -qE '^pre_loop_stash_ref: \"[a-f0-9]' '$stateA3'"
mainReadmeA3=$(git -C "$envA3" diff --name-only 2>/dev/null | grep README || true)
assert "A3 主仓 README 已被 stash" "[ -z '$mainReadmeA3' ]"
mainSrcA3=$(git -C "$envA3" diff --name-only 2>/dev/null | grep 'src/main.py' || true)
assert "A3 主仓 src/main.py 仍 dirty" "[ -n '$mainSrcA3' ]"
wtA3=$(grep -E '^worktree_path:' "$stateA3" | head -1 | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?.*/\1/')
wtReadmeA3=$(git -C "$wtA3" status --porcelain 2>/dev/null | grep README || true)
assert "A3 worktree 内 README 改动可见" "[ -n '$wtReadmeA3' ]"

# ============================================================
# A4: rebase 进行中 + --touched-files → exit 2
# ============================================================
section "A4: rebase 进行中 → exit 2 拒绝"
envA4=$(mk_setup_repo)
(
  cd "$envA4"
  echo "modified" >> README.md
  git add README.md
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Branch commit"
  git checkout -q -b conflict-branch HEAD~1
  echo "conflict" >> README.md
  git add README.md
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Conflict commit"
  git rebase main 2>/dev/null || true
)
ecA4=0
(cd "$envA4" && bash "$HARNESS_SETUP" --touched-files "README.md" "a4-rebase-test" >/dev/null 2>&1) || ecA4=$?
stateA4=$(ls "$envA4"/.claude/builder-loop/state/*.yml 2>/dev/null | head -1 || true)
assert "A4 exit code = 2" "[ '$ecA4' -eq 2 ]"
assert "A4 state 未创建" "[ -z '$stateA4' ]"

harness_report
