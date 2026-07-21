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

# ---- Case 9: semantic_checks 交叉引用命中 ----
section "Case 9: changed file basename in CLAUDE.md → semantic_checks 含 CLAUDE.md"
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
assert "Case 9 semantic_checks 含 CLAUDE.md" "echo '$OUT9' | python3 -c \"import json,sys; d=json.load(sys.stdin); sc=d['doc_freshness_check']['semantic_checks']; assert any(c['file']=='CLAUDE.md' for c in sc), sc\""

# ---- Case 10: semantic_checks 无命中 ----
section "Case 10: changed file basename not in any doc → semantic_checks 空"
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
assert "Case 10 semantic_checks 空" "echo '$OUT10' | python3 -c \"import json,sys; d=json.load(sys.stdin); assert d['doc_freshness_check']['semantic_checks']==[], d\""

# ---- Case 11: changelog_needed=true（改 .sh + CHANGELOG 未改）----
section "Case 11: .sh changed without CHANGELOG → changelog_needed=true"
env11=$(mk_py_repo)
mkdir -p "$env11/scripts"
cat > "$env11/scripts/helper.sh" <<'SH'
#!/usr/bin/env bash
echo "original"
SH
cat > "$env11/CHANGELOG.md" <<'MD'
## V1.0 Initial
MD
git -C "$env11" add -A
git -C "$env11" -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Add helper.sh + CHANGELOG"
echo 'echo "modified"' >> "$env11/scripts/helper.sh"
git -C "$env11" add scripts/helper.sh
OUT11=$(bash "$DLCHECK" "$env11" 2>/dev/null)
assert "Case 11 changelog_needed=true" "echo '$OUT11' | python3 -c \"import json,sys; d=json.load(sys.stdin); assert d['doc_freshness_check']['machine_checks']['changelog_needed']==True, d\""

# ---- Case 12: plan_version_stale=true（plan 版本号 < CHANGELOG 版本号）----
section "Case 12: plan.md version behind CHANGELOG → plan_version_stale=true"
env12=$(mk_py_repo)
mkdir -p "$env12/docs"
cat > "$env12/docs/plan.md" <<'MD'
## 当前阶段：V1.0 已发布
MD
cat > "$env12/CHANGELOG.md" <<'MD'
## V2.0 New feature
## V1.0 Initial
MD
git -C "$env12" add -A
git -C "$env12" -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Add stale plan"
echo "# changed" >> "$env12/src/engine.py"
git -C "$env12" add src/engine.py
OUT12=$(bash "$DLCHECK" "$env12" 2>/dev/null)
assert "Case 12 plan_version_stale=true" "echo '$OUT12' | python3 -c \"import json,sys; d=json.load(sys.stdin); assert d['doc_freshness_check']['machine_checks']['plan_version_stale']==True, d\""

# ---- Case 13: improvements_status 匹配（活跃条目标题含 changed basename）----
section "Case 13: active improvements entry title contains changed basename"
env13=$(mk_py_repo)
cat > "$env13/improvements.md" <<'MD'
# Improvements
## 2026-07-05 engine.py 缺少错误处理
- 触发场景：load_config 无 try/except
- 优先级：中
MD
git -C "$env13" add improvements.md
git -C "$env13" -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Add improvements"
echo "# changed" >> "$env13/src/engine.py"
git -C "$env13" add src/engine.py
OUT13=$(bash "$DLCHECK" "$env13" 2>/dev/null)
assert "Case 13 improvements_status 非空" "echo '$OUT13' | python3 -c \"import json,sys; d=json.load(sys.stdin); imp=d['doc_freshness_check']['candidates']['improvements_status']; assert len(imp)>0, imp\""
assert "Case 13 匹配含 engine.py" "echo '$OUT13' | python3 -c \"import json,sys; d=json.load(sys.stdin); imp=d['doc_freshness_check']['candidates']['improvements_status']; assert any('engine.py' in t for t in imp), imp\""

