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

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "run-pass-cmd-args"

PASS_CMD_SCRIPT="${HARNESS_REPO_ROOT}/skills/builder-loop/scripts/run-pass-cmd.sh"
assert "run-pass-cmd.sh 脚本存在" "[ -f '$PASS_CMD_SCRIPT' ]"

TMP="$(mktemp -d)"
_HARNESS_TMPDIRS+=("$TMP")

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
  git -C "${dir}" init -q 2>/dev/null || true
  git -C "${dir}" config user.email "e2e@test.local"
  git -C "${dir}" config user.name "e2e-test"
  echo "seed" > "${dir}/README.md"
  git -C "${dir}" add -A 2>/dev/null || true
  git -C "${dir}" -c core.hooksPath=/dev/null commit -q \
    -m "chore(test): [cr_id_skip] Fixture seed" 2>/dev/null || true
}

# 辅助：跑 run-pass-cmd.sh 并返回 handle
# 用法：run_pass_cmd <run_cwd> <iter> [log_root]
run_pass_cmd() {
  local handle
  handle="$(mktemp -d -t harness-result-XXXXXX)"
  _HARNESS_TMPDIRS+=("$handle")
  local ec=0
  bash "${PASS_CMD_SCRIPT}" "$@" >"$handle/stdout" 2>"$handle/stderr" || ec=$?
  echo "$ec" > "$handle/ec"
  echo "$handle"
}

# ============================================================
# Case 1：三参调用 — 日志落 LOG_ROOT，不落 RUN_CWD
# ============================================================
section "Case 1: 三参调用 — 日志落 LOG_ROOT（RUN_CWD != LOG_ROOT）"
RUN_CWD1="${TMP}/run_cwd_1"
LOG_ROOT1="${TMP}/log_root_1"
make_loop_dir "${RUN_CWD1}"
mkdir -p "${LOG_ROOT1}"

RES1="$(run_pass_cmd "${RUN_CWD1}" 0 "${LOG_ROOT1}")"
assert_stdout_contains "Case 1: stdout 输出 PASS" "$RES1" '^PASS'
assert_file_exists "Case 1: 日志落在 LOG_ROOT/.claude/loop-runs/" "${LOG_ROOT1}/.claude/loop-runs/iter-0-smoke.log"
assert "Case 1: 日志不落在 RUN_CWD/.claude/loop-runs/" "[ ! -d '${RUN_CWD1}/.claude/loop-runs' ]"

# ============================================================
# Case 2：两参调用（缺第三参）— 日志落 RUN_CWD（log_root 缺省）
# ============================================================
section "Case 2: 两参调用（缺 log_root）— 日志落 RUN_CWD"
RUN_CWD2="${TMP}/run_cwd_2"
make_loop_dir "${RUN_CWD2}"

RES2="$(run_pass_cmd "${RUN_CWD2}" 0)"
assert_stdout_contains "Case 2: stdout 输出 PASS" "$RES2" '^PASS'
assert_file_exists "Case 2: 日志落在 RUN_CWD/.claude/loop-runs/" "${RUN_CWD2}/.claude/loop-runs/iter-0-smoke.log"

# ============================================================
# Case 3：三参 + FAIL stage — FAIL 消息含正确日志路径（LOG_ROOT）
# ============================================================
section "Case 3: 三参 + FAIL stage — FAIL 消息日志路径指向 LOG_ROOT"
RUN_CWD3="${TMP}/run_cwd_3"
LOG_ROOT3="${TMP}/log_root_3"
make_loop_dir "${RUN_CWD3}" "false"
mkdir -p "${LOG_ROOT3}"

RES3="$(run_pass_cmd "${RUN_CWD3}" 1 "${LOG_ROOT3}")"
assert_stdout_contains "Case 3: stdout 含 FAIL" "$RES3" '^FAIL'
assert_stdout_contains "Case 3: FAIL 消息日志路径含 LOG_ROOT3" "$RES3" "${LOG_ROOT3}"
assert_file_exists "Case 3: 日志文件落在 LOG_ROOT3" "${LOG_ROOT3}/.claude/loop-runs/iter-1-smoke.log"
assert "Case 3: 日志不落在 RUN_CWD3" "[ ! -d '${RUN_CWD3}/.claude/loop-runs' ]"

# ============================================================
# Case 4：RUN_CWD 内 loop.yml 缺失 → fallback 警告 + 用 LOG_ROOT 内 loop.yml 跑
# ============================================================
section "Case 4: RUN_CWD 无 loop.yml → fallback 警告 + 用 LOG_ROOT loop.yml 跑"
MAIN_REPO4="${TMP}/main_repo_4"
RUN_CWD4="${TMP}/run_cwd_4"
LOG_ROOT4="${TMP}/log_root_4"

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

git -C "${MAIN_REPO4}" init -q
git -C "${MAIN_REPO4}" config user.email "e2e@test.local"
git -C "${MAIN_REPO4}" config user.name "e2e-test"
echo "seed" > "${MAIN_REPO4}/README.md"
git -C "${MAIN_REPO4}" add -A
git -C "${MAIN_REPO4}" -c core.hooksPath=/dev/null commit -q \
  -m "chore(test): [cr_id_skip] Fixture seed case 4" 2>/dev/null || true
git -C "${MAIN_REPO4}" worktree add "${RUN_CWD4}" HEAD >/dev/null 2>&1 || true
rm -f "${RUN_CWD4}/.claude/loop.yml"

RES4="$(run_pass_cmd "${RUN_CWD4}" 0 "${LOG_ROOT4}")"
assert_stderr_contains "Case 4: stderr 含 fallback 关键词" "$RES4" 'allback'
assert_file_exists "Case 4: fallback 后仍产出日志文件" "${LOG_ROOT4}/.claude/loop-runs/iter-0-fallback_smoke.log"
assert_contains "Case 4: fallback 命令真跑了" "${LOG_ROOT4}/.claude/loop-runs/iter-0-fallback_smoke.log" 'FALLBACK_RAN'

harness_report
