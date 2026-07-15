#!/usr/bin/env bash
# test-judge-edge-cases.sh — 补测 4 个 missing edge case（来自 reviewer TESTER_HINT）
#
# M1: PASS 分支 judge stop_done 后 consecutive_nudge_count 清零验证
# M2: outcome 后置补标的幂等性
# M3: run-judge-agent.sh --self-check 在凭证全缺时返回 exit 1
# M4: FAIL 分支 judge 脚本缺失的降级路径
#
# 用法：bash test-judge-edge-cases.sh
# 退出码：0=全部通过 / 1=有失败
#
# 预期耗时：~30 秒（全黑盒，不依赖网络/真实凭证）

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "edge case 补测 M1-M4"

HOOK_SCRIPT="${HARNESS_HOOK}"
JUDGE_SCRIPT="${HARNESS_REPO_ROOT}/skills/builder-loop/scripts/run-judge-agent.sh"

MOCK_PORT=19199
MOCK_PID=""

# ---- Mock server cleanup (supplement harness cleanup) ----
_edge_cleanup() {
  if [ -n "$MOCK_PID" ] && kill -0 "$MOCK_PID" 2>/dev/null; then
    kill "$MOCK_PID" 2>/dev/null || true
    wait "$MOCK_PID" 2>/dev/null || true
  fi
  _harness_cleanup
}
trap _edge_cleanup EXIT

TMP="$(mktemp -d)"
_HARNESS_TMPDIRS+=("$TMP")

assert "stop hook 脚本存在" "[ -f '${HOOK_SCRIPT}' ]"
assert "judge 脚本存在" "[ -f '${JUDGE_SCRIPT}' ]"

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

        if MODE == "stop_done":
            resp_action = "stop_done"
        elif MODE == "continue_nudge":
            resp_action = "continue_nudge"
        else:
            resp_action = "stop_done"

        inner = json.dumps({"action": resp_action, "confidence": CONF, "reason": "edge-case test"})
        response = {
            "id": "msg_edge",
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

# ---- Fixture 辅助 ----

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
  git -C "$dir" config user.email "edge@test.local"
  git -C "$dir" config user.name "edge-test"
  echo "fixture" > "${dir}/README.md"
  git -C "$dir" add -A 2>/dev/null || true
  git -C "$dir" -c core.hooksPath=/dev/null commit -q \
    -m "chore(test): [cr_id_skip] Edge case fixture" 2>/dev/null || true

  local HEAD
  HEAD=$(git -C "$dir" rev-parse HEAD 2>/dev/null || echo "abc123deadbeef")

  local slug="__main__"
  mkdir -p "${dir}/.claude/builder-loop/state"
  cat > "${dir}/.claude/builder-loop/state/${slug}.yml" <<STATEEOF
slug: ${slug}
iter: 1
max_iter: 5
start_head: ${HEAD}
project_root: "${dir}"
task_description: "Edge case test task"
${state_extra}
STATEEOF

  mkdir -p "${dir}/.claude/builder-loop"
  cat > "${dir}/.claude/builder-loop/transcript.jsonl" <<'JSONLEOF'
{"type":"user","message":{"role":"user","content":"add a feature"}}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"已完成，所有改动已提交。"}]}}
JSONLEOF

  echo "$slug"
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

_read_state_field() {
  local dir="$1" slug="$2" field="$3"
  local state_file="${dir}/.claude/builder-loop/state/${slug}.yml"
  [ -f "$state_file" ] || { echo ""; return 0; }
  grep "^${field}:" "$state_file" 2>/dev/null | head -1 | sed 's/^[^:]*: *//' | tr -d "'\"" || true
}

