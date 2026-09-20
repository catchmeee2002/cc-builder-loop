"""classify_counterexample 的反例分类必须只看 junit 结构位（outcome），不得解析失败信息文本。

见 run mutation-counterexample-classification-20260920-165156-2c0d 的 behaviors C1-C6。
"""

from builder_loop import proof as P


def case(classname: str, name: str, outcome: str, message: str = "") -> dict[str, str]:
    return {"classname": classname, "name": name, "outcome": outcome, "message": message}


MOD = "tests.test_classify_counterexample"


# ---------------------------------------------------------------- C1


def test_c1_single_failure_among_declared_is_assertion_failure_regardless_of_message():
    # 反例命令 rc=1，junit 无 error，declared 匹配到的用例里恰好一个 failure，其余 passed。
    # 不变量：失败信息内容不影响结论——这里用一个既不以 AssertionError/assert /Failed: 开头的
    # 普通文本，证明分类不能依赖消息文本前缀（否则当前实现会把它误判成 error）。
    cases = [
        case(MOD, "test_a", "failure", "boom, something exploded"),
        case(MOD, "test_b", "passed"),
    ]
    test_ids = ["tests/test_classify_counterexample.py::test_a", "tests/test_classify_counterexample.py::test_b"]
    result = P.classify_counterexample("pytest", 1, cases, test_ids)
    assert result == "assertion-failure"


def test_c1_boundary_declared_matches_exactly_one_case():
    cases = [case(MOD, "test_only", "failure", "kaboom")]
    result = P.classify_counterexample("pytest", 1, cases, ["tests/test_classify_counterexample.py::test_only"])
    assert result == "assertion-failure"


def test_c1_boundary_declared_matches_many_but_only_one_failure():
    cases = [
        case(MOD, "test_a", "failure", "whatever text here"),
        case(MOD, "test_b", "passed"),
        case(MOD, "test_c", "passed"),
    ]
    result = P.classify_counterexample("pytest", 1, cases, [
        "tests/test_classify_counterexample.py::test_a",
        "tests/test_classify_counterexample.py::test_b",
        "tests/test_classify_counterexample.py::test_c",
    ])
    assert result == "assertion-failure"


# ---------------------------------------------------------------- C2


def test_c2_mixed_assertion_prefixed_and_other_failures_is_assertion_failure():
    cases = [
        case(MOD, "test_a", "failure", "AssertionError: expected 1 got 2"),
        case(MOD, "test_b", "failure", "KeyError: 'missing'"),
    ]
    test_ids = ["tests/test_classify_counterexample.py::test_a", "tests/test_classify_counterexample.py::test_b"]
    result = P.classify_counterexample("pytest", 1, cases, test_ids)
    assert result == "assertion-failure"


def test_c2_order_of_failures_does_not_change_result():
    cases_forward = [
        case(MOD, "test_a", "failure", "AssertionError: boom"),
        case(MOD, "test_b", "failure", "TypeError: nope"),
    ]
    cases_reversed = [
        case(MOD, "test_b", "failure", "TypeError: nope"),
        case(MOD, "test_a", "failure", "AssertionError: boom"),
    ]
    test_ids = ["tests/test_classify_counterexample.py::test_a", "tests/test_classify_counterexample.py::test_b"]
    assert P.classify_counterexample("pytest", 1, cases_forward, test_ids) == "assertion-failure"
    assert P.classify_counterexample("pytest", 1, cases_reversed, test_ids) == "assertion-failure"


# ---------------------------------------------------------------- C3


def test_c3_sole_failure_message_starts_with_keyerror_is_assertion_failure():
    cases = [case(MOD, "test_only", "failure", "KeyError: 'foo'")]
    result = P.classify_counterexample("pytest", 1, cases, ["tests/test_classify_counterexample.py::test_only"])
    assert result == "assertion-failure"


def test_c3_boundary_message_starts_with_valueerror():
    cases = [case(MOD, "test_only", "failure", "ValueError: bad input")]
    result = P.classify_counterexample("pytest", 1, cases, ["tests/test_classify_counterexample.py::test_only"])
    assert result == "assertion-failure"


def test_c3_boundary_message_starts_with_typeerror():
    cases = [case(MOD, "test_only", "failure", "TypeError: unsupported operand")]
    result = P.classify_counterexample("pytest", 1, cases, ["tests/test_classify_counterexample.py::test_only"])
    assert result == "assertion-failure"


def test_c3_boundary_message_is_empty_string():
    cases = [case(MOD, "test_only", "failure", "")]
    result = P.classify_counterexample("pytest", 1, cases, ["tests/test_classify_counterexample.py::test_only"])
    assert result == "assertion-failure"


# ---------------------------------------------------------------- C4 (mutation：当前实现已正确，无法 baseline-red)


def test_c4_any_error_outcome_in_junit_yields_error():
    cases = [
        case(MOD, "test_a", "error", "collection failed"),
        case(MOD, "test_b", "passed"),
    ]
    result = P.classify_counterexample("pytest", 1, cases, ["tests/test_classify_counterexample.py::test_b"])
    assert result == "error"


def test_c4_boundary_error_case_outside_declared_still_yields_error():
    # error 检查范围必须是全部用例，不得收窄到 declared test_ids。declared 本身是 failure：
    # 若判据被错误地收窄到只看 declared，declared 里没有 error 条目，会被误判成 assertion-failure。
    cases = [
        case(MOD, "test_unrelated", "error", "setup blew up"),
        case(MOD, "test_declared", "failure", "AssertionError: x"),
    ]
    result = P.classify_counterexample("pytest", 1, cases, ["tests/test_classify_counterexample.py::test_declared"])
    assert result == "error"


def test_c4_boundary_error_alongside_failure_still_yields_error():
    cases = [
        case(MOD, "test_a", "error", "boom"),
        case(MOD, "test_b", "failure", "AssertionError: x"),
    ]
    result = P.classify_counterexample("pytest", 1, cases, [
        "tests/test_classify_counterexample.py::test_a",
        "tests/test_classify_counterexample.py::test_b",
    ])
    assert result == "error"


# ---------------------------------------------------------------- C5 (mutation：当前实现已正确，无法 baseline-red)


def test_c5_zero_returncode_is_pass_for_pytest():
    cases = [case(MOD, "test_a", "passed")]
    result = P.classify_counterexample("pytest", 0, cases, ["tests/test_classify_counterexample.py::test_a"])
    assert result == "pass"


def test_c5_boundary_zero_returncode_is_pass_for_generic():
    result = P.classify_counterexample("generic", 0, [], ["whatever"])
    assert result == "pass"


# ---------------------------------------------------------------- C6 (mutation：当前实现已正确，无法 baseline-red)


def test_c6_generic_nonzero_returncode_is_assertion_failure():
    result = P.classify_counterexample("generic", 1, [], ["whatever"])
    assert result == "assertion-failure"


def test_c6_generic_ignores_junit_shaped_cases_and_only_looks_at_returncode():
    # 不变量：generic 只看退出码，不解析 junit——即便传入的 cases 里全是 error，也不该改变结论。
    cases = [case(MOD, "test_a", "error", "should be irrelevant for generic")]
    result = P.classify_counterexample("generic", 7, cases, ["whatever"])
    assert result == "assertion-failure"
