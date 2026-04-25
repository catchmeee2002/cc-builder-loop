#!/usr/bin/env bash
# test-judge-agent.sh — 单元测试：run-judge-agent.sh（mock Anthropic API）
#
# 覆盖 9 个 case（C1-C9），用 python3 BaseHTTPRequestHandler 起 mock 服务。
# 全部测试不依赖真实网络/凭证（mock 注入 ANTHROPIC_API_KEY=test + BASE_URL）。
#
# 用法：bash test-judge-agent.sh
# 退出码：0=全部通过 / 1=有失败
#
# 预期耗时：~20 秒（C3 API 超时 case 需等 timeout=2s 到期）

set -euo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$THIS_DIR/../../../.." && pwd)"
JUDGE_SCRIPT="${REPO_ROOT}/skills/builder-loop/scripts/run-judge-agent.sh"

PASS=0
FAIL=0
MOCK_PORT=18999
MOCK_PID=""
TMP=""

# ---- 颜色 ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass() { echo -e "  ${GREEN}PASS${NC} $1"; PASS=$(( PASS + 1 )); }
fail() { echo -e "  ${RED}FAIL${NC} $1"; FAIL=$(( FAIL + 1 )); }

assert() {
  local desc="$1" cond="$2"
  if eval "$cond" 2>/dev/null; then pass "$desc"; else fail "$desc  [cond: $cond]"; fi
}

assert_json_field() {
  # assert_json_field <desc> <json_string> <field> <expected_value>
  local desc="$1" json="$2" field="$3" expected="$4"
  local actual
  actual="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('${field}','__MISSING__'))" "$json" 2>/dev/null || echo '__PARSE_ERROR__')"
  if [ "$actual" = "$expected" ]; then
    pass "$desc (${field}=${expected})"
  else
    fail "$desc (${field}: expected=${expected}, actual=${actual})"
  fi
}

# ---- Cleanup ----
cleanup() {
  if [ -n "$MOCK_PID" ] && kill -0 "$MOCK_PID" 2>/dev/null; then
    kill "$MOCK_PID" 2>/dev/null || true
    wait "$MOCK_PID" 2>/dev/null || true
  fi
  [ -n "$TMP" ] && rm -rf "$TMP"
}
trap cleanup EXIT

# ---- 创建临时工作目录 ----
TMP="$(mktemp -d)"

echo "=== 单元测试：run-judge-agent.sh（mock API）==="
echo "    被测脚本：${JUDGE_SCRIPT}"
echo "    Mock 端口：${MOCK_PORT}"
echo "    临时目录：${TMP}"
echo ""

assert "被测脚本存在" "[ -f '${JUDGE_SCRIPT}' ]"

# ---- Mock server 辅助函数 ----

# 启动 mock server，写 python 脚本到 TMP
# $1 = 行为模式: "stop_done|continue_nudge|conf_low|http500|timeout|parse_error|check_model"
# $2 = confidence（可选，默认 0.9）
start_mock_server() {
  local mode="$1"
  local conf="${2:-0.9}"

  # Kill 前一个 mock server（如存在）
  if [ -n "$MOCK_PID" ] && kill -0 "$MOCK_PID" 2>/dev/null; then
    kill "$MOCK_PID" 2>/dev/null || true
    wait "$MOCK_PID" 2>/dev/null || true
    MOCK_PID=""
  fi

  local py_script="${TMP}/mock_anthropic_${mode}.py"
  cat > "$py_script" <<PYEOF
#!/usr/bin/env python3
"""Mock Anthropic API server for test-judge-agent.sh"""
import json
import time
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler

MODE = "${mode}"
CONF = ${conf}
RECEIVED_BODY = None

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 静默

    def do_POST(self):
        global RECEIVED_BODY
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        try:
            RECEIVED_BODY = json.loads(body)
        except Exception:
            RECEIVED_BODY = None

        if MODE == "http500":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"Internal Server Error")
            return

        if MODE == "timeout":
            # sleep 超过 api_timeout_sec（测试用 2s，这里 sleep 8s 保证超时）
            time.sleep(8)
            self.send_response(200)
            self.end_headers()
            return

        if MODE == "parse_error":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"hello world")
            return

        if MODE == "check_model":
            # 验证 payload.model 字段，返回 stop_done
            model = RECEIVED_BODY.get("model", "") if RECEIVED_BODY else ""
            # 将验证结果写到临时文件
            with open("${TMP}/received_model.txt", "w") as f:
                f.write(model)
            action = "stop_done"
            resp_action = action
        elif MODE == "stop_done":
            resp_action = "stop_done"
        elif MODE == "continue_nudge":
            resp_action = "continue_nudge"
        else:
            resp_action = "stop_done"

        # 构造 Anthropic API 响应格式
        inner = json.dumps({"action": resp_action, "confidence": CONF, "reason": "test reason"})
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

  # 等待 server 就绪（最多 3s）
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