# =============================================================
# M1: PASS + judge stop_done → state rm + 新 setup 不残留 nudge 计数
# =============================================================
section "M1: PASS + judge stop_done → state rm + 新 setup 不残留 nudge 计数"
{
  PROJ="${TMP}/proj_m1"
  SLUG=$(make_fixture_project "$PROJ" "judge:
  enabled: true" "consecutive_nudge_count: 1" "true")

  start_mock_server "stop_done" 0.9

  ERR="${TMP}/err_m1.txt"
  EC=0
  call_stop_hook "$PROJ" "$ERR" || EC=$?

  assert "M1: exit code = 2" "[ '$EC' = '2' ]"
  assert "M1: stderr 含 PASS 关键词" "grep -qi 'pass' '${ERR}'"
  assert "M1: state 文件已被 rm（stop_done 后不残留）" \
    "[ ! -f '${PROJ}/.claude/builder-loop/state/${SLUG}.yml' ]"

  NEW_SLUG="edge-proj_m1_new"
  HEAD=$(git -C "$PROJ" rev-parse HEAD 2>/dev/null || echo "abc123")
  cat > "${PROJ}/.claude/builder-loop/state/${NEW_SLUG}.yml" <<NEWSTATEEOF
slug: ${NEW_SLUG}
iter: 1
max_iter: 5
start_head: ${HEAD}
project_root: "${PROJ}"
task_description: "New task after stop_done"
NEWSTATEEOF

  NEW_NUDGE_COUNT=$(_read_state_field "$PROJ" "$NEW_SLUG" "consecutive_nudge_count")
  assert "M1: 新 state 不含旧 consecutive_nudge_count" \
    "[ -z '${NEW_NUDGE_COUNT}' ] || [ '${NEW_NUDGE_COUNT}' = '0' ]"
}

# =============================================================
# M2: backfill 幂等性
# =============================================================
section "M2: backfill 幂等性（outcome 补标只补一次）"
{
  PROJ="${TMP}/proj_m2"
  SLUG=$(make_fixture_project "$PROJ" "judge:
  enabled: true" "" "false")

  cat > "${PROJ}/.claude/builder-loop/judge-trace.jsonl" <<'TRACEOF'
{"action":"continue_nudge","iter":1,"outcome":"nudge_was_correct","judge":{"action":"continue_nudge"}}
{"action":"continue_nudge","iter":2,"outcome":null,"judge":{"action":"continue_nudge"}}
TRACEOF

  ERR="${TMP}/err_m2_first.txt"
  EC=0
  call_stop_hook "$PROJ" "$ERR" || EC=$?

  ROW2_AFTER_FIRST=$(sed -n '2p' "${PROJ}/.claude/builder-loop/judge-trace.jsonl" 2>/dev/null || echo "{}")
  LAST_OUTCOME=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('outcome','__MISSING__'))" "$ROW2_AFTER_FIRST" 2>/dev/null || echo "__PARSE_ERROR__")
  assert "M2: 首次运行后第 2 行 outcome 已补标（非 null）" \
    "[ '${LAST_OUTCOME}' != 'null' ] && [ '${LAST_OUTCOME}' != 'None' ] && [ '${LAST_OUTCOME}' != '__MISSING__' ] && [ '${LAST_OUTCOME}' != '__PARSE_ERROR__' ]"

  # 第二次跑
  HEAD=$(git -C "$PROJ" rev-parse HEAD 2>/dev/null || echo "abc123")
  cat > "${PROJ}/.claude/builder-loop/state/${SLUG}.yml" <<STATEEOF2
slug: ${SLUG}
iter: 2
max_iter: 5
start_head: ${HEAD}
project_root: "${PROJ}"
task_description: "Edge case test task"
STATEEOF2

  ERR2="${TMP}/err_m2_second.txt"
  EC2=0
  call_stop_hook "$PROJ" "$ERR2" || EC2=$?

  ROW2_AFTER_SECOND=$(sed -n '2p' "${PROJ}/.claude/builder-loop/judge-trace.jsonl" 2>/dev/null || echo "{}")
  ORIG_OUTCOME_AFTER_SECOND=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('outcome','__MISSING__'))" "$ROW2_AFTER_SECOND" 2>/dev/null || echo "__PARSE_ERROR__")
  assert "M2: 第二次运行后原行 outcome 保持首次补标值（幂等）" \
    "[ '${ORIG_OUTCOME_AFTER_SECOND}' = '${LAST_OUTCOME}' ]"

  # 多行场景
  HEAD=$(git -C "$PROJ" rev-parse HEAD 2>/dev/null || echo "abc123")
  cat > "${PROJ}/.claude/builder-loop/state/${SLUG}.yml" <<STATEEOF3
slug: ${SLUG}
iter: 3
max_iter: 5
start_head: ${HEAD}
project_root: "${PROJ}"
task_description: "Edge case test task"
STATEEOF3

  cat > "${PROJ}/.claude/builder-loop/judge-trace.jsonl" <<'TRACEOF2'
{"action":"continue_nudge","iter":1,"outcome":"nudge_was_correct","judge":{"action":"continue_nudge"}}
{"action":"continue_nudge","iter":2,"outcome":"false_positive","judge":{"action":"continue_nudge"}}
{"action":"continue_nudge","iter":3,"outcome":null,"judge":{"action":"continue_nudge"}}
TRACEOF2

  ERR3="${TMP}/err_m2_multi.txt"
  EC3=0
  call_stop_hook "$PROJ" "$ERR3" || EC3=$?

  LINE1=$(sed -n '1p' "${PROJ}/.claude/builder-loop/judge-trace.jsonl" 2>/dev/null || echo "{}")
  LINE2=$(sed -n '2p' "${PROJ}/.claude/builder-loop/judge-trace.jsonl" 2>/dev/null || echo "{}")
  OUT1=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('outcome','__MISSING__'))" "$LINE1" 2>/dev/null || echo "__PARSE_ERROR__")
  OUT2=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('outcome','__MISSING__'))" "$LINE2" 2>/dev/null || echo "__PARSE_ERROR__")
  assert "M2: 多行场景 第1行 outcome 未被改写（保持 nudge_was_correct）" \
    "[ '${OUT1}' = 'nudge_was_correct' ]"
  assert "M2: 多行场景 第2行 outcome 未被改写（保持 false_positive）" \
    "[ '${OUT2}' = 'false_positive' ]"

  LINE3=$(sed -n '3p' "${PROJ}/.claude/builder-loop/judge-trace.jsonl" 2>/dev/null || echo "{}")
  OUT3=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('outcome','__MISSING__'))" "$LINE3" 2>/dev/null || echo "__PARSE_ERROR__")
  assert "M2: 多行场景 末尾未标的那条已被补标" \
    "[ '${OUT3}' != 'null' ] && [ '${OUT3}' != '__MISSING__' ]"
}

