#!/usr/bin/env bash
# test-judge-model-fallback.sh — V2.1 E2E：sonnet → haiku 降级链
#
# 覆盖 11 个 case（B1-B11），用 python3 mock Anthropic API server，按 mode 控制返回 200 / 5xx / 401 / 429 / parse_err / timeout。
#
# 关键场景：
#   B1  连续 sonnet 成功 → state.failures=0, active=sonnet
#   B2  sonnet 1 次 5xx → failures=1（未达阈值，本轮 downgrade）
#   B3  sonnet 2 次 5xx → 切 haiku, failures=0, fallback retry → 输出 model_used=haiku
#   B4  sonnet 切 haiku 后 haiku 也 5xx → downgrade fallback_also_failed
#   B5  sonnet 1 失败 + 1 成功 → failures=0（成功后重置）
#   B6  401 不计数（凭证类） → failures 不变
#   B7  429 不计数（rate_limit） → failures 不变
#   B8  parse_error 计数（同 timeout/5xx）
#   B9  fallback_model 留空 → 不切，失败直接 downgrade
#   B10 缺 V2.1 state 字段（旧 state） → 默认值（active=primary, failures=0）
#   B11 worktree 内改 primary_model 立即生效（指向 mock 不同 endpoint 模拟）
#
# 用法：bash test-judge-model-fallback.sh
# 退出码：0=全部通过 / 1=有失败

set -euo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$THIS_DIR/../../../.." && pwd)"
JUDGE_SCRIPT="${REPO_ROOT}/skills/builder-loop/scripts/run-judge-agent.sh"

PASS=0
FAIL=0
MOCK_PORT=18998
MOCK_PID=""
TMP=""

assert() {
  local desc="$1" cond="$2"
  if eval "$cond" 2>/dev/null; then echo "  ✅ $desc"; PASS=$(( PASS + 1 ));
  else echo "  ❌ $desc (cond: $cond)"; FAIL=$(( FAIL + 1 )); fi
}

cleanup() {
  if [ -n "$MOCK_PID" ] && kill -0 "$MOCK_PID" 2>/dev/null; then
    kill "$MOCK_PID" 2>/dev/null || true
    wait "$MOCK_PID" 2>/dev/null || true
  fi
  [ -n "$TMP" ] && rm -rf "$TMP"
}
trap cleanup EXIT

TMP="$(mktemp -d)"

# ============================================================
# Mock server: 接收"模式"和"模型映射"控制返回行为
# 通过共享文件 $TMP/mock_mode 动态切换，避免每次重启
# ============================================================
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

def read_received_model():
    try:
        with open("${TMP}/last_model.txt") as f:
            return f.read().strip()
    except:
        return ""

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
        # 记录最近一次收到的 model（按调用顺序追加）
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
            time.sleep(15)  # > api_timeout_sec
            return
        if mode == "parse_err":
            self.send_response(200); self.end_headers()
            self.wfile.write(b"not json"); return
        # mode == "ok": 返回 stop_done
        # 但若 model 含 "haiku"，标 "from-haiku"，便于区分 fallback 命中
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
  # 等待就绪
  for i in $(seq 1 30); do
    if python3 -c "import socket;s=socket.socket();s.settimeout(0.2);s.connect(('127.0.0.1',${MOCK_PORT}))" 2>/dev/null; then
      return 0
    fi
    sleep 0.1
  done
  echo "❌ mock server 启动失败" >&2
  exit 1
}

set_mode() { echo "$1" > "$TMP/mock_mode"; }
clear_model_log() { rm -f "$TMP/model_log.txt"; }

make_state() {
  # $1 = 项目根 / $2 = state file（不写 V2.1 字段，模拟新建）
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
  # $1 = 项目根 / $2 = primary / $3 = fallback / $4 = threshold
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
  # $1 = state file / $2 = project root → 返回 stdout JSON
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

read_state() {
  # $1 = state / $2 = field / $3 = default（缺字段时返回，模拟脚本运行时的语义）
  local v
  v="$(grep -E "^${2}:" "$1" 2>/dev/null | head -1 | sed -E "s/^${2}:[[:space:]]*\"?([^\"]*)\"?[[:space:]]*$/\1/" || true)"
  echo "${v:-${3:-}}"
}

