#!/usr/bin/env bash
# setup-builder-loop.sh — 启动 builder 自闭环
#
# 用法：bash setup-builder-loop.sh "<task description>"
#
# 行为：
#   1. 校验项目根 .claude/loop.yml 存在
#   2. 自动探测 layout（源码目录/测试目录），写回 loop.yml 缺省字段
#   3. （可选）EnterWorktree 进入隔离分支 — V1 先用 git worktree CLI
#   4. (V3.4 移除) 原 local.md 已取消，stop hook 用 locate-state.sh CWD 匹配
#   5. 提示用户「自闭环已启动，下一次 Stop 会自动跑 PASS_CMD」
#
# 输出：状态文件路径 + 后续提示
# 退出码：0=成功 / 1=配置缺失 / 2=worktree 失败 / 3=探测失败 / 6=孤儿 worktree 需用户决策

set -euo pipefail

PROJECT_ROOT="$(cd "$(pwd)" && pwd -P)"
# V5.3: worktree 内调用时追溯到主仓（.git 是文件 → worktree；是目录 → 主仓）
if [ -f "${PROJECT_ROOT}/.git" ]; then
  _common="$(git -C "$PROJECT_ROOT" rev-parse --git-common-dir 2>/dev/null || echo "")"
  if [ -n "$_common" ]; then
    _main="$(cd "$_common/.." && pwd -P 2>/dev/null || echo "")"
    if [ -n "$_main" ] && [ -f "${_main}/.claude/loop.yml" ]; then
      echo "[setup-builder-loop] ⚠️  当前在 worktree 内，已追溯到主仓：${_main}" >&2
      PROJECT_ROOT="$_main"
    fi
  fi
fi
LOOP_YML="${PROJECT_ROOT}/.claude/loop.yml"
STATE_DIR="${PROJECT_ROOT}/.claude/builder-loop/state"
LOG_DIR="${PROJECT_ROOT}/.claude/loop-runs"

# 解析 flags
FORCE_NO_WORKTREE=0
FORCE_NO_STASH=0
TOUCHED_FILES=""
REUSE_WORKTREE=""
IGNORE_ORPHANS=0
while [ $# -gt 0 ]; do
  case "${1:-}" in
    --no-worktree)     FORCE_NO_WORKTREE=1; shift ;;
    --no-stash)        FORCE_NO_STASH=1; shift ;;
    --touched-files)   TOUCHED_FILES="${2:-}"; shift; shift ;;
    --reuse-worktree)  REUSE_WORKTREE="${2:-}"; shift; shift ;;
    --ignore-orphans)  IGNORE_ORPHANS=1; shift ;;
    *) break ;;
  esac
done
TASK_DESC="${1:-untitled-task}"

# ---- flag 互斥校验 ----
if [ -n "$REUSE_WORKTREE" ] && [ "$FORCE_NO_WORKTREE" -eq 1 ]; then
  echo "❌ --reuse-worktree 和 --no-worktree 互斥，不能同时使用" >&2
  exit 2
fi

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
    git -C "$PROJECT_ROOT" check-ignore -q "$r" 2>/dev/null && continue
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