# ---- Case 14: 全空（三层都不触发）----
section "Case 14: no code change, no improvements, no doc refs → all empty"
env14=$(mk_py_repo)
mkdir -p "$env14/docs"
cat > "$env14/CHANGELOG.md" <<'MD'
## V1.0 Initial
MD
cat > "$env14/docs/plan.md" <<'MD'
## 当前阶段：V1.0 已发布
MD
git -C "$env14" add -A
git -C "$env14" -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Add CHANGELOG + plan"
echo "# comment" >> "$env14/src/engine.py"
git -C "$env14" add src/engine.py
OUT14=$(bash "$DLCHECK" "$env14" 2>/dev/null)
assert "Case 14 changelog_needed=false" "echo '$OUT14' | python3 -c \"import json,sys; d=json.load(sys.stdin); assert d['doc_freshness_check']['machine_checks']['changelog_needed']==False, d\""
assert "Case 14 plan_version_stale=false" "echo '$OUT14' | python3 -c \"import json,sys; d=json.load(sys.stdin); assert d['doc_freshness_check']['machine_checks']['plan_version_stale']==False, d\""
assert "Case 14 improvements_status 空" "echo '$OUT14' | python3 -c \"import json,sys; d=json.load(sys.stdin); assert d['doc_freshness_check']['candidates']['improvements_status']==[], d\""

# ---- Case 15: improvements 标题不含扩展名 → stem 匹配仍命中 ----
section "Case 15: improvements title without extension → stem match hits"
env15=$(mk_py_repo)
cat > "$env15/improvements.md" <<'MD'
# Improvements
## 2026-07-06 diff-level-check 输出粒度不足
- 触发场景：builder 跳过更新
- 优先级：高
MD
mkdir -p "$env15/skills/builder-loop/scripts"
cat > "$env15/skills/builder-loop/scripts/diff-level-check.sh" <<'SH'
#!/usr/bin/env bash
echo "original"
SH
git -C "$env15" add -A
git -C "$env15" -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Add improvements + script"
echo 'echo "modified"' >> "$env15/skills/builder-loop/scripts/diff-level-check.sh"
git -C "$env15" add skills/builder-loop/scripts/diff-level-check.sh
OUT15=$(bash "$DLCHECK" "$env15" 2>/dev/null)
assert "Case 15 improvements_status 非空" "echo '$OUT15' | python3 -c \"import json,sys; d=json.load(sys.stdin); imp=d['doc_freshness_check']['candidates']['improvements_status']; assert len(imp)>0, 'got: '+str(imp)\""
assert "Case 15 匹配含 diff-level-check" "echo '$OUT15' | python3 -c \"import json,sys; d=json.load(sys.stdin); imp=d['doc_freshness_check']['candidates']['improvements_status']; assert any('diff-level-check' in t for t in imp), imp\""

# ---- Case 16: fixture-only .sh 改动 → changelog_needed=false ----
section "Case 16: fixture .sh change only → changelog_needed=false"
env16=$(mk_py_repo)
mkdir -p "$env16/skills/builder-loop/fixtures/e2e"
cat > "$env16/skills/builder-loop/fixtures/e2e/test-something.sh" <<'SH'
#!/usr/bin/env bash
echo "test"
SH
cat > "$env16/CHANGELOG.md" <<'MD'
## V1.0 Initial
MD
git -C "$env16" add -A
git -C "$env16" -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Add fixture + CHANGELOG"
echo 'echo "modified"' >> "$env16/skills/builder-loop/fixtures/e2e/test-something.sh"
git -C "$env16" add skills/builder-loop/fixtures/e2e/test-something.sh
OUT16=$(bash "$DLCHECK" "$env16" 2>/dev/null)
assert "Case 16 changelog_needed=false" "echo '$OUT16' | python3 -c \"import json,sys; d=json.load(sys.stdin); assert d['doc_freshness_check']['machine_checks']['changelog_needed']==False, d\""

# ============================================================
# GitHub fallback tests (mock gh via PATH injection)
# ============================================================

mk_mock_gh() {
  local mock_dir="$1" response="$2"
  mkdir -p "$mock_dir/mock_bin"
  cat > "$mock_dir/mock_bin/gh" << GHEOF
#!/usr/bin/env bash
echo '$response'
GHEOF
  chmod +x "$mock_dir/mock_bin/gh"
}

