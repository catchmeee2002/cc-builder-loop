#!/usr/bin/env bash
# setup-builder-loop.sh — 启动 builder 自闭环
#
# 用法：bash setup-builder-loop.sh "<task description>"
#
# 行为：
#   1. 校验项目根 .claude/loop.yml 存在
#   2. 自动探测 layout（源码目录/测试目录），写回 loop.yml 缺省字段
#   3. （可选）EnterWorktree 进入隔离分支 — V1 先用 git worktree CLI
#   4. 生成 .claude/builder-loop.local.md 状态文件
#   5. 提示用户「自闭环已启动，下一次 Stop 会自动跑 PASS_CMD」
#
# 输出：状态文件路径 + 后续提示
# 退出码：0=成功 / 1=配置缺失 / 2=worktree 失败 / 3=探测失败

set -euo pipefail

PROJECT_ROOT="$(cd "$(pwd)" && pwd -P)"
LOOP_YML="${PROJECT_ROOT}/.claude/loop.yml"
STATE_FILE="${PROJECT_ROOT}/.claude/builder-loop.local.md"
LOG_DIR="${PROJECT_ROOT}/.claude/loop-runs"

# 解析 --no-worktree flag（兜底激活时使用，跳过 worktree 创建）
FORCE_NO_WORKTREE=0
if [ "${1:-}" = "--no-worktree" ]; then
  FORCE_NO_WORKTREE=1
  shift
fi
TASK_DESC="${1:-untitled-task}"

# ---- 校验配置 ----
if [ ! -f "$LOOP_YML" ]; then
  echo "❌ 项目根缺少 .claude/loop.yml，无法启动自闭环。请先按 schema 创建配置。" >&2
  echo "   schema 路径：~/.claude/skills/builder-loop/schema/loop.schema.yml" >&2
  exit 1
fi

mkdir -p "$LOG_DIR"

# ---- 优先读 loop.yml 的 layout 字段，fallback 到自动探测 ----
LAYOUT_JSON="$(python3 -c "
import yaml, json
cfg = yaml.safe_load(open('$LOOP_YML')) or {}
layout = cfg.get('layout', {})
print(json.dumps({
    'source_dirs': layout.get('source_dirs', []),
    'test_dirs': layout.get('test_dirs', [])
}))
" 2>/dev/null || echo '{"source_dirs":[],"test_dirs":[]}')"

CONFIGURED_SRC="$(echo "$LAYOUT_JSON" | python3 -c "import sys,json; print(','.join(json.load(sys.stdin).get('source_dirs',[])))" 2>/dev/null || echo "")"
CONFIGURED_TEST="$(echo "$LAYOUT_JSON" | python3 -c "import sys,json; print(','.join(json.load(sys.stdin).get('test_dirs',[])))" 2>/dev/null || echo "")"

# 自动探测 fallback（仅当 layout 未配置时）
detect_dirs() {
  local kind="$1"  # source | test
  case "$kind" in
    source)
      for d in src lib app pkg; do [ -d "$PROJECT_ROOT/$d" ] && echo "$d"; done
      ;;
    test)
      for d in tests test spec __tests__ t; do [ -d "$PROJECT_ROOT/$d" ] && echo "$d"; done
      ;;
  esac
  # 关键：所有 [ -d ... ] 都不命中时返回 1 + pipefail + set -e 会提前杀进程
  # POC 实跑发现：空仓（无 src/lib/app/pkg）触发该问题，必须显式 return 0
  return 0
}

DETECTED_SRC="${CONFIGURED_SRC:-$(detect_dirs source | tr '\n' ',' | sed 's/,$//')}"
DETECTED_TEST="${CONFIGURED_TEST:-$(detect_dirs test | tr '\n' ',' | sed 's/,$//')}"

# ---- 起始 HEAD ----
START_HEAD="$(git -C "$PROJECT_ROOT" rev-parse --short HEAD 2>/dev/null || echo 'no-git')"

# ---- 自动找当前任务对应的方案文件（最近修改的 .claude/plans/*.md）----
# 用于 split-plan-by-role 过滤（builder 自身/spawn reviewer/spawn tester 时按 role 拿对应视图）
PLAN_DIR="${PROJECT_ROOT}/.claude/plans"
PLAN_FILE=""
if [ -d "$PLAN_DIR" ]; then
  # shellcheck disable=SC2012  # ls -t 用 mtime 排序，find -printf 不便携，文件名约定 .md 不含特殊字符
  PLAN_FILE="$(ls -1t "$PLAN_DIR"/*.md 2>/dev/null | head -n 1 || echo '')"