# ---- 幂等自愈 bgIsolation（V4.6+） ----
# CC 内置 EnterWorktree + bgIsolation 会与 builder-loop 的 git CLI worktree 管理冲突：
# base ref 不一致（CC 默认 origin/main vs builder-loop 用 HEAD）、state 不绑定、merge-back 链断裂。
# 项目级 settings.json 设 bgIsolation: "none" 关闭 CC 的 worktree 强制介入。
ensure_bg_isolation_none() {
  local proj_settings="${PROJECT_ROOT}/.claude/settings.json"
  # 无 settings.json → 创建最小文件
  if [ ! -f "$proj_settings" ]; then
    mkdir -p "${PROJECT_ROOT}/.claude"
    printf '{"bgIsolation":"none"}\n' > "$proj_settings"
    echo "[setup-builder-loop] 🛡️  已创建 .claude/settings.json 并设置 bgIsolation: none（防 CC 内置 worktree 干扰）" >&2
    return 0
  fi
  # 有 settings.json → 检查 bgIsolation 字段
  local current
  current="$(python3 -c "
import json
with open('$proj_settings') as f:
    d = json.load(f)
print(d.get('bgIsolation', ''))
" 2>/dev/null || echo "")"
  if [ "$current" = "none" ]; then
    return 0
  fi
  # 不是 "none" → 增量写入（保留其他字段）
  python3 -c "
import json
with open('$proj_settings') as f:
    d = json.load(f)
d['bgIsolation'] = 'none'
with open('$proj_settings', 'w') as f:
    json.dump(d, f, indent=2, ensure_ascii=False)
    f.write('\n')
" 2>/dev/null || true
  echo "[setup-builder-loop] 🛡️  已在 .claude/settings.json 设置 bgIsolation: none（防 CC 内置 worktree 干扰 builder-loop）" >&2
}
ensure_bg_isolation_none

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

# ---- plan_file 已移除（V3.6）----
# 方案文件路径由 builder 对话上下文持有（/planner 产出或用户指定），不再由 setup 启发式猜测。
# builder 直接 Read 方案全文（role 视图隔离已退役，见 CHANGELOG 范式变更节）。

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
WT_BASE_DIR="$(echo "$WT_CFG" | python3 -c "import sys,json; print(json.load(sys.stdin).get('base_dir','.claude/worktrees'))" 2>/dev/null || echo '.claude/worktrees')"
WT_PREFIX="$(echo "$WT_CFG" | python3 -c "import sys,json; print(json.load(sys.stdin).get('branch_prefix','loop/'))" 2>/dev/null || echo 'loop/')"

