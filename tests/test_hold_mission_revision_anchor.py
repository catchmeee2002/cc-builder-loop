"""B5：`bl contract revise` 把 mission revision 往上推之后，旧 revision 下的 AskUserQuestion 回答
不再算数。新 revision 下 gate 还没全部通过过 → HOLD_NOT_READY；通过了但还没有新的 AskUserQuestion
回答 → USER_DECISION_REQUIRED。旧 run 里 gates_passed 事件没有 revision 字段时按 revision 1 处理。

用不需要 tester 的轻量 contract（assurance.required=[machine, reviewer]）避免每次重验都要走一遍
tester 的两段式流程。
"""

from __future__ import annotations

from pathlib import Path

from builder_loop import ledger as L
from conftest import contract_with, reviewer_pass, write_plan


def _answer_askuserquestion(hook, session: str = "S1") -> None:
    hook("PreToolUse", {"session_id": session, "tool_name": "AskUserQuestion", "tool_use_id": "q-hold"})
    hook("PostToolUse", {"session_id": session, "tool_name": "AskUserQuestion"})


def _lite_ready_to_finalize(repo, cli, hook, session: str = "S1", plan_name: str = "lite.md") -> dict:
    write_plan(repo.root, contract_with(**{"assurance.required": ["machine", "reviewer"]}), plan_name)
    out = cli("start", "--plan", str(repo.root / plan_name), "--session", session)
    wt = Path(out["worktree"])
    (wt / "src" / "extra.py").write_text("# v1\n")
    cli("checkpoint", "--session", session, "--role", "builder")
    assert cli("machine", "--session", session)["result"] == "PASS"
    reviewer_pass(hook)
    assert cli("status", "--session", session)["readiness"]["next_action"] == "finalize"
    return {"worktree": wt, "ledger": Path(out["ledger"])}


def test_hold_boundary_after_mission_revision_requires_fresh_gate_pass_and_new_answer(repo, cli, hook):
    ctx = _lite_ready_to_finalize(repo, cli, hook)
    wt = ctx["worktree"]
    _answer_askuserquestion(hook)

    c2 = contract_with(**{"assurance.required": ["machine", "reviewer"], "mission.revision": 2})
    write_plan(repo.root, c2, "lite-rev2.md")
    revised = cli("contract", "revise", "--session", "S1", "--plan", str(repo.root / "lite-rev2.md"), "--authorize")
    assert "MISSION_REVISION" in revised["changes"]

    # 新 revision 下 gate 还没全过：HOLD_NOT_READY（即便 revision 1 时已经回答过 AskUserQuestion）
    out = cli("hold", "--session", "S1", "--reason", "x", expect=1)
    assert out["code"] == "HOLD_NOT_READY"

    # 重新把 gate 在新 revision 下跑绿，仍然没有补新的 AskUserQuestion
    (wt / "src" / "extra.py").write_text("# v2\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    reviewer_pass(hook, agent_id="R2")
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "finalize"

    out2 = cli("hold", "--session", "S1", "--reason", "y", expect=3)
    assert out2["code"] == "USER_DECISION_REQUIRED"

    # 新 revision 下补一次回答才行
    _answer_askuserquestion(hook)
    out3 = cli("hold", "--session", "S1", "--reason", "y")
    assert out3.get("ok", True) is not False
    assert L.load(ctx["ledger"])["events"][-1]["kind"] == "hold"


def test_boundary_legacy_gates_passed_event_without_revision_field_treated_as_revision_1(repo, cli, hook):
    ctx = _lite_ready_to_finalize(repo, cli, hook)
    ledger_path = ctx["ledger"]
    with L.mutate(ledger_path) as lg2:
        for e in lg2["events"]:
            if e["kind"] == "gates_passed":
                e.pop("revision", None)
    assert lg2["contract"]["mission"]["revision"] == 1

    _answer_askuserquestion(hook)
    out = cli("hold", "--session", "S1", "--reason", "x")
    assert out.get("ok", True) is not False
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "held"


def test_invariant_already_held_still_rejected(repo, cli, hook):
    _lite_ready_to_finalize(repo, cli, hook)
    _answer_askuserquestion(hook)
    cli("hold", "--session", "S1", "--reason", "x")
    out = cli("hold", "--session", "S1", "--reason", "y", expect=1)
    assert out["code"] == "ALREADY_HELD"


def test_invariant_hold_release_needs_no_authorization(repo, cli, hook):
    _lite_ready_to_finalize(repo, cli, hook)
    _answer_askuserquestion(hook)
    cli("hold", "--session", "S1", "--reason", "x")
    out = cli("hold", "--session", "S1", "--release")
    assert out.get("ok", True) is not False
