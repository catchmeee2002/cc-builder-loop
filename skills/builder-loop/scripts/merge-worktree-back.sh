#!/usr/bin/env bash
# merge-worktree-back.sh — V2.x 立即合主线路径（commit + ff merge + cleanup 一气合）
#
# V3.0 角色变化：
#   - V3.0 起 hook PASS 路径不再调用本脚本；hook 改调 worktree-commit-only.sh，
#     ff merge + cleanup 推迟到 reviewer 通过后由 builder 主动调 merge-and-cleanup.sh。
#   - 本脚本保留作"立即合主线"语义入口：arbiter 仲裁后续路径（run-apply-arbitration.sh）/
#     bare 模式 NOOP / 现有 e2e fixture（test-conflict.sh / test-bare-loop-merge.sh /
#     test-stop-hook-race-and-commit-msg.sh）继续使用。
#   - 行为不变。后续可考虑把 arbiter 续路径迁到 merge-and-cleanup.sh，本期保留兼容。
#
# ⚠️  V3.0 已知技术债 — arbiter 续路径绕过 reviewer-as-gate：
#     run-apply-arbitration.sh 在 rebase 冲突由 arbiter 解决后调本脚本（"立即合"语义），
#     此时 commit 直接 ff 进主线，**跳过了 reviewer gate**。这是 V3.0 落地的已知缺口：
#     冲突解决场景下 reviewer 看不到合并后的代码，无法发挥门禁作用。
#     跟踪在 .claude/improvements.md「arbiter 续路径迁移到 reviewer-as-gate」候选条目。
#     建议修法：run-apply-arbitration.sh 改调 merge-and-cleanup.sh（V3.0 拆 merge 路径）。
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
# 依赖 state 字段：main_repo_path（V2.0+，缺失则 fallback project_root 视为旧主仓） /
#                  worktree_path / start_head

set -euo pipefail

STATE="${1:?state file path required}"
[ -f "$STATE" ] || { echo "ERROR state-not-found"; exit 3; }

read_field() {
  # V1.9 fix: 字段不存在时 grep exit 1 + pipefail + set -e 会让脚本提前退出（bare loop 场景：
  # state 没 worktree_path 字段触发 stop hook merge_action 为空），加 || true 容错
  grep -E "^${1}:" "$STATE" 2>/dev/null | head -1 | sed -E "s/^${1}:[[:space:]]*\"?([^\"]*)\"?[[:space:]]*\$/\1/" || true
}

# V2.0：main_repo_path 是主仓（git merge / branch / worktree prune 都在此）。
# 老 V1.x state 没 main_repo_path 字段 → 它的 project_root 就是主仓（旧语义）。
PROJECT_ROOT="$(read_field main_repo_path)"
[ -z "$PROJECT_ROOT" ] && PROJECT_ROOT="$(read_field project_root)"
WORKTREE_PATH="$(read_field worktree_path)"
START_HEAD="$(read_field start_head)"

# V2.3 新增字段：dirty stash 流程使用（缺字段视为空 = clean / bare 模式）
PRE_LOOP_DIRTY_FILES="$(read_field pre_loop_dirty_files)"
SLUG_FIELD="$(read_field slug)"

# V2.3: PASS 路径合回主仓后 drop 主仓 stash 副本（已通过 worktree commit 合回，副本冗余）
# 通过 SLUG 签名（builder-loop:auto:slug=<slug>:）匹配 stash list，避免 stash@{N} 多 builder 串味
# 注：bash 内 stash@{N} 字面量正常；若用户在 zsh 拼接调用本函数需 quote `stash@\{N\}` 防 brace expansion
drop_pre_loop_stash() {
  [ -z "$SLUG_FIELD" ] && return 0
  local sig="builder-loop:auto:slug=${SLUG_FIELD}:"
  local idx
  idx="$(git -C "$PROJECT_ROOT" stash list 2>/dev/null | grep -F "$sig" | head -1 | awk -F: '{print $1}' || true)"
  if [ -n "$idx" ]; then
    git -C "$PROJECT_ROOT" stash drop "$idx" 2>/dev/null || \
      echo "[merge-worktree-back] ⚠️  stash drop 失败（${idx}），需手动 git stash drop" >&2
  fi
}

