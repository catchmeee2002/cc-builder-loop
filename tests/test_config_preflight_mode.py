"""B3：loop.yml 顶层键 `preflight` 决定 LoopConfig.preflight_mode。

缺省 → always；always → always；on-failure → on-failure；off → off。起点 LoopConfig 没有
preflight_mode 属性，取它会 AttributeError → call 阶段失败（baseline-red）；非法的 preflight
值在起点被静默忽略，`CONFIG_PREFLIGHT_INVALID` 的断言同样失败。

边界（非法值报配置错；preflight_mode 不阻止手动 `bl preflight`）与不变量（不写该键时既有解析
结果与 JSON 形状不变）作为守卫留在同一文件，不列入 proof group。
"""

from __future__ import annotations

import pytest

from builder_loop.config import load_loop_config
from builder_loop.errors import Problem

from keypath_helpers import loop_yml, start_with_loop


def _write(repo, text: str) -> None:
    (repo.root / ".claude" / "loop.yml").write_text(text, encoding="utf-8")


@pytest.mark.parametrize("raw,expected", [
    (None, "always"),
    ("always", "always"),
    ("on-failure", "on-failure"),
    ("off", "off"),
])
def test_b3_preflight_mode_parsing(repo, raw, expected):
    _write(repo, loop_yml(preflight=raw))
    cfg = load_loop_config(repo.root)
    assert cfg.preflight_mode == expected


def test_b3_boundary_invalid_preflight_value(repo):
    _write(repo, loop_yml(preflight="sometimes"))
    with pytest.raises(Problem) as ei:
        load_loop_config(repo.root)
    assert ei.value.code == "CONFIG_PREFLIGHT_INVALID", ei.value


def test_b3_boundary_preflight_mode_does_not_block_manual_preflight(repo, cli):
    """preflight_mode 只影响建议：显式 off 时手动 `bl preflight` 照常可跑。"""
    ctx = start_with_loop(repo, cli, loop_yml(preflight="off", stage_cmd="python3 -c 'import sys; sys.exit(0)'"))
    out = cli("preflight", "--session", "S1")
    assert "result" in out, out


def test_b3_invariant_absent_preflight_key_keeps_existing_config(repo):
    """不变量：loop.yml 不写 preflight 时既有字段解析不变，且 JSON 形状不因新键漂移。"""
    _write(repo, loop_yml())
    cfg = load_loop_config(repo.root)
    assert cfg.max_iterations == 5
    assert cfg.pass_cmd[0].stage == "test"
    body = cfg.to_json()
    assert "preflight" not in body and "preflight_mode" not in body, body
    assert "concurrency" not in body, body