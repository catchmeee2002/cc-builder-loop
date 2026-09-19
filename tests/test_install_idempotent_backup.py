"""install.sh 幂等备份：只在 settings.json 内容真的要变时才产生 settings.json.bak.*

B1: 重跑一个已安装过的 CLAUDE_HOME 不产生新备份，settings.json 内容逐字节不变。
B2: settings.json 内容与 install.sh 将写入的不同时，先生成恰好一份忠实快照备份，
    再写入含 builder-loop 全部 hook 注册的新内容，用户自己的 hook 予以保留。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"

BAK_RE = re.compile(r"settings\.json\.bak\.\d{14}$")


def _run_install(claude_home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CLAUDE_HOME"] = str(claude_home)
    return subprocess.run(
        ["bash", str(INSTALL_SH)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _bak_files(claude_home: Path) -> list[Path]:
    return sorted(p for p in claude_home.glob("settings.json.bak.*") if BAK_RE.match(p.name))


BL_HOOKS = 10  # install.sh 的 spec 条数；新增 hook 时同步


def _bl_hook_count(data: dict) -> int:
    return sum(
        1
        for entries in data.get("hooks", {}).values()
        for m in entries
        for h in m.get("hooks", [])
        if "bl-hook.sh" in h.get("command", "")
    )


# ---------------------------------------------------------------- B1


def test_b1_no_new_backup_on_unchanged_rerun(tmp_path: Path) -> None:
    home = tmp_path / "claude_home"
    r1 = _run_install(home)
    assert r1.returncode == 0, r1.stderr
    settings = home / "settings.json"
    assert settings.is_file()
    content_before = settings.read_bytes()
    # 首次安装：目标目录之前没有 settings.json，不应该产生备份
    assert _bak_files(home) == []

    r2 = _run_install(home)
    assert r2.returncode == 0, r2.stderr
    assert _bak_files(home) == [], "内容未变的重跑不应该产生新的 settings.json.bak.*"
    assert settings.read_bytes() == content_before


def test_b1_repeated_reruns_no_backup_accumulation(tmp_path: Path) -> None:
    home = tmp_path / "claude_home"
    r0 = _run_install(home)
    assert r0.returncode == 0, r0.stderr
    settings = home / "settings.json"
    content_before = settings.read_bytes()

    for _ in range(3):
        r = _run_install(home)
        assert r.returncode == 0, r.stderr

    assert _bak_files(home) == [], "连续多次重跑都不应该堆积备份"
    assert settings.read_bytes() == content_before


def test_b1_existing_backup_untouched(tmp_path: Path) -> None:
    home = tmp_path / "claude_home"
    r0 = _run_install(home)
    assert r0.returncode == 0, r0.stderr

    # 模拟一份此前真实变更留下的旧备份
    stale_bak = home / "settings.json.bak.20200101000000"
    stale_bak.write_text("stale backup content\n", encoding="utf-8")
    stale_mtime_before = stale_bak.stat().st_mtime
    stale_content_before = stale_bak.read_bytes()

    r1 = _run_install(home)
    assert r1.returncode == 0, r1.stderr

    assert stale_bak.read_bytes() == stale_content_before
    assert stale_bak.stat().st_mtime == stale_mtime_before
    # 内容未变的重跑：唯一存在的备份仍然只是那份旧的，没有新增
    assert _bak_files(home) == [stale_bak]


# ---------------------------------------------------------------- B2


def test_b2_backup_created_when_content_differs(tmp_path: Path) -> None:
    home = tmp_path / "claude_home"
    home.mkdir()
    settings = home / "settings.json"
    original = {
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "echo user-hook", "timeout": 5}]}
            ]
        },
        "myOwnSetting": True,
    }
    settings.write_text(json.dumps(original, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    original_bytes = settings.read_bytes()

    r = _run_install(home)
    assert r.returncode == 0, r.stderr

    baks = _bak_files(home)
    assert len(baks) == 1, f"内容有变化应恰好产生一份备份，实际 {baks}"
    assert baks[0].read_bytes() == original_bytes

    data = json.loads(settings.read_text(encoding="utf-8"))
    up_hooks = [h for m in data["hooks"].get("UserPromptSubmit", []) for h in m.get("hooks", [])]
    assert any(h.get("command") == "echo user-hook" for h in up_hooks), "用户自己的 hook 必须保留"
    assert data.get("myOwnSetting") is True
    assert _bl_hook_count(data) == BL_HOOKS


def test_b2_missing_registration_triggers_backup_and_gets_completed(tmp_path: Path) -> None:
    home = tmp_path / "claude_home"
    home.mkdir()
    settings = home / "settings.json"
    original = {"hooks": {}, "other": 1}
    settings.write_text(json.dumps(original, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    original_bytes = settings.read_bytes()

    r = _run_install(home)
    assert r.returncode == 0, r.stderr

    baks = _bak_files(home)
    assert len(baks) == 1
    assert baks[0].read_bytes() == original_bytes

    data = json.loads(settings.read_text(encoding="utf-8"))
    assert _bl_hook_count(data) == BL_HOOKS
    assert data.get("other") == 1


def test_b2_no_backup_when_settings_missing(tmp_path: Path) -> None:
    home = tmp_path / "claude_home"
    r = _run_install(home)
    assert r.returncode == 0, r.stderr
    assert _bak_files(home) == [], "settings.json 不存在时创建它，不应该产生备份"
    settings = home / "settings.json"
    assert settings.is_file()
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert _bl_hook_count(data) == BL_HOOKS


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_session_start_puts_bl_on_path(tmp_path):
    """#245：SKILL 与 runtime 提示都写裸 `bl`。SessionStart 往 CLAUDE_ENV_FILE 写 export，
    CC 把它注入之后的每条 Bash（主会话与 subagent，2.1.272 实测）。这里验证写出来的那行能用。"""
    repo = Path(__file__).resolve().parents[1]
    env_file = tmp_path / "sessionstart-hook-1.sh"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CLAUDE_ENV_FILE": str(env_file)}
    r = subprocess.run(["bash", str(repo / "hooks" / "bl-hook.sh"), "SessionStart"], input='{"session_id":"unbound"}',
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    found = subprocess.run(["bash", "-c", f"source {env_file}; command -v bl"], capture_output=True, text=True,
                           env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert found.stdout.strip() == str(repo / "bin" / "bl")
    # 没有 CLAUDE_ENV_FILE（老版本 CC）时安静退出，不报错、不阻塞会话
    r2 = subprocess.run(["bash", str(repo / "hooks" / "bl-hook.sh"), "SessionStart"], input="{}", capture_output=True, text=True,
                        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert r2.returncode == 0 and r2.stderr == ""
