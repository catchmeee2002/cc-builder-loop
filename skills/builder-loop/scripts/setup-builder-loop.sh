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
STATE_DIR="${PROJECT_ROOT}/.claude/builder-loop/state"
LOG_DIR="${PROJECT_ROOT}/.claude/loop-runs"

# 解析 --no-worktree / --no-stash flag（V2.3）
#   --no-worktree: 跳过 worktree 创建（兜底激活 / 用户显式 bare）
#   --no-stash   : 主仓 dirty 时跳过 stash 流程，直接走 bare（V2.3）。
#                   仅 worktree 模式下有意义；--no-worktree 已 implicitly 等价于 no-stash
FORCE_NO_WORKTREE=0
FORCE_NO_STASH=0
while [ $# -gt 0 ]; do
  case "${1:-}" in
    --no-worktree) FORCE_NO_WORKTREE=1; shift ;;
    --no-stash)    FORCE_NO_STASH=1; shift ;;
    *) break ;;
  esac
done
TASK_DESC="${1:-untitled-task}"

# ---- 校验配置 ----
if [ ! -f "$LOOP_YML" ]; then
  echo "❌ 项目根缺少 .claude/loop.yml，无法启动自闭环。请先按 schema 创建配置。" >&2
  echo "   schema 路径：~/.claude/skills/builder-loop/schema/loop.schema.yml" >&2
  exit 1
fi

mkdir -p "$LOG_DIR" "$STATE_DIR"

# ---- 幂等自愈 .gitignore（V2.1+） ----
# 防 worktree merge ff 撞 telemetry/reviewer 中转文件 untracked（K1 教训预防）。
# 存量项目（接入时漏 V1.5/V1.6/V2.x 引入的规则）每次 setup 时自动补齐。
ensure_gitignore_rules() {
  local gi="${PROJECT_ROOT}/.gitignore"
  git -C "$PROJECT_ROOT" rev-parse --git-dir >/dev/null 2>&1 || return 0
  local rules=(
    ".claude/builder-loop/"
    ".claude/loop-runs/"
    ".claude/loop-trace.jsonl"
    ".claude/reviewer-params.json"
    ".claude/reviewer-diff.txt"
  )
  local added=0
  for r in "${rules[@]}"; do
    if [ ! -f "$gi" ] || ! grep -qFx "$r" "$gi" 2>/dev/null; then
      printf '%s\n' "$r" >> "$gi"
      echo "[setup-builder-loop] 🛡️  .gitignore 自愈追加：$r" >&2
      added=$(( added + 1 ))
    fi
  done
  if [ "$added" -gt 0 ]; then
    echo "[setup-builder-loop] 💡 共追加 $added 条规则到 .gitignore（防 worktree merge ff 撞 untracked telemetry）" >&2
  fi
}
ensure_gitignore_rules

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

# ---- 计算 slug（不管 worktree 模式如何都需要，state 文件名就是 slug）----
# worktree 模式：slug = <timestamp>-<task-slug>，天然 unique，并发安全
# bare 模式（--no-worktree 或 worktree.enabled=false）：slug = __main__，固定
if [ "$FORCE_NO_WORKTREE" -eq 0 ] && [ "$WT_ENABLED" = "True" ] && [ "$START_HEAD" != "no-git" ]; then
  TASK_SLUG="$(echo "$TASK_DESC" | head -c 24 | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-' | sed -E 's/-+/-/g; s/^-|-$//g')"
  [ -z "$TASK_SLUG" ] && TASK_SLUG="task"
  TASK_ID="$(date +%s)-${TASK_SLUG}"
  SLUG="$TASK_ID"
  WT_BASE_DIR="$(echo "$WT_CFG" | python3 -c "import sys,json; print(json.load(sys.stdin).get('base_dir','.claude/worktrees'))")"
  WT_PREFIX="$(echo "$WT_CFG" | python3 -c "import sys,json; print(json.load(sys.stdin).get('branch_prefix','loop/'))")"
  WORKTREE_BRANCH="${WT_PREFIX}${TASK_ID}"
  WORKTREE_PATH="${PROJECT_ROOT}/${WT_BASE_DIR}/${TASK_ID}"
else
  SLUG="__main__"
fi

STATE_FILE="${STATE_DIR}/${SLUG}.yml"

# ---- flock 保护 GC + 冲突检测 + 写入这段临界区 ----
# 防两个并发 setup（尤其 bare __main__）同时通过存在性检查后双写污染。
# 锁放 STATE_DIR 外层（dir 被 rm 时不拖累），进程退出 FD 自动释放。
LOCK_FILE="${STATE_DIR}/.setup.lock"
exec 9>"$LOCK_FILE" || { echo "❌ 无法打开 setup lock：$LOCK_FILE" >&2; exit 5; }
if ! flock -w 10 9; then
  echo "❌ 获取 setup lock 超时（10s），可能另一 setup 正在运行" >&2
  exit 5
