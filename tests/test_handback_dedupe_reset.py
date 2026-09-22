"""B1：同轮 A→B→A 的第三次 A 照常登记（只跟本轮最后一条结果去重，不是跟任何历史结果去重）。
B2：登记成功后不合规计数清零（下一次不合规重新从「第 1 次」算起）。

覆盖对象是 runtime/builder_loop/hooks.py 里 `_already_recorded`（B1）与 `_retry_or_fail` /
`_accept` 的 `stops = 0` 复位（B2）。两条行为都只经 PostToolUse(SubagentHandback) 生效——
SubagentStop 兜底路径一旦本轮已有 role_result 就整体静默（见 handle_subagent_stop 里
`if any(e["kind"] == "role_result" for e in turn): return _silent()`，这是
test_handback_stop_fallback.py::test_c1_second_stop_same_turn_adds_nothing 锁定的既有语义），
所以本轮 A→B→A 的三次登记与不合规计数复位只在 handback 路径上成立，不在 stop 路径上成立。
"""

from __future__ import annotations

import builder_loop.ledger as L
from conftest import handback, implement_mul, make_tester_result, marker, write_mul_test

REVIEW_A = {  # changes_requested
    "role": "reviewer", "verdict": "changes_requested",
    "findings": [{"severity": "major", "owner": "builder", "file": "src/foo.py", "line": 5, "summary": "缺 0 边界"}],
    "behaviors_verified": [],
}
REVIEW_B = {"role": "reviewer", "verdict": "pass", "findings": [], "behaviors_verified": ["B1"]}  # pass


def _start(hook, role: str, agent_id: str) -> None:
    hook("SubagentStart", {"session_id": "S1", "agent_id": agent_id, "agent_type": role})


def _events(started, kind: str, role: str | None = None) -> list[dict]:
    lg = L.load(started["ledger"])
    return [e for e in lg["events"] if e["kind"] == kind and (role is None or e.get("role") == role)]


def _results(started, role: str) -> list[dict]:
    return _events(started, "role_result", role)


def _ev_status(started, role: str):
    return (L.load(started["ledger"])["evidence"].get(role) or {}).get("status")


def _reviewer_ready(started, cli, hook) -> None:
    implement_mul(started["worktree"])
    cli("checkpoint", "--session", "S1", "--role", "builder")
    _start(hook, "reviewer", "R1")


def _tester_ready(started, cli, hook) -> None:
    _start(hook, "tester", "T1")
    implement_mul(started["worktree"])
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(started["tester_worktree"])


# ================================================================== B1


def test_b1_reviewer_a_b_a_all_three_recorded_via_handback(started, cli, hook):
    """given reviewer 已 start 一轮、候选已 checkpoint / when 同轮内依次交 A、B、A / then 三次都
    返回 0；ledger 里该 reviewer 本轮的 role_result 共 3 条，verdict 依次为
    changes_requested、pass、changes_requested；evidence.reviewer.status 最终为 fail。"""
    _reviewer_ready(started, cli, hook)

    r1 = handback(hook, "reviewer", "R1", marker(REVIEW_A))
    r2 = handback(hook, "reviewer", "R1", marker(REVIEW_B))
    r3 = handback(hook, "reviewer", "R1", marker(REVIEW_A))
    assert r1["code"] == 0 and r2["code"] == 0 and r3["code"] == 0, (r1, r2, r3)

    res = _results(started, "reviewer")
    assert len(res) == 3, res
    assert [r.get("verdict") for r in res] == ["changes_requested", "pass", "changes_requested"], res
    assert all(r.get("via") == "handback" for r in res), res
    assert _ev_status(started, "reviewer") == "fail"


