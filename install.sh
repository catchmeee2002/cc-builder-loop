#!/usr/bin/env bash
# builder-loop V8 安装：软链 agents / skills / bin，注册 hooks，清理旧版断链与幽灵 hook。幂等。
set -euo pipefail
REPO="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
CLAUDE_HOME="${CLAUDE_HOME:-$HOME/.claude}"

python3 - "$REPO" "$CLAUDE_HOME" <<'PY'
import json, os, shutil, sys, tempfile, time
from pathlib import Path

repo = Path(sys.argv[1]); home = Path(sys.argv[2])
sys.path.insert(0, str(repo / "runtime"))
from builder_loop.doctor import HOOK_MARKER  # 「哪些 hook 是本版自家的」只有 doctor 这一个判据
# V7 及更早版本的 hook 命令是 scripts/builder-loop-*.sh，不含 HOOK_MARKER；仅作退役清理，不用于识别本版 hook
LEGACY_MARKER = "builder-loop"
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
    home / "skills" / "file-issue": repo / "skills" / "file-issue",
    home / "bin" / "bl": repo / "bin" / "bl",
    home / "doc-policy.md": repo / "docs" / "doc-policy.md",
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
old_text = settings.read_text(encoding="utf-8") if settings.is_file() else None
data = json.loads(old_text) if old_text is not None else {}
hooks = data.setdefault("hooks", {})
for ev in list(hooks):
    kept = []
    for m in hooks[ev]:
        hs = [h for h in m.get("hooks", []) if HOOK_MARKER not in h.get("command", "") and LEGACY_MARKER not in h.get("command", "")]
        report["removed_hooks"] += len(m.get("hooks", [])) - len(hs)
        if hs:
            m["hooks"] = hs; kept.append(m)
    if kept:
        hooks[ev] = kept
    else:
        del hooks[ev]

script = str(repo / "hooks" / "bl-hook.sh")
spec = [
    # 把本仓 bin/ 写进会话 PATH（经 CLAUDE_ENV_FILE），SKILL 与 runtime 提示里的裸 `bl` 才能直接用
    ("SessionStart", None, 5),
    ("Stop", None, 20),
    ("SubagentStart", "tester|reviewer", 10),
    ("SubagentStop", "tester|reviewer", 120),
    ("PreToolUse", "AskUserQuestion", 5),
    # SubagentHandback：角色交卷投递前的校验，不合规就 deny（#303）；要校验 proof_spec 与 patch，超时放宽
    ("PreToolUse", "EnterWorktree|SubagentHandback", 120),
    # tester 首次 integrate 前的读隔离 + 角色写边界 + 心跳续租；SendMessage = builder 续接角色的 intent（#306）。
    # 高频工具，靠 bl-hook.sh 的纯 bash 快速路径兜成本
    ("PreToolUse", "Read|Grep|Glob|Write|Edit|MultiEdit|NotebookEdit|Bash|SendMessage", 5),
    ("PostToolUse", "AskUserQuestion", 5),
    # 角色结果的唯一登记点：tester 登记要提交 worktree + 校验 proof_spec，超时与 SubagentStop 同级。
    # Bash：角色前台命令超时被转后台（backgroundTaskId）；TaskStop：builder 停掉它（#283 #308）
    ("PostToolUse", "SubagentHandback|Bash|TaskStop", 120),
    ("UserPromptSubmit", None, 5),
]
for ev, matcher, timeout in spec:
    entry = {"hooks": [{"type": "command", "command": f"{script} {ev}", "timeout": timeout}]}
    if matcher:
        entry["matcher"] = matcher
    hooks.setdefault(ev, []).append(entry)
    report["hooks"].append(f"{ev}{' [' + matcher + ']' if matcher else ''}")

new_text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
if new_text != old_text:
    if old_text is not None:
        backup = settings.with_name(f"settings.json.bak.{time.strftime('%Y%m%d%H%M%S')}")
        shutil.copy2(settings, backup)
    fd, tmp = tempfile.mkstemp(prefix=".settings.", suffix=".tmp", dir=str(home))
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    json.loads(Path(tmp).read_text(encoding="utf-8"))
    os.replace(tmp, settings)
print(json.dumps(report, ensure_ascii=False, indent=2))
PY

chmod +x "$REPO/bin/bl" "$REPO/hooks/bl-hook.sh"
echo
echo "== bl doctor =="
"$REPO/bin/bl" doctor | python3 -c "import json,sys;d=json.load(sys.stdin);print('healthy' if d['healthy'] else 'problems: '+'; '.join(d['problems']))"
echo "把 $CLAUDE_HOME/bin 加进 PATH 后可直接用 bl；新开 Claude Code session 使 skills 重新发现。"
