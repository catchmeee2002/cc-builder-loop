"""B6: machine 失败日志里提到的、落在 authority.tester_write 内的路径都要出现在
failure.tester_files_mentioned 里——不限于 tester 本 run 实际改过的文件（按 path_owner 归属判定）。
B7/B8: readiness 据此在 tester 没回应前把 next_action 改成 resume_tester，回应后回到 machine。
"""

from __future__ import annotations

from pathlib import Path

from builder_loop import ledger as L
from conftest import contract_with, implement_mul, make_tester_result, role_turn, write_mul_test, write_plan


def _start_lite_failing(repo, cli, msg: str, *, builder_write=None, tester_write=None, session="S1"):
    (repo.root / ".claude" / "loop.yml").write_text(
        f"pass_cmd:\n  - stage: mut\n    cmd: \"echo '{msg}' && exit 1\"\n    timeout: 5\n", encoding="utf-8")
    repo.commit_all()
    patches = {"assurance.required": ["machine", "reviewer"]}
    if builder_write is not None:
        patches["authority.builder_write"] = builder_write
    if tester_write is not None:
        patches["authority.tester_write"] = tester_write
    write_plan(repo.root, contract_with(**patches), "lite.md")
    out = cli("start", "--plan", str(repo.root / "lite.md"), "--session", session)
    return Path(out["worktree"]), Path(out["ledger"])


def test_b6_tester_owned_path_mentioned_in_failure_log_is_attributed_even_if_untouched(repo, cli):
    """tester 本 run 根本没跑（不在 assurance.required 里），日志仍提到一条落在 tester_write 内的既有路径。"""
    wt, lp = _start_lite_failing(repo, cli, "see tests/test_foo.py for details")
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    res = cli("machine", "--session", "S1", expect=1)
    assert "tests/test_foo.py" in res["failure"]["tester_files_mentioned"]


def test_b6_boundary_builder_owned_path_is_not_attributed(repo, cli):
    wt, lp = _start_lite_failing(repo, cli, "see src/foo.py for details")
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    res = cli("machine", "--session", "S1", expect=1)
    assert "src/foo.py" not in res["failure"]["tester_files_mentioned"]


def test_b6_boundary_overlap_owned_by_tester_wins(repo, cli):
    """builder_write 与 tester_write 都命中同一路径时按 path_owner 归 tester。"""
    wt, lp = _start_lite_failing(repo, cli, "see tests/test_foo.py for details",
                                  builder_write=["**"], tester_write=["tests/**"])
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    res = cli("machine", "--session", "S1", expect=1)
    assert "tests/test_foo.py" in res["failure"]["tester_files_mentioned"]


def test_b6_invariant_tester_edited_file_mentioned_in_failure_still_shows(started, cli, hook):
    """不变量：tester 本 run 改过、且被日志提到的文件仍然出现（既有行为不能被新逻辑削弱）。"""
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt, "assert mul(3, 4) == 13  # 写错了")
    role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    cli("integrate", "--session", "S1")
    res = cli("machine", "--session", "S1", expect=1)
    assert res["failure"]["tester_files_mentioned"] == ["tests/test_mul.py"]


def test_b7_readiness_resume_tester_when_machine_fail_mentions_tester_file(started, cli, hook):
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt, "assert mul(3, 4) == 13  # 写错了")
    role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    cli("integrate", "--session", "S1")
    res = cli("machine", "--session", "S1", expect=1)
    assert res["failure"]["tester_files_mentioned"] == ["tests/test_mul.py"]
    assert res["readiness"]["next_action"] == "resume_tester"
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "resume_tester"


def test_b7_boundary_empty_mentions_keeps_machine_action(repo, cli):
    """边界：tester_files_mentioned 为空时 next_action 仍为 machine。"""
    wt, lp = _start_lite_failing(repo, cli, "unrelated failure, nothing to see here")
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    res = cli("machine", "--session", "S1", expect=1)
    assert res["failure"]["tester_files_mentioned"] == []
    assert res["readiness"]["next_action"] == "machine"


def test_b7_boundary_never_spawned_tester_is_spawn_tester(started, cli):
    """边界：tester 从未 spawn 过时为 spawn_tester。"""
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "spawn_tester"


def test_b8_readiness_returns_to_machine_after_tester_replies(started, cli, hook):
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt, "assert mul(3, 4) == 13  # 写错了")
    role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    cli("integrate", "--session", "S1")
    res = cli("machine", "--session", "S1", expect=1)
    assert res["readiness"]["next_action"] == "resume_tester"

    # tester 交了一次卷（哪怕内容没变）：readiness 应回到 machine，不再挂着 resume_tester。
    # 真实续接是 builder SendMessage：SubagentStart 与 Stop/handback 都会重发，开一个新 turn，
    # 否则同一 turn 内重复同一份结论会被去重，不产生新的 role_result（#257 的 _already_recorded）。
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=True)
    assert r["code"] == 0, r
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "machine"
    lg = L.load(started["ledger"])
    assert lg["evidence"]["machine"]["status"] == "fail"  # 判据结论本身没变，只是回应过了
