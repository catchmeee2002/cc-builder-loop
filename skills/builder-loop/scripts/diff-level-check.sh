#!/usr/bin/env bash
# diff-level-check.sh — 扫 diff 中新增的函数/类/文件，输出 L3 信号供 builder 逐项回应
#
# 用法：bash diff-level-check.sh [project_root] [diff_base]
# 退出码：0=无 L3 信号  1=有 L3 信号需要 builder 回应

set -uo pipefail

PROJECT_ROOT="${1:-$(pwd)}"
DIFF_BASE="${2:-HEAD}"

if ! git -C "$PROJECT_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo '{"level_signals":[],"count":0}'
  exit 0
fi

SIGNALS=""
COUNT=0

add_signal() {
  local sig="$1"
  if [ -z "$SIGNALS" ]; then
    SIGNALS="\"$sig\""
  else
    SIGNALS="${SIGNALS},\"$sig\""
  fi
  COUNT=$((COUNT + 1))
}

# ---- 1. 新增文件（排除测试）----
NEW_FILES="$(git -C "$PROJECT_ROOT" diff "$DIFF_BASE" --diff-filter=A --name-only 2>/dev/null || true)"
if [ -n "$NEW_FILES" ]; then
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    case "$f" in
      test_*|tests/*|**/test_*|*_test.*|*.test.*) continue ;;
      *__init__*) continue ;;
    esac
    add_signal "new file: $f"
  done <<< "$NEW_FILES"
fi

# ---- 2. 新增的函数/类定义（排除测试文件）----
DIFF_OUTPUT="$(git -C "$PROJECT_ROOT" diff "$DIFF_BASE" -U0 2>/dev/null || true)"
if [ -n "$DIFF_OUTPUT" ]; then
  CURRENT_FILE=""
  while IFS= read -r line; do
    case "$line" in
      "diff --git a/"*)
        CURRENT_FILE="$(echo "$line" | sed 's|^diff --git a/.* b/||')"
        case "$CURRENT_FILE" in
          test_*|tests/*|**/test_*|*_test.*|*.test.*) CURRENT_FILE="" ;;
        esac
        ;;
      "+def "*)
        [ -z "$CURRENT_FILE" ] && continue
        FNAME="$(echo "$line" | sed -n 's/^+[[:space:]]*def \([a-zA-Z_][a-zA-Z0-9_]*\).*/\1/p')"
        [ -z "$FNAME" ] && continue
        [ "$FNAME" = "__init__" ] && continue
        [ "$FNAME" = "__str__" ] && continue
        [ "$FNAME" = "__repr__" ] && continue
        add_signal "def $FNAME ($CURRENT_FILE)"
        ;;
      "+class "*)
        [ -z "$CURRENT_FILE" ] && continue
        CNAME="$(echo "$line" | sed -n 's/^+[[:space:]]*class \([a-zA-Z_][a-zA-Z0-9_]*\).*/\1/p')"
        [ -z "$CNAME" ] && continue
        add_signal "class $CNAME ($CURRENT_FILE)"
        ;;
      "+function "*)
        [ -z "$CURRENT_FILE" ] && continue
        FNAME="$(echo "$line" | sed -n 's/^+[[:space:]]*function \([a-zA-Z_][a-zA-Z0-9_]*\).*/\1/p')"
        [ -z "$FNAME" ] && continue
        add_signal "function $FNAME ($CURRENT_FILE)"
        ;;
      "+"*"() {"*)
        [ -z "$CURRENT_FILE" ] && continue
        FNAME="$(echo "$line" | sed -n 's/^+\([a-zA-Z_][a-zA-Z0-9_]*\)()[[:space:]]*{.*/\1/p')"
        [ -z "$FNAME" ] && continue
        add_signal "$FNAME() ($CURRENT_FILE)"
        ;;
    esac
  done <<< "$DIFF_OUTPUT"
fi

# ---- 3. doc_freshness_check 三层检测（V5.9: specific 检查指令替代文件列表）----
CHANGED_FILES="$(git -C "$PROJECT_ROOT" diff "$DIFF_BASE" --name-only 2>/dev/null || true)"

DOC_CHECK_JSON="$(python3 -c "
import json, re, sys, os, subprocess

project_root = sys.argv[1]
changed_files_raw = sys.argv[2]

changed_files = [f for f in changed_files_raw.strip().splitlines() if f]
changed_basenames = [os.path.basename(f) for f in changed_files if f]

# --- machine_checks ---
_test_pat = re.compile(r'(^test_|/tests?/|/fixtures?/|_test\.|\.test\.)')
has_code_change = any(
    (f.endswith('.sh') or (f.startswith('agents/') and f.endswith('.md')))
    and not _test_pat.search(f)
    for f in changed_files
)
changelog_in_diff = 'CHANGELOG.md' in changed_files
changelog_needed = has_code_change and not changelog_in_diff

