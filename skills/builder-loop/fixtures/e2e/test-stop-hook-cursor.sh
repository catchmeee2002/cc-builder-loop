#!/usr/bin/env bash
# test-stop-hook-cursor.sh — E2E：bootstrap 触发器 + HEAD 游标兼容性
#
# V2.2 议题 3 行为变更：bootstrap 兜底**只看** HAS_DIFF（未提交工作树改动），
# 砍 HAS_RECENT_COMMIT 触发器。详见 CLAUDE.md V2.2 段 + §7.7。
#
# 覆盖场景（V2.2 新行为）：
#   Step 2: 首次 Stop（HEAD 刚 commit + 工作树干净）→ 静默 exit 0（V2.2 行为变更，原 V1.8.2 期望 exit 2）
#   Step 3: 同 HEAD 再次 Stop → 仍 exit 0（行为一致）
#   Step 4: 新 commit + 工作树干净 → 静默 exit 0（V2.2 行为变更）
#   Step 5: HAS_DIFF 非空 → exit 2 + bootstrap（保留旧行为，新游标写入仍工作）
#   Step 6: HAS_DIFF 空 + 游标损坏 → 仍 exit 0（不再降级为旧 bootstrap）
#
# 用法：bash test-stop-hook-cursor.sh
# 退出码：0=全部通过 / 1=有失败

set -euo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# .../skills/builder-loop/fixtures/e2e → 仓库根
REPO_ROOT="$(cd "$THIS_DIR/../../../.." && pwd)"
HOOK_SCRIPT="${REPO_ROOT}/scripts/builder-loop-stop.sh"

PASS=0
FAIL=0

assert() {
  local desc="$1" cond="$2"
  if eval "$cond"; then echo "  ✅ $desc"; PASS=$(( PASS + 1 ));
  else echo "  ❌ $desc (cond: $cond)"; FAIL=$(( FAIL + 1 )); fi
}

call_stop_hook() {
  # $1 = cwd, $2 = stderr 输出文件
  local proj="$1" err_file="$2" ec=0
  printf '{"cwd": "%s"}' "$proj" | bash "$HOOK_SCRIPT" 2>"$err_file" >/dev/null || ec=$?
  return "$ec"
}

echo "=== E2E: V2.2 bootstrap 触发器 + HEAD 游标兼容性 ==="
echo "    被测脚本：$HOOK_SCRIPT"
assert "被测脚本存在" "[ -f '$HOOK_SCRIPT' ]"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "    临时仓库：$TMP"

# ---- Step 1: 最小仓库 + loop.yml（pass_cmd=true 保证 PASS）----
echo "--- Step 1: 初始化仓库 + 种子 commit ---"
cd "$TMP"
git init -q
git config user.email "e2e@test.local"
git config user.name "e2e-test"
mkdir -p .claude src tests
cat > .claude/loop.yml <<'YMLEOF'
pass_cmd:
  - stage: smoke
    cmd: "true"
    timeout: 10
max_iterations: 3
layout:
  source_dirs: [src]
  test_dirs: [tests]
worktree:
  enabled: false
YMLEOF
echo "seed" > README.md
git add -A
git commit -q -m "chore(test): [cr_id_skip] Initial seed for e2e cursor test"

HEAD1="$(git rev-parse HEAD)"
CURSOR="$TMP/.claude/builder-loop/last_processed_head"
STATE_FILE="$TMP/.claude/builder-loop/state/__main__.yml"

assert "loop.yml 存在" "[ -f '$TMP/.claude/loop.yml' ]"
assert "HEAD1 已记录" "[ -n '$HEAD1' ]"
assert "游标文件初始不存在" "[ ! -f '$CURSOR' ]"

# ---- Step 2: V2.2 行为 — 首次 Stop（HAS_DIFF 空 + HEAD 刚 commit）→ 静默 ----
echo "--- Step 2: 首次 Stop（工作树干净）→ V2.2 期望静默 exit 0（不再因 HAS_RECENT_COMMIT 激活）----"
ERR1="$(mktemp)"
EC1=0
call_stop_hook "$TMP" "$ERR1" || EC1=$?

assert "第 1 次 exit 0（V2.2 不再激活）" "[ '$EC1' -eq 0 ]"
assert "第 1 次 stderr 不含 '兜底激活'" "! grep -q '兜底激活' '$ERR1'"
assert "游标文件未创建（未走 PASS 路径）" "[ ! -f '$CURSOR' ]"
assert "state 文件未创建" "[ ! -f '$STATE_FILE' ]"

