#!/usr/bin/env bash
# test-dirty-stash-flow.sh — V2.3 主仓 dirty stash + worktree apply 流程
#
# 覆盖 5 个 case：
#   A1: clean 主仓 + setup → worktree_mode=clean，pre_loop_* 字段为空
#   A2: dirty 主仓 + setup → stash + worktree apply（主仓 clean / worktree 含 dirty）
#   A3: 主仓在 rebase 进行中 + dirty + setup → exit 2 拒绝（state 不创建）
#   A4: dirty + --no-stash → 跳过 stash 流程，worktree 创建但 dirty 留主仓
#   A5: stash 副本（commit hash 形式）能用 git stash apply 还原主仓
#
# 用法：bash test-dirty-stash-flow.sh
# 退出码：0=全过 / 1=有失败

set -euo pipefail

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
  d="$(mktemp -d -t builder-loop-v23-XXXXXX)"
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
    git add -A
    git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Initial seed"
  )
  echo "$d"
}

cleanup_repo() {
  local d="$1"
  # 先 prune worktree（防 .git/worktrees 残留）
  if [ -d "$d/.git" ]; then
    while IFS= read -r wt; do
      [ -n "$wt" ] && [ "$wt" != "$d" ] && git -C "$d" worktree remove --force "$wt" 2>/dev/null || true
    done < <(git -C "$d" worktree list --porcelain 2>/dev/null | awk '/^worktree /{print $2}' || true)
  fi
  rm -rf "$d"
}

echo "=== E2E V2.3: dirty stash + worktree apply 流程 ==="
echo "    被测脚本：$SETUP"
assert "被测脚本存在" "[ -f '$SETUP' ]"

