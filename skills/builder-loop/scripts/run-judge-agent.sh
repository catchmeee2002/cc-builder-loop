#!/usr/bin/env bash
# run-judge-agent.sh — Judge agent (V1.9+)
#
# 职责：在 stop hook PASS_CMD 判据之上叠加一道 LLM 语义判定，识别 PASS_CMD 二值判据看不见的盲区
# （假完成 / 求助 / 偷懒 / 网络中断）。
#
# 调用约定：
#   bash run-judge-agent.sh \
#     --state-file <path>          # state.yml 路径（必填）
#     --project-root <path>        # 干活的地方（V2.0 起 = worktree 路径 / bare loop = 主仓）
#                                  #   loop.yml 从此读、git diff 在此跑——这才能让 worktree 内
#                                  #   builder 改的 loop.yml.judge 配置 / 改的代码立即可见
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
# V2.1: env file 加载（仅在 ANTHROPIC_API_KEY 主 env 缺失时 source）
# 用途：让正版 Max CC 主会话保持 OAuth 干净，judge agent 独立从配置文件读 copilot-proxy 凭证
# 优先级：主 env > judge-env.sh > oauth > none
# 安全：source 失败不阻断（仅 stderr 警告 + 走后续 oauth/none 检测路径）
# ==================================================================
maybe_source_env_file() {
  local file="$1"
  if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -f "$file" ]; then
    # set -a 让 source 内的 var= 都隐式 export，省得用户每行写 export
    set -a
    # shellcheck disable=SC1090
    if ! source "$file" 2>/dev/null; then
      echo "[run-judge-agent] WARN: failed to source env file: $file" >&2
    fi
    set +a
  fi
}

JUDGE_ENV_FILE_DEFAULT="$HOME/.claude/skills/builder-loop/judge-env.sh"
# Phase 0: 全局默认路径（loop.yml 加载之前就先试一次）
maybe_source_env_file "$JUDGE_ENV_FILE_DEFAULT"

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
    # V1.9 兼容字段（旧）
    'model': '',
    # V2.1 新增
    'primary_model': '',
    'fallback_model': '',
    'fallback_after_failures': '2',
    'credentials_file': '',
    # 不变字段
    'confidence_threshold': '0.5',
    'max_consecutive_nudges': '2',
    'api_timeout_sec': '15',           # V2.1: 默认 8 → 15（sonnet 单次 5.8s 留余量）
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
# 输出 KEY=VALUE 一行
for k, v in fields.items():
    print(f'{k}={v}')
PY
}