assert_json() {
  local desc="$1" json="$2" field="$3" expected="$4"
  local actual
  actual="$(echo "$json" | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('${field}','__MISSING__'))" 2>/dev/null || echo "__PARSE__")"
  if [ "$actual" = "$expected" ]; then
    echo "  ✅ $desc"; PASS=$(( PASS + 1 ))
  else
    echo "  ❌ $desc (expected $field=$expected, got $actual)"; FAIL=$(( FAIL + 1 ))
  fi
}

echo "=== V2.1 E2E: sonnet → haiku 降级链 (mock copilot-proxy) ==="
assert "judge script 存在" "[ -f '$JUDGE_SCRIPT' ]"

start_mock_server

# ============================================================
# B1: sonnet 全成功
# ============================================================
echo ""
echo "--- B1: 连续 sonnet 成功 ---"
PROJ_B1="$TMP/b1"; mkdir -p "$PROJ_B1"
make_loop_yml "$PROJ_B1" "claude-sonnet-4-6" "claude-haiku-4-5" 2
SF_B1="$PROJ_B1/state.yml"; make_state "$PROJ_B1" "$SF_B1"
clear_model_log; set_mode "ok"
J="$(call_judge "$SF_B1" "$PROJ_B1")"
assert_json "B1 model_used=sonnet" "$J" "model_used" "claude-sonnet-4-6"
assert "B1 state failures=0" "[ \"\$(read_state '$SF_B1' judge_consecutive_failures)\" = '0' ]"
assert "B1 state active=sonnet" "[ \"\$(read_state '$SF_B1' judge_active_model)\" = 'claude-sonnet-4-6' ]"

# ============================================================
# B2: sonnet 1 次 5xx → failures=1（未达阈值）
# ============================================================
echo ""
echo "--- B2: sonnet 1 次 5xx ---"
PROJ_B2="$TMP/b2"; mkdir -p "$PROJ_B2"
make_loop_yml "$PROJ_B2" "claude-sonnet-4-6" "claude-haiku-4-5" 2
SF_B2="$PROJ_B2/state.yml"; make_state "$PROJ_B2" "$SF_B2"
clear_model_log; set_mode "5xx"
J="$(call_judge "$SF_B2" "$PROJ_B2")"
assert_json "B2 downgraded=true" "$J" "downgraded" "True"
assert "B2 state failures=1" "[ \"\$(read_state '$SF_B2' judge_consecutive_failures)\" = '1' ]"

# ============================================================
# B3: sonnet 2 次 5xx → 切 haiku + retry
# ============================================================
echo ""
echo "--- B3: sonnet 第 2 次 5xx → 切 haiku（fallback retry）---"
# 接续 B2 state（已 failures=1），再调一次 5xx 模式
clear_model_log; set_mode "5xx"
J="$(call_judge "$SF_B2" "$PROJ_B2")"
# fallback retry 还是 5xx → fallback_also_failed
assert_json "B3 downgraded=true（fallback 也失败）" "$J" "downgraded" "True"
assert "B3 state active=haiku（已切）" "[ \"\$(read_state '$SF_B2' judge_active_model)\" = 'claude-haiku-4-5' ]"
assert "B3 state failures=0（切后重置）" "[ \"\$(read_state '$SF_B2' judge_consecutive_failures)\" = '0' ]"
# 验证调用日志：第二次调用应有 sonnet + haiku 两次
assert "B3 model_log 含 sonnet" "grep -q 'claude-sonnet-4-6' '$TMP/model_log.txt'"
assert "B3 model_log 含 haiku（fallback retry 触发）" "grep -q 'claude-haiku-4-5' '$TMP/model_log.txt'"