# ---- A1: clean 主仓 + setup → 走原 worktree 路径 ----
echo "--- A1: clean 主仓 + setup → worktree_mode=clean，pre_loop_* 空 ---"
TMP1="$(mk_repo)"
cd "$TMP1"
bash "$SETUP" "A1 clean test" >/dev/null 2>&1 || { echo "  ❌ A1 setup 失败"; FAIL=$(( FAIL + 1 )); }
STATE="$(ls "$TMP1"/.claude/builder-loop/state/*.yml 2>/dev/null | head -1 || true)"
assert "A1 state 存在" "[ -f '$STATE' ]"
assert "A1 worktree_mode=clean" "grep -q 'worktree_mode: \"clean\"' '$STATE'"
assert "A1 pre_loop_stash_ref 空" "grep -q 'pre_loop_stash_ref: \"\"' '$STATE'"
assert "A1 pre_loop_dirty_files 空" "grep -q 'pre_loop_dirty_files: \"\"' '$STATE'"
WT_PATH_A1="$(grep '^worktree_path:' "$STATE" | head -1 | sed -E 's/^worktree_path:[[:space:]]*"([^"]*)".*/\1/')"
assert "A1 worktree 已创建" "[ -d '$WT_PATH_A1' ]"
cd /tmp
cleanup_repo "$TMP1"

# ---- A2: dirty 主仓 + setup → stash + worktree apply ----
echo "--- A2: dirty 主仓（tracked + untracked） + setup → stash + worktree apply ---"
TMP2="$(mk_repo)"
cd "$TMP2"
echo "modified line" >> README.md         # tracked modify
echo "new file content" > untracked.py    # untracked
bash "$SETUP" "A2 dirty stash test" >/dev/null 2>&1 || { echo "  ❌ A2 setup 失败"; FAIL=$(( FAIL + 1 )); }
STATE="$(ls "$TMP2"/.claude/builder-loop/state/*.yml 2>/dev/null | head -1 || true)"
assert "A2 state 存在" "[ -f '$STATE' ]"
assert "A2 worktree_mode=dirty" "grep -q 'worktree_mode: \"dirty\"' '$STATE'"
assert "A2 pre_loop_stash_ref 是 commit hash" "grep -qE '^pre_loop_stash_ref: \"[a-f0-9]{40}\"' '$STATE'"
assert "A2 pre_loop_dirty_files 含 README.md" "grep -E '^pre_loop_dirty_files:.*README' '$STATE'"
assert "A2 pre_loop_dirty_files 含 untracked.py" "grep -E '^pre_loop_dirty_files:.*untracked' '$STATE'"
# 主仓 working tree 应已清理（排除 setup 副产品：.gitignore / worktrees / builder-loop state）
MAIN_DIRTY_A2="$(git -C "$TMP2" status --porcelain 2>/dev/null \
  | grep -v -E '^\?\? \.gitignore$' \
  | grep -v -E '^\?\? \.claude/(builder-loop|worktrees|loop-runs)' \
  || true)"
assert "A2 主仓真实 dirty 已被 stash 收走" "[ -z \"\$MAIN_DIRTY_A2\" ]"
# worktree 内应有 dirty 改动
WT_PATH_A2="$(grep '^worktree_path:' "$STATE" | head -1 | sed -E 's/^worktree_path:[[:space:]]*"([^"]*)".*/\1/')"
WT_STATUS_A2="$(git -C "$WT_PATH_A2" status --porcelain 2>/dev/null || true)"
assert "A2 worktree 内 README 改动可见" "echo \"\$WT_STATUS_A2\" | grep -q 'README.md'"
assert "A2 worktree 内 untracked 可见" "echo \"\$WT_STATUS_A2\" | grep -q 'untracked'"
# stash list 应有签名条目
STASH_LIST_A2="$(git -C "$TMP2" stash list 2>/dev/null || true)"
assert "A2 主仓 stash list 含 builder-loop:auto 签名" "echo \"\$STASH_LIST_A2\" | grep -q 'builder-loop:auto:slug='"
cd /tmp
cleanup_repo "$TMP2"

# ---- A3: 主仓在 rebase 进行中 → setup 拒绝 ----
echo "--- A3: 主仓 rebase 进行中 + dirty → setup exit 2 拒绝 ---"
TMP3="$(mk_repo)"
cd "$TMP3"
echo "dirty line" >> README.md
mkdir -p .git/rebase-merge   # 模拟 rebase 进行中
EC_A3=0
bash "$SETUP" "A3 rebase reject test" >/dev/null 2>&1 || EC_A3=$?
assert "A3 exit code = 2" "[ '$EC_A3' -eq 2 ]"
STATE_A3="$(ls "$TMP3"/.claude/builder-loop/state/*.yml 2>/dev/null | head -1 || true)"
assert "A3 state 未创建（拒绝路径）" "[ -z \"\$STATE_A3\" ]"
rm -rf .git/rebase-merge
cd /tmp
cleanup_repo "$TMP3"

# ---- A4: dirty + --no-stash → 跳过 stash 流程 ----
echo "--- A4: dirty + --no-stash → worktree 创建但 dirty 留主仓 ---"
TMP4="$(mk_repo)"
cd "$TMP4"
echo "no-stash dirty" >> README.md
bash "$SETUP" --no-stash "A4 no-stash test" >/dev/null 2>&1 || { echo "  ❌ A4 setup 失败"; FAIL=$(( FAIL + 1 )); }
STATE="$(ls "$TMP4"/.claude/builder-loop/state/*.yml 2>/dev/null | head -1 || true)"
assert "A4 state 存在" "[ -f '$STATE' ]"
assert "A4 worktree_mode=clean（跳过 stash）" "grep -q 'worktree_mode: \"clean\"' '$STATE'"
assert "A4 pre_loop_stash_ref 空" "grep -q 'pre_loop_stash_ref: \"\"' '$STATE'"
# 主仓 dirty 应保留
MAIN_DIRTY_A4="$(git -C "$TMP4" status --porcelain 2>/dev/null || true)"
assert "A4 主仓 dirty 保留（无 stash 副本）" "[ -n \"\$MAIN_DIRTY_A4\" ]"
cd /tmp
cleanup_repo "$TMP4"

# ---- A5: stash 副本能 apply 还原（验证 EARLY_STOP 还原能力的核心机制）----
echo "--- A5: stash 副本（commit hash）能 git stash apply 还原 ---"
TMP5="$(mk_repo)"
cd "$TMP5"
echo "to-restore" >> README.md
echo "to-restore-untracked" > untracked5.py
bash "$SETUP" "A5 restore mechanism test" >/dev/null 2>&1 || { echo "  ❌ A5 setup 失败"; FAIL=$(( FAIL + 1 )); }
STATE="$(ls "$TMP5"/.claude/builder-loop/state/*.yml 2>/dev/null | head -1 || true)"
STASH_REF_A5="$(grep '^pre_loop_stash_ref:' "$STATE" | head -1 | sed -E 's/^pre_loop_stash_ref:[[:space:]]*"([^"]*)".*/\1/')"
assert "A5 stash ref 已记录" "[ -n \"\$STASH_REF_A5\" ]"
# 主仓应 clean（A2 已验证），现在 apply stash hash 还原
git -C "$TMP5" stash apply "$STASH_REF_A5" 2>/dev/null || true
RESTORED_A5="$(git -C "$TMP5" status --porcelain 2>/dev/null || true)"
assert "A5 stash apply 还原 README modify" "echo \"\$RESTORED_A5\" | grep -q 'README.md'"
assert "A5 stash apply 还原 untracked5.py" "echo \"\$RESTORED_A5\" | grep -q 'untracked5'"
# stash 副本仍在 stash list（apply 不 pop，验证副本保留语义）
STASH_LIST_A5="$(git -C "$TMP5" stash list 2>/dev/null || true)"
assert "A5 stash 副本仍在 stash list（apply 不 pop）" "echo \"\$STASH_LIST_A5\" | grep -q 'builder-loop:auto'"
cd /tmp
cleanup_repo "$TMP5"

echo ""
echo "=== Summary: PASS=$PASS FAIL=$FAIL ==="
[ "$FAIL" -eq 0 ]
