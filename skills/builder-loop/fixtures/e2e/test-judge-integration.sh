#!/usr/bin/env bash
# test-judge-integration.sh — 集成测试：judge agent 与 stop hook 全流程
#
# 构造 fixture 项目（loop.yml + git + transcript），调用真实 stop hook 入口，
# 验证 judge action 如何路由 stop hook 出口（exit 2 文案 / state 字段 / telemetry）。
#
# 所有 API 调用走 mock（不依赖真实凭证 / 网络）。
# 每个 case 用独立临时目录避免 flock 互斥锁互相干扰。
#
# 用法：bash test-judge-integration.sh
# 退出码：0=全部通过 / 1=有失败
#
# 预期耗时：~60 秒（每个集成 case 含 PASS_CMD 执行 + stop hook 处理，约 5-10s/case）

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "集成测试：judge agent + stop hook 全流程"

HOOK_SCRIPT="${HARNESS_HOOK}"
JUDGE_SCRIPT="${HARNESS_REPO_ROOT}/skills/builder-loop/scripts/run-judge-agent.sh"

MOCK_PORT=19099
MOCK_PID=""

# ---- Mock server cleanup (supplement harness cleanup) ----
_integration_cleanup() {
  if [ -n "$MOCK_PID" ] && kill -0 "$MOCK_PID" 2>/dev/null; then
    kill "$MOCK_PID" 2>/dev/null || true
    wait "$MOCK_PID" 2>/dev/null || true
  fi
  _harness_cleanup
}
trap _integration_cleanup EXIT

TMP="$(mktemp -d)"
_HARNESS_TMPDIRS+=("$TMP")

assert "stop hook 脚本存在" "[ -f '${HOOK_SCRIPT}' ]"
assert "judge 脚本存在" "[ -f '${JUDGE_SCRIPT}' ]"

# ---- 自定义断言：JSON 字段 ----
assert_json_field() {
  local desc="$1" json="$2" field="$3" expected="$4"
  local actual
  actual="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('${field}','__MISSING__'))" "$json" 2>/dev/null || echo '__PARSE_ERROR__')"
  if [ "$actual" = "$expected" ]; then
    assert "$desc (${field}=${expected})" "true"
  else
    assert "$desc (${field}: expected=${expected}, actual=${actual})" "false"
  fi
}

# ---- Mock server 管理 ----
start_mock_server() {
  local mode="$1"
  local conf="${2:-0.9}"

  if [ -n "$MOCK_PID" ] && kill -0 "$MOCK_PID" 2>/dev/null; then
    kill "$MOCK_PID" 2>/dev/null || true
    wait "$MOCK_PID" 2>/dev/null || true
    MOCK_PID=""
  fi

  local py_script="${TMP}/mock_${mode}_${RANDOM}.py"
  cat > "$py_script" <<PYEOF
#!/usr/bin/env python3
import json, time
from http.server import HTTPServer, BaseHTTPRequestHandler

MODE = "${mode}"
CONF = ${conf}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        self.rfile.read(length)

        if MODE == "http500":
            self.send_response(500)
            self.end_headers()
            return

        if MODE == "kill":
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"Service Unavailable")
            return

        if MODE == "timeout":
            time.sleep(10)
            self.send_response(200)
            self.end_headers()
            return

        if MODE == "stop_done":
            resp_action = "stop_done"
        elif MODE == "continue_nudge":
            resp_action = "continue_nudge"
        elif MODE == "retry_transient":
            resp_action = "retry_transient"
        else:
            resp_action = "stop_done"

        inner = json.dumps({"action": resp_action, "confidence": CONF, "reason": "integration test"})
        response = {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": inner}],
            "model": "claude-haiku-4-5",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5}
        }
        body_out = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_out)))
        self.end_headers()
        self.wfile.write(body_out)

server = HTTPServer(("127.0.0.1", ${MOCK_PORT}), Handler)
server.serve_forever()
PYEOF

  python3 "$py_script" &
  MOCK_PID=$!

  local i=0
  while [ "$i" -lt 30 ]; do
    if python3 -c "
import socket, sys
s = socket.socket()
s.settimeout(0.2)
try:
    s.connect(('127.0.0.1', ${MOCK_PORT}))
    s.close()
    sys.exit(0)
except:
    sys.exit(1)
" 2>/dev/null; then
      break
    fi
    sleep 0.1
    i=$(( i + 1 ))
  done
}

stop_mock_server() {
  if [ -n "$MOCK_PID" ] && kill -0 "$MOCK_PID" 2>/dev/null; then
    kill "$MOCK_PID" 2>/dev/null || true
    wait "$MOCK_PID" 2>/dev/null || true
    MOCK_PID=""
  fi
}

# ---- Fixture 辅助函数 ----

