#!/usr/bin/env bash
# uninstall.sh — 删除 cc-builder-loop 的所有软链和 hook 注册
#
# 执行后 builder-loop 完全从 CC 运行时消失。
# 如需恢复，重跑 install.sh 即可。

set -euo pipefail

CLAUDE_DIR="${HOME}/.claude"

echo "正在卸载 cc-builder-loop..."

# ---- 1. 删除 skill 目录软链 ----
if [ -L "$CLAUDE_DIR/skills/builder-loop" ]; then
  rm "$CLAUDE_DIR/skills/builder-loop"
  echo "✓ 已删除 ~/.claude/skills/builder-loop"
fi

# ---- 2. 删除 companion scripts 软链 ----
for f in builder-loop-stop.sh tester-lock-write.sh tester-lock-check.sh tester-lock-clear.sh tester-write-guard.sh; do
  target="$CLAUDE_DIR/scripts/$f"
  if [ -L "$target" ]; then
    rm "$target"
    echo "✓ 已删除 ~/.claude/scripts/$f"
  fi
done

# ---- 3. 删除 agents 软链 ----
for f in tester.md arbiter.md; do
  target="$CLAUDE_DIR/agents/$f"
  if [ -L "$target" ]; then
    rm "$target"
    echo "✓ 已删除 ~/.claude/agents/$f"
  fi
done

# ---- 4. 从 settings.json 删除 hook 条目 ----
SETTINGS="$CLAUDE_DIR/settings.json"
if [ -f "$SETTINGS" ] && command -v python3 &>/dev/null; then
  cp "$SETTINGS" "${SETTINGS}.bak"
  python3 - "$SETTINGS" <<'PYEOF'
import json, sys

settings_path = sys.argv[1]
with open(settings_path) as f:
    cfg = json.load(f)

hooks = cfg.get("hooks", {})
bl_scripts = ["builder-loop-stop.sh", "tester-lock-write.sh",
              "tester-lock-check.sh", "tester-lock-clear.sh",
              "tester-write-guard.sh"]

removed = 0
for hook_type in list(hooks.keys()):
    arr = hooks[hook_type]
    new_arr = []
    for item in arr:
        dominated = False
        for h in item.get("hooks", []):
            cmd = h.get("command", "")
            if any(s in cmd for s in bl_scripts):
                dominated = True
                break
        if not dominated:
            new_arr.append(item)
        else:
            removed += 1
    if new_arr:
        hooks[hook_type] = new_arr
    else:
        del hooks[hook_type]

with open(settings_path, "w") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
    f.write("\n")

print(f"✓ hooks: 已删除 {removed} 条 builder-loop 相关条目")
PYEOF
fi

echo ""
echo "✅ cc-builder-loop 已卸载"
echo "   如需恢复：cd <cc-builder-loop-repo> && ./install.sh"
