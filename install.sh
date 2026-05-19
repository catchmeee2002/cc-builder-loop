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

# ---- 0. 方案识别（max / copilot）----
# 运行时唯一判据：ANTHROPIC_BASE_URL 含 localhost / 127.0.0.1 → copilot；否则 → max
# max 方案下不需要 tester-write-guard.sh hook（CC 直连不走代理拦截）
detect_plan() {
  local url="${ANTHROPIC_BASE_URL:-}"
  case "$url" in
    *localhost*|*127.0.0.1*) echo "copilot" ;;
    *)                       echo "max" ;;
  esac
}
PLAN="$(detect_plan)"
echo "✓ 检测方案=${PLAN} base_url=${ANTHROPIC_BASE_URL:-unset}"

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

# ---- 3.5 清理 V3.1 废弃的旧软链 ----
for f in tester-lock-write.sh tester-write-guard.sh; do
  old_link="$CLAUDE_DIR/scripts/$f"
  [ -L "$old_link" ] && rm "$old_link" && echo "✓ removed deprecated: $f"
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

    # ---- 5.1 写前自检：源 JSON 必须合法，否则拒绝写并提示用户 ----
    if ! python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$SETTINGS" 2>/dev/null; then
      echo "❌ ~/.claude/settings.json 已损坏（非法 JSON），拒绝改写" >&2
      echo "   请先手动修复或恢复，然后重新运行 install.sh" >&2
      echo "   可用历史快照：" >&2
      ls -t "${SETTINGS}".bak* 2>/dev/null | head -3 >&2 || true
      exit 1
    fi

    # ---- 5.2 备份：保留最近一份 .bak（覆盖式），同时落一份带时间戳的快照（不覆盖）----
    cp "$SETTINGS" "${SETTINGS}.bak"
    TS="$(date +%Y%m%d-%H%M%S)"
    cp "$SETTINGS" "${SETTINGS}.bak.${TS}" 2>/dev/null || true

    # ---- 5.3 写入到 .tmp，再原子替换（避免半成品落到目标路径）----
    SETTINGS_TMP="${SETTINGS}.tmp.$$"
    if ! PLAN="$PLAN" python3 - "$SETTINGS" "$SETTINGS_TMP" "$SCRIPTS_DIR" <<'PYEOF'
import json, sys, os

settings_path = sys.argv[1]
out_path = sys.argv[2]
scripts_dir = sys.argv[3]
# PLAN env 由外层 bash 通过 detect_plan() 注入；env 传递失败时静默降级到 "copilot"
# （注册全部 6 条，向后兼容老行为）。
plan = os.environ.get("PLAN", "copilot")

with open(settings_path) as f:
    cfg = json.load(f)

hooks = cfg.setdefault("hooks", {})

def make_entry(cmd_name, matcher=None):
    entry = {"hooks": [{"type": "command", "command": os.path.join(scripts_dir, cmd_name)}]}
    if matcher:
        entry["matcher"] = matcher
    return entry

def find_entry_status(arr, cmd_name, matcher):
    # 返回 (status, index)。status: 'missing' | 'match' | 'stale'
    # 'match' = 脚本名命中且 matcher 字面相等；'stale' = 脚本名命中但 matcher 不同
    for i, item in enumerate(arr):
        for h in item.get("hooks", []):
            if cmd_name in h.get("command", ""):
                if item.get("matcher") == matcher:
                    return ("match", i)
                return ("stale", i)
    return ("missing", -1)

# 元组第 4 字段 plan_filter：""=通用，"copilot"=仅 copilot 方案装
# 未来加新方案（gemini / openrouter 等）：plan_filter 可写 "copilot,gemini" 这种逗号分隔
# 字符串，过滤逻辑改 `plan in plan_filter.split(',')` 即可保持兼容
registrations = [
    ("Stop",           "builder-loop-stop.sh",      None,                    ""),
    ("SubagentStart",  "subagent-start-guard.sh",   None,                    ""),
    ("SubagentStop",   "tester-lock-clear.sh",      None,                    ""),
    ("PreToolUse",     "tester-lock-check.sh",      "Read|Grep|Glob",        ""),
    ("PreToolUse",     "worktree-write-guard.sh",   "Write|Edit|MultiEdit",  ""),
    ("PreToolUse",     "reviewer-timing-check.sh",  "Agent",                 ""),
]

# V3.1: remove deprecated hooks from previous installs
deprecated = [
    ("SubagentStart", "tester-lock-write.sh"),
    ("SubagentStop",  "tester-lock-clear.sh"),  # matcher changed, remove stale entry
    ("PreToolUse",    "tester-write-guard.sh"),
]
for hook_type, old_cmd in deprecated:
    arr = hooks.get(hook_type, [])
    hooks[hook_type] = [
        item for item in arr
        if not any(old_cmd in h.get("command", "") for h in item.get("hooks", []))
    ]

added = 0
updated = 0
skipped = 0
for hook_type, cmd_name, matcher, plan_filter in registrations:
    if plan_filter and plan_filter != plan:
        skipped += 1
        continue
    arr = hooks.setdefault(hook_type, [])
    status, idx = find_entry_status(arr, cmd_name, matcher)
    if status == "missing":
        arr.append(make_entry(cmd_name, matcher))
        added += 1
    elif status == "stale":
        arr.pop(idx)
        arr.append(make_entry(cmd_name, matcher))
        updated += 1
    # 'match' 则跳过

applicable = len(registrations) - skipped
existed = applicable - added - updated

with open(out_path, "w") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
    f.write("\n")

print(f"✓ hooks ({plan} plan): {added} 条新增，{updated} 条更新，{existed} 条已存在，{skipped} 条跳过（不属于本方案）")
PYEOF
    then
      echo "❌ python3 改写失败，未触碰原 settings.json" >&2
      rm -f "$SETTINGS_TMP"
      exit 1
    fi

    # ---- 5.4 写后自检：tmp 必须合法 JSON，否则不替换 ----
    if ! python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$SETTINGS_TMP" 2>/dev/null; then
      echo "❌ 写入产物非法 JSON，已丢弃，settings.json 未变化" >&2
      rm -f "$SETTINGS_TMP"
      exit 1
    fi

    # ---- 5.5 原子替换（mv 在同一文件系统内是 rename，无半写状态）----
    mv "$SETTINGS_TMP" "$SETTINGS"

    # ---- 5.6 终检：再读一次目标文件，万一 mv 之后有外部干扰也能发现 ----
    if ! python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$SETTINGS" 2>/dev/null; then
      echo "❌ settings.json 终检失败（写入后被外部破坏？），从 .bak 回滚" >&2
      cp "${SETTINGS}.bak" "$SETTINGS"
      exit 1
    fi
  fi
fi

echo ""
echo "✅ cc-builder-loop 安装完成"
echo "   skill: ~/.claude/skills/builder-loop/"
echo "   scripts: ~/.claude/scripts/builder-loop-stop.sh + subagent-start-guard.sh + tester-lock-*.sh + worktree-write-guard.sh + reviewer-timing-check.sh"
echo "   agents: ~/.claude/agents/tester.md + arbiter.md"
