"""B3：framework=pytest 时，交卷 / `bl proof` 校验 proof_spec 会把 test_ids 指向的函数 / 类 /
方法在测试文件里核对一遍——文件存在但 node id 指向的函数 / 类 / 方法不存在时 PROOF_SPEC_INVALID，
details.test_ids_missing 列出这些完整 node id；全部存在则通过。

起点代码只核对文件存在，不核对文件里的定义，所以「不存在的函数 / 类 / 方法被拒」在起点上不会抛错、
正向断言于 call 阶段失败（baseline-red）。参数化 / 嵌套类 / async / 继承豁免 / SyntaxError 降级 /
generic 不校验 / 既有拒绝信息不变——都是守卫（不列入 proof group，仍由 machine 全量运行）。
"""

from __future__ import annotations

import pytest

from builder_loop import ledger as L
from builder_loop import proof as P
from builder_loop.errors import Problem

from conftest import make_tester_result, role_turn

NODES = '''\
import pytest


def test_top():
    assert True


class Base:
    def test_inherited(self):
        assert True


class TestInner:
    def test_method(self):
        assert True


class TestDerived(Base):
    pass


class TestOuter:
    class TestNested:
        def test_deep(self):
            assert True


async def test_async_case():
    assert True


@pytest.mark.parametrize("x", [1])
def test_param(x):
    assert x == 1


@pytest.mark.parametrize("x", ["x::y"])
def test_param_colon(x):
    assert x == "x::y"
'''

NODES_FILE = "tests/test_nodes.py"

OPAQUE_IMPORT_FILE = "tests/test_opaque_import.py"
OPAQUE_ALIAS_FILE = "tests/test_opaque_alias.py"
OPAQUE_STAR_FILE = "tests/test_opaque_star.py"
# reviewer finding 2 的回归守卫素材：导入来的类、赋值别名、`from x import *` 都不是「文件里真的没有」。
# 三者拆成互不含星号导入的独立文件（reviewer 复审 finding）：星号导入让 `_module_symbols` 整模块返回
# None，混在一起会让前两条守卫实际走整模块跳过、锁不住 per-symbol opaque 分支；混进 NODES 还会把
# 上面的缺失检测用例一起弄绿。
OPAQUE_IMPORT = '''\
from collections import OrderedDict


class TestLocal:
    def test_ok(self):
        assert True
'''

OPAQUE_ALIAS = '''\
from collections import OrderedDict

ImportedBase = OrderedDict


class TestLocal:
    def test_ok(self):
        assert True
'''

OPAQUE_STAR = '''\
from math import *


class TestLocal:
    def test_ok(self):
        assert True
'''


def _spec(test_ids: list[str]) -> dict:
    return {"groups": [{"kind": "mutation", "behavior_ids": ["B1"], "test_ids": list(test_ids), "timeout": 60}]}


def _check(started, test_ids: list[str], *, worktree_files: bool = True, lg: dict | None = None) -> dict:
    lg = lg or L.load(started["ledger"])
    return P.check_structure(_spec(test_ids), lg, started["repo"].root, worktree_files=worktree_files)


def _write_nodes(started) -> None:
    (started["tester_worktree"] / "tests").mkdir(parents=True, exist_ok=True)
    (started["tester_worktree"] / NODES_FILE).write_text(NODES, encoding="utf-8")


def _write_opaque(started, path: str, body: str) -> None:
    (started["tester_worktree"] / path).write_text(body, encoding="utf-8")


def _assert_missing(excinfo, expected: list[str]) -> None:
    assert excinfo.value.code == "PROOF_SPEC_INVALID", excinfo.value.to_json()
    got = excinfo.value.details.get("test_ids_missing")
    assert isinstance(got, list), excinfo.value.to_json()
    assert sorted(got) == sorted(expected), excinfo.value.to_json()


# ---------------------------------------------------------------- B3：正向（起点上 call 阶段失败）


