#!/usr/bin/env bash
# test-judge-env-file-load.sh — V2.1 E2E：env file 自动加载机制
#
# 覆盖 6 个 case：
#   A1 主 env 干净 + judge-env.sh 存在（含 sk-666） → source 后 self-check OK
#   A2 主 env 已设 sk-real + judge-env.sh 含别的值 → 不覆盖主 env
#   A3 主 env 干净 + judge-env.sh 不存在 → missing credentials（V1.9 行为）
#   A4 主 env 干净 + judge-env.sh 语法错 → stderr WARN + 仍 missing credentials
#   A5 主 env 干净 + loop.yml.judge.credentials_file 指定别处 → phase 1 source 别处
#   A6 loop.yml.credentials_file 等于全局默认路径 → phase 0 已 source，phase 1 跳过不报错
#
# 测试技巧：修改 HOME 环境变量重定向 ~/.claude/... 到临时目录，避免污染用户真实 env file
#
# 用法：bash test-judge-env-file-load.sh
# 退出码：0=全部通过 / 1=有失败

set -euo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$THIS_DIR/../../../.." && pwd)"
JUDGE_SCRIPT="${REPO_ROOT}/skills/builder-loop/scripts/run-judge-agent.sh"

PASS=0
FAIL=0

assert() {
  local desc="$1" cond="$2"
  if eval "$cond"; then echo "  ✅ $desc"; PASS=$(( PASS + 1 ));
  else echo "  ❌ $desc (cond: $cond)"; FAIL=$(( FAIL + 1 )); fi
}

echo "=== V2.1 E2E: env file 加载机制 ==="
assert "判 script 存在" "[ -f '$JUDGE_SCRIPT' ]"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# 全局公用：FAKE_HOME，重定向 ~/.claude/... 到这里
FAKE_HOME="$TMP/fake-home"
mkdir -p "$FAKE_HOME/.claude/skills/builder-loop"

run_self_check() {
  local fake_home="$1" extra_env="$2"
  # 注意 unset ANTHROPIC_API_KEY 让脚本完全靠 env file
  if [ -n "$extra_env" ]; then
    HOME="$fake_home" env -u ANTHROPIC_API_KEY $extra_env bash "$JUDGE_SCRIPT" --self-check 2>&1
  else
    HOME="$fake_home" env -u ANTHROPIC_API_KEY bash "$JUDGE_SCRIPT" --self-check 2>&1
  fi
}

# ============================================================
# A1: 主 env 干净 + judge-env.sh 存在 → source 后 OK
# ============================================================
echo ""
echo "--- A1: 主 env 干净 + judge-env.sh 存在 ---"
cat > "$FAKE_HOME/.claude/skills/builder-loop/judge-env.sh" <<'EOF'
ANTHROPIC_API_KEY=sk-666-from-file
ANTHROPIC_BASE_URL=http://localhost:4142
EOF

OUT_A1="$(run_self_check "$FAKE_HOME" "")" || EC_A1=$?
EC_A1="${EC_A1:-0}"

assert "A1 self-check exit=0" "[ '$EC_A1' -eq 0 ]"
assert "A1 输出 credentials: env" "echo '$OUT_A1' | grep -q 'credentials:    env'"
# self-check 输出 ANTHROPIC_API_KEY 仅前 10 字符：sk-666-fro...
assert "A1 输出含 sk-666 前缀（验证文件被 source）" "echo '$OUT_A1' | grep -q 'sk-666-fro'"

# ============================================================
# A2: 主 env 已设 + judge-env.sh 含别的 → 不覆盖
# ============================================================
echo ""
echo "--- A2: 主 env 已设 sk-real + 文件含 sk-666 → 不覆盖 ---"
EC_A2=0
OUT_A2="$(HOME="$FAKE_HOME" ANTHROPIC_API_KEY=sk-real-priority bash "$JUDGE_SCRIPT" --self-check 2>&1)" || EC_A2=$?