make_fixture_project() {
  local dir="$1"
  local loop_extra="${2:-}"
  local state_extra="${3:-}"
  local pass_cmd_result="${4:-true}"

  mkdir -p "${dir}/src" "${dir}/tests" "${dir}/.claude"

  cat > "${dir}/.claude/loop.yml" <<LOOPEOF
pass_cmd:
  - stage: smoke
    cmd: "${pass_cmd_result}"
    timeout: 10
max_iterations: 5
layout:
  source_dirs: [src]
  test_dirs: [tests]
worktree:
  enabled: false
${loop_extra}
LOOPEOF

  git -C "$dir" init -q 2>/dev/null || true
  git -C "$dir" config user.email "e2e@test.local"
  git -C "$dir" config user.name "e2e-test"
  echo "fixture" > "${dir}/README.md"
  git -C "$dir" add -A 2>/dev/null || true
  git -C "$dir" -c core.hooksPath=/dev/null commit -q \
    -m "chore(test): [cr_id_skip] Integration fixture" 2>/dev/null || true

  local HEAD
  HEAD=$(git -C "$dir" rev-parse HEAD 2>/dev/null || echo "abc123deadbeef")

  local slug="__main__"
  mkdir -p "${dir}/.claude/builder-loop/state"
  cat > "${dir}/.claude/builder-loop/state/${slug}.yml" <<STATEEOF
active: true
slug: ${slug}
iter: 1
max_iter: 5
start_head: ${HEAD}
project_root: "${dir}"
task_description: "Integration test task"
${state_extra}
STATEEOF

  mkdir -p "${dir}/.claude/builder-loop"
  cat > "${dir}/.claude/builder-loop/transcript.jsonl" <<'JSONLEOF'
{"type":"user","message":{"role":"user","content":"add a feature"}}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"已完成，所有改动已提交。"}]}}
JSONLEOF

  echo "$dir"
}

call_stop_hook() {
  local proj="$1" err_file="$2"
  local ec=0
  printf '{"cwd": "%s", "transcript_path": "%s"}' \
    "$proj" \
    "${proj}/.claude/builder-loop/transcript.jsonl" \
  | env -i \
      HOME="${TMP}/fakehome" \
      PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
      ANTHROPIC_API_KEY="test" \
      ANTHROPIC_BASE_URL="http://127.0.0.1:${MOCK_PORT}" \
      bash "${HOOK_SCRIPT}" \
    2>"$err_file" >/dev/null \
  || ec=$?
  return "$ec"
}

call_stop_hook_no_creds() {
  local proj="$1" err_file="$2"
  local fake_home="${TMP}/fakehome_nocreds"
  mkdir -p "$fake_home"
  local ec=0
  printf '{"cwd": "%s", "transcript_path": "%s"}' \
    "$proj" \
    "${proj}/.claude/builder-loop/transcript.jsonl" \
  | env -i \
      HOME="$fake_home" \
      PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
      bash "${HOOK_SCRIPT}" \
    2>"$err_file" >/dev/null \
  || ec=$?
  return "$ec"
}

read_last_trace() {
  local dir="$1"
  local trace_file="${dir}/.claude/builder-loop/judge-trace.jsonl"
  [ -f "$trace_file" ] || { echo "{}"; return; }
  tail -1 "$trace_file"
}

_read_state_field() {
  local dir="$1" slug="$2" field="$3"
  local state_file="${dir}/.claude/builder-loop/state/${slug}.yml"
  [ -f "$state_file" ] || { echo ""; return 0; }
  grep "^${field}:" "$state_file" 2>/dev/null | head -1 | sed 's/^[^:]*: *//' | tr -d "'\"" || true
}

# =============================================================
# I1: PASS_CMD pass + judge stop_done → exit 2 + PASS 文案
# =============================================================
section "I1: PASS_CMD pass + judge stop_done → exit 2 + PASS 文案"
{
  PROJ="${TMP}/proj_i1"
  make_fixture_project "$PROJ" "judge:
  enabled: true" "" "true" >/dev/null

  start_mock_server "stop_done" 0.9

  ERR="${TMP}/err_i1.txt"
  EC=0
  call_stop_hook "$PROJ" "$ERR" || EC=$?

  assert "I1: exit code = 2" "[ '$EC' = '2' ]"
  assert "I1: stderr 含 PASS 关键词" "grep -qi 'pass' '${ERR}'"

  TRACE=$(read_last_trace "$PROJ")
  assert "I1: telemetry 落盘" "[ -n '$TRACE' ]"
}

# =============================================================
# I2: PASS_CMD pass + judge continue_nudge
# =============================================================
section "I2: PASS_CMD pass + judge continue_nudge → nudge 文案 + 计数递增"
{
  PROJ="${TMP}/proj_i2"
  SLUG="__main__"
  make_fixture_project "$PROJ" "judge:
  enabled: true" "consecutive_nudge_count: 0" "true" >/dev/null

  start_mock_server "continue_nudge" 0.87

  ERR="${TMP}/err_i2.txt"
  EC=0
  call_stop_hook "$PROJ" "$ERR" || EC=$?

  assert "I2: exit code = 2" "[ '$EC' = '2' ]"
  assert "I2: stderr 含 [builder-loop judge 前缀" \
    "grep -q '\[builder-loop judge' '${ERR}'"
  assert "I2: stderr 含 judge=continue_nudge" \
    "grep -q 'continue_nudge' '${ERR}'"

  NUDGE_COUNT=$(_read_state_field "$PROJ" "$SLUG" "consecutive_nudge_count")
  assert "I2: state.consecutive_nudge_count=1" "[ '${NUDGE_COUNT}' = '1' ]"
}

