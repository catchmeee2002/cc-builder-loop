#!/usr/bin/env bash
# handle-pass-result.sh — PASS_CMD 通过后的统一处理（commit + state write + e2e/reward-hack 检测）
#
# 用法：bash handle-pass-result.sh <state_file> <next_iter> <run_cwd> <project_root>
#
# 从 builder-loop-stop.sh PASS 路径提取（V5.4），供 stop hook 和 builder 共用。
#
# stdout（JSON，最后一行）：
#   {"type":"pass", "state_file":"...", "slug":"...", ...}       ← 正常 PASS，proceed to reviewer
#   {"type":"e2e_needed", "e2e_cases":"...", ...}                ← 需要 e2e 验证
#   {"type":"reward_hack", "file":"...", "keyword":"..."}        ← reward hacking 疑似
#   {"type":"commit_error", "detail":"..."}                      ← commit 失败
#
# exit code：
#   0 = pass handled (type=pass)
#   2 = e2e needed (type=e2e_needed)
#   3 = reward hacking detected (type=reward_hack)
#   4 = commit error (type=commit_error)

set -euo pipefail

STATE_FILE="${1:?state_file required}"
NEXT_ITER="${2:?next_iter required}"
RUN_CWD="${3:?run_cwd required}"
PROJECT_ROOT="${4:?project_root required}"

[ -f "$STATE_FILE" ] || { echo '{"type":"commit_error","detail":"state file not found"}'; exit 4; }

SKILL_DIR="${BASH_SOURCE[0]%/*}"

read_field() {
  grep -E "^${1}:" "$STATE_FILE" 2>/dev/null | head -1 | sed -E "s/^${1}:[[:space:]]*\"?([^\"]*)\"?[[:space:]]*\$/\1/" || echo ""
}

# ---- 1. Pre-read fields ----
PASS_START_HEAD="$(read_field start_head)"
PLAN_PATH="$(read_field plan_path)"
if [ -z "$PLAN_PATH" ]; then
  PLAN_PATH="$(read_field e2e_plan_path)"
fi

# ---- 2. E2E behavioral verification ----
E2E_CASES_PATH=""
E2E_LEVEL="full"
if [ -f "${PROJECT_ROOT}/.claude/loop.yml" ]; then
  E2E_CASES_PATH="$(grep -E '^e2e_cases_path:' "${PROJECT_ROOT}/.claude/loop.yml" 2>/dev/null | head -1 | sed -E 's/^e2e_cases_path:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || echo "")"
  _lvl="$(grep -E '^e2e_level:' "${PROJECT_ROOT}/.claude/loop.yml" 2>/dev/null | head -1 | sed -E 's/^e2e_level:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || echo "")"
  [ -n "$_lvl" ] && E2E_LEVEL="$_lvl"
fi

E2E_PLAN_PATH="$PLAN_PATH"
if [ -n "$E2E_PLAN_PATH" ]; then
  if [ "${E2E_PLAN_PATH#/}" = "$E2E_PLAN_PATH" ]; then
    E2E_PLAN_FULL="${PROJECT_ROOT}/${E2E_PLAN_PATH}"
  else
    E2E_PLAN_FULL="$E2E_PLAN_PATH"
  fi

  E2E_VERIFIED_HEAD="$(read_field e2e_verified_head)"
  CURRENT_HEAD="$(git -C "$RUN_CWD" rev-parse HEAD 2>/dev/null || echo "")"

  if [ "$E2E_VERIFIED_HEAD" != "$CURRENT_HEAD" ] && [ -f "$E2E_PLAN_FULL" ]; then
    EXTRACT_SCRIPT="${SKILL_DIR}/../../../scripts/extract-e2e-cases.sh"
    [ -f "$EXTRACT_SCRIPT" ] || EXTRACT_SCRIPT="$(dirname "${BASH_SOURCE[0]}")/../../scripts/extract-e2e-cases.sh"
    [ -f "$EXTRACT_SCRIPT" ] || EXTRACT_SCRIPT="$(cd "${SKILL_DIR}/../../.." 2>/dev/null && pwd)/scripts/extract-e2e-cases.sh"

    E2E_CASES=""
    if [ -f "$EXTRACT_SCRIPT" ]; then
      E2E_CASES="$(bash "$EXTRACT_SCRIPT" "$E2E_PLAN_FULL" 2>/dev/null || echo "")"
    fi

    if [ -n "$E2E_CASES" ]; then
      # Read tester identity for resume
      _TESTER_AID="$(STATE_FILE="$STATE_FILE" python3 -c "