mk_gh_repo() {
  local d
  d=$(mktemp -d -t "harness-dlcheck-gh-XXXXXX")
  _HARNESS_TMPDIRS+=("$d")
  (
    cd "$d"
    git init -q
    git config user.email "harness@test.local"
    git config user.name "harness"
    git remote add origin "https://github.com/testowner/testrepo.git"
    mkdir -p src
    cat > src/engine.py <<'PY'
def load_config():
    return {}
PY
    git add -A
    git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Seed"
  )
  echo "$d"
}

# ---- Case 17: no local improvements.md + mock gh returns active issue with changed basename → match ----
section "Case 17: GitHub fallback — active issue matches changed basename"
env17=$(mk_gh_repo)
mk_mock_gh "$env17" '[{"title":"2026-07-06 engine.py 缺少错误处理","labels":[{"name":"active"},{"name":"priority:mid"}]}]'
echo "# changed" >> "$env17/src/engine.py"
git -C "$env17" add src/engine.py
OUT17=$(PATH="$env17/mock_bin:$PATH" bash "$DLCHECK" "$env17" 2>/dev/null)
assert "Case 17 improvements_status 非空" "echo '$OUT17' | python3 -c \"import json,sys; d=json.load(sys.stdin); imp=d['doc_freshness_check']['candidates']['improvements_status']; assert len(imp)>0, imp\""
assert "Case 17 improvements_source=github" "echo '$OUT17' | python3 -c \"import json,sys; d=json.load(sys.stdin); assert d['doc_freshness_check']['improvements_source']=='github', d\""

# ---- Case 18: no local improvements.md + mock gh returns only observation issue → no match ----
section "Case 18: GitHub fallback — observation issue filtered out"
env18=$(mk_gh_repo)
mk_mock_gh "$env18" '[{"title":"2026-07-06 [观察期] engine.py fix","labels":[{"name":"observation"}]}]'
echo "# changed" >> "$env18/src/engine.py"
git -C "$env18" add src/engine.py
OUT18=$(PATH="$env18/mock_bin:$PATH" bash "$DLCHECK" "$env18" 2>/dev/null)
assert "Case 18 improvements_status 空" "echo '$OUT18' | python3 -c \"import json,sys; d=json.load(sys.stdin); assert d['doc_freshness_check']['candidates']['improvements_status']==[], d\""
assert "Case 18 improvements_source=github" "echo '$OUT18' | python3 -c \"import json,sys; d=json.load(sys.stdin); assert d['doc_freshness_check']['improvements_source']=='github', d\""

# ---- Case 19: no local improvements.md + gh not available → graceful fallback, empty ----
section "Case 19: GitHub fallback — gh not in PATH → empty + source=none"
env19=$(mk_gh_repo)
echo "# changed" >> "$env19/src/engine.py"
git -C "$env19" add src/engine.py
OUT19=$(PATH="/usr/bin:/bin" bash "$DLCHECK" "$env19" 2>/dev/null)
assert "Case 19 improvements_status 空" "echo '$OUT19' | python3 -c \"import json,sys; d=json.load(sys.stdin); assert d['doc_freshness_check']['candidates']['improvements_status']==[], d\""
assert "Case 19 improvements_source=none" "echo '$OUT19' | python3 -c \"import json,sys; d=json.load(sys.stdin); assert d['doc_freshness_check']['improvements_source']=='none', d\""

# ---- Case 20: local improvements.md exists + mock gh also has data → reads local (priority) ----
section "Case 20: local file takes priority over GitHub"
env20=$(mk_gh_repo)
cat > "$env20/improvements.md" <<'MD'
# Improvements
## 2026-07-06 engine.py local entry
- 优先级：中
MD
mk_mock_gh "$env20" '[{"title":"2026-07-06 engine.py github entry","labels":[{"name":"active"}]}]'
git -C "$env20" add -A
git -C "$env20" -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Add improvements"
echo "# changed" >> "$env20/src/engine.py"
git -C "$env20" add src/engine.py
OUT20=$(PATH="$env20/mock_bin:$PATH" bash "$DLCHECK" "$env20" 2>/dev/null)
assert "Case 20 improvements_source=local" "echo '$OUT20' | python3 -c \"import json,sys; d=json.load(sys.stdin); assert d['doc_freshness_check']['improvements_source']=='local', d\""
assert "Case 20 匹配含 local entry" "echo '$OUT20' | python3 -c \"import json,sys; d=json.load(sys.stdin); imp=d['doc_freshness_check']['candidates']['improvements_status']; assert any('local' in t for t in imp), imp\""

