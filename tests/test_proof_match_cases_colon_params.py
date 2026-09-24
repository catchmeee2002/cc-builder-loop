"""B1: proof.match_cases 切分 pytest node id 时要与 pytest junitxml 的 mangle_test_address 一致：
先在第一个 '[' 处切开，只把前半段按 '::' 切分，参数部分原样接回最后一段 name。当前实现对整段
id（含参数）无差别 split('::')，参数里带 '::' 的 node id（如把另一个 test id 当参数拼进去的
test_survives[...]，或 class 方法配上带 '::' 的参数）就切错，判成 missing——这在起点上已经能
观察到（baseline-red，纯函数，不需要新接口，call 阶段用 assert 即可复现）。

覆盖：judge_candidate / discrimination 对这类 id 的处理（B1 given 的两条用例），以及不受影响的
既有边界（不含 '::' 的参数、不带 '[' 的 id、只给文件路径、classname 后缀匹配、真正缺失的用例）
保持不变。
"""

from __future__ import annotations

from builder_loop import proof as P


def _case(classname, name, outcome="passed"):
    return {"classname": classname, "name": name, "outcome": outcome, "message": ""}


# ---------------------------------------------------------------- B1 given：两条 id，参数里含 '::'


ID_NESTED_ID_PARAM = "tests/unit/test_gate_race.py::test_survives[tests/unit/test_x.py::test_y]"
ID_CLASS_METHOD_COLON_PARAM = "tests/test_a.py::TestK::test_m[a::b]"


def _given_cases(outcome: str) -> list[dict]:
    return [
        _case("tests.unit.test_gate_race", "test_survives[tests/unit/test_x.py::test_y]", outcome),
        _case("tests.test_a.TestK", "test_m[a::b]", outcome),
    ]


def test_colon_in_params_judge_candidate_passed_and_ok():
    cases = _given_cases("passed")
    ids = [ID_NESTED_ID_PARAM, ID_CLASS_METHOD_COLON_PARAM]
    result = P.judge_candidate("pytest", 0, cases, ids)
    assert result["per_id"] == {ID_NESTED_ID_PARAM: "passed", ID_CLASS_METHOD_COLON_PARAM: "passed"}
    assert result["ok"] is True


def test_colon_in_params_discrimination_red_on_failure():
    cases = _given_cases("failure")
    ids = [ID_NESTED_ID_PARAM, ID_CLASS_METHOD_COLON_PARAM]
    disc = P.discrimination(cases, ids)
    assert disc == {ID_NESTED_ID_PARAM: "red", ID_CLASS_METHOD_COLON_PARAM: "red"}


# ---------------------------------------------------------------- 边界：不受切分规则改动影响，应保持不变


def test_boundary_param_without_colon_still_matches():
    cases = [_case("tests.t", "test_a[1-2]")]
    ids = ["tests/t.py::test_a[1-2]"]
    assert P.judge_candidate("pytest", 0, cases, ids)["per_id"] == {ids[0]: "passed"}


def test_boundary_id_without_brackets_matches_all_param_instances():
    cases = [_case("tests.t", "test_a[1]"), _case("tests.t", "test_a[2]")]
    ids = ["tests/t.py::test_a"]
    assert P.judge_candidate("pytest", 0, cases, ids)["per_id"] == {ids[0]: "passed"}


def test_boundary_file_only_id_matches_whole_file():
    cases = [_case("tests.t", "test_a"), _case("tests.t", "test_b")]
    ids = ["tests/t.py"]
    assert P.judge_candidate("pytest", 0, cases, ids)["per_id"] == {ids[0]: "passed"}


def test_invariant_classname_suffix_matching_unchanged():
    # monorepo 子目录自带 ini、rootdir 变化：classname 只是完整 module path 的后缀
    cases = [_case("sub.tests.t", "test_a")]
    ids = ["tests/t.py::test_a"]
    assert P.judge_candidate("pytest", 0, cases, ids)["per_id"] == {ids[0]: "passed"}


def test_boundary_colon_param_id_with_no_matching_case_is_missing():
    # 参数里含 '::' 但 junit 里确实没有这条用例：即使切分规则改了，仍应判 missing（不是切分 bug 导致的假阴性）
    cases = [_case("tests.other", "test_z")]
    ids = ["tests/unit/test_gate_race.py::test_survives[tests/unit/test_x.py::test_y]"]
    assert P.judge_candidate("pytest", 0, cases, ids)["per_id"] == {ids[0]: "missing"}
    assert P.discrimination(cases, ids) == {ids[0]: "missing"}
