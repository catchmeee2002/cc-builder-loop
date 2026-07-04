#!/usr/bin/env bash
# test-diff-level-check.sh — diff-level-check.sh L3 信号检测
#
# Case 1: 新增公开函数 → exit 1 + 输出含函数名
# Case 2: 新增 _private 函数 → exit 1（脚本只检测不判断，判断权给 builder）
# Case 3: 只改函数体不加新签名 → exit 0
# Case 4: 测试文件里的新函数不算 → exit 0
# Case 5: 新增文件 → exit 1
# Case 6: 新增 bash 函数 → exit 1

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "diff-level-check"

DLCHECK="${HARNESS_REPO_ROOT}/skills/builder-loop/scripts/diff-level-check.sh"
assert_file_exists "diff-level-check.sh 存在" "$DLCHECK"

mk_py_repo() {
  local d
  d=$(mktemp -d -t "harness-dlcheck-XXXXXX")
  _HARNESS_TMPDIRS+=("$d")
  (
    cd "$d"
    git init -q
    git config user.email "harness@test.local"
    git config user.name "harness"
    mkdir -p src tests
    cat > src/engine.py <<'PY'
def load_config():
    return {}

class Engine:
    def run(self):
        pass
PY
    cat > tests/test_engine.py <<'PY'
def test_load():
    pass
PY
    git add -A
    git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Seed"
  )
  echo "$d"
}

