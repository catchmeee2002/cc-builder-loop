#!/usr/bin/env bash
# builder-loop V8 安装：软链 agents / skills / bin，注册 hooks，清理旧版断链与幽灵 hook。幂等。
set -euo pipefail
REPO="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
CLAUDE_HOME="${CLAUDE_HOME:-$HOME/.claude}"

python3 - "$REPO" "$CLAUDE_HOME" <<'PY'
import json, os, shutil, sys, tempfile, time
from pathlib import Path

repo = Path(sys.argv[1]); home = Path(sys.argv[2])
home.mkdir(parents=True, exist_ok=True)
report = {"symlinks": [], "removed_symlinks": [], "removed_hooks": 0, "hooks": []}

# 1. 清理指向本项目旧 checkout 的断链
for sub in ("agents", "skills", "commands", "scripts", "bin"):
    d = home / sub
    if not d.is_dir():
        continue
    for entry in d.iterdir():
        if entry.is_symlink() and not entry.exists() and "builder-loop" in os.readlink(entry):
            entry.unlink(); report["removed_symlinks"].append(str(entry))

# 2. 软链
links = {
    home / "agents" / "tester.md": repo / "agents" / "tester.md",
    home / "agents" / "reviewer.md": repo / "agents" / "reviewer.md",
    home / "skills" / "builder": repo / "skills" / "builder",
    home / "skills" / "planner": repo / "skills" / "planner",
    home / "skills" / "builder-loop": repo / "skills" / "builder-loop",
    home / "bin" / "bl": repo / "bin" / "bl",
}
for dst, src in links.items():
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() or dst.exists():
        if dst.is_dir() and not dst.is_symlink():
            raise SystemExit(f"{dst} 是实体目录，请先手动移走")
        dst.unlink()
    dst.symlink_to(src)
    report["symlinks"].append(f"{dst} -> {src}")

# 3. hooks
settings = home / "settings.json"
data = json.loads(settings.read_text(encoding="utf-8")) if settings.is_file() else {}
if settings.is_file():
    backup = settings.with_name(f"settings.json.bak.{time.strftime('%Y%m%d%H%M%S')}")
    shutil.copy2(settings, backup)
hooks = data.setdefault("hooks", {})
for ev in list(hooks):
    kept = []
    for m in hooks[ev]:
        hs = [h for h in m.get("hooks", []) if "builder-loop" not in h.get("command", "")]
        report["removed_hooks"] += len(m.get("hooks", [])) - len(hs)
        if hs:
            m["hooks"] = hs; kept.append(m)
    if kept:
        hooks[ev] = kept
    else:
        del hooks[ev]

script = str(repo / "hooks" / "bl-hook.sh")
spec = [
    ("Stop", None, 20),
    ("SubagentStart", "tester|reviewer", 10),
    ("SubagentStop", "tester|reviewer", 120),
    ("PreToolUse", "AskUserQuestion", 5),
    ("PreToolUse", "EnterWorktree", 5),
    ("PreToolUse", "Write|Edit|MultiEdit", 5),
    ("PostToolUse", "AskUserQuestion", 5),
    ("UserPromptSubmit", None, 5),
]
for ev, matcher, timeout in spec:
    entry = {"hooks": [{"type": "command", "command": f"{script} {ev}", "timeout": timeout}]}
    if matcher:
        entry["matcher"] = matcher
    hooks.setdefault(ev, []).append(entry)
    report["hooks"].append(f"{ev}{' [' + matcher + ']' if matcher else ''}")

fd, tmp = tempfile.mkstemp(prefix=".settings.", suffix=".tmp", dir=str(home))
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2, ensure_ascii=False); fh.write("\n")
json.loads(Path(tmp).read_text(encoding="utf-8"))
os.replace(tmp, settings)
print(json.dumps(report, ensure_ascii=False, indent=2))
PY

chmod +x "$REPO/bin/bl" "$REPO/hooks/bl-hook.sh"
echo
echo "== bl doctor =="
"$REPO/bin/bl" doctor | python3 -c "import json,sys;d=json.load(sys.stdin);print('healthy' if d['healthy'] else 'problems: '+'; '.join(d['problems']))"
echo "把 $CLAUDE_HOME/bin 加进 PATH 后可直接用 bl；新开 Claude Code session 使 skills 重新发现。"
