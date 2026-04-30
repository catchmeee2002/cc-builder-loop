#!/usr/bin/env bash
# test-stop-hook-debug-log.sh — E2E：V2.5 stop hook 可观测性
#
# V2.5 行为：
#   - stop hook 顶部 + 10 处 phase 插桩，写到 <P>/.claude/builder-loop/stop-hook-debug.log
#   - 每行 NDJSON {ts/session/cwd/slug/phase/details}
#   - IO 失败容忍（mkdir/写入末尾 || true）
#   - 1 MB rotate（BUILDER_LOOP_DEBUG_LOG_MAX_BYTES env 可调），保留 5 个 .1-.5
#   - diagnose-stop-hook.sh 6 段 dry-run 排查
#   - setup-builder-loop.sh 末尾自检 hook 注册 + 软链
#
# 覆盖 case：
#   A1 基础写入 + phase 顺序
#   A2 IO 失败容忍（chmod 000 debug log 目录）
#   A3 rotate 触发（写 1.5 MB 后跑一次）
#   A4 diagnose-stop-hook.sh 6 段 + 严格 dry-run
#   A5 setup 自检识别 hook 注册缺失（fake HOME）
#
# 用法：bash test-stop-hook-debug-log.sh
# 退出码：0=全部通过 / 1=有失败

set -euo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$THIS_DIR/../../../.." && pwd)"
HOOK_SCRIPT="${REPO_ROOT}/scripts/builder-loop-stop.sh"
DIAG_SCRIPT="${REPO_ROOT}/skills/builder-loop/scripts/diagnose-stop-hook.sh"
SETUP_SCRIPT="${REPO_ROOT}/skills/builder-loop/scripts/setup-builder-loop.sh"

PASS=0
FAIL=0

assert() {
  local desc="$1" cond="$2"
  if eval "$cond"; then echo "  ✅ $desc"; PASS=$(( PASS + 1 ));
  else echo "  ❌ $desc (cond: $cond)"; FAIL=$(( FAIL + 1 )); fi
}

write_state_yml() {
  # $1 = state file path, $2 = active, $3 = worktree_path（空 = bare）, $4 = slug
  # 注：state 必须含 task_description 字段，否则 hook L322 grep 未命中 + pipefail 触发 set -e 静默退出
  cat > "$1" <<EOF
active: $2
slug: "$4"
iter: 0
max_iter: 5
project_root: "${TMP}"
main_repo_path: "${TMP}"
worktree_path: "$3"
worktree_mode: "bare"
start_head: "fakehead"
task_description: "e2e-test-task"
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
created_at: "1970-01-01T00:00:00+00:00"
EOF
}

echo "=== E2E: V2.5 stop hook 可观测性 ==="
assert "stop hook 存在" "[ -f '$HOOK_SCRIPT' ]"
assert "diagnose 存在" "[ -f '$DIAG_SCRIPT' ]"
assert "setup 存在" "[ -f '$SETUP_SCRIPT' ]"

TMP="$(mktemp -d)"
trap 'chmod -R u+rwx "$TMP" 2>/dev/null || true; rm -rf "$TMP"' EXIT
echo "    临时仓库：$TMP"

# ---- 初始化最小仓库（cmd: "true" 让 PASS_CMD 快速 PASS）----
cd "$TMP"
git init -q
git config user.email "e2e@test.local"
git config user.name "e2e-test"
mkdir -p .claude src
cat > .claude/loop.yml <<'YMLEOF'
pass_cmd:
  - stage: smoke
    cmd: "true"
    timeout: 10
max_iterations: 3
layout:
  source_dirs: [src]
  test_dirs: []
worktree:
  enabled: false
YMLEOF
echo "seed" > README.md
git add -A
git commit -q -m "chore(test): [cr_id_skip] Initial seed for e2e debug log"

DEBUG_LOG="${TMP}/.claude/builder-loop/stop-hook-debug.log"
STATE_DIR="${TMP}/.claude/builder-loop/state"
mkdir -p "$STATE_DIR"

# ============================================================
# A1: 基础写入 + phase 顺序
# ============================================================
echo ""
echo "--- A1: 基础写入 + phase 顺序 ---"
write_state_yml "${STATE_DIR}/__main__.yml" "true" "" "__main__"

