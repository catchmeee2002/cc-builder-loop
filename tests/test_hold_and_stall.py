"""B1-B4：gate 全过后可 hold finalize；hold 期间 Stop 放行、finalize 拒绝；release 恢复；
finalize 就绪时的 stall 逃生提示 `bl hold`。"""

from __future__ import annotations

from pathlib import Path

from builder_loop import ledger as L
from builder_loop.cli import build_parser, dispatch
from builder_loop.errors import Problem
from conftest import drive_to_proof_pass, git, reviewer_pass


def _call(repo, *args):
    """同 conftest 的 cli fixture，但把 (out, exit_code) 都还给调用方——部分边界只约定「非 0」不给定值。"""
    ns = build_parser().parse_args(["--repo", str(repo.root), *args])
    try:
        out, code = dispatch(ns)
    except Problem as exc:
        out, code = exc.to_json(), exc.exit_code
    return out, code


def _answer_askuserquestion(hook, session: str = "S1") -> None:
    hook("PreToolUse", {"session_id": session, "tool_name": "AskUserQuestion", "tool_use_id": "q-hold"})
    hook("PostToolUse", {"session_id": session, "tool_name": "AskUserQuestion"})


def _ready_to_finalize(started, cli, hook) -> None:
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook)
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "finalize"


# ---------------------------------------------------------------- B1


def test_hold_succeeds_after_gate_pass_and_user_answer(started, cli, hook):
    _ready_to_finalize(started, cli, hook)
    _answer_askuserquestion(hook)
    out = cli("hold", "--session", "S1", "--reason", "等 #228 发版")
    assert out.get("ok", True) is not False

    lg = L.load(started["ledger"])
    assert "hold" not in lg  # 不新增 ledger 顶层字段：从事件派生
    assert lg["events"][-1]["kind"] == "hold" and lg["events"][-1]["reason"] == "等 #228 发版"

    st = cli("status", "--session", "S1")
    assert st["readiness"]["next_action"] == "held"
    assert st["hold"]["reason"] == "等 #228 发版"
    assert isinstance(st["hold"]["at"], str) and st["hold"]["at"]


def test_hold_boundary_no_answer_at_all(started, cli, hook):
    _ready_to_finalize(started, cli, hook)
    n_events = len(L.load(started["ledger"])["events"])
    out = cli("hold", "--session", "S1", "--reason", "等 #228 发版", expect=3)
    assert out["code"] == "USER_DECISION_REQUIRED"
    assert len(L.load(started["ledger"])["events"]) == n_events


def test_hold_boundary_answer_predates_last_evidence(started, cli, hook):
    # 回答早于最后一项 evidence 的 at：不算数
    _answer_askuserquestion(hook)
    _ready_to_finalize(started, cli, hook)
    out = cli("hold", "--session", "S1", "--reason", "太早了", expect=3)
    assert out["code"] == "USER_DECISION_REQUIRED"


def test_hold_boundary_task_notification_is_not_an_answer(started, cli, hook):
    _ready_to_finalize(started, cli, hook)
    hook("UserPromptSubmit", {"session_id": "S1", "prompt": "<task-notification>…</task-notification>"})
    out = cli("hold", "--session", "S1", "--reason", "x", expect=3)
    assert out["code"] == "USER_DECISION_REQUIRED"


def test_hold_boundary_next_action_not_finalize(started, cli, hook):
    # 刚 start，next_action 还是 spawn_tester
    assert cli("status", "--session", "S1")["readiness"]["next_action"] != "finalize"
    out = cli("hold", "--session", "S1", "--reason", "太早了", expect=1)
    assert out["code"] == "HOLD_NOT_READY"


def test_hold_boundary_reason_required(started, cli, hook, repo):
    _ready_to_finalize(started, cli, hook)
    _answer_askuserquestion(hook)
    out, code = _call(repo, "hold", "--session", "S1", "--reason", "   ")
    assert code != 0
    assert out["code"] == "REASON_REQUIRED"


def test_hold_boundary_run_terminal(started, cli, hook, repo):
    cli("abandon", "--session", "S1", "--reason", "不做了")
    out, code = _call(repo, "hold", "--session", "S1", "--reason", "x")
    assert code != 0
    assert out["code"] == "RUN_TERMINAL"


def test_resume_authorization_semantics_unchanged(started, cli, hook):
    """不变量的针对性重申：hold 相关改动不影响 resume 授权语义（完整场景见
    test_machine_evidence.py::test_iteration_limit_is_a_real_stop_and_needs_user_authorization）。"""
    assert cli("resume", "--session", "S1", "--reason", "没有 blocker", expect=1)["code"] == "NOTHING_TO_RESUME"


def test_no_hold_gate_pass_next_action_still_finalize(started, cli, hook):
    _ready_to_finalize(started, cli, hook)
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "finalize"


# ---------------------------------------------------------------- B2


def test_stop_permissive_during_hold_and_finalize_rejected(started, cli, hook):
    repo = started["repo"]
    _ready_to_finalize(started, cli, hook)
    _answer_askuserquestion(hook)
    cli("hold", "--session", "S1", "--reason", "等 #228 发版")

    lg_before = L.load(started["ledger"])
    stall_before = lg_before["counters"]["stall"]["count"]
    target_head_before = git(repo.root, "rev-parse", lg_before["repo"]["target_branch"])

    for active in (True, False):
        r = hook("Stop", {"session_id": "S1", "stop_hook_active": active})
        assert r["code"] == 0 and r["stderr"] == ""

    lg_after = L.load(started["ledger"])
    assert lg_after["counters"]["stall"]["count"] == stall_before
    assert [e for e in lg_after["events"] if e["kind"] == "stall_escape"] == []

    out = cli("finalize", "--session", "S1", "-m", "feat(x): [cr_id_skip] X", expect=1)
    assert out["code"] == "HOLD_ACTIVE"
    assert git(repo.root, "rev-parse", lg_after["repo"]["target_branch"]) == target_head_before
    assert not L.load(started["ledger"]).get("terminal")


