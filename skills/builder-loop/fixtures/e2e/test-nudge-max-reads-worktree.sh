#!/usr/bin/env bash
# test-nudge-max-reads-worktree.sh — V2.0 E2E：stop hook 优先读 worktree 内 loop.yml 的 max_consecutive_nudges
#
# 验证场景：
#   Case 1: 主仓 loop.yml 写 max_consecutive_nudges=99
#           worktree 内 loop.yml 写 max_consecutive_nudges=1
#           state 已有 consecutive_nudge_count=1（已达 worktree 的 max=1）
#           mock judge 始终返回 continue_nudge
#           → stop hook 应走 max_nudge_reached 分支：
#               stderr 含"强制 stop_done（防脱缰）"文案，不含 nudge 推进文案
#
#   Case 2: 反向验证：worktree 内无 loop.yml（fallback 主仓 max=99）
#           state.consecutive_nudge_count=1（1 < 99）
#           → stop hook 应走 nudge 分支：
#               exit 2 + stderr 含 [builder-loop judge | ... | judge=continue_nudge]
#
# 两个 case 使用完全独立的主仓（REPO_C1 / REPO_C2），避免 worktree cleanup 干扰。
#
# 所有 API 调用走 mock（不依赖真实凭证 / 网络）。
#
# 用法：bash test-nudge-max-reads-worktree.sh
# 退出码：0=全部通过 / 1=有失败

set -euo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$THIS_DIR/../../../.." && pwd)"
HOOK_SCRIPT="${REPO_ROOT}/scripts/builder-loop-stop.sh"
SETUP_SCRIPT="${REPO_ROOT}/skills/builder-loop/scripts/setup-builder-loop.sh"

PASS=0
FAIL=0
MOCK_PORT=19198
MOCK_PID=""
TMP=""

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

pass() { echo -e "  ${GREEN}PASS${NC} $1"; PASS=$(( PASS + 1 )); }
fail() { echo -e "  ${RED}FAIL${NC} $1  [cond: $2]"; FAIL=$(( FAIL + 1 )); }

assert() {
  local desc="$1" cond="$2"
  if eval "$cond" 2>/dev/null; then pass "$desc"; else fail "$desc" "$cond"; fi
}

# ---- Cleanup ----
cleanup() {
  if [ -n "$MOCK_PID" ] && kill -0 "$MOCK_PID" 2>/dev/null; then
    kill "$MOCK_PID" 2>/dev/null || true
    wait "$MOCK_PID" 2>/dev/null || true
    MOCK_PID=""
  fi
  [ -n "$TMP" ] && rm -rf "$TMP"
}
trap cleanup EXIT

TMP="$(mktemp -d)"

echo "=== V2.0 E2E: stop hook nudge 上限优先读 worktree loop.yml ==="
echo "    Stop hook：${HOOK_SCRIPT}"
echo "    Mock 端口：${MOCK_PORT}"
echo "    临时目录：${TMP}"
echo ""

assert "stop hook 脚本存在" "[ -f '${HOOK_SCRIPT}' ]"
assert "setup 脚本存在" "[ -f '${SETUP_SCRIPT}' ]"

# ---- Mock server 管理 ----
# 始终返回 continue_nudge（让 stop hook 走 judge 路径，由 max_consecutive_nudges 决定最终行为）
start_mock_server() {
  if [ -n "$MOCK_PID" ] && kill -0 "$MOCK_PID" 2>/dev/null; then
    kill "$MOCK_PID" 2>/dev/null || true
    wait "$MOCK_PID" 2>/dev/null || true
    MOCK_PID=""
  fi

  local py_script="${TMP}/mock_server.py"
  cat > "$py_script" <<PYEOF
#!/usr/bin/env python3
import json
from http.server import HTTPServer, BaseHTTPRequestHandler

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        self.rfile.read(length)

        inner = json.dumps({
            "action": "continue_nudge",
            "confidence": 0.9,
            "reason": "mock for nudge-max-reads-worktree test"
        })
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
    i=$(( i + 1 ))
  done
}

# 调用 stop hook（注入 mock env）
# $1 = cwd（worktree 路径）
# $2 = 输出 stderr 到文件
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