fi

# ---- worktree 真接入（V1.1 T2.2）----
# 读 loop.yml.worktree.enabled（缺省 false）→ true 则 git worktree add
# 失败 exit 2；向后兼容老配置（boolean 旧写法 "worktree: true" 亦视为 enabled=true）
WORKTREE_PATH=""
WORKTREE_BRANCH=""
WT_CFG="$(python3 - <<PY 2>/dev/null || true
import yaml, json, sys
try:
    cfg = yaml.safe_load(open("$LOOP_YML")) or {}
    wt = cfg.get("worktree", {})
    if isinstance(wt, bool):
        wt = {"enabled": wt}
    print(json.dumps({
        "enabled": bool(wt.get("enabled", False)),
        "base_dir": wt.get("base_dir", ".claude/worktrees"),
        "branch_prefix": wt.get("branch_prefix", "loop/"),
    }))
except Exception:
    print('{"enabled": false}')
PY
)"
WT_ENABLED="$(echo "$WT_CFG" | python3 -c "import sys,json; print(json.load(sys.stdin).get('enabled', False))" 2>/dev/null || echo "False")"

if [ "$FORCE_NO_WORKTREE" -eq 0 ] && [ "$WT_ENABLED" = "True" ] && [ "$START_HEAD" != "no-git" ]; then
  WT_BASE_DIR="$(echo "$WT_CFG" | python3 -c "import sys,json; print(json.load(sys.stdin).get('base_dir','.claude/worktrees'))")"
  WT_PREFIX="$(echo "$WT_CFG" | python3 -c "import sys,json; print(json.load(sys.stdin).get('branch_prefix','loop/'))")"
  TASK_SLUG="$(echo "$TASK_DESC" | head -c 24 | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-' | sed -E 's/-+/-/g; s/^-|-$//g')"
  [ -z "$TASK_SLUG" ] && TASK_SLUG="task"
  TASK_ID="$(date +%s)-${TASK_SLUG}"
  WORKTREE_BRANCH="${WT_PREFIX}${TASK_ID}"
  WORKTREE_PATH="${PROJECT_ROOT}/${WT_BASE_DIR}/${TASK_ID}"
  mkdir -p "${PROJECT_ROOT}/${WT_BASE_DIR}"
  if ! git -C "$PROJECT_ROOT" worktree add -b "$WORKTREE_BRANCH" "$WORKTREE_PATH" HEAD >&2; then
    echo "❌ git worktree add 失败，worktree_path=${WORKTREE_PATH} branch=${WORKTREE_BRANCH}" >&2
    rm -rf "$WORKTREE_PATH" 2>/dev/null || true
    exit 2
  fi
  echo "[setup-builder-loop] 🌿 worktree 已创建：${WORKTREE_PATH} (branch=${WORKTREE_BRANCH})" >&2
fi

# ---- 写状态文件 ----
# 注意：layout 字段拍平为顶层（source_dirs / test_dirs），方便 early-stop-check.sh
# 用 grep '^test_dirs:' 直接匹配；不用嵌套结构。
cat > "$STATE_FILE" <<EOF
# builder-loop state file (do NOT manually edit while loop is active)
active: true
iter: 0
max_iter: 5
project_root: "${PROJECT_ROOT}"
start_head: "${START_HEAD}"
worktree_path: "${WORKTREE_PATH}"
plan_file: "${PLAN_FILE}"
task_description: |
  ${TASK_DESC}
source_dirs: "${DETECTED_SRC}"
test_dirs: "${DETECTED_TEST}"
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
stopped_reason: ""
created_at: "$(date -Iseconds)"
EOF

echo "✅ builder-loop 已启动"
echo "   配置文件：${LOOP_YML}"
PASS_CNT=$(python3 -c "import yaml; print(len(yaml.safe_load(open('$LOOP_YML')).get('pass_cmd', [])))" 2>/dev/null || echo "?")
echo "   PASS_CMD 阶段数：${PASS_CNT}"
echo "   状态文件：${STATE_FILE}"
echo "   起始 HEAD：${START_HEAD}"
echo "   方案文件：${PLAN_FILE:-<未找到 .claude/plans/*.md>}"
echo "   探测 source_dirs：${DETECTED_SRC:-<空>}"
echo "   探测 test_dirs：${DETECTED_TEST:-<空>}"
echo ""
echo "提示：下次 Stop hook 触发时会自动跑 loop.yml.pass_cmd 验证。"