section "Case 1: 新增公开函数 → exit 1"
env1=$(mk_py_repo)
(cd "$env1" && cat >> src/engine.py <<'PY'

def save_snapshot():
    pass
PY
git -C "$env1" add -A && git -C "$env1" -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Add public func")
OUT1=$(bash "$DLCHECK" "$env1" "HEAD~1" 2>/dev/null)
EC1=$?
assert "Case 1 exit 1" "[ '$EC1' -eq 1 ]"
assert "Case 1 输出含 save_snapshot" "echo '$OUT1' | grep -q 'save_snapshot'"

section "Case 2: 新增 _private 函数 → exit 1（脚本不判 public/private）"
env2=$(mk_py_repo)
(cd "$env2" && cat >> src/engine.py <<'PY'

def _internal_helper():
    pass
PY
git -C "$env2" add -A && git -C "$env2" -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Add private func")
OUT2=$(bash "$DLCHECK" "$env2" "HEAD~1" 2>/dev/null)
EC2=$?
assert "Case 2 exit 1" "[ '$EC2' -eq 1 ]"
assert "Case 2 输出含 _internal_helper" "echo '$OUT2' | grep -q '_internal_helper'"

section "Case 3: 只改函数体 → exit 0"
env3=$(mk_py_repo)
(cd "$env3" && sed -i 's/return {}/return {"key": "val"}/' src/engine.py
git -C "$env3" add -A && git -C "$env3" -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Change body only")
OUT3=$(bash "$DLCHECK" "$env3" "HEAD~1" 2>/dev/null)
EC3=$?
assert "Case 3 exit 0" "[ '$EC3' -eq 0 ]"
assert "Case 3 count=0" "echo '$OUT3' | grep -q '\"count\":0'"

section "Case 4: 测试文件里的新函数不算 → exit 0"
env4=$(mk_py_repo)
(cd "$env4" && cat >> tests/test_engine.py <<'PY'

def test_save_snapshot():
    pass

class TestNewFeature:
    def test_it(self):
        pass
PY
git -C "$env4" add -A && git -C "$env4" -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Add test funcs")
OUT4=$(bash "$DLCHECK" "$env4" "HEAD~1" 2>/dev/null)
EC4=$?
assert "Case 4 exit 0" "[ '$EC4' -eq 0 ]"

section "Case 5: 新增文件 → exit 1"
env5=$(mk_py_repo)
(cd "$env5" && echo 'print("new")' > src/validator.py
git -C "$env5" add -A && git -C "$env5" -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Add new file")
OUT5=$(bash "$DLCHECK" "$env5" "HEAD~1" 2>/dev/null)
EC5=$?
assert "Case 5 exit 1" "[ '$EC5' -eq 1 ]"
assert "Case 5 输出含 validator.py" "echo '$OUT5' | grep -q 'validator.py'"

section "Case 6: 新增 bash 函数 → exit 1"
env6=$(mk_py_repo)
(cd "$env6" && cat > src/helper.sh <<'SH'
#!/usr/bin/env bash
do_cleanup() {
  rm -rf /tmp/test
}
SH
git -C "$env6" add -A && git -C "$env6" -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Add bash func")
OUT6=$(bash "$DLCHECK" "$env6" "HEAD~1" 2>/dev/null)
EC6=$?
assert "Case 6 exit 1" "[ '$EC6' -eq 1 ]"
assert "Case 6 输出含 do_cleanup" "echo '$OUT6' | grep -q 'do_cleanup'"

# ============================================================
# Case 7: staged 新函数 + 默认 HEAD → exit 1
# ============================================================
section "Case 7: staged new function with default HEAD → exit 1"
env7=$(mk_py_repo)
(cd "$env7" && cat > src/validator.py <<'PY'
def validate_input(data):
    return bool(data)
PY
git -C "$env7" add src/validator.py)
OUT7=$(bash "$DLCHECK" "$env7" 2>/dev/null)
EC7=$?
assert "Case 7 exit 1" "[ '$EC7' -eq 1 ]"
assert "Case 7 输出含 validate_input" "echo '$OUT7' | grep -q 'validate_input'"

# ============================================================
# Case 8: prior commit 加函数 + staged 无关改动 + 默认 HEAD → exit 0
# 验证 HEAD vs HEAD~1 行为差异
# ============================================================
section "Case 8: prior commit added function + staged unrelated change → exit 0 with HEAD"
env8=$(mk_py_repo)
(cd "$env8" && cat > src/validator.py <<'PY'
def validate_input(data):
    return bool(data)
PY
git -C "$env8" add -A && git -C "$env8" -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Add validator"
echo "# comment" >> "$env8/src/engine.py"
git -C "$env8" add src/engine.py)
OUT8_HEAD=$(bash "$DLCHECK" "$env8" 2>/dev/null)
EC8_HEAD=$?
assert "Case 8a exit 0 with default HEAD" "[ '$EC8_HEAD' -eq 0 ]"
OUT8_OLD=$(bash "$DLCHECK" "$env8" "HEAD~1" 2>/dev/null)
EC8_OLD=$?
assert "Case 8b exit 1 with HEAD~1 (proves regression guard)" "[ '$EC8_OLD' -eq 1 ]"

# ---- Case 9: doc_freshness_check 交叉引用命中 ----
section "Case 9: changed file basename in CLAUDE.md → doc_freshness_check 含 CLAUDE.md"
env9=$(mk_py_repo)
cat > "$env9/CLAUDE.md" <<'MD'
# Project
Uses engine.py for core logic.
MD
git -C "$env9" add CLAUDE.md
git -C "$env9" commit -q -m "docs(test): [cr_id_skip] Add CLAUDE.md"
echo "changed" >> "$env9/src/engine.py"
git -C "$env9" add src/engine.py
OUT9=$(bash "$DLCHECK" "$env9" 2>/dev/null)
assert "Case 9 doc_freshness_check 含 CLAUDE.md" "echo '$OUT9' | grep -q 'CLAUDE.md'"

# ---- Case 10: doc_freshness_check 无命中 ----
section "Case 10: changed file basename not in any doc → doc_freshness_check 空"
env10=$(mk_py_repo)
cat > "$env10/CLAUDE.md" <<'MD'
# Project
Nothing relevant here.
MD
git -C "$env10" add CLAUDE.md
git -C "$env10" commit -q -m "docs(test): [cr_id_skip] Add CLAUDE.md"
echo "changed" >> "$env10/src/engine.py"
git -C "$env10" add src/engine.py
OUT10=$(bash "$DLCHECK" "$env10" 2>/dev/null)
assert "Case 10 doc_freshness_check 不含 CLAUDE.md" "! echo '$OUT10' | grep -q 'CLAUDE.md'"

harness_report
