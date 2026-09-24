"""B5: 空 CLAUDE_HOME 上跑 install.sh，settings.json 里 bl-hook.sh 的注册恰为 10 条，
依次与 HOOK_SPEC 一一对应；在已安装的 CLAUDE_HOME 上重跑，settings.json 逐字节不变、不产生新备份。

install.sh 当前用内联列表生成这 10 条注册，字面内容已经与 HOOK_SPEC 一致，所以这里的黑盒断言
在起点上就会通过——这条行为要证明的是"注册表只有一份、install.sh 从 hookspec.HOOK_SPEC 派生"，
只有对着实现打 mutation 才能鉴别（例如改坏其中一条 matcher 或 timeout），走 mutation。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from conftest import HOOK_SPEC, ROOT

INSTALL_SH = ROOT / "install.sh"


def _run_install(claude_home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, CLAUDE_HOME=str(claude_home))
    return subprocess.run(["bash", str(INSTALL_SH)], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=90)


def _bl_registrations(home: Path) -> list[tuple[str, str | None, int]]:
    data = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    out: list[tuple[str, str | None, int]] = []
    for ev, matchers in data.get("hooks", {}).items():
        for m in matchers:
            for h in m.get("hooks", []):
                if "bl-hook.sh" in h.get("command", ""):
                    out.append((ev, m.get("matcher"), h.get("timeout")))
    return out


def test_b5_fresh_install_matches_hook_spec_exactly(tmp_path):
    home = tmp_path / "claude_home"
    r = _run_install(home)
    assert r.returncode == 0, r.stderr

    regs = _bl_registrations(home)
    assert len(regs) == 10, regs
    assert tuple(regs) == HOOK_SPEC, (regs, HOOK_SPEC)


def test_b5_boundary_reinstall_is_byte_identical_no_backup(tmp_path):
    home = tmp_path / "claude_home"
    _run_install(home)
    before = (home / "settings.json").read_bytes()
    before_baks = sorted(home.glob("settings.json.bak.*"))

    r2 = _run_install(home)
    assert r2.returncode == 0, r2.stderr
    after = (home / "settings.json").read_bytes()
    after_baks = sorted(home.glob("settings.json.bak.*"))

    assert after == before
    assert after_baks == before_baks