# ---- Case 21: single-word stem (builder) does NOT match generic issue titles ----
section "Case 21: single-word stem skips generic matches, compound stem still matches"
env21=$(mk_gh_repo)
mk_mock_gh "$env21" '[{"title":"2026-07-06 builder 步骤 5 重复确认","labels":[{"name":"active"}]},{"title":"2026-07-06 diff-level-check 输出粒度不足","labels":[{"name":"active"}]}]'
mkdir -p "$env21/commands"
echo "changed" > "$env21/commands/builder.md"
mkdir -p "$env21/skills/builder-loop/scripts"
echo "changed" > "$env21/skills/builder-loop/scripts/diff-level-check.sh"
git -C "$env21" add -A
git -C "$env21" -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Add files"
echo "modified" >> "$env21/commands/builder.md"
echo "modified" >> "$env21/skills/builder-loop/scripts/diff-level-check.sh"
git -C "$env21" add commands/builder.md skills/builder-loop/scripts/diff-level-check.sh
OUT21=$(PATH="$env21/mock_bin:$PATH" bash "$DLCHECK" "$env21" 2>/dev/null)
assert "Case 21 不含 builder 单词匹配" "echo '$OUT21' | python3 -c \"import json,sys; d=json.load(sys.stdin); imp=d['doc_freshness_check']['candidates']['improvements_status']; assert not any('builder 步骤' in t for t in imp), 'unexpected: '+str(imp)\""
assert "Case 21 含 diff-level-check 复合匹配" "echo '$OUT21' | python3 -c \"import json,sys; d=json.load(sys.stdin); imp=d['doc_freshness_check']['candidates']['improvements_status']; assert any('diff-level-check' in t for t in imp), imp\""

mk_symbol_move_repo() {
  local d
  d=$(mktemp -d -t "harness-dlcheck-symbol-XXXXXX")
  _HARNESS_TMPDIRS+=("$d")
  (
    cd "$d"
    git init -q
    git config user.email "harness@test.local"
    git config user.name "harness"
    mkdir -p handler card
    cat > handler/ops_hmi_video.py <<'PY'
def _parse_picker_to_epoch(value):
    return value

def keep_local():
    return True
PY
    git add -A
    git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Seed symbol move"
  )
  echo "$d"
}

move_symbol() {
  local d="$1"
  cat > "$d/handler/ops_hmi_video.py" <<'PY'
def keep_local():
    return True
PY
  cat > "$d/handler/card_shared.py" <<'PY'
def _parse_picker_to_epoch(value):
    return value
PY
  git -C "$d" add -A
}

# ---- Case 22: nested Markdown stale path::symbol after move → machine check ----
section "Case 22: moved symbol stale qualified pointer → machine check"
env22=$(mk_symbol_move_repo)
cat > "$env22/card/CLAUDE.md" <<'MD'
# Card

解析在 `handler/ops_hmi_video.py::_parse_picker_to_epoch`。
MD
git -C "$env22" add card/CLAUDE.md
git -C "$env22" -c core.hooksPath=/dev/null commit -q -m "docs(test): [cr_id_skip] Add nested pointer"
move_symbol "$env22"
OUT22=$(bash "$DLCHECK" "$env22" 2>/dev/null)
assert "Case 22 broken_symbol_references 命中 nested CLAUDE.md" "echo '$OUT22' | python3 -c \"import json,sys; d=json.load(sys.stdin); refs=d['doc_freshness_check']['machine_checks']['broken_symbol_references']; assert any(r['file']=='card/CLAUDE.md' and r['symbol']=='_parse_picker_to_epoch' and r['old_path']=='handler/ops_hmi_video.py' and r['new_paths']==['handler/card_shared.py'] for r in refs), refs\""

# ---- Case 23: pointer updated to destination → no machine finding ----
section "Case 23: moved symbol pointer already updated → no machine finding"
env23=$(mk_symbol_move_repo)
cat > "$env23/card/CLAUDE.md" <<'MD'
# Card