assert "A2 self-check exit=0" "[ '$EC_A2' -eq 0 ]"
assert "A2 输出 sk-real 前缀（主 env 优先）" "echo '$OUT_A2' | grep -q 'sk-real-pr'"
assert "A2 输出不含 sk-666（未覆盖）" "! echo '$OUT_A2' | grep -q 'sk-666'"

# ============================================================
# A3: 主 env 干净 + 文件不存在 → missing credentials（V1.9 行为）
# ============================================================
echo ""
echo "--- A3: 主 env 干净 + 文件不存在 → exit 1 ---"
rm -f "$FAKE_HOME/.claude/skills/builder-loop/judge-env.sh"
# 同时确保没有 ~/.claude.json oauth fallback
rm -f "$FAKE_HOME/.claude.json" 2>/dev/null || true

EC_A3=0
OUT_A3="$(run_self_check "$FAKE_HOME" "")" || EC_A3=$?

assert "A3 self-check exit=1（missing credentials）" "[ '$EC_A3' -eq 1 ]"
assert "A3 stderr 含 missing credentials" "echo '$OUT_A3' | grep -q 'missing credentials'"

# ============================================================
# A4: 主 env 干净 + 文件语法错 → WARN + 仍 missing
# ============================================================
echo ""
echo "--- A4: judge-env.sh 语法错 → stderr WARN + missing credentials ---"
cat > "$FAKE_HOME/.claude/skills/builder-loop/judge-env.sh" <<'EOF'
this is not valid bash syntax !!! @@@ %%%
EOF

EC_A4=0
OUT_A4="$(run_self_check "$FAKE_HOME" "" 2>&1)" || EC_A4=$?

assert "A4 self-check 仍 exit=1" "[ '$EC_A4' -eq 1 ]"
assert "A4 stderr 含 WARN: failed to source" "echo '$OUT_A4' | grep -q 'WARN: failed to source'"

# ============================================================
# A5: loop.yml.judge.credentials_file 指定别处 → phase 1 source
# ============================================================
echo ""
echo "--- A5: loop.yml 指定别处 credentials_file ---"
PROJ="$TMP/proj-a5"
mkdir -p "$PROJ/.claude"
ALT_FILE="$PROJ/.claude/local-judge-env.sh"
cat > "$ALT_FILE" <<'EOF'
ANTHROPIC_API_KEY=sk-from-loop-yml-path
ANTHROPIC_BASE_URL=http://localhost:4142
EOF
# 默认全局路径不存在
rm -f "$FAKE_HOME/.claude/skills/builder-loop/judge-env.sh"
cat > "$PROJ/.claude/loop.yml" <<EOF
pass_cmd:
  - { stage: smoke, cmd: "true", timeout: 10 }
judge:
  enabled: true
  credentials_file: "$ALT_FILE"
EOF

# 准备 state file + transcript file 跑一次完整调用，验证 phase 1 加载
# 但 self-check 不会读 loop.yml，必须用真实调用模拟（state-file 通过参数传）
mkdir -p "$PROJ/.claude/builder-loop/state"
cat > "$PROJ/.claude/builder-loop/state/test-a5.yml" <<EOF
active: true
slug: test-a5
iter: 1
max_iter: 3
project_root: "$PROJ"
main_repo_path: "$PROJ"
start_head: deadbeef
EOF
echo '{"role":"assistant","content":[{"type":"text","text":"ok"}]}' > "$PROJ/transcript.jsonl"

# 调用：phase 0 默认全局路径文件不存在不 source；phase 1 读到 loop.yml.credentials_file 后 source
# 期望：env 检测能成功，model 走 sonnet 默认（无网调通时 timeout downgrade，但凭证检测先通过）
JSON_A5="$(HOME="$FAKE_HOME" env -u ANTHROPIC_API_KEY bash "$JUDGE_SCRIPT" \
  --state-file "$PROJ/.claude/builder-loop/state/test-a5.yml" \
  --project-root "$PROJ" \
  --transcript-path "$PROJ/transcript.jsonl" \
  --pass-cmd-status PASS 2>/dev/null || true)"

