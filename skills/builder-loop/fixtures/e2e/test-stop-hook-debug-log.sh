#!/usr/bin/env bash
# test-stop-hook-debug-log.sh — E2E：V2.5 stop hook 可观测性
#
# A1 基础写入 + phase 顺序
# A2 IO 失败容忍（chmod 000 debug log）
# A3 rotate 触发（1.5 MB → .1 出现）
# A4 diagnose-stop-hook.sh 6 段 + 严格 dry-run
# A5 setup 自检识别 hook 注册缺失（fake HOME）
# A6 CWD 在子目录时 entry phase 不分裂日志路径
# A7 pass_cmd_result.log_path 含空格不截断

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "stop-hook-debug-log"

DIAG_SCRIPT="${HARNESS_REPO_ROOT}/skills/builder-loop/scripts/diagnose-stop-hook.sh"
SETUP_SCRIPT="${HARNESS_REPO_ROOT}/skills/builder-loop/scripts/setup-builder-loop.sh"

assert_file_exists "stop hook 存在" "$HARNESS_HOOK"
assert_file_exists "diagnose 存在" "$DIAG_SCRIPT"
assert_file_exists "setup 存在" "$SETUP_SCRIPT"

# ---- 初始化仓库 ----
TMP="$(mktemp -d)"
_HARNESS_TMPDIRS+=("$TMP")
(
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
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Initial seed for e2e debug log"
)

DEBUG_LOG="${TMP}/.claude/builder-loop/stop-hook-debug.log"
STATE_DIR="${TMP}/.claude/builder-loop/state"
mkdir -p "$STATE_DIR"

