#!/usr/bin/env bash
# run-judge-agent.sh — Judge agent (V1.9+)
#
# 职责：在 stop hook PASS_CMD 判据之上叠加一道 LLM 语义判定，识别 PASS_CMD 二值判据看不见的盲区
# （假完成 / 求助 / 偷懒 / 网络中断）。
#
# 调用约定：
#   bash run-judge-agent.sh \
#     --state-file <path>          # state.yml 路径（必填）
#     --project-root <path>        # 项目根（必填）
#     --transcript-path <path>     # CC transcript jsonl（必填）
#     --pass-cmd-status PASS|FAIL  # 上轮 PASS_CMD 结果（必填）
#     [--pass-cmd-stage <name>]    # 失败阶段名（FAIL 时填）
#     [--pass-cmd-log <path>]      # 失败日志路径（FAIL 时填）
#
#   bash run-judge-agent.sh --self-check
#     输出凭证状态 / 模型选择 / loop.yml 路径，不调真实 API。
#
# 输出（始终一行 JSON 到 stdout）：
#   {"action":"...", "confidence":..., "reason":"...", "downgraded":..., "downgrade_reason":"...",
#    "model_used":"...", "credential_path":"env|oauth|none", "elapsed_ms":...}
#
# 退出码：始终 0（脚本本身不失败；任何错误通过 downgraded=true 表达）。
#
# 凭证检测优先级：
#   ANTHROPIC_API_KEY (env) > ~/.claude.json oauthAccount.accessToken (oauth) > none
#   注意：env 优先于 oauth，因为 copilot 方案会同时存在 ~/.claude.json，但需要走 ANTHROPIC_BASE_URL。
#
# 模型选择三层 fallback：
#   loop.yml.judge.model > $ANTHROPIC_DEFAULT_HAIKU_MODEL > "claude-haiku-4-5"
#   命名规范：dot 写法（claude-haiku-4.5）自动规范化为 dash（claude-haiku-4-5）。

# 不用 set -e：所有错误路径走降级，不要让脚本异常退出。
set -uo pipefail

# ==================================================================
# 默认参数
# ==================================================================
JUDGE_STATE_FILE=""
JUDGE_PROJECT_ROOT=""
JUDGE_TRANSCRIPT_PATH=""
JUDGE_PASS_CMD_STATUS=""
JUDGE_PASS_CMD_STAGE=""
JUDGE_PASS_CMD_LOG=""
JUDGE_SELF_CHECK=false

START_TS_MS=$(($(date +%s%N 2>/dev/null || echo "$(date +%s)000000000")/1000000))

# ==================================================================
# 参数解析
# ==================================================================
while [ $# -gt 0 ]; do
  case "$1" in
    --state-file)         JUDGE_STATE_FILE="${2:-}"; shift 2 ;;
    --project-root)       JUDGE_PROJECT_ROOT="${2:-}"; shift 2 ;;
    --transcript-path)    JUDGE_TRANSCRIPT_PATH="${2:-}"; shift 2 ;;
    --pass-cmd-status)    JUDGE_PASS_CMD_STATUS="${2:-}"; shift 2 ;;
    --pass-cmd-stage)     JUDGE_PASS_CMD_STAGE="${2:-}"; shift 2 ;;
    --pass-cmd-log)       JUDGE_PASS_CMD_LOG="${2:-}"; shift 2 ;;
    --self-check)         JUDGE_SELF_CHECK=true; shift ;;
    *) shift ;;
  esac
done

SKILL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd 2>/dev/null)" || \
  SKILL_ROOT="$HOME/.claude/skills/builder-loop"

