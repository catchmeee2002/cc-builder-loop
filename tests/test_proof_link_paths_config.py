"""B1：loop.yml 的 proof_runner 支持 link_paths，冻结进 assurance 面。

起点代码只认 framework / cmd 两个键，link_paths 被静默丢弃，所以下面每条正向断言在起点上都会在
call 阶段失败（baseline-red）。framework / cmd 的既有校验与「改 proof_runner = ASSURANCE_DOWNGRADE」
是起点上已成立的行为，作为守卫不列入 proof_spec（由 machine 全量跑）。
"""

from __future__ import annotations

import copy

import pytest

from builder_loop import contract as C
from builder_loop.config import load_loop_config
from builder_loop.errors import Problem

from conftest import CONTRACT, PROOF_RUNNER_CMD

BASE = 'pass_cmd:\n  - stage: t\n    cmd: "true"\n'


def _write(repo, proof_runner: str) -> None:
    (repo.root / ".claude" / "loop.yml").write_text(BASE + proof_runner, encoding="utf-8")


def _frozen_runner(repo) -> dict:
    """解析 loop.yml → 冻结进 assurance 的 proof_runner（与 start 同一条路径）。"""
    cfg = load_loop_config(repo.root)
    return C.freeze_assurance(copy.deepcopy(CONTRACT), cfg)["assurance"]["proof_runner"]


def _runner(extra: str) -> str:
    return 'proof_runner:\n  framework: pytest\n  cmd: "python3 -m pytest"\n' + extra


# ---------------------------------------------------------------- 正向（baseline-red）


def test_link_paths_declared_is_frozen_into_proof_runner(repo):
    _write(repo, _runner('  link_paths: ["node_modules"]\n'))
    runner = _frozen_runner(repo)
    assert runner["link_paths"] == ["node_modules"]
    # framework / cmd 既有键一个不少，值不变
    assert runner["framework"] == "pytest" and runner["cmd"] == "python3 -m pytest"


def test_link_paths_absent_keeps_runner_byte_identical(repo):
    """未声明时冻结的 proof_runner 与升级前完全一致：只有 framework 与 cmd 两个键。"""
    assert set(load_loop_config(repo.root).proof_runner) == {"framework", "cmd"}
    assert load_loop_config(repo.root).proof_runner == {"framework": "pytest", "cmd": PROOF_RUNNER_CMD}


def test_link_paths_empty_array_is_frozen_as_empty_array(repo):
    """边界：空数组等同未声明（不建任何链接），但冻结值是一个空数组。"""
    _write(repo, _runner("  link_paths: []\n"))
    runner = _frozen_runner(repo)
    assert runner["link_paths"] == []


def test_link_paths_duplicates_keep_declaration_order(repo):
    """边界：重复项允许，按声明顺序保留。"""
    _write(repo, _runner('  link_paths: ["vendor", "node_modules", "vendor"]\n'))
    assert _frozen_runner(repo)["link_paths"] == ["vendor", "node_modules", "vendor"]


def test_link_paths_trailing_slash_is_normalized(repo):
    """边界：路径用正斜杠，末尾斜杠被接受并规整掉。"""
    _write(repo, _runner('  link_paths: ["node_modules/", "vendor/deps/"]\n'))
    assert _frozen_runner(repo)["link_paths"] == ["node_modules", "vendor/deps"]


@pytest.mark.parametrize("value", [
    '"node_modules"',                # 不是数组
    '["node_modules", 123]',         # 元素不是字符串
    '[""]',                          # 空串
    '["/opt/deps"]',                 # 绝对路径
    '["vendor/../node_modules"]',    # 含 `..` 段
    '[".git/config"]',               # 以 `.git` 开头
])
def test_invalid_link_paths_is_config_proof_runner_invalid(repo, value):
    _write(repo, _runner(f"  link_paths: {value}\n"))
    with pytest.raises(Problem) as ei:
        load_loop_config(repo.root)
    assert ei.value.code == "CONFIG_PROOF_RUNNER_INVALID"


# ---------------------------------------------------------------- 守卫：不变量（不列入 proof_spec）


def test_guard_framework_and_cmd_validation_unchanged(repo):
    """不变量：framework / cmd 的既有校验与错误码不变（generic 缺 cmd 仍拒）。"""
    _write(repo, "proof_runner:\n  framework: generic\n")
    with pytest.raises(Problem) as ei:
        load_loop_config(repo.root)
    assert ei.value.code == "CONFIG_PROOF_RUNNER_INVALID"
    _write(repo, "proof_runner:\n  framework: junit5\n  cmd: x\n")
    with pytest.raises(Problem):
        load_loop_config(repo.root)


def test_changing_link_paths_is_assurance_downgrade(repo):
    """改 link_paths 属于 proof_runner 变化，仍走 ASSURANCE_DOWNGRADE 授权。"""
    _write(repo, _runner('  link_paths: ["node_modules"]\n'))
    old = C.freeze_assurance(copy.deepcopy(CONTRACT), load_loop_config(repo.root))
    _write(repo, _runner('  link_paths: ["node_modules", "vendor"]\n'))
    new = C.freeze_assurance(copy.deepcopy(CONTRACT), load_loop_config(repo.root))
    assert old["assurance"]["proof_runner"] != new["assurance"]["proof_runner"]
    assert C.CHANGE_ASSURANCE_DOWNGRADE in C.classify_change(old, new)
