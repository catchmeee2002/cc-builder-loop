"""B1：reviewer 这一轮里候选 worktree 的 git 状态被改动（HEAD 不变）——修改 tracked 文件内容、
`git add` 造成 index 变化、或新建未被 ignore 的 untracked 文件——则它这次交的结论（即便是
verdict=pass、findings=[]）作废：ledger.evidence.reviewer.status 记为 fail，details 带出改动了
哪些路径，readiness 回到 resume_reviewer，retro 信号里出现 S-role-candidate-changed。

覆盖对象：runtime/builder_loop/gitx.py::worktree_fingerprint（新函数）、role_start 事件新增
candidate_fingerprint、record_role_result 里 reviewer 分支新增的比对逻辑。
"""

from __future__ import annotations

from pathlib import Path

from builder_loop import ledger as L
from conftest import drive_to_proof_pass, git, handback


REVIEW_PASS = 'BUILDER_LOOP_RESULT: {"role":"reviewer","verdict":"pass","findings":[],"behaviors_verified":["B1"]}'


def _start_reviewer(hook, agent_id: str = "R1") -> None:
    hook("SubagentStart", {"session_id": "S1", "agent_id": agent_id, "agent_type": "reviewer"})


def _ev_status(started) -> str | None:
    return (L.load(started["ledger"])["evidence"].get("reviewer") or {}).get("status")


def _last_result(started) -> dict:
    res = [e for e in L.load(started["ledger"])["events"] if e["kind"] == "role_result" and e.get("role") == "reviewer"]
    return res[-1]


def _readiness(started, cli) -> str:
    return cli("status", "--session", "S1")["readiness"]["next_action"]


# ---------------------------------------------------------------- positive