plan_version_stale = False
for p in ['docs/plan.md', 'plan.md']:
    pp = os.path.join(project_root, p)
    if os.path.isfile(pp):
        with open(pp) as f:
            for line in f:
                m = re.search(r'当前阶段[：:]\s*(V[\d.]+)', line)
                if m:
                    plan_ver = m.group(1)
                    cl = os.path.join(project_root, 'CHANGELOG.md')
                    if os.path.isfile(cl):
                        with open(cl) as cf:
                            for cl_line in cf:
                                cm = re.match(r'^## (V[\d.]+)', cl_line)
                                if cm:
                                    plan_version_stale = (plan_ver != cm.group(1))
                                    break
                    break
        break

machine_checks = {
    'changelog_needed': changelog_needed,
    'plan_version_stale': plan_version_stale,
}

# --- candidates ---
imp_path = os.path.join(project_root, 'improvements.md')
improvements_status = []
improvements_source = 'none'

if os.path.isfile(imp_path) and changed_basenames:
    improvements_source = 'local'
    with open(imp_path) as f:
        for line in f:
            if not line.startswith('## '):
                continue
            if '[观察期]' in line or '~~' in line or '已关闭' in line:
                continue
            title = line.strip().lstrip('# ').strip()
            date_stripped = re.sub(r'^\d{4}-\d{2}-\d{2}\s*', '', title)
            for bn in changed_basenames:
                stem = os.path.splitext(bn)[0]
                if bn in date_stripped or stem in date_stripped:
                    improvements_status.append(date_stripped)
                    break
elif changed_basenames:
    try:
        repo_url = subprocess.check_output(
            ['git', '-C', project_root, 'remote', 'get-url', 'origin'],
            stderr=subprocess.DEVNULL, text=True).strip()
        m_repo = re.search(r'[:/]([^/]+/[^/.]+?)(?:\.git)?$', repo_url)
        if m_repo:
            gh_out = subprocess.check_output(
                ['gh', 'issue', 'list', '--repo', m_repo.group(1),
                 '--state', 'open', '--json', 'title,labels', '-L', '100'],
                stderr=subprocess.DEVNULL, text=True, timeout=15)
            issues = json.loads(gh_out)
            improvements_source = 'github'
            for iss in issues:
                label_names = [l['name'] for l in iss.get('labels', [])]
                if 'observation' in label_names:
                    continue
                title = iss['title']
                date_stripped = re.sub(r'^\d{4}-\d{2}-\d{2}\s*', '', title)
                for bn in changed_basenames:
                    stem = os.path.splitext(bn)[0]
                    if bn in date_stripped or stem in date_stripped:
                        improvements_status.append(date_stripped)
                        break
    except Exception:
        pass

candidates = {'improvements_status': improvements_status}

# --- semantic_checks ---
semantic_checks = []
doc_list = ['CLAUDE.md', 'skills/builder-loop/SKILL.md', 'skills/builder-loop/README.md']
if changed_basenames:
    for doc in doc_list:
        doc_full = os.path.join(project_root, doc)
        if not os.path.isfile(doc_full):
            continue
        with open(doc_full) as f:
            content = f.read()
        for bn in changed_basenames:
            if bn in content:
                semantic_checks.append({
                    'file': doc,
                    'question': '本次改动的 ' + bn + ' 在 ' + doc + ' 中被引用，检查引用的行为描述是否仍准确',
                })
                break

result = {
    'machine_checks': machine_checks,
    'candidates': candidates,
    'semantic_checks': semantic_checks,
    'improvements_source': improvements_source,
}
print(json.dumps(result, ensure_ascii=False))
" "$PROJECT_ROOT" "$CHANGED_FILES" 2>/dev/null || echo '{"machine_checks":{"changelog_needed":false,"plan_version_stale":false},"candidates":{"improvements_status":[]},"semantic_checks":[],"improvements_source":"none"}')"

IMP_SRC="$(echo "$DOC_CHECK_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin).get('improvements_source','none'))" 2>/dev/null || echo "none")"
if [ "$IMP_SRC" = "none" ] && [ ! -f "$PROJECT_ROOT/improvements.md" ]; then
  echo "[diff-level-check] ⚠️  no improvements.md + gh unavailable, skipping improvements check" >&2
fi

echo "{\"level_signals\":[${SIGNALS}],\"count\":${COUNT},\"doc_freshness_check\":${DOC_CHECK_JSON}}"

if [ "$COUNT" -gt 0 ]; then
  exit 1
fi
exit 0
