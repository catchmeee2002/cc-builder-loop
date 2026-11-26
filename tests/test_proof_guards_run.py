"""B8-B11：`proof.run_proof` 执行 guard 的变异鉴别。

B8：guard 打上 patch 后全部用例在 call 阶段变红 → proof pass，evidence 的 groups[i].guards 逐项记录
    classification / per_id / patch_sha256 / patch_paths / log / returncode。
B9：guard 打 patch 后仍有用例通过（或只有部分变红，或收集/setup 出错）→ FAIL TEST_MUTATION_SURVIVED，
    details.guard 指向该 guard 下标、undiscriminated 列出未变红的 id、suggested_owner=tester。
B10：guard patch 越界 / 触及不存在文件 / 打不上 / 缺 patch → TEST_MUTATION_INVALID / TEST_MUTATION_PATCH_MISSING，
    details.guard 为下标、suggested_owner=tester。
B11：guard 的 test_ids 在未打 patch 的候选上就不是全绿 → TEST_PROOF_CANDIDATE_FAILED / TEST_PROOF_NOT_EXECUTED，
    details.guard 为下标、不进入反例段。

起点代码对 guards 视而不见：proof 直接 PASS / evidence 没有 guards 键，所以这些正向断言在起点上
call 阶段失败（baseline-red）。没有 guards 的既有失败形状、guard 之外的行为不变是守卫（不列入 group）。
"""

from __future__ import annotations

import json

from builder_loop.jsonutil import sha256_bytes

from conftest import git, implement_mul, make_tester_result, mutation_patch, role_turn, write_mul_test

GROUP_TEST = "tests/test_mul.py::test_mul"
GUARD_FILE = "tests/test_guard_add.py"
GUARD_SMALL = f"{GUARD_FILE}::test_guard_add_small"
GUARD_BIG = f"{GUARD_FILE}::test_guard_add_big"
GUARD_SRC = (
    "from src.foo import add\n\n\n"
    "def test_guard_add_small():\n    assert add(2, 3) == 5\n\n\n"
    "def test_guard_add_big():\n    assert add(10, 1) == 11\n"
)
BAD_GUARD_FILE = "tests/test_guard_bad.py"
BAD_GUARD = f"{BAD_GUARD_FILE}::test_guard_bad"
BAD_GUARD_SRC = "def test_guard_bad():\n    assert False\n"
SKIP_GUARD_FILE = "tests/test_guard_skip.py"
SKIP_GUARD = f"{SKIP_GUARD_FILE}::test_guard_skip"
SKIP_GUARD_SRC = "import pytest\n\n\n@pytest.mark.skip(reason='intentionally skipped')\ndef test_guard_skip():\n    assert True\n"
# 隔离类守卫：全量跑时靠前一个用例留下模块状态而通过，单独跑该 id 时状态缺失 → 失败。
# 用来触发 B11 boundary「全量 machine 已通过时既有 isolation 规则同样适用于 guard」。
ISOLATION_GUARD_FILE = "tests/test_guard_isolation.py"
ISOLATION_GUARD = f"{ISOLATION_GUARD_FILE}::test_zzz_requires_prior_state"
ISOLATION_GUARD_SRC = (
    "STATE = {}\n\n\n"
    "def test_aaa_sets_state():\n    STATE['ready'] = True\n\n\n"
    "def test_zzz_requires_prior_state():\n    assert STATE.get('ready') is True\n"
)

MISSING_FILE_PATCH = (
    "diff --git a/src/nope.py b/src/nope.py\n"
    "--- a/src/nope.py\n"
    "+++ b/src/nope.py\n"
    "@@ -1 +1 @@\n"
    "-a\n"
    "+b\n"
)
BAD_CONTEXT_PATCH = (
    "diff --git a/src/foo.py b/src/foo.py\n"
    "--- a/src/foo.py\n"
    "+++ b/src/foo.py\n"
    "@@ -1,2 +1,2 @@\n"
    "-nonexistent-line\n"
    "+other\n"
)


