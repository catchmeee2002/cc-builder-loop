#!/usr/bin/env bash
# test-stop-hook-cursor.sh — E2E：V1.8.2 兜底激活 HEAD 游标
#
# 覆盖场景：
#   1. 首次 Stop（刚 commit）→ 兜底激活 + 写游标
#   2. 同 HEAD 再次 Stop → 游标命中 exit 0 静默
#   3. 新 commit 后 Stop → 游标过期，重新激活
#   4. HEAD 未动但有未提交改动 → HAS_DIFF 优先，仍然激活
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

echo "=== E2E: V1.8.2 兜底激活 HEAD 游标 ==="
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

# ---- Step 2: 第 1 次 Stop → 兜底激活 + 写游标 ----
echo "--- Step 2: 首次 Stop → 期望激活 bootstrap + 写游标 ----"
ERR1="$(mktemp)"
EC1=0
call_stop_hook "$TMP" "$ERR1" || EC1=$?

assert "第 1 次 exit 2（PASS 续接）" "[ '$EC1' -eq 2 ]"
assert "第 1 次 stderr 含 '兜底激活'" "grep -q '兜底激活' '$ERR1'"
assert "第 1 次 stderr 含 'PASS_CMD 全部阶段通过'" "grep -q 'PASS_CMD 全部阶段通过' '$ERR1'"
assert "游标文件已创建" "[ -f '$CURSOR' ]"
assert "游标内容 == HEAD1" "[ \"\$(cat '$CURSOR' 2>/dev/null | tr -d '[:space:]')\" = '$HEAD1' ]"
assert "state 文件已被 rm（loop 结束）" "[ ! -f '$STATE_FILE' ]"

# ---- Step 3: 第 2 次 Stop（同 HEAD）→ 游标命中 exit 0 ----
echo "--- Step 3: 同 HEAD 再次 Stop → 期望游标命中静默 ----"
ERR2="$(mktemp)"
EC2=0
call_stop_hook "$TMP" "$ERR2" || EC2=$?

assert "第 2 次 exit 0（游标跳过）" "[ '$EC2' -eq 0 ]"
assert "第 2 次 stderr 不含 '兜底激活'" "! grep -q '兜底激活' '$ERR2'"
assert "第 2 次 未创建新 state" "[ ! -f '$STATE_FILE' ]"
assert "游标仍等于 HEAD1（未被覆盖为空）" "[ \"\$(cat '$CURSOR' 2>/dev/null | tr -d '[:space:]')\" = '$HEAD1' ]"

# ---- Step 4: 新 commit → 第 3 次 Stop 应重新激活 ----
echo "--- Step 4: 新增 commit → 期望游标过期，重新 bootstrap ----"
echo "more" > CHANGES.md
git add -A
git commit -q -m "feat(test): [cr_id_skip] Second commit for cursor test"
HEAD2="$(git rev-parse HEAD)"

assert "HEAD2 与 HEAD1 不同" "[ '$HEAD1' != '$HEAD2' ]"

ERR3="$(mktemp)"
EC3=0
call_stop_hook "$TMP" "$ERR3" || EC3=$?

assert "第 3 次 exit 2（HEAD 前进后重新激活）" "[ '$EC3' -eq 2 ]"
assert "第 3 次 stderr 含 '兜底激活'" "grep -q '兜底激活' '$ERR3'"
assert "游标已更新为 HEAD2" "[ \"\$(cat '$CURSOR' 2>/dev/null | tr -d '[:space:]')\" = '$HEAD2' ]"

# ---- Step 5: HEAD 未动 + HAS_DIFF 非空 → 游标不阻塞 ----
echo "--- Step 5: 未提交改动 + 同 HEAD → 期望 HAS_DIFF 优先，触发 bootstrap ----"
echo "local edit" >> README.md
# 确认此刻有 unstaged diff
DIFF_STAT="$(git diff --stat)"
assert "HAS_DIFF 非空" "[ -n '$DIFF_STAT' ]"

ERR4="$(mktemp)"
EC4=0
call_stop_hook "$TMP" "$ERR4" || EC4=$?

assert "第 4 次 exit 2（HAS_DIFF 优先于游标）" "[ '$EC4' -eq 2 ]"
assert "第 4 次 stderr 含 '兜底激活'" "grep -q '兜底激活' '$ERR4'"

# ---- Step 6: 游标文件损坏 → 降级为旧行为 ----
echo "--- Step 6: 游标内容损坏 → 期望降级为旧 bootstrap ----"
# 清理未提交改动，让 HAS_DIFF 空
cd "$TMP"
git checkout -q -- README.md
# 损坏游标
echo "not-a-valid-sha" > "$CURSOR"

ERR5="$(mktemp)"
EC5=0
call_stop_hook "$TMP" "$ERR5" || EC5=$?

assert "第 5 次 exit 2（游标损坏 → 降级 bootstrap）" "[ '$EC5' -eq 2 ]"
assert "第 5 次 stderr 含 '兜底激活'" "grep -q '兜底激活' '$ERR5'"
assert "游标已被刷新为真实 HEAD" "[ \"\$(cat '$CURSOR' 2>/dev/null | tr -d '[:space:]')\" = '$HEAD2' ]"

# ---- 汇总 ----
echo ""
echo "=== 汇总: ✅ PASS=${PASS}  ❌ FAIL=${FAIL} ==="

if [ "$FAIL" -gt 0 ]; then exit 1; fi
exit 0