import os, re
text = open(os.environ['STATE_FILE']).read()
m = re.search(r'^  tester:\n(?:    .*\n)*?    agent_id: \"([^\"]+)\"', text, re.M)
s = re.search(r'^  tester:\n(?:    .*\n)*?    status: \"([^\"]+)\"', text, re.M)
if m and s and s.group(1) == 'idle':
    print(m.group(1))
" 2>/dev/null || echo "")"

      # Write phase=e2e_pending
      STATE_FILE="$STATE_FILE" python3 -c "
import os, re
sf = os.environ['STATE_FILE']
text = open(sf).read()
text = re.sub(r'^phase:.*$', 'phase: \"e2e_pending\"', text, flags=re.M)
open(sf, 'w').write(text)
" 2>/dev/null || true

      # Output e2e request as JSON
      STATE_FILE="$STATE_FILE" RUN_CWD="$RUN_CWD" NEXT_ITER="$NEXT_ITER" \
        E2E_CASES_PATH="$E2E_CASES_PATH" E2E_LEVEL="$E2E_LEVEL" \
        E2E_CASES="$E2E_CASES" TESTER_AID="$_TESTER_AID" \
        CURRENT_HEAD="$CURRENT_HEAD" \
        python3 -c "
import os, json
print(json.dumps({
    'type': 'e2e_needed',
    'iter': int(os.environ.get('NEXT_ITER', '0') or 0),
    'state_file': os.environ['STATE_FILE'],
    'worktree_path': os.environ['RUN_CWD'],
    'e2e_cases_path': os.environ.get('E2E_CASES_PATH', ''),
    'e2e_level': os.environ.get('E2E_LEVEL', 'full'),
    'e2e_cases': os.environ.get('E2E_CASES', ''),
    'tester_agent_id': os.environ.get('TESTER_AID', ''),
    'current_head': os.environ.get('CURRENT_HEAD', ''),
}, ensure_ascii=False))
" 2>/dev/null
      exit 2
    fi
  fi
fi

