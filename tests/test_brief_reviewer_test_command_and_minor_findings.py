"""B2：reviewer evidence（verdict pass 或 changes_requested）里既有 owner=tester 的 minor finding，
也有 owner=builder 的 minor finding，且 tester 最后一次交卷早于该 reviewer evidence —— tester 的
brief 出现一条可选的 consider_minor_findings 待办，只含 owner=tester 且 severity=minor 的条目。

B3：reviewer 的 brief 携带 test_command = contract.assurance.proof_runner.cmd，render 文本里同时
出现这条命令与字面锚句「复跑测试：在候选 worktree 里用冻结的测试命令」；proof_runner 缺失或没有
cmd 时 test_command 为 None，render 不输出这一行也不报错。
"""

from __future__ import annotations

from builder_loop import brief as brief_mod
from builder_loop import ledger as L
from conftest import drive_to_proof_pass, role_turn


def _tester_todo_whats(lg, root):
    b = brief_mod.build(lg, root, "tester")
    return [t["what"] for t in b["todo"]], b


# ---------------------------------------------------------------- B2


def test_consider_minor_findings_appears_and_render_includes_it(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    findings = [
        {"severity": "minor", "owner": "tester", "file": "tests/test_mul.py", "line": 1, "summary": "断言可以更严格"},
        {"severity": "minor", "owner": "builder", "file": "src/foo.py", "line": 1, "summary": "命名风格"},
    ]
    r = role_turn(hook, "reviewer", "R1",
                  {"role": "reviewer", "verdict": "changes_requested", "findings": findings, "behaviors_verified": ["B1"]})
    assert r["code"] == 0, r

    lg = L.load(started["ledger"])
    root = started["repo"].root
    whats, b = _tester_todo_whats(lg, root)
    assert "consider_minor_findings" in whats
    entry = next(t for t in b["todo"] if t["what"] == "consider_minor_findings")
    assert entry.get("optional") is True
    assert entry["findings"] == [f for f in findings if f["owner"] == "tester" and f["severity"] == "minor"]

    text = brief_mod.render(b)
    assert "consider_minor_findings" in text


def test_boundary_no_minor_owned_by_tester_no_todo(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    findings = [{"severity": "minor", "owner": "builder", "file": "src/foo.py", "line": 1, "summary": "x"}]
    r = role_turn(hook, "reviewer", "R1",
                  {"role": "reviewer", "verdict": "changes_requested", "findings": findings, "behaviors_verified": ["B1"]})
    assert r["code"] == 0, r
    lg = L.load(started["ledger"])
    whats, _ = _tester_todo_whats(lg, started["repo"].root)
    assert "consider_minor_findings" not in whats


def test_boundary_tester_replied_after_reviewer_no_todo(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    findings = [{"severity": "minor", "owner": "tester", "file": "tests/test_mul.py", "line": 1, "summary": "x"}]
    r = role_turn(hook, "reviewer", "R1",
                  {"role": "reviewer", "verdict": "changes_requested", "findings": findings, "behaviors_verified": ["B1"]})
    assert r["code"] == 0, r

    # tester 在 reviewer 之后又交了一次（无改动，declined）
    r2 = role_turn(hook, "tester", "T1", {"role": "tester", "status": "insufficient_spec", "notes": "nothing to add"})
    assert r2["code"] == 0, r2

    lg = L.load(started["ledger"])
    whats, _ = _tester_todo_whats(lg, started["repo"].root)
    assert "consider_minor_findings" not in whats


def test_invariant_owner_tester_blocking_major_still_only_fix_review_findings(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    findings = [
        {"severity": "major", "owner": "tester", "file": "tests/test_mul.py", "line": 1, "summary": "缺边界"},
        {"severity": "minor", "owner": "tester", "file": "tests/test_mul.py", "line": 2, "summary": "命名"},
    ]
    r = role_turn(hook, "reviewer", "R1",
                  {"role": "reviewer", "verdict": "changes_requested", "findings": findings, "behaviors_verified": ["B1"]})
    assert r["code"] == 0, r
    lg = L.load(started["ledger"])
    whats, b = _tester_todo_whats(lg, started["repo"].root)
    assert "fix_review_findings" in whats
    assert "consider_minor_findings" in whats
    fix = next(t for t in b["todo"] if t["what"] == "fix_review_findings")
    assert all(f["severity"] in ("blocking", "major") for f in fix["findings"])
    minor = next(t for t in b["todo"] if t["what"] == "consider_minor_findings")
    assert all(f["severity"] == "minor" for f in minor["findings"])


def test_invariant_reviewer_pass_only_minor_still_finalize(started, cli, hook):
    from builder_loop import evidence

    drive_to_proof_pass(started, cli, hook)
    findings = [{"severity": "minor", "owner": "tester", "file": "tests/test_mul.py", "line": 1, "summary": "x"}]
    r = role_turn(hook, "reviewer", "R1",
                  {"role": "reviewer", "verdict": "pass", "findings": findings, "behaviors_verified": ["B1"]})
    assert r["code"] == 0, r
    lg = L.load(started["ledger"])
    assert evidence.readiness(lg, started["repo"].root)["next_action"] == "finalize"


# ---------------------------------------------------------------- B3


def test_reviewer_brief_has_test_command_and_render_anchor(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    lg = L.load(started["ledger"])
    b = brief_mod.build(lg, started["repo"].root, "reviewer")
    cmd = lg["contract"]["assurance"]["proof_runner"]["cmd"]
    assert b["test_command"] == cmd

    text = brief_mod.render(b)
    assert cmd in text
    assert "复跑测试：在候选 worktree 里用冻结的测试命令" in text


def test_boundary_test_command_null_when_proof_runner_missing(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    lg = L.load(started["ledger"])
    lg["contract"]["assurance"].pop("proof_runner", None)
    b = brief_mod.build(lg, started["repo"].root, "reviewer")
    assert b.get("test_command") is None
    text = brief_mod.render(b)
    assert "复跑测试" not in text


def test_invariant_reviewer_brief_other_fields_unchanged(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    lg = L.load(started["ledger"])
    b = brief_mod.build(lg, started["repo"].root, "reviewer")
    for key in ("candidate_worktree", "diff_range", "evidence", "review_focus", "todo"):
        assert key in b
