#!/usr/bin/env bash
# test-run-pass-cmd-args.sh — V2.0 E2E：run-pass-cmd.sh 三参 vs 两参日志归档路径
#
# 验证场景：
#   1. 三参调用 run-pass-cmd.sh <run_cwd> <iter> <log_root> 时，
#      日志落在 $LOG_ROOT/.claude/loop-runs/，不在 $RUN_CWD/.claude/loop-runs/
#   2. 两参调用（缺第三参）时，日志落在 $RUN_CWD/.claude/loop-runs/（log_root 缺省 = run_cwd）
#   3. RUN_CWD 内 loop.yml 缺失时，stderr 含 "fallback" 警告（V2.0 fallback 行为）
#      + fallback 到主仓后仍能跑出日志
#
# 用法：bash test-run-pass-cmd-args.sh
# 退出码：0=全部通过 / 1=有失败

set -euo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$THIS_DIR/../../../.." && pwd)"
PASS_CMD_SCRIPT="${REPO_ROOT}/skills/builder-loop/scripts/run-pass-cmd.sh"

PASS=0
FAIL=0

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

pass() { echo -e "  ${GREEN}PASS${NC} $1"; PASS=$(( PASS + 1 )); }
fail() { echo -e "  ${RED}FAIL${NC} $1  [cond: $2]"; FAIL=$(( FAIL + 1 )); }

assert() {
  local desc="$1" cond="$2"
  if eval "$cond" 2>/dev/null; then pass "$desc"; else fail "$desc" "$cond"; fi
}

echo "=== V2.0 E2E: run-pass-cmd.sh 三参 vs 两参日志归档路径 ==="
assert "run-pass-cmd.sh 脚本存在" "[ -f '${PASS_CMD_SCRIPT}' ]"

TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
echo "    临时目录：${TMP}"

# ============================================================
# 辅助：创建一个带 loop.yml 的最小目录（含 git 仓库）
# $1 = 目标目录
# $2 = pass_cmd 里跑的命令（"true" 或 "false"）
# ============================================================
make_loop_dir() {
  local dir="$1"
  local cmd="${2:-true}"
  mkdir -p "${dir}/.claude" "${dir}/src"
  cat > "${dir}/.claude/loop.yml" <<YMLEOF
pass_cmd:
  - stage: smoke
    cmd: "${cmd}"
    timeout: 10
max_iterations: 3
layout:
  source_dirs: [src]
  test_dirs: []
worktree:
  enabled: false
YMLEOF
  # git 初始化（run-pass-cmd.sh 内某些路径可能调 git 操作）
  git -C "${dir}" init -q 2>/dev/null || true
  git -C "${dir}" config user.email "e2e@test.local"
  git -C "${dir}" config user.name "e2e-test"
  echo "seed" > "${dir}/README.md"
  git -C "${dir}" add -A 2>/dev/null || true
  git -C "${dir}" -c core.hooksPath=/dev/null commit -q \
    -m "chore(test): [cr_id_skip] Fixture seed" 2>/dev/null || true
}

# ============================================================
# Case 1：三参调用 — 日志落 LOG_ROOT，不落 RUN_CWD
# ============================================================
echo ""
echo "=== Case 1: 三参调用 — 日志落 LOG_ROOT（RUN_CWD ≠ LOG_ROOT）==="
RUN_CWD1="${TMP}/run_cwd_1"
LOG_ROOT1="${TMP}/log_root_1"
make_loop_dir "${RUN_CWD1}"
mkdir -p "${LOG_ROOT1}"

RESULT1=""
ERR1="${TMP}/err1.txt"
RESULT1="$(bash "${PASS_CMD_SCRIPT}" "${RUN_CWD1}" 0 "${LOG_ROOT1}" 2>"${ERR1}" || true)"

# 日志应落在 LOG_ROOT1
assert "Case 1: stdout 输出 PASS" \
  "echo '${RESULT1}' | grep -q '^PASS'"
assert "Case 1: 日志落在 LOG_ROOT/.claude/loop-runs/" \
  "[ -f '${LOG_ROOT1}/.claude/loop-runs/iter-0-smoke.log' ]"
assert "Case 1: 日志不落在 RUN_CWD/.claude/loop-runs/（RUN_CWD ≠ LOG_ROOT 时）" \
  "[ ! -d '${RUN_CWD1}/.claude/loop-runs' ]"

# ============================================================
# Case 2：两参调用（缺第三参）— 日志落 RUN_CWD（log_root 缺省）
# ============================================================
echo ""
echo "=== Case 2: 两参调用（缺 log_root）— 日志落 RUN_CWD ==="
RUN_CWD2="${TMP}/run_cwd_2"
make_loop_dir "${RUN_CWD2}"

RESULT2=""
ERR2="${TMP}/err2.txt"
RESULT2="$(bash "${PASS_CMD_SCRIPT}" "${RUN_CWD2}" 0 2>"${ERR2}" || true)"

assert "Case 2: stdout 输出 PASS" \
  "echo '${RESULT2}' | grep -q '^PASS'"
assert "Case 2: 日志落在 RUN_CWD/.claude/loop-runs/（log_root 缺省 = run_cwd）" \
  "[ -f '${RUN_CWD2}/.claude/loop-runs/iter-0-smoke.log' ]"