# ---- Step 3: 同 HEAD 再次 Stop → 仍 exit 0 ----
echo "--- Step 3: 同 HEAD 再次 Stop → 仍 exit 0（行为一致）----"
ERR2="$(mktemp)"
EC2=0
call_stop_hook "$TMP" "$ERR2" || EC2=$?

assert "第 2 次 exit 0" "[ '$EC2' -eq 0 ]"
assert "第 2 次 stderr 不含 '兜底激活'" "! grep -q '兜底激活' '$ERR2'"
assert "第 2 次 未创建 state" "[ ! -f '$STATE_FILE' ]"

# ---- Step 4: 新 commit + 工作树干净 → 仍 exit 0（V2.2 行为变更）----
echo "--- Step 4: 新增 commit + 工作树干净 → V2.2 期望仍 exit 0（行为变更，原 V1.8.2 会激活）----"
echo "more" > CHANGES.md
git add -A
git commit -q -m "feat(test): [cr_id_skip] Second commit for cursor test"
HEAD2="$(git rev-parse HEAD)"

assert "HEAD2 与 HEAD1 不同" "[ '$HEAD1' != '$HEAD2' ]"

ERR3="$(mktemp)"
EC3=0
call_stop_hook "$TMP" "$ERR3" || EC3=$?

assert "第 3 次 exit 0（V2.2 不再因 HEAD 前进而激活）" "[ '$EC3' -eq 0 ]"
assert "第 3 次 stderr 不含 '兜底激活'" "! grep -q '兜底激活' '$ERR3'"
assert "游标文件仍未创建" "[ ! -f '$CURSOR' ]"

# ---- Step 5: 非文档改动 → 触发 bootstrap + 写游标 ----
# 注意：V2.2.1 起 README.md 等 *.md 命中文档白名单不触发，故必须改非白名单文件
# 改动需 git add 进 staged 才被 git diff --cached --name-only 看见（untracked 不算 HAS_DIFF）
echo "--- Step 5: 未提交非文档改动 → 触发 bootstrap，验证游标写入仍工作 ----"
echo "package main" > src/main.go
git add src/main.go
DIFF_STAT="$(git diff --cached --stat)"
assert "HAS_DIFF（staged）非空" "[ -n '$DIFF_STAT' ]"

ERR4="$(mktemp)"
EC4=0
call_stop_hook "$TMP" "$ERR4" || EC4=$?

assert "第 4 次 exit 2（HAS_DIFF 触发兜底）" "[ '$EC4' -eq 2 ]"
assert "第 4 次 stderr 含 '兜底激活'" "grep -q '兜底激活' '$ERR4'"
assert "第 4 次 stderr 含 'PASS_CMD 全部阶段通过'" "grep -q 'PASS_CMD 全部阶段通过' '$ERR4'"
# bootstrap → setup → PASS → write_processed_cursor → state 删除
assert "游标文件已创建（V2.2 写入逻辑保留）" "[ -f '$CURSOR' ]"
assert "游标内容 == HEAD2" "[ \"\$(cat '$CURSOR' 2>/dev/null | tr -d '[:space:]')\" = '$HEAD2' ]"
assert "state 文件已被 rm（loop 结束）" "[ ! -f '$STATE_FILE' ]"

# ---- Step 6: HAS_DIFF 空 + 游标损坏 → 仍 exit 0（不再降级激活）----
echo "--- Step 6: 游标损坏 + 工作树干净 → V2.2 仍 exit 0（不再降级激活）----"
cd "$TMP"
# step 5 的 src/main.go staged 在 bootstrap PASS 后未被 commit，需 reset 干净
git reset -q HEAD src/main.go 2>/dev/null || true
rm -f src/main.go
git checkout -q -- README.md 2>/dev/null || true
echo "not-a-valid-sha" > "$CURSOR"

ERR5="$(mktemp)"
EC5=0
call_stop_hook "$TMP" "$ERR5" || EC5=$?

assert "第 5 次 exit 0（V2.2 工作树干净一律放行）" "[ '$EC5' -eq 0 ]"
assert "第 5 次 stderr 不含 '兜底激活'" "! grep -q '兜底激活' '$ERR5'"
# 游标损坏不会被刷新（V2.2 不进 bootstrap 路径，无 PASS 出口写游标）
assert "游标内容仍是损坏值（未走 PASS 写入）" "[ \"\$(cat '$CURSOR' 2>/dev/null | tr -d '[:space:]')\" = 'not-a-valid-sha' ]"

