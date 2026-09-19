"""CC 2.1.273 SubagentHandback：角色结果只认 PostToolUse(SubagentHandback) 的 message；
SubagentStop 不再解析结果；doctor 报告 CC 版本。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest

import builder_loop.ledger as L
from conftest import (
    drive_to_proof_pass, handback, implement_mul, make_tester_result, marker, write_mul_test,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
HEX64 = re.compile(r"^[0-9a-f]{64}$")
REVIEW_PASS = {"role": "reviewer", "verdict": "pass", "findings": [], "behaviors_verified": ["B1"]}
REVIEW_CHANGES = {"role": "reviewer", "verdict": "changes_requested",
                  "findings": [{"severity": "major", "owner": "builder", "file": "src/foo.py", "line": 5, "summary": "缺 0 边界"}],
                  "behaviors_verified": []}


def _start(hook, role: str, agent_id: str) -> None:
    hook("SubagentStart", {"session_id": "S1", "agent_id": agent_id, "agent_type": role})


def _stop(hook, role: str, agent_id: str, text: str):
    return hook("SubagentStop", {"session_id": "S1", "agent_id": agent_id, "agent_type": role, "last_assistant_message": text})


def _events(started, kind: str, role: str | None = None) -> list[dict]:
    lg = L.load(started["ledger"])
    return [e for e in lg["events"] if e["kind"] == kind and (role is None or e.get("role") == role)]


def _ev_status(started, role: str):
    return (L.load(started["ledger"])["evidence"].get(role) or {}).get("status")


def _results(started, role: str) -> list[dict]:
    return _events(started, "role_result", role)


def _tester_ready(started, cli, hook) -> None:
    """已登记 tester T1、builder 已实现并 checkpoint、tester worktree 写了 tests/test_mul.py。"""
    _start(hook, "tester", "T1")
    implement_mul(started["worktree"])
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(started["tester_worktree"])


def _reviewer_ready(started, cli, hook) -> None:
    implement_mul(started["worktree"])
    cli("checkpoint", "--session", "S1", "--role", "builder")
    _start(hook, "reviewer", "R1")


# ---------------------------------------------------------------- B1


def test_b1_tester_result_recorded_from_handback(started, cli, hook):
    _tester_ready(started, cli, hook)
    r = handback(hook, "tester", "T1", "report\nBUILDER_LOOP_RESULT: " + json.dumps(make_tester_result("mutation")))
    assert r["code"] == 0, r
    assert _ev_status(started, "tester") == "pass"
    res = _results(started, "tester")
    assert len(res) == 1, res
    assert res[0].get("agent_id") == "T1" and res[0].get("via") == "handback"
    assert isinstance(res[0].get("payload_sha256"), str) and HEX64.match(res[0]["payload_sha256"]), res[0]
    st = cli("status", "--session", "S1")
    assert st["running"]["tester"] is False
    assert st["readiness"]["next_action"] == "integrate"


def test_b1_last_marker_line_wins(started, cli, hook):
    _tester_ready(started, cli, hook)
    bad = {"role": "tester", "status": "bogus"}
    msg = "BUILDER_LOOP_RESULT: " + json.dumps(bad) + "\n中间的说明\nBUILDER_LOOP_RESULT: " + json.dumps(make_tester_result("mutation"))
    r = handback(hook, "tester", "T1", msg)
    assert r["code"] == 0, r
    assert _ev_status(started, "tester") == "pass"
    assert len(_results(started, "tester")) == 1


def test_b1_declined_via_handback(started, cli, hook):
    _tester_ready(started, cli, hook)
    assert handback(hook, "tester", "T1", marker(make_tester_result("mutation")))["code"] == 0
    cli("integrate", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    _start(hook, "tester", "T1")  # 续接
    r = handback(hook, "tester", "T1", marker({"role": "tester", "status": "insufficient_spec", "notes": "补不出 patch"}))
    assert r["code"] == 0, r
    assert _ev_status(started, "tester") == "pass"
    res = _results(started, "tester")
    assert len(res) == 2 and res[-1].get("status") == "declined" and res[-1].get("via") == "handback", res


def test_b1_cli_record_marks_via_cli(started, cli, hook, tmp_path):
    _tester_ready(started, cli, hook)
    f = tmp_path / "payload.json"
    f.write_text(json.dumps(make_tester_result("mutation")), encoding="utf-8")
    cli("evidence", "--session", "S1", "record", "--kind", "tester", "--agent-id", "T1", "--payload-file", str(f))
    res = _results(started, "tester")
    assert len(res) == 1 and res[0].get("via") == "cli", res
    assert _ev_status(started, "tester") == "pass"


def test_b1_ask_user_question_still_records_user_input(started, hook):
    hook("PreToolUse", {"session_id": "S1", "tool_name": "AskUserQuestion", "tool_use_id": "u1"})
    assert L.load(started["ledger"])["waiting_for_user"]
    r = hook("PostToolUse", {"session_id": "S1", "tool_name": "AskUserQuestion", "tool_use_id": "u1"})
    assert r["code"] == 0
    lg = L.load(started["ledger"])
    assert not lg["waiting_for_user"]
    assert len([e for e in lg["events"] if e["kind"] == "user_input"]) == 1


# ---------------------------------------------------------------- B2


def test_b2_reviewer_result_from_handback(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    _start(hook, "reviewer", "R1")
    r = handback(hook, "reviewer", "R1", 'Verdict\nBUILDER_LOOP_RESULT: {"role":"reviewer","verdict":"pass","findings":[],"behaviors_verified":["B1"]}')
    assert r["code"] == 0, r
    assert _ev_status(started, "reviewer") == "pass"
    res = _results(started, "reviewer")
    assert len(res) == 1 and res[0].get("via") == "handback" and res[0].get("agent_id") == "R1", res
    st = cli("status", "--session", "S1")
    assert st["running"]["reviewer"] is False
    assert st["readiness"]["next_action"] == "finalize"


def test_b2_changes_requested_records_fail(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    _start(hook, "reviewer", "R1")
    assert handback(hook, "reviewer", "R1", marker(REVIEW_CHANGES))["code"] == 0
    assert _ev_status(started, "reviewer") == "fail"
    res = _results(started, "reviewer")
    assert len(res) == 1 and res[0].get("via") == "handback", res


def test_b2_candidate_moved_during_review_is_invalid(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    _start(hook, "reviewer", "R1")
    wt = started["worktree"]
    (wt / "src" / "foo.py").write_text((wt / "src" / "foo.py").read_text() + "\n# moved\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    assert handback(hook, "reviewer", "R1", marker(REVIEW_PASS))["code"] == 0
    assert _ev_status(started, "reviewer") == "fail"
    res = _results(started, "reviewer")
    assert len(res) == 1 and res[0].get("candidate_moved") is True and res[0].get("via") == "handback", res


def test_b2_reviewer_write_still_denied(started, cli, hook):
    _reviewer_ready(started, cli, hook)
    out = hook("PreToolUse", {"session_id": "S1", "agent_id": "R1", "agent_type": "reviewer", "tool_name": "Write",
                              "tool_input": {"file_path": str(started["worktree"] / "src" / "foo.py")}})
    assert out["json"]["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------- B3


def _snapshot(started):
    lg = L.load(started["ledger"])
    return len(lg["events"]), json.dumps(lg["evidence"].get("tester"), sort_keys=True)


@pytest.mark.parametrize("case", ["empty_type", "general_purpose", "other_agent", "abandoned"])
def test_b3_unregistered_handback_is_silent(started, cli, hook, case):
    _tester_ready(started, cli, hook)
    agent_type, agent_id = "tester", "T1"
    if case == "empty_type":
        agent_type = ""
    elif case == "general_purpose":
        agent_type = "general-purpose"
    elif case == "other_agent":
        agent_id = "OTHER"
    else:
        cli("abandon", "--session", "S1", "--reason", "测试放弃")
    before = _snapshot(started)
    r = handback(hook, agent_type, agent_id, marker(make_tester_result("mutation")))
    assert r["code"] == 0 and r["stdout"] == "" and r["stderr"] == "", r
    assert _snapshot(started) == before


def test_b3_unbound_session_is_silent(started, cli, hook):
    _tester_ready(started, cli, hook)
    before = _snapshot(started)
    r = handback(hook, "tester", "T1", marker(make_tester_result("mutation")), session="NOT-BOUND")
    assert r["code"] == 0 and r["stdout"] == "" and r["stderr"] == "", r
    assert _snapshot(started) == before


# ---------------------------------------------------------------- B4


def test_b4_three_malformed_handbacks(started, cli, hook):
    _tester_ready(started, cli, hook)
    for i in (1, 2):
        r = handback(hook, "tester", "T1", "已交付。")
        assert r["code"] == 2, r
        assert "SubagentHandback" in r["stderr"] and "BUILDER_LOOP_RESULT" in r["stderr"], r["stderr"]
        mal = _events(started, "role_malformed", "tester")
        assert len(mal) == i and mal[-1].get("via") == "handback" and mal[-1].get("final") is False, mal
    r = handback(hook, "tester", "T1", "已交付。")
    assert r["code"] == 0, r
    mal = _events(started, "role_malformed", "tester")
    assert len(mal) == 3 and mal[-1].get("final") is True and mal[-1].get("via") == "handback", mal
    assert _ev_status(started, "tester") == "fail"


def test_b4_bad_json_says_parse_failed(started, cli, hook):
    _tester_ready(started, cli, hook)
    r = handback(hook, "tester", "T1", 'report\nBUILDER_LOOP_RESULT: {"role":"tester","status":"pass",}')
    assert r["code"] == 2 and "JSON 解析失败" in r["stderr"], r


def test_b4_record_problem_code_in_stderr(started, cli, hook):
    _tester_ready(started, cli, hook)
    (started["tester_worktree"] / "src" / "foo.py").write_text("# tester 越界\n")
    r = handback(hook, "tester", "T1", marker(make_tester_result("mutation")))
    assert r["code"] == 2 and "CHECKPOINT_REJECTED" in r["stderr"], r
    assert _results(started, "tester") == []


def test_b4_recovers_after_one_malformed(started, cli, hook):
    _tester_ready(started, cli, hook)
    assert handback(hook, "tester", "T1", "已交付。")["code"] == 2
    r = handback(hook, "tester", "T1", marker(make_tester_result("mutation")))
    assert r["code"] == 0, r
    res = _results(started, "tester")
    assert len(res) == 1 and res[0].get("via") == "handback"
    assert _ev_status(started, "tester") == "pass"


# ---------------------------------------------------------------- B5


def _reviewer_changes_requested(started, cli, hook) -> None:
    _reviewer_ready(started, cli, hook)
    assert handback(hook, "reviewer", "R1", "第一版报告\n" + "BUILDER_LOOP_RESULT: " + json.dumps(REVIEW_CHANGES))["code"] == 0
    assert len(_results(started, "reviewer")) == 1


def test_b5a_same_payload_same_turn_is_deduped(started, cli, hook):
    _reviewer_changes_requested(started, cli, hook)
    # 报告正文不同、JSON 排版不同，解析后的对象相同 → 视为同一结果
    again = "完全不同的正文\n再来一段\nBUILDER_LOOP_RESULT: " + json.dumps(REVIEW_CHANGES, separators=(",", ":"), ensure_ascii=False)
    r = handback(hook, "reviewer", "R1", again)
    assert r["code"] == 0, r
    assert len(_results(started, "reviewer")) == 1
    assert _ev_status(started, "reviewer") == "fail"


def test_b5b_different_payload_same_turn_is_recorded(started, cli, hook):
    _reviewer_changes_requested(started, cli, hook)
    r = handback(hook, "reviewer", "R1", marker(REVIEW_PASS))
    assert r["code"] == 0, r
    assert len(_results(started, "reviewer")) == 2
    assert _ev_status(started, "reviewer") == "pass"


def test_b5c_same_payload_new_turn_is_recorded(started, cli, hook):
    _reviewer_changes_requested(started, cli, hook)
    _start(hook, "reviewer", "R1")  # 续接 = 新一轮
    r = handback(hook, "reviewer", "R1", "第一版报告\n" + "BUILDER_LOOP_RESULT: " + json.dumps(REVIEW_CHANGES))
    assert r["code"] == 0, r
    assert len(_results(started, "reviewer")) == 2


# ---------------------------------------------------------------- B6


def test_b6a_stop_after_handback_is_silent(started, cli, hook):
    _tester_ready(started, cli, hook)
    assert handback(hook, "tester", "T1", marker(make_tester_result("mutation")))["code"] == 0
    n = len(L.load(started["ledger"])["events"])
    for _ in range(2):
        r = _stop(hook, "tester", "T1", "已交付。")
        assert r["code"] == 0 and r["stderr"] == "", r
        assert len(L.load(started["ledger"])["events"]) == n


def test_b6a_reviewer_stop_after_handback_is_silent(started, cli, hook):
    _reviewer_ready(started, cli, hook)
    assert handback(hook, "reviewer", "R1", marker(REVIEW_PASS))["code"] == 0
    n = len(L.load(started["ledger"])["events"])
    for _ in range(2):
        r = _stop(hook, "reviewer", "R1", "已交付。")
        assert r["code"] == 0 and r["stderr"] == "", r
        assert len(L.load(started["ledger"])["events"]) == n


def test_b6b_stop_marker_is_not_parsed(started, cli, hook):
    _tester_ready(started, cli, hook)
    before = json.dumps(L.load(started["ledger"])["evidence"].get("tester"), sort_keys=True)
    r = _stop(hook, "tester", "T1", marker(make_tester_result("mutation")))
    assert r["code"] == 0, r
    assert _results(started, "tester") == []
    assert json.dumps(L.load(started["ledger"])["evidence"].get("tester"), sort_keys=True) == before


def test_b6b_reviewer_stop_marker_is_not_parsed(started, cli, hook):
    _reviewer_ready(started, cli, hook)
    r = _stop(hook, "reviewer", "R1", marker(REVIEW_PASS))
    assert r["code"] == 0, r
    assert _results(started, "reviewer") == []
    assert (L.load(started["ledger"])["evidence"].get("reviewer") or {}).get("status") != "pass"


def test_b6c_stop_without_handback_no_malformed(started, cli, hook):
    _tester_ready(started, cli, hook)
    r = _stop(hook, "tester", "T1", "已交付。")
    assert r["code"] == 0, r
    assert _events(started, "role_malformed") == []


def test_b6d_stop_after_malformed_handback_blocks(started, cli, hook):
    _tester_ready(started, cli, hook)
    assert handback(hook, "tester", "T1", "已交付。")["code"] == 2
    r = _stop(hook, "tester", "T1", "已交付。")
    assert r["code"] == 2 and "SubagentHandback" in r["stderr"], r
    mal = _events(started, "role_malformed", "tester")
    assert len(mal) == 2 and [m.get("via") for m in mal] == ["handback", "stop"], mal
    assert _results(started, "tester") == []


def test_b6_stop_unregistered_agent_is_silent(started, cli, hook):
    _tester_ready(started, cli, hook)
    assert handback(hook, "tester", "T1", "已交付。")["code"] == 2
    n = len(L.load(started["ledger"])["events"])
    r = _stop(hook, "tester", "OTHER", "已交付。")
    assert r["code"] == 0 and r["stdout"] == "" and r["stderr"] == "", r
    assert len(L.load(started["ledger"])["events"]) == n


# ---------------------------------------------------------------- B7


def test_b7_brief_and_agent_files_mention_handback(started):
    from builder_loop import brief as B

    lg = L.load(started["ledger"])
    root = started["repo"].root
    t = B.render(B.build(lg, root, "tester"))
    r = B.render(B.build(lg, root, "reviewer"))
    assert "SubagentHandback" in t and B.TESTER_RESULT_FORMAT in t
    assert "SubagentHandback" in r and B.REVIEWER_RESULT_FORMAT in r
    assert str(started["tester_worktree"]) in t and "Behaviors:" in t and "B1" in t
    assert str(started["worktree"]) in r and "Behaviors:" in r and "B1" in r
    for name in ("tester.md", "reviewer.md"):
        assert "SubagentHandback" in (REPO_ROOT / "agents" / name).read_text(encoding="utf-8"), name


# ---------------------------------------------------------------- B8

EXPECTED_DIST = {"SessionStart": 1, "Stop": 1, "SubagentStart": 1, "SubagentStop": 1, "PreToolUse": 3, "PostToolUse": 2, "UserPromptSubmit": 1}


def _install(clone: Path, home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, CLAUDE_HOME=str(home))
    return subprocess.run(["bash", str(clone / "install.sh")], cwd=str(clone), env=env, capture_output=True, text=True, timeout=90)


def _bl_entries(home: Path) -> list[tuple[str, str | None]]:
    data = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    return [(ev, m.get("matcher")) for ev, ms in data.get("hooks", {}).items() for m in ms
            for h in m.get("hooks", []) if "bl-hook.sh" in h.get("command", "")]


def test_b8_install_registers_handback_hook(tmp_path):
    clone = tmp_path / "clone"
    shutil.copytree(REPO_ROOT, clone, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"), symlinks=True)
    home = tmp_path / "claude_home"
    for _ in range(2):
        r = _install(clone, home)
        assert r.returncode == 0, r.stderr
        entries = _bl_entries(home)
        assert len(entries) == 10, entries
        assert dict(Counter(ev for ev, _ in entries)) == EXPECTED_DIST
        assert ("PostToolUse", "SubagentHandback") in entries
    env = dict(os.environ, CLAUDE_HOME=str(home), BUILDER_LOOP_HOME=str(tmp_path / "blhome"))
    out = subprocess.run([str(clone / "bin" / "bl"), "doctor"], env=env, capture_output=True, text=True, timeout=60).stdout
    assert len(json.loads(out)["hooks"]["registered"]) == 10


def _git_only_dir(tmp_path: Path) -> Path:
    d = tmp_path / "gitbin"
    d.mkdir()
    os.symlink(shutil.which("git"), d / "git")
    return d


def _doctor_with(tmp_path: Path, monkeypatch, version: str | None) -> dict:
    from builder_loop.doctor import doctor

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude_home"))
    monkeypatch.setenv("BUILDER_LOOP_HOME", str(tmp_path / "blhome"))
    base = _git_only_dir(tmp_path)
    path = str(base)
    if version is not None:
        fake = tmp_path / "fakebin"
        fake.mkdir()
        exe = fake / "claude"
        exe.write_text(f"#!/bin/sh\necho '{version} (Claude Code)'\n")
        exe.chmod(0o755)
        path = f"{fake}{os.pathsep}{path}"
    monkeypatch.setenv("PATH", path)
    return doctor(None)


def _problems_for(tmp_path_factory, monkeypatch, version: str | None) -> tuple[dict, list[str]]:
    rep = _doctor_with(tmp_path_factory.mktemp("doc"), monkeypatch, version)
    return rep, rep["problems"]


@pytest.mark.parametrize("version,expect_problem", [
    ("2.1.272", True), ("2.1.273", False), ("2.2.0", False),
    ("2.1.300", False), ("2.1.30", True),  # 按数字逐段比较，不是字符串比较
])
def test_b8_doctor_reports_claude_version(tmp_path_factory, monkeypatch, version, expect_problem):
    rep, problems = _problems_for(tmp_path_factory, monkeypatch, version)
    assert rep.get("claude_version") == version
    _, ok_problems = _problems_for(tmp_path_factory, monkeypatch, "2.1.273")
    extra = [p for p in problems if p not in ok_problems]
    if expect_problem:
        assert len(extra) == 1 and "2.1.273" in extra[0], problems
    else:
        assert extra == [] and not any("2.1.273" in p for p in problems), problems


def test_b8_doctor_without_claude(tmp_path_factory, monkeypatch):
    rep, problems = _problems_for(tmp_path_factory, monkeypatch, None)
    assert rep.get("claude_version") is None
    _, ok_problems = _problems_for(tmp_path_factory, monkeypatch, "2.1.273")
    assert problems == ok_problems
    assert not any("2.1.273" in p for p in problems), problems
    assert "hooks 未注册（运行 install.sh）" in problems  # 既有检查项照旧