# ==================================================================
# 输出 JSON 工具
# ==================================================================
output_result_json() {
  # 参数顺序：action confidence reason downgraded downgrade_reason model_used credential_path
  local action="$1" confidence="$2" reason="$3" downgraded="$4" dgr="$5" model="$6" cred="$7"
  local end_ts_ms=$(($(date +%s%N 2>/dev/null || echo "$(date +%s)000000000")/1000000))
  local elapsed=$((end_ts_ms - START_TS_MS))
  python3 - "$action" "$confidence" "$reason" "$downgraded" "$dgr" "$model" "$cred" "$elapsed" <<'PY'
import json, sys
out = {
    "action": sys.argv[1],
    "confidence": float(sys.argv[2]) if sys.argv[2] else 0.0,
    "reason": sys.argv[3],
    "downgraded": sys.argv[4] == "true",
    "downgrade_reason": sys.argv[5],
    "model_used": sys.argv[6],
    "credential_path": sys.argv[7],
    "elapsed_ms": int(sys.argv[8]),
}
print(json.dumps(out, ensure_ascii=False))
PY
}

# 降级输出（PASS 默认 stop_done，FAIL 默认 continue_strict）
output_downgrade() {
  local reason="$1" model="${2:-}" cred="${3:-none}"
  local action="stop_done"
  if [ "$JUDGE_PASS_CMD_STATUS" = "FAIL" ]; then
    action="continue_strict"
  fi
  output_result_json "$action" "0.0" "downgraded:$reason" "true" "$reason" "$model" "$cred"
}

# ==================================================================
# 凭证检测：env > oauth > none
# ==================================================================
detect_credentials() {
  if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    echo "env"
    return 0
  fi
  if [ -f "$HOME/.claude.json" ]; then
    local has_tok
    has_tok="$(python3 - "$HOME/.claude.json" 2>/dev/null <<'PY'
import json, sys
try:
    j = json.load(open(sys.argv[1]))
    oa = j.get('oauthAccount') or {}
    tok = oa.get('accessToken') or oa.get('access_token')
    print('yes' if tok else 'no')
except Exception:
    print('no')
PY
)"
    if [ "$has_tok" = "yes" ]; then
      echo "oauth"
      return 0
    fi
  fi
  echo "none"
}

# 读 OAuth access token
read_oauth_token() {
  python3 - "$HOME/.claude.json" 2>/dev/null <<'PY'
import json, sys
try:
    j = json.load(open(sys.argv[1]))
    oa = j.get('oauthAccount') or {}
    tok = oa.get('accessToken') or oa.get('access_token') or ""
    print(tok)
except Exception:
    pass
PY
}

# ==================================================================
# loop.yml judge 段读取（不依赖 PyYAML）
# ==================================================================
read_judge_config() {
  local yml="${JUDGE_PROJECT_ROOT}/.claude/loop.yml"
  python3 - "$yml" <<'PY'
import sys, re, os
path = sys.argv[1]
defaults = {
    'enabled': 'true',
    'model': '',
    'confidence_threshold': '0.5',
    'max_consecutive_nudges': '2',
    'api_timeout_sec': '8',
    'system_prompt_path': '',
}
fields = dict(defaults)
try:
    text = open(path).read() if os.path.isfile(path) else ''
except Exception:
    text = ''
# 找 "judge:" 段（顶层）
m = re.search(r'(?ms)^judge:\s*(?:#[^\n]*)?\n((?:[ \t]+[^\n]*\n?)*)', text)
if m:
    block = m.group(1)
    for key in fields:
        # 匹配 "  key: value" 形式（支持 "value"、'value'、bare）
        km = re.search(rf'(?m)^\s+{key}:\s*(?:"([^"]*)"|\'([^\']*)\'|([^\n#]*))', block)
        if km:
            val = (km.group(1) or km.group(2) or km.group(3) or '').strip()
            fields[key] = val
# 输出 KEY=VALUE 一行（VALUE 用 base64 编码避免特殊字符问题不大；这里值都简单）
for k, v in fields.items():
    print(f'{k}={v}')
PY
}

# ==================================================================
# 模型选择 + 规范化（dot → dash）
# ==================================================================
resolve_model() {
  local from_yml="$1" from_env="${ANTHROPIC_DEFAULT_HAIKU_MODEL:-}"
  local raw=""
  if [ -n "$from_yml" ]; then
    raw="$from_yml"
  elif [ -n "$from_env" ]; then
    raw="$from_env"
  else
    raw="claude-haiku-4-5"
  fi
  echo "$raw" | sed -E 's/([0-9])\.([0-9])/\1-\2/g'
}

