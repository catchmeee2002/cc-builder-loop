#!/usr/bin/env bash
# run-pass-cmd.sh — 按阶段顺序跑 loop.yml.pass_cmd
#
# 用法：bash run-pass-cmd.sh <run_cwd> <iter_num> [<log_root>]
#   run_cwd  = 干活的地方（V2.0 = worktree / bare = 主仓）
#              LOOP_YML 从此读，PASS_CMD 在此跑
#   log_root = 日志归档根 + loop.yml fallback 源（V2.0 = 主仓 / 缺省时 = run_cwd）
#              worktree 模式必须传主仓路径，即 state.main_repo_path。
#              传 state.project_root 会踩坑：worktree 模式下该字段 = worktree_path，
#              于是 log_root == run_cwd，下方 fallback 条件不成立（issue #67）
#
# 输出（stdout 最后一行）：
#   PASS                              ← 至少跑过一个 stage 且全部通过
#   FAIL <stage> <log_file_path>      ← 判据执行了、结果为负 → 改代码
#   FATAL <reason>                    ← 判据没能执行（loop.yml 缺失 / 解析失败 / pass_cmd 为空）
#                                       → 改参数或配置，改代码没用
#
# 退出码：0=PASS，1=FAIL，2=FATAL
#
# 副作用：
#   - 每阶段日志落地 <log_root>/.claude/loop-runs/iter-<N>-<stage>.log
#   - 阶段超时则在日志末尾写 [TIMEOUT]

set -euo pipefail

RUN_CWD="${1:?run_cwd required}"
ITER="${2:?iter number required}"
LOG_ROOT="${3:-$RUN_CWD}"
LOOP_YML="${RUN_CWD}/.claude/loop.yml"
LOG_DIR="${LOG_ROOT}/.claude/loop-runs"
mkdir -p "$LOG_DIR"

# V2.0 兼容：worktree 启用但 loop.yml 未 commit 时 worktree 内不存在 → fallback 到主仓
# 真实场景：用户首次接入仓库写完 loop.yml 立即跑 setup，loop.yml 还在 untracked 状态
if [ ! -f "$LOOP_YML" ] && [ "$LOG_ROOT" != "$RUN_CWD" ] && [ -f "${LOG_ROOT}/.claude/loop.yml" ]; then
  echo "[run-pass-cmd] ⚠️  ${LOOP_YML} 不存在（可能 worktree 内 loop.yml 未 commit），fallback 到主仓 ${LOG_ROOT}/.claude/loop.yml" >&2
  LOOP_YML="${LOG_ROOT}/.claude/loop.yml"
fi

if [ ! -f "$LOOP_YML" ]; then
  echo "[run-pass-cmd] ❌ loop.yml 不存在：${LOOP_YML}" >&2
  echo "[run-pass-cmd]    run_cwd=${RUN_CWD}" >&2
  echo "[run-pass-cmd]    log_root=${LOG_ROOT}" >&2
  echo "[run-pass-cmd]    两者相同时上方 fallback 不触发。worktree 模式下第三参数须传主仓路径" >&2
  echo "[run-pass-cmd]    （state.main_repo_path），而非 state.project_root（= worktree_path）" >&2
  echo "FATAL loop.yml not found: ${LOOP_YML}"
  exit 2
fi

# ---- 简易 yaml 解析（仅取 pass_cmd 数组）----
# 不引入额外依赖（yq 可能没装），用 python3 解析
parse_pass_cmd() {
  python3 -c "
import sys, yaml
with open('$LOOP_YML') as f:
    cfg = yaml.safe_load(f) or {}
for item in (cfg.get('pass_cmd') or []):
    if isinstance(item, dict):
        stage = item.get('stage', 'unknown')
        cmd = item.get('cmd', '')
        timeout = item.get('timeout', 300)
        print(f'{stage}\t{timeout}\t{cmd}')
"
}

# ---- 解析 pass_cmd ----
# 不写成 `done < <(parse_pass_cmd)`：process substitution 的退出码父 shell 收不到，
# set -e 也够不着它。parse 崩掉（loop.yml 缺失 / yaml 损坏）时 while 只是读到空输入 →
# 零次循环 → 直落末尾 echo "PASS"，一个 stage 都没跑却报通过（issue #67）。
# 改为先捕获、显式判退出码，再用 here-string 喂循环。
STAGES=""
if ! STAGES="$(parse_pass_cmd)"; then
  echo "[run-pass-cmd] ❌ pass_cmd 解析失败（见上方 python traceback）：${LOOP_YML}" >&2
  echo "FATAL pass_cmd parse failed: ${LOOP_YML}"
  exit 2
fi

if [ -z "${STAGES//[[:space:]]/}" ]; then
  echo "[run-pass-cmd] ❌ loop.yml 的 pass_cmd 为空或缺失，没有任何机器判据可跑：${LOOP_YML}" >&2
  echo "FATAL pass_cmd is empty: ${LOOP_YML}"
  exit 2
fi

# ---- 主循环 ----
RAN=0
while IFS=$'\t' read -r STAGE TIMEOUT CMD; do
  [ -z "$STAGE" ] && continue
  RAN=$((RAN + 1))
  LOG="${LOG_DIR}/iter-${ITER}-${STAGE}.log"
  echo "▶ stage=${STAGE} timeout=${TIMEOUT}s cmd=${CMD}" | tee "$LOG"

  # cmd 引号转义校验 — 含引号/反引号时 warn（结构化格式便于 grep 过滤）
  # 安全说明：eval 在子 shell 内执行（bash -c），cmd 来自项目 owner 的 loop.yml，
  # 非外部用户输入，风险可控。保留 eval 是因为 cmd 可能含管道/重定向/引号等 shell 语法。
  if printf '%s' "$CMD" | grep -qE '["`]'; then
    echo "[WARN] stage=${STAGE} cmd contains quotes/backticks, executing via eval in sub-shell" | tee -a "$LOG" >&2
  fi

  set +e
  # shellcheck disable=SC2016
  # single quotes intentional: $1/$2 expand in inner bash, not outer
  timeout "${TIMEOUT}s" bash -c 'cd "$1" && eval "$2"' -- "$RUN_CWD" "$CMD" >> "$LOG" 2>&1
  EC=$?
  set -e

  if [ "$EC" -eq 124 ]; then
    echo "[TIMEOUT after ${TIMEOUT}s]" >> "$LOG"
    echo "FAIL ${STAGE} ${LOG}"
    exit 1
  fi
  if [ "$EC" -ne 0 ]; then
    echo "FAIL ${STAGE} ${LOG}"
    exit 1
  fi
done <<< "$STAGES"

# 解析出内容却一个 stage 都没真跑（如全是空行）→ 同样不许报 PASS
if [ "$RAN" -eq 0 ]; then
  echo "[run-pass-cmd] ❌ 解析出 pass_cmd 但零个有效 stage 被执行：${LOOP_YML}" >&2
  echo "FATAL no stage executed: ${LOOP_YML}"
  exit 2
fi

echo "PASS"
exit 0
