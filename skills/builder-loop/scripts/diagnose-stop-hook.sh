#!/usr/bin/env bash
# diagnose-stop-hook.sh — V2.5 一键排查 stop hook 行为
#
# 用法：
#   bash diagnose-stop-hook.sh [project_root] [--json]
#   不传 project_root → 从 cwd 向上找 .claude/loop.yml
#
# 输出 6 段（人读模式）：
#   [1/6] settings.json hook 注册
#   [2/6] 软链状态
#   [3/6] state 目录
#   [4/6] lock / cursor / stash 状态
#   [5/6] 最近 trace + debug log 摘要
#   [6/6] git worktree list
#
# --json 模式：单 JSON 对象 stdout
#
# 退出码：
#   0 = 全 ok（所有段绿）
#   1 = 至少一段 ⚠️ warn
#   2 = 至少一段 ❌ fail
#
# dry-run 严格：执行前后 <P>/ 任何文件不变（mtime / size / content）

set -uo pipefail

# ---- 方案识别（max / copilot），与 install.sh 同判据 ----
detect_plan() {
  local url="${ANTHROPIC_BASE_URL:-}"
  case "$url" in
    *localhost*|*127.0.0.1*) echo "copilot" ;;
    *)                       echo "max" ;;
  esac
}
PLAN="$(detect_plan)"

JSON_MODE=0
PROJECT_ROOT_ARG=""
for arg in "$@"; do
  case "$arg" in
    --json) JSON_MODE=1 ;;
    *) PROJECT_ROOT_ARG="$arg" ;;
  esac
done

# ---- 找 PROJECT_ROOT ----
find_project_root() {
  local dir="$1" i=0
  while [ "$i" -lt 5 ]; do
    if [ -f "${dir}/.claude/loop.yml" ]; then
      echo "$dir"
      return 0
    fi
    [ "$dir" = "/" ] && return 1
    dir="$(dirname "$dir")"
    i=$(( i + 1 ))
  done
  return 1
}

if [ -n "$PROJECT_ROOT_ARG" ]; then
  PROJECT_ROOT="$(cd "$PROJECT_ROOT_ARG" 2>/dev/null && pwd -P || echo "$PROJECT_ROOT_ARG")"
else
  PROJECT_ROOT="$(find_project_root "$(pwd -P)" || echo "")"
fi

if [ -z "$PROJECT_ROOT" ] || [ ! -d "$PROJECT_ROOT" ]; then
  if [ "$JSON_MODE" -eq 1 ]; then
    echo '{"error":"project_root_not_found","hint":"传 [project_root] 参数或在含 .claude/loop.yml 的项目目录里跑"}'
  else
    echo "❌ 未找到 PROJECT_ROOT（含 .claude/loop.yml 的目录）" >&2
    echo "   用法: bash diagnose-stop-hook.sh [project_root] [--json]" >&2
  fi
  exit 2
fi

# ---- 收集诊断数据 ----
TS_NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "unknown")"
SETTINGS_JSON="${HOME}/.claude/settings.json"
STATE_DIR="${PROJECT_ROOT}/.claude/builder-loop/state"
LEGACY_DIR="${PROJECT_ROOT}/.claude/builder-loop/legacy"
TRACE_FILE="${PROJECT_ROOT}/.claude/loop-trace.jsonl"
DEBUG_LOG="${PROJECT_ROOT}/.claude/builder-loop/stop-hook-debug.log"
CURSOR_FILE="${PROJECT_ROOT}/.claude/builder-loop/last_processed_head"

# overall verdict（0 ok / 1 warn / 2 fail）
VERDICT=0
update_verdict() { local v="$1"; [ "$v" -gt "$VERDICT" ] && VERDICT="$v"; }

