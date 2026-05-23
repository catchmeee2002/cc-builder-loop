#!/usr/bin/env bash
# test-judge-model-fallback.sh — V2.1 E2E：sonnet → haiku 降级链
#
# 覆盖 12 个 case（B1-B12），用 python3 mock Anthropic API server，按 mode 控制返回 200 / 5xx / 401 / 429 / parse_err / timeout。
#
# 关键场景：
#   B1  连续 sonnet 成功 → state.failures=0, active=sonnet
#   B2  sonnet 1 次 5xx → failures=1（未达阈值，本轮 downgrade）
#   B3  sonnet 2 次 5xx → 切 haiku, failures=0, fallback retry → 输出 model_used=haiku，downgrade_reason 前缀 fallback_also_failed:
#   B4  sonnet 切 haiku 后 haiku 也 5xx → downgrade fallback_also_failed
#   B5  sonnet 1 失败 + 1 成功 → failures=0（成功后重置）
#   B6  401 不计数（凭证类） → failures 不变
#   B7  429 不计数（rate_limit） → failures 不变
#   B8  parse_error 计数（同 timeout/5xx）
#   B9  fallback_model 留空 → 不切，失败直接 downgrade
#   B10 缺 V2.1 state 字段（旧 state） → 默认值（active=primary, failures=0）
#   B11 worktree 内改 primary_model 立即生效（指向 mock 不同 endpoint 模拟）
#   B12 active_model=haiku 时成功一次 → failures 重置 0，active 仍 haiku（haiku 路径成功语义对偶 B5）
#
# 用法：bash test-judge-model-fallback.sh
# 退出码：0=全部通过 / 1=有失败

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "V2.1 sonnet → haiku 降级链"

JUDGE_SCRIPT="${HARNESS_REPO_ROOT}/skills/builder-loop/scripts/run-judge-agent.sh"

MOCK_PORT=18998
MOCK_PID=""

# ---- Mock server cleanup (supplement harness cleanup) ----
_fallback_cleanup() {
  if [ -n "$MOCK_PID" ] && kill -0 "$MOCK_PID" 2>/dev/null; then
    kill "$MOCK_PID" 2>/dev/null || true
    wait "$MOCK_PID" 2>/dev/null || true
  fi
  _harness_cleanup
}
trap _fallback_cleanup EXIT

TMP="$(mktemp -d)"
_HARNESS_TMPDIRS+=("$TMP")

assert "judge script 存在" "[ -f '$JUDGE_SCRIPT' ]"

# ---- 自定义断言：JSON 字段 (pipe stdin) ----
assert_json() {
  local desc="$1" json="$2" field="$3" expected="$4"
  local actual
  actual="$(echo "$json" | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('${field}','__MISSING__'))" 2>/dev/null || echo "__PARSE__")"
  if [ "$actual" = "$expected" ]; then
    assert "$desc" "true"
  else
    assert "$desc (expected $field=$expected, got $actual)" "false"
  fi
}

# ---- Mock server (dynamic mode via shared file) ----
start_mock_server() {
  if [ -n "$MOCK_PID" ] && kill -0 "$MOCK_PID" 2>/dev/null; then
    return 0
  fi
  local py="${TMP}/mock_anthropic.py"
  cat > "$py" <<PYEOF
#!/usr/bin/env python3
import json, os, time, sys
from http.server import HTTPServer, BaseHTTPRequestHandler

MODE_FILE = "${TMP}/mock_mode"
PORT = ${MOCK_PORT}

def read_mode():
    try:
        with open(MODE_FILE) as f:
            return f.read().strip()
    except:
        return "ok"

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except:
            payload = {}
        model = payload.get('model', '')
        with open("${TMP}/model_log.txt", "a") as f:
            f.write(model + "\\n")

        mode = read_mode()
        if mode == "5xx":
            self.send_response(502); self.end_headers(); return
        if mode == "401":
            self.send_response(401); self.end_headers(); return
        if mode == "429":
            self.send_response(429); self.end_headers(); return
        if mode == "timeout":
            time.sleep(15)
            return
        if mode == "parse_err":
            self.send_response(200); self.end_headers()
            self.wfile.write(b"not json"); return
        marker = "from-haiku" if "haiku" in model else "from-sonnet"
        inner = json.dumps({"action":"stop_done","confidence":0.9,"reason":marker})
        resp = {"id":"m","type":"message","role":"assistant","content":[{"type":"text","text":inner}],"model":model,"stop_reason":"end_turn","usage":{"input_tokens":10,"output_tokens":5}}
        out = json.dumps(resp).encode()
        self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(out))); self.end_headers()
        self.wfile.write(out)

HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
PYEOF
  python3 "$py" >/dev/null 2>&1 &
  MOCK_PID=$!
  for i in $(seq 1 30); do
    if python3 -c "import socket;s=socket.socket();s.settimeout(0.2);s.connect(('127.0.0.1',${MOCK_PORT}))" 2>/dev/null; then
      return 0
    fi
    sleep 0.1
  done
  echo "mock server launch failed" >&2
  exit 1
}

set_mode() { echo "$1" > "$TMP/mock_mode"; }
clear_model_log() { rm -f "$TMP/model_log.txt"; }

make_state() {
  local proj="$1" sf="$2"
  cat > "$sf" <<EOF
active: true
slug: test
iter: 1
max_iter: 5
project_root: "$proj"
main_repo_path: "$proj"
start_head: deadbeef
task_description: "fallback test"
EOF
}

make_loop_yml() {
  local proj="$1" pri="$2" fb="$3" thr="$4"
  mkdir -p "$proj/.claude"
  cat > "$proj/.claude/loop.yml" <<EOF
pass_cmd:
  - { stage: smoke, cmd: "true", timeout: 10 }
judge:
  enabled: true
  primary_model: "$pri"
  fallback_model: "$fb"
  fallback_after_failures: $thr
  api_timeout_sec: 5
  confidence_threshold: 0.5
EOF
}

call_judge() {
  local sf="$1" proj="$2"
  echo '{"role":"assistant","content":[{"type":"text","text":"ok"}]}' > "$proj/transcript.jsonl"
  ANTHROPIC_API_KEY=sk-666 \
    ANTHROPIC_BASE_URL="http://127.0.0.1:${MOCK_PORT}" \
    bash "$JUDGE_SCRIPT" \
      --state-file "$sf" \
      --project-root "$proj" \
      --transcript-path "$proj/transcript.jsonl" \
      --pass-cmd-status PASS 2>/dev/null
}

_read_state() {
  local v
  v="$(grep -E "^${2}:" "$1" 2>/dev/null | head -1 | sed -E "s/^${2}:[[:space:]]*\"?([^\"]*)\"?[[:space:]]*$/\1/" || true)"
  echo "${v:-${3:-}}"
}

start_mock_server

# ============================================================
# B1: sonnet 全成功
# ============================================================
section "B1: 连续 sonnet 成功"
PROJ_B1="$TMP/b1"; mkdir -p "$PROJ_B1"
make_loop_yml "$PROJ_B1" "claude-sonnet-4-6" "claude-haiku-4-5" 2
SF_B1="$PROJ_B1/state.yml"; make_state "$PROJ_B1" "$SF_B1"
clear_model_log; set_mode "ok"
J="$(call_judge "$SF_B1" "$PROJ_B1")"
assert_json "B1 model_used=sonnet" "$J" "model_used" "claude-sonnet-4-6"
V="$(_read_state "$SF_B1" judge_consecutive_failures)"; assert "B1 state failures=0" "[ '$V' = '0' ]"
V="$(_read_state "$SF_B1" judge_active_model)"; assert "B1 state active=sonnet" "[ '$V' = 'claude-sonnet-4-6' ]"

# ============================================================
# B2: sonnet 1 次 5xx → failures=1
# ============================================================
section "B2: sonnet 1 次 5xx"
PROJ_B2="$TMP/b2"; mkdir -p "$PROJ_B2"
make_loop_yml "$PROJ_B2" "claude-sonnet-4-6" "claude-haiku-4-5" 2
SF_B2="$PROJ_B2/state.yml"; make_state "$PROJ_B2" "$SF_B2"
clear_model_log; set_mode "5xx"
J="$(call_judge "$SF_B2" "$PROJ_B2")"
assert_json "B2 downgraded=true" "$J" "downgraded" "True"
V="$(_read_state "$SF_B2" judge_consecutive_failures)"; assert "B2 state failures=1" "[ '$V' = '1' ]"