assert "A5 输出含 credential_path=env（phase 1 source 成功）" \
  "echo '$JSON_A5' | python3 -c 'import json,sys;d=json.loads(sys.stdin.read());sys.exit(0 if d.get(\"credential_path\")==\"env\" else 1)'"

# ============================================================
# A6: credentials_file == 默认全局路径 → phase 1 检测到重复，不报错也不重复 source
# ============================================================
echo ""
echo "--- A6: loop.yml.credentials_file 等于默认全局路径 → 跳过 phase 1 二次 source ---"
PROJ_A6="$TMP/proj-a6"
mkdir -p "$PROJ_A6/.claude"
# 还原 judge-env.sh（A4 写了语法错文件，这里重置为合法内容）
cat > "$FAKE_HOME/.claude/skills/builder-loop/judge-env.sh" <<'EOF'
ANTHROPIC_API_KEY=sk-666-a6-global
ANTHROPIC_BASE_URL=http://localhost:4142
EOF

# loop.yml 将 credentials_file 显式写成与默认全局路径相同（$HOME 展开形式）
cat > "$PROJ_A6/.claude/loop.yml" <<EOF
pass_cmd:
  - { stage: smoke, cmd: "true", timeout: 10 }
judge:
  enabled: true
  credentials_file: "$FAKE_HOME/.claude/skills/builder-loop/judge-env.sh"
EOF

mkdir -p "$PROJ_A6/.claude/builder-loop/state"
cat > "$PROJ_A6/.claude/builder-loop/state/test-a6.yml" <<EOF
active: true
slug: test-a6
iter: 1
max_iter: 3
project_root: "$PROJ_A6"
main_repo_path: "$PROJ_A6"
start_head: deadbeef
EOF
echo '{"role":"assistant","content":[{"type":"text","text":"ok"}]}' > "$PROJ_A6/transcript.jsonl"

# 期望：phase 0 source 全局文件后，phase 1 发现 credentials_file == 默认全局路径 → 跳过
# 最终：不报错、不 exit 非 0，凭证来自 phase 0 source 的全局文件
EC_A6=0
JSON_A6="$(HOME="$FAKE_HOME" env -u ANTHROPIC_API_KEY bash "$JUDGE_SCRIPT" \
  --state-file "$PROJ_A6/.claude/builder-loop/state/test-a6.yml" \
  --project-root "$PROJ_A6" \
  --transcript-path "$PROJ_A6/transcript.jsonl" \
  --pass-cmd-status PASS 2>/tmp/stderr-a6.txt)" || EC_A6=$?

STDERR_A6="$(cat /tmp/stderr-a6.txt 2>/dev/null || true)"

# 凭证来自 phase 0 source 的全局文件（不管 phase 1 是否二次 source，结果应一致）
assert "A6 脚本无异常退出（exit=0 或因无 API 网络降级 downgraded 但不 exit 1）" \
  "[ '$EC_A6' -eq 0 ] || echo '$JSON_A6' | python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); sys.exit(0 if d.get(\"downgraded\") else 1)' 2>/dev/null"
assert "A6 stderr 不含 ERROR（phase 1 重复路径不报错）" \
  "! echo '$STDERR_A6' | grep -qi '^ERROR:'"
# 验证凭证来自全局 env file（credential_path=env 或 downgrade 时降级，但不因路径重复 fail）
assert "A6 输出中 credential_path=env（env file 被正确加载）" \
  "echo '$JSON_A6' | python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); sys.exit(0 if d.get(\"credential_path\")==\"env\" else 1)' 2>/dev/null"

# ============================================================
# 总结
# ============================================================
echo ""
echo "=== 总计 ==="
echo "  ✅ PASS: $PASS"
echo "  ❌ FAIL: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