# 构建最小 state.yml fixture
make_state_file() {
  local dir="$1" head="$2"
  mkdir -p "${dir}/.claude/builder-loop/state"
  cat > "${dir}/.claude/builder-loop/state/test-slug.yml" <<STATEEOF
active: true
slug: test-slug
iter: 1
max_iter: 5
start_head: ${head}
project_root: "${dir}"
task_description: "E2E test task"
STATEEOF
  echo "${dir}/.claude/builder-loop/state/test-slug.yml"
}

# 构建 loop.yml（可含 judge 段）
make_loop_yml() {
  local dir="$1"
  shift
  local extra="${1:-}"
  mkdir -p "${dir}/.claude"
  cat > "${dir}/.claude/loop.yml" <<LOOPYMLEOF
pass_cmd:
  - stage: smoke
    cmd: "true"
    timeout: 10
max_iterations: 5
layout:
  source_dirs: [src]
  test_dirs: [tests]
worktree:
  enabled: false
${extra}
LOOPYMLEOF
}

# 构建 transcript.jsonl fixture
make_transcript() {
  local path="$1"
  cat > "$path" <<'JSONLEOF'
{"type":"user","message":{"role":"user","content":"add a feature"}}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"已完成，所有改动已提交。"}]}}
JSONLEOF
}

# 初始化 git 仓库（仅需一次）
init_git_repo() {
  local dir="$1"
  git -C "$dir" init -q 2>/dev/null || true
  git -C "$dir" -c user.email=t@t -c user.name=t add -A 2>/dev/null || true
  git -C "$dir" -c user.email=t@t -c user.name=t -c core.hooksPath=/dev/null \
    commit -m "chore(test): [cr_id_skip] Init" --allow-empty -q 2>/dev/null || true
}

# 读取 judge-trace.jsonl 最后一行
read_last_trace() {
  local dir="$1"
  local trace_file="${dir}/.claude/builder-loop/judge-trace.jsonl"
  [ -f "$trace_file" ] || { echo "{}"; return; }
  tail -1 "$trace_file"
}

# 调用 judge 脚本（注入 mock 凭证 + BASE_URL）
call_judge() {
  # $1 = state_file $2 = project_root $3 = transcript $4 = pass_cmd_status
  # $5.. = 额外参数
  local state_file="$1" project_root="$2" transcript="$3" status="$4"
  shift 4
  env -i \
    HOME="${TMP}/fakehome" \
    PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    ANTHROPIC_API_KEY="test" \
    ANTHROPIC_BASE_URL="http://127.0.0.1:${MOCK_PORT}" \
    bash "${JUDGE_SCRIPT}" \
      --state-file "$state_file" \
      --project-root "$project_root" \
      --transcript-path "$transcript" \
      --pass-cmd-status "$status" \
      "$@" 2>/dev/null
}

# 同 call_judge 但保留 env（用于 C7 凭证测试）
call_judge_no_creds() {
  local state_file="$1" project_root="$2" transcript="$3" status="$4"
  shift 4
  # 用隔离的 HOME，不含 .claude.json；去掉 API key env
  local fake_home="${TMP}/fakehome_c7"
  mkdir -p "$fake_home"
  env -i \
    HOME="$fake_home" \
    PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    bash "${JUDGE_SCRIPT}" \
      --state-file "$state_file" \
      --project-root "$project_root" \
      --transcript-path "$transcript" \
      --pass-cmd-status "$status" \
      "$@" 2>/dev/null
}

