#!/usr/bin/env bash
# test-doc-lint.sh — doc-lint.sh 过时文档引用检测
#
# Case 1: 删了函数，doc 里有引用 → exit 1
# Case 2: 删了函数，doc 里没引用 → exit 0
# Case 3: 没有删除 → exit 0
# Case 4: 删了文件，doc 里有引用 → exit 1

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "doc-lint"

DOC_LINT="${HARNESS_REPO_ROOT}/skills/builder-loop/scripts/doc-lint.sh"
assert_file_exists "doc-lint.sh 存在" "$DOC_LINT"

mk_lint_repo() {
  local d
  d=$(mktemp -d -t "harness-doclint-XXXXXX")
  _HARNESS_TMPDIRS+=("$d")
  (
    cd "$d"
    git init -q
    git config user.email "harness@test.local"
    git config user.name "harness"
    mkdir -p src docs
    cat > src/engine.py <<'PY'
def save_chapter_snapshot():
    pass

def load_config():
    pass

class ChapterManager:
    pass
PY
    cat > docs/architecture.md <<'MD'
# Architecture

The engine uses `save_chapter_snapshot` to persist state.
Configuration is loaded via `load_config` at startup.
The `ChapterManager` class handles all chapter operations.
MD
    echo "seed" > README.md
    git add -A
    git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Doc-lint seed"
  )
  echo "$d"
}

# ============================================================
# Case 1: 删函数 + doc 有引用 → exit 1
# ============================================================
section "Case 1: 删函数 + doc 有引用 → exit 1"
env1=$(mk_lint_repo)
(
  cd "$env1"
  cat > src/engine.py <<'PY'
def load_config():
    pass

class ChapterManager:
    pass
PY
  git add -A
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Remove save_chapter_snapshot"
)
OUT1=$(bash "$DOC_LINT" "$env1" "HEAD~1" 2>/dev/null)
EC1=$?
assert "Case 1 exit 1" "[ '$EC1' -eq 1 ]"
assert "Case 1 输出含 save_chapter_snapshot" "echo '$OUT1' | grep -q 'save_chapter_snapshot'"
assert "Case 1 输出含文件路径" "echo '$OUT1' | grep -q 'architecture.md'"

# ============================================================
# Case 2: 删函数 + doc 无引用 → exit 0
# ============================================================
section "Case 2: 删函数 + doc 无引用 → exit 0"
env2=$(mk_lint_repo)
(
  cd "$env2"
  cat > docs/architecture.md <<'MD'
# Architecture

General description without specific function references.
MD
  git add -A
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Clean docs first"
  cat > src/engine.py <<'PY'
def load_config():
    pass
PY
  git add -A
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Remove save_chapter_snapshot"
)
OUT2=$(bash "$DOC_LINT" "$env2" "HEAD~1" 2>/dev/null)
EC2=$?
assert "Case 2 exit 0" "[ '$EC2' -eq 0 ]"

# ============================================================
# Case 3: 没有删除 → exit 0
# ============================================================
section "Case 3: 没有删除 → exit 0"
env3=$(mk_lint_repo)
(
  cd "$env3"
  echo "# new section" >> docs/architecture.md
  git add -A
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Doc only change"
)
OUT3=$(bash "$DOC_LINT" "$env3" "HEAD~1" 2>/dev/null)
EC3=$?
assert "Case 3 exit 0" "[ '$EC3' -eq 0 ]"

# ============================================================
# Case 4: 删文件 + doc 有引用 → exit 1
# ============================================================
section "Case 4: 删文件 + doc 有引用 → exit 1"
env4=$(mk_lint_repo)
(
  cd "$env4"
  cat > docs/architecture.md <<'MD'
# Architecture

See src/engine.py for the core logic.
MD
  git add -A
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Add file ref to docs"
  rm src/engine.py
  git add -A
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Delete engine.py"
)
OUT4=$(bash "$DOC_LINT" "$env4" "HEAD~1" 2>/dev/null)
EC4=$?
assert "Case 4 exit 1" "[ '$EC4' -eq 1 ]"
assert "Case 4 输出含 engine.py" "echo '$OUT4' | grep -q 'engine.py'"

# ============================================================
# Case 5: git rm --cached（文件仍在磁盘）→ exit 0（不误报）
# ============================================================
section "Case 5: git rm --cached 文件仍在磁盘 → exit 0"
env5=$(mk_lint_repo)
(
  cd "$env5"
  git rm --cached src/engine.py -q
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Untrack engine.py"
)
OUT5=$(bash "$DOC_LINT" "$env5" "HEAD~1" 2>/dev/null)
EC5=$?
assert "Case 5 exit 0" "[ '$EC5' -eq 0 ]"

harness_report
