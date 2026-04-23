#!/usr/bin/env bash
# install.sh — 把 cc-builder-loop 的文件软链到 CC 运行时路径（~/.claude/）
#
# 功能：
#   1. ln -sfn 整目录链 skills/builder-loop/ → ~/.claude/skills/builder-loop/
#   2. ln -sf 逐文件链 scripts/*.sh → ~/.claude/scripts/
#   3. ln -sf 逐文件链 agents/*.md → ~/.claude/agents/
#   4. jq 增量合并 4 个 hook 条目到 ~/.claude/settings.json
#
# 幂等：重复跑不报错，已存在的软链会被 -f 覆盖
# 依赖：jq（hook 注册用）

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${HOME}/.claude"

# ---- 前置检查 ----
if [ ! -d "$CLAUDE_DIR" ]; then
  echo "❌ ~/.claude/ 不存在，请先部署 dotfiles（stow claude）" >&2
  exit 1
fi

if ! command -v jq &>/dev/null; then
  echo "⚠️  jq 未安装，跳过 hook 注册（软链仍会创建）" >&2
  JQ_AVAILABLE=false
else
  JQ_AVAILABLE=true
fi

# ---- 1. 创建必要目录 ----
mkdir -p "$CLAUDE_DIR/skills" "$CLAUDE_DIR/scripts" "$CLAUDE_DIR/agents"

# ---- 2. 软链 skill 整目录 ----
ln -sfn "$REPO_DIR/skills/builder-loop" "$CLAUDE_DIR/skills/builder-loop"
echo "✓ skills/builder-loop/ → ~/.claude/skills/builder-loop/"

# ---- 3. 软链 companion scripts（逐文件）----
for f in "$REPO_DIR/scripts/"*.sh; do
  [ -f "$f" ] || continue
  bn="$(basename "$f")"
  ln -sf "$f" "$CLAUDE_DIR/scripts/$bn"
  echo "✓ scripts/$bn → ~/.claude/scripts/$bn"
done

# ---- 4. 软链 agents（逐文件）----
for f in "$REPO_DIR/agents/"*.md; do
  [ -f "$f" ] || continue
  bn="$(basename "$f")"
  ln -sf "$f" "$CLAUDE_DIR/agents/$bn"
  echo "✓ agents/$bn → ~/.claude/agents/$bn"
done

# ---- 5. 注册 hooks 到 settings.json（jq 增量合并）----
if [ "$JQ_AVAILABLE" = true ]; then
  SETTINGS="$CLAUDE_DIR/settings.json"
  if [ ! -f "$SETTINGS" ]; then
    echo "⚠️  ~/.claude/settings.json 不存在，跳过 hook 注册" >&2
  else
    SCRIPTS_DIR="$CLAUDE_DIR/scripts"
    cp "$SETTINGS" "${SETTINGS}.bak"

    # 用 python3 做增量合并（比 jq 更好处理数组去重 + 追加）
    python3 - "$SETTINGS" "$SCRIPTS_DIR" <<'PYEOF'
import json, sys, os

settings_path = sys.argv[1]
scripts_dir = sys.argv[2]

with open(settings_path) as f:
    cfg = json.load(f)

hooks = cfg.setdefault("hooks", {})

def make_entry(cmd_name, matcher=None):
    entry = {"hooks": [{"type": "command", "command": os.path.join(scripts_dir, cmd_name)}]}
    if matcher:
        entry["matcher"] = matcher
    return entry

def has_entry(arr, cmd_name):
    for item in arr:
        for h in item.get("hooks", []):
            if cmd_name in h.get("command", ""):
                return True
    return False

registrations = [
    ("Stop",           "builder-loop-stop.sh",      None),
    ("SubagentStart",  "tester-lock-write.sh",      "tester"),
    ("SubagentStop",   "tester-lock-clear.sh",      "tester"),
    ("PreToolUse",     "tester-lock-check.sh",      "Read|Grep|Glob"),
    ("PreToolUse",     "reviewer-timing-check.sh",  "Agent"),
]

added = 0
for hook_type, cmd_name, matcher in registrations:
    arr = hooks.setdefault(hook_type, [])
    if not has_entry(arr, cmd_name):
        arr.append(make_entry(cmd_name, matcher))
        added += 1

with open(settings_path, "w") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
    f.write("\n")

print(f"✓ hooks: {added} 条新增，{5 - added} 条已存在")
PYEOF
  fi
fi

echo ""
echo "✅ cc-builder-loop 安装完成"
echo "   skill: ~/.claude/skills/builder-loop/"
echo "   scripts: ~/.claude/scripts/builder-loop-stop.sh + tester-lock-*.sh + reviewer-timing-check.sh"
echo "   agents: ~/.claude/agents/tester.md + arbiter.md"