# ---- [1/6] settings.json hook 注册 ----
diagnose_hooks_python="$(cat <<'PYEOF'
import json, os, sys
# PLAN env 由外层 bash 通过 detect_plan() 注入；env 传递失败时静默降级到 "copilot"
# （要求全部 6 条，与 install.sh 老行为兼容）。
plan = os.environ.get('PLAN', 'copilot')
# 第 4 字段 plan_filter：''=通用，'copilot'=仅 copilot 方案要求
# 未来加新方案：plan_filter 可写 'copilot,gemini' 逗号分隔，过滤改 plan in pf.split(',')
expected_all = [
    ('Stop', 'builder-loop-stop.sh', None, ''),
    ('SubagentStart', 'tester-lock-write.sh', 'tester', ''),
    ('SubagentStop', 'tester-lock-clear.sh', 'tester', ''),
    ('PreToolUse', 'tester-lock-check.sh', 'Read|Grep|Glob', ''),
    ('PreToolUse', 'tester-write-guard.sh', 'Write|Edit|MultiEdit', 'copilot'),
    ('PreToolUse', 'reviewer-timing-check.sh', 'Agent', ''),
]
expected = [(t, s, m) for (t, s, m, pf) in expected_all if (not pf) or pf == plan]
results = []
sj = os.environ.get('SETTINGS_JSON', '')
if not os.path.isfile(sj):
    for hook_type, script, matcher in expected:
        results.append({'hook_type': hook_type, 'script': script, 'matcher': matcher, 'present': False, 'reason': 'settings.json not found'})
    print(json.dumps({'verdict': 'fail', 'items': results}))
    sys.exit(0)
try:
    cfg = json.load(open(sj))
    hooks = cfg.get('hooks', {})
except Exception as e:
    for hook_type, script, matcher in expected:
        results.append({'hook_type': hook_type, 'script': script, 'matcher': matcher, 'present': False, 'reason': f'parse error: {e}'})
    print(json.dumps({'verdict': 'fail', 'items': results}))
    sys.exit(0)
for hook_type, script, matcher in expected:
    found = False
    arr = hooks.get(hook_type, [])
    for entry in arr:
        for h in entry.get('hooks', []):
            cmd = h.get('command', '')
            if cmd.endswith(script) or ('/' + script) in cmd:
                found = True
                break
        if found:
            break
    results.append({'hook_type': hook_type, 'script': script, 'matcher': matcher, 'present': found})
verdict = 'ok' if all(r['present'] for r in results) else 'fail'
print(json.dumps({'verdict': verdict, 'items': results}))
PYEOF
)"

HOOKS_JSON="$(SETTINGS_JSON="$SETTINGS_JSON" PLAN="$PLAN" python3 -c "$diagnose_hooks_python" 2>/dev/null || echo '{"verdict":"fail","items":[]}')"
HOOKS_VERDICT="$(echo "$HOOKS_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('verdict','fail'))" 2>/dev/null || echo "fail")"
case "$HOOKS_VERDICT" in
  ok) ;;
  fail) update_verdict 2 ;;
  *) update_verdict 1 ;;
esac

# ---- [2/6] 软链状态 ----
LINKS_PYTHON="$(cat <<'PYEOF'
import json, os
expected = [
    '~/.claude/scripts/builder-loop-stop.sh',
    '~/.claude/scripts/tester-lock-write.sh',
    '~/.claude/scripts/tester-lock-check.sh',
    '~/.claude/scripts/tester-lock-clear.sh',
    '~/.claude/scripts/tester-write-guard.sh',
    '~/.claude/scripts/reviewer-timing-check.sh',
    '~/.claude/skills/builder-loop',
    '~/.claude/agents/tester.md',
    '~/.claude/agents/arbiter.md',
]
items = []
for p in expected:
    real = os.path.expanduser(p)
    is_link = os.path.islink(real)
    target = os.readlink(real) if is_link else None
    target_exists = os.path.exists(real)  # follows symlinks
    items.append({'path': p, 'is_link': is_link, 'target': target, 'target_exists': target_exists})