# ============================================================
# B3: sonnet 2 次 5xx → 切 haiku + retry
# ============================================================
section "B3: sonnet 第 2 次 5xx → 切 haiku (fallback retry)"
clear_model_log; set_mode "5xx"
J="$(call_judge "$SF_B2" "$PROJ_B2")"
assert_json "B3 downgraded=true（fallback 也失败）" "$J" "downgraded" "True"
V="$(_read_state "$SF_B2" judge_active_model)"; assert "B3 state active=haiku" "[ '$V' = 'claude-haiku-4-5' ]"
V="$(_read_state "$SF_B2" judge_consecutive_failures)"; assert "B3 state failures=0（切后重置）" "[ '$V' = '0' ]"
assert "B3 model_log 含 sonnet" "grep -q 'claude-sonnet-4-6' '$TMP/model_log.txt'"
assert "B3 model_log 含 haiku（fallback retry）" "grep -q 'claude-haiku-4-5' '$TMP/model_log.txt'"
REASON_B3="$(echo "$J" | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('downgrade_reason','__MISSING__'))" 2>/dev/null || echo "__PARSE__")"
assert "B3 downgrade_reason 前缀 fallback_also_failed:" "echo '$REASON_B3' | grep -q '^fallback_also_failed:'"

# ============================================================
# B4: 已切 haiku 后 haiku 也 5xx
# ============================================================
section "B4: 已 haiku + haiku 5xx"
clear_model_log; set_mode "5xx"
J="$(call_judge "$SF_B2" "$PROJ_B2")"
assert_json "B4 downgraded=true" "$J" "downgraded" "True"
V="$(_read_state "$SF_B2" judge_active_model)"; assert "B4 state active 仍 haiku" "[ '$V' = 'claude-haiku-4-5' ]"

# ============================================================
# B5: sonnet 失败后成功 → failures 重置
# ============================================================
section "B5: sonnet 失败后成功 → failures 重置"
PROJ_B5="$TMP/b5"; mkdir -p "$PROJ_B5"
make_loop_yml "$PROJ_B5" "claude-sonnet-4-6" "claude-haiku-4-5" 2
SF_B5="$PROJ_B5/state.yml"; make_state "$PROJ_B5" "$SF_B5"

set_mode "5xx"; J="$(call_judge "$SF_B5" "$PROJ_B5")"
V="$(_read_state "$SF_B5" judge_consecutive_failures)"; assert "B5 第 1 次 5xx 后 failures=1" "[ '$V' = '1' ]"

set_mode "ok"; J="$(call_judge "$SF_B5" "$PROJ_B5")"
V="$(_read_state "$SF_B5" judge_consecutive_failures)"; assert "B5 第 2 次 ok 后 failures 重置为 0" "[ '$V' = '0' ]"
V="$(_read_state "$SF_B5" judge_active_model)"; assert "B5 active 仍是 sonnet" "[ '$V' = 'claude-sonnet-4-6' ]"

# ============================================================
# B6: 401 不计数
# ============================================================
section "B6: 401 不计数（凭证问题）"
PROJ_B6="$TMP/b6"; mkdir -p "$PROJ_B6"
make_loop_yml "$PROJ_B6" "claude-sonnet-4-6" "claude-haiku-4-5" 2
SF_B6="$PROJ_B6/state.yml"; make_state "$PROJ_B6" "$SF_B6"
set_mode "401"
J="$(call_judge "$SF_B6" "$PROJ_B6")"
V="$(_read_state "$SF_B6" judge_consecutive_failures 0)"; assert "B6 failures 不增加" "[ '$V' = '0' ]"

# ============================================================
# B7: 429 不计数
# ============================================================
section "B7: 429 不计数（rate_limit）"
PROJ_B7="$TMP/b7"; mkdir -p "$PROJ_B7"
make_loop_yml "$PROJ_B7" "claude-sonnet-4-6" "claude-haiku-4-5" 2
SF_B7="$PROJ_B7/state.yml"; make_state "$PROJ_B7" "$SF_B7"
set_mode "429"
J="$(call_judge "$SF_B7" "$PROJ_B7")"
V="$(_read_state "$SF_B7" judge_consecutive_failures 0)"; assert "B7 failures 不增加" "[ '$V' = '0' ]"

# ============================================================
# B8: parse_error 计数
# ============================================================
section "B8: parse_error 计数（同 5xx）"
PROJ_B8="$TMP/b8"; mkdir -p "$PROJ_B8"
make_loop_yml "$PROJ_B8" "claude-sonnet-4-6" "claude-haiku-4-5" 2
SF_B8="$PROJ_B8/state.yml"; make_state "$PROJ_B8" "$SF_B8"
set_mode "parse_err"
J="$(call_judge "$SF_B8" "$PROJ_B8")"
V="$(_read_state "$SF_B8" judge_consecutive_failures)"; assert "B8 failures=1（parse_error 计数）" "[ '$V' = '1' ]"

set_mode "parse_err"
J="$(call_judge "$SF_B8" "$PROJ_B8")"
V="$(_read_state "$SF_B8" judge_active_model)"; assert "B8 第 2 次 parse_error → 切 haiku" "[ '$V' = 'claude-haiku-4-5' ]"