def test_reviewer_result_invalid_when_tracked_file_content_changes_without_commit(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    wt = started["worktree"]
    head_before = git(wt, "rev-parse", "HEAD")
    _start_reviewer(hook)

    (wt / "src" / "foo.py").write_text((wt / "src" / "foo.py").read_text() + "\n# dirty, not committed\n")
    assert git(wt, "rev-parse", "HEAD") == head_before  # HEAD 没变，只是工作树脏了

    r = handback(hook, "reviewer", "R1", REVIEW_PASS)
    assert r["code"] == 0, r

    assert _ev_status(started) == "fail"
    rec = L.load(started["ledger"])["evidence"]["reviewer"]
    assert rec["details"].get("reason")
    assert "src/foo.py" in rec["details"].get("candidate_changed", [])

    res = _last_result(started)
    assert res.get("agent_id") == "R1"

    changed_events = [e for e in L.load(started["ledger"])["events"]
                      if e["kind"] == "role_candidate_changed" and e.get("role") == "reviewer" and e.get("agent_id") == "R1"]
    assert len(changed_events) == 1, changed_events

    assert _readiness(started, cli) == "resume_reviewer"

    git(wt, "checkout", "--", "src/foo.py")  # 清理，避免污染后续 fixture 状态


def test_reviewer_result_invalid_when_index_changes_without_commit(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    wt = started["worktree"]
    _start_reviewer(hook)

    (wt / "src" / "foo.py").write_text((wt / "src" / "foo.py").read_text() + "\n# staged only\n")
    git(wt, "add", "src/foo.py")

    r = handback(hook, "reviewer", "R1", REVIEW_PASS)
    assert r["code"] == 0, r
    assert _ev_status(started) == "fail"
    rec = L.load(started["ledger"])["evidence"]["reviewer"]
    assert rec["details"].get("index_changed") is True

    git(wt, "reset", "--hard", "HEAD")


def test_reviewer_result_invalid_when_untracked_file_created(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    wt = started["worktree"]
    _start_reviewer(hook)

    (wt / "src" / "surprise.py").write_text("# new untracked file\n")

    r = handback(hook, "reviewer", "R1", REVIEW_PASS)
    assert r["code"] == 0, r
    assert _ev_status(started) == "fail"
    rec = L.load(started["ledger"])["evidence"]["reviewer"]
    assert "src/surprise.py" in rec["details"].get("candidate_changed", [])

    (wt / "src" / "surprise.py").unlink()


def test_retro_signal_present_for_role_candidate_changed(started, cli, hook):
    from builder_loop import retro

    drive_to_proof_pass(started, cli, hook)
    wt = started["worktree"]
    _start_reviewer(hook)
    (wt / "src" / "foo.py").write_text((wt / "src" / "foo.py").read_text() + "\n# dirty\n")
    handback(hook, "reviewer", "R1", REVIEW_PASS)
    git(wt, "checkout", "--", "src/foo.py")

    cli("abandon", "--session", "S1", "--reason", "done for the test")
    sigs = retro.signals(started["ledger"])
    assert any(s["id"] == "S-role-candidate-changed" for s in sigs["signals"]), sigs["signals"]


# ---------------------------------------------------------------- boundaries


def test_boundary_no_change_still_registers_pass(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    _start_reviewer(hook)
    r = handback(hook, "reviewer", "R1", REVIEW_PASS)
    assert r["code"] == 0, r
    assert _ev_status(started) == "pass"
    changed_events = [e for e in L.load(started["ledger"])["events"] if e["kind"] == "role_candidate_changed"]
    assert changed_events == []


def test_boundary_preexisting_dirty_state_unchanged_during_review_still_passes(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    wt = started["worktree"]
    # 这一轮开始之前就已经有未提交改动
    (wt / "src" / "already_dirty.py").write_text("# pre-existing\n")
    _start_reviewer(hook)
    # 期间没有再变化
    r = handback(hook, "reviewer", "R1", REVIEW_PASS)
    assert r["code"] == 0, r
    assert _ev_status(started) == "pass"
    changed_events = [e for e in L.load(started["ledger"])["events"] if e["kind"] == "role_candidate_changed"]
    assert changed_events == []
    (wt / "src" / "already_dirty.py").unlink()


def test_boundary_only_ignored_untracked_file_does_not_count(started, cli, hook):
    """fixture 仓库的 .gitignore 已经声明 `__pycache__/`：在候选里造一个落在这条规则下的 untracked
    文件，不改 .gitignore 本身（改它不归 builder_write，checkpoint 会拒）。"""
    drive_to_proof_pass(started, cli, hook)
    wt = started["worktree"]
    _start_reviewer(hook)
    (wt / "src" / "__pycache__").mkdir(exist_ok=True)
    (wt / "src" / "__pycache__" / "junk.pyc").write_text("noise\n")

    r = handback(hook, "reviewer", "R1", REVIEW_PASS)
    assert r["code"] == 0, r
    assert _ev_status(started) == "pass"
    changed_events = [e for e in L.load(started["ledger"])["events"] if e["kind"] == "role_candidate_changed"]
    assert changed_events == []


def test_boundary_role_start_without_candidate_fingerprint_skips_comparison(started, cli, hook):
    """旧 run（或取指纹失败）：role_start 事件没有 candidate_fingerprint 字段 → 不做比对，照常登记。"""
    drive_to_proof_pass(started, cli, hook)
    wt = started["worktree"]
    _start_reviewer(hook)
    with L.mutate(started["ledger"]) as lg2:
        for e in lg2["events"]:
            if e["kind"] == "role_start" and e.get("role") == "reviewer" and e.get("agent_id") == "R1":
                e.pop("candidate_fingerprint", None)

    (wt / "src" / "foo.py").write_text((wt / "src" / "foo.py").read_text() + "\n# dirty but not compared\n")
    r = handback(hook, "reviewer", "R1", REVIEW_PASS)
    assert r["code"] == 0, r
    assert _ev_status(started) == "pass"
    git(wt, "checkout", "--", "src/foo.py")
