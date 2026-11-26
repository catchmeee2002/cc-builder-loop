"""B5：proof_spec 的 group 新增可选键 `guards`，结构校验（check_structure / validate_spec）接受合法形状，
拒绝非法形状并抛 PROOF_SPEC_INVALID。

合法形状 `guards:[{"test_ids":[非空字符串数组],"patch_file":绝对路径(可省略)}]`；非法情形见各用例。
一个 group 里既有「合法 guards 通过」又有「非法 guards 被拒」，两个方向都要判据覆盖，所以整组走 mutation：
把候选里的 guards 校验改坏后，下面的每条都必须变红。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from builder_loop import ledger as L
from builder_loop import proof as P
from builder_loop.errors import Problem

from conftest import contract_with, write_plan

GUARDS_FILE = "tests/test_guards_node.py"
GROUP_ID = f"{GUARDS_FILE}::test_group"
GUARD_ID = f"{GUARDS_FILE}::test_guard"
GUARD_ID2 = f"{GUARDS_FILE}::test_guard2"
GUARDS_SRC = (
    "def test_group():\n    assert True\n\n\n"
    "def test_guard():\n    assert True\n\n\n"
    "def test_guard2():\n    assert True\n"
)
README_DIFF = (
    "diff --git a/README.md b/README.md\n"
    "--- a/README.md\n"
    "+++ b/README.md\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new\n"
)


def _write_guards_file(started) -> None:
    (started["tester_worktree"] / "tests").mkdir(parents=True, exist_ok=True)
    (started["tester_worktree"] / GUARDS_FILE).write_text(GUARDS_SRC, encoding="utf-8")


def _spec(guards, *, kind: str = "mutation", test_ids=None) -> dict:
    return {"groups": [{"kind": kind, "behavior_ids": ["B1"], "test_ids": list(test_ids or [GROUP_ID]),
                        "timeout": 60, "guards": guards}]}


def _check(started, guards, *, kind: str = "mutation", test_ids=None, lg=None):
    lg = lg or L.load(started["ledger"])
    return P.check_structure(_spec(guards, kind=kind, test_ids=test_ids), lg, started["repo"].root,
                             worktree_files=True)


def _assert_invalid(started, guards, *, kind: str = "mutation", lg=None):
    with pytest.raises(Problem) as ei:
        _check(started, guards, kind=kind, lg=lg)
    assert ei.value.code == "PROOF_SPEC_INVALID", ei.value.to_json()
    return ei.value


# ---------------------------------------------------------------- 合法形状通过


def test_b5_valid_guards_shape_passes(started):
    _write_guards_file(started)
    out = _check(started, [{"test_ids": [GUARD_ID]}])
    assert out["groups"][0]["guards"] == [{"test_ids": [GUARD_ID]}]


def test_b5_boundary_empty_guards_equals_no_guards(started):
    _write_guards_file(started)
    out = _check(started, [])
    assert out["groups"][0]["guards"] == []


def test_b5_boundary_unknown_guard_keys_not_rejected(started):
    _write_guards_file(started)
    out = _check(started, [{"test_ids": [GUARD_ID], "note": "future-key"}])
    assert out["groups"][0]["guards"][0]["note"] == "future-key"


# ---------------------------------------------------------------- 非法形状被拒


def test_b5_guards_not_array_rejected(started):
    _write_guards_file(started)
    _assert_invalid(started, "not-an-array")


def test_b5_guard_element_not_object_rejected(started):
    _write_guards_file(started)
    _assert_invalid(started, [GUARD_ID])


def test_b5_guard_empty_test_ids_rejected(started):
    _write_guards_file(started)
    _assert_invalid(started, [{"test_ids": []}])


def test_b5_guard_non_string_test_id_rejected(started):
    _write_guards_file(started)
    _assert_invalid(started, [{"test_ids": [1]}])


def test_b5_guard_dash_prefixed_test_id_rejected(started):
    _write_guards_file(started)
    _assert_invalid(started, [{"test_ids": ["-k"]}])


def test_b5_guard_id_duplicating_group_test_ids_rejected(started):
    _write_guards_file(started)
    _assert_invalid(started, [{"test_ids": [GROUP_ID]}])


def test_b5_guard_file_outside_tester_write_rejected(started):
    _write_guards_file(started)
    _assert_invalid(started, [{"test_ids": ["src/foo.py::test_x"]}])


def test_b5_guard_missing_function_rejected(started):
    _write_guards_file(started)
    _assert_invalid(started, [{"test_ids": [f"{GUARDS_FILE}::test_absent"]}])


def test_b5_generic_framework_guard_must_have_single_test_id(started):
    _write_guards_file(started)
    lg = L.load(started["ledger"])
    lg["contract"]["assurance"]["proof_runner"] = {"framework": "generic", "cmd": "bash"}
    _assert_invalid(started, [{"test_ids": [GUARD_ID, GUARD_ID2]}], lg=lg)  # group 仍只有 1 个 test_id


def test_b5_boundary_guard_patch_outside_authority_rejected_with_contract_owner(started):
    """边界：guard 的 patch 触及非 builder 拥有的路径，报错形态与 mutation 组一致（含 suggested_owner）。"""
    _write_guards_file(started)
    err = _assert_invalid(started, [{"test_ids": [GUARD_ID], "patch": README_DIFF}])
    assert err.details.get("suggested_owner") == "contract", err.to_json()


def test_b5_boundary_reviewed_boundaries_group_with_guards_allowed(repo, cli):
    c = contract_with()
    c["mission"]["behaviors"][0]["proof"] = "reviewed-boundaries"
    write_plan(repo.root, c, "reviewed-guards.md")
    out = cli("start", "--plan", str(repo.root / "reviewed-guards.md"), "--session", "S1")
    twt = Path(out["tester_worktree"])
    (twt / "tests" / "test_guards_node.py").write_text(GUARDS_SRC, encoding="utf-8")
    lg = L.load(out["ledger"])
    spec = {"groups": [{"kind": "reviewed-boundaries", "behavior_ids": ["B1"], "test_ids": [GROUP_ID], "timeout": 60,
                        "reviewed_boundaries": {"positive": [GROUP_ID], "negative": [], "boundary": [], "invariant": []},
                        "guards": [{"test_ids": [GUARD_ID]}]}]}
    got = P.check_structure(spec, lg, repo.root, worktree_files=True)
    assert got["groups"][0]["guards"] == [{"test_ids": [GUARD_ID]}]