# ==================================================================
# 从 transcript jsonl 反向扫 last_assistant_text + last_user_text
# 防 retrospective T4 时序坑：用 message id + timestamp 综合判定
# ==================================================================
extract_messages() {
  local transcript="${JUDGE_TRANSCRIPT_PATH}"
  python3 - "$transcript" <<'PY'
import sys, json
path = sys.argv[1]
last_assistant_text = ""
last_user_text = ""

def extract_text_from_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for blk in content:
            if isinstance(blk, dict) and blk.get('type') == 'text':
                return blk.get('text', '')
            if isinstance(blk, str):
                return blk
    return ""

try:
    with open(path) as f:
        lines = f.readlines()
    # 反向扫，定位最后一条 assistant 文本 + 最近一条 user 文本
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        # CC transcript 格式兼容：
        # 形式1: {"type":"assistant","message":{"role":"assistant","content":[...]}}
        # 形式2: {"role":"assistant","content":[...]}
        msg_type = obj.get('type', '')
        role = ""
        content = None
        if msg_type in ('user', 'assistant'):
            role = msg_type
            content = obj.get('message', {}).get('content', obj.get('content'))
        elif obj.get('role') in ('user', 'assistant'):
            role = obj['role']
            content = obj.get('content')
        if not role:
            continue
        text = extract_text_from_content(content)
        if not text:
            continue
        if role == 'assistant' and not last_assistant_text:
            last_assistant_text = text
        elif role == 'user' and not last_user_text:
            last_user_text = text
        if last_assistant_text and last_user_text:
            break
except Exception:
    pass

# 截断
last_assistant_text = (last_assistant_text or '')[-4000:]
last_user_text = (last_user_text or '')[-1500:]

print(json.dumps({
    "last_assistant_text": last_assistant_text,
    "last_user_text": last_user_text,
}, ensure_ascii=False))
PY
}

# ==================================================================
# 读 state 字段（iter / consecutive_nudge_count / start_head）
# ==================================================================
read_state_field() {
  local key="$1"
  if [ ! -f "$JUDGE_STATE_FILE" ]; then
    echo ""
    return
  fi
  grep -E "^${key}:" "$JUDGE_STATE_FILE" 2>/dev/null | head -1 | sed -E "s/^${key}:[[:space:]]*\"?([^\"]*)\"?[[:space:]]*$/\1/"
}

# ==================================================================
# git diff stat（前 30 行）
# ==================================================================
get_diff_stat() {
  local start_head
  start_head="$(read_state_field 'start_head')"
  if [ -z "$start_head" ] || [ ! -d "$JUDGE_PROJECT_ROOT" ]; then
    echo ""
    return
  fi
  git -C "$JUDGE_PROJECT_ROOT" diff --stat "${start_head}..HEAD" 2>/dev/null | head -30
}

