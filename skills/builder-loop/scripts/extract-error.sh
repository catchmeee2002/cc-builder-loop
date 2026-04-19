#!/usr/bin/env bash
# extract-error.sh — 错误反馈处理器（V1 = full 模式 + 精确脱敏）
#
# 用法：bash extract-error.sh <log_file> <stage> [project_root]
#
# stdout：处理后的反馈文本（builder 下轮 prompt 用）
# 退出码：0=成功输出（即便脱敏失败也保底原样输出）
#
# V1 设计原则：
#   - 不裁剪信息（信任 1M opus 上下文）
#   - 仅对 pytest assertion 字面值 + 用例名做精确脱敏（防作弊）
#   - 保留所有 Error type、坐标、traceback、堆栈
#   - 任何脱敏失败 → 原样传，绝不给空字符串

set -euo pipefail

LOG_FILE="${1:?log_file required}"
STAGE="${2:?stage required}"
PROJECT_ROOT="${3:-}"

# ---- 从 loop.yml 读 max_chars（python3 yaml + fallback 硬编码）----
DEFAULT_MAX_CHARS=500000
read_max_chars() {
  local yml="${PROJECT_ROOT}/.claude/loop.yml"
  [ -z "$PROJECT_ROOT" ] || [ ! -f "$yml" ] && { echo "$DEFAULT_MAX_CHARS"; return 0; }
  python3 -c "
import yaml, sys
with open(sys.argv[1]) as f:
    cfg = yaml.safe_load(f) or {}
ef = cfg.get('error_feedback', {}) or {}
print(ef.get('max_chars', $DEFAULT_MAX_CHARS))
" "$yml" 2>/dev/null && return 0
  echo "$DEFAULT_MAX_CHARS"
}
MAX_CHARS="$(read_max_chars)"

if [ ! -f "$LOG_FILE" ]; then
  echo "[extract-error] 日志文件不存在：$LOG_FILE" >&2
  exit 0
fi

RAW="$(cat "$LOG_FILE")"
PROCESSED="$RAW"

# ---- 仅对 test 阶段做脱敏（其他阶段原样传）----
if [ "$STAGE" = "test" ] || [ "$STAGE" = "tests" ]; then
  # 脱敏 1：pytest assertion 字面值
  # 例 `assert x == 5` → `assert x == <expected>`
  # 例 `Expected: [1,2,3], Got: [1,2]` → `Expected: <expected>, Got: <actual>`
  # 用 python 处理，失败回退原文。
  # 注意：python3 - 读 stdin 已被 heredoc 占用，改用环境变量传 RAW。
  DESENS="$(BUILDER_LOOP_RAW="$RAW" python3 - <<'PY' 2>/dev/null || true
import os, re
text = os.environ.get('BUILDER_LOOP_RAW', '')
# pytest assertion: assert <lhs> == <rhs>
text = re.sub(r'(assert\s+[^=]+==\s*)([^,\n]+?)(\s*,\s*got\s+)([^,\n]+)',
              r'\1<expected>\3<actual>', text)
text = re.sub(r'(Expected:\s*)([^\n,]+)(,?\s*Got:\s*)([^\n]+)',
              r'\1<expected>\3<actual>', text)
# pytest 用例名脱敏：def test_xxx(  → def test_<redacted>(
text = re.sub(r'(def\s+test_)\w+(\s*\()', r'\1<redacted>\2', text)
# pytest FAILED 行的用例名
text = re.sub(r'(FAILED\s+\S+::test_)\w+', r'\1<redacted>', text)
import sys; sys.stdout.write(text)
PY
)"
  if [ -n "$DESENS" ]; then
    PROCESSED="$DESENS"
  fi
fi

# ---- 长度截断（超 MAX_CHARS 从中间截断）----
LEN=${#PROCESSED}
if [ "$LEN" -gt "$MAX_CHARS" ]; then
  HEAD_LEN=$((MAX_CHARS / 2))
  TAIL_LEN=$((MAX_CHARS - HEAD_LEN))
  HEAD="${PROCESSED:0:$HEAD_LEN}"
  TAIL="${PROCESSED: -$TAIL_LEN}"
  PROCESSED="${HEAD}

[... TRUNCATED MIDDLE: ${LEN} chars total, see full log file ...]

${TAIL}"
fi

# ---- 输出 ----
cat <<EOF
[stage=${STAGE} log=${LOG_FILE}]
${PROCESSED}

---
[完整原始日志: ${LOG_FILE}，本反馈已 V1 脱敏（仅 pytest 字面值 + 用例名），需要时可 Read 原文]
EOF
