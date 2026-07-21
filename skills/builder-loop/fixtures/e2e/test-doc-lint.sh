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

# ============================================================
# Case 6: 签名变更（函数仍在）→ exit 0（不误判）
# ============================================================
section "Case 6: 签名变更不误判"
env6=$(mk_lint_repo)
(
  cd "$env6"
  cat > docs/architecture.md <<'MD'
# Architecture

Use `save_chapter_snapshot()` to persist state.
MD
  git add -A
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Add ref to save_chapter_snapshot"
  cat > src/engine.py <<'PY'
def save_chapter_snapshot(extra_param=None):
    pass

def load_config():
    pass
PY
  git add -A
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Change signature of save_chapter_snapshot"
)
OUT6=$(bash "$DOC_LINT" "$env6" "HEAD~1" 2>/dev/null)
EC6=$?
assert "Case 6 exit 0 (signature change not false positive)" "[ '$EC6' -eq 0 ]"

# ============================================================
# Case 7: append/clear 黑名单过滤
# ============================================================
section "Case 7: append/clear 黑名单过滤"
env7=$(mk_lint_repo)
(
  cd "$env7"
  cat > src/engine.py <<'PY'
def save_chapter_snapshot():
    pass

def load_config():
    pass

def append(data):
    pass

def clear():
    pass
PY
  git add -A
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Add append and clear"
  cat > docs/architecture.md <<'MD'
# Architecture

Use sheets +append to add rows. Call clear to reset.
MD
  git add -A
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Add docs with append/clear refs"
  cat > src/engine.py <<'PY'
def save_chapter_snapshot():
    pass

def load_config():
    pass
PY
  git add -A
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Remove append and clear functions"
)
OUT7=$(bash "$DOC_LINT" "$env7" "HEAD~1" 2>/dev/null)
EC7=$?
assert "Case 7 exit 0 (append/clear blacklisted)" "[ '$EC7' -eq 0 ]"

# ============================================================
# Case 8: staged 删除（未 commit）+ 默认 HEAD → exit 1
# 模拟 pass_cmd 场景：builder 删了文件但还没 auto-commit
# ============================================================
section "Case 8: staged deletion with default HEAD → exit 1"
env8=$(mk_lint_repo)
(
  cd "$env8"
  rm src/engine.py
  git add -A
)
OUT8=$(bash "$DOC_LINT" "$env8" 2>/dev/null)
EC8=$?
assert "Case 8 exit 1" "[ '$EC8' -eq 1 ]"
assert "Case 8 输出含 save_chapter_snapshot" "echo '$OUT8' | grep -q 'save_chapter_snapshot'"

# ============================================================
# Case 9: prior commit 删函数 + staged 无关改动 + 默认 HEAD → exit 0
# 验证 HEAD vs HEAD~1 行为差异：HEAD~1 会误报，HEAD 不会
# ============================================================
section "Case 9: prior commit removed function + staged unrelated change → exit 0 with HEAD"
env9=$(mk_lint_repo)
(
  cd "$env9"
  cat > src/engine.py <<'PY'
def load_config():
    pass

class ChapterManager:
    pass
PY
  git add -A
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Remove save_chapter_snapshot"
  echo "# new section" >> README.md
  git add README.md
)
OUT9_HEAD=$(bash "$DOC_LINT" "$env9" 2>/dev/null)
EC9_HEAD=$?
assert "Case 9a exit 0 with default HEAD" "[ '$EC9_HEAD' -eq 0 ]"
OUT9_OLD=$(bash "$DOC_LINT" "$env9" "HEAD~1" 2>/dev/null)
EC9_OLD=$?
assert "Case 9b exit 1 with HEAD~1 (proves regression guard)" "[ '$EC9_OLD' -eq 1 ]"

# ============================================================
# Case 10: 函数迁移 + 旧 path::symbol 指针 → exit 1
# ============================================================
section "Case 10: moved function stale qualified pointer → exit 1"
env10=$(mk_lint_repo)
(
  cd "$env10"
  cat > docs/architecture.md <<'MD'
# Architecture

Snapshot logic lives at `src/engine.py::save_chapter_snapshot`.
MD
  git add -A
  git -c core.hooksPath=/dev/null commit -q -m "docs(test): [cr_id_skip] Add qualified pointer"
  cat > src/engine.py <<'PY'
def load_config():
    pass

class ChapterManager:
    pass
PY
  cat > src/snapshot.py <<'PY'
def save_chapter_snapshot():
    pass
PY
  git add -A
)
OUT10=$(bash "$DOC_LINT" "$env10" 2>/dev/null)
EC10=$?
assert "Case 10 exit 1" "[ '$EC10' -eq 1 ]"
assert "Case 10 输出旧路径与符号" "echo '$OUT10' | grep -q 'src/engine.py::save_chapter_snapshot'"

# ============================================================
# Case 11: 函数迁移 + symbol-only 引用仍可成立 → exit 0
# ============================================================
section "Case 11: moved function symbol-only reference → exit 0"
env11=$(mk_lint_repo)
(
  cd "$env11"
  cat > docs/architecture.md <<'MD'
# Architecture

The snapshot flow uses `save_chapter_snapshot`.
MD
  git add -A
  git -c core.hooksPath=/dev/null commit -q -m "docs(test): [cr_id_skip] Add symbol reference"
  cat > src/engine.py <<'PY'
def load_config():
    pass

class ChapterManager:
    pass
PY
  cat > src/snapshot.py <<'PY'
def save_chapter_snapshot():
    pass
PY
  git add -A
)
OUT11=$(bash "$DOC_LINT" "$env11" 2>/dev/null)
EC11=$?
assert "Case 11 exit 0" "[ '$EC11' -eq 0 ]"

# ============================================================
# Case 12: detector 无法解析 diff base → exit 2，不静默 PASS
# ============================================================
section "Case 12: reference detector failure → exit 2"
env12=$(mk_lint_repo)
OUT12=$(bash "$DOC_LINT" "$env12" "missing-reference" 2>/dev/null)
EC12=$?
assert "Case 12 exit 2" "[ '$EC12' -eq 2 ]"

# ============================================================
# Case 13: 历史 CHANGELOG 中的删除符号不要求回写历史
# ============================================================
section "Case 13: deleted symbol in CHANGELOG history → excluded"
env13=$(mk_lint_repo)
(
  cd "$env13"
  cat > docs/architecture.md <<'MD'
# Architecture

Current architecture without private symbol pointers.
MD
  cat > CHANGELOG.md <<'MD'
# Changelog

- Earlier versions used `save_chapter_snapshot`.
MD
  git add -A
  git -c core.hooksPath=/dev/null commit -q -m "docs(test): [cr_id_skip] Move reference to history"
  cat > src/engine.py <<'PY'
def load_config():
    pass

class ChapterManager:
    pass
PY
  git add -A
)
OUT13=$(bash "$DOC_LINT" "$env13" 2>/dev/null)
EC13=$?
assert "Case 13 exit 0" "[ '$EC13' -eq 0 ]"

harness_report