# ==================================================================
# 调 Anthropic API
# ==================================================================
call_anthropic_api() {
  local model="$1" sys_prompt="$2" user_msg="$3" timeout="$4" cred_path="$5"
  local base_url="${ANTHROPIC_BASE_URL:-https://api.anthropic.com}"
  local url="${base_url%/}/v1/messages"

  # 构造请求体
  local payload
  payload="$(python3 - "$model" "$sys_prompt" "$user_msg" <<'PY'
import json, sys
print(json.dumps({
    "model": sys.argv[1],
    "max_tokens": 256,
    "system": sys.argv[2],
    "messages": [{"role": "user", "content": sys.argv[3]}],
}, ensure_ascii=False))
PY
)"

  # 构造 headers + 调 curl
  local resp http_code curl_extra=()
  if [ "$cred_path" = "oauth" ]; then
    local token
    token="$(read_oauth_token)"
    if [ -z "$token" ]; then
      echo "ERR_NO_TOKEN"
      return 1
    fi
    curl_extra+=("-H" "Authorization: Bearer $token")
    curl_extra+=("-H" "anthropic-beta: oauth-2025-04-20")
  else
    curl_extra+=("-H" "x-api-key: ${ANTHROPIC_API_KEY:-}")
  fi
  curl_extra+=("-H" "anthropic-version: 2023-06-01")
  curl_extra+=("-H" "content-type: application/json")

  # curl 输出格式：响应体\n---HTTP_CODE---\n<code>
  resp="$(curl --max-time "$timeout" -sS -X POST "$url" \
    -w '\n---HTTP_CODE---\n%{http_code}' \
    "${curl_extra[@]}" \
    -d "$payload" 2>/dev/null)"
  local exit_code=$?

  if [ $exit_code -ne 0 ]; then
    if [ $exit_code -eq 28 ]; then
      echo "ERR_TIMEOUT"
    else
      echo "ERR_CURL_$exit_code"
    fi
    return 1
  fi

  http_code="$(echo "$resp" | awk '/^---HTTP_CODE---$/{getline; print; exit}')"
  local body
  body="$(echo "$resp" | sed '/^---HTTP_CODE---$/,$d')"

  if [ "$http_code" != "200" ]; then
    echo "ERR_HTTP_${http_code}"
    return 1
  fi

  echo "$body"
}

# ==================================================================
# 解析 API 响应 → 提取 judge JSON
# ==================================================================
parse_api_response() {
  local body="$1"
  printf '%s' "$body" | python3 - <<'PY'
import json, sys, re
body = sys.stdin.read()
try:
    resp = json.loads(body)
    blocks = resp.get('content', [])
    text = ''
    for b in blocks:
        if b.get('type') == 'text':
            text = b.get('text', '')
            break
    if not text:
        print("PARSE_NO_TEXT")
        sys.exit(0)
    text = text.strip()
    if text.startswith('```'):
        m = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, flags=re.S)
        if m:
            text = m.group(1).strip()
    obj = json.loads(text)
    action = obj.get('action', '')
    if action not in ('stop_done', 'continue_nudge', 'retry_transient', 'continue_strict'):
        print("PARSE_BAD_ACTION")
        sys.exit(0)
    confidence = float(obj.get('confidence', 0))
    reason = str(obj.get('reason', ''))[:200]
    print(json.dumps({
        "action": action,
        "confidence": confidence,
        "reason": reason,
    }, ensure_ascii=False))
except Exception as e:
    print(f"PARSE_ERR_{type(e).__name__}")
PY
}

