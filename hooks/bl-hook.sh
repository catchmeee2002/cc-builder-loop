#!/usr/bin/env bash
# builder-loop 唯一 hook 入口：`bl-hook.sh <event>`，stdin 原样透传给 runtime。
#
# 快速路径：PreToolUse 挂在 Read / Bash 等高频工具上，绝大多数 session 根本没有绑定 run。
# 先用纯 bash（零子进程）从 stdin 抠出 session_id，看有没有对应的 session 指针；没有就直接退出，
# 不启动 python。任何异常都不能阻塞会话：runtime 内部把错误写 stderr 并 exit 0。
set -u
INPUT="$(cat)"

# SessionStart：让本会话的 Bash（含 subagent）直接找得到 bl。CC 把写进 CLAUDE_ENV_FILE 的 export
# 注入之后的每条 Bash 命令（2.1.272 实测主会话与 subagent 都生效）。所有会话都会走到这里，所以不起 python。
# 加的是本仓的 bin/：PATH 上的 bl 与注册这些 hook 的 runtime 必然是同一份代码（#245）。
if [[ "${1:-}" == "SessionStart" ]]; then
  if [[ -n "${CLAUDE_ENV_FILE:-}" ]]; then
    BIN="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../bin" && pwd)"
    printf 'export PATH=%q:"$PATH"\n' "$BIN" >> "$CLAUDE_ENV_FILE"
  fi
  exit 0
fi

HOME_DIR="${BUILDER_LOOP_HOME:-$HOME/.claude/builder-loop}"
if [[ "$INPUT" =~ \"session_id\"[[:space:]]*:[[:space:]]*\"([^\"]+)\" ]]; then
  SID="${BASH_REMATCH[1]//[^A-Za-z0-9_-]/_}"
  [[ -f "$HOME_DIR/sessions/$SID.json" ]] || exit 0
else
  exit 0
fi

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
command -v python3 >/dev/null 2>&1 || { echo "[builder-loop hook] python3 不可用" >&2; exit 0; }
export PYTHONDONTWRITEBYTECODE=1
PYTHONPATH="${HERE}/../runtime${PYTHONPATH:+:${PYTHONPATH}}" exec python3 -m builder_loop hook "$1" <<<"$INPUT"
