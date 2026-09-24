"""B7：install.sh 要把 SendMessage 接进某条 PreToolUse matcher（供 hooks.py 记 resume_request），
把 TaskStop 接进某条 PostToolUse matcher（与 Bash 同一条，供角色后台任务的登记/停止）；hook 总数仍是
10 条。doctor 要能发现 settings.json 里 SendMessage 缺失。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"


def _run_install(claude_home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CLAUDE_HOME"] = str(claude_home)
    return subprocess.run(["bash", str(INSTALL_SH)], cwd=str(REPO_ROOT), env=env,
                          capture_output=True, text=True, timeout=60)


def _bl_entries(home: Path) -> list[dict]:
    data = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    out = []
    for ev, matchers in data.get("hooks", {}).items():
        for m in matchers:
            for h in m.get("hooks", []):
                if "bl-hook.sh" in h.get("command", ""):
                    out.append({"event": ev, "matcher": m.get("matcher") or ""})
    return out


def _run_doctor(claude_home: Path, bl_home: Path) -> dict:
    env = dict(os.environ, CLAUDE_HOME=str(claude_home), BUILDER_LOOP_HOME=str(bl_home))
    out = subprocess.run([str(REPO_ROOT / "bin" / "bl"), "doctor"], env=env, capture_output=True, text=True, timeout=60)
    return json.loads(out.stdout)


def test_b7_install_wires_sendmessage_and_taskstop(tmp_path):
    home = tmp_path / "claude_home"
    r = _run_install(home)
    assert r.returncode == 0, r.stderr

    entries = _bl_entries(home)
    assert len(entries) == 10, entries

    pre_matchers = [set(e["matcher"].split("|")) for e in entries if e["event"] == "PreToolUse"]
    assert any("SendMessage" in ms for ms in pre_matchers), pre_matchers

    post_matchers = [set(e["matcher"].split("|")) for e in entries if e["event"] == "PostToolUse"]
    assert any({"Bash", "TaskStop"} <= ms for ms in post_matchers), post_matchers

    rep = _run_doctor(home, tmp_path / "blhome")
    assert rep["healthy"] is True, rep


def test_b7_boundary_repeated_install_is_idempotent(tmp_path):
    home = tmp_path / "claude_home"
    _run_install(home)
    n1 = len(_bl_entries(home))
    r2 = _run_install(home)
    assert r2.returncode == 0, r2.stderr
    assert len(_bl_entries(home)) == n1 == 10


def test_b7_doctor_flags_missing_sendmessage_matcher(tmp_path):
    home = tmp_path / "claude_home"
    _run_install(home)
    settings_path = home / "settings.json"
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    for ev, matchers in data.get("hooks", {}).items():
        for m in matchers:
            if m.get("matcher") and "SendMessage" in m["matcher"].split("|"):
                parts = [p for p in m["matcher"].split("|") if p != "SendMessage"]
                m["matcher"] = "|".join(parts)
    settings_path.write_text(json.dumps(data), encoding="utf-8")

    rep = _run_doctor(home, tmp_path / "blhome")
    assert rep["healthy"] is False, rep
    assert any("SendMessage" in p for p in rep["problems"]), rep["problems"]