verdict = 'ok' if all(i['target_exists'] for i in items) else 'fail'
print(json.dumps({'verdict': verdict, 'items': items}))
PYEOF
)"

LINKS_JSON="$(python3 -c "$LINKS_PYTHON" 2>/dev/null || echo '{"verdict":"fail","items":[]}')"
LINKS_VERDICT="$(echo "$LINKS_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('verdict','fail'))" 2>/dev/null || echo "fail")"
case "$LINKS_VERDICT" in
  ok) ;;
  fail) update_verdict 2 ;;
  *) update_verdict 1 ;;
esac

# ---- [3/6] state 目录 ----
STATE_PYTHON="$(cat <<'PYEOF'
import json, os, re
sd = os.environ.get('STATE_DIR','')
items = []
if not os.path.isdir(sd):
    print(json.dumps({'verdict': 'ok', 'items': [], 'note': 'state dir does not exist (no active loop)'}))
    raise SystemExit
for fn in sorted(os.listdir(sd)):
    if not fn.endswith('.yml'):
        continue
    fp = os.path.join(sd, fn)
    if not os.path.isfile(fp):
        continue
    try:
        text = open(fp).read()
    except Exception:
        continue
    def grab(key):
        m = re.search(rf'^{key}:\s*"?([^"\n]*)"?\s*$', text, re.M)
        return m.group(1) if m else ''
    active = grab('active')
    iter_v = grab('iter')
    wt = grab('worktree_path')
    wt_alive = bool(wt) and os.path.isdir(wt)
    items.append({'file': fn, 'active': active, 'iter': iter_v, 'worktree_path': wt, 'worktree_alive': wt_alive})
# verdict: active=true 但 worktree 死 = warn；其余 ok
warn = any(i['active'] == 'true' and i['worktree_path'] and not i['worktree_alive'] for i in items)
print(json.dumps({'verdict': 'warn' if warn else 'ok', 'items': items}))
PYEOF
)"

STATE_JSON="$(STATE_DIR="$STATE_DIR" python3 -c "$STATE_PYTHON" 2>/dev/null || echo '{"verdict":"ok","items":[]}')"
STATE_VERDICT="$(echo "$STATE_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('verdict','ok'))" 2>/dev/null || echo "ok")"
case "$STATE_VERDICT" in
  ok) ;;
  warn) update_verdict 1 ;;
  fail) update_verdict 2 ;;
esac

# ---- [4/6] lock / cursor / stash ----
LOCK_PYTHON="$(cat <<'PYEOF'
import json, os, glob, subprocess
sd = os.environ.get('STATE_DIR','')
pr = os.environ.get('PROJECT_ROOT','')
cf = os.environ.get('CURSOR_FILE','')
locks = []
for fp in glob.glob(os.path.join(os.path.dirname(sd), 'stop-hook-*.lock')):
    try:
        sz = os.path.getsize(fp)
    except Exception:
        sz = 0
    locks.append({'file': os.path.basename(fp), 'size': sz})
cursor_content = ''
if os.path.isfile(cf):
    try:
        cursor_content = open(cf).read().strip()[:64]
    except Exception:
        pass
head = ''
try:
    head = subprocess.check_output(['git','-C',pr,'rev-parse','HEAD'], text=True, stderr=subprocess.DEVNULL).strip()
except Exception:
    pass
cursor_match = bool(cursor_content) and bool(head) and head.startswith(cursor_content)
stash_entries = []
try:
    out = subprocess.check_output(['git','-C',pr,'stash','list'], text=True, stderr=subprocess.DEVNULL)
    for ln in out.splitlines():
        if 'builder-loop:auto:slug=' in ln:
            stash_entries.append(ln[:120])
except Exception:
    pass
print(json.dumps({'verdict': 'ok', 'locks': locks, 'cursor_content': cursor_content, 'head_short': head[:8], 'cursor_matches_head': cursor_match, 'stash_entries': stash_entries}))
PYEOF
)"