def test_b3_missing_function_rejected(started):
    _write_nodes(started)
    with pytest.raises(Problem) as ei:
        _check(started, [f"{NODES_FILE}::test_absent"])
    _assert_missing(ei, [f"{NODES_FILE}::test_absent"])


def test_b3_missing_method_rejected(started):
    _write_nodes(started)
    with pytest.raises(Problem) as ei:
        _check(started, [f"{NODES_FILE}::TestInner::test_absent"])
    _assert_missing(ei, [f"{NODES_FILE}::TestInner::test_absent"])


def test_b3_missing_nested_method_rejected(started):
    _write_nodes(started)
    with pytest.raises(Problem) as ei:
        _check(started, [f"{NODES_FILE}::TestOuter::TestNested::test_absent"])
    _assert_missing(ei, [f"{NODES_FILE}::TestOuter::TestNested::test_absent"])


def test_b3_missing_class_rejected(started):
    _write_nodes(started)
    with pytest.raises(Problem) as ei:
        _check(started, [f"{NODES_FILE}::TestAbsent"])
    _assert_missing(ei, [f"{NODES_FILE}::TestAbsent"])


def test_b3_mixed_ids_report_only_the_missing_ones(started):
    _write_nodes(started)
    missing = [f"{NODES_FILE}::test_absent", f"{NODES_FILE}::TestInner::test_absent"]
    present = [f"{NODES_FILE}::test_top", f"{NODES_FILE}::TestInner::test_method"]
    with pytest.raises(Problem) as ei:
        _check(started, present + missing)
    _assert_missing(ei, missing)


def test_b3_head_path_rejects_missing_function(started, hook):
    """proof 入口读的是 tester head（worktree_files=False），不是工作树。"""
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    _write_nodes(started)
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation", test_ids=[f"{NODES_FILE}::test_top"]), start=False)
    assert r["code"] == 0, r
    lg = L.load(started["ledger"])
    assert lg["tester"].get("head"), lg["tester"]
    with pytest.raises(Problem) as ei:
        _check(started, [f"{NODES_FILE}::test_absent"], worktree_files=False, lg=lg)
    _assert_missing(ei, [f"{NODES_FILE}::test_absent"])


# ---------------------------------------------------------------- B3：边界 / 不变量（守卫）


def test_b3_boundary_declared_shapes_all_exist_pass(started):
    """file::func、file::Class::method、嵌套类、async def、带 [参数] 的 id 全部存在时通过，返回值与输入一致。"""
    _write_nodes(started)
    ids = [
        f"{NODES_FILE}::test_top",
        f"{NODES_FILE}::TestInner::test_method",
        f"{NODES_FILE}::TestOuter::TestNested::test_deep",
        f"{NODES_FILE}::test_async_case",
        f"{NODES_FILE}::test_param[1]",
        f"{NODES_FILE}::test_param_colon[x::y]",
    ]
    spec = _spec(ids)
    lg = L.load(started["ledger"])
    out = P.check_structure(spec, lg, started["repo"].root, worktree_files=True)
    assert out == spec
    assert out["groups"][0]["test_ids"] == ids


def test_b3_boundary_parameter_part_may_contain_colons(started):
    """参数部分含 `::`：只取第一个 `[` 之前核对（test_param_colon 存在即通过）。"""
    _write_nodes(started)
    out = _check(started, [f"{NODES_FILE}::test_param_colon[x::y]"])
    assert out["groups"][0]["test_ids"] == [f"{NODES_FILE}::test_param_colon[x::y]"]


def test_b3_boundary_inherited_method_on_subclass_not_missing(started):
    """Class 存在但声明了基类（非 object）、method 不在类体内：可能继承，不判缺失。"""
    _write_nodes(started)
    out = _check(started, [f"{NODES_FILE}::TestDerived::test_inherited"])
    assert out["groups"][0]["test_ids"] == [f"{NODES_FILE}::TestDerived::test_inherited"]


def test_b3_boundary_class_only_id_passes_when_class_exists(started):
    """`file::Class` 只到类名：类存在即通过。"""
    _write_nodes(started)
    out = _check(started, [f"{NODES_FILE}::TestInner"])
    assert out["groups"][0]["test_ids"] == [f"{NODES_FILE}::TestInner"]