# 创建主仓（worktree 启用 + judge 段）并调 setup 创建 worktree
# $1 = 目录名（在 TMP 下）
# $2 = judge 段 YAML（缩进 2 空格的 judge: 块）
# 输出 WORKTREE_PATH 和 STATE_FILE 到两个全局变量（_WT 和 _SF 后缀）
make_worktree_repo() {
  local dir_name="$1"
  local judge_yaml="$2"
  local repo="${TMP}/${dir_name}"
  mkdir -p "${repo}/.claude" "${repo}/src"
  cat > "${repo}/.claude/loop.yml" <<YMLEOF
pass_cmd:
  - stage: smoke
    cmd: "true"
    timeout: 10
max_iterations: 5
layout:
  source_dirs: [src]
  test_dirs: []
worktree:
  enabled: true
${judge_yaml}
YMLEOF
  echo "seed" > "${repo}/README.md"
  # .gitignore：排除 builder-loop 运行时文件，防止 merge-worktree-back 时 untracked 冲突
  cat > "${repo}/.gitignore" <<'IGNEOF'
.claude/builder-loop/
.claude/loop-runs/
IGNEOF
  git -C "${repo}" init -q
  git -C "${repo}" config user.email "e2e@test.local"
  git -C "${repo}" config user.name "e2e-test"
  git -C "${repo}" add -A
  git -C "${repo}" -c core.hooksPath=/dev/null commit -q \
    -m "chore(test): [cr_id_skip] ${dir_name} fixture init"
  local old_cwd
  old_cwd="$(pwd)"
  cd "${repo}"
  bash "${SETUP_SCRIPT}" "${dir_name}-slug" 2>&1 | head -5 || true
  cd "${old_cwd}"

  # 输出到带前缀的全局变量（bash 无引用传 hack，用 eval）
  local sf
  sf="$(find "${repo}/.claude/builder-loop/state" -maxdepth 1 -name "*-${dir_name}-slug.yml" 2>/dev/null | head -1 || true)"
  local wt
  wt="$(grep -E '^worktree_path:' "${sf}" 2>/dev/null | head -1 | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || true)"

  # 写 transcript（stop hook 需要）
  mkdir -p "${wt}/.claude/builder-loop"
  cat > "${wt}/.claude/builder-loop/transcript.jsonl" <<'JSONLEOF'
{"type":"user","message":{"role":"user","content":"add a feature"}}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"已完成，所有改动已提交。"}]}}
JSONLEOF

  REPO_PATH="${repo}"
  WORKTREE_PATH="${wt}"
  STATE_FILE="${sf}"
}

mkdir -p "${TMP}/fakehome"

# ============================================================
# Case 1：worktree max=1 + state nudge_count=1 → max_nudge_reached 分支
#          （主仓 max=99，worktree 覆盖为 max=1）
# ============================================================
echo ""
echo "=== Case 1: worktree max=1 + nudge_count=1 → 强制 stop_done（防脱缰）==="

make_worktree_repo "c1" "judge:
  enabled: true
  max_consecutive_nudges: 99"

C1_REPO="${REPO_PATH}"
C1_WT="${WORKTREE_PATH}"
C1_SF="${STATE_FILE}"

assert "C1: setup 创建了 state 文件" "[ -n '${C1_SF}' ] && [ -f '${C1_SF}' ]"
assert "C1: worktree 已创建" "[ -n '${C1_WT}' ] && [ -d '${C1_WT}' ]"
assert "C1: 主仓 loop.yml max=99" \
  "grep -q 'max_consecutive_nudges: 99' '${C1_REPO}/.claude/loop.yml'"

# 把 worktree 内 loop.yml 改为 max=1（不需要 commit）
cat > "${C1_WT}/.claude/loop.yml" <<'YMLEOF'
pass_cmd:
  - stage: smoke
    cmd: "true"
    timeout: 10
max_iterations: 5
layout:
  source_dirs: [src]
  test_dirs: []
worktree:
  enabled: true
judge:
  enabled: true
  max_consecutive_nudges: 1
YMLEOF
assert "C1: worktree loop.yml 已改为 max=1" \
  "grep -q 'max_consecutive_nudges: 1' '${C1_WT}/.claude/loop.yml'"
assert "C1: 主仓 loop.yml 仍是 max=99（未被修改）" \
  "grep -q 'max_consecutive_nudges: 99' '${C1_REPO}/.claude/loop.yml'"

# 设 state.consecutive_nudge_count=1（已达 worktree max=1）
python3 - <<PYEOF
import re

sf = '${C1_SF}'
with open(sf) as f:
    c = f.read()
if 'consecutive_nudge_count:' in c:
    c = re.sub(r'^consecutive_nudge_count:.*$', 'consecutive_nudge_count: 1', c, flags=re.MULTILINE)
else:
    c = c.rstrip('\n') + '\nconsecutive_nudge_count: 1\n'
with open(sf, 'w') as f:
    f.write(c)