# V2.3: 失败路径用——提示主仓 stash 残留（不 drop，让用户决定是否清理）
# 调用点：NEED_ARBITRATION（中间态，arbiter 处理后 PASS 时再 drop）/ ff-after-rebase-failed（终态错误）
warn_stash_residual() {
  [ -z "$SLUG_FIELD" ] && return 0
  local sig="builder-loop:auto:slug=${SLUG_FIELD}:"
  local idx
  idx="$(git -C "$PROJECT_ROOT" stash list 2>/dev/null | grep -F "$sig" | head -1 | awk -F: '{print $1}' || true)"
  if [ -n "$idx" ]; then
    echo "[merge-worktree-back] ⚠️  主仓 dirty stash 副本仍在 stash list：${idx}" >&2
    echo "                           签名：${sig}" >&2
    echo "                           本路径不自动 drop（PASS 路径才 drop）；用户人工清理：git stash drop ${idx}" >&2
  fi
}

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
  # V1.8.3: message 从 state 的 task_description 构造，保留本次改动语义
  # state schema 用 YAML block scalar（`task_description: |` + 下一行缩进），用 awk 解析
  TASK_DESC="$(awk '/^task_description: \|/{getline; sub(/^[[:space:]]+/, ""); print; exit}' "$STATE" 2>/dev/null || echo "")"
  if [ -n "$TASK_DESC" ]; then
    COMMIT_SUBJECT="chore(loop): [cr_id_skip] Auto-commit ${TASK_DESC}"
  else
    # 降级：task_description 为空时回退旧格式（向后兼容）
    COMMIT_SUBJECT="chore(loop): [cr_id_skip] Auto-commit iter ${ITER_NUM}"
  fi
  # V2.3: dirty stash 模式下，subject 加 [+N main-dirty] 标记 + body 列文件清单
  DIRTY_BODY=""
  if [ -n "$PRE_LOOP_DIRTY_FILES" ]; then
    DIRTY_COUNT="$(printf '%s' "$PRE_LOOP_DIRTY_FILES" | tr ',' '\n' | grep -c . || true)"
    if [ "$DIRTY_COUNT" -gt 0 ]; then
      COMMIT_SUBJECT="${COMMIT_SUBJECT} [+${DIRTY_COUNT} main-dirty]"
      DIRTY_BODY=$'\n\n含主仓预存改动（V2.3 dirty stash apply）：'
      while IFS= read -r _f; do
        [ -n "$_f" ] && DIRTY_BODY="${DIRTY_BODY}"$'\n'"  - ${_f}"
      done < <(printf '%s' "$PRE_LOOP_DIRTY_FILES" | tr ',' '\n')
    fi
  fi
  COMMIT_MSG="${COMMIT_SUBJECT}${DIRTY_BODY}"
  git -C "$WORKTREE_PATH" add -A >&2
  # 用 -F - 从 stdin 读避免 TASK_DESC 中特殊字符（引号/反引号/$）被 shell 二次展开
  printf '%s\n' "$COMMIT_MSG" | git -C "$WORKTREE_PATH" commit -F - >&2 || {
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
    # V2.3: drop 主仓 stash 副本（合回主仓后冗余），需在 cleanup 删 state 之前
    drop_pre_loop_stash
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
    drop_pre_loop_stash
    cleanup_worktree
    echo "MERGED ${BRANCH}"
    exit 0
  fi
  # V2.3: ff 失败终态错误 → 提示主仓 stash 副本仍在（用户须手动清理）
  warn_stash_residual
  echo "ERROR ff-after-rebase-failed"
  exit 3
fi

# === 路径 C：rebase 冲突 → 标记仲裁 ===
CONFLICT_FILES="$(git -C "$WORKTREE_PATH" diff --name-only --diff-filter=U 2>/dev/null | tr '\n' ',' | sed 's/,$//')"
git -C "$WORKTREE_PATH" rebase --abort 2>/dev/null || true
# V2.3: 仲裁中间态 → 提示 stash 副本仍在（arbiter 处理后再次 ff 成功才 drop）
warn_stash_residual
mark_arbitration "$WORKTREE_PATH" "$CONFLICT_FILES"
exit 1