LOCK_JSON="$(STATE_DIR="$STATE_DIR" PROJECT_ROOT="$PROJECT_ROOT" CURSOR_FILE="$CURSOR_FILE" python3 -c "$LOCK_PYTHON" 2>/dev/null || echo '{"verdict":"ok"}')"
LOCK_VERDICT="$(echo "$LOCK_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('verdict','ok'))" 2>/dev/null || echo "ok")"

# ---- [5/6] trace + debug log 摘要 ----
TRACE_PYTHON="$(cat <<'PYEOF'
import json, os
tf = os.environ.get('TRACE_FILE','')
dl = os.environ.get('DEBUG_LOG','')
trace_tail = []
debug_tail = []
debug_size = 0
if os.path.isfile(tf):
    try:
        with open(tf) as f:
            lines = f.readlines()
        trace_tail = [ln.strip()[:200] for ln in lines[-5:] if ln.strip()]
    except Exception:
        pass
if os.path.isfile(dl):
    try:
        debug_size = os.path.getsize(dl)
        with open(dl) as f:
            lines = f.readlines()
        for ln in lines[-10:]:
            try:
                obj = json.loads(ln)
                debug_tail.append({'ts': obj.get('ts',''), 'phase': obj.get('phase',''), 'details': obj.get('details',{})})
            except Exception:
                debug_tail.append({'raw': ln.strip()[:120]})
    except Exception:
        pass
verdict = 'warn' if (not trace_tail and not debug_tail) else 'ok'
print(json.dumps({'verdict': verdict, 'trace_tail': trace_tail, 'debug_tail': debug_tail, 'debug_log_bytes': debug_size}))
PYEOF
)"

TRACE_JSON="$(TRACE_FILE="$TRACE_FILE" DEBUG_LOG="$DEBUG_LOG" python3 -c "$TRACE_PYTHON" 2>/dev/null || echo '{"verdict":"ok"}')"
TRACE_VERDICT="$(echo "$TRACE_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('verdict','ok'))" 2>/dev/null || echo "ok")"
case "$TRACE_VERDICT" in
  warn) update_verdict 1 ;;
  fail) update_verdict 2 ;;
esac

# ---- [6/6] git worktree list ----
WT_PYTHON="$(cat <<'PYEOF'
import json, os, subprocess
pr = os.environ.get('PROJECT_ROOT','')
items = []
try:
    out = subprocess.check_output(['git','-C',pr,'worktree','list','--porcelain'], text=True, stderr=subprocess.DEVNULL)
    cur = {}
    for ln in out.splitlines():
        if ln.startswith('worktree '):
            if cur:
                items.append(cur); cur = {}
            cur['path'] = ln[len('worktree '):]
        elif ln.startswith('HEAD '):
            cur['head'] = ln[len('HEAD '):][:8]
        elif ln.startswith('branch '):
            cur['branch'] = ln[len('branch '):]
    if cur:
        items.append(cur)
except Exception:
    pass
ghosts = []
for it in items:
    p = it.get('path','')
    if p and not os.path.isdir(p):
        ghosts.append(p); it['ghost'] = True
    else:
        it['ghost'] = False
    br = it.get('branch','')
    it['is_loop_branch'] = br.startswith('refs/heads/loop/') or br.startswith('loop/')
verdict = 'warn' if ghosts else 'ok'
print(json.dumps({'verdict': verdict, 'items': items, 'ghosts': ghosts}))
PYEOF
)"

WT_JSON="$(PROJECT_ROOT="$PROJECT_ROOT" python3 -c "$WT_PYTHON" 2>/dev/null || echo '{"verdict":"ok","items":[]}')"
WT_VERDICT="$(echo "$WT_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('verdict','ok'))" 2>/dev/null || echo "ok")"
case "$WT_VERDICT" in
  warn) update_verdict 1 ;;
  fail) update_verdict 2 ;;
esac