print("C1 state: consecutive_nudge_count=1")
PYEOF

# 启动 mock server（始终 continue_nudge）
start_mock_server

ERR_C1="${TMP}/err_c1.txt"
EC_C1=0
call_stop_hook "${C1_WT}" "${ERR_C1}" || EC_C1=$?

# 断言：max_nudge_reached → fall-through merge → PASS 路径 → exit 2
assert "C1: exit code=2（走 PASS/merge 路径）" "[ '${EC_C1}' = '2' ]"
# 应含强制 stop_done 文案（consecutive_nudge_count >= max）
assert "C1: stderr 含强制 stop_done 文案（max_nudge_reached）" \
  "grep -qiE '强制 stop_done|consecutive_nudge_count.*>=.*max|max.*nudge.*防脱缰' '${ERR_C1}'"
# PASS 后的"请继续执行后续流程"是正常 PASS 路径输出，不是 nudge 推进文案
# 验证：不含 continue_nudge 后跟随的 nudge 推进词句（"请确认"是 nudge 推进询问）
assert "C1: stderr 不含 nudge 推进文案（请确认...是否完成）" \
  "! grep -qi '请确认：是确实完成' '${ERR_C1}'"

# ============================================================
# Case 2：反向验证 — 删掉 worktree loop.yml（fallback 主仓 max=99）
#          使用独立主仓（REPO_C2），consecutive_nudge_count=1（< 99）
#          → 应走 nudge 分支（exit 2 + nudge 文案）
# ============================================================
echo ""
echo "=== Case 2: 无 worktree loop.yml（fallback 主仓 max=99）→ nudge 分支 ==="

make_worktree_repo "c2" "judge:
  enabled: true
  max_consecutive_nudges: 99"

C2_REPO="${REPO_PATH}"
C2_WT="${WORKTREE_PATH}"
C2_SF="${STATE_FILE}"

assert "C2: setup 创建了 state 文件" "[ -n '${C2_SF}' ] && [ -f '${C2_SF}' ]"
assert "C2: worktree 已创建" "[ -n '${C2_WT}' ] && [ -d '${C2_WT}' ]"
assert "C2: 主仓 loop.yml max=99" \
  "grep -q 'max_consecutive_nudges: 99' '${C2_REPO}/.claude/loop.yml'"

# 删掉 worktree 内 loop.yml（模拟 worktree 内 loop.yml 不存在 → fallback 主仓）
rm -f "${C2_WT}/.claude/loop.yml"
assert "C2: worktree loop.yml 已删除" "[ ! -f '${C2_WT}/.claude/loop.yml' ]"

# 设 state.consecutive_nudge_count=1（< 主仓 max=99，应走 nudge 分支）
python3 - <<PYEOF
import re

sf = '${C2_SF}'
with open(sf) as f:
    c = f.read()
if 'consecutive_nudge_count:' in c:
    c = re.sub(r'^consecutive_nudge_count:.*$', 'consecutive_nudge_count: 1', c, flags=re.MULTILINE)
else:
    c = c.rstrip('\n') + '\nconsecutive_nudge_count: 1\n'
with open(sf, 'w') as f:
    f.write(c)
print("C2 state: consecutive_nudge_count=1")
PYEOF

ERR_C2="${TMP}/err_c2.txt"
EC_C2=0
call_stop_hook "${C2_WT}" "${ERR_C2}" || EC_C2=$?

# 断言：nudge 分支 → exit 2 + nudge 推进文案
assert "C2: exit code=2（nudge 分支）" "[ '${EC_C2}' = '2' ]"
assert "C2: stderr 含 [builder-loop judge 前缀（nudge 注入）" \
  "grep -q '\[builder-loop judge' '${ERR_C2}'"
# nudge 推进文案：judge 输出中包含"请确认"或"判定 agent"相关词
assert "C2: stderr 含 nudge 推进询问文案（请确认...）" \
  "grep -qi '请确认' '${ERR_C2}'"
# 不应含 max_nudge_reached（1 < 99）
assert "C2: stderr 不含 max_nudge_reached（未触发上限）" \
  "! grep -qi 'max_nudge_reached' '${ERR_C2}'"

# ============================================================
# 汇总
# ============================================================
echo ""
echo "=============================="
echo "测试结果汇总"
echo "=============================="
echo -e "  ${GREEN}PASS: ${PASS}${NC}"
if [ "${FAIL}" -gt 0 ]; then
  echo -e "  ${RED}FAIL: ${FAIL}${NC}"
  exit 1
else
  echo -e "  ${GREEN}FAIL: 0${NC}"
  exit 0
fi
