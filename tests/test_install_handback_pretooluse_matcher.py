"""B9: install.sh 要把 SubagentHandback 接进 PreToolUse 的 builder-loop matcher（供新的
投递前 gate 生效），hook 总数仍是 10 条；重复 install 幂等，matcher 不重复。手工把
settings.json 里 PreToolUse 的 matcher 去掉 SubagentHandback 后运行 `bl doctor`：报不健康，
输出中点名 SubagentHandback。PostToolUse 的 SubagentHandback|Bash|TaskStop matcher 与其余
hook 不变。

覆盖对象：install.sh 的 PreToolUse builder-loop matcher 列表、doctor.py::REQUIRED_MATCHERS——
冻结基线上 install.sh 的 PreToolUse matcher 缺 SubagentHandback，下面的断言在起点代码上会在
call 阶段直接失败（baseline-red：真实运行 install.sh / bl doctor，不需要 import 新符号）。
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


def test_b9_install_wires_subagenthandback_into_pretooluse(tmp_path):
    home = tmp_path / "claude_home"
    r = _run_install(home)
    assert r.returncode == 0, r.stderr

    entries = _bl_entries(home)
    assert len(entries) == 10, entries

    pre_matchers = [set(e["matcher"].split("|")) for e in entries if e["event"] == "PreToolUse"]
    assert any("SubagentHandback" in ms for ms in pre_matchers), pre_matchers

    post_matchers = [set(e["matcher"].split("|")) for e in entries if e["event"] == "PostToolUse"]
    assert any({"SubagentHandback", "Bash", "TaskStop"} <= ms for ms in post_matchers), post_matchers


def test_b9_boundary_repeated_install_idempotent(tmp_path):
    home = tmp_path / "claude_home"
    _run_install(home)
    n1 = len(_bl_entries(home))
    r2 = _run_install(home)
    assert r2.returncode == 0, r2.stderr
    entries2 = _bl_entries(home)
    assert len(entries2) == n1 == 10
    pre_matchers = [e["matcher"].split("|") for e in entries2 if e["event"] == "PreToolUse"]
    for m in pre_matchers:
        assert m.count("SubagentHandback") <= 1, m


def test_b9_doctor_flags_missing_subagenthandback_in_pretooluse(tmp_path):
    home = tmp_path / "claude_home"
    _run_install(home)
    settings_path = home / "settings.json"
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    for ev, matchers in data.get("hooks", {}).items():
        if ev != "PreToolUse":
            continue
        for m in matchers:
            if m.get("matcher") and "SubagentHandback" in m["matcher"].split("|"):
                parts = [p for p in m["matcher"].split("|") if p != "SubagentHandback"]
                m["matcher"] = "|".join(parts)
    settings_path.write_text(json.dumps(data), encoding="utf-8")

    rep = _run_doctor(home, tmp_path / "blhome")
    assert rep["healthy"] is False, rep
    assert any("SubagentHandback" in p for p in rep["problems"]), rep["problems"]