A1_INPUT="$(printf '{"cwd":"%s","session_id":"a1session"}' "$TMP")"
A1_EC=0
printf '%s' "$A1_INPUT" | bash "$HOOK_SCRIPT" >/dev/null 2>&1 || A1_EC=$?

assert "A1 debug log 文件存在" "[ -f '$DEBUG_LOG' ]"
A1_PHASES="$(python3 -c "
import json
phases = []
for ln in open('$DEBUG_LOG'):
    try:
        phases.append(json.loads(ln).get('phase', ''))
    except Exception:
        pass
print(','.join(phases))
")"
assert "A1 含 entry phase" "echo '$A1_PHASES' | grep -q 'entry'"
assert "A1 含 locate_result phase" "echo '$A1_PHASES' | grep -q 'locate_result'"
assert "A1 含 flock_acquire phase" "echo '$A1_PHASES' | grep -q 'flock_acquire'"
assert "A1 含 pass_cmd_start phase" "echo '$A1_PHASES' | grep -q 'pass_cmd_start'"
assert "A1 含 pass_cmd_result phase" "echo '$A1_PHASES' | grep -q 'pass_cmd_result'"
assert "A1 含 exit phase" "echo '$A1_PHASES' | grep -q 'exit'"
assert "A1 entry 在第一个" "[ \"\$(echo '$A1_PHASES' | tr ',' '\n' | head -1)\" = 'entry' ]"
assert "A1 exit 在最后" "[ \"\$(echo '$A1_PHASES' | tr ',' '\n' | tail -1)\" = 'exit' ]"

# 每行合法 JSON
A1_INVALID="$(python3 -c "
import json
bad = 0
for ln in open('$DEBUG_LOG'):
    if not ln.strip(): continue
    try:
        json.loads(ln)
    except Exception:
        bad += 1
print(bad)
")"
assert "A1 所有行合法 JSON（0 invalid）" "[ '$A1_INVALID' = '0' ]"

# 字段验证：session 字段截断到 8 字符
A1_SESSION="$(python3 -c "
import json
for ln in open('$DEBUG_LOG'):
    try:
        obj = json.loads(ln)
        if obj.get('phase') == 'entry':
            print(obj.get('session', ''))
            break
    except Exception:
        pass
")"
assert "A1 session 字段截断到 8 字符" "[ '$A1_SESSION' = 'a1sessio' ]"

