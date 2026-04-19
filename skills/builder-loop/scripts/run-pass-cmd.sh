#!/usr/bin/env bash
# run-pass-cmd.sh — 按阶段顺序跑 loop.yml.pass_cmd
#
# 用法：bash run-pass-cmd.sh <project_root> <iter_num>
#
# 输出（stdout）：
#   PASS                              ← 全部阶段通过
#   FAIL <stage> <log_file_path>      ← 任一阶段失败，输出失败的阶段名和日志路径
#
# 退出码：0=PASS，非0=FAIL
#
# 副作用：
#   - 每阶段日志落地 <log_dir>/iter-<N>-<stage>.log
#   - 阶段超时则在日志末尾写 [TIMEOUT]

set -euo pipefail

PROJECT_ROOT="${1:?project_root required}"
ITER="${2:?iter number required}"
LOOP_YML="${PROJECT_ROOT}/.claude/loop.yml"
LOG_DIR="${PROJECT_ROOT}/.claude/loop-runs"
STATE_FILE="${PROJECT_ROOT}/.claude/builder-loop.local.md"
mkdir -p "$LOG_DIR"

# T2.5：worktree 启用时，PASS_CMD 应在 worktree 内执行（能看到 builder 改动）
# 日志仍落主项目 log_dir（集中归档），命令工作目录切到 worktree
RUN_CWD="$PROJECT_ROOT"
if [ -f "$STATE_FILE" ]; then
  WT="$(grep -E '^worktree_path:' "$STATE_FILE" | head -1 | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/')"
  if [ -n "$WT" ] && [ -d "$WT" ]; then
    RUN_CWD="$WT"
  fi
fi

# ---- 简易 yaml 解析（仅取 pass_cmd 数组）----
# 不引入额外依赖（yq 可能没装），用 python3 解析
parse_pass_cmd() {
  python3 -c "
import sys, yaml
with open('$LOOP_YML') as f:
    cfg = yaml.safe_load(f)
for item in cfg.get('pass_cmd', []):
    if isinstance(item, dict):
        stage = item.get('stage', 'unknown')
        cmd = item.get('cmd', '')
        timeout = item.get('timeout', 300)
        print(f'{stage}\t{timeout}\t{cmd}')
"
}

# ---- 主循环 ----
while IFS=$'\t' read -r STAGE TIMEOUT CMD; do
  [ -z "$STAGE" ] && continue
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
done < <(parse_pass_cmd)

echo "PASS"
exit 0