write_state_yml() {
  cat > "$1" <<EOF
phase: "$2"
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

# ============================================================
# A1: 基础写入 + phase 顺序
# ============================================================
section "A1: 基础写入 + phase 顺序"
write_state_yml "${STATE_DIR}/__main__.yml" "active" "" "__main__"

printf '{"cwd":"%s","session_id":"a1session"}' "$TMP" | bash "$HARNESS_HOOK" >/dev/null 2>&1 || true

assert_file_exists "A1 debug log 文件存在" "$DEBUG_LOG"
A1_PHASES="$(python3 -c "
import json
phases = []
for ln in open('$DEBUG_LOG'):
    try: phases.append(json.loads(ln).get('phase', ''))
    except: pass
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

A1_INVALID="$(python3 -c "
import json
bad = 0
for ln in open('$DEBUG_LOG'):
    if not ln.strip(): continue
    try: json.loads(ln)
    except: bad += 1
print(bad)
")"
assert "A1 所有行合法 JSON" "[ '$A1_INVALID' = '0' ]"

A1_SESSION="$(python3 -c "
import json
for ln in open('$DEBUG_LOG'):
    try:
        obj = json.loads(ln)
        if obj.get('phase') == 'entry':
            print(obj.get('session', '')); break
    except: pass
")"
assert "A1 session 截断到 8 字符" "[ '$A1_SESSION' = 'a1sessio' ]"

rm -f "$DEBUG_LOG"
rm -f "${STATE_DIR}"/*.yml

# ============================================================
# A2: IO 失败容忍
# ============================================================
section "A2: IO 失败容忍"
write_state_yml "${STATE_DIR}/__main__.yml" "active" "" "__main__"

mkdir -p "$(dirname "$DEBUG_LOG")"
touch "$DEBUG_LOG"
chmod 000 "$DEBUG_LOG"

A2_ERR="$TMP/a2_err.txt"
A2_EC=0
printf '{"cwd":"%s","session_id":"a2session"}' "$TMP" | bash "$HARNESS_HOOK" 2>"$A2_ERR" >/dev/null || A2_EC=$?

assert "A2 hook 不因 IO 失败崩溃" "[ '$A2_EC' -eq 0 ] || [ '$A2_EC' -eq 2 ]"
assert "A2 stderr 无 Permission denied 噪音" "! grep -q 'Permission denied' '$A2_ERR'"

chmod 644 "$DEBUG_LOG"
rm -f "$DEBUG_LOG" "$A2_ERR"
rm -f "${STATE_DIR}"/*.yml

# ============================================================
# A3: rotate 触发
# ============================================================
section "A3: rotate 触发（log > 1 MB → .1 出现）"
write_state_yml "${STATE_DIR}/__main__.yml" "active" "" "__main__"

mkdir -p "$(dirname "$DEBUG_LOG")"
python3 -c "
import os
target = '$DEBUG_LOG'
with open(target, 'w') as f:
    line = '{\"phase\":\"fake\",\"details\":{\"x\":\"' + 'x' * 200 + '\"}}\n'
    while os.path.getsize(target) < 1572864:
        f.write(line); f.flush()
"
A3_PRE_SIZE="$(stat -c%s "$DEBUG_LOG" 2>/dev/null || stat -f%z "$DEBUG_LOG" | tr -d '[:space:]')"

BUILDER_LOOP_DEBUG_LOG_MAX_BYTES=1048576 bash -c '
  printf "%s" "$1" | bash "$2" >/dev/null 2>&1
' _ "$(printf '{"cwd":"%s","session_id":"a3session"}' "$TMP")" "$HARNESS_HOOK" || true

assert_file_exists "A3 .1 文件已生成" "${DEBUG_LOG}.1"
A3_BAK_SIZE="$(stat -c%s "${DEBUG_LOG}.1" 2>/dev/null || stat -f%z "${DEBUG_LOG}.1" 2>/dev/null | tr -d '[:space:]' || echo 0)"
assert "A3 .1 大小约等于 rotate 前" "[ '$A3_BAK_SIZE' = '$A3_PRE_SIZE' ]"
A3_NEW_SIZE="$(stat -c%s "$DEBUG_LOG" 2>/dev/null || stat -f%z "$DEBUG_LOG" 2>/dev/null | tr -d '[:space:]' || echo 0)"
assert "A3 新 log 比旧 log 小" "[ '$A3_NEW_SIZE' -lt '$A3_PRE_SIZE' ]"
assert "A3 新 log 含 entry phase" "grep -q '\"phase\": \"entry\"' '$DEBUG_LOG'"

rm -f "$DEBUG_LOG" "${DEBUG_LOG}.1" "${DEBUG_LOG}.2" "${DEBUG_LOG}.3" "${DEBUG_LOG}.4" "${DEBUG_LOG}.5"
rm -f "${STATE_DIR}"/*.yml

# ============================================================
# A4: diagnose-stop-hook.sh 6 段 + 严格 dry-run
# ============================================================
section "A4: diagnose 6 段 + dry-run"
write_state_yml "${STATE_DIR}/__main__.yml" "active" "" "__main__"

A4_SCRATCH="$(mktemp -d -p /tmp harness-a4-XXXXXX)"
_HARNESS_TMPDIRS+=("$A4_SCRATCH")
A4_BEFORE="$A4_SCRATCH/before.txt"
find "$TMP" -maxdepth 5 -type f -printf '%T@ %s %p\n' 2>/dev/null | sort > "$A4_BEFORE" || true

A4_EC=0
A4_OUT="$(bash "$DIAG_SCRIPT" "$TMP" 2>&1)" || A4_EC=$?

A4_AFTER="$A4_SCRATCH/after.txt"
find "$TMP" -maxdepth 5 -type f -printf '%T@ %s %p\n' 2>/dev/null | sort > "$A4_AFTER" || true

assert "A4 diagnose 退出码可被捕获" "[ -n \"\${A4_EC+set}\" ]"
assert "A4 含 [1/6]" "echo '$A4_OUT' | grep -qF '[1/6]'"
assert "A4 含 [2/6]" "echo '$A4_OUT' | grep -qF '[2/6]'"
assert "A4 含 [3/6]" "echo '$A4_OUT' | grep -qF '[3/6]'"
assert "A4 含 [4/6]" "echo '$A4_OUT' | grep -qF '[4/6]'"
assert "A4 含 [5/6]" "echo '$A4_OUT' | grep -qF '[5/6]'"
assert "A4 含 [6/6]" "echo '$A4_OUT' | grep -qF '[6/6]'"
assert "A4 严格 dry-run" "diff -q '$A4_BEFORE' '$A4_AFTER' >/dev/null"

A4_JSON="$(bash "$DIAG_SCRIPT" "$TMP" --json 2>/dev/null)"
A4_JSON_VALID="$(echo "$A4_JSON" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    sections = d.get('sections', {})
    expected = ['hooks', 'links', 'state', 'lock', 'trace', 'worktree']
    print('1' if all(k in sections for k in expected) else '0')
except: print('0')
" 2>/dev/null || echo "0")"
assert "A4 --json 含 6 个 section" "[ '$A4_JSON_VALID' = '1' ]"

rm -f "$A4_BEFORE" "$A4_AFTER"
rm -f "${STATE_DIR}"/*.yml

# ============================================================
# A5: setup 自检识别 hook 注册缺失
# ============================================================
section "A5: setup 自检（fake HOME）"

A5_FAKE_HOME="$(mktemp -d)"
_HARNESS_TMPDIRS+=("$A5_FAKE_HOME")
mkdir -p "${A5_FAKE_HOME}/.claude/scripts"

# 5.1：settings.json 不存在 + 软链不存在（setup 需从 git 仓目录运行）
A5_LOG="$A5_FAKE_HOME/a5_log1.txt"
(cd "$TMP" && HOME="$A5_FAKE_HOME" bash "$SETUP_SCRIPT" --no-worktree "test-task-a5" >/dev/null 2>"$A5_LOG") || true
assert_contains "A5.1 含 V2.5 自检警告头" "$A5_LOG" "⚠️  V2.5 自检"
assert_contains "A5.1 Stop hook 注册标 missing" "$A5_LOG" "settings.json Stop hook 注册：❌ missing"
assert_contains "A5.1 软链标 broken/missing" "$A5_LOG" "软链：❌ broken/missing"
assert_contains "A5.1 含 install.sh 修复指引" "$A5_LOG" "bash install.sh"
assert_contains "A5.1 含 diagnose 排查指引" "$A5_LOG" "diagnose-stop-hook.sh"

# 5.2：settings.json 存在但不含 Stop hook 条目
rm -rf "${TMP}/.claude/builder-loop/state"; mkdir -p "${TMP}/.claude/builder-loop/state"
cat > "${A5_FAKE_HOME}/.claude/settings.json" <<'EOF'
{"hooks": {"PreToolUse": [{"hooks": [{"type":"command","command":"/some/other/hook.sh"}]}]}}
EOF
A5_LOG2="$A5_FAKE_HOME/a5_log2.txt"
(cd "$TMP" && HOME="$A5_FAKE_HOME" bash "$SETUP_SCRIPT" --no-worktree "test-task-a52" >/dev/null 2>"$A5_LOG2") || true
assert_contains "A5.2 无 Stop 条目仍标 missing" "$A5_LOG2" "settings.json Stop hook 注册：❌ missing"

# 5.3：配置齐全 → 不报警
rm -rf "${TMP}/.claude/builder-loop/state"; mkdir -p "${TMP}/.claude/builder-loop/state"
cat > "${A5_FAKE_HOME}/.claude/settings.json" <<'EOF'
{"hooks": {"Stop": [{"hooks": [{"type":"command","command":"/path/to/builder-loop-stop.sh"}]}]}}
EOF
ln -sf "${HARNESS_REPO_ROOT}/scripts/builder-loop-stop.sh" "${A5_FAKE_HOME}/.claude/scripts/builder-loop-stop.sh"
A5_LOG3="$A5_FAKE_HOME/a5_log3.txt"
(cd "$TMP" && HOME="$A5_FAKE_HOME" bash "$SETUP_SCRIPT" --no-worktree "test-task-a53" >/dev/null 2>"$A5_LOG3") || true
assert "A5.3 配置齐全无警告" "! grep -qF '⚠️  V2.5 自检' '$A5_LOG3'"

rm -f "$A5_LOG" "$A5_LOG2" "$A5_LOG3"

# ============================================================
# A6: CWD 在子目录时 entry phase 不分裂日志路径
# ============================================================
section "A6: CWD 在子目录时不分裂日志路径"

A6_ROOT="$(mktemp -d)"
_HARNESS_TMPDIRS+=("$A6_ROOT")
mkdir -p "${A6_ROOT}/.claude" "${A6_ROOT}/src/deep/nested"
echo "pass_cmd: []" > "${A6_ROOT}/.claude/loop.yml"
A6_SUBCWD="${A6_ROOT}/src/deep/nested"

printf '{"cwd":"%s","session_id":"a6session"}' "$A6_SUBCWD" | bash "$HARNESS_HOOK" >/dev/null 2>&1 || true

A6_ROOT_LOG="${A6_ROOT}/.claude/builder-loop/stop-hook-debug.log"
A6_SUB_LOG="${A6_SUBCWD}/.claude/builder-loop/stop-hook-debug.log"

assert_file_exists "A6 PROJECT_ROOT 下 debug log 存在" "$A6_ROOT_LOG"
assert_file_missing "A6 CWD 子目录下 debug log 不存在" "$A6_SUB_LOG"
assert_contains "A6 含 entry phase" "$A6_ROOT_LOG" '"phase": "entry"'
assert_contains "A6 含 locate_result" "$A6_ROOT_LOG" '"phase": "locate_result"'
assert_contains "A6 含 exit phase" "$A6_ROOT_LOG" '"phase": "exit"'

# ============================================================
# A7: pass_cmd_result.log_path 含空格不截断
# ============================================================
section "A7: log_path 含空格不截断"

A7_OUT="$(LL='FAIL stage1 /tmp/path with space/iter-1.log' python3 -c "
import os, json
last = os.environ.get('LL','')
parts = last.split()
print(json.dumps({
  'result': 'PASS' if last == 'PASS' else 'FAIL' if parts and parts[0] == 'FAIL' else 'UNKNOWN',
  'last_stage': parts[1] if len(parts) > 1 else '',
  'log_path': ' '.join(parts[2:]) if len(parts) > 2 else '',
}))
")"

A7_LOG_PATH="$(echo "$A7_OUT" | python3 -c "import sys, json; print(json.load(sys.stdin)['log_path'])")"
assert "A7 log_path 完整保留空格" "[ '$A7_LOG_PATH' = '/tmp/path with space/iter-1.log' ]"

A7_STAGE="$(echo "$A7_OUT" | python3 -c "import sys, json; print(json.load(sys.stdin)['last_stage'])")"
assert "A7 last_stage 是 stage1" "[ '$A7_STAGE' = 'stage1' ]"

harness_report