# ==================================================================
# Telemetry 落盘
# ==================================================================
write_telemetry() {
  local action="$1" conf="$2" reason="$3" downgraded="$4" dgr="$5" model="$6" cred="$7"
  local diff_stat="$8" last_a="$9" last_u="${10}"
  local trace_file="${JUDGE_PROJECT_ROOT}/.claude/builder-loop/judge-trace.jsonl"
  mkdir -p "$(dirname "$trace_file")" 2>/dev/null || true

  local end_ts_ms=$(($(date +%s%N 2>/dev/null || echo "$(date +%s)000000000")/1000000))
  local elapsed=$((end_ts_ms - START_TS_MS))

  local slug iter cnc
  slug="$(basename "$JUDGE_STATE_FILE" .yml 2>/dev/null || echo "")"
  iter="$(read_state_field 'iter')"
  cnc="$(read_state_field 'consecutive_nudge_count')"
  cnc="${cnc:-0}"

  ACTION="$action" CONF="$conf" REASON="$reason" DOWNGRADED="$downgraded" \
  DGR="$dgr" MODEL="$model" CRED="$cred" DIFF_STAT="$diff_stat" \
  LAST_A="$last_a" LAST_U="$last_u" SLUG="$slug" ITER="$iter" CNC="$cnc" \
  STATUS="$JUDGE_PASS_CMD_STATUS" STAGE="$JUDGE_PASS_CMD_STAGE" \
  ELAPSED="$elapsed" TRACE="$trace_file" \
  python3 - <<'PY'
import os, json, datetime
trace = os.environ['TRACE']
line = {
    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "slug": os.environ.get('SLUG', ''),
    "iter": int(os.environ.get('ITER') or 0),
    "input": {
        "pass_cmd_status": os.environ.get('STATUS', ''),
        "pass_cmd_stage": os.environ.get('STAGE', ''),
        "diff_stat_summary": (os.environ.get('DIFF_STAT', '') or '').splitlines()[-1:][0] if os.environ.get('DIFF_STAT') else '',
        "last_assistant_snippet": (os.environ.get('LAST_A', '') or '')[:200],
        "last_user_snippet": (os.environ.get('LAST_U', '') or '')[:100],
    },
    "judge": {
        "action": os.environ.get('ACTION', ''),
        "confidence": float(os.environ.get('CONF') or 0),
        "reason": os.environ.get('REASON', ''),
        "model_used": os.environ.get('MODEL', ''),
        "credential_path": os.environ.get('CRED', ''),
        "elapsed_ms": int(os.environ.get('ELAPSED') or 0),
    },
    "downgraded": os.environ.get('DOWNGRADED') == 'true',
    "downgrade_reason": os.environ.get('DGR', ''),
    "consecutive_nudge_count_after": int(os.environ.get('CNC') or 0),
    "outcome": None,
}
try:
    with open(trace, 'a') as f:
        f.write(json.dumps(line, ensure_ascii=False) + '\n')
except Exception:
    pass
PY
}

# ==================================================================
# Self-check 子命令
# ==================================================================
run_self_check() {
  echo "[judge self-check]"
  local cred
  cred="$(detect_credentials)"
  echo "  credentials:    $cred"
  if [ "$cred" = "env" ]; then
    echo "    ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:0:10}... (len=${#ANTHROPIC_API_KEY})"
    echo "    ANTHROPIC_BASE_URL: ${ANTHROPIC_BASE_URL:-<unset, default https://api.anthropic.com>}"
  elif [ "$cred" = "oauth" ]; then
    echo "    ~/.claude.json: oauthAccount.accessToken present"
  fi
  local env_model="${ANTHROPIC_DEFAULT_HAIKU_MODEL:-<unset>}"
  echo "  env haiku model: $env_model"
  local resolved
  resolved="$(resolve_model "")"
  echo "  resolved model:  $resolved"
  if [ "$cred" = "none" ]; then
    cat >&2 <<'HINT'
ERROR: missing credentials (no env, no oauth)

提示：
  - Copilot CC 用户：检查 ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL（应在 ~/.claude/settings.json env 段）
  - 正版 Max CC 用户：CC 自己的 OAuth token 不在 ~/.claude.json 公开字段中，judge agent 无法直接复用。
    workaround：从 https://console.anthropic.com 申请独立 API key（不影响 Max 订阅），
                export ANTHROPIC_API_KEY=sk-ant-... 即可启用 judge。
    或保持现状 — judge 会自动降级回 PASS_CMD 二值判据，行为等价 V1.8。
HINT
    return 1
  fi
  echo "OK"
  return 0
}

# ==================================================================
# Main
# ==================================================================
if [ "$JUDGE_SELF_CHECK" = "true" ]; then
  run_self_check
  exit $?
fi

# 必填参数校验
if [ -z "$JUDGE_STATE_FILE" ] || [ -z "$JUDGE_PROJECT_ROOT" ] || [ -z "$JUDGE_TRANSCRIPT_PATH" ] || [ -z "$JUDGE_PASS_CMD_STATUS" ]; then
  output_downgrade "missing_args"
  exit 0
fi