def test_b3_boundary_syntax_error_file_not_judged_missing(started):
    """文件无法被 ast 解析：不判缺失，留给 machine 与 proof 的 missing 兜底。"""
    (started["tester_worktree"] / "tests" / "test_broken_syntax.py").write_text(
        "def broken(:\n    pass\n", encoding="utf-8")
    out = _check(started, ["tests/test_broken_syntax.py::test_whatever"])
    assert out["groups"][0]["test_ids"] == ["tests/test_broken_syntax.py::test_whatever"]


def test_b3_boundary_imported_class_symbol_is_opaque(started):
    """reviewer finding 2：导入来的类（非定义）符号未知，找不到的方法不判缺失——否则误拒合法 node id。
    素材只含一条普通 import，确保命中的是 per-symbol opaque 分支，而不是星号导入的整模块跳过。"""
    _write_opaque(started, OPAQUE_IMPORT_FILE, OPAQUE_IMPORT)
    out = _check(started, [f"{OPAQUE_IMPORT_FILE}::OrderedDict::test_absent"])
    assert out["groups"][0]["test_ids"] == [f"{OPAQUE_IMPORT_FILE}::OrderedDict::test_absent"]


def test_b3_boundary_assignment_alias_symbol_is_opaque(started):
    """reviewer finding 2：赋值别名（`ImportedBase = OrderedDict`）同样未知，不判缺失。
    素材不含星号导入，命中 per-symbol opaque 分支。"""
    _write_opaque(started, OPAQUE_ALIAS_FILE, OPAQUE_ALIAS)
    out = _check(started, [f"{OPAQUE_ALIAS_FILE}::ImportedBase::test_absent"])
    assert out["groups"][0]["test_ids"] == [f"{OPAQUE_ALIAS_FILE}::ImportedBase::test_absent"]


def test_b3_boundary_star_import_makes_whole_module_opaque(started):
    """reviewer finding 2：`from x import *` 让整模块符号表不可知，任何名字都不判缺失。"""
    _write_opaque(started, OPAQUE_STAR_FILE, OPAQUE_STAR)
    out = _check(started, [f"{OPAQUE_STAR_FILE}::TestAbsent::test_absent", f"{OPAQUE_STAR_FILE}::test_absent"])
    assert out["groups"][0]["test_ids"] == [f"{OPAQUE_STAR_FILE}::TestAbsent::test_absent", f"{OPAQUE_STAR_FILE}::test_absent"]


def test_b3_boundary_generic_framework_skips_node_check(started):
    """framework=generic：test_ids 不是 pytest node id，不做这项校验。"""
    _write_nodes(started)
    lg = L.load(started["ledger"])
    lg["contract"]["assurance"]["proof_runner"] = {"framework": "generic", "cmd": "bash"}
    out = P.check_structure(_spec([f"{NODES_FILE}::test_absent"]), lg, started["repo"].root, worktree_files=True)
    assert out["groups"][0]["test_ids"] == [f"{NODES_FILE}::test_absent"]


def test_b3_invariant_existing_file_rejections_unchanged(started):
    """文件不存在、非 .py、不在 tester_write 内的既有拒绝及其错误信息不变。"""
    _write_nodes(started)
    with pytest.raises(Problem) as ei:
        _check(started, ["tests/test_no_such_file.py::test_x"])
    assert ei.value.code == "PROOF_SPEC_INVALID"
    assert "引用的文件在你的 worktree 里不存在" in ei.value.message

    with pytest.raises(Problem) as ei:
        _check(started, ["tests/check.sh::test_x"])
    assert ei.value.code == "PROOF_SPEC_INVALID"
    assert "不是 Python 测试文件" in ei.value.message

    with pytest.raises(Problem) as ei:
        _check(started, ["src/test_foo.py::test_x"])
    assert ei.value.code == "PROOF_SPEC_INVALID"
    assert "必须是 tester_write 内的路径" in ei.value.message