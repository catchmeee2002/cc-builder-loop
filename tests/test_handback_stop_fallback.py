"""无 SubagentHandback 的环境：SubagentStop 解析最后一条消息兜底登记角色结果；
有 handback 的轮次仍只认 handback；doctor 不再检查 CC 版本。"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

import pytest

import builder_loop.ledger as L
from conftest import handback, implement_mul, make_tester_result, marker, write_mul_test

REPO_ROOT = Path(__file__).resolve().parents[1]
HEX64 = re.compile(r"^[0-9a-f]{64}$")
REVIEW_PASS = {"role": "reviewer", "verdict": "pass", "findings": [], "behaviors_verified": ["B1"]}
REVIEW_CHANGES = {"role": "reviewer", "verdict": "changes_requested",
                  "findings": [{"severity": "major", "owner": "builder", "file": "src/foo.py", "line": 5, "summary": "缺 0 边界"}],
                  "behaviors_verified": []}


def _start(hook, role: str, agent_id: str) -> None:
    hook("SubagentStart", {"session_id": "S1", "agent_id": agent_id, "agent_type": role})


def _stop(hook, role: str, agent_id: str, text: str, *, active: bool = False):
    return hook("SubagentStop", {"session_id": "S1", "agent_id": agent_id, "agent_type": role,
                                 "last_assistant_message": text, "stop_hook_active": active})


def _all_events(started) -> list[dict]:
    return L.load(started["ledger"])["events"]


def _events(started, kind: str, role: str | None = None) -> list[dict]:
    return [e for e in _all_events(started) if e["kind"] == kind and (role is None or e.get("role") == role)]


def _results(started, role: str) -> list[dict]:
    return _events(started, "role_result", role)


def _evidence(started, role: str):
    return L.load(started["ledger"])["evidence"].get(role)


def _ev_status(started, role: str):
    return (_evidence(started, role) or {}).get("status")


def _tester_ready(started, cli, hook) -> None:
    _start(hook, "tester", "T1")
    implement_mul(started["worktree"])
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(started["tester_worktree"])


def _reviewer_ready(started, cli, hook) -> None:
    implement_mul(started["worktree"])
    cli("checkpoint", "--session", "S1", "--role", "builder")
    _start(hook, "reviewer", "R1")


# ---------------------------------------------------------------- C1


def test_c1_tester_result_recorded_from_stop(started, cli, hook):
    _tester_ready(started, cli, hook)
    r = _stop(hook, "tester", "T1", "done\nBUILDER_LOOP_RESULT: " + json.dumps(make_tester_result("mutation")))
    assert r["code"] == 0, r
    assert _ev_status(started, "tester") == "pass"
    res = _results(started, "tester")
    assert len(res) == 1, res
    assert res[0].get("agent_id") == "T1" and res[0].get("via") == "stop", res
    assert isinstance(res[0].get("payload_sha256"), str) and HEX64.match(res[0]["payload_sha256"]), res[0]
    st = cli("status", "--session", "S1")
    assert st["running"]["tester"] is False
    assert st["readiness"]["next_action"] == "integrate"


def test_c1_reviewer_result_recorded_from_stop(started, cli, hook):
    _reviewer_ready(started, cli, hook)
    r = _stop(hook, "reviewer", "R1", marker(REVIEW_PASS))
    assert r["code"] == 0, r
    res = _results(started, "reviewer")
    assert len(res) == 1 and res[0].get("via") == "stop" and res[0].get("agent_id") == "R1", res
    assert isinstance(res[0].get("payload_sha256"), str) and HEX64.match(res[0]["payload_sha256"]), res[0]
    assert _ev_status(started, "reviewer") == "pass"


@pytest.mark.parametrize("second", ["已交付。", "", "done\nBUILDER_LOOP_RESULT: {bad json"])
def test_c1_second_stop_same_turn_adds_nothing(started, cli, hook, second):
    _tester_ready(started, cli, hook)
    assert _stop(hook, "tester", "T1", marker(make_tester_result("mutation")))["code"] == 0
    assert len(_results(started, "tester")) == 1
    n = len(_all_events(started))
    r = _stop(hook, "tester", "T1", second, active=True)
    assert r["code"] == 0, r
    assert len(_all_events(started)) == n
    assert _ev_status(started, "tester") == "pass"


def test_c1_resumed_turn_stop_records_again(started, cli, hook):
    _tester_ready(started, cli, hook)
    assert _stop(hook, "tester", "T1", marker(make_tester_result("mutation")))["code"] == 0
    assert len(_results(started, "tester")) == 1
    _start(hook, "tester", "T1")  # 续接 = 新一轮
    write_mul_test(started["tester_worktree"], "assert mul(2, 5) == 10")
    r = _stop(hook, "tester", "T1", marker(make_tester_result("mutation")))
    assert r["code"] == 0, r
    res = _results(started, "tester")
    assert len(res) == 2 and all(e.get("via") == "stop" for e in res), res


@pytest.mark.parametrize("agent_type,agent_id", [
    ("tester", "OTHER"), ("general-purpose", "T1"), ("", "T1"), ("builder", "T1"),
])
def test_c1_stop_from_unregistered_agent_is_silent(started, cli, hook, agent_type, agent_id):
    _tester_ready(started, cli, hook)
    before = (len(_all_events(started)), json.dumps(_evidence(started, "tester"), sort_keys=True))
    r = _stop(hook, agent_type, agent_id, marker(make_tester_result("mutation")))
    assert r["code"] == 0 and r["stdout"] == "" and r["stderr"] == "", r
    assert (len(_all_events(started)), json.dumps(_evidence(started, "tester"), sort_keys=True)) == before


# ---------------------------------------------------------------- C2


def test_c2_three_stops_without_marker(started, cli, hook):
    _tester_ready(started, cli, hook)
    for i in (1, 2):
        r = _stop(hook, "tester", "T1", "已交付。", active=(i > 1))
        assert r["code"] == 2, r
        assert "SubagentHandback" in r["stderr"] and "BUILDER_LOOP_RESULT" in r["stderr"], r["stderr"]
        mal = _events(started, "role_malformed", "tester")
        assert len(mal) == i and mal[-1].get("via") == "stop" and mal[-1].get("final") is False, mal
    r = _stop(hook, "tester", "T1", "已交付。", active=True)
    assert r["code"] == 0, r
    mal = _events(started, "role_malformed", "tester")
    assert len(mal) == 3 and mal[-1].get("final") is True and mal[-1].get("via") == "stop", mal
    assert _ev_status(started, "tester") == "fail"
    assert _results(started, "tester") == []


def test_c2_bad_json_in_stop_says_parse_failed(started, cli, hook):
    _tester_ready(started, cli, hook)
    r = _stop(hook, "tester", "T1", 'report\nBUILDER_LOOP_RESULT: {"role":"tester","status":"pass",}')
    assert r["code"] == 2 and "JSON 解析失败" in r["stderr"], r
    assert _results(started, "tester") == []


def test_c2_record_problem_code_in_stop_stderr(started, cli, hook):
    _tester_ready(started, cli, hook)
    (started["tester_worktree"] / "src" / "foo.py").write_text("# tester 越界\n")
    r = _stop(hook, "tester", "T1", marker(make_tester_result("mutation")))
    assert r["code"] == 2 and "CHECKPOINT_REJECTED" in r["stderr"], r
    assert _results(started, "tester") == []


def test_c2_recovers_after_one_malformed_stop(started, cli, hook):
    _tester_ready(started, cli, hook)
    assert _stop(hook, "tester", "T1", "已交付。")["code"] == 2
    r = _stop(hook, "tester", "T1", marker(make_tester_result("mutation")), active=True)
    assert r["code"] == 0, r
    res = _results(started, "tester")
    assert len(res) == 1 and res[0].get("via") == "stop", res
    assert _ev_status(started, "tester") == "pass"


# ---------------------------------------------------------------- C3


def _reviewer_changes_via_stop(started, cli, hook) -> None:
    _reviewer_ready(started, cli, hook)
    r = _stop(hook, "reviewer", "R1", "第一版报告\nBUILDER_LOOP_RESULT: " + json.dumps(REVIEW_CHANGES))
    assert r["code"] == 0, r
    res = _results(started, "reviewer")
    assert len(res) == 1 and res[0].get("via") == "stop", res
    assert _ev_status(started, "reviewer") == "fail"


def test_c3a_same_payload_handback_after_stop_is_deduped(started, cli, hook):
    _reviewer_changes_via_stop(started, cli, hook)
    again = "完全不同的正文\nBUILDER_LOOP_RESULT: " + json.dumps(REVIEW_CHANGES, separators=(",", ":"), ensure_ascii=False)
    r = handback(hook, "reviewer", "R1", again)
    assert r["code"] == 0, r
    assert len(_results(started, "reviewer")) == 1
    assert _ev_status(started, "reviewer") == "fail"


def test_c3b_different_payload_handback_after_stop_is_recorded(started, cli, hook):
    _reviewer_changes_via_stop(started, cli, hook)
    r = handback(hook, "reviewer", "R1", marker(REVIEW_PASS))
    assert r["code"] == 0, r
    res = _results(started, "reviewer")
    assert len(res) == 2 and res[-1].get("via") == "handback", res
    assert _ev_status(started, "reviewer") == "pass"


# ---------------------------------------------------------------- C4


def test_c4a_tester_stop_after_handback_is_silent(started, cli, hook):
    _tester_ready(started, cli, hook)
    assert handback(hook, "tester", "T1", marker(make_tester_result("mutation")))["code"] == 0
    n = len(_all_events(started))
    r = _stop(hook, "tester", "T1", "已交付。")
    assert r["code"] == 0, r
    assert len(_all_events(started)) == n


def test_c4a_reviewer_stop_after_handback_is_silent(started, cli, hook):
    _reviewer_ready(started, cli, hook)
    assert handback(hook, "reviewer", "R1", marker(REVIEW_PASS))["code"] == 0
    n = len(_all_events(started))
    r = _stop(hook, "reviewer", "R1", "已交付。")
    assert r["code"] == 0, r
    assert len(_all_events(started)) == n


def test_c4b_tester_stop_marker_ignored_after_malformed_handback(started, cli, hook):
    _tester_ready(started, cli, hook)
    assert handback(hook, "tester", "T1", "已交付。")["code"] == 2
    before_ev = json.dumps(_evidence(started, "tester"), sort_keys=True)
    n_mal = len(_events(started, "role_malformed", "tester"))
    r = _stop(hook, "tester", "T1", marker(make_tester_result("mutation")))
    assert r["code"] == 2 and "SubagentHandback" in r["stderr"], r
    mal = _events(started, "role_malformed", "tester")
    assert len(mal) == n_mal + 1 and mal[-1].get("via") == "stop", mal
    assert _results(started, "tester") == []
    assert json.dumps(_evidence(started, "tester"), sort_keys=True) == before_ev


def test_c4b_reviewer_stop_marker_ignored_after_malformed_handback(started, cli, hook):
    _reviewer_ready(started, cli, hook)
    assert handback(hook, "reviewer", "R1", "已交付。")["code"] == 2
    before_ev = json.dumps(_evidence(started, "reviewer"), sort_keys=True)
    n_mal = len(_events(started, "role_malformed", "reviewer"))
    r = _stop(hook, "reviewer", "R1", marker(REVIEW_PASS))
    assert r["code"] == 2 and "SubagentHandback" in r["stderr"], r
    mal = _events(started, "role_malformed", "reviewer")
    assert len(mal) == n_mal + 1 and mal[-1].get("via") == "stop", mal
    assert _results(started, "reviewer") == []
    assert json.dumps(_evidence(started, "reviewer"), sort_keys=True) == before_ev


# ---------------------------------------------------------------- C5


def _doctor_with(tmp_path: Path, monkeypatch, with_claude: bool) -> dict:
    from builder_loop.doctor import doctor

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude_home"))
    monkeypatch.setenv("BUILDER_LOOP_HOME", str(tmp_path / "blhome"))
    base = tmp_path / "gitbin"
    base.mkdir()
    os.symlink(shutil.which("git"), base / "git")
    path = str(base)
    if with_claude:
        fake = tmp_path / "fakebin"
        fake.mkdir()
        exe = fake / "claude"
        exe.write_text("#!/bin/sh\necho '2.1.272 (Claude Code)'\n")
        exe.chmod(0o755)
        path = f"{fake}{os.pathsep}{path}"
    monkeypatch.setenv("PATH", path)
    return doctor(None)


def test_c5_doctor_has_no_claude_version_check(tmp_path_factory, monkeypatch):
    rep = _doctor_with(tmp_path_factory.mktemp("doc"), monkeypatch, True)
    assert "claude_version" not in rep, rep
    problems = rep["problems"]
    assert not any("Claude Code" in p or "2.1.273" in p for p in problems), problems
    # 既有检查照旧
    assert "hooks 未注册（运行 install.sh）" in problems, problems
    no_claude = _doctor_with(tmp_path_factory.mktemp("doc"), monkeypatch, False)
    assert problems == no_claude["problems"]


# ---------------------------------------------------------------- C6


def test_c6_brief_and_agents_mention_last_message_fallback(started):
    from builder_loop import brief as B

    lg = L.load(started["ledger"])
    root = started["repo"].root
    t = B.render(B.build(lg, root, "tester"))
    r = B.render(B.build(lg, root, "reviewer"))
    for text in (t, r):
        assert "SubagentHandback" in text and "最后一条消息" in text, text
    assert B.TESTER_RESULT_FORMAT in t
    assert B.REVIEWER_RESULT_FORMAT in r
    for name in ("tester.md", "reviewer.md"):
        body = (REPO_ROOT / "agents" / name).read_text(encoding="utf-8")
        assert "SubagentHandback" in body and "最后一条消息" in body, name