# 读 judge 配置
JUDGE_CONFIG="$(read_judge_config)"
JUDGE_ENABLED="$(echo "$JUDGE_CONFIG" | grep '^enabled=' | head -1 | cut -d= -f2-)"
JUDGE_MODEL_YML="$(echo "$JUDGE_CONFIG" | grep '^model=' | head -1 | cut -d= -f2-)"
JUDGE_CONF_THR="$(echo "$JUDGE_CONFIG" | grep '^confidence_threshold=' | head -1 | cut -d= -f2-)"
JUDGE_TIMEOUT="$(echo "$JUDGE_CONFIG" | grep '^api_timeout_sec=' | head -1 | cut -d= -f2-)"
JUDGE_PROMPT_PATH_REL="$(echo "$JUDGE_CONFIG" | grep '^system_prompt_path=' | head -1 | cut -d= -f2-)"
JUDGE_CONF_THR="${JUDGE_CONF_THR:-0.5}"
JUDGE_TIMEOUT="${JUDGE_TIMEOUT:-8}"

if [ "$JUDGE_ENABLED" = "false" ]; then
  RESOLVED_MODEL="$(resolve_model "$JUDGE_MODEL_YML")"
  CRED="$(detect_credentials)"
  output_downgrade "disabled" "$RESOLVED_MODEL" "$CRED"
  write_telemetry "stop_done" "0" "disabled" "true" "disabled" "$RESOLVED_MODEL" "$CRED" "" "" ""
  exit 0
fi

# 凭证检测
CRED="$(detect_credentials)"
if [ "$CRED" = "none" ]; then
  RESOLVED_MODEL="$(resolve_model "$JUDGE_MODEL_YML")"
  output_downgrade "missing_credentials" "$RESOLVED_MODEL" "none"
  write_telemetry "stop_done" "0" "missing_credentials" "true" "missing_credentials" "$RESOLVED_MODEL" "none" "" "" ""
  exit 0
fi

# 模型 resolve
RESOLVED_MODEL="$(resolve_model "$JUDGE_MODEL_YML")"

