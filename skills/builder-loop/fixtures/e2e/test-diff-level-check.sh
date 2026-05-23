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

harness_report