# ---- 计算 slug（state 文件名就是 slug）----
if [ -n "$REUSE_WORKTREE" ]; then
  # --reuse-worktree: 从已有 worktree 路径反推 slug（必须是绝对路径）
  case "$REUSE_WORKTREE" in
    /*) ;; # 绝对路径 OK
    *)
      echo "❌ --reuse-worktree 必须传绝对路径（孤儿检测输出的 ORPHAN 路径可直接复制）：$REUSE_WORKTREE" >&2
      exit 2
      ;;
  esac
  if [ ! -d "$REUSE_WORKTREE" ] || [ ! -f "$REUSE_WORKTREE/.git" ]; then
    echo "❌ --reuse-worktree 路径无效或不是 git worktree：$REUSE_WORKTREE" >&2
    exit 2
  fi
  WORKTREE_PATH="$(cd "$REUSE_WORKTREE" && pwd -P)"
  SLUG="$(basename "$WORKTREE_PATH")"
  WORKTREE_BRANCH="$(git -C "$WORKTREE_PATH" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "${WT_PREFIX}${SLUG}")"
  echo "[setup-builder-loop] ♻️  复用孤儿 worktree：${WORKTREE_PATH} (branch=${WORKTREE_BRANCH})" >&2
elif [ "$FORCE_NO_WORKTREE" -eq 0 ] && [ "$WT_ENABLED" = "True" ] && [ "$START_HEAD" != "no-git" ]; then
  TASK_SLUG="$(echo "$TASK_DESC" | head -c 24 | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-' | sed -E 's/-+/-/g; s/^-|-$//g')"
  [ -z "$TASK_SLUG" ] && TASK_SLUG="task"
  TASK_ID="$(date +%s)-${TASK_SLUG}"
  SLUG="$TASK_ID"
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

# ---- 孤儿 worktree 检测（V3.3: worktree 目录存在但无对应 active state）----
if [ -z "$REUSE_WORKTREE" ] && [ "$IGNORE_ORPHANS" -eq 0 ] && \
   [ "$FORCE_NO_WORKTREE" -eq 0 ] && [ "$WT_ENABLED" = "True" ] && [ "$START_HEAD" != "no-git" ]; then
  _wt_base="${PROJECT_ROOT}/${WT_BASE_DIR}"
  if [ -d "$_wt_base" ]; then
    _orphan_count=0
    for _odir in "$_wt_base"/*/; do
      [ -d "$_odir" ] || continue
      [ -f "${_odir}.git" ] || continue
      _odir_clean="${_odir%/}"
      _has_state=0
      for _sf in "$STATE_DIR"/*.yml; do
        [ -e "$_sf" ] || continue
        _sf_wt="$(grep -E '^worktree_path:' "$_sf" 2>/dev/null | head -1 | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || true)"
        if [ "$_sf_wt" = "$_odir_clean" ]; then
          _has_state=1
          break
        fi
      done
      if [ "$_has_state" -eq 0 ]; then
        if [ "$_orphan_count" -eq 0 ]; then
          echo "[setup-builder-loop] 🔍 发现孤儿 worktree（目录存在但无 active state）：" >&2
        fi
        _orphan_count=$(( _orphan_count + 1 ))
        _o_branch="$(git -C "$_odir_clean" rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'unknown')"
        _o_dirty="$(git -C "$_odir_clean" status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
        _main_head="$(git -C "$PROJECT_ROOT" rev-parse HEAD 2>/dev/null || echo '')"
        _o_ahead="0"
        if [ -n "$_main_head" ]; then
          _o_ahead="$(git -C "$_odir_clean" rev-list HEAD --not "$_main_head" 2>/dev/null | wc -l | tr -d ' ' || echo '?')"
        fi
        _o_last="$(git -C "$_odir_clean" log --oneline -1 2>/dev/null || echo 'no commits')"
        echo "  ORPHAN: ${_odir_clean}" >&2
        echo "    branch: ${_o_branch} | dirty: ${_o_dirty} files | ahead: ${_o_ahead} commits" >&2
        echo "    last: ${_o_last}" >&2
      fi
    done
    if [ "$_orphan_count" -gt 0 ]; then
      echo "" >&2
      echo "[setup-builder-loop] 💡 可选操作：" >&2
      echo "  复用：bash $0 --reuse-worktree <ORPHAN_PATH> \"<task>\"" >&2
      echo "  忽略（新建 worktree）：bash $0 --ignore-orphans \"<task>\"" >&2
      echo "  清理：git worktree remove --force <path> && git branch -D <branch>" >&2
      exit 6
    fi
  fi
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

# ---- V3.2: 默认干净 worktree + 可选 selective stash ----
# 默认：不 stash，worktree 从 HEAD 干净创建（V3.0 setup 在 builder 写代码前跑，dirty 必来自其他任务）
# --touched-files a,b,c → 只 stash 指定文件（builder 已在主仓编辑后中途接入 loop 时用）
# --no-stash → 显式跳过（V2.3 兼容，V3.2 等价于默认）
DIRTY_FILES=""
PRE_LOOP_STASH_REF=""
WORKTREE_MODE_DETECTED="clean"
if [ -z "$REUSE_WORKTREE" ] && [ "$FORCE_NO_WORKTREE" -eq 0 ] && [ "$WT_ENABLED" = "True" ] && \
   [ "$START_HEAD" != "no-git" ] && [ -n "$TOUCHED_FILES" ] && [ "$FORCE_NO_STASH" -eq 0 ]; then
  GIT_DIR_REL="$(git -C "$PROJECT_ROOT" rev-parse --git-dir 2>/dev/null || echo "")"
  if [ -n "$GIT_DIR_REL" ]; then
    if [[ "$GIT_DIR_REL" = /* ]]; then
      GIT_DIR_ABS="$GIT_DIR_REL"
    else
      GIT_DIR_ABS="${PROJECT_ROOT}/${GIT_DIR_REL}"
    fi
    for _special in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD; do
      if [ -e "${GIT_DIR_ABS}/${_special}" ]; then
        echo "❌ git 状态特殊（${_special} 存在），无法 stash" >&2
        exit 2
      fi
    done
    if [ -d "${GIT_DIR_ABS}/rebase-merge" ] || [ -d "${GIT_DIR_ABS}/rebase-apply" ]; then
      echo "❌ git rebase 进行中，无法 stash" >&2
      exit 2
    fi
  fi
  TOUCH_ARGS="$(echo "$TOUCHED_FILES" | tr ',' ' ')"
  STASH_MSG="builder-loop:auto:slug=${SLUG}:ts=$(date +%s)"
  if ! eval git -C '"$PROJECT_ROOT"' stash push -u -m '"$STASH_MSG"' -- $TOUCH_ARGS >/dev/null 2>&1; then
    echo "❌ git stash push --touched-files 失败" >&2
    exit 2
  fi
  PRE_LOOP_STASH_REF="$(git -C "$PROJECT_ROOT" rev-parse 'stash@{0}^{commit}' 2>/dev/null || true)"
  if [ -z "$PRE_LOOP_STASH_REF" ]; then
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
  DIRTY_FILES="$TOUCHED_FILES"
  WORKTREE_MODE_DETECTED="selective"
  echo "[setup-builder-loop] 📦 选择性 stash：${TOUCHED_FILES}" >&2
  echo "[setup-builder-loop]    hash=${PRE_LOOP_STASH_REF:0:12}" >&2
fi
if [ -z "$REUSE_WORKTREE" ] && [ "$FORCE_NO_WORKTREE" -eq 0 ] && [ "$WT_ENABLED" = "True" ] && \
   [ "$START_HEAD" != "no-git" ] && [ -z "$TOUCHED_FILES" ]; then
  _dirty_count="$(git -C "$PROJECT_ROOT" status --porcelain 2>/dev/null \
    | { grep -v -E '^.. \.claude/(builder-loop|loop-runs|worktrees)(/|$)' || true; } \
    | { grep -v -E '^.. \.gitignore$' || true; } \
    | wc -l)"
  _dirty_count="$(echo "$_dirty_count" | tr -d ' ')"
  if [ "${_dirty_count:-0}" -gt 0 ] 2>/dev/null; then
    echo "[setup-builder-loop] ⚠️  主仓有 ${_dirty_count} 个未提交文件，不带入 worktree。如需带入请传 --touched-files" >&2
  fi
fi

# ---- worktree 真接入（--reuse-worktree 跳过，worktree 已存在）----
if [ -z "$REUSE_WORKTREE" ] && [ "$FORCE_NO_WORKTREE" -eq 0 ] && [ "$WT_ENABLED" = "True" ] && [ "$START_HEAD" != "no-git" ]; then
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

# ---- stash apply（selective / dirty 模式时 apply 到 worktree）----
if [ -n "$PRE_LOOP_STASH_REF" ] && [ -n "$WORKTREE_PATH" ]; then
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
#   - worktree_mode          : clean | selective | bare | reuse
# Step 1 仅写默认值；Step 2 实施 dirty stash 时填非空值
if [ -n "$WORKTREE_PATH" ]; then
  RUNTIME_ROOT="$WORKTREE_PATH"
  if [ -n "$REUSE_WORKTREE" ]; then
    WORKTREE_MODE="reuse"
  else
    WORKTREE_MODE="${WORKTREE_MODE_DETECTED:-clean}"
  fi
else
  RUNTIME_ROOT="$PROJECT_ROOT"
  WORKTREE_MODE="bare"
fi
OWNER_CWD="$(pwd -P)"
if [ -n "$REUSE_WORKTREE" ]; then
  LAST_ITER_HEAD="$(git -C "$WORKTREE_PATH" rev-parse --short HEAD 2>/dev/null || echo "$START_HEAD")"
else
  LAST_ITER_HEAD="$START_HEAD"
fi
cat > "$STATE_FILE" <<EOF
# builder-loop state file (do NOT manually edit while loop is active)
active: true
phase: "active"
slug: "${SLUG}"
owner_cwd: "${OWNER_CWD}"
iter: 0
max_iter: 5
project_root: "${RUNTIME_ROOT}"
main_repo_path: "${PROJECT_ROOT}"
start_head: "${START_HEAD}"
last_iter_head: "${LAST_ITER_HEAD}"
worktree_path: "${WORKTREE_PATH}"
worktree_mode: "${WORKTREE_MODE}"
pre_loop_stash_ref: "${PRE_LOOP_STASH_REF}"
pre_loop_dirty_files: "${DIRTY_FILES}"
task_description: |
  ${TASK_DESC}
source_dirs: "${DETECTED_SRC}"
test_dirs: "${DETECTED_TEST}"
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
stopped_reason: ""
cleanup_phase: ""
created_at: "$(date -Iseconds)"
EOF

# V3.4: local.md 已移除。stop hook 通过 locate-state.sh CWD→state 匹配定位。

echo "✅ builder-loop 已启动"
if [ -n "$REUSE_WORKTREE" ]; then
  echo "   模式：worktree（♻️ 复用已有 — reviewer-as-gate）"
elif [ -n "$WORKTREE_PATH" ]; then
  echo "   模式：worktree（reviewer-as-gate — PASS 后挂牌等审，reviewer 通过才合主线）"
else
  echo "   模式：bare（事后审 — PASS 后立即 commit 主仓，reviewer 事后审查）"
fi
echo "   配置文件：${LOOP_YML}"
PASS_CNT=$(python3 -c "import yaml; print(len(yaml.safe_load(open('$LOOP_YML')).get('pass_cmd', [])))" 2>/dev/null || echo "?")
echo "   PASS_CMD 阶段数：${PASS_CNT}"
echo "   状态文件：${STATE_FILE}"
echo "   起始 HEAD：${START_HEAD}"
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

# V2.5: hook 注册 + 软链自检（缺失时 stderr 醒目警告）
# 触发：每次 setup 都跑（轻量 IO，python3 解析 JSON 一次 + 几个 stat）
# 目的：c1 根因 1 — settings.json hook 条目丢失 / 软链断 → stop hook 永不触发，但用户毫无察觉
# 不阻断 setup：缺则警告 + 给修复指引，state 仍正常创建
SETTINGS_JSON_V25="${HOME}/.claude/settings.json"
HOOK_REG_OK=1
LINK_OK=1
if [ -f "$SETTINGS_JSON_V25" ]; then
  if ! SJ="$SETTINGS_JSON_V25" python3 -c "
import json, os, sys
try:
    cfg = json.load(open(os.environ['SJ']))
    hooks = cfg.get('hooks', {})
    for entry in hooks.get('Stop', []):
        for h in entry.get('hooks', []):
            cmd = h.get('command', '')
            if 'builder-loop-stop.sh' in cmd:
                sys.exit(0)
    sys.exit(1)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
    HOOK_REG_OK=0
  fi
else
  HOOK_REG_OK=0
fi
[ ! -e "${HOME}/.claude/scripts/builder-loop-stop.sh" ] && LINK_OK=0
if [ "$HOOK_REG_OK" -eq 0 ] || [ "$LINK_OK" -eq 0 ]; then
  HOOK_STATUS="❌ missing"
  LINK_STATUS="❌ broken/missing"
  [ "$HOOK_REG_OK" -eq 1 ] && HOOK_STATUS="✅ ok"
  [ "$LINK_OK" -eq 1 ] && LINK_STATUS="✅ ok"
  cat >&2 <<HOOK_WARN

⚠️  V2.5 自检：builder-loop hook 配置异常，stop hook 可能不会触发！
   ① settings.json Stop hook 注册：${HOOK_STATUS}
   ② ~/.claude/scripts/builder-loop-stop.sh 软链：${LINK_STATUS}
   修复：cd <cc-builder-loop 仓> && bash install.sh
   排查：bash ~/.claude/skills/builder-loop/scripts/diagnose-stop-hook.sh
HOOK_WARN
fi

echo "提示：下次 Stop hook 触发时会自动跑 loop.yml.pass_cmd 验证。"