# 读 system prompt
SYSTEM_PROMPT_FILE=""
if [ -n "$JUDGE_PROMPT_PATH_REL" ]; then
  if [[ "$JUDGE_PROMPT_PATH_REL" = /* ]]; then
    SYSTEM_PROMPT_FILE="$JUDGE_PROMPT_PATH_REL"
  else
    SYSTEM_PROMPT_FILE="${JUDGE_PROJECT_ROOT}/${JUDGE_PROMPT_PATH_REL}"
  fi
fi
[ -z "$SYSTEM_PROMPT_FILE" ] || [ ! -f "$SYSTEM_PROMPT_FILE" ] && SYSTEM_PROMPT_FILE="${SKILL_ROOT}/prompts/judge-system.md"
if [ ! -f "$SYSTEM_PROMPT_FILE" ]; then
  output_downgrade "missing_prompt" "$RESOLVED_MODEL" "$CRED"
  write_telemetry "stop_done" "0" "missing_prompt" "true" "missing_prompt" "$RESOLVED_MODEL" "$CRED" "" "" ""
  exit 0
fi
SYSTEM_PROMPT="$(cat "$SYSTEM_PROMPT_FILE")"

# 抽取消息
MESSAGES_JSON="$(extract_messages)"
LAST_A="$(echo "$MESSAGES_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin).get('last_assistant_text',''))" 2>/dev/null || echo "")"
LAST_U="$(echo "$MESSAGES_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin).get('last_user_text',''))" 2>/dev/null || echo "")"
DIFF_STAT="$(get_diff_stat)"

ITER="$(read_state_field 'iter')"
ITER="${ITER:-0}"
MAX_ITER="$(read_state_field 'max_iter')"
MAX_ITER="${MAX_ITER:-5}"
CNC="$(read_state_field 'consecutive_nudge_count')"
CNC="${CNC:-0}"

# 构造 user message（喂给 LLM 的输入）
USER_MSG="$(LAST_A="$LAST_A" LAST_U="$LAST_U" DIFF_STAT="$DIFF_STAT" \
  STATUS="$JUDGE_PASS_CMD_STATUS" STAGE="$JUDGE_PASS_CMD_STAGE" \
  ITER="$ITER" MAX_ITER="$MAX_ITER" CNC="$CNC" \
  python3 - <<'PY'
import os
out = []
out.append(f"pass_cmd_status: {os.environ.get('STATUS','')}")
if os.environ.get('STAGE'):
    out.append(f"pass_cmd_stage: {os.environ.get('STAGE')}")
out.append(f"iter: {os.environ.get('ITER','0')}/{os.environ.get('MAX_ITER','5')}")
out.append(f"consecutive_nudge_count: {os.environ.get('CNC','0')}")
out.append('')
out.append('=== diff_stat ===')
out.append(os.environ.get('DIFF_STAT','') or '(no diff)')
out.append('')
out.append('=== last_user_text ===')
out.append(os.environ.get('LAST_U','') or '(empty)')
out.append('')
out.append('=== last_assistant_text ===')
out.append(os.environ.get('LAST_A','') or '(empty)')
out.append('')
out.append('请按 system prompt 给出严格 JSON 输出。')
print('\n'.join(out))
PY
)"

# 调 API
API_RESP="$(call_anthropic_api "$RESOLVED_MODEL" "$SYSTEM_PROMPT" "$USER_MSG" "$JUDGE_TIMEOUT" "$CRED")"
API_STATUS=$?

if [ $API_STATUS -ne 0 ] || [[ "$API_RESP" == ERR_* ]]; then
  case "$API_RESP" in
    ERR_TIMEOUT)        DGR="timeout" ;;
    ERR_HTTP_*)         DGR="http_${API_RESP#ERR_HTTP_}" ;;
    ERR_NO_TOKEN)       DGR="no_oauth_token" ;;
    *)                  DGR="api_error" ;;
  esac
  output_downgrade "$DGR" "$RESOLVED_MODEL" "$CRED"
  write_telemetry "stop_done" "0" "$DGR" "true" "$DGR" "$RESOLVED_MODEL" "$CRED" "$DIFF_STAT" "$LAST_A" "$LAST_U"
  exit 0
fi

# 解析响应
PARSED="$(parse_api_response "$API_RESP")"
if [[ "$PARSED" == PARSE_* ]]; then
  output_downgrade "parse_error" "$RESOLVED_MODEL" "$CRED"
  write_telemetry "stop_done" "0" "parse_error:$PARSED" "true" "parse_error" "$RESOLVED_MODEL" "$CRED" "$DIFF_STAT" "$LAST_A" "$LAST_U"
  exit 0
fi

JUDGE_ACTION="$(echo "$PARSED" | python3 -c "import json,sys; print(json.load(sys.stdin)['action'])" 2>/dev/null || echo "")"
JUDGE_CONF="$(echo "$PARSED" | python3 -c "import json,sys; print(json.load(sys.stdin)['confidence'])" 2>/dev/null || echo "0")"
JUDGE_REASON="$(echo "$PARSED" | python3 -c "import json,sys; print(json.load(sys.stdin).get('reason',''))" 2>/dev/null || echo "")"

# confidence 阈值检查
LOW_CONF="$(python3 -c "import sys; print('1' if float('$JUDGE_CONF') < float('$JUDGE_CONF_THR') else '0')" 2>/dev/null || echo "1")"
if [ "$LOW_CONF" = "1" ]; then
  output_downgrade "low_confidence" "$RESOLVED_MODEL" "$CRED"
  write_telemetry "$JUDGE_ACTION" "$JUDGE_CONF" "$JUDGE_REASON" "true" "low_confidence" "$RESOLVED_MODEL" "$CRED" "$DIFF_STAT" "$LAST_A" "$LAST_U"
  exit 0
fi

# 正常输出
output_result_json "$JUDGE_ACTION" "$JUDGE_CONF" "$JUDGE_REASON" "false" "" "$RESOLVED_MODEL" "$CRED"
write_telemetry "$JUDGE_ACTION" "$JUDGE_CONF" "$JUDGE_REASON" "false" "" "$RESOLVED_MODEL" "$CRED" "$DIFF_STAT" "$LAST_A" "$LAST_U"
exit 0
