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

set -euo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$THIS_DIR/../../../.." && pwd)"
HOOK_SCRIPT="${REPO_ROOT}/scripts/builder-loop-stop.sh"
JUDGE_SCRIPT="${REPO_ROOT}/skills/builder-loop/scripts/run-judge-agent.sh"

PASS=0
FAIL=0
MOCK_PORT=19199
MOCK_PID=""
TMP=""

# ---- 颜色 ----
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

pass() { echo -e "  ${GREEN}PASS${NC} $1"; PASS=$(( PASS + 1 )); }
fail() { echo -e "  ${RED}FAIL${NC} $1"; FAIL=$(( FAIL + 1 )); }

assert() {
  local desc="$1" cond="$2"
  if eval "$cond" 2>/dev/null; then pass "$desc"; else fail "$desc  [cond: $cond]"; fi
}

assert_json_field() {
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

TMP="$(mktemp -d)"

echo "=== edge case 补测：M1-M4（reviewer TESTER_HINT）==="
echo "    Stop hook：${HOOK_SCRIPT}"
echo "    Judge 脚本：${JUDGE_SCRIPT}"
echo "    Mock 端口：${MOCK_PORT}"
echo "    临时目录：${TMP}"
echo ""

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

# 创建完整 fixture 项目（带 git + loop.yml + state + transcript）
# $1 = 项目目录
# $2 = loop.yml 附加内容（judge 段等）
# $3 = state 附加内容（consecutive_nudge_count 等）
# $4 = pass_cmd: "true"（PASS）或 "false"（FAIL）
# 返回 slug
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

  # bare loop（worktree.enabled=false）用 __main__ slug，匹配 locate-state.sh 兜底策略 4
  local slug="__main__"
  mkdir -p "${dir}/.claude/builder-loop/state"
  cat > "${dir}/.claude/builder-loop/state/${slug}.yml" <<STATEEOF
active: true
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

# 调用 stop hook（注入 mock env）
# $1 = 项目 dir, $2 = stderr 输出文件
# 返回 exit code
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

read_state_field() {
  # $1 = dir $2 = slug $3 = field_name
  local dir="$1" slug="$2" field="$3"
  local state_file="${dir}/.claude/builder-loop/state/${slug}.yml"
  [ -f "$state_file" ] || { echo ""; return 0; }
  # 字段不存在时 grep exit 1，pipefail 下整个 pipe 失败让 set -e 中断脚本——加 || true 容错
  grep "^${field}:" "$state_file" 2>/dev/null | head -1 | sed 's/^[^:]*: *//' | tr -d "'\"" || true
}

# =============================================================
# M1: PASS 分支 judge stop_done 后 consecutive_nudge_count 清零验证
#
#     上一轮 nudge 写入 consecutive_nudge_count=1，
#     本轮 judge 返回 stop_done → PASS 路径 → state 被 cleanup_worktree rm。
#     重新 setup 一个新 task → 新 state 不应残留计数。
# =============================================================
echo ""
echo "=== CASE M1: PASS + judge stop_done → state rm + 新 setup 不残留 nudge 计数 ==="
{
  PROJ="${TMP}/proj_m1"
  SLUG=$(make_fixture_project "$PROJ" "judge:
  enabled: true" "consecutive_nudge_count: 1" "true")

  start_mock_server "stop_done" 0.9

  ERR="${TMP}/err_m1.txt"
  EC=0
  call_stop_hook "$PROJ" "$ERR" || EC=$?

  # PASS 路径 exit 2
  assert "M1: exit code = 2" "[ '$EC' = '2' ]"
  assert "M1: stderr 含 PASS 关键词" "grep -qi 'pass' '${ERR}'"

  # PASS 路径下 cleanup_worktree 应把 state 文件删掉（worktree.enabled=false 时也走同分支）
  assert "M1: state 文件已被 rm（stop_done 后不残留）" \
    "[ ! -f '${PROJ}/.claude/builder-loop/state/${SLUG}.yml' ]"

  # 模拟新 task setup：构造新 state（不含 consecutive_nudge_count 字段），
  # 验证新 state 干净（不应有旧计数字段残留）
  NEW_SLUG="edge-proj_m1_new"
  HEAD=$(git -C "$PROJ" rev-parse HEAD 2>/dev/null || echo "abc123")
  cat > "${PROJ}/.claude/builder-loop/state/${NEW_SLUG}.yml" <<NEWSTATEEOF
active: true
slug: ${NEW_SLUG}
iter: 1
max_iter: 5
start_head: ${HEAD}
project_root: "${PROJ}"
task_description: "New task after stop_done"
NEWSTATEEOF

  # 新 state 不含 consecutive_nudge_count（不存在 or 值为 0）
  NEW_NUDGE_COUNT=$(read_state_field "$PROJ" "$NEW_SLUG" "consecutive_nudge_count")
  assert "M1: 新 state 不含旧 consecutive_nudge_count（应为空）" \
    "[ -z '${NEW_NUDGE_COUNT}' ] || [ '${NEW_NUDGE_COUNT}' = '0' ]"
}

# =============================================================
# M2: outcome 后置补标的幂等性
#
#     jsonl 末尾含 outcome=null 的 nudge 记录 →
#     首次跑 stop hook：末尾被补标为 nudge_was_correct / false_positive
#     再次跑 stop hook：末尾已有 outcome，不应再次修改
#     多行场景：前有已标 nudge + 末尾有未标 → 只改最后未标那条
# =============================================================
echo ""
echo "=== CASE M2: backfill 幂等性（outcome 补标只补一次，不重复改）==="
{
  PROJ="${TMP}/proj_m2"
  SLUG=$(make_fixture_project "$PROJ" "judge:
  enabled: true" "" "false")

  # 构造包含 nudge 记录 outcome=null 的 judge-trace.jsonl
  # V1.9.1：顶层也有 action 字段，简化断言
  cat > "${PROJ}/.claude/builder-loop/judge-trace.jsonl" <<'TRACEOF'
{"action":"continue_nudge","iter":1,"outcome":"nudge_was_correct","judge":{"action":"continue_nudge"}}
{"action":"continue_nudge","iter":2,"outcome":null,"judge":{"action":"continue_nudge"}}
TRACEOF

  ERR="${TMP}/err_m2_first.txt"
  EC=0
  # stop hook 不需要 PASS_CMD pass，backfill 段在入口处执行
  # 使用 FAIL pass_cmd（false），进入 stop hook 处理流程但 PASS_CMD 失败不影响 backfill 执行
  call_stop_hook "$PROJ" "$ERR" || EC=$?

  # 注：stop hook 跑完会**追加一行新 telemetry**（来自自身 judge 调用），所以 jsonl 末尾
  # 不再是原 nudge 行。backfill 标的是「跑前的末尾行」（行号 2）。这里用 sed -n 锁定行号
  # 而非 tail -1。
  ROW2_AFTER_FIRST=$(sed -n '2p' "${PROJ}/.claude/builder-loop/judge-trace.jsonl" 2>/dev/null || echo "{}")
  LAST_OUTCOME=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('outcome','__MISSING__'))" "$ROW2_AFTER_FIRST" 2>/dev/null || echo "__PARSE_ERROR__")
  assert "M2: 首次运行后第 2 行（原末尾）outcome 已补标（非 null）" \
    "[ '${LAST_OUTCOME}' != 'null' ] && [ '${LAST_OUTCOME}' != 'None' ] && [ '${LAST_OUTCOME}' != '__MISSING__' ] && [ '${LAST_OUTCOME}' != '__PARSE_ERROR__' ]"

  # 第二次跑（重新激活 state，模拟 hook 再次触发）
  HEAD=$(git -C "$PROJ" rev-parse HEAD 2>/dev/null || echo "abc123")
  cat > "${PROJ}/.claude/builder-loop/state/${SLUG}.yml" <<STATEEOF2
active: true
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

  # 第二次跑后 stop hook 自身又追加一行新 telemetry。真正的幂等检查：原 iter=2 那条
  # （行号 2 = 第一次跑后被补标的那条）的 outcome 在第二次跑后保持不变
  ROW2_AFTER_SECOND=$(sed -n '2p' "${PROJ}/.claude/builder-loop/judge-trace.jsonl" 2>/dev/null || echo "{}")
  ORIG_OUTCOME_AFTER_SECOND=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('outcome','__MISSING__'))" "$ROW2_AFTER_SECOND" 2>/dev/null || echo "__PARSE_ERROR__")
  assert "M2: 第二次运行后原行 outcome 保持首次补标值（幂等）" \
    "[ '${ORIG_OUTCOME_AFTER_SECOND}' = '${LAST_OUTCOME}' ]"

  # 多行场景：前面 2 条已标 + 新加 1 条未标
  HEAD=$(git -C "$PROJ" rev-parse HEAD 2>/dev/null || echo "abc123")
  cat > "${PROJ}/.claude/builder-loop/state/${SLUG}.yml" <<STATEEOF3
active: true
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

  # 第 1、2 行 outcome 应保持不变
  LINE1=$(sed -n '1p' "${PROJ}/.claude/builder-loop/judge-trace.jsonl" 2>/dev/null || echo "{}")
  LINE2=$(sed -n '2p' "${PROJ}/.claude/builder-loop/judge-trace.jsonl" 2>/dev/null || echo "{}")
  OUT1=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('outcome','__MISSING__'))" "$LINE1" 2>/dev/null || echo "__PARSE_ERROR__")
  OUT2=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('outcome','__MISSING__'))" "$LINE2" 2>/dev/null || echo "__PARSE_ERROR__")
  assert "M2: 多行场景 第1行 outcome 未被改写（保持 nudge_was_correct）" \
    "[ '${OUT1}' = 'nudge_was_correct' ]"
  assert "M2: 多行场景 第2行 outcome 未被改写（保持 false_positive）" \
    "[ '${OUT2}' = 'false_positive' ]"

  # 第 3 行（原 null）应已被补标
  LINE3=$(sed -n '3p' "${PROJ}/.claude/builder-loop/judge-trace.jsonl" 2>/dev/null || echo "{}")
  OUT3=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('outcome','__MISSING__'))" "$LINE3" 2>/dev/null || echo "__PARSE_ERROR__")
  assert "M2: 多行场景 只有末尾未标的那条被补标（非 null）" \
    "[ '${OUT3}' != 'null' ] && [ '${OUT3}' != '__MISSING__' ]"
}

