#!/usr/bin/env bash
# migrate-state.sh — 一次性迁移旧 .claude/builder-loop.local.md 到新多状态目录
#
# 用法：
#   bash migrate-state.sh [<project_root>]
#
# 默认 project_root = $PWD。
#
# 行为：
#   1. 若 <project>/.claude/builder-loop.local.md 存在：
#      - 读 slug（无则从 worktree_path basename 推断，再不行用 __main__）
#      - 若 worktree_path 目录仍存在（或为 bare loop 空值）→ mv 到 .claude/builder-loop/state/<slug>.yml
#      - 若 worktree_path 目录已失效 → 归档到 .claude/builder-loop/legacy/<ts>.bak
#   2. 幂等：迁移完成后 .claude/builder-loop.local.md 会被删除
#
# 注意：本脚本只迁移文件位置，不改写 schema。
#   V1.8 → V1.9.x：state schema 不变，无需改写。
#   V1.x → V2.0  ：state 增加 main_repo_path 字段；老 state 缺该字段时由
#                  builder-loop-stop.sh / merge-worktree-back.sh / run-apply-arbitration.sh
#                  在运行时按"老 V1.x state.project_root 等于主仓"的旧语义兜底兼容。
#                  无需手动迁移；下次 setup-builder-loop.sh 触发时会写新 schema。
#
# 输出：迁移动作的简要日志（stdout）
# 退出码：0=成功或无需迁移 / 1=失败

set -euo pipefail

PROJECT_ROOT="${1:-$PWD}"
PROJECT_ROOT="$(cd "$PROJECT_ROOT" && pwd -P)"
OLD_STATE="${PROJECT_ROOT}/.claude/builder-loop.local.md"
NEW_DIR="${PROJECT_ROOT}/.claude/builder-loop/state"
LEGACY_DIR="${PROJECT_ROOT}/.claude/builder-loop/legacy"

if [ ! -f "$OLD_STATE" ]; then
  echo "[migrate-state] 无旧 state（${OLD_STATE}），跳过"
  exit 0
fi

mkdir -p "$NEW_DIR" "$LEGACY_DIR"

# 解析 slug / worktree_path（grep 不匹配时返回 1 + pipefail 会杀脚本，用 || true 兜底）
SLUG="$(grep -E '^slug:' "$OLD_STATE" 2>/dev/null | head -1 | sed -E 's/^slug:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || true)"
WT_PATH="$(grep -E '^worktree_path:' "$OLD_STATE" 2>/dev/null | head -1 | sed -E 's/^worktree_path:[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/' || true)"

# slug fallback：从 worktree_path basename 推断，再不行就 __main__
if [ -z "$SLUG" ]; then
  if [ -n "$WT_PATH" ]; then
    SLUG="$(basename "$WT_PATH")"
  else
    SLUG="__main__"
  fi
fi

# worktree_path 失效 → 归档
if [ -n "$WT_PATH" ] && [ ! -d "$WT_PATH" ]; then
  TS="$(date +%s)"
  DEST="${LEGACY_DIR}/${SLUG}.${TS}.bak"
  mv "$OLD_STATE" "$DEST"
  echo "[migrate-state] 归档过期 state → ${DEST} (worktree_path=${WT_PATH} 已失效)"
  exit 0
fi

# worktree 仍在（或 bare loop）→ 正常迁移
DEST="${NEW_DIR}/${SLUG}.yml"
if [ -e "$DEST" ]; then
  # 目标已存在 → 归档旧的以避免覆盖
  TS="$(date +%s)"
  BACKUP="${LEGACY_DIR}/${SLUG}.${TS}.bak"
  mv "$OLD_STATE" "$BACKUP"
  echo "[migrate-state] ⚠️  目标 ${DEST} 已存在，旧 state 归档到 ${BACKUP}"
  exit 0
fi

# 补上 slug 字段（若旧文件无）
if ! grep -q "^slug:" "$OLD_STATE"; then
  # 在文件开头第二行插入（首行通常是注释）
  awk -v slug="$SLUG" 'NR==1 {print; print "slug: \"" slug "\""; next} {print}' "$OLD_STATE" > "$DEST"
  rm -f "$OLD_STATE"
else
  mv "$OLD_STATE" "$DEST"
fi
echo "[migrate-state] ✅ 迁移完成：${OLD_STATE} → ${DEST}"