# =============================================================
# I3: 连续 nudge 达上限 → 强制 stop_done
# =============================================================
section "I3: 连续 nudge 达上限（2次）→ 强制 stop_done"
{
  PROJ="${TMP}/proj_i3"
  SLUG="__main__"
  make_fixture_project "$PROJ" "judge:
  enabled: true
  max_consecutive_nudges: 2" "consecutive_nudge_count: 2" "true" >/dev/null

  start_mock_server "continue_nudge" 0.87

  ERR="${TMP}/err_i3.txt"
  EC=0
  call_stop_hook "$PROJ" "$ERR" || EC=$?

  assert "I3: exit code = 2" "[ '$EC' = '2' ]"
  assert "I3: stderr 含强制 stop 关键词" \
    "grep -qiE 'max_nudge|force|强制' '${ERR}'"

  TRACE=$(read_last_trace "$PROJ")
  assert_json_field "I3: telemetry downgrade_reason=max_nudge_reached" \
    "$TRACE" "downgrade_reason" "max_nudge_reached"
}

# =============================================================
# I4: judge 降级 + PASS_CMD pass → 原 PASS 路径
# =============================================================
section "I4: judge 降级（missing_credentials）+ PASS_CMD pass → 原 PASS 路径"
{
  PROJ="${TMP}/proj_i4"
  make_fixture_project "$PROJ" "judge:
  enabled: true" "" "true" >/dev/null

  stop_mock_server

  ERR="${TMP}/err_i4.txt"
  EC=0
  call_stop_hook_no_creds "$PROJ" "$ERR" || EC=$?

  assert "I4: exit code = 2" "[ '$EC' = '2' ]"
  assert "I4: stderr 含 PASS 关键词" "grep -qi 'pass' '${ERR}'"
  assert "I4: stderr 不含 continue_nudge" \
    "! grep -qi 'continue_nudge' '${ERR}'"
}

# =============================================================
# I5: PASS_CMD FAIL + judge retry_transient
# =============================================================
section "I5: PASS_CMD FAIL + judge retry_transient → retry 文案"
{
  PROJ="${TMP}/proj_i5"
  make_fixture_project "$PROJ" "judge:
  enabled: true" "" "false" >/dev/null

  start_mock_server "retry_transient" 0.8

  ERR="${TMP}/err_i5.txt"
  EC=0
  call_stop_hook "$PROJ" "$ERR" || EC=$?

  assert "I5: exit code = 2" "[ '$EC' = '2' ]"
  assert "I5: stderr 含 retry_transient" \
    "grep -q 'retry_transient' '${ERR}'"
}

# =============================================================
# I6: PASS_CMD FAIL + judge 降级 → 原 FAIL 路径
# =============================================================
section "I6: PASS_CMD FAIL + judge 降级 → 原 FAIL 路径"
{
  PROJ="${TMP}/proj_i6"
  make_fixture_project "$PROJ" "judge:
  enabled: true" "" "false" >/dev/null

  stop_mock_server

  ERR="${TMP}/err_i6.txt"
  EC=0
  call_stop_hook_no_creds "$PROJ" "$ERR" || EC=$?

  assert "I6: exit code = 2" "[ '$EC' = '2' ]"
  assert "I6: stderr 含 FAIL/失败 关键词" \
    "grep -qiE 'fail|失败|error' '${ERR}'"
  assert "I6: stderr 不含 retry_transient" \
    "! grep -qi 'retry_transient' '${ERR}'"
}

# =============================================================
# I7: judge.enabled=false → 行为等价 V1.8
# =============================================================
section "I7: judge.enabled=false → 行为等价 V1.8"
{
  PROJ="${TMP}/proj_i7"
  make_fixture_project "$PROJ" "judge:
  enabled: false" "" "true" >/dev/null

  start_mock_server "continue_nudge" 0.87

  ERR="${TMP}/err_i7.txt"
  EC=0
  call_stop_hook "$PROJ" "$ERR" || EC=$?

  assert "I7: exit code = 2" "[ '$EC' = '2' ]"
  assert "I7: stderr 含 PASS 关键词" "grep -qi 'pass' '${ERR}'"
  assert "I7: stderr 不含 continue_nudge 文案" \
    "! grep -qi 'continue_nudge' '${ERR}'"

  TRACE=$(read_last_trace "$PROJ")
  assert "I7: telemetry 降级 disabled" \
    "python3 -c \"import json,sys; d=json.loads(sys.argv[1]); assert d.get('downgrade_reason')=='disabled', repr(d)\" '$TRACE' 2>/dev/null"
}

harness_report
