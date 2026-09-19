"""bl proof 执行期间输入投影变化：作废本次（PROOF_INPUT_CHANGED），不记 evidence、不记失败。"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from builder_loop import evidence
from builder_loop import ledger as L
from builder_loop import proof as proof_mod
from conftest import (git, implement_mul, make_tester_result, mutation_patch, role_turn, write_mul_test)

_ORIG_RUN = proof_mod._run


def _ready_for_proof(started, cli, hook, patch_kind: str = "kill", timeout: int = 60):
    """走到「tester pass + integrate + machine pass + 已带 patch」，但还没跑 bl proof。"""
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    if patch_kind == "kill":
        patch = mutation_patch(wt)
    else:  # survive：只破坏 add()，mul 的测试照样通过
        src = wt / "src" / "foo.py"
        orig = src.read_text()
        src.write_text(orig.replace("return a + b", "return a - b", 1))
        patch = git(wt, "diff")
        src.write_text(orig)
    res = make_tester_result("mutation", patch)
    res["proof_spec"]["groups"][0]["timeout"] = timeout
    r = role_turn(hook, "tester", "T1", res)
    assert r["code"] == 0, r
    return wt


def _install_once(monkeypatch, action) -> dict:
    state = {"n": 0}

    def wrapped(*a, **k):
        state["n"] += 1
        if state["n"] == 1:
            action()
        return _ORIG_RUN(*a, **k)

    monkeypatch.setattr(proof_mod, "_run", wrapped)
    return state


def _uninstall(monkeypatch) -> None:
    monkeypatch.setattr(proof_mod, "_run", _ORIG_RUN)


def _checkpoint_action(wt: Path, cli, name: str = "extra.py"):
    def go():
        (wt / "src" / name).write_text("# new\n")
        cli("checkpoint", "--session", "S1", "--role", "builder")
    return go


def _replace_spec_action(lp: Path, timeout: int):
    def go():
        with L.mutate(lp) as x:
            spec = copy.deepcopy(x["proof_spec"])
            spec["groups"][0]["timeout"] = timeout
            x["proof_spec"] = spec
    return go


def test_b1_candidate_head_advance_invalidates(started, cli, hook, monkeypatch):
    wt = _ready_for_proof(started, cli, hook)
    lp = started["ledger"]
    before = L.load(lp)
    head0 = before["candidate"]["head"]
    assert before["evidence"].get("proof") is None
    _install_once(monkeypatch, _checkpoint_action(wt, cli))
    res = cli("proof", "--session", "S1", expect=1)
    assert res["ok"] is False and res["code"] == "PROOF_INPUT_CHANGED", res
    assert res["exit_code"] == 1
    d = res["details"]
    after = L.load(lp)
    assert "candidate_head" in d["changed_inputs"]
    assert d["changed_inputs"] == sorted(d["changed_inputs"])
    assert d["head_at_start"] == head0
    assert d["head_now"] == after["candidate"]["head"] and d["head_now"] != head0
    assert d["attempt"] == len(before["failures"]["proof"]) + 1
    assert after["evidence"].get("proof") is None
    assert after["failures"]["proof"] == before["failures"]["proof"]
    ev = after["events"][-1]
    assert ev["kind"] == "proof_input_changed"
    assert ev["changed_inputs"] == d["changed_inputs"] and ev["attempt"] == d["attempt"]


def test_b1_existing_evidence_untouched_on_invalidation(started, cli, hook, monkeypatch):
    wt = _ready_for_proof(started, cli, hook)
    lp = started["ledger"]
    assert cli("proof", "--session", "S1")["result"] == "PASS"
    before = L.load(lp)
    ev_before = copy.deepcopy(before["evidence"].get("proof"))
    assert ev_before is not None
    _install_once(monkeypatch, _checkpoint_action(wt, cli))
    res = cli("proof", "--session", "S1", expect=1)
    assert res["code"] == "PROOF_INPUT_CHANGED"
    assert L.load(lp)["evidence"].get("proof") == ev_before
    assert L.load(lp)["failures"]["proof"] == before["failures"]["proof"]


def test_b1_proof_spec_replacement_invalidates(started, cli, hook, monkeypatch):
    _ready_for_proof(started, cli, hook)
    lp = started["ledger"]
    before = L.load(lp)
    _install_once(monkeypatch, _replace_spec_action(lp, 61))
    res = cli("proof", "--session", "S1", expect=1)
    assert res["code"] == "PROOF_INPUT_CHANGED", res
    d = res["details"]
    assert "proof_spec" in d["changed_inputs"] and "candidate_head" not in d["changed_inputs"]
    assert d["changed_inputs"] == sorted(d["changed_inputs"])
    assert d["head_at_start"] == d["head_now"] == before["candidate"]["head"]
    after = L.load(lp)
    assert after["evidence"].get("proof") is None
    assert after["failures"]["proof"] == before["failures"]["proof"]
    assert after["events"][-1]["kind"] == "proof_input_changed"


def test_b1_would_be_failure_still_invalidated_not_recorded(started, cli, hook, monkeypatch):
    wt = _ready_for_proof(started, cli, hook, patch_kind="survive")
    lp = started["ledger"]
    before = L.load(lp)
    _install_once(monkeypatch, _checkpoint_action(wt, cli))
    res = cli("proof", "--session", "S1", expect=1)
    assert res.get("code") == "PROOF_INPUT_CHANGED", res
    after = L.load(lp)
    assert after["failures"]["proof"] == before["failures"]["proof"]
    assert after["evidence"].get("proof") is None


def test_b1_rerun_after_invalidation_passes_with_same_attempt(started, cli, hook, monkeypatch):
    wt = _ready_for_proof(started, cli, hook)
    _install_once(monkeypatch, _checkpoint_action(wt, cli))
    res = cli("proof", "--session", "S1", expect=1)
    attempt = res["details"]["attempt"]
    _uninstall(monkeypatch)
    out = cli("proof", "--session", "S1")
    assert out["result"] == "PASS"
    assert out["attempt"] == attempt


def test_b1_three_invalidations_do_not_stall(started, cli, hook, monkeypatch):
    _ready_for_proof(started, cli, hook)
    lp = started["ledger"]
    for i in range(3):
        _install_once(monkeypatch, _replace_spec_action(lp, 70 + i))
        res = cli("proof", "--session", "S1", expect=1)
        assert res["code"] == "PROOF_INPUT_CHANGED", res
    _uninstall(monkeypatch)
    st = cli("status", "--session", "S1")
    assert "PROOF_STALL" not in [b["code"] for b in st["readiness"]["blockers"]]
    assert L.load(lp)["failures"]["proof"] == []


def test_b1_unrelated_ledger_change_does_not_invalidate(started, cli, hook, monkeypatch):
    _ready_for_proof(started, cli, hook)
    lp = started["ledger"]

    def noise():
        with L.mutate(lp) as x:
            x["events"].append({"kind": "unrelated_noise", "at": L.now_iso()})

    _install_once(monkeypatch, noise)
    out = cli("proof", "--session", "S1")
    assert out["result"] == "PASS"
    assert L.load(lp)["evidence"]["proof"]


def test_b1_uninterrupted_fail_keeps_shape(started, cli, hook):
    _ready_for_proof(started, cli, hook, patch_kind="survive")
    lp = started["ledger"]
    res = cli("proof", "--session", "S1", expect=1)
    assert res["result"] == "FAIL" and res["attempt"] == 1
    assert res["failure"]["code"] == "TEST_MUTATION_SURVIVED"
    assert len(L.load(lp)["failures"]["proof"]) == 1


def test_b1_input_changes_interface(started, cli, hook):
    wt = _ready_for_proof(started, cli, hook)
    lp, root = started["ledger"], started["repo"].root
    a = L.load(lp)
    assert evidence.input_changes(a, copy.deepcopy(a), "proof", root) == []
    b = copy.deepcopy(a)
    spec = copy.deepcopy(b["proof_spec"])
    spec["groups"][0]["timeout"] = 99
    b["proof_spec"] = spec
    assert evidence.input_changes(a, b, "proof", root) == ["proof_spec"]
    (wt / "src" / "extra2.py").write_text("# x\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    c = L.load(lp)
    ch = evidence.input_changes(a, c, "proof", root)
    assert "candidate_head" in ch and ch == sorted(ch)