def test_stop_resumes_blocking_when_candidate_goes_stale_during_hold(started, cli, hook):
    _ready_to_finalize(started, cli, hook)
    _answer_askuserquestion(hook)
    cli("hold", "--session", "S1", "--reason", "等 #228 发版")

    wt = started["worktree"]
    (wt / "src" / "foo.py").write_text((wt / "src" / "foo.py").read_text() + "\n# touch after hold\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")

    st = cli("status", "--session", "S1")["readiness"]
    assert st["next_action"] != "held" and st["next_action"] == "machine"

    r = hook("Stop", {"session_id": "S1", "stop_hook_active": False})
    assert r["code"] == 2 and "next_action=machine" in r["stderr"]


# ---------------------------------------------------------------- B3


def test_hold_release(started, cli, hook):
    _ready_to_finalize(started, cli, hook)
    _answer_askuserquestion(hook)
    cli("hold", "--session", "S1", "--reason", "等 #228 发版")

    out = cli("hold", "--session", "S1", "--release")
    assert out.get("ok", True) is not False

    lg = L.load(started["ledger"])
    assert lg["events"][-1]["kind"] == "hold_release"

    st = cli("status", "--session", "S1")
    assert st["readiness"]["next_action"] == "finalize"
    assert st["hold"] is None

    r = hook("Stop", {"session_id": "S1", "stop_hook_active": False})
    assert r["code"] == 2 and "next_action=finalize" in r["stderr"]

    out = cli("finalize", "--session", "S1", "-m", "feat(x): [cr_id_skip] X")
    assert out["terminal"] == "finalized"


def test_hold_release_boundary_nothing_to_release(started, cli, hook):
    _ready_to_finalize(started, cli, hook)
    out = cli("hold", "--session", "S1", "--release", expect=1)
    assert out["code"] == "NOTHING_TO_RELEASE"


def test_hold_release_then_target_drift(started, cli, hook):
    repo = started["repo"]
    _ready_to_finalize(started, cli, hook)
    _answer_askuserquestion(hook)
    cli("hold", "--session", "S1", "--reason", "等 #228 发版")

    (repo.root / "README.md").write_text("hi\n")
    repo.commit_all("docs(x): [cr_id_skip] Readme")

    out = cli("hold", "--session", "S1", "--release")
    assert out.get("ok", True) is not False

    err = cli("finalize", "--session", "S1", "-m", "feat(x): [cr_id_skip] X", expect=1)
    assert err["code"] == "TARGET_DRIFT"


def test_hold_release_then_hold_again_requires_new_answer(started, cli, hook):
    """授权锚点 = max(最近一次 hold_release 事件的时刻, 本 run 内 gate 首次全部 pass 的时刻)：
    release 会把锚点推到 release 那一刻，release 之前的回答不再够用（rebase-integrate-evidence
    run，B8）。这取代了旧版「reuse 上一次回答」的假设——那是这次要改的行为，不是要保留的不变量。"""
    _ready_to_finalize(started, cli, hook)
    _answer_askuserquestion(hook)
    cli("hold", "--session", "S1", "--reason", "第一次")
    cli("hold", "--session", "S1", "--release")

    n_events = len(L.load(started["ledger"])["events"])
    out = cli("hold", "--session", "S1", "--reason", "第二次", expect=3)
    assert out["code"] == "USER_DECISION_REQUIRED"
    assert len(L.load(started["ledger"])["events"]) == n_events

    _answer_askuserquestion(hook)
    out = cli("hold", "--session", "S1", "--reason", "第二次")
    assert out.get("ok", True) is not False
    assert L.load(started["ledger"])["events"][-1]["kind"] == "hold"
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "held"


def test_hold_release_does_not_touch_evidence_or_machine_iter(started, cli, hook):
    _ready_to_finalize(started, cli, hook)
    _answer_askuserquestion(hook)
    cli("hold", "--session", "S1", "--reason", "等 #228 发版")
    before = L.load(started["ledger"])
    cli("hold", "--session", "S1", "--release")
    after = L.load(started["ledger"])
    assert after["evidence"] == before["evidence"]
    assert after["counters"]["machine_iter"] == before["counters"]["machine_iter"]


# ---------------------------------------------------------------- B4


def test_stall_escape_hints_bl_hold_when_finalize_ready(started, cli, hook):
    _ready_to_finalize(started, cli, hook)
    # 建立 baseline（不计入「3 次 active」）
    r0 = hook("Stop", {"session_id": "S1", "stop_hook_active": False})
    assert r0["code"] == 2

    r1 = hook("Stop", {"session_id": "S1", "stop_hook_active": True})
    assert r1["code"] == 2
    r2 = hook("Stop", {"session_id": "S1", "stop_hook_active": True})
    assert r2["code"] == 2
    r3 = hook("Stop", {"session_id": "S1", "stop_hook_active": True})
    assert r3["code"] == 0 and "bl hold" in r3["stderr"]

    kinds = [e["kind"] for e in L.load(started["ledger"])["events"]]
    assert "stall_escape" in kinds