def test_b1_boundary_tester_a_b_a_final_proof_spec_is_first(started, cli, hook):
    """边界：tester 角色同轮 handback 交 A（proof_spec 甲）、B（proof_spec 乙）、A → 3 条
    role_result，ledger.proof_spec 最终等于甲（最后一次登记的是 A）。"""
    _tester_ready(started, cli, hook)
    a = make_tester_result("mutation", test_ids=["tests/test_mul.py::test_mul"])
    b = make_tester_result("mutation", test_ids=["tests/test_mul.py::test_mul"])
    b["proof_spec"]["groups"][0]["timeout"] = 90  # 与甲不同的 proof_spec，但结构同样合法

    r1 = handback(hook, "tester", "T1", marker(a))
    r2 = handback(hook, "tester", "T1", marker(b))
    r3 = handback(hook, "tester", "T1", marker(a))
    assert r1["code"] == 0 and r2["code"] == 0 and r3["code"] == 0, (r1, r2, r3)

    res = _results(started, "tester")
    assert len(res) == 3, res
    assert L.load(started["ledger"])["proof_spec"]["groups"][0]["timeout"] == 60, "最终 proof_spec 应等于甲（timeout=60）"


def test_b1_invariant_repeat_last_a_after_aba_not_recorded(started, cli, hook):
    """不变量：A→B→A 之后再紧接着交同一个 A（与最后一条相同）→ 不新增 role_result。"""
    _reviewer_ready(started, cli, hook)
    assert handback(hook, "reviewer", "R1", marker(REVIEW_A))["code"] == 0
    assert handback(hook, "reviewer", "R1", marker(REVIEW_B))["code"] == 0
    assert handback(hook, "reviewer", "R1", marker(REVIEW_A))["code"] == 0
    assert len(_results(started, "reviewer")) == 3

    r4 = handback(hook, "reviewer", "R1", marker(REVIEW_A))
    assert r4["code"] == 0, r4
    assert len(_results(started, "reviewer")) == 3, "紧接着交同一个 A 不应新增 role_result"


def test_b1_invariant_new_turn_after_aba_records_same_payload_again(started, cli, hook):
    """不变量：SubagentStart 开始新一轮后再交 A（与旧一轮最后一次登记的内容相同）→ 照常登记。"""
    _reviewer_ready(started, cli, hook)
    assert handback(hook, "reviewer", "R1", marker(REVIEW_A))["code"] == 0
    assert len(_results(started, "reviewer")) == 1

    _start(hook, "reviewer", "R1")  # 续接 = 新一轮
    r2 = handback(hook, "reviewer", "R1", marker(REVIEW_A))
    assert r2["code"] == 0, r2
    assert len(_results(started, "reviewer")) == 2


def test_b1_invariant_stop_after_registered_result_is_silent_same_turn(started, cli, hook):
    """不变量：stop 兜底来源——本轮已经登记过结果后，同轮后续的 SubagentStop 静默（不新增
    role_result、不计不合规），由既有 test_c1_second_stop_same_turn_adds_nothing 守住；
    A→B→A 只在 handback 路径上成立，不在 stop 路径上成立。"""
    _reviewer_ready(started, cli, hook)
    assert handback(hook, "reviewer", "R1", marker(REVIEW_A))["code"] == 0
    n_events = len(L.load(started["ledger"])["events"])

    r = hook("SubagentStop", {"session_id": "S1", "agent_id": "R1", "agent_type": "reviewer",
                              "last_assistant_message": marker(REVIEW_B)})
    assert r["code"] == 0 and r["stderr"] == "", r
    assert len(L.load(started["ledger"])["events"]) == n_events, "已登记结果后同轮 Stop 不应新增任何事件"
    assert len(_results(started, "reviewer")) == 1
    assert _ev_status(started, "reviewer") == "fail"  # 仍是 A 的结论，不受这次 Stop 影响


def test_b1_invariant_dirty_tester_worktree_not_deduped(started, cli, hook):
    """不变量：tester 连续交同一个 A，但 worktree 里有未提交的测试文件 → 不算重复，照常登记
    并提交这些文件。"""
    _tester_ready(started, cli, hook)
    a = make_tester_result("mutation", test_ids=["tests/test_mul.py::test_mul"])
    assert handback(hook, "tester", "T1", marker(a))["code"] == 0
    assert len(_results(started, "tester")) == 1

    (started["tester_worktree"] / "tests" / "test_extra.py").write_text(
        "def test_extra():\n    assert True\n", encoding="utf-8"
    )
    r2 = handback(hook, "tester", "T1", marker(a))
    assert r2["code"] == 0, r2
    assert len(_results(started, "tester")) == 2, "worktree 有未提交改动时同一份结论不应被去重"