# ==================================================================
# 模型选择 + 规范化（dot → dash）
# V2.1 顺序：from_yml（caller 已合并 primary_model > model 优先级）> $ANTHROPIC_DEFAULT_HAIKU_MODEL > sonnet 默认
# 注意默认从 V1.9 的 claude-haiku-4-5 改为 V2.1 的 claude-sonnet-4-6（搭配 fallback 链兜底）
# ==================================================================
resolve_model() {
  local from_yml="$1" from_env="${ANTHROPIC_DEFAULT_HAIKU_MODEL:-}"
  local raw=""
  if [ -n "$from_yml" ]; then
    raw="$from_yml"
  elif [ -n "$from_env" ]; then
    raw="$from_env"
  else
    raw="claude-sonnet-4-6"
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
# 读 state 字段（iter / consecutive_nudge_count / start_head / V2.1 judge_active_model 等）
# ==================================================================
read_state_field() {
  local key="$1"
  if [ ! -f "$JUDGE_STATE_FILE" ]; then
    echo ""
    return
  fi
  # || true 兜底：字段不存在时 grep exit 1 + pipefail + set -e 让脚本静默退出
  grep -E "^${key}:" "$JUDGE_STATE_FILE" 2>/dev/null | head -1 | sed -E "s/^${key}:[[:space:]]*\"?([^\"]*)\"?[[:space:]]*$/\1/" || true
}

# ==================================================================
# V2.1: state 字段 upsert（缺字段则追加，存在则替换）
# 用于把 judge_active_model / judge_consecutive_failures 写回 state
# 不写整个 state 是因为 stop hook 在 judge 调用前后还有自己的 state 读写流程
# ==================================================================
upsert_state_field() {
  local key="$1" value="$2"
  [ -f "$JUDGE_STATE_FILE" ] || return 0
  STATE_FILE="$JUDGE_STATE_FILE" KEY="$key" VALUE="$value" python3 - <<'PY'
import os, re
sf = os.environ['STATE_FILE']
key = os.environ['KEY']
value = os.environ['VALUE']
try:
    text = open(sf).read()
except Exception:
    raise SystemExit
pat = re.compile(rf'^{re.escape(key)}:.*$', re.M)
# value 是字符串字面量，含引号需要 python 自己加（仅模型名加引号、纯数字不加）
quoted = False
try:
    int(value)
except ValueError:
    quoted = True
formatted = f'{key}: "{value}"' if quoted else f'{key}: {value}'
if pat.search(text):
    text = pat.sub(formatted, text)
else:
    if not text.endswith('\n'):
        text += '\n'
    text += formatted + '\n'
try:
    open(sf, 'w').write(text)
except Exception:
    pass
PY
}

# ==================================================================
# V2.1: 失败分类 — 决定该次 API 调用是否计入"sonnet 失败计数"
# 计数：timeout / 5xx / parse_error
# 不计数：401 / 403（凭证问题） / 429（rate_limit） / 其他
# ==================================================================
classify_failure() {
  local err="$1"
  case "$err" in
    ERR_TIMEOUT) echo "1"; return ;;
    ERR_HTTP_5*) echo "1"; return ;;
    PARSE_*)     echo "1"; return ;;   # parse_api_response 返回的 PARSE_NO_TEXT/PARSE_BAD_ACTION/PARSE_ERR_*
    *)           echo "0" ;;
  esac
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
  # 注：早期写法 `printf "$body" | python3 - <<'PY'` 有 bug——bash 的 here-doc
  # `<<'PY'` 会**覆盖** pipe 提供的 stdin，python 实际读到的是 here-doc 内的脚本文本，
  # 不是 body。改用 BODY env var 传递。
  BODY="$body" python3 - <<'PY'
import json, os, re, sys
body = os.environ.get('BODY', '')
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
    # 顶层 action 是 judge.action 的快捷别名（便于 jq / python 简单查询）；
    # 完整结构在 judge 嵌套字段里
    "action": os.environ.get('ACTION', ''),
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

# 读 judge 配置（V2.1：含 primary_model / fallback_model / fallback_after_failures / credentials_file）
JUDGE_CONFIG="$(read_judge_config)"
JUDGE_ENABLED="$(echo "$JUDGE_CONFIG" | grep '^enabled=' | head -1 | cut -d= -f2-)"
JUDGE_MODEL_YML_LEGACY="$(echo "$JUDGE_CONFIG" | grep '^model=' | head -1 | cut -d= -f2-)"        # V1.9 兼容
JUDGE_PRIMARY_YML="$(echo "$JUDGE_CONFIG" | grep '^primary_model=' | head -1 | cut -d= -f2-)"     # V2.1
JUDGE_FALLBACK_YML="$(echo "$JUDGE_CONFIG" | grep '^fallback_model=' | head -1 | cut -d= -f2-)"   # V2.1
JUDGE_FALLBACK_THRESHOLD="$(echo "$JUDGE_CONFIG" | grep '^fallback_after_failures=' | head -1 | cut -d= -f2-)"
JUDGE_CRED_FILE_YML="$(echo "$JUDGE_CONFIG" | grep '^credentials_file=' | head -1 | cut -d= -f2-)"  # V2.1
JUDGE_CONF_THR="$(echo "$JUDGE_CONFIG" | grep '^confidence_threshold=' | head -1 | cut -d= -f2-)"
JUDGE_TIMEOUT="$(echo "$JUDGE_CONFIG" | grep '^api_timeout_sec=' | head -1 | cut -d= -f2-)"
JUDGE_PROMPT_PATH_REL="$(echo "$JUDGE_CONFIG" | grep '^system_prompt_path=' | head -1 | cut -d= -f2-)"
JUDGE_CONF_THR="${JUDGE_CONF_THR:-0.5}"
JUDGE_TIMEOUT="${JUDGE_TIMEOUT:-15}"                       # V2.1: 默认 8 → 15
JUDGE_FALLBACK_THRESHOLD="${JUDGE_FALLBACK_THRESHOLD:-2}"