# ============================================================
# B9: fallback_model 留空 → 不切
# ============================================================
section "B9: fallback_model 留空 → 失败直接 downgrade"
PROJ_B9="$TMP/b9"; mkdir -p "$PROJ_B9"
make_loop_yml "$PROJ_B9" "claude-sonnet-4-6" "" 2
SF_B9="$PROJ_B9/state.yml"; make_state "$PROJ_B9" "$SF_B9"
set_mode "5xx"
J="$(call_judge "$SF_B9" "$PROJ_B9")"
J="$(call_judge "$SF_B9" "$PROJ_B9")"   # 调 2 次到达阈值
V="$(_read_state "$SF_B9" judge_active_model "claude-sonnet-4-6")"; assert "B9 active 仍 sonnet（fallback 空不切）" "[ '$V' = 'claude-sonnet-4-6' ]"
assert_json "B9 downgraded=true" "$J" "downgraded" "True"

# ============================================================
# B10: 旧 state（无 V2.1 字段） → 默认值
# ============================================================
section "B10: 旧 state（无 V2.1 字段）→ 默认 active=primary"
PROJ_B10="$TMP/b10"; mkdir -p "$PROJ_B10"
make_loop_yml "$PROJ_B10" "claude-sonnet-4-6" "claude-haiku-4-5" 2
SF_B10="$PROJ_B10/state.yml"
cat > "$SF_B10" <<EOF
active: true
slug: legacy
iter: 1
project_root: "$PROJ_B10"
main_repo_path: "$PROJ_B10"
start_head: deadbeef
EOF
set_mode "ok"
J="$(call_judge "$SF_B10" "$PROJ_B10")"
assert_json "B10 model_used=sonnet（默认 primary）" "$J" "model_used" "claude-sonnet-4-6"
V="$(_read_state "$SF_B10" judge_active_model)"; assert "B10 state 已写入 active_model" "[ '$V' = 'claude-sonnet-4-6' ]"
V="$(_read_state "$SF_B10" judge_consecutive_failures)"; assert "B10 state 已写入 failures=0" "[ '$V' = '0' ]"

# ============================================================
# B11: 改 primary_model 立即生效
# ============================================================
section "B11: 改 primary_model 立即生效"
PROJ_B11="$TMP/b11"; mkdir -p "$PROJ_B11"
make_loop_yml "$PROJ_B11" "claude-sonnet-4-6" "claude-haiku-4-5" 2
SF_B11="$PROJ_B11/state.yml"; make_state "$PROJ_B11" "$SF_B11"
clear_model_log; set_mode "ok"
J="$(call_judge "$SF_B11" "$PROJ_B11")"
assert_json "B11 第 1 次 model_used=sonnet" "$J" "model_used" "claude-sonnet-4-6"

make_loop_yml "$PROJ_B11" "claude-haiku-4-5" "" 2
J="$(call_judge "$SF_B11" "$PROJ_B11")"
assert_json "B11 第 2 次 model_used 仍是 state.judge_active_model=sonnet" "$J" "model_used" "claude-sonnet-4-6"

sed -i '/^judge_active_model:/d; /^judge_consecutive_failures:/d' "$SF_B11"
J="$(call_judge "$SF_B11" "$PROJ_B11")"
assert_json "B11 清 state 后 model_used=haiku" "$J" "model_used" "claude-haiku-4-5"

# ============================================================
# B12: active_model=haiku 成功 → failures 重置，active 仍 haiku
# ============================================================
section "B12: active_model=haiku 成功 → failures 重置"
PROJ_B12="$TMP/b12"; mkdir -p "$PROJ_B12"
make_loop_yml "$PROJ_B12" "claude-sonnet-4-6" "claude-haiku-4-5" 2
SF_B12="$PROJ_B12/state.yml"
make_state "$PROJ_B12" "$SF_B12"
printf 'judge_active_model: "claude-haiku-4-5"\njudge_consecutive_failures: 1\n' >> "$SF_B12"

clear_model_log; set_mode "ok"
J="$(call_judge "$SF_B12" "$PROJ_B12")"

V="$(_read_state "$SF_B12" judge_active_model)"; assert "B12 state active_model 仍是 haiku" "[ '$V' = 'claude-haiku-4-5' ]"
V="$(_read_state "$SF_B12" judge_consecutive_failures)"; assert "B12 state failures 重置为 0" "[ '$V' = '0' ]"
assert_json "B12 downgraded=False" "$J" "downgraded" "False"
assert_json "B12 model_used=haiku" "$J" "model_used" "claude-haiku-4-5"

harness_report
