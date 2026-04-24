#!/usr/bin/env bash
# merge-worktree-back.sh — PASS 后把 worktree 的分支合回主干并清理
#
# 用法：bash merge-worktree-back.sh <state_file>
#
# 输出（stdout 的最后一行为决定性结果）：
#   MERGED <branch>                    ← fast-forward / rebase 成功并已清理
#   NOOP                               ← worktree 未启用或 state 无 worktree_path，啥也不做
#   NEED_ARBITRATION <worktree_path>   ← rebase 冲突，留 worktree 等 arbiter
#   ERROR <reason>                     ← 其他失败（exit 3）
#
# 退出码：0=MERGED/NOOP  1=NEED_ARBITRATION  3=ERROR
#
# 副作用：
#   - PASS 且无冲突 → `git merge --ff-only` + `git worktree remove` + `git branch -d`
#   - rebase 冲突 → 在 state 里写 `need_arbitration: true` + `conflict_files: <...>`
#
# 依赖 state 字段：project_root / worktree_path / start_head

set -euo pipefail

STATE="${1:?state file path required}"
[ -f "$STATE" ] || { echo "ERROR state-not-found"; exit 3; }

read_field() {
  grep -E "^${1}:" "$STATE" | head -1 | sed -E "s/^${1}:[[:space:]]*\"?([^\"]*)\"?[[:space:]]*\$/\1/"
}

PROJECT_ROOT="$(read_field project_root)"
WORKTREE_PATH="$(read_field worktree_path)"
START_HEAD="$(read_field start_head)"

# worktree 未启用 → 直接放行（V1 老配置 / worktree.enabled=false 场景）
if [ -z "$WORKTREE_PATH" ] || [ ! -d "$WORKTREE_PATH" ]; then
  echo "NOOP"
  exit 0
fi
if [ -z "$PROJECT_ROOT" ] || [ ! -d "$PROJECT_ROOT" ]; then
  echo "ERROR project-root-missing"
  exit 3
fi

# 从 worktree 取分支名（state 未存该字段，就地取）
BRANCH="$(git -C "$WORKTREE_PATH" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
if [ -z "$BRANCH" ] || [ "$BRANCH" = "HEAD" ]; then
  echo "ERROR cannot-detect-worktree-branch"
  exit 3
fi

# 主干当前分支名（必须在 PROJECT_ROOT 非 worktree 调 git）
MAIN_BRANCH="$(git -C "$PROJECT_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
CURRENT_HEAD="$(git -C "$PROJECT_ROOT" rev-parse --short HEAD 2>/dev/null || echo "")"

mark_arbitration() {
  local wt="$1"
  local files="$2"
  # 用 python 安全改 yaml（防特殊字符）+ 提取对方 commits 上下文
  STATE="$STATE" FILES="$files" PROJECT_ROOT="$PROJECT_ROOT" START_HEAD="$START_HEAD" python3 - <<'PY'
import os, re, json, subprocess

sf = os.environ['STATE']
files = os.environ['FILES']
proj = os.environ['PROJECT_ROOT']
start = os.environ['START_HEAD']

text = open(sf).read()

# 写 need_arbitration + conflict_files
if re.search(r'^need_arbitration:', text, re.M):
    text = re.sub(r'^need_arbitration:.*$', 'need_arbitration: true', text, flags=re.M)
else:
    text += f"\nneed_arbitration: true\n"
if re.search(r'^conflict_files:', text, re.M):
    text = re.sub(r'^conflict_files:.*$', f'conflict_files: "{files}"', text, flags=re.M)
else:
    text += f'conflict_files: "{files}"\n'

# 提取对方 commits（主干 start_head 之后的新 commit）
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
            if len(parts[0]) <= 12:  # hash 长度合理
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

cleanup_worktree() {
  # 删 worktree + 分支（忽略失败；用户可事后手动 prune）
  git -C "$PROJECT_ROOT" worktree remove --force "$WORKTREE_PATH" 2>/dev/null || \
    rm -rf "$WORKTREE_PATH" 2>/dev/null || true
  git -C "$PROJECT_ROOT" worktree prune 2>/dev/null || true
  git -C "$PROJECT_ROOT" branch -D "$BRANCH" 2>/dev/null || true
  # 清理对应 state 文件（多状态模式下每 worktree 一份）
  rm -f "$STATE" 2>/dev/null || true
}

# ---- auto-commit：worktree 内未提交改动 → 自动 commit（防 cleanup 丢数据）----
WT_STATUS="$(git -C "$WORKTREE_PATH" status --porcelain 2>/dev/null || echo "")"
if [ -n "$WT_STATUS" ]; then
  ITER_NUM="$(grep -E '^iter:' "$STATE" | head -1 | awk '{print $2}')"
  ITER_NUM="${ITER_NUM:-0}"
  git -C "$WORKTREE_PATH" add -A >&2
  git -C "$WORKTREE_PATH" commit -m "chore(loop): auto-commit iter ${ITER_NUM}" >&2 || {
    echo "ERROR auto-commit-failed"
    exit 3
  }
fi

# worktree 分支无新 commit（含 auto-commit 后仍未前进）→ NOOP（防 MERGED 假阳性）
WT_HEAD="$(git -C "$WORKTREE_PATH" rev-parse --short HEAD 2>/dev/null || echo "")"
if [ "$WT_HEAD" = "$START_HEAD" ]; then
  cleanup_worktree
  echo "NOOP"
  exit 0
fi

# === 路径 A：主干 HEAD 未变 → 直接 fast-forward ===
if [ "$CURRENT_HEAD" = "$START_HEAD" ] || git -C "$PROJECT_ROOT" merge-base --is-ancestor "$START_HEAD" HEAD 2>/dev/null; then
  if git -C "$PROJECT_ROOT" merge --ff-only "$BRANCH" >&2; then
    cleanup_worktree
    echo "MERGED ${BRANCH}"
    exit 0
  fi
  # ff 失败（极少，可能主干已经有新 commit）→ 走路径 B
fi

# === 路径 B：主干 HEAD 已变 → 先在 worktree 内 rebase 主干 ===
if git -C "$WORKTREE_PATH" rebase "$MAIN_BRANCH" >&2; then
  # rebase 成功 → 回主干 ff
  if git -C "$PROJECT_ROOT" merge --ff-only "$BRANCH" >&2; then
    cleanup_worktree
    echo "MERGED ${BRANCH}"
    exit 0
  fi
  echo "ERROR ff-after-rebase-failed"
  exit 3
fi

# === 路径 C：rebase 冲突 → 标记仲裁 ===
CONFLICT_FILES="$(git -C "$WORKTREE_PATH" diff --name-only --diff-filter=U 2>/dev/null | tr '\n' ',' | sed 's/,$//')"
git -C "$WORKTREE_PATH" rebase --abort 2>/dev/null || true
mark_arbitration "$WORKTREE_PATH" "$CONFLICT_FILES"
exit 1