# ---- Step 7: V2.2.1 纯文档改动（CLAUDE.md / docs/ / *.txt / LICENSE / .gitignore）→ 静默放行 ----
echo "--- Step 7: 纯文档改动 → V2.2.1 期望静默 exit 0（不再因 *.md / docs/ 触发 NOOP loop）----"
cd "$TMP"
mkdir -p docs
echo "# initial doc" > CLAUDE.md
echo "# license" > LICENSE
echo "# changelog" > docs/CHANGELOG.md
echo "*.bak" > .gitignore
git add -A
git commit -q -m "chore(test): [cr_id_skip] Seed for doc whitelist test"
HEAD3="$(git rev-parse HEAD)"

# 改纯文档文件（unstaged）
echo "## new section" >> CLAUDE.md
echo "## v2" >> docs/CHANGELOG.md
echo "MIT" >> LICENSE
echo "*.tmp" >> .gitignore

DOC_DIFF="$(git diff --name-only)"
assert "纯文档改动 git diff 含 4 个文件" "[ \"\$(echo '$DOC_DIFF' | wc -l)\" -eq 4 ]"

ERR6="$(mktemp)"
EC6=0
call_stop_hook "$TMP" "$ERR6" || EC6=$?

assert "第 6 次 exit 0（V2.2.1 纯文档放行）" "[ '$EC6' -eq 0 ]"
assert "第 6 次 stderr 不含 '兜底激活'" "! grep -q '兜底激活' '$ERR6'"
assert "第 6 次 未创建 state（doc-only 未触发 setup）" "[ ! -f '$STATE_FILE' ]"

# ---- Step 8: V2.2.1 mixed 改动（doc + code）→ 仍触发 bootstrap ----
echo "--- Step 8: mixed 改动（doc + code 混合）→ V2.2.1 期望仍触发 bootstrap ----"
# 此时 step 7 的 4 个 doc 文件还 unstaged，新增 src/main.py（非白名单）让改动变 mixed
echo "real code" > src/main.py
git add src/main.py

ALL_CHANGED="$(git diff --name-only; git diff --cached --name-only)"
assert "mixed 改动含 src/main.py（非 doc）" "echo '$ALL_CHANGED' | grep -q 'src/main.py'"

ERR7="$(mktemp)"
EC7=0
call_stop_hook "$TMP" "$ERR7" || EC7=$?

assert "第 7 次 exit 2（mixed 改动仍触发）" "[ '$EC7' -eq 2 ]"
assert "第 7 次 stderr 含 '兜底激活'" "grep -q '兜底激活' '$ERR7'"

# Step 9 之前彻底 reset 工作树（避免 step 5/7/8 残余 staged/unstaged 影响判定）
# 注意：fixture 临时仓未把 .claude/builder-loop/ 加进 .gitignore，前面 setup 写的游标可能被 git tracked，
# 必须 git rm --cached 撤出索引，否则 step 9 的 git diff 会显示 builder-loop/last_processed_head（非白名单）→ 触发 bootstrap
cd "$TMP"
git reset -q --hard HEAD
git rm -q --cached -r .claude/builder-loop/ 2>/dev/null || true
git commit -q -m "chore(test): [cr_id_skip] Drop builder-loop runtime files from git" 2>/dev/null || true
rm -rf .claude/builder-loop/
git clean -qfd

# ---- Step 9: 仅 docs/ 子目录改动（不含 .md 后缀文件）→ 静默放行 ----
echo "--- Step 9: docs/ 下任意类型文件改动 → V2.2.1 静默放行（docs/ 路径模式）----"
mkdir -p docs/diagrams
echo "binary placeholder" > docs/diagrams/arch.svg
git add docs/diagrams/arch.svg
git commit -q -m "chore(test): [cr_id_skip] Add arch diagram"
echo "updated svg" > docs/diagrams/arch.svg

ERR8="$(mktemp)"
EC8=0
call_stop_hook "$TMP" "$ERR8" || EC8=$?

assert "第 8 次 exit 0（docs/ 路径放行 .svg）" "[ '$EC8' -eq 0 ]"
assert "第 8 次 stderr 不含 '兜底激活'" "! grep -q '兜底激活' '$ERR8'"

# ---- 汇总 ----
echo ""
echo "=== 汇总: ✅ PASS=${PASS}  ❌ FAIL=${FAIL} ==="

if [ "$FAIL" -gt 0 ]; then exit 1; fi
exit 0