# ---- 输出 ----
if [ "$JSON_MODE" -eq 1 ]; then
  HOOKS_JSON="$HOOKS_JSON" LINKS_JSON="$LINKS_JSON" STATE_JSON="$STATE_JSON" \
    LOCK_JSON="$LOCK_JSON" TRACE_JSON="$TRACE_JSON" WT_JSON="$WT_JSON" \
    PROJECT_ROOT="$PROJECT_ROOT" TS_NOW="$TS_NOW" VERDICT="$VERDICT" \
    PLAN="$PLAN" BASE_URL="${ANTHROPIC_BASE_URL:-}" \
    python3 -c "
import os, json
out = {
    'project_root': os.environ.get('PROJECT_ROOT',''),
    'ts': os.environ.get('TS_NOW',''),
    'plan': os.environ.get('PLAN',''),
    'base_url': os.environ.get('BASE_URL',''),
    'verdict_code': int(os.environ.get('VERDICT','0')),
    'sections': {
        'hooks': json.loads(os.environ.get('HOOKS_JSON','{}')),
        'links': json.loads(os.environ.get('LINKS_JSON','{}')),
        'state': json.loads(os.environ.get('STATE_JSON','{}')),
        'lock': json.loads(os.environ.get('LOCK_JSON','{}')),
        'trace': json.loads(os.environ.get('TRACE_JSON','{}')),
        'worktree': json.loads(os.environ.get('WT_JSON','{}')),
    },
}
print(json.dumps(out, ensure_ascii=False, indent=2))
"
  exit "$VERDICT"
fi

# 人读模式
echo "=== diagnose-stop-hook v0.1 ==="
echo "project_root: $PROJECT_ROOT"
echo "ts: $TS_NOW"
echo "plan: $PLAN base_url=${ANTHROPIC_BASE_URL:-unset}"
echo ""

verdict_icon() {
  case "$1" in
    ok) echo "✅ ok" ;;
    warn) echo "⚠️  warn" ;;
    fail) echo "❌ fail" ;;
    *) echo "? $1" ;;
  esac
}

# [1/6]
echo "[1/6] settings.json hook 注册 ............ $(verdict_icon "$HOOKS_VERDICT")"
echo "$HOOKS_JSON" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for it in d.get('items', []):
    icon = '✅' if it.get('present') else '❌'
    matcher = it.get('matcher') or '(no matcher)'
    extra = it.get('reason','')
    print(f'  {icon} {it.get(\"hook_type\",\"\"):<14} {it.get(\"script\",\"\"):<30} matcher={matcher}' + (f'  // {extra}' if extra else ''))
" 2>/dev/null || echo "  (parse failed)"
[ "$HOOKS_VERDICT" != "ok" ] && echo "  → 修复：cd <cc-builder-loop 仓> && bash install.sh"
echo ""

# [2/6]
echo "[2/6] 软链状态 ........................... $(verdict_icon "$LINKS_VERDICT")"
echo "$LINKS_JSON" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for it in d.get('items', []):
    if it.get('target_exists'):
        icon = '✅'
        target = it.get('target') or '(file)'
    else:
        icon = '❌'
        target = it.get('target') or '(missing)'
    print(f'  {icon} {it.get(\"path\",\"\"):<48} → {target}')
" 2>/dev/null || echo "  (parse failed)"
[ "$LINKS_VERDICT" != "ok" ] && echo "  → 修复：cd <cc-builder-loop 仓> && bash install.sh"
echo ""

# [3/6]
echo "[3/6] state 目录 .......................... $(verdict_icon "$STATE_VERDICT")"
echo "$STATE_JSON" | python3 -c "
import sys, json
d = json.load(sys.stdin)
items = d.get('items', [])
note = d.get('note','')
if note:
    print(f'  {note}')
elif not items:
    print('  state 目录存在但无 .yml 文件（无 active loop）')