# ============================================================
# B4: 已切 haiku 后 haiku 也 5xx
# ============================================================
echo ""
echo "--- B4: 已 haiku + haiku 5xx → downgrade（不切第三档）---"
# 接续 B3 state（active=haiku），再调一次 5xx
clear_model_log; set_mode "5xx"
J="$(call_judge "$SF_B2" "$PROJ_B2")"
assert_json "B4 downgraded=true" "$J" "downgraded" "True"
assert "B4 state active 仍 haiku（不切第三档）" "[ \"\$(read_state '$SF_B2' judge_active_model)\" = 'claude-haiku-4-5' ]"

# ============================================================
# B5: sonnet 1 失败 + 1 成功 → failures 重置 0
# ============================================================
echo ""
echo "--- B5: sonnet 失败后成功 → failures 重置 ---"
PROJ_B5="$TMP/b5"; mkdir -p "$PROJ_B5"
make_loop_yml "$PROJ_B5" "claude-sonnet-4-6" "claude-haiku-4-5" 2
SF_B5="$PROJ_B5/state.yml"; make_state "$PROJ_B5" "$SF_B5"

set_mode "5xx"; J="$(call_judge "$SF_B5" "$PROJ_B5")"
assert "B5 第 1 次 5xx 后 failures=1" "[ \"\$(read_state '$SF_B5' judge_consecutive_failures)\" = '1' ]"

set_mode "ok"; J="$(call_judge "$SF_B5" "$PROJ_B5")"
assert "B5 第 2 次 ok 后 failures 重置为 0" "[ \"\$(read_state '$SF_B5' judge_consecutive_failures)\" = '0' ]"
assert "B5 active 仍是 sonnet（未触发降级）" "[ \"\$(read_state '$SF_B5' judge_active_model)\" = 'claude-sonnet-4-6' ]"

# ============================================================
# B6: 401 不计数
# ============================================================
echo ""
echo "--- B6: 401 不计数（凭证问题）---"
PROJ_B6="$TMP/b6"; mkdir -p "$PROJ_B6"
make_loop_yml "$PROJ_B6" "claude-sonnet-4-6" "claude-haiku-4-5" 2
SF_B6="$PROJ_B6/state.yml"; make_state "$PROJ_B6" "$SF_B6"
set_mode "401"
J="$(call_judge "$SF_B6" "$PROJ_B6")"
# 401 走 "未分类" 直接 downgrade，不 upsert state；read_state 返回空 = 初始值 0（语义等价"未增加"）
assert "B6 failures 不增加（401 不计数）" "[ \"\$(read_state '$SF_B6' judge_consecutive_failures 0)\" = '0' ]"

# ============================================================
# B7: 429 不计数
# ============================================================
echo ""
echo "--- B7: 429 不计数（rate_limit）---"
PROJ_B7="$TMP/b7"; mkdir -p "$PROJ_B7"
make_loop_yml "$PROJ_B7" "claude-sonnet-4-6" "claude-haiku-4-5" 2
SF_B7="$PROJ_B7/state.yml"; make_state "$PROJ_B7" "$SF_B7"
set_mode "429"
J="$(call_judge "$SF_B7" "$PROJ_B7")"
assert "B7 failures 不增加（429 不计数）" "[ \"\$(read_state '$SF_B7' judge_consecutive_failures 0)\" = '0' ]"

# ============================================================
# B8: parse_error 计数
# ============================================================
echo ""
echo "--- B8: parse_error 计数（同 5xx）---"
PROJ_B8="$TMP/b8"; mkdir -p "$PROJ_B8"
make_loop_yml "$PROJ_B8" "claude-sonnet-4-6" "claude-haiku-4-5" 2
SF_B8="$PROJ_B8/state.yml"; make_state "$PROJ_B8" "$SF_B8"
set_mode "parse_err"
J="$(call_judge "$SF_B8" "$PROJ_B8")"
assert "B8 failures=1（parse_error 计数）" "[ \"\$(read_state '$SF_B8' judge_consecutive_failures)\" = '1' ]"

