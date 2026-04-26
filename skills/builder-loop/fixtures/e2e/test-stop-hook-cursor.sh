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

# ---- Step 5: HAS_DIFF 非空 → 触发 bootstrap + 写游标 ----
echo "--- Step 5: 未提交改动 → 触发 bootstrap，验证游标写入仍工作 ----"
echo "local edit" >> README.md
DIFF_STAT="$(git diff --stat)"
assert "HAS_DIFF 非空" "[ -n '$DIFF_STAT' ]"

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
git checkout -q -- README.md
echo "not-a-valid-sha" > "$CURSOR"

ERR5="$(mktemp)"
EC5=0
call_stop_hook "$TMP" "$ERR5" || EC5=$?

assert "第 5 次 exit 0（V2.2 工作树干净一律放行）" "[ '$EC5' -eq 0 ]"
assert "第 5 次 stderr 不含 '兜底激活'" "! grep -q '兜底激活' '$ERR5'"
# 游标损坏不会被刷新（V2.2 不进 bootstrap 路径，无 PASS 出口写游标）
assert "游标内容仍是损坏值（未走 PASS 写入）" "[ \"\$(cat '$CURSOR' 2>/dev/null | tr -d '[:space:]')\" = 'not-a-valid-sha' ]"

# ---- 汇总 ----
echo ""
echo "=== 汇总: ✅ PASS=${PASS}  ❌ FAIL=${FAIL} ==="

if [ "$FAIL" -gt 0 ]; then exit 1; fi
exit 0