# ================================================================== B2


def test_b2_tester_malformed_retry_resets_after_success(started, cli, hook):
    """given tester 已 start 一轮且 worktree 已写好测试 / when 同轮内依次交：不合规 → 合规 pass →
    不合规 → 不合规 / then 第 1 次返回 2 且 stderr 含「第 1 次」；第 2 次返回 0 并登记 pass，
    此后 stops == 0；第 3 次返回 2 且 stderr 含「第 1 次」（不是「第 2 次」）；第 4 次返回 2 且
    stderr 含「第 2 次」；evidence.tester.status 在第 3、4 次之后仍为 pass。"""
    _tester_ready(started, cli, hook)

    r1 = handback(hook, "tester", "T1", "已交付。")
    assert r1["code"] == 2 and "第 1 次" in r1["stderr"], r1

    r2 = handback(hook, "tester", "T1", marker(make_tester_result("mutation")))
    assert r2["code"] == 0, r2
    assert L.load(started["ledger"])["agents"]["tester"]["stops"] == 0

    r3 = handback(hook, "tester", "T1", "又不合规。")
    assert r3["code"] == 2 and "第 1 次" in r3["stderr"] and "第 2 次" not in r3["stderr"], r3
    assert _ev_status(started, "tester") == "pass"

    r4 = handback(hook, "tester", "T1", "还是不合规。")
    assert r4["code"] == 2 and "第 2 次" in r4["stderr"], r4
    assert _ev_status(started, "tester") == "pass"


def test_b2_boundary_three_consecutive_malformed_after_reset_fails(started, cli, hook):
    """边界：合规登记之后连续 3 次不合规 → 第 3 次返回 0，stderr 含「连续 3 次不合规」，
    evidence.tester.status 变为 fail（计数从清零后重新累计到上限）。"""
    _tester_ready(started, cli, hook)
    assert handback(hook, "tester", "T1", marker(make_tester_result("mutation")))["code"] == 0
    assert _ev_status(started, "tester") == "pass"

    assert handback(hook, "tester", "T1", "不合规1")["code"] == 2
    assert handback(hook, "tester", "T1", "不合规2")["code"] == 2
    r3 = handback(hook, "tester", "T1", "不合规3")
    assert r3["code"] == 0 and "连续 3 次不合规" in r3["stderr"], r3
    assert _ev_status(started, "tester") == "fail"


def test_b2_boundary_reviewer_same_reset_semantics(started, cli, hook):
    """边界：reviewer 角色同样成立——不合规 → 合规 changes_requested → 不合规 → stderr 含
    「第 1 次」（计数已随中间那次合规登记清零）。"""
    _reviewer_ready(started, cli, hook)

    r1 = handback(hook, "reviewer", "R1", "不合规")
    assert r1["code"] == 2 and "第 1 次" in r1["stderr"], r1

    r2 = handback(hook, "reviewer", "R1", marker(REVIEW_A))  # changes_requested 仍是「合规」结果
    assert r2["code"] == 0, r2
    assert L.load(started["ledger"])["agents"]["reviewer"]["stops"] == 0

    r3 = handback(hook, "reviewer", "R1", "又不合规")
    assert r3["code"] == 2 and "第 1 次" in r3["stderr"], r3


def test_b2_invariant_never_succeeded_fails_on_third_malformed(started, cli, hook):
    """不变量：从未登记过合规结果时，连续 3 次不合规仍然在第 3 次记 fail（现有语义），
    role_malformed 事件每次不合规各记一条，final 字段只在达到上限那次为 true。"""
    _tester_ready(started, cli, hook)

    for i in (1, 2):
        r = handback(hook, "tester", "T1", "不合规")
        assert r["code"] == 2, r
        mal = _events(started, "role_malformed", "tester")
        assert len(mal) == i and mal[-1].get("final") is False, mal

    r3 = handback(hook, "tester", "T1", "不合规")
    assert r3["code"] == 0, r3
    mal = _events(started, "role_malformed", "tester")
    assert len(mal) == 3 and mal[-1].get("final") is True, mal
    assert _ev_status(started, "tester") == "fail"