# =============================================================
# M3: --self-check 凭证全缺 → exit 1
# =============================================================
section "M3: --self-check 凭证全缺 → exit 1"
{
  EMPTY_HOME="${TMP}/empty_home_m3"
  mkdir -p "$EMPTY_HOME"

  SC_STDOUT="${TMP}/selfcheck_out.txt"
  SC_STDERR="${TMP}/selfcheck_err.txt"
  SC_EC=0

  env -i \
    HOME="$EMPTY_HOME" \
    PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    bash "${JUDGE_SCRIPT}" --self-check \
    >"$SC_STDOUT" 2>"$SC_STDERR" \
  || SC_EC=$?

  assert "M3: 凭证全缺时 exit code = 1" "[ '$SC_EC' = '1' ]"
  assert_contains "M3: stdout 含 credentials 行" "$SC_STDOUT" "credentials"
  assert "M3: stdout credentials 值为 none" \
    "grep -i 'credentials' '${SC_STDOUT}' | grep -qi 'none'"
  assert_contains "M3: stderr 含 ANTHROPIC_API_KEY 提示" "$SC_STDERR" "ANTHROPIC_API_KEY"

  # 凭证存在时 self-check 应 exit 0
  SC2_STDOUT="${TMP}/selfcheck2_out.txt"
  SC2_STDERR="${TMP}/selfcheck2_err.txt"
  SC2_EC=0

  env -i \
    HOME="$EMPTY_HOME" \
    PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    ANTHROPIC_API_KEY="test" \
    ANTHROPIC_BASE_URL="http://127.0.0.1:${MOCK_PORT}" \
    bash "${JUDGE_SCRIPT}" --self-check \
    >"$SC2_STDOUT" 2>"$SC2_STDERR" \
  || SC2_EC=$?

  assert "M3: 凭证存在时 exit code = 0" "[ '$SC2_EC' = '0' ]"
  assert_contains "M3: 凭证存在时 stdout 含 credentials 行" "$SC2_STDOUT" "credentials"
}

# =============================================================
# M4: FAIL 分支 + judge 脚本缺失 → 降级走原 V1.8 FAIL 路径
# =============================================================
section "M4: FAIL 分支 + judge 脚本缺失 → 降级走原 V1.8 FAIL 路径"
{
  PROJ="${TMP}/proj_m4"
  SLUG=$(make_fixture_project "$PROJ" "judge:
  enabled: true" "" "false")

  JUDGE_BACKUP="${JUDGE_SCRIPT}.bak_m4_${RANDOM}"
  mv "${JUDGE_SCRIPT}" "${JUDGE_BACKUP}"

  ERR="${TMP}/err_m4.txt"
  EC=0
  call_stop_hook "$PROJ" "$ERR" || EC=$?

  # 恢复脚本（放在断言之前避免测试失败时遗漏恢复）
  mv "${JUDGE_BACKUP}" "${JUDGE_SCRIPT}"

  assert "M4: exit code = 2（原 FAIL 路径）" "[ '$EC' = '2' ]"
  assert "M4: stderr 含 FAIL/error/失败 关键词" \
    "grep -qiE 'fail|失败|error' '${ERR}'"
  assert "M4: stderr 不含 [builder-loop judge 前缀（judge 未被调用）" \
    "! grep -q '\[builder-loop judge' '${ERR}'"
}

harness_report