def _diff(wt, old: str, new: str) -> str:
    src = wt / "src" / "foo.py"
    original = src.read_text()
    assert old in original, (old, original)
    src.write_text(original.replace(old, new, 1))
    patch = git(wt, "diff")
    src.write_text(original)
    return patch


def _append_diff(wt, text: str) -> str:
    src = wt / "src" / "foo.py"
    original = src.read_text()
    src.write_text(original + text)
    patch = git(wt, "diff")
    src.write_text(original)
    return patch


def _ready(started, cli, hook, extra: dict[str, str] | None = None):
    """tester 盲写（mutation 组无 patch）→ builder 实现 → integrate；候选里带上组测试与守卫测试文件。"""
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    (twt / GUARD_FILE).write_text(GUARD_SRC, encoding="utf-8")
    for path, body in (extra or {}).items():
        (twt / path).write_text(body, encoding="utf-8")
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")
    return wt, twt


def _spec(group_patch: str, guards: list[dict], *, group_ids=None, kind: str = "mutation") -> dict:
    return {"groups": [{"kind": kind, "behavior_ids": ["B1"], "test_ids": list(group_ids or [GROUP_TEST]),
                        "timeout": 60, "patch": group_patch, "guards": guards}]}


def _run_proof(cli, tmp_path, spec: dict, *, expect: int):
    path = tmp_path / "guard-spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return cli("proof", "--session", "S1", "--spec-file", str(path), expect=expect)


def _all_red_patch(wt) -> str:
    return _diff(wt, "    return a + b", "    return a - b")


# ---------------------------------------------------------------- B8：全部变红 → evidence 记录


def test_b8_guard_all_red_proof_passes_with_guard_evidence(started, cli, hook, tmp_path):
    wt, _ = _ready(started, cli, hook)
    group_patch = mutation_patch(wt)
    guard_patch = _all_red_patch(wt)

    out = _run_proof(cli, tmp_path, _spec(group_patch, [{"test_ids": [GUARD_SMALL, GUARD_BIG], "patch": guard_patch}]),
                     expect=0)
    assert out["result"] == "PASS", out
    g = out["groups"][0]
    guards = g["guards"]
    assert isinstance(guards, list) and len(guards) == 1, guards
    gd = guards[0]
    assert gd["classification"] == "assertion-failure", gd
    assert gd["per_id"] == {GUARD_SMALL: "red", GUARD_BIG: "red"}, gd
    assert gd["patch_sha256"] == sha256_bytes(guard_patch.encode()), gd
    assert gd["patch_paths"] == ["src/foo.py"], gd
    assert gd["returncode"] != 0, gd
    assert gd["log"], gd
    # 边界：执行完成后候选 worktree 无残留改动
    assert git(wt, "status", "--porcelain") == ""


def test_b8_boundary_multiple_guards_judged_independently(started, cli, hook, tmp_path):
    wt, _ = _ready(started, cli, hook)
    group_patch = mutation_patch(wt)
    guard_a = _diff(wt, "    return a + b", "    return a - b")
    guard_b = _diff(wt, "    return a + b", "    return a * b")

    out = _run_proof(cli, tmp_path, _spec(group_patch, [
        {"test_ids": [GUARD_SMALL, GUARD_BIG], "patch": guard_a},
        {"test_ids": [GUARD_SMALL, GUARD_BIG], "patch": guard_b},
    ]), expect=0)
    assert out["result"] == "PASS", out
    guards = out["groups"][0]["guards"]
    assert len(guards) == 2, guards
    assert all(g["classification"] == "assertion-failure" for g in guards), guards
    assert guards[0]["patch_sha256"] != guards[1]["patch_sha256"], guards


# ---------------------------------------------------------------- B9：仍绿 / 部分变红 / error → SURVIVED


