#!/usr/bin/env bash
# run-apply-arbitration.sh — 解析 arbiter 输出并应用 patch 到 worktree
#
# 用法：bash run-apply-arbitration.sh <state_file> <arbiter_output_file>
#
# 流程：
#   1. 读 state_file 取 worktree_path / project_root
#   2. 读 loop.yml 取 arbitration.auto_apply_confidence（默认 medium）
#   3. 解析 arbiter 输出：提取 ARBITER_SUMMARY 行的信心度
#   4. 信心 < 阈值 → LOW_CONFIDENCE，exit 1
#   5. 提取 ARBITER_PATCH_BEGIN/END 块 → 写临时 diff 文件
#   6. cd worktree → git rebase main → git apply patch → git add → git rebase --continue
#   7. 清除 state 中 need_arbitration / conflict_files
#   8. 调 merge-worktree-back.sh 重试合回 → APPLIED / MERGE_FAILED
#
# stdout 最后一行：APPLIED / LOW_CONFIDENCE / APPLY_FAILED / MERGE_FAILED
# 退出码：0=APPLIED  1=LOW_CONFIDENCE  2=APPLY_FAILED  3=MERGE_FAILED
#
# 注意：arbiter.md 步骤 5 会 git rebase --abort，所以 apply 脚本拿到的
# worktree 是干净态，需重新 git rebase 制造冲突再 apply patch。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

STATE="${1:?用法: run-apply-arbitration.sh <state_file> <arbiter_output_file>}"
ARBITER_OUTPUT="${2:?用法: run-apply-arbitration.sh <state_file> <arbiter_output_file>}"

[ -f "$STATE" ] || { echo "ERROR: state file not found: $STATE" >&2; exit 2; }
[ -f "$ARBITER_OUTPUT" ] || { echo "ERROR: arbiter output file not found: $ARBITER_OUTPUT" >&2; exit 2; }

# ---- 辅助函数 ----
read_field() {
  # || true 兜底：字段不存在时 grep exit 1 + pipefail + set -e 让脚本静默退出
  # 老 V1.x state 缺 main_repo_path 字段时尤其重要
  grep -E "^${1}:" "$STATE" 2>/dev/null | head -1 | sed -E "s/^${1}:[[:space:]]*\"?([^\"]*)\"?[[:space:]]*\$/\1/" || true
}

# ---- 1. 读 state 取关键字段 ----
# V2.0: 仲裁脚本里 PROJECT_ROOT 仅用于主仓 git 操作（rebase / merge / branch），
#       优先读 main_repo_path（V2.0+），缺失时按旧语义把 project_root 当主仓。
PROJECT_ROOT="$(read_field main_repo_path)"
[ -z "$PROJECT_ROOT" ] && PROJECT_ROOT="$(read_field project_root)"
WORKTREE_PATH="$(read_field worktree_path)"

if [ -z "$WORKTREE_PATH" ] || [ ! -d "$WORKTREE_PATH" ]; then
  echo "ERROR: worktree_path 无效: $WORKTREE_PATH" >&2
  echo "APPLY_FAILED"
  exit 2
fi
if [ -z "$PROJECT_ROOT" ] || [ ! -d "$PROJECT_ROOT" ]; then
  echo "ERROR: 主仓路径无效（main_repo_path/project_root 都为空或不存在）: $PROJECT_ROOT" >&2
  echo "APPLY_FAILED"
  exit 2
fi

# ---- 2. 读 loop.yml 取 auto_apply_confidence ----
LOOP_YML="${PROJECT_ROOT}/.claude/loop.yml"
THRESHOLD="medium"  # schema 默认值
if [ -f "$LOOP_YML" ]; then
  THRESHOLD_RAW="$(LOOP_YML_PATH="$LOOP_YML" python3 -c "
import sys, re, os
text = open(os.environ['LOOP_YML_PATH']).read()
m = re.search(r'auto_apply_confidence:\s*(\w+)', text)
print(m.group(1) if m else 'medium')
" 2>/dev/null || echo "medium")"
  [ -n "$THRESHOLD_RAW" ] && THRESHOLD="$THRESHOLD_RAW"
fi

# ---- 3. 解析 arbiter 输出：提取信心度 ----
SUMMARY_LINE="$(grep -E '^ARBITER_SUMMARY:' "$ARBITER_OUTPUT" | tail -1 || echo "")"
if [ -z "$SUMMARY_LINE" ]; then
  echo "ERROR: arbiter 输出中未找到 ARBITER_SUMMARY 行" >&2
  echo "APPLY_FAILED"
  exit 2
fi

# 提取信心度（格式: 信心: high/medium/low）
CONFIDENCE="$(echo "$SUMMARY_LINE" | grep -oE '信心:\s*(high|medium|low)' | awk -F: '{gsub(/^[[:space:]]*/,"",$2); print $2}' || echo "low")"
[ -z "$CONFIDENCE" ] && CONFIDENCE="low"

echo "[arbitration] 信心度: $CONFIDENCE, 阈值: $THRESHOLD" >&2

# ---- 4. 信心度 vs 阈值比较 ----
# 数值映射: low=1, medium=2, high=3
confidence_to_num() {
  case "$1" in
    high)   echo 3 ;;
    medium) echo 2 ;;
    low)    echo 1 ;;
    *)      echo 0 ;;
  esac
}
CONF_NUM="$(confidence_to_num "$CONFIDENCE")"
THRESH_NUM="$(confidence_to_num "$THRESHOLD")"