# =============================================================
# C1: env 路径凭证 + PASS + 正常 builder → stop_done, downgraded=false
# =============================================================
echo ""
echo "=== CASE C1: env 凭证 + PASS + 正常 builder → stop_done ==="
{
  PROJ="${TMP}/proj_c1"
  mkdir -p "$PROJ/src"
  init_git_repo "$PROJ"
  make_loop_yml "$PROJ"
  echo "real content" > "$PROJ/src/main.py"
  git -C "$PROJ" add -A 2>/dev/null || true
  git -C "$PROJ" -c user.email=t@t -c user.name=t -c core.hooksPath=/dev/null \
    commit -m "chore(test): [cr_id_skip] Add src" -q 2>/dev/null || true

  HEAD=$(git -C "$PROJ" rev-parse HEAD 2>/dev/null || echo "abc123")
  STATE=$(make_state_file "$PROJ" "$HEAD")
  TRANSCRIPT="${TMP}/transcript_c1.jsonl"
  make_transcript "$TRANSCRIPT"

  start_mock_server "stop_done" 0.9

  OUT=$(call_judge "$STATE" "$PROJ" "$TRANSCRIPT" "PASS")
  EC=$?

  assert "C1: exit code = 0" "[ '$EC' = '0' ]"
  assert_json_field "C1: action=stop_done" "$OUT" "action" "stop_done"
  assert_json_field "C1: downgraded=false" "$OUT" "downgraded" "False"
  assert_json_field "C1: credential_path=env" "$OUT" "credential_path" "env"

  TRACE=$(read_last_trace "$PROJ")
  assert "C1: telemetry 落盘（trace 非空）" "[ -n '$TRACE' ]"
  assert_json_field "C1: telemetry downgraded=false" "$TRACE" "downgraded" "False"
}

# =============================================================
# C2: env 路径 + PASS + diff 为空 + builder 声称完成 → continue_nudge
# =============================================================
echo ""
echo "=== CASE C2: env 凭证 + PASS + builder 声称完成 → continue_nudge ==="
{
  PROJ="${TMP}/proj_c2"
  mkdir -p "$PROJ/src"
  init_git_repo "$PROJ"
  make_loop_yml "$PROJ"

  HEAD=$(git -C "$PROJ" rev-parse HEAD 2>/dev/null || echo "abc123")
  STATE=$(make_state_file "$PROJ" "$HEAD")
  TRANSCRIPT="${TMP}/transcript_c2.jsonl"
  make_transcript "$TRANSCRIPT"

  start_mock_server "continue_nudge" 0.87

  OUT=$(call_judge "$STATE" "$PROJ" "$TRANSCRIPT" "PASS")
  EC=$?

  assert "C2: exit code = 0" "[ '$EC' = '0' ]"
  assert_json_field "C2: action=continue_nudge" "$OUT" "action" "continue_nudge"
  assert_json_field "C2: downgraded=false" "$OUT" "downgraded" "False"

  TRACE=$(read_last_trace "$PROJ")
  assert_json_field "C2: telemetry action=continue_nudge" "$TRACE" "action" "continue_nudge"
}

# =============================================================
# C3: API 超时（api_timeout_sec=2，mock sleep 8s）→ downgraded=true, reason=timeout
# =============================================================
echo ""
echo "=== CASE C3: API 超时 → downgraded=true, downgrade_reason=timeout ==="
{
  PROJ="${TMP}/proj_c3"
  mkdir -p "$PROJ/src"
  init_git_repo "$PROJ"
  # 配置 judge.api_timeout_sec=2 加速超时
  make_loop_yml "$PROJ" "judge:
  enabled: true
  api_timeout_sec: 2"

  HEAD=$(git -C "$PROJ" rev-parse HEAD 2>/dev/null || echo "abc123")
  STATE=$(make_state_file "$PROJ" "$HEAD")
  TRANSCRIPT="${TMP}/transcript_c3.jsonl"
  make_transcript "$TRANSCRIPT"

  start_mock_server "timeout" 0.9

  OUT=$(call_judge "$STATE" "$PROJ" "$TRANSCRIPT" "PASS")
  EC=$?

  assert "C3: exit code = 0" "[ '$EC' = '0' ]"
  assert_json_field "C3: downgraded=true" "$OUT" "downgraded" "True"
  assert_json_field "C3: downgrade_reason=timeout" "$OUT" "downgrade_reason" "timeout"

  TRACE=$(read_last_trace "$PROJ")
  assert_json_field "C3: telemetry downgraded=true" "$TRACE" "downgraded" "True"
  assert_json_field "C3: telemetry reason=timeout" "$TRACE" "downgrade_reason" "timeout"
}

# =============================================================
# C4: API 返回 500 → downgraded=true, downgrade_reason=http_500
# =============================================================
echo ""
echo "=== CASE C4: API 500 → downgraded=true, downgrade_reason=http_500 ==="
{
  PROJ="${TMP}/proj_c4"
  mkdir -p "$PROJ/src"
  init_git_repo "$PROJ"
  make_loop_yml "$PROJ"

  HEAD=$(git -C "$PROJ" rev-parse HEAD 2>/dev/null || echo "abc123")
  STATE=$(make_state_file "$PROJ" "$HEAD")
  TRANSCRIPT="${TMP}/transcript_c4.jsonl"
  make_transcript "$TRANSCRIPT"

  start_mock_server "http500" 0.9

  OUT=$(call_judge "$STATE" "$PROJ" "$TRANSCRIPT" "PASS")
  EC=$?

  assert "C4: exit code = 0" "[ '$EC' = '0' ]"
  assert_json_field "C4: downgraded=true" "$OUT" "downgraded" "True"
  assert_json_field "C4: downgrade_reason=http_500" "$OUT" "downgrade_reason" "http_500"

  TRACE=$(read_last_trace "$PROJ")
  assert_json_field "C4: telemetry downgraded=true" "$TRACE" "downgraded" "True"
}