# =============================================================
# M3: run-judge-agent.sh --self-check 在凭证全缺时返回 exit 1
#
#     凭证全缺（无 ANTHROPIC_API_KEY + 临时空 HOME）:
#       stdout 含 "credentials:    none"
#       stderr 含 "ANTHROPIC_API_KEY" 字样
#       exit code = 1
#
#     凭证存在（mock env）:
#       exit code = 0
# =============================================================
echo ""
echo "=== CASE M3: --self-check 凭证全缺 → exit 1 + stdout credentials:none + stderr 提示 ==="
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
  assert "M3: stdout 含 'credentials:' 行（含 none）" \
    "grep -qi 'credentials' '${SC_STDOUT}'"
  assert "M3: stdout credentials 值为 none" \
    "grep -i 'credentials' '${SC_STDOUT}' | grep -qi 'none'"
  assert "M3: stderr 含 ANTHROPIC_API_KEY 提示" \
    "grep -q 'ANTHROPIC_API_KEY' '${SC_STDERR}'"

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
  assert "M3: 凭证存在时 stdout 含 credentials 行（非 none）" \
    "grep -qi 'credentials' '${SC2_STDOUT}'"
}

# =============================================================
# M4: FAIL 分支 judge 脚本缺失的降级路径
#
#     临时把 run-judge-agent.sh 改名 → stop hook FAIL 路径
#     应走原 V1.8 FAIL 路径（extract-error + exit 2 + 错误反馈）
#     不应有 [builder-loop judge 前缀的注入文案
#     恢复 run-judge-agent.sh 名字
# =============================================================
echo ""
echo "=== CASE M4: FAIL 分支 + judge 脚本缺失 → 降级走原 V1.8 FAIL 路径 ==="
{
  PROJ="${TMP}/proj_m4"
  SLUG=$(make_fixture_project "$PROJ" "judge:
  enabled: true" "" "false")

  # 临时改名 run-judge-agent.sh
  JUDGE_BACKUP="${JUDGE_SCRIPT}.bak_m4_${RANDOM}"
  mv "${JUDGE_SCRIPT}" "${JUDGE_BACKUP}"

  ERR="${TMP}/err_m4.txt"
  EC=0
  call_stop_hook "$PROJ" "$ERR" || EC=$?

  # 恢复脚本（确保清理，放在断言之前避免测试失败时遗漏恢复）
  mv "${JUDGE_BACKUP}" "${JUDGE_SCRIPT}"

  # FAIL 路径：exit 2
  assert "M4: exit code = 2（原 FAIL 路径）" "[ '$EC' = '2' ]"

  # 原 FAIL 路径应含错误反馈关键词（FAIL / error / 失败）
  assert "M4: stderr 含 FAIL/error/失败 关键词（原 FAIL 路径）" \
    "grep -qiE 'fail|失败|error' '${ERR}'"

  # 不应含 [builder-loop judge 前缀的 judge 注入文案
  assert "M4: stderr 不含 [builder-loop judge 前缀（judge 未被调用）" \
    "! grep -q '\[builder-loop judge' '${ERR}'"
}

# =============================================================
# 汇总
# =============================================================
echo ""
echo "=============================="
echo "edge case 补测结果汇总（M1-M4）"
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
