#!/usr/bin/env bash
# merge-and-cleanup.sh — V3.0 reviewer-as-gate：reviewer 通过后 ff merge + cleanup
#
# 用法：bash merge-and-cleanup.sh <state_file>
#
# 设计：
#   - 由 builder 在 reviewer 通过后主动调（不再由 hook 自动调）
#   - 幂等：state.cleanup_phase 字段记进度，中断后重跑可续
#       cleanup_phase=""               → 跑完整流程：ff merge → drop stash → worktree remove → rm state
#       cleanup_phase="ff_merged"      → 跳过 ff merge，做 drop stash + worktree remove + rm state
#       cleanup_phase="worktree_removed" → 仅 drop stash + rm state
#
# 输出（stdout 最后一行为决定性结果）：
#   MERGED <branch>                    ← 全部完成
#   NEED_ARBITRATION <worktree_path>   ← rebase 冲突，state 写 need_arbitration: true
#   ERROR <reason>                     ← 失败（exit 3）
#
# 退出码：0=MERGED  1=NEED_ARBITRATION  3=ERROR
#
# 依赖 state 字段：main_repo_path / worktree_path / start_head / slug / cleanup_phase

set -euo pipefail

STATE="${1:?state file path required}"
[ -f "$STATE" ] || { echo "ERROR state-not-found"; exit 3; }

read_field() {
  grep -E "^${1}:" "$STATE" 2>/dev/null | head -1 | sed -E "s/^${1}:[[:space:]]*\"?([^\"]*)\"?[[:space:]]*\$/\1/; s/^'(.*)'$/\1/" || true
}

PROJECT_ROOT="$(read_field main_repo_path)"
[ -z "$PROJECT_ROOT" ] && PROJECT_ROOT="$(read_field project_root)"
WORKTREE_PATH="$(read_field worktree_path)"
START_HEAD="$(read_field start_head)"
SLUG_FIELD="$(read_field slug)"
CLEANUP_PHASE="$(read_field cleanup_phase)"

if [ -z "$PROJECT_ROOT" ] || [ ! -d "$PROJECT_ROOT" ]; then
  echo "ERROR project-root-missing"
  exit 3
fi

# 写 cleanup_phase 字段（幂等，has-then-replace / append）
write_cleanup_phase() {
  local phase="$1"
  STATE="$STATE" PHASE_VAL="$phase" python3 - <<'PY'
import os, re
sf = os.environ['STATE']
val = os.environ['PHASE_VAL']
text = open(sf).read()
if re.search(r'^cleanup_phase:', text, re.M):
    text = re.sub(r'^cleanup_phase:.*$', f'cleanup_phase: "{val}"', text, flags=re.M)
else:
    text += f'\ncleanup_phase: "{val}"\n'
open(sf, 'w').write(text)
PY
}

# V2.3 stash 副本管理（同 merge-worktree-back.sh）
drop_pre_loop_stash() {
  [ -z "$SLUG_FIELD" ] && return 0
  local sig="builder-loop:auto:slug=${SLUG_FIELD}:"
  local idx
  idx="$(git -C "$PROJECT_ROOT" stash list 2>/dev/null | grep -F "$sig" | head -1 | awk -F: '{print $1}' || true)"
  if [ -n "$idx" ]; then
    git -C "$PROJECT_ROOT" stash drop "$idx" 2>/dev/null || \
      echo "[merge-and-cleanup] ⚠️  stash drop 失败（${idx}），需手动 git stash drop" >&2
  fi
}

warn_stash_residual() {
  [ -z "$SLUG_FIELD" ] && return 0
  local sig="builder-loop:auto:slug=${SLUG_FIELD}:"
  local idx
  idx="$(git -C "$PROJECT_ROOT" stash list 2>/dev/null | grep -F "$sig" | head -1 | awk -F: '{print $1}' || true)"
  if [ -n "$idx" ]; then
    echo "[merge-and-cleanup] ⚠️  主仓 dirty stash 副本仍在 stash list：${idx}" >&2
    echo "                       签名：${sig}" >&2
    echo "                       本路径不自动 drop（PASS 路径才 drop）；用户人工清理：git stash drop ${idx}" >&2
  fi
}