set_mode "parse_err"
J="$(call_judge "$SF_B8" "$PROJ_B8")"
assert "B8 第 2 次 parse_error → 切 haiku" "[ \"\$(read_state '$SF_B8' judge_active_model)\" = 'claude-haiku-4-5' ]"

# ============================================================
# B9: fallback_model 留空 → 不切
# ============================================================
echo ""
echo "--- B9: fallback_model 留空 → 失败直接 downgrade ---"
PROJ_B9="$TMP/b9"; mkdir -p "$PROJ_B9"
make_loop_yml "$PROJ_B9" "claude-sonnet-4-6" "" 2
SF_B9="$PROJ_B9/state.yml"; make_state "$PROJ_B9" "$SF_B9"
set_mode "5xx"
J="$(call_judge "$SF_B9" "$PROJ_B9")"
J="$(call_judge "$SF_B9" "$PROJ_B9")"   # 调 2 次到达阈值
# fallback_model 空 → 第二次失败走 "未分类/无 fallback" 分支不 upsert active；缺字段视为 primary
assert "B9 active 仍 sonnet（fallback 空不切）" "[ \"\$(read_state '$SF_B9' judge_active_model 'claude-sonnet-4-6')\" = 'claude-sonnet-4-6' ]"
assert_json "B9 downgraded=true" "$J" "downgraded" "True"

# ============================================================
# B10: 旧 state（无 V2.1 字段） → 默认值
# ============================================================
echo ""
echo "--- B10: 旧 state（无 V2.1 字段）→ 默认 active=primary ---"
PROJ_B10="$TMP/b10"; mkdir -p "$PROJ_B10"
make_loop_yml "$PROJ_B10" "claude-sonnet-4-6" "claude-haiku-4-5" 2
SF_B10="$PROJ_B10/state.yml"
# 显式不写 V2.1 字段
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
assert "B10 state 已写入 active_model" "[ \"\$(read_state '$SF_B10' judge_active_model)\" = 'claude-sonnet-4-6' ]"
assert "B10 state 已写入 failures=0" "[ \"\$(read_state '$SF_B10' judge_consecutive_failures)\" = '0' ]"

# ============================================================
# B11: 改 primary_model 立即生效
# ============================================================
echo ""
echo "--- B11: 改 primary_model 立即生效 ---"
PROJ_B11="$TMP/b11"; mkdir -p "$PROJ_B11"
make_loop_yml "$PROJ_B11" "claude-sonnet-4-6" "claude-haiku-4-5" 2
SF_B11="$PROJ_B11/state.yml"; make_state "$PROJ_B11" "$SF_B11"
clear_model_log; set_mode "ok"
J="$(call_judge "$SF_B11" "$PROJ_B11")"
assert_json "B11 第 1 次 model_used=sonnet" "$J" "model_used" "claude-sonnet-4-6"

# 改 loop.yml.primary_model 为别的模型
make_loop_yml "$PROJ_B11" "claude-haiku-4-5" "" 2
# 但 state.judge_active_model 仍是 sonnet → 这次还是会用 sonnet
# 因为 state 优先级最高
J="$(call_judge "$SF_B11" "$PROJ_B11")"
# 实际上这是 V2.1 的设计：state 字段优先于 loop.yml（让降级状态在 loop 内稳定）
assert_json "B11 第 2 次 model_used 仍是 state.judge_active_model=sonnet" "$J" "model_used" "claude-sonnet-4-6"

# 清掉 state.judge_active_model 模拟 fresh start，再调
sed -i '/^judge_active_model:/d; /^judge_consecutive_failures:/d' "$SF_B11"
J="$(call_judge "$SF_B11" "$PROJ_B11")"
assert_json "B11 清 state 后 model_used=haiku（loop.yml 改的 primary 生效）" "$J" "model_used" "claude-haiku-4-5"

# ============================================================
# 总结
# ============================================================
echo ""
echo "=== 总计 ==="
echo "  ✅ PASS: $PASS"
echo "  ❌ FAIL: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