if [ "$CONF_NUM" -lt "$THRESH_NUM" ]; then
  echo "[arbitration] 信心 $CONFIDENCE < 阈值 $THRESHOLD，不自动 apply" >&2
  echo "LOW_CONFIDENCE"
  exit 1
fi

# ---- 5. 提取 ARBITER_PATCH_BEGIN/END 块 ----
PATCH_FILE="$(mktemp /tmp/arbiter-patch-XXXXXX.diff)"
trap 'rm -f "$PATCH_FILE"' EXIT

sed -n '/^ARBITER_PATCH_BEGIN$/,/^ARBITER_PATCH_END$/{ /^ARBITER_PATCH_BEGIN$/d; /^ARBITER_PATCH_END$/d; p; }' "$ARBITER_OUTPUT" > "$PATCH_FILE"

if [ ! -s "$PATCH_FILE" ]; then
  echo "ERROR: 未能从 arbiter 输出提取有效 patch" >&2
  echo "APPLY_FAILED"
  exit 2
fi

echo "[arbitration] 已提取 patch ($(wc -l < "$PATCH_FILE") 行)" >&2

# ---- 6. 在 worktree 里重新 rebase 制造冲突 → apply patch ----
# arbiter 步骤 5 已经 git rebase --abort，worktree 是干净态
MAIN_BRANCH="$(git -C "$PROJECT_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")"

echo "[arbitration] 重新 rebase $MAIN_BRANCH 制造冲突态..." >&2
# rebase 预期失败（有冲突），捕获退出码
REBASE_EC=0
git -C "$WORKTREE_PATH" rebase "$MAIN_BRANCH" >/dev/null 2>&1 || REBASE_EC=$?

if [ "$REBASE_EC" -eq 0 ]; then
  # rebase 成功了（无冲突，可能主干已 ff），直接走 merge
  echo "[arbitration] rebase 无冲突，跳过 patch apply" >&2
else
  # 有冲突 → apply patch 解冲突
  echo "[arbitration] 正在 apply patch..." >&2
  if ! git -C "$WORKTREE_PATH" apply --allow-overlap "$PATCH_FILE" 2>&1; then
    # fallback: 先 dry-run 验证 patch 格式，再实际 apply
    if ! (cd "$WORKTREE_PATH" && patch --dry-run -p1 < "$PATCH_FILE") >/dev/null 2>&1; then
      echo "ERROR: patch dry-run 失败，patch 格式无效" >&2
      git -C "$WORKTREE_PATH" rebase --abort 2>/dev/null || true
      echo "APPLY_FAILED"
      exit 2
    fi
    if ! (cd "$WORKTREE_PATH" && patch -p1 < "$PATCH_FILE") 2>&1; then
      echo "ERROR: patch apply 失败" >&2
      git -C "$WORKTREE_PATH" rebase --abort 2>/dev/null || true
      echo "APPLY_FAILED"
      exit 2
    fi
  fi

  # stage 所有冲突文件并 continue rebase
  git -C "$WORKTREE_PATH" add -A 2>/dev/null
  if ! git -C "$WORKTREE_PATH" -c core.hooksPath=/dev/null rebase --continue 2>&1; then
    echo "ERROR: rebase --continue 失败" >&2
    git -C "$WORKTREE_PATH" rebase --abort 2>/dev/null || true
    echo "APPLY_FAILED"
    exit 2
  fi
fi

echo "[arbitration] patch apply + rebase 成功" >&2

# ---- 7. 清除 state 中 need_arbitration / conflict_files ----
STATE="$STATE" python3 - <<'PY'
import os, re
sf = os.environ['STATE']
text = open(sf).read()
text = re.sub(r'^need_arbitration:.*\n?', '', text, flags=re.M)
text = re.sub(r'^conflict_files:.*\n?', '', text, flags=re.M)
open(sf, 'w').write(text)
PY

# ---- 8. 调 merge-worktree-back.sh 重试合回 ----
MERGE_SCRIPT="${SCRIPT_DIR}/merge-worktree-back.sh"
if [ ! -f "$MERGE_SCRIPT" ]; then
  echo "ERROR: merge-worktree-back.sh not found at $MERGE_SCRIPT" >&2
  echo "MERGE_FAILED"
  exit 3
fi

echo "[arbitration] 重试 merge-worktree-back..." >&2
MERGE_EC=0
MERGE_OUT="$(bash "$MERGE_SCRIPT" "$STATE" 2>&1)" || MERGE_EC=$?
MERGE_LAST="$(echo "$MERGE_OUT" | tail -1)"
MERGE_ACTION="$(echo "$MERGE_LAST" | awk '{print $1}')"

case "$MERGE_ACTION" in
  MERGED|NOOP)
    echo "[arbitration] ✅ 合回成功 ($MERGE_ACTION)" >&2
    echo "APPLIED"
    exit 0
    ;;
  NEED_ARBITRATION)
    echo "[arbitration] ⚠️  合回再次冲突" >&2
    echo "MERGE_FAILED"
    exit 3
    ;;
  *)
    echo "[arbitration] ❌ merge 未知结果: $MERGE_OUT" >&2
    echo "MERGE_FAILED"
    exit 3
    ;;
esac