解析在 `handler/ops_hmi_video.py::_parse_picker_to_epoch`。
MD
git -C "$env23" add card/CLAUDE.md
git -C "$env23" -c core.hooksPath=/dev/null commit -q -m "docs(test): [cr_id_skip] Add pointer"
move_symbol "$env23"
sed -i 's|handler/ops_hmi_video.py|handler/card_shared.py|' "$env23/card/CLAUDE.md"
git -C "$env23" add card/CLAUDE.md
OUT23=$(bash "$DLCHECK" "$env23" 2>/dev/null)
assert "Case 23 broken_symbol_references 为空" "echo '$OUT23' | python3 -c \"import json,sys; d=json.load(sys.stdin); assert d['doc_freshness_check']['machine_checks']['broken_symbol_references']==[], d\""

# ---- Case 24: symbol-only mention → semantic candidate, not hard finding ----
section "Case 24: moved symbol without old path → semantic candidate"
env24=$(mk_symbol_move_repo)
cat > "$env24/card/CLAUDE.md" <<'MD'
# Card

时间解析统一复用 `_parse_picker_to_epoch`。
MD
git -C "$env24" add card/CLAUDE.md
git -C "$env24" -c core.hooksPath=/dev/null commit -q -m "docs(test): [cr_id_skip] Add symbol mention"
move_symbol "$env24"
OUT24=$(bash "$DLCHECK" "$env24" 2>/dev/null)
assert "Case 24 machine finding 为空" "echo '$OUT24' | python3 -c \"import json,sys; d=json.load(sys.stdin); assert d['doc_freshness_check']['machine_checks']['broken_symbol_references']==[], d\""
assert "Case 24 semantic_checks 命中 nested CLAUDE.md" "echo '$OUT24' | python3 -c \"import json,sys; d=json.load(sys.stdin); checks=d['doc_freshness_check']['semantic_checks']; assert any(c['file']=='card/CLAUDE.md' and '_parse_picker_to_epoch' in c['question'] for c in checks), checks\""

# ---- Case 25: historical CHANGELOG pointer is excluded ----
section "Case 25: historical CHANGELOG pointer → excluded"
env25=$(mk_symbol_move_repo)
cat > "$env25/CHANGELOG.md" <<'MD'
# Changelog

- 曾由 `handler/ops_hmi_video.py::_parse_picker_to_epoch` 解析。
MD
git -C "$env25" add CHANGELOG.md
git -C "$env25" -c core.hooksPath=/dev/null commit -q -m "docs(test): [cr_id_skip] Add history"
move_symbol "$env25"
OUT25=$(bash "$DLCHECK" "$env25" 2>/dev/null)
assert "Case 25 broken_symbol_references 为空" "echo '$OUT25' | python3 -c \"import json,sys; d=json.load(sys.stdin); assert d['doc_freshness_check']['machine_checks']['broken_symbol_references']==[], d\""

# ---- Case 26: signature-only change in same file keeps pointer valid ----
section "Case 26: same-file signature change → pointer remains valid"
env26=$(mk_symbol_move_repo)
cat > "$env26/card/CLAUDE.md" <<'MD'
# Card

解析在 `handler/ops_hmi_video.py::_parse_picker_to_epoch`。
MD
git -C "$env26" add card/CLAUDE.md
git -C "$env26" -c core.hooksPath=/dev/null commit -q -m "docs(test): [cr_id_skip] Add pointer"
sed -i 's/def _parse_picker_to_epoch(value):/def _parse_picker_to_epoch(value, timezone=None):/' "$env26/handler/ops_hmi_video.py"
git -C "$env26" add handler/ops_hmi_video.py
OUT26=$(bash "$DLCHECK" "$env26" 2>/dev/null)
assert "Case 26 broken_symbol_references 为空" "echo '$OUT26' | python3 -c \"import json,sys; d=json.load(sys.stdin); assert d['doc_freshness_check']['machine_checks']['broken_symbol_references']==[], d\""

# ---- Case 27: invalid diff base → scan error, not silent empty ----
section "Case 27: reference detector failure → machine scan error"
env27=$(mk_py_repo)
OUT27=$(bash "$DLCHECK" "$env27" "missing-reference" 2>/dev/null)
assert "Case 27 doc_reference_scan_error 非空" "echo '$OUT27' | python3 -c \"import json,sys; d=json.load(sys.stdin); assert d['doc_freshness_check']['machine_checks']['doc_reference_scan_error'], d\""

harness_report