# =============================================================
# C5: API 返回非法 JSON（plain text）→ downgraded=true, reason=parse_error
# =============================================================
echo ""
echo "=== CASE C5: API 返回非法 JSON → downgraded=true, downgrade_reason=parse_error ==="
{
  PROJ="${TMP}/proj_c5"
  mkdir -p "$PROJ/src"
  init_git_repo "$PROJ"
  make_loop_yml "$PROJ"

  HEAD=$(git -C "$PROJ" rev-parse HEAD 2>/dev/null || echo "abc123")
  STATE=$(make_state_file "$PROJ" "$HEAD")
  TRANSCRIPT="${TMP}/transcript_c5.jsonl"
  make_transcript "$TRANSCRIPT"

  start_mock_server "parse_error" 0.9

  OUT=$(call_judge "$STATE" "$PROJ" "$TRANSCRIPT" "PASS")
  EC=$?

  assert "C5: exit code = 0" "[ '$EC' = '0' ]"
  assert_json_field "C5: downgraded=true" "$OUT" "downgraded" "True"
  assert_json_field "C5: downgrade_reason=parse_error" "$OUT" "downgrade_reason" "parse_error"

  TRACE=$(read_last_trace "$PROJ")
  assert_json_field "C5: telemetry downgraded=true" "$TRACE" "downgraded" "True"
}

# =============================================================
# C6: API 返回 confidence=0.3（低于阈值 0.5）→ downgraded=true, reason=low_confidence
# =============================================================
echo ""
echo "=== CASE C6: confidence=0.3（低于阈值）→ downgraded=true, downgrade_reason=low_confidence ==="
{
  PROJ="${TMP}/proj_c6"
  mkdir -p "$PROJ/src"
  init_git_repo "$PROJ"
  make_loop_yml "$PROJ"

  HEAD=$(git -C "$PROJ" rev-parse HEAD 2>/dev/null || echo "abc123")
  STATE=$(make_state_file "$PROJ" "$HEAD")
  TRANSCRIPT="${TMP}/transcript_c6.jsonl"
  make_transcript "$TRANSCRIPT"

  start_mock_server "continue_nudge" 0.3

  OUT=$(call_judge "$STATE" "$PROJ" "$TRANSCRIPT" "PASS")
  EC=$?

  assert "C6: exit code = 0" "[ '$EC' = '0' ]"
  assert_json_field "C6: downgraded=true" "$OUT" "downgraded" "True"
  assert_json_field "C6: downgrade_reason=low_confidence" "$OUT" "downgrade_reason" "low_confidence"

  TRACE=$(read_last_trace "$PROJ")
  assert_json_field "C6: telemetry downgraded=true" "$TRACE" "downgraded" "True"
  assert_json_field "C6: telemetry reason=low_confidence" "$TRACE" "downgrade_reason" "low_confidence"
}

# =============================================================
# C7: 凭证全缺（无 ANTHROPIC_API_KEY + HOME 下无 .claude.json）→ missing_credentials
# =============================================================
echo ""
echo "=== CASE C7: 凭证全缺 → downgraded=true, downgrade_reason=missing_credentials ==="
{
  PROJ="${TMP}/proj_c7"
  mkdir -p "$PROJ/src"
  init_git_repo "$PROJ"
  make_loop_yml "$PROJ"

  HEAD=$(git -C "$PROJ" rev-parse HEAD 2>/dev/null || echo "abc123")
  STATE=$(make_state_file "$PROJ" "$HEAD")
  TRANSCRIPT="${TMP}/transcript_c7.jsonl"
  make_transcript "$TRANSCRIPT"

  # 启动 mock server（虽然不应该被调用，但保险起见开着）
  start_mock_server "stop_done" 0.9

  # 使用 call_judge_no_creds：HOME 指向空目录，无 ANTHROPIC_API_KEY
  OUT=$(call_judge_no_creds "$STATE" "$PROJ" "$TRANSCRIPT" "PASS")
  EC=$?

  assert "C7: exit code = 0" "[ '$EC' = '0' ]"
  assert_json_field "C7: downgraded=true" "$OUT" "downgraded" "True"
  assert_json_field "C7: downgrade_reason=missing_credentials" "$OUT" "downgrade_reason" "missing_credentials"

  TRACE=$(read_last_trace "$PROJ")
  assert_json_field "C7: telemetry downgraded=true" "$TRACE" "downgraded" "True"
}

