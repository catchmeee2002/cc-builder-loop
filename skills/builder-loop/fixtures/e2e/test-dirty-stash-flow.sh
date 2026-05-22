#!/usr/bin/env bash
# test-dirty-stash-flow.sh — V3.2 setup 默认干净 worktree + --touched-files 选择性带入
#
# 覆盖 4 个 case：
#   A1: clean 主仓 + setup → worktree_mode=clean
#   A2: dirty 主仓 + 默认调用 → worktree 干净，dirty 留主仓，stderr 有提示
#   A3: dirty 主仓 + --touched-files → 只带指定文件，其余留主仓
#   A4: rebase 进行中 + --touched-files → exit 2 拒绝

set -uo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$THIS_DIR/../../../.." && pwd)"
SETUP="${REPO_ROOT}/skills/builder-loop/scripts/setup-builder-loop.sh"

PASS=0
FAIL=0

assert() {
  local desc="$1" cond="$2"
  if eval "$cond"; then echo "  ✅ $desc"; PASS=$(( PASS + 1 ));
  else echo "  ❌ $desc (cond: $cond)"; FAIL=$(( FAIL + 1 )); fi
}

mk_repo() {
  local d
  d="$(mktemp -d -t builder-loop-v32-XXXXXX)"
  (
    cd "$d"
    git init -q
    git config user.email "e2e@test.local"
    git config user.name "e2e-test"
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

echo "=== V3.2: setup 默认干净 worktree + --touched-files ==="
echo "    被测脚本：${SETUP}"
assert "被测脚本存在" "[ -f '$SETUP' ]"

# ============================================================
# A1: clean 主仓 → worktree_mode=clean
# ============================================================
echo "--- A1: clean 主仓 → worktree_mode=clean ---"
TMPA1="$(mk_repo)"
trap 'rm -rf "${TMPA1:-}" "${TMPA2:-}" "${TMPA3:-}" "${TMPA4:-}"' EXIT
(cd "$TMPA1" && bash "$SETUP" "a1-clean-test" >/dev/null 2>&1) || true
STATE_A1="$(ls "$TMPA1"/.claude/builder-loop/state/*.yml 2>/dev/null | head -1)"
assert "A1 state 存在" "[ -f '$STATE_A1' ]"
assert "A1 worktree_mode=clean" "grep -q 'worktree_mode: \"clean\"' '$STATE_A1'"
assert "A1 pre_loop_stash_ref 空" "grep -q 'pre_loop_stash_ref: \"\"' '$STATE_A1'"

# ============================================================
# A2: dirty 主仓 + 默认调用 → worktree 干净，dirty 留主仓
# ============================================================
echo "--- A2: dirty 主仓 + 默认 → worktree 干净，dirty 留主仓 ---"
TMPA2="$(mk_repo)"
(cd "$TMPA2" && echo "modified" >> README.md && echo "new" > untracked.py)
STDERR_A2="$(mktemp)"
(cd "$TMPA2" && bash "$SETUP" "a2-dirty-default-test" >/dev/null 2>"$STDERR_A2") || true
STATE_A2="$(ls "$TMPA2"/.claude/builder-loop/state/*.yml 2>/dev/null | head -1)"
assert "A2 state 存在" "[ -f '$STATE_A2' ]"
assert "A2 worktree_mode=clean" "grep -q 'worktree_mode: \"clean\"' '$STATE_A2'"
assert "A2 pre_loop_stash_ref 空（未 stash）" "grep -q 'pre_loop_stash_ref: \"\"' '$STATE_A2'"
MAIN_DIRTY_A2="$(git -C "$TMPA2" status --porcelain 2>/dev/null | grep -E 'README|untracked' || true)"
assert "A2 主仓 dirty 仍在（未被 stash）" "[ -n '$MAIN_DIRTY_A2' ]"
WT_A2="$(grep -E '^worktree_path:' "$STATE_A2" | head -1 | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?.*/\1/')"
WT_DIRTY_A2="$(git -C "$WT_A2" status --porcelain 2>/dev/null || true)"
assert "A2 worktree 干净（无 dirty）" "[ -z '$WT_DIRTY_A2' ]"
assert "A2 stderr 有 dirty 提示" "grep -q '未提交文件' '$STDERR_A2'"

# ============================================================
# A3: dirty + --touched-files → 选择性带入
# ============================================================
echo "--- A3: dirty + --touched-files → 选择性 stash ---"
TMPA3="$(mk_repo)"
(cd "$TMPA3" && echo "modified" >> README.md && echo "src-change" >> src/main.py && echo "new" > untracked.py)
(cd "$TMPA3" && bash "$SETUP" --touched-files "README.md,untracked.py" "a3-touched-test" >/dev/null 2>&1) || true
STATE_A3="$(ls "$TMPA3"/.claude/builder-loop/state/*.yml 2>/dev/null | head -1)"
assert "A3 state 存在" "[ -f '$STATE_A3' ]"
assert "A3 worktree_mode=selective" "grep -q 'worktree_mode: \"selective\"' '$STATE_A3'"
assert "A3 pre_loop_stash_ref 非空" "grep -qE '^pre_loop_stash_ref: \"[a-f0-9]' '$STATE_A3'"
MAIN_README_A3="$(git -C "$TMPA3" diff --name-only 2>/dev/null | grep README || true)"
assert "A3 主仓 README 已被 stash（不在 dirty 列表）" "[ -z '$MAIN_README_A3' ]"
MAIN_SRC_A3="$(git -C "$TMPA3" diff --name-only 2>/dev/null | grep 'src/main.py' || true)"
assert "A3 主仓 src/main.py 仍 dirty（不在 touched-files 里）" "[ -n '$MAIN_SRC_A3' ]"
WT_A3="$(grep -E '^worktree_path:' "$STATE_A3" | head -1 | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?.*/\1/')"
WT_README_A3="$(git -C "$WT_A3" status --porcelain 2>/dev/null | grep README || true)"
assert "A3 worktree 内 README 改动可见" "[ -n '$WT_README_A3' ]"

# ============================================================
# A4: rebase 进行中 + --touched-files → exit 2
# ============================================================
echo "--- A4: rebase 进行中 + --touched-files → exit 2 ---"
TMPA4="$(mk_repo)"
(
  cd "$TMPA4"
  echo "modified" >> README.md
  git add README.md
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Branch commit"
  git checkout -q -b conflict-branch HEAD~1
  echo "conflict" >> README.md
  git add README.md
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Conflict commit"
  git rebase main 2>/dev/null || true
)
EC_A4=0
(cd "$TMPA4" && bash "$SETUP" --touched-files "README.md" "a4-rebase-test" >/dev/null 2>&1) || EC_A4=$?
STATE_A4="$(ls "$TMPA4"/.claude/builder-loop/state/*.yml 2>/dev/null | head -1 || true)"
assert "A4 exit code = 2" "[ '$EC_A4' -eq 2 ]"
assert "A4 state 未创建" "[ -z '$STATE_A4' ]"

echo ""
echo "=== 总计 ==="
echo "  ✅ PASS: $PASS"
echo "  ❌ FAIL: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