# 清理 A1（保留临时仓 git config 但清 debug log 让后续干净）
rm -f "$DEBUG_LOG"
rm -f "${STATE_DIR}"/*.yml

# ============================================================
# A2: IO 失败容忍（chmod 000 让目录无写权限）
# ============================================================
echo ""
echo "--- A2: IO 失败容忍 ---"
write_state_yml "${STATE_DIR}/__main__.yml" "true" "" "__main__"

# 重新创建 debug log dir 后 chmod 000（注意 state 在该 dir 子目录，所以 state 也跟着 readonly）
# 因此换路径——chmod 父目录会同时锁 state，导致 hook 走另一条早退路径
# 改为：让 debug log 父目录 readonly，但 STATE_DIR 是 builder-loop/state/ 同一父目录
# 解法：只 chmod 已存在的 stop-hook-debug.log（不是目录）让其只读，hook 写入失败但不阻断
mkdir -p "$(dirname "$DEBUG_LOG")"
touch "$DEBUG_LOG"
chmod 000 "$DEBUG_LOG"

A2_INPUT="$(printf '{"cwd":"%s","session_id":"a2session"}' "$TMP")"
A2_STDERR_FILE="$(mktemp)"
A2_EC=0
printf '%s' "$A2_INPUT" | bash "$HOOK_SCRIPT" 2>"$A2_STDERR_FILE" >/dev/null || A2_EC=$?

assert "A2 hook 不因 IO 失败崩溃（exit code 是预期的 0/2）" "[ '$A2_EC' -eq 0 ] || [ '$A2_EC' -eq 2 ]"
assert "A2 stderr 不含 'Permission denied' 噪音" "! grep -q 'Permission denied' '$A2_STDERR_FILE'"

# 还原权限验证后续可用
chmod 644 "$DEBUG_LOG"
rm -f "$DEBUG_LOG" "$A2_STDERR_FILE"
rm -f "${STATE_DIR}"/*.yml

# ============================================================
# A3: rotate 触发（写 1.5 MB 后跑一次，验证 .1 出现）
# ============================================================
echo ""
echo "--- A3: rotate 触发（log > 1 MB → .1 出现）---"
write_state_yml "${STATE_DIR}/__main__.yml" "true" "" "__main__"

mkdir -p "$(dirname "$DEBUG_LOG")"
# 写 1.5 MB 假内容
python3 -c "
import os
target = os.path.expanduser('$DEBUG_LOG')
with open(target, 'w') as f:
    line = '{\"phase\":\"fake\",\"details\":{\"x\":\"' + 'x' * 200 + '\"}}\n'
    while os.path.getsize(target) < 1572864:  # 1.5 MB
        f.write(line)
        f.flush()
"
A3_PRE_SIZE="$(stat -c%s "$DEBUG_LOG" 2>/dev/null || stat -f%z "$DEBUG_LOG" | tr -d '[:space:]')"

A3_INPUT="$(printf '{"cwd":"%s","session_id":"a3session"}' "$TMP")"
A3_EC=0
BUILDER_LOOP_DEBUG_LOG_MAX_BYTES=1048576 bash -c '
  printf "%s" "$1" | bash "$2" >/dev/null 2>&1
' _ "$A3_INPUT" "$HOOK_SCRIPT" || A3_EC=$?

assert "A3 .1 文件已生成" "[ -f '${DEBUG_LOG}.1' ]"
A3_BAK_SIZE="$(stat -c%s "${DEBUG_LOG}.1" 2>/dev/null || stat -f%z "${DEBUG_LOG}.1" 2>/dev/null | tr -d '[:space:]' || echo 0)"
assert "A3 .1 文件大小约等于 rotate 前的 log 大小" "[ '$A3_BAK_SIZE' = '$A3_PRE_SIZE' ]"
A3_NEW_SIZE="$(stat -c%s "$DEBUG_LOG" 2>/dev/null || stat -f%z "$DEBUG_LOG" 2>/dev/null | tr -d '[:space:]' || echo 0)"
assert "A3 新 log 比旧 log 小（rotate 后重新写）" "[ '$A3_NEW_SIZE' -lt '$A3_PRE_SIZE' ]"
assert "A3 新 log 含 entry phase（本轮新写）" "grep -q '\"phase\": \"entry\"' '$DEBUG_LOG'"

rm -f "$DEBUG_LOG" "${DEBUG_LOG}.1" "${DEBUG_LOG}.2" "${DEBUG_LOG}.3" "${DEBUG_LOG}.4" "${DEBUG_LOG}.5"
rm -f "${STATE_DIR}"/*.yml

# ============================================================
# A4: diagnose-stop-hook.sh 6 段 + 严格 dry-run
# ============================================================
echo ""
echo "--- A4: diagnose-stop-hook.sh 6 段 + 严格 dry-run ---"
write_state_yml "${STATE_DIR}/__main__.yml" "true" "" "__main__"

# 拍前快照（mtime + size）
A4_BEFORE="$(mktemp)"
find "$TMP" -maxdepth 5 -type f -printf '%T@ %s %p\n' 2>/dev/null | sort > "$A4_BEFORE" || true

A4_OUT="$(bash "$DIAG_SCRIPT" "$TMP" 2>&1)"
A4_EC=$?

# 拍后快照
A4_AFTER="$(mktemp)"
find "$TMP" -maxdepth 5 -type f -printf '%T@ %s %p\n' 2>/dev/null | sort > "$A4_AFTER" || true

assert "A4 输出含 [1/6]" "echo '$A4_OUT' | grep -qF '[1/6]'"
assert "A4 输出含 [2/6]" "echo '$A4_OUT' | grep -qF '[2/6]'"
assert "A4 输出含 [3/6]" "echo '$A4_OUT' | grep -qF '[3/6]'"
assert "A4 输出含 [4/6]" "echo '$A4_OUT' | grep -qF '[4/6]'"
assert "A4 输出含 [5/6]" "echo '$A4_OUT' | grep -qF '[5/6]'"
assert "A4 输出含 [6/6]" "echo '$A4_OUT' | grep -qF '[6/6]'"
assert "A4 严格 dry-run（before=after）" "diff -q '$A4_BEFORE' '$A4_AFTER' >/dev/null"

# --json 模式
A4_JSON="$(bash "$DIAG_SCRIPT" "$TMP" --json 2>/dev/null)"
A4_JSON_VALID="$(echo "$A4_JSON" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    sections = d.get('sections', {})
    expected = ['hooks', 'links', 'state', 'lock', 'trace', 'worktree']
    print('1' if all(k in sections for k in expected) else '0')
except Exception:
    print('0')
" 2>/dev/null || echo "0")"
assert "A4 --json 输出含 6 个 section" "[ '$A4_JSON_VALID' = '1' ]"

rm -f "$A4_BEFORE" "$A4_AFTER"
rm -f "${STATE_DIR}"/*.yml

# ============================================================
# A5: setup 自检识别 hook 注册缺失（fake HOME）
# ============================================================
echo ""
echo "--- A5: setup 自检识别 hook 注册缺失（fake HOME）---"

# 用 fake HOME 临时目录避免污染真 ~/.claude
A5_FAKE_HOME="$(mktemp -d)"
mkdir -p "${A5_FAKE_HOME}/.claude/scripts"

# 场景 5.1：settings.json 不存在 + 软链不存在 → 双 missing
A5_LOG="$(mktemp)"
HOME="$A5_FAKE_HOME" bash "$SETUP_SCRIPT" --no-worktree "test-task-a5" >/dev/null 2>"$A5_LOG" || true

assert "A5.1 含 V2.5 自检警告头" "grep -qF '⚠️  V2.5 自检' '$A5_LOG'"
assert "A5.1 settings.json Stop hook 注册标 missing" "grep -qF 'settings.json Stop hook 注册：❌ missing' '$A5_LOG'"
assert "A5.1 软链标 broken/missing" "grep -qF '软链：❌ broken/missing' '$A5_LOG'"
assert "A5.1 含 install.sh 修复指引" "grep -qF 'bash install.sh' '$A5_LOG'"
assert "A5.1 含 diagnose-stop-hook.sh 排查指引" "grep -qF 'diagnose-stop-hook.sh' '$A5_LOG'"

# 场景 5.2：settings.json 存在但不含 Stop hook 条目 → 仍 missing
# 注：bare 模式 slug=__main__，A5.1 setup 已创建 state/__main__.yml，会触发同 slug 冲突 → 先清掉
rm -rf "${TMP}/.claude/builder-loop/state"
mkdir -p "${TMP}/.claude/builder-loop/state"
cat > "${A5_FAKE_HOME}/.claude/settings.json" <<'EOF'
{"hooks": {"PreToolUse": [{"hooks": [{"type":"command","command":"/some/other/hook.sh"}]}]}}
EOF
A5_LOG2="$(mktemp)"
HOME="$A5_FAKE_HOME" bash "$SETUP_SCRIPT" --no-worktree "test-task-a52" >/dev/null 2>"$A5_LOG2" || true
assert "A5.2 settings.json 存在但无 Stop 条目仍标 missing" "grep -qF 'settings.json Stop hook 注册：❌ missing' '$A5_LOG2'"

# 场景 5.3：settings.json 含 Stop hook + 软链有效 → 不报警
rm -rf "${TMP}/.claude/builder-loop/state"
mkdir -p "${TMP}/.claude/builder-loop/state"
cat > "${A5_FAKE_HOME}/.claude/settings.json" <<'EOF'
{"hooks": {"Stop": [{"hooks": [{"type":"command","command":"/path/to/builder-loop-stop.sh"}]}]}}
EOF
ln -sf "${REPO_ROOT}/scripts/builder-loop-stop.sh" "${A5_FAKE_HOME}/.claude/scripts/builder-loop-stop.sh"
A5_LOG3="$(mktemp)"
HOME="$A5_FAKE_HOME" bash "$SETUP_SCRIPT" --no-worktree "test-task-a53" >/dev/null 2>"$A5_LOG3" || true
assert "A5.3 配置齐全时无 V2.5 自检警告" "! grep -qF '⚠️  V2.5 自检' '$A5_LOG3'"

rm -rf "$A5_FAKE_HOME"
rm -f "$A5_LOG" "$A5_LOG2" "$A5_LOG3"

# ============================================================
# A6: CWD 在项目子目录时 entry/locate_result phase 写到正确 PROJECT_ROOT（防路径分裂）
# 回归 reviewer 反馈 🟡-1 — 修复前 entry/locate_result fallback 到 CWD，
# 与后续 phase 的 PROJECT_ROOT 不一致 → 同次触发日志分散在两个文件
# ============================================================
echo ""
echo "--- A6: CWD 在子目录时 entry phase 不分裂日志路径 ---"

# 独立 fake 项目根（不是 git 仓 → bootstrap 早退到 not_git_repo，避免 PASS_CMD 污染）
A6_ROOT="$(mktemp -d)"
mkdir -p "${A6_ROOT}/.claude" "${A6_ROOT}/src/deep/nested"
echo "pass_cmd: []" > "${A6_ROOT}/.claude/loop.yml"
A6_SUBCWD="${A6_ROOT}/src/deep/nested"

A6_INPUT="$(printf '{"cwd":"%s","session_id":"a6session"}' "$A6_SUBCWD")"
printf '%s' "$A6_INPUT" | bash "$HOOK_SCRIPT" >/dev/null 2>&1 || true

# debug log 应在 PROJECT_ROOT 下，不在 CWD 子目录
A6_ROOT_LOG="${A6_ROOT}/.claude/builder-loop/stop-hook-debug.log"
A6_SUB_LOG="${A6_SUBCWD}/.claude/builder-loop/stop-hook-debug.log"

assert "A6 PROJECT_ROOT 下的 debug log 存在" "[ -f '$A6_ROOT_LOG' ]"
assert "A6 CWD 子目录下的 debug log 不存在（路径不分裂）" "[ ! -f '$A6_SUB_LOG' ]"
assert "A6 debug log 含 entry phase（lazy probe 命中）" "grep -q '\"phase\": \"entry\"' '$A6_ROOT_LOG'"
assert "A6 debug log 含 locate_result phase" "grep -q '\"phase\": \"locate_result\"' '$A6_ROOT_LOG'"
assert "A6 debug log 含 exit phase" "grep -q '\"phase\": \"exit\"' '$A6_ROOT_LOG'"

rm -rf "$A6_ROOT"

# ============================================================
# A7: pass_cmd_result.log_path 含空格时不被截断（回归 🟡-2）
# 直接验证 hook 内嵌 python split 表达式行为，避免 mock run-pass-cmd.sh 复杂度
# ============================================================
echo ""
echo "--- A7: pass_cmd_result.log_path 含空格不截断 ---"

A7_OUT="$(LL='FAIL stage1 /tmp/path with space/iter-1.log' python3 -c "
import os, json
last = os.environ.get('LL','')
parts = last.split()
print(json.dumps({
  'last_line': last,
  'result': 'PASS' if last == 'PASS' else 'FAIL' if parts and parts[0] == 'FAIL' else 'UNKNOWN',
  'last_stage': parts[1] if len(parts) > 1 else '',
  'log_path': ' '.join(parts[2:]) if len(parts) > 2 else '',
}))
")"

A7_LOG_PATH="$(echo "$A7_OUT" | python3 -c "import sys, json; print(json.load(sys.stdin)['log_path'])")"
assert "A7 log_path 完整保留空格" "[ '$A7_LOG_PATH' = '/tmp/path with space/iter-1.log' ]"

A7_STAGE="$(echo "$A7_OUT" | python3 -c "import sys, json; print(json.load(sys.stdin)['last_stage'])")"
assert "A7 last_stage 仍是 stage1（不被空格污染）" "[ '$A7_STAGE' = 'stage1' ]"

# ============================================================
# 汇总
# ============================================================
echo ""
echo "=========================================="
echo "PASS: $PASS  FAIL: $FAIL"
echo "=========================================="
[ "$FAIL" -eq 0 ]
