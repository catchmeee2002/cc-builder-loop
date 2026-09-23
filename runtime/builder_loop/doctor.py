"""doctor：只读诊断，不修复。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import ledger as ledger_mod
from .errors import Problem
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
    orphans, active, retro_pending = [], [], []
    if d.is_dir():
        for f in d.glob("*.json"):
            try:
                ptr = read_json(f)
                lg = ledger_mod.peek(Path(ptr["ledger_path"]))
                if lg is None:
                    orphans.append({"file": str(f), "reason": "ledger 不存在或不可读"})
                    continue
                info = {"session_id": ptr["session_id"], "run_id": ptr["run_id"], "repo_root": ptr["repo_root"]}
                if lg.get("schema") in ledger_mod.LEGACY_SCHEMAS:
                    orphans.append({"file": str(f), "reason": f"旧版 ledger（{lg.get('schema')}），hook 对它静默；用 `bl abandon --run` 结束它"})
                elif not lg.get("terminal"):
                    active.append(info)
                elif not lg.get("retrospective"):
                    retro_pending.append(info)  # 终态但未复盘：绑定是有意保留的，不是孤儿
                else:
                    orphans.append({"file": str(f), "reason": "run 已终态且已复盘"})
            except Exception as exc:  # noqa: BLE001
                orphans.append({"file": str(f), "reason": str(exc)})
    return {"dir": str(d), "active": active, "retro_pending": retro_pending, "orphans": orphans}


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


def _check_proof_runner(root: Path) -> dict[str, Any]:
    """proof 门禁的命令在这台机器上起不起得来。start 时才发现就已经晚了（#241）。"""
    from .config import load_loop_config
    from .proof import smoke_runner

    try:
        runner = load_loop_config(root).proof_runner
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"loop.yml 不可用: {exc}"}
    try:
        smoke_runner(runner, root)
    except Problem as exc:
        return {"ok": False, "cmd": runner.get("cmd"), "error": exc.message, "hint": exc.details.get("hint")}
    return {"ok": True, "cmd": runner.get("cmd")}


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
            report["repo"] = {"root": str(root), "runs": ledger_mod.list_runs(root), "proof_runner": _check_proof_runner(root)}
        except Exception as exc:  # noqa: BLE001
            report["repo"] = {"error": str(exc)}
    problems = []
    if report["hooks"].get("error") or not report["hooks"]["registered"]:
        problems.append("hooks 未注册（运行 install.sh）")
    if report["hooks"].get("broken"):
        problems.append("hook 脚本路径失效")
    if report["sessions"]["orphans"]:
        problems.append("存在孤儿 session 指针（可安全删除）")
    if report["sessions"]["retro_pending"]:
        problems.append("有 run 已结束但尚未复盘（`bl retro signals --run <id>`）")
    if report["broken_symlinks"]:
        problems.append("~/.claude 下有断链")
    pr = (report.get("repo") or {}).get("proof_runner") or {}
    if pr and not pr.get("ok"):
        problems.append(f"proof_runner 跑不起来：{pr.get('error')}（改 .claude/loop.yml 的 proof_runner.cmd）")
    report["problems"] = problems
    report["healthy"] = not problems
    return report
