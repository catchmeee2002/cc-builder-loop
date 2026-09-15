"""doctor：只读诊断，不修复。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import ledger as ledger_mod
from .jsonutil import read_json

HOOK_MARKER = "bl-hook.sh"


def _claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_HOME") or (Path.home() / ".claude"))


def _settings_path() -> Path:
    return _claude_home() / "settings.json"


def _check_hooks() -> dict[str, Any]:
    p = _settings_path()
    if not p.is_file():
        return {"settings": str(p), "registered": [], "error": "settings.json 不存在"}
    try:
        data = read_json(p)
    except Exception as exc:  # noqa: BLE001
        return {"settings": str(p), "registered": [], "error": f"settings.json 无法解析: {exc}"}
    registered, broken = [], []
    for event, matchers in (data.get("hooks") or {}).items():
        for m in matchers:
            for h in m.get("hooks", []):
                cmd = h.get("command", "")
                if HOOK_MARKER in cmd:
                    registered.append({"event": event, "matcher": m.get("matcher"), "command": cmd})
                    script = cmd.split()[0]
                    if not Path(os.path.expanduser(script)).exists():
                        broken.append(cmd)
    return {"settings": str(p), "registered": registered, "broken": broken}


def _check_sessions() -> dict[str, Any]:
    d = ledger_mod.sessions_dir()
    orphans, active = [], []
    if d.is_dir():
        for f in d.glob("*.json"):
            try:
                ptr = read_json(f)
                lg = ledger_mod.load(Path(ptr["ledger_path"]))
                if ledger_mod.is_terminal(lg):
                    orphans.append({"file": str(f), "reason": "run 已终态"})
                else:
                    active.append({"session_id": ptr["session_id"], "run_id": ptr["run_id"], "repo_root": ptr["repo_root"]})
            except Exception as exc:  # noqa: BLE001
                orphans.append({"file": str(f), "reason": str(exc)})
    return {"dir": str(d), "active": active, "orphans": orphans}


def _check_symlinks() -> list[dict[str, Any]]:
    out = []
    home = _claude_home()
    for sub in ("agents", "skills", "commands", "scripts", "bin"):
        d = home / sub
        if not d.is_dir():
            continue
        for entry in d.iterdir():
            if entry.is_symlink() and not entry.exists():
                out.append({"path": str(entry), "target": os.readlink(entry)})
    return out


def doctor(repo_arg: str | None) -> dict[str, Any]:
    git_version = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout.strip()
    report: dict[str, Any] = {
        "python": sys.version.split()[0],
        "git": git_version,
        "bl_on_path": shutil.which("bl"),
        "hooks": _check_hooks(),
        "sessions": _check_sessions(),
        "broken_symlinks": _check_symlinks(),
    }
    if repo_arg:
        from .run import resolve_repo_root

        try:
            root = resolve_repo_root(repo_arg)
            report["repo"] = {"root": str(root), "runs": ledger_mod.list_runs(root)}
        except Exception as exc:  # noqa: BLE001
            report["repo"] = {"error": str(exc)}
    problems = []
    if report["hooks"].get("error") or not report["hooks"]["registered"]:
        problems.append("hooks 未注册（运行 install.sh）")
    if report["hooks"].get("broken"):
        problems.append("hook 脚本路径失效")
    if report["sessions"]["orphans"]:
        problems.append("存在孤儿 session 指针（可安全删除）")
    if report["broken_symlinks"]:
        problems.append("~/.claude 下有断链")
    report["problems"] = problems
    report["healthy"] = not problems
    return report