fi

# ---- 懒 gc：扫描同目录下孤儿 state（worktree_path 已失效）----
# 只删同 STATE_DIR 下明确失效的（worktree_path 非空且目录不存在）
if [ -d "$STATE_DIR" ]; then
  for _sf in "$STATE_DIR"/*.yml; do
    [ -e "$_sf" ] || continue
    [ "$_sf" = "$STATE_FILE" ] && continue
    _wt="$(grep -E '^worktree_path:' "$_sf" 2>/dev/null | head -1 | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || true)"
    if [ -n "$_wt" ] && [ ! -d "$_wt" ]; then
      echo "[setup-builder-loop] 🧹 清理孤儿 state：$(basename "$_sf") (worktree_path=${_wt} 已不存在)" >&2
      rm -f "$_sf"
    fi
  done
fi

# ---- 若同 slug state 已 active 且对应 worktree 还在 → 拒绝 ----
if [ -f "$STATE_FILE" ]; then
  EXIST_ACTIVE="$(grep -E '^active:' "$STATE_FILE" | head -1 | awk '{print $2}')"
  EXIST_WT="$(grep -E '^worktree_path:' "$STATE_FILE" | head -1 | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/')"
  if [ "$EXIST_ACTIVE" = "true" ]; then
    # bare 模式（worktree_path 空）或 worktree 目录仍存在 → 拒绝
    if [ -z "$EXIST_WT" ] || [ -d "$EXIST_WT" ]; then
      echo "❌ 同 slug (${SLUG}) 已有 active loop，state=${STATE_FILE}" >&2
      echo "   若确认要清理，请手动 rm \"${STATE_FILE}\" 再重试" >&2
      exit 4
    fi
    # worktree 已失效 → 落到本次覆盖
    echo "[setup-builder-loop] ⚠️  同 slug 旧 state 的 worktree 已失效，覆盖写入" >&2
  fi
fi

# ---- V2.3: 主仓 dirty 检测 + stash 副本（worktree 模式专用）----
# 决定 WORKTREE_MODE_DETECTED；clean→走原 worktree 路径；dirty→stash 后再 worktree apply
# bare（FORCE_NO_WORKTREE / WT_ENABLED=False）以及 --no-stash 直接跳过本段
DIRTY_FILES=""
PRE_LOOP_STASH_REF=""
WORKTREE_MODE_DETECTED="clean"  # 默认；后面根据 pre-flight 调整
if [ "$FORCE_NO_WORKTREE" -eq 0 ] && [ "$WT_ENABLED" = "True" ] && \
   [ "$START_HEAD" != "no-git" ] && [ "$FORCE_NO_STASH" -eq 0 ]; then
  # pre-flight: 检测 git 特殊状态（rebase/merge/cherry-pick/revert）
  GIT_DIR_REL="$(git -C "$PROJECT_ROOT" rev-parse --git-dir 2>/dev/null || echo "")"
  if [ -n "$GIT_DIR_REL" ]; then
    if [[ "$GIT_DIR_REL" = /* ]]; then
      GIT_DIR_ABS="$GIT_DIR_REL"
    else
      GIT_DIR_ABS="${PROJECT_ROOT}/${GIT_DIR_REL}"
    fi
    for _special in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD; do
      if [ -e "${GIT_DIR_ABS}/${_special}" ]; then
        echo "❌ git 状态特殊（${_special} 存在），无法 dirty stash + worktree" >&2
        echo "   请先结束 merge/cherry-pick/revert，或加 --no-stash 跳过 stash 流程" >&2
        exit 2
      fi
    done
    if [ -d "${GIT_DIR_ABS}/rebase-merge" ] || [ -d "${GIT_DIR_ABS}/rebase-apply" ]; then
      echo "❌ git rebase 进行中，无法 dirty stash + worktree" >&2
      echo "   请 git rebase --abort 或 --continue 后重试，或加 --no-stash 跳过" >&2
      exit 2
    fi
    # submodule dirty 检测（本期不支持）
    SUBMOD_DIRTY="$(git -C "$PROJECT_ROOT" submodule foreach --quiet 'git status --porcelain 2>/dev/null | head -1' 2>/dev/null || true)"
    if [ -n "$SUBMOD_DIRTY" ]; then
      echo "❌ submodule 含未提交改动，本期 dirty stash 暂不支持" >&2
      echo "   请 git submodule foreach git stash 后重试，或加 --no-stash 跳过" >&2
      exit 2
    fi
    # 收集 dirty 文件清单（含 untracked，逗号分隔）
    # V2.3: 排除 setup 自管理 / 不应进 stash 的路径：
    #   .claude/builder-loop/ — setup 自身的 state/lock/log
    #   .claude/loop-runs/ / .claude/loop-trace.jsonl — telemetry
    #   .claude/worktrees/ — git worktree add 创建（setup 副产品）
    #   .gitignore — V2.1.1 ensure_gitignore_rules 自愈写入；进 stash 会让 worktree 内缺规则撞 telemetry
    DIRTY_FILES="$(git -C "$PROJECT_ROOT" status --porcelain 2>/dev/null \
      | awk 'NF{print $NF}' \
      | grep -v -E '^\.claude/builder-loop(/|$)' \
      | grep -v -E '^\.claude/loop-runs(/|$)' \
      | grep -v -E '^\.claude/loop-trace\.jsonl$' \
      | grep -v -E '^\.claude/worktrees(/|$)' \
      | grep -v -E '^\.gitignore$' \
      | tr '\n' ',' | sed 's/,$//' || true)"
  fi
  # dirty 非空 → 创建 stash 副本（commit hash 形式，多 builder 安全）
  if [ -n "$DIRTY_FILES" ]; then
    STASH_MSG="builder-loop:auto:slug=${SLUG}:ts=$(date +%s)"
    # 用 git stash push -u -m 一步完成 create + store + working tree cleanup
    # 注：git stash create -u 在"仅 untracked 无 tracked 改动"场景会返回空字符串，必须用 push
    if ! git -C "$PROJECT_ROOT" stash push -u -m "$STASH_MSG" >/dev/null 2>&1; then
      echo "❌ git stash push 失败（dirty 但 stash 创建不成功）" >&2
      echo "   请人工 git stash 后再调用，或加 --no-stash 跳过" >&2
      exit 2
    fi
    # 立即拿 commit hash（^{commit} 解析；多 builder race 时用 message 兜底匹配）
    PRE_LOOP_STASH_REF="$(git -C "$PROJECT_ROOT" rev-parse 'stash@{0}^{commit}' 2>/dev/null || true)"
    if [ -z "$PRE_LOOP_STASH_REF" ]; then
      # message 兜底：从 stash list 找匹配 STASH_MSG 的条目
      _stash_idx="$(git -C "$PROJECT_ROOT" stash list 2>/dev/null | grep -F "$STASH_MSG" | head -1 | grep -oE 'stash@\{[0-9]+\}' | head -1 || true)"
      if [ -n "$_stash_idx" ]; then
        PRE_LOOP_STASH_REF="$(git -C "$PROJECT_ROOT" rev-parse "${_stash_idx}^{commit}" 2>/dev/null || true)"
      fi
    fi
    if [ -z "$PRE_LOOP_STASH_REF" ]; then
      echo "❌ stash 已创建但拿不到 commit hash，回滚" >&2
      git -C "$PROJECT_ROOT" stash pop 2>/dev/null || true
      exit 2
    fi
    WORKTREE_MODE_DETECTED="dirty"
    echo "[setup-builder-loop] 📦 主仓 dirty 已 stash → ${STASH_MSG}" >&2
    echo "[setup-builder-loop]    hash=${PRE_LOOP_STASH_REF:0:12}, 主仓 working tree 已 clean" >&2
    echo "[setup-builder-loop]    dirty 改动将在 worktree 内以 unstaged 形态可见" >&2
  fi
fi

# ---- worktree 真接入 ----
if [ "$FORCE_NO_WORKTREE" -eq 0 ] && [ "$WT_ENABLED" = "True" ] && [ "$START_HEAD" != "no-git" ]; then
  mkdir -p "${PROJECT_ROOT}/${WT_BASE_DIR}"
  if ! git -C "$PROJECT_ROOT" worktree add -b "$WORKTREE_BRANCH" "$WORKTREE_PATH" HEAD >&2; then
    echo "❌ git worktree add 失败，worktree_path=${WORKTREE_PATH} branch=${WORKTREE_BRANCH}" >&2
    rm -rf "$WORKTREE_PATH" 2>/dev/null || true
    # 主仓 stash 已写入但 worktree 失败 → 回滚主仓还原 dirty
    if [ -n "$PRE_LOOP_STASH_REF" ]; then
      git -C "$PROJECT_ROOT" stash apply "$PRE_LOOP_STASH_REF" 2>/dev/null || true
      echo "[setup-builder-loop] ↩️  主仓 dirty 已从 stash 恢复（stash 副本仍保留）" >&2
    fi
    exit 2
  fi
  echo "[setup-builder-loop] 🌿 worktree 已创建：${WORKTREE_PATH} (branch=${WORKTREE_BRANCH})" >&2
fi

# ---- V2.3: dirty 模式 → 在 worktree 内 apply stash（不 pop，副本保留）----
if [ "$WORKTREE_MODE_DETECTED" = "dirty" ] && [ -n "$PRE_LOOP_STASH_REF" ] && [ -n "$WORKTREE_PATH" ]; then
  if ! git -C "$WORKTREE_PATH" stash apply "$PRE_LOOP_STASH_REF" >&2; then
    echo "❌ git stash apply 在 worktree 失败，回滚 worktree + 还原主仓" >&2
    # 回滚 worktree
    git -C "$PROJECT_ROOT" worktree remove --force "$WORKTREE_PATH" 2>/dev/null || rm -rf "$WORKTREE_PATH" 2>/dev/null || true
    git -C "$PROJECT_ROOT" branch -D "$WORKTREE_BRANCH" 2>/dev/null || true
    # 主仓恢复 dirty（apply 副本，不动 stash list）
    git -C "$PROJECT_ROOT" stash apply "$PRE_LOOP_STASH_REF" 2>/dev/null || true
    exit 2
  fi
  echo "[setup-builder-loop] ✅ stash 已 apply 到 worktree（unstaged 形态可见，副本仍在 stash list）" >&2
fi

# ---- 写状态文件 ----
# V2.0 schema：project_root 语义改为"干活的地方"
#   - worktree 启用 → project_root = worktree_path（PASS_CMD 在此跑，loop.yml 从此读）
#   - bare 模式    → project_root = main_repo_path = 主仓
# main_repo_path 永远是主仓（git merge / worktree prune / reviewer-params 写入位置）
# 老 V1.x 兼容：缺 main_repo_path 字段时，下游脚本把 project_root 当主仓使用
#
# V2.3 schema 新增三字段：
#   - pre_loop_stash_ref     : git stash commit hash（worktree-dirty 模式才填，否则空）
#   - pre_loop_dirty_files   : 进 stash 的文件列表（逗号分隔，merge commit message 用）
#   - worktree_mode          : clean | dirty | bare（语义清晰化）
# Step 1 仅写默认值；Step 2 实施 dirty stash 时填非空值
if [ -n "$WORKTREE_PATH" ]; then
  RUNTIME_ROOT="$WORKTREE_PATH"
  # V2.3: WORKTREE_MODE_DETECTED 由前置 pre-flight 决定（clean / dirty）
  # 注：用户显式 --no-stash 时 WORKTREE_MODE_DETECTED 保持 "clean"，dirty 留主仓不进 stash
  #     这种"看似 clean 实则用户主仓 dirty"的语义对维护者可能 confusing；EARLY_STOP 路径
  #     按 worktree_mode=clean 不还原 stash 也是预期（无 stash 可还原）
  WORKTREE_MODE="${WORKTREE_MODE_DETECTED:-clean}"
else
  RUNTIME_ROOT="$PROJECT_ROOT"
  WORKTREE_MODE="bare"
fi
OWNER_CWD="$(pwd -P)"
cat > "$STATE_FILE" <<EOF
# builder-loop state file (do NOT manually edit while loop is active)
active: true
slug: "${SLUG}"
owner_cwd: "${OWNER_CWD}"
iter: 0
max_iter: 5
project_root: "${RUNTIME_ROOT}"
main_repo_path: "${PROJECT_ROOT}"
start_head: "${START_HEAD}"
worktree_path: "${WORKTREE_PATH}"
worktree_mode: "${WORKTREE_MODE}"
pre_loop_stash_ref: "${PRE_LOOP_STASH_REF}"
pre_loop_dirty_files: "${DIRTY_FILES}"
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

# V2.4: 检测 setup 调用 cwd 与 worktree path 不一致 → 醒目警告
# 触发：worktree 模式 AND OWNER_CWD = 主仓（CC session cwd 仍在主仓）
# 后果：stop hook 直接定位 state 时主仓 cwd 不在 worktrees 子目录 / 不等于 worktree_path
#       → 策略 2/3/4 全 miss，需靠 V2.4 策略 5（唯一 active worktree 自动绑定）兜底
# 多 active 场景策略 5 也不绑，必须 cd 到对应 worktree
if [ -n "$WORKTREE_PATH" ] && [ "$OWNER_CWD" = "$PROJECT_ROOT" ]; then
  cat >&2 <<WARN
⚠️  CC session cwd 仍在主仓：${OWNER_CWD}
   stop hook 触发时不能直接定位本 worktree state（V2.4 策略 5 仅在唯一 active worktree
   时自动绑定；多 active 必须显式 cd 到对应 worktree 才能让 stop hook 跟踪）。
   建议下一步：
     1. 若本 session 还要继续：cd ${WORKTREE_PATH}
     2. 若并发多 worktree：在新 CC session 用 --cwd ${WORKTREE_PATH} 启动
WARN
fi

echo "提示：下次 Stop hook 触发时会自动跑 loop.yml.pass_cmd 验证。"