# V2.1 Phase 1: env file 二次加载 — loop.yml.judge.credentials_file 指定了别处 + env 仍缺时再 source
if [ -n "$JUDGE_CRED_FILE_YML" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  # eval echo 展开 ~/$VAR；用户值是从自己的 loop.yml 读的，可信
  EXPANDED_CRED_FILE="$(eval echo "$JUDGE_CRED_FILE_YML" 2>/dev/null || echo "")"
  if [ -n "$EXPANDED_CRED_FILE" ] && [ "$EXPANDED_CRED_FILE" != "$JUDGE_ENV_FILE_DEFAULT" ]; then
    maybe_source_env_file "$EXPANDED_CRED_FILE"
  fi
fi

# 模型 resolve：primary > V1.9 兼容 model > $ANTHROPIC_DEFAULT_HAIKU_MODEL > 默认 sonnet
PRIMARY_MODEL_RAW="${JUDGE_PRIMARY_YML:-$JUDGE_MODEL_YML_LEGACY}"
PRIMARY_MODEL="$(resolve_model "$PRIMARY_MODEL_RAW")"
if [ -n "$JUDGE_FALLBACK_YML" ]; then
  FALLBACK_MODEL="$(echo "$JUDGE_FALLBACK_YML" | sed -E 's/([0-9])\.([0-9])/\1-\2/g')"
else
  FALLBACK_MODEL=""
fi

if [ "$JUDGE_ENABLED" = "false" ]; then
  CRED="$(detect_credentials)"
  output_downgrade "disabled" "$PRIMARY_MODEL" "$CRED"
  write_telemetry "stop_done" "0" "disabled" "true" "disabled" "$PRIMARY_MODEL" "$CRED" "" "" ""
  exit 0
fi

# 凭证检测
CRED="$(detect_credentials)"
if [ "$CRED" = "none" ]; then
  output_downgrade "missing_credentials" "$PRIMARY_MODEL" "none"
  write_telemetry "stop_done" "0" "missing_credentials" "true" "missing_credentials" "$PRIMARY_MODEL" "none" "" "" ""
  exit 0
fi

# V2.1: 读 state 当前活跃模型 + 失败计数（缺则取默认值）
JUDGE_ACTIVE_MODEL_FROM_STATE="$(read_state_field 'judge_active_model')"
JUDGE_ACTIVE_MODEL="${JUDGE_ACTIVE_MODEL_FROM_STATE:-$PRIMARY_MODEL}"
JUDGE_FAILURES="$(read_state_field 'judge_consecutive_failures')"
JUDGE_FAILURES="${JUDGE_FAILURES:-0}"
# RESOLVED_MODEL 沿用 V1.9 命名表达"本轮调用的模型"，初值 = active model
RESOLVED_MODEL="$JUDGE_ACTIVE_MODEL"

# 读 system prompt
SYSTEM_PROMPT_FILE=""
if [ -n "$JUDGE_PROMPT_PATH_REL" ]; then
  if [[ "$JUDGE_PROMPT_PATH_REL" = /* ]]; then
    SYSTEM_PROMPT_FILE="$JUDGE_PROMPT_PATH_REL"
  else
    SYSTEM_PROMPT_FILE="${JUDGE_PROJECT_ROOT}/${JUDGE_PROMPT_PATH_REL}"
  fi
fi
if [ -z "$SYSTEM_PROMPT_FILE" ] || [ ! -f "$SYSTEM_PROMPT_FILE" ]; then
  SYSTEM_PROMPT_FILE="${SKILL_ROOT}/prompts/judge-system.md"
fi
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

# V2.1: 把 API 调用 + 响应解析抽到函数里，便于 fallback retry 复用
# 输出（设置全局 var）：
#   CALL_RESULT = parse 后的 PARSED JSON（成功）
#   CALL_ERR    = ERR_* / PARSE_* 错误标识（失败）
do_call_and_parse() {
  local model_to_call="$1"
  local resp status parsed
  resp="$(call_anthropic_api "$model_to_call" "$SYSTEM_PROMPT" "$USER_MSG" "$JUDGE_TIMEOUT" "$CRED")"
  status=$?
  if [ $status -ne 0 ] || [[ "$resp" == ERR_* ]]; then
    CALL_ERR="$resp"
    return 1
  fi
  parsed="$(parse_api_response "$resp")"
  if [[ "$parsed" == PARSE_* ]]; then
    CALL_ERR="$parsed"
    return 1
  fi
  CALL_RESULT="$parsed"
  return 0
}

# 把 ERR_* / PARSE_* 转成 downgrade_reason 字符串
err_to_dgr() {
  local err="$1"
  case "$err" in
    ERR_TIMEOUT)   echo "timeout" ;;
    ERR_HTTP_*)    echo "http_${err#ERR_HTTP_}" ;;
    ERR_NO_TOKEN)  echo "no_oauth_token" ;;
    PARSE_*)       echo "parse_error" ;;
    *)             echo "api_error" ;;
  esac
}

# V2.1 主调用 + 失败处理状态机
PARSED=""
FALLBACK_TRIGGERED="false"
if do_call_and_parse "$RESOLVED_MODEL"; then
  # 成功 → 重置计数 + 保存当前 active（保 V2.0 schema 字段写回兼容）
  upsert_state_field "judge_consecutive_failures" "0"
  upsert_state_field "judge_active_model" "$RESOLVED_MODEL"
  PARSED="$CALL_RESULT"
else
  CLASSIFIED="$(classify_failure "$CALL_ERR")"
  if [ "$CLASSIFIED" = "1" ] && [ "$RESOLVED_MODEL" = "$PRIMARY_MODEL" ] && [ -n "$FALLBACK_MODEL" ]; then
    NEW_FAILURES=$((JUDGE_FAILURES + 1))
    if [ "$NEW_FAILURES" -ge "$JUDGE_FALLBACK_THRESHOLD" ]; then
      # 达阈值 → 切 fallback + 立即 retry 一次
      RESOLVED_MODEL="$FALLBACK_MODEL"
      upsert_state_field "judge_active_model" "$FALLBACK_MODEL"
      upsert_state_field "judge_consecutive_failures" "0"   # 切 fallback 即重置计数（避免再次降级）
      FALLBACK_TRIGGERED="true"
      echo "[run-judge-agent] sonnet 连续失败 ${NEW_FAILURES} 次（阈值 ${JUDGE_FALLBACK_THRESHOLD}），切 fallback 模型 ${FALLBACK_MODEL} 重试" >&2
      if do_call_and_parse "$FALLBACK_MODEL"; then
        PARSED="$CALL_RESULT"
      else
        # fallback 也失败 → 不再切第三档，直接 downgrade
        DGR="$(err_to_dgr "$CALL_ERR")"
        output_downgrade "fallback_also_failed:$DGR" "$FALLBACK_MODEL" "$CRED"
        write_telemetry "stop_done" "0" "fallback_also_failed:$DGR" "true" "$DGR" "$FALLBACK_MODEL" "$CRED" "$DIFF_STAT" "$LAST_A" "$LAST_U"
        exit 0
      fi
    else
      # 未达阈值 → 仅累计 failures，本轮不切；走 downgrade 让 stop hook 退回原 PASS 路径
      upsert_state_field "judge_consecutive_failures" "$NEW_FAILURES"
      DGR="$(err_to_dgr "$CALL_ERR")"
      output_downgrade "$DGR" "$RESOLVED_MODEL" "$CRED"
      write_telemetry "stop_done" "0" "${DGR}:failures=${NEW_FAILURES}/${JUDGE_FALLBACK_THRESHOLD}" "true" "$DGR" "$RESOLVED_MODEL" "$CRED" "$DIFF_STAT" "$LAST_A" "$LAST_U"
      exit 0
    fi
  else
    # 1) classified=0（401/403/429/no_token 等凭证类失败）→ 不计数
    # 2) active_model 已经是 fallback → 不再切第三档
    # 3) classified=1 但 fallback_model 为空 → 用户禁用了降级链
    # 三种情况都直接 downgrade
    DGR="$(err_to_dgr "$CALL_ERR")"
    output_downgrade "$DGR" "$RESOLVED_MODEL" "$CRED"
    write_telemetry "stop_done" "0" "$DGR" "true" "$DGR" "$RESOLVED_MODEL" "$CRED" "$DIFF_STAT" "$LAST_A" "$LAST_U"
    exit 0
  fi
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