# =============================================================
# C8: judge.enabled=false（loop.yml 中设置）→ downgraded=true, reason=disabled
# =============================================================
echo ""
echo "=== CASE C8: judge.enabled=false → downgraded=true, downgrade_reason=disabled ==="
{
  PROJ="${TMP}/proj_c8"
  mkdir -p "$PROJ/src"
  init_git_repo "$PROJ"
  make_loop_yml "$PROJ" "judge:
  enabled: false"

  HEAD=$(git -C "$PROJ" rev-parse HEAD 2>/dev/null || echo "abc123")
  STATE=$(make_state_file "$PROJ" "$HEAD")
  TRANSCRIPT="${TMP}/transcript_c8.jsonl"
  make_transcript "$TRANSCRIPT"

  start_mock_server "stop_done" 0.9

  OUT=$(call_judge "$STATE" "$PROJ" "$TRANSCRIPT" "PASS")
  EC=$?

  assert "C8: exit code = 0" "[ '$EC' = '0' ]"
  assert_json_field "C8: downgraded=true" "$OUT" "downgraded" "True"
  assert_json_field "C8: downgrade_reason=disabled" "$OUT" "downgrade_reason" "disabled"

  TRACE=$(read_last_trace "$PROJ")
  assert_json_field "C8: telemetry downgraded=true" "$TRACE" "downgraded" "True"
}

# =============================================================
# C9: 模型 ID 含 dot（ANTHROPIC_DEFAULT_HAIKU_MODEL=claude-haiku-4.5）
#     → API 调用收到的 payload.model = claude-haiku-4-5（dash 规范化）
# =============================================================
echo ""
echo "=== CASE C9: 模型 ID dot 规范化为 dash（claude-haiku-4.5 → claude-haiku-4-5）==="
{
  PROJ="${TMP}/proj_c9"
  mkdir -p "$PROJ/src"
  init_git_repo "$PROJ"
  make_loop_yml "$PROJ"

  HEAD=$(git -C "$PROJ" rev-parse HEAD 2>/dev/null || echo "abc123")
  STATE=$(make_state_file "$PROJ" "$HEAD")
  TRANSCRIPT="${TMP}/transcript_c9.jsonl"
  make_transcript "$TRANSCRIPT"

  # 删除之前可能存在的 received_model.txt
  rm -f "${TMP}/received_model.txt"

  start_mock_server "check_model" 0.9

  # 注入 ANTHROPIC_DEFAULT_HAIKU_MODEL=claude-haiku-4.5（含 dot）
  env -i \
    HOME="${TMP}/fakehome" \
    PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    ANTHROPIC_API_KEY="test" \
    ANTHROPIC_BASE_URL="http://127.0.0.1:${MOCK_PORT}" \
    ANTHROPIC_DEFAULT_HAIKU_MODEL="claude-haiku-4.5" \
    bash "${JUDGE_SCRIPT}" \
      --state-file "$STATE" \
      --project-root "$PROJ" \
      --transcript-path "$TRANSCRIPT" \
      --pass-cmd-status "PASS" \
    2>/dev/null
  EC=$?

  assert "C9: exit code = 0" "[ '$EC' = '0' ]"

  # 等待 mock 写入 received_model.txt（最多 2s）
  RECEIVED_MODEL=""
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    if [ -f "${TMP}/received_model.txt" ]; then
      RECEIVED_MODEL="$(cat "${TMP}/received_model.txt" 2>/dev/null || echo '')"
      break
    fi
    sleep 0.1
  done

  assert "C9: mock 收到 payload.model=claude-haiku-4-5（dot 已规范化为 dash）" \
    "[ '${RECEIVED_MODEL}' = 'claude-haiku-4-5' ]"
}

# =============================================================
# 汇总
# =============================================================
echo ""
echo "=============================="
echo "单元测试结果汇总"
echo "=============================="
echo -e "  ${GREEN}PASS: ${PASS}${NC}"
if [ "${FAIL}" -gt 0 ]; then
  echo -e "  ${RED}FAIL: ${FAIL}${NC}"
  echo ""
  echo "退出码 1（有失败）"
  exit 1
else
  echo -e "  ${GREEN}FAIL: ${FAIL}${NC}"
  echo ""
  echo "全部通过"
  exit 0
fi
