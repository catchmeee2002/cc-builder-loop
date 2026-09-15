#!/usr/bin/env bash
# builder-loop V8 唯一 hook 入口：`bl-hook.sh <event>`，stdin 原样透传给 runtime。
# 任何异常都不能阻塞会话：runtime 内部把错误写 stderr 并 exit 0，这里只兜底找不到 python 的情况。
set -u
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
RUNTIME="${HERE}/../runtime"
if ! command -v python3 >/dev/null 2>&1; then
  echo "[builder-loop hook] python3 不可用" >&2
  exit 0
fi
export PYTHONDONTWRITEBYTECODE=1
PYTHONPATH="${RUNTIME}${PYTHONPATH:+:${PYTHONPATH}}" exec python3 -m builder_loop hook "$1"