for it in items:
    icon = '✅' if it.get('worktree_alive') or not it.get('worktree_path') else '⚠️'
    wt = it.get('worktree_path') or '(bare)'
    alive = '存活' if it.get('worktree_alive') else '已删/孤儿'
    print(f'  {icon} {it.get(\"file\",\"\"):<40} active={it.get(\"active\",\"?\"):<6} iter={it.get(\"iter\",\"?\"):<3} wt={wt} ({alive})')
" 2>/dev/null || echo "  (parse failed)"
echo ""

# [4/6]
echo "[4/6] lock / cursor / stash 状态 ......... $(verdict_icon "$LOCK_VERDICT")"
echo "$LOCK_JSON" | python3 -c "
import sys, json
d = json.load(sys.stdin)
locks = d.get('locks', [])
cur = d.get('cursor_content','')
head = d.get('head_short','')
match = d.get('cursor_matches_head', False)
stashes = d.get('stash_entries', [])
print(f'  - lock 文件：{len(locks)} 个 ' + (', '.join(l['file'] for l in locks) if locks else '(无)'))
print(f'  - last_processed_head: {cur or \"(未写入)\"} | 当前 HEAD: {head or \"?\"}', end='')
print(f' | {\"已 catch up\" if match else \"未匹配\"}' if cur and head else '')
print(f'  - builder-loop stash 副本：{len(stashes)} 个')
for s in stashes:
    print(f'      {s}')
" 2>/dev/null || echo "  (parse failed)"
echo ""

# [5/6]
echo "[5/6] 最近 trace + debug log 摘要 ........ $(verdict_icon "$TRACE_VERDICT")"
echo "$TRACE_JSON" | python3 -c "
import sys, json
d = json.load(sys.stdin)
trace = d.get('trace_tail', [])
debug = d.get('debug_tail', [])
size = d.get('debug_log_bytes', 0)
print(f'  loop-trace.jsonl 末 5 行：{\"(空)\" if not trace else \"\"}')
for ln in trace:
    print(f'    {ln}')
print(f'  stop-hook-debug.log 末 10 行（总 {size} 字节）：{\"(空 — V2.4 及之前未写入)\" if not debug else \"\"}')
for it in debug:
    if 'raw' in it:
        print(f'    {it[\"raw\"]}')
    else:
        det = json.dumps(it.get('details',{}), ensure_ascii=False)[:80]
        print(f'    [{it.get(\"ts\",\"\")[:19]}] {it.get(\"phase\",\"\"):<18} {det}')
" 2>/dev/null || echo "  (parse failed)"
[ "$TRACE_VERDICT" = "warn" ] && echo "  → 提示：debug log 为空可能 stop hook 从未在本仓触发，参 [1/6] 看 hook 注册"
echo ""

# [6/6]
echo "[6/6] git worktree list ................... $(verdict_icon "$WT_VERDICT")"
echo "$WT_JSON" | python3 -c "
import sys, json
d = json.load(sys.stdin)
items = d.get('items', [])
ghosts = d.get('ghosts', [])
for it in items:
    icon = '⚠️' if it.get('ghost') else ('🌿' if it.get('is_loop_branch') else '📁')
    print(f'  {icon} {it.get(\"path\",\"\"):<70} {it.get(\"head\",\"?\")} {it.get(\"branch\",\"?\")}')
if ghosts:
    print(f'  → 警告：{len(ghosts)} 个 ghost worktree（git 记录但物理目录不存在），考虑 git worktree prune')
" 2>/dev/null || echo "  (parse failed)"
echo ""

# 总结
echo "=========================================="
case "$VERDICT" in
  0) echo "✅ 总体：全 ok（$VERDICT）" ;;
  1) echo "⚠️  总体：至少一段 warn（$VERDICT），通常不阻断功能" ;;
  2) echo "❌ 总体：至少一段 fail（$VERDICT），stop hook 可能不工作！" ;;
esac

exit "$VERDICT"