# 标记仲裁（路径 C 用），同 merge-worktree-back.sh
mark_arbitration() {
  local wt="$1"
  local files="$2"
  STATE="$STATE" FILES="$files" PROJECT_ROOT="$PROJECT_ROOT" START_HEAD="$START_HEAD" python3 - <<'PY'
import os, re, json, subprocess

sf = os.environ['STATE']
files = os.environ['FILES']
proj = os.environ['PROJECT_ROOT']
start = os.environ['START_HEAD']

text = open(sf).read()

if re.search(r'^need_arbitration:', text, re.M):
    text = re.sub(r'^need_arbitration:.*$', 'need_arbitration: true', text, flags=re.M)
else:
    text += f"\nneed_arbitration: true\n"
if re.search(r'^conflict_files:', text, re.M):
    text = re.sub(r'^conflict_files:.*$', f'conflict_files: "{files}"', text, flags=re.M)
else:
    text += f'conflict_files: "{files}"\n'

their_commits = []
try:
    log_out = subprocess.check_output(
        ['git', '-C', proj, 'log', f'{start}..HEAD',
         '--format=%h|%s', '--stat', '-20'],
        stderr=subprocess.DEVNULL, text=True
    )
    current = None
    for line in log_out.strip().split('\n'):
        if not line.strip():
            continue
        if '|' in line and not line.startswith(' '):
            parts = line.split('|', 1)
            if len(parts[0]) <= 12:
                if current:
                    their_commits.append(current)
                current = {
                    'hash': parts[0].strip(),
                    'message': parts[1].strip()[:200],
                    'files': []
                }
                continue
        if current and '|' in line and line.startswith(' '):
            fname = line.split('|')[0].strip()
            if fname:
                current['files'].append(fname)
    if current:
        their_commits.append(current)
except Exception:
    pass

tc_json = json.dumps(their_commits, ensure_ascii=False)
if re.search(r'^their_commits:', text, re.M):
    text = re.sub(r'^their_commits:.*$', f"their_commits: '{tc_json}'", text, flags=re.M)
else:
    text += f"their_commits: '{tc_json}'\n"

open(sf, 'w').write(text)
PY
  echo "NEED_ARBITRATION ${wt}"
}

# bare 模式：commit 已在主仓，跳过 merge/worktree remove，只做 stash drop + rm state。
# 不使用 cleanup_phase 幂等保护——两步均幂等且顺序无关。
if [ -z "$WORKTREE_PATH" ]; then
  drop_pre_loop_stash 2>/dev/null || true
  rm -f "$STATE" 2>/dev/null || true
  echo "MERGED __main__"
  exit 0
fi

# === 阶段 1：ff merge（cleanup_phase="" 时跑） ===
if [ -z "$CLEANUP_PHASE" ]; then
  if [ ! -d "$WORKTREE_PATH" ]; then
    echo "ERROR worktree-missing-before-ff"
    exit 3
  fi
  BRANCH="$(git -C "$WORKTREE_PATH" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
  if [ -z "$BRANCH" ] || [ "$BRANCH" = "HEAD" ]; then
    echo "ERROR cannot-detect-worktree-branch"
    exit 3
  fi
  MAIN_BRANCH="$(git -C "$PROJECT_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
  CURRENT_HEAD="$(git -C "$PROJECT_ROOT" rev-parse --short HEAD 2>/dev/null || echo "")"

  # 前置断言：主仓必须在分支上（detached HEAD 下 merge 只移 HEAD 不移 branch ref）
  if [ -z "$MAIN_BRANCH" ] || [ "$MAIN_BRANCH" = "HEAD" ]; then
    echo "[merge-and-cleanup] FATAL: 主仓处于 detached HEAD，merge 会静默丢代码" >&2
    echo "ERROR main-repo-detached-head"
    exit 3
  fi

  # 前置断言：worktree 不能有未提交改动（merge 只合已 commit 的，remove --force 会丢未提交的）
  WT_DIRTY="$(git -C "$WORKTREE_PATH" status --porcelain 2>/dev/null || true)"
  if [ -n "$WT_DIRTY" ]; then
    echo "[merge-and-cleanup] FATAL: worktree 有未提交改动，merge + cleanup 会丢失这些文件：" >&2
    echo "$WT_DIRTY" | head -20 >&2
    echo "[merge-and-cleanup] 请先在 worktree 内 commit 或 abandon-loop" >&2
    echo "ERROR worktree-has-uncommitted-changes"
    exit 3
  fi

  # 路径 A：主干 HEAD 未变 → 直接 ff merge
  if [ "$CURRENT_HEAD" = "$START_HEAD" ] || git -C "$PROJECT_ROOT" merge-base --is-ancestor "$START_HEAD" HEAD 2>/dev/null; then
    if git -C "$PROJECT_ROOT" merge --ff-only "$BRANCH" >&2; then
      POST_HEAD="$(git -C "$PROJECT_ROOT" rev-parse --short "$MAIN_BRANCH" 2>/dev/null || echo "")"
      if [ "$POST_HEAD" = "$CURRENT_HEAD" ]; then
        echo "[merge-and-cleanup] FATAL: ff merge 报成功但分支 ref 未移动（${MAIN_BRANCH} 仍在 ${CURRENT_HEAD}）" >&2
        echo "ERROR branch-ref-not-advanced-after-ff"
        exit 3
      fi
      write_cleanup_phase "ff_merged"
      CLEANUP_PHASE="ff_merged"
    fi
  fi

  # 路径 B：路径 A 未走通 → 在 worktree 内 rebase 主干 → 再 ff
  if [ -z "$CLEANUP_PHASE" ]; then
    if git -C "$WORKTREE_PATH" rebase "$MAIN_BRANCH" >&2; then
      if git -C "$PROJECT_ROOT" merge --ff-only "$BRANCH" >&2; then
        POST_HEAD="$(git -C "$PROJECT_ROOT" rev-parse --short "$MAIN_BRANCH" 2>/dev/null || echo "")"
        if [ "$POST_HEAD" = "$CURRENT_HEAD" ]; then
          echo "[merge-and-cleanup] FATAL: ff merge 报成功但分支 ref 未移动（${MAIN_BRANCH} 仍在 ${CURRENT_HEAD}）" >&2
          echo "ERROR branch-ref-not-advanced-after-ff"
          exit 3
        fi
        write_cleanup_phase "ff_merged"
        CLEANUP_PHASE="ff_merged"
      else
        warn_stash_residual
        echo "ERROR ff-after-rebase-failed"
        exit 3
      fi
    else
      # 路径 C：rebase 冲突 → 仲裁
      CONFLICT_FILES="$(git -C "$WORKTREE_PATH" diff --name-only --diff-filter=U 2>/dev/null | tr '\n' ',' | sed 's/,$//')"
      git -C "$WORKTREE_PATH" rebase --abort 2>/dev/null || true
      warn_stash_residual
      mark_arbitration "$WORKTREE_PATH" "$CONFLICT_FILES"
      exit 1
    fi
  fi