# ============================================================
# Case 3：三参 + FAIL stage — FAIL 消息含正确日志路径（LOG_ROOT）
# ============================================================
echo ""
echo "=== Case 3: 三参 + FAIL stage — FAIL 消息日志路径指向 LOG_ROOT ==="
RUN_CWD3="${TMP}/run_cwd_3"
LOG_ROOT3="${TMP}/log_root_3"
make_loop_dir "${RUN_CWD3}" "false"
mkdir -p "${LOG_ROOT3}"

RESULT3=""
ERR3="${TMP}/err3.txt"
RESULT3="$(bash "${PASS_CMD_SCRIPT}" "${RUN_CWD3}" 1 "${LOG_ROOT3}" 2>"${ERR3}" || true)"

assert "Case 3: stdout 含 FAIL" \
  "echo '${RESULT3}' | grep -q '^FAIL'"
assert "Case 3: FAIL 消息日志路径含 LOG_ROOT3" \
  "echo '${RESULT3}' | grep -q '${LOG_ROOT3}'"
assert "Case 3: 日志文件落在 LOG_ROOT3（不在 RUN_CWD3）" \
  "[ -f '${LOG_ROOT3}/.claude/loop-runs/iter-1-smoke.log' ]"
assert "Case 3: 日志不落在 RUN_CWD3" \
  "[ ! -d '${RUN_CWD3}/.claude/loop-runs' ]"

# ============================================================
# Case 4：RUN_CWD 内 loop.yml 缺失 → fallback 警告 + 用 LOG_ROOT 内 loop.yml 跑
#
# 模拟 worktree 内 loop.yml 未 commit 的场景（V2.0 行为）：
#   - RUN_CWD4（worktree）无 loop.yml
#   - LOG_ROOT4（主仓）有 loop.yml（含 fallback_smoke stage）
#   - 期望：
#     a. stderr 含 "fallback" 关键词（V2.0 警告）
#     b. 日志落 LOG_ROOT4（fallback 后跑 LOG_ROOT4 的 loop.yml）
#     c. fallback 阶段名 fallback_smoke 出现在日志路径中
# ============================================================
echo ""
echo "=== Case 4: RUN_CWD 无 loop.yml → fallback 警告 + 用 LOG_ROOT loop.yml 跑 ==="
MAIN_REPO4="${TMP}/main_repo_4"
RUN_CWD4="${TMP}/run_cwd_4"
LOG_ROOT4="${TMP}/log_root_4"

# 建主仓 + 在 LOG_ROOT4 放一份 loop.yml（fallback 目标）
mkdir -p "${MAIN_REPO4}/.claude" "${MAIN_REPO4}/src"
mkdir -p "${LOG_ROOT4}/.claude"
cat > "${LOG_ROOT4}/.claude/loop.yml" <<YMLEOF4
pass_cmd:
  - stage: fallback_smoke
    cmd: "echo FALLBACK_RAN"
    timeout: 10
max_iterations: 3
layout:
  source_dirs: [src]
  test_dirs: []
worktree:
  enabled: false
YMLEOF4

# 初始化 git 仓库（不带 loop.yml）+ worktree
git -C "${MAIN_REPO4}" init -q
git -C "${MAIN_REPO4}" config user.email "e2e@test.local"
git -C "${MAIN_REPO4}" config user.name "e2e-test"
echo "seed" > "${MAIN_REPO4}/README.md"
git -C "${MAIN_REPO4}" add -A
git -C "${MAIN_REPO4}" -c core.hooksPath=/dev/null commit -q \
  -m "chore(test): [cr_id_skip] Fixture seed case 4" 2>/dev/null || true
git -C "${MAIN_REPO4}" worktree add "${RUN_CWD4}" HEAD >/dev/null 2>&1 || true
# 确认 worktree 内无 loop.yml（git checkout 不会带入 LOG_ROOT4 的 loop.yml）
rm -f "${RUN_CWD4}/.claude/loop.yml"

RESULT4=""
ERR4="${TMP}/err4.txt"
RESULT4="$(bash "${PASS_CMD_SCRIPT}" "${RUN_CWD4}" 0 "${LOG_ROOT4}" 2>"${ERR4}" || true)"

assert "Case 4: stderr 含 fallback 关键词（V2.0 fallback 提示）" \
  "grep -qi 'fallback' '${ERR4}'"
assert "Case 4: fallback 后仍产出日志文件（LOG_ROOT4 的 fallback_smoke stage）" \
  "[ -f '${LOG_ROOT4}/.claude/loop-runs/iter-0-fallback_smoke.log' ]"
assert "Case 4: fallback 命令真跑了（日志含 FALLBACK_RAN）" \
  "grep -q 'FALLBACK_RAN' '${LOG_ROOT4}/.claude/loop-runs/iter-0-fallback_smoke.log'"

# ============================================================
# 汇总
# ============================================================
echo ""
echo "=============================="
echo "测试结果汇总"
echo "=============================="
echo -e "  ${GREEN}PASS: ${PASS}${NC}"
if [ "${FAIL}" -gt 0 ]; then
  echo -e "  ${RED}FAIL: ${FAIL}${NC}"
  exit 1
else
  echo -e "  ${GREEN}FAIL: 0${NC}"
  exit 0
fi
