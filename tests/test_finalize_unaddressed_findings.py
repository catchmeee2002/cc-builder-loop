"""B9: reviewer pass 但仍留有 owner=tester 的 finding 时，finalize 的返回里要点名它
（unaddressed_findings），但不能因此阻塞 finalize 本身。
"""

from __future__ import annotations

from builder_loop import ledger as L
from conftest import drive_to_proof_pass, role_turn


def _reviewer_pass_with_findings(hook, findings, agent_id="R1"):
    r = role_turn(hook, "reviewer", agent_id, {
        "role": "reviewer", "verdict": "pass", "findings": findings, "behaviors_verified": ["B1"],
    })
    assert r["code"] == 0, r


def test_b9_finalize_reports_unaddressed_tester_owned_finding(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    finding = {"severity": "minor", "owner": "tester", "file": "tests/test_mul.py", "line": 1, "summary": "命名可以更清楚"}
    _reviewer_pass_with_findings(hook, [finding])
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "finalize"

    out = cli("finalize", "--session", "S1")
    assert out["terminal"] == "finalized"  # 不变量：finalize 仍然成功，不因存在该 finding 而阻塞
    assert "unaddressed_findings" in out
    assert finding in out["unaddressed_findings"]


def test_b9_boundary_no_tester_owned_findings_is_empty(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    builder_finding = {"severity": "minor", "owner": "builder", "file": "src/foo.py", "line": 1, "summary": "命名"}
    _reviewer_pass_with_findings(hook, [builder_finding])
    out = cli("finalize", "--session", "S1")
    assert out["terminal"] == "finalized"
    assert out.get("unaddressed_findings") == []


def test_b9_boundary_builder_owned_finding_excluded_when_tester_finding_also_present(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    tester_finding = {"severity": "minor", "owner": "tester", "file": "tests/test_mul.py", "line": 1, "summary": "t"}
    builder_finding = {"severity": "minor", "owner": "builder", "file": "src/foo.py", "line": 1, "summary": "b"}
    _reviewer_pass_with_findings(hook, [tester_finding, builder_finding])
    out = cli("finalize", "--session", "S1")
    assert tester_finding in out["unaddressed_findings"]
    assert builder_finding not in out["unaddressed_findings"]