def test_b9_guard_partial_red_fails_mutation_survived(started, cli, hook, tmp_path):
    wt, _ = _ready(started, cli, hook)
    group_patch = mutation_patch(wt)
    guard_patch = _diff(wt, "    return a + b", "    return a + b if a < 5 else a - b")

    out = _run_proof(cli, tmp_path, _spec(group_patch, [{"test_ids": [GUARD_SMALL, GUARD_BIG], "patch": guard_patch}]),
                     expect=1)
    assert out["result"] == "FAIL", out
    f = out["failure"]
    assert f["code"] == "TEST_MUTATION_SURVIVED", f
    assert f["guard"] == 0, f
    assert f["undiscriminated"] == [GUARD_SMALL], f
    assert f["suggested_owner"] == "tester", f


def test_b9_boundary_guard_collection_error_is_mutation_survived(started, cli, hook, tmp_path):
    wt, _ = _ready(started, cli, hook)
    group_patch = mutation_patch(wt)
    guard_patch = _append_diff(wt, "this is not valid python\n")

    out = _run_proof(cli, tmp_path, _spec(group_patch, [{"test_ids": [GUARD_SMALL, GUARD_BIG], "patch": guard_patch}]),
                     expect=1)
    f = out["failure"]
    assert f["code"] == "TEST_MUTATION_SURVIVED", f
    assert f["guard"] == 0, f
    assert f["suggested_owner"] == "tester", f


def test_b9_boundary_multiple_guards_report_first_failing_index(started, cli, hook, tmp_path):
    wt, _ = _ready(started, cli, hook)
    group_patch = mutation_patch(wt)
    guard_all_red = _all_red_patch(wt)
    guard_partial = _diff(wt, "    return a + b", "    return a + b if a < 5 else a - b")

    out = _run_proof(cli, tmp_path, _spec(group_patch, [
        {"test_ids": [GUARD_SMALL, GUARD_BIG], "patch": guard_all_red},
        {"test_ids": [GUARD_SMALL, GUARD_BIG], "patch": guard_partial},
    ]), expect=1)
    f = out["failure"]
    assert f["code"] == "TEST_MUTATION_SURVIVED", f
    assert f["guard"] == 1, f
    assert f["undiscriminated"] == [GUARD_SMALL], f


# ---------------------------------------------------------------- B10：越界 / 不存在 / 打不上 / 缺 patch


def test_b10_guard_patch_touching_nonexistent_file_invalid(started, cli, hook, tmp_path):
    wt, _ = _ready(started, cli, hook)
    out = _run_proof(cli, tmp_path, _spec(mutation_patch(wt), [
        {"test_ids": [GUARD_SMALL, GUARD_BIG], "patch": MISSING_FILE_PATCH}]), expect=1)
    f = out["failure"]
    assert f["code"] == "TEST_MUTATION_INVALID", f
    assert f["guard"] == 0, f
    assert f["suggested_owner"] == "tester", f


def test_b10_guard_patch_not_applying_invalid(started, cli, hook, tmp_path):
    wt, _ = _ready(started, cli, hook)
    out = _run_proof(cli, tmp_path, _spec(mutation_patch(wt), [
        {"test_ids": [GUARD_SMALL, GUARD_BIG], "patch": BAD_CONTEXT_PATCH}]), expect=1)
    f = out["failure"]
    assert f["code"] == "TEST_MUTATION_INVALID", f
    assert f["guard"] == 0, f
    assert f["suggested_owner"] == "tester", f


def test_b10_guard_missing_patch_reports_patch_missing(started, cli, hook, tmp_path):
    wt, _ = _ready(started, cli, hook)
    out = _run_proof(cli, tmp_path, _spec(mutation_patch(wt), [
        {"test_ids": [GUARD_SMALL, GUARD_BIG]}]), expect=1)
    f = out["failure"]
    assert f["code"] == "TEST_MUTATION_PATCH_MISSING", f
    assert f["guard"] == 0, f
    assert f["suggested_owner"] == "tester", f


# ---------------------------------------------------------------- B11：guard 在候选上就不是全绿