# ---- 3. Reward hacking check ----
RH_DIFF="$(git -C "$RUN_CWD" diff "${PASS_START_HEAD}..HEAD" --name-only 2>/dev/null || echo "")"
RH_CONFIG_HIT=""
for rh_f in $RH_DIFF; do
  case "$rh_f" in
    *loop.yml|*pyproject.toml|*pytest.ini|*setup.cfg|*conftest.py|tests*/*.py|test_*) RH_CONFIG_HIT="$rh_f"; break ;;
  esac
done
if [ -n "$RH_CONFIG_HIT" ]; then
  RH_CONTENT="$(git -C "$RUN_CWD" diff "${PASS_START_HEAD}..HEAD" -- "$RH_CONFIG_HIT" 2>/dev/null || echo "")"
  RH_KEYWORD_HIT=""
  for rh_kw in '--reruns' '@pytest.mark.flaky' 'xfail' 'pytest.skip' 'pytest.mark.skip' '@unittest.skip' '.skipTest(' '-k "not'; do
    case "$RH_CONTENT" in
      *"$rh_kw"*) RH_KEYWORD_HIT="$rh_kw"; break ;;
    esac
  done
  if [ -n "$RH_KEYWORD_HIT" ]; then
    RH_CONFIG_HIT="$RH_CONFIG_HIT" RH_KEYWORD_HIT="$RH_KEYWORD_HIT" python3 -c "
import os, json
print(json.dumps({
    'type': 'reward_hack',
    'file': os.environ['RH_CONFIG_HIT'],
    'keyword': os.environ['RH_KEYWORD_HIT'],
}))
" 2>/dev/null
    exit 3
  fi
fi

# ---- 4. Commit ----
WORKTREE_PATH_PASS="$(read_field worktree_path)"
COMMIT_OUT="$(bash "${SKILL_DIR}/loop-commit.sh" "$STATE_FILE" 2>&1 || true)"
COMMIT_LAST="$(echo "$COMMIT_OUT" | tail -1)"
COMMIT_ACTION="$(echo "$COMMIT_LAST" | awk '{print $1}')"

case "$COMMIT_ACTION" in
  COMMIT_DONE|NOOP)
    NEW_HEAD_SHORT="$(echo "$COMMIT_LAST" | awk '{print $2}')"
    [ -z "$NEW_HEAD_SHORT" ] && NEW_HEAD_SHORT="$(git -C "$RUN_CWD" rev-parse --short HEAD 2>/dev/null || echo "")"
    SLUG="$(read_field slug)"
    DIFF_FILE="${PROJECT_ROOT}/.claude/reviewer-diff-${SLUG}.txt"
    PROJ_NAME="$(basename "$PROJECT_ROOT")"
    mkdir -p "${PROJECT_ROOT}/.claude/review_reports" 2>/dev/null || true
    REPORT_TS="$(date +%Y%m%d_%H%M%S)"
    REPORT_PATH="${PROJECT_ROOT}/.claude/review_reports/${PROJ_NAME}_${SLUG}_${REPORT_TS}.md"
    REVIEWER_FILES="$(git -C "$RUN_CWD" diff --name-only "${PASS_START_HEAD}..HEAD" 2>/dev/null | tr '\n' ',' | sed 's/,$//' || echo "")"
    git -C "$RUN_CWD" diff "${PASS_START_HEAD}..HEAD" > "$DIFF_FILE" 2>/dev/null || echo "" > "$DIFF_FILE"

    # ---- 5. Write state: phase + last_iter_head + reviewer_pending ----
    STATE_FILE="$STATE_FILE" NEW_HEAD="$NEW_HEAD_SHORT" \
      PASS_SH="$PASS_START_HEAD" RFILES="$REVIEWER_FILES" \
      DFILE="$DIFF_FILE" RPATH="$REPORT_PATH" \
      PLAN_PATH_V="$PLAN_PATH" \
      WAT="$(date -Iseconds 2>/dev/null || date +%s)" python3 - <<'PY' 2>/dev/null || true
import os, re
sf = os.environ['STATE_FILE']
text = open(sf).read()

def upsert(text, key, val):
    pat = re.compile(rf'^{key}:.*$', re.M)
    if pat.search(text):
        return pat.sub(f'{key}: {val}', text)
    if not text.endswith('\n'):
        text += '\n'
    return text + f'{key}: {val}\n'

text = upsert(text, 'phase', '"passed_pending_review"')
text = upsert(text, 'last_iter_head', f'"{os.environ["NEW_HEAD"]}"')

text = re.sub(r'^reviewer_pending:\n(?:  .+\n)*', '', text, flags=re.M)
if not text.endswith('\n'):
    text += '\n'
pending_block = (
    'reviewer_pending:\n'
    f'  pass_start_head: "{os.environ["PASS_SH"]}"\n'
    f'  reviewer_files: "{os.environ["RFILES"]}"\n'
    f'  diff_file: "{os.environ["DFILE"]}"\n'
    f'  report_path: "{os.environ["RPATH"]}"\n'
    f'  plan_path: "{os.environ.get("PLAN_PATH_V", "")}"\n'
    f'  written_at: "{os.environ["WAT"]}"\n'
)
text += pending_block
open(sf, 'w').write(text)
PY

    # Write processed cursor
    _head_sha="$(git -C "$PROJECT_ROOT" rev-parse HEAD 2>/dev/null || echo "")"
    if [ -n "$_head_sha" ]; then
      mkdir -p "${PROJECT_ROOT}/.claude/builder-loop" 2>/dev/null || true
      printf '%s\n' "$_head_sha" > "${PROJECT_ROOT}/.claude/builder-loop/last_processed_head" 2>/dev/null || true
    fi

    # ---- 6. Read reviewer identity for resume ----
    _REVIEWER_AID="$(STATE_FILE="$STATE_FILE" python3 -c "
import os, re
text = open(os.environ['STATE_FILE']).read()
m = re.search(r'^  reviewer:\n(?:    .*\n)*?    agent_id: \"([^\"]+)\"', text, re.M)
s = re.search(r'^  reviewer:\n(?:    .*\n)*?    status: \"([^\"]+)\"', text, re.M)
if m and s and s.group(1) == 'idle':
    print(m.group(1))
" 2>/dev/null || echo "")"

    # Output pass result as JSON
    STATE_FILE="$STATE_FILE" SLUG="$SLUG" RUN_CWD="$RUN_CWD" \
      WORKTREE_PATH_PASS="$WORKTREE_PATH_PASS" NEXT_ITER="$NEXT_ITER" \
      REVIEWER_AID="$_REVIEWER_AID" \
      python3 -c "
import os, json
print(json.dumps({
    'type': 'pass',
    'iter': int(os.environ.get('NEXT_ITER', '0') or 0),
    'state_file': os.environ['STATE_FILE'],
    'slug': os.environ.get('SLUG', ''),
    'worktree_path': os.environ.get('WORKTREE_PATH_PASS', ''),
    'reviewer_agent_id': os.environ.get('REVIEWER_AID', ''),
}, ensure_ascii=False))
" 2>/dev/null
    exit 0
    ;;
  *)
    COMMIT_LAST="$COMMIT_LAST" python3 -c "
import os, json
print(json.dumps({
    'type': 'commit_error',
    'detail': os.environ.get('COMMIT_LAST', 'unknown error'),
}))
" 2>/dev/null
    exit 4
    ;;
esac