fi

# === 阶段 2：worktree remove（cleanup_phase=ff_merged 时跑） ===
if [ "$CLEANUP_PHASE" = "ff_merged" ]; then
  drop_pre_loop_stash
  # BRANCH 可能阶段 1 没赋值（cleanup_phase 重入时）→ 重新取
  if [ -d "$WORKTREE_PATH" ]; then
    WT_BRANCH="$(git -C "$WORKTREE_PATH" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
  else
    WT_BRANCH=""
  fi
  # V3.2: warn about gitignored files that will be lost
  if [ -d "$WORKTREE_PATH" ]; then
    _ignored="$(git -C "$WORKTREE_PATH" ls-files --others --ignored --exclude-standard 2>/dev/null \
      | grep -v -E '^\.(claude/builder-loop|claude/loop-runs|claude/worktrees)(/|$)' \
      | head -10 || true)"
    if [ -n "$_ignored" ]; then
      echo "[merge-and-cleanup] ⚠️  worktree 内有 gitignored 文件将随 worktree 删除丢失：" >&2
      echo "$_ignored" | sed 's/^/  /' >&2
      echo "[merge-and-cleanup]    如需保留请先手动 cp 到主仓" >&2
    fi
  fi
  git -C "$PROJECT_ROOT" worktree remove --force "$WORKTREE_PATH" 2>/dev/null || \
    rm -rf "$WORKTREE_PATH" 2>/dev/null || true
  git -C "$PROJECT_ROOT" worktree prune 2>/dev/null || true
  cd "$PROJECT_ROOT" 2>/dev/null || cd /tmp
  [ -n "$WT_BRANCH" ] && git -C "$PROJECT_ROOT" branch -D "$WT_BRANCH" 2>/dev/null || true
  write_cleanup_phase "worktree_removed"
  CLEANUP_PHASE="worktree_removed"
fi

# === 阶段 3：rm state（cleanup_phase=worktree_removed 时跑） ===
if [ "$CLEANUP_PHASE" = "worktree_removed" ]; then
  # 取分支名给 stdout（worktree 已删，从 SLUG 推）
  RESULT_BRANCH="loop/${SLUG_FIELD}"
  rm -f "$STATE" 2>/dev/null || true
  echo "MERGED ${RESULT_BRANCH}"
  exit 0
fi

# 不应到达
echo "ERROR unknown-cleanup-phase=${CLEANUP_PHASE}"
exit 3