def test_b11_guard_failing_on_candidate_reports_candidate_failed(started, cli, hook, tmp_path):
    wt, _ = _ready(started, cli, hook, extra={BAD_GUARD_FILE: BAD_GUARD_SRC})
    out = _run_proof(cli, tmp_path, _spec(mutation_patch(wt), [
        {"test_ids": [BAD_GUARD], "patch": _all_red_patch(wt)}]), expect=1)
    f = out["failure"]
    assert f["code"] == "TEST_PROOF_CANDIDATE_FAILED", f
    assert f["guard"] == 0, f
    # 没有 machine fresh pass 时按既有规则默认归 builder（隔离规则见下面的 boundary 用例）
    assert f["suggested_owner"] == "builder", f
    # 不进入反例段
    assert "counterexample" not in out["groups"][0], out["groups"][0]


def test_b11_boundary_machine_passed_guard_failure_marks_tester_with_isolation_hint(started, cli, hook, tmp_path):
    """边界：全量 machine 已通过时，TEST_PROOF_CANDIDATE_FAILED 的既有隔离规则同样适用于 guard。"""
    wt, _ = _ready(started, cli, hook, extra={ISOLATION_GUARD_FILE: ISOLATION_GUARD_SRC})
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    out = _run_proof(cli, tmp_path, _spec(mutation_patch(wt), [
        {"test_ids": [ISOLATION_GUARD], "patch": _all_red_patch(wt)}]), expect=1)
    f = out["failure"]
    assert f["code"] == "TEST_PROOF_CANDIDATE_FAILED", f
    assert f["guard"] == 0, f
    assert f["suggested_owner"] == "tester", f
    assert f.get("isolation_hint"), f
    assert "counterexample" not in out["groups"][0], out["groups"][0]


def test_b11_guard_skipped_on_candidate_reports_not_executed(started, cli, hook, tmp_path):
    wt, _ = _ready(started, cli, hook, extra={SKIP_GUARD_FILE: SKIP_GUARD_SRC})
    out = _run_proof(cli, tmp_path, _spec(mutation_patch(wt), [
        {"test_ids": [SKIP_GUARD], "patch": _all_red_patch(wt)}]), expect=1)
    f = out["failure"]
    assert f["code"] == "TEST_PROOF_NOT_EXECUTED", f
    assert f["guard"] == 0, f
    assert "counterexample" not in out["groups"][0], out["groups"][0]


# ---------------------------------------------------------------- 守卫（不列入 proof group）


def test_guard_b9_mutation_group_survived_has_no_guard_key(started, cli, hook, tmp_path):
    """不变量：mutation 组自身的 TEST_MUTATION_SURVIVED 形状不变，且不带 details.guard。"""
    wt, _ = _ready(started, cli, hook)
    # 组 patch 只改 add()，不影响组测试（mul）→ 组在反例下仍绿 → SURVIVED
    group_patch = _diff(wt, "    return a + b", "    return a - b")
    spec = {"groups": [{"kind": "mutation", "behavior_ids": ["B1"], "test_ids": [GROUP_TEST],
                        "timeout": 60, "patch": group_patch}]}
    out = _run_proof(cli, tmp_path, spec, expect=1)
    f = out["failure"]
    assert f["code"] == "TEST_MUTATION_SURVIVED", f
    assert "guard" not in f, f


def test_guard_b11_group_candidate_failure_has_no_guard_key(started, cli, hook, tmp_path):
    """不变量：group 的 test_ids 在候选上失败时的失败形状不变，且不带 details.guard。"""
    wt, _ = _ready(started, cli, hook, extra={"tests/test_group_bad.py": "def test_group_bad():\n    assert False\n"})
    spec = {"groups": [{"kind": "mutation", "behavior_ids": ["B1"],
                        "test_ids": ["tests/test_group_bad.py::test_group_bad"], "timeout": 60,
                        "patch": mutation_patch(wt)}]}
    out = _run_proof(cli, tmp_path, spec, expect=1)
    f = out["failure"]
    assert f["code"] == "TEST_PROOF_CANDIDATE_FAILED", f
    assert "guard" not in f, f