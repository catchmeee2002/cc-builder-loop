"""install.sh 按 HOOK_MARKER(bl-hook.sh) 去重：无论仓库路径是否含 builder-loop，重复安装恰好一套 hook。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_DIST = {"SessionStart": 1, "Stop": 1, "SubagentStart": 1, "SubagentStop": 1, "PreToolUse": 3, "PostToolUse": 1, "UserPromptSubmit": 1}


def _copy_repo(dst: Path) -> Path:
    shutil.copytree(REPO_ROOT, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"), symlinks=True)
    return dst


def _install(clone: Path, home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, CLAUDE_HOME=str(home))
    return subprocess.run(["bash", str(clone / "install.sh")], cwd=str(clone), env=env, capture_output=True, text=True, timeout=90)


def _bl_hooks(home: Path) -> list[tuple[str, str]]:
    data = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    return [(ev, h["command"]) for ev, ms in data.get("hooks", {}).items() for m in ms for h in m.get("hooks", []) if "bl-hook.sh" in h.get("command", "")]


def _all_commands(home: Path, event: str) -> list[str]:
    data = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    return [h["command"] for m in data.get("hooks", {}).get(event, []) for h in m.get("hooks", [])]


def _baks(home: Path) -> list[Path]:
    return sorted(home.glob("settings.json.bak.*"))


def _check_exact(home: Path, clone: Path) -> None:
    hooks = _bl_hooks(home)
    assert len(hooks) == 9, hooks
    prefix = str(clone / "hooks" / "bl-hook.sh")
    assert all(cmd.startswith(prefix) for _, cmd in hooks), hooks
    assert dict(Counter(ev for ev, _ in hooks)) == EXPECTED_DIST


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    c = _copy_repo(tmp_path / "clone")
    assert "builder-loop" not in str(c)
    return c


def test_b1_three_installs_exactly_nine(clone: Path, tmp_path: Path) -> None:
    home = tmp_path / "claude_home"
    r = _install(clone, home)
    assert r.returncode == 0, r.stderr
    _check_exact(home, clone)
    first = (home / "settings.json").read_bytes()
    baks = _baks(home)
    for _ in range(2):
        r = _install(clone, home)
        assert r.returncode == 0, r.stderr
        _check_exact(home, clone)
        assert (home / "settings.json").read_bytes() == first
        assert _baks(home) == baks


def test_b1_replaces_other_checkout_and_v7_and_keeps_user_hooks(clone: Path, tmp_path: Path) -> None:
    home = tmp_path / "claude_home"
    home.mkdir()
    seed = {"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": "/some/other/path/hooks/bl-hook.sh Stop", "timeout": 20}]},
        {"hooks": [{"type": "command", "command": "/x/.claude/scripts/builder-loop-stop.sh", "timeout": 20}]},
        {"matcher": "keepme", "hooks": [{"type": "command", "command": "/usr/local/bin/my-hook.sh", "timeout": 7}]},
    ]}}
    (home / "settings.json").write_text(json.dumps(seed), encoding="utf-8")
    for i in range(3):
        r = _install(clone, home)
        assert r.returncode == 0, r.stderr
        _check_exact(home, clone)
        if i == 0:
            first = (home / "settings.json").read_bytes()
            baks = _baks(home)
        else:
            assert (home / "settings.json").read_bytes() == first
            assert _baks(home) == baks
    cmds = _all_commands(home, "Stop")
    assert not any("/some/other/path" in c for c in cmds)
    assert not any("builder-loop-stop.sh" in c for c in cmds)
    data = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    mine = [m for m in data["hooks"]["Stop"] if any(h["command"] == "/usr/local/bin/my-hook.sh" for h in m["hooks"])]
    assert mine == [seed["hooks"]["Stop"][2]]


def test_b1_repo_path_containing_builder_loop(tmp_path: Path) -> None:
    c = _copy_repo(tmp_path / "builder-loop-copy")
    home = tmp_path / "claude_home"
    for _ in range(3):
        r = _install(c, home)
        assert r.returncode == 0, r.stderr
        _check_exact(home, c)


def test_b1_doctor_reports_nine(clone: Path, tmp_path: Path) -> None:
    home = tmp_path / "claude_home"
    seed = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/some/other/path/hooks/bl-hook.sh Stop"}]}]}}
    home.mkdir()
    (home / "settings.json").write_text(json.dumps(seed), encoding="utf-8")
    assert _install(clone, home).returncode == 0
    env = dict(os.environ, CLAUDE_HOME=str(home))
    out = subprocess.run([str(clone / "bin" / "bl"), "doctor"], env=env, capture_output=True, text=True, timeout=60).stdout
    assert len(json.loads(out)["hooks"]["registered"]) == 9
