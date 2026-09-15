import json
import threading
from pathlib import Path

import pytest

from builder_loop import ledger as L
from conftest import CONTRACT, git, implement_mul, write_plan


def test_ledger_mutate_bumps_seq_and_serializes(tmp_path, monkeypatch):
    monkeypatch.setenv("BUILDER_LOOP_HOME", str(tmp_path / "home"))
    p = tmp_path / "ledger.json"
    lg = L.new_ledger(run_id="r", contract={}, loop_config={})
    L.create(p, lg)

    def bump():
        for _ in range(20):
            with L.mutate(p) as x:
                x["counters"]["machine_iter"] += 1

    threads = [threading.Thread(target=bump) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    final = L.load(p)
    assert final["seq"] == 80 and final["counters"]["machine_iter"] == 80


def test_session_pointer_must_match_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("BUILDER_LOOP_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    lg = L.new_ledger(run_id="r1", session={"owner_session_id": "A"}, contract={}, loop_config={})
    L.create(L.ledger_path(repo, "r1"), lg)
    L.bind_session("A", repo, "r1")
    assert L.lookup_session("A")["ledger"]["run_id"] == "r1"
    L.bind_session("B", repo, "r1")  # 指针指向别人的 run
    assert L.lookup_session("B") is None
    assert L.lookup_session(None) is None


def test_start_checkpoint_abandon(repo, cli):
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")
    wt = Path(out["worktree"])
    assert wt.is_dir() and not str(wt).startswith(str(repo.root))
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "builder-loop/" + out["run_id"]
    assert out["readiness"]["next_action"] == "checkpoint"
    # 同 session 二次 start → exit 3
    err = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1", expect=3)
    assert err["code"] == "RUN_ALREADY_ACTIVE"
    # noop checkpoint
    assert cli("checkpoint", "--session", "S1", "--role", "builder")["noop"] is True
    # 越界：protected + tester_owned
    implement_mul(wt)
    (wt / "tests" / "hack.py").write_text("x")
    (wt / ".claude" / "loop.yml").write_text("pass_cmd: []\n")
    err = cli("checkpoint", "--session", "S1", "--role", "builder", expect=1)
    reasons = {r["path"]: r["reason"] for r in err["details"]["rejected"]}
    assert reasons == {"tests/hack.py": "tester_owned", ".claude/loop.yml": "protected"}
    assert git(wt, "status", "--porcelain") != ""  # 未提交任何东西
    (wt / "tests" / "hack.py").unlink()
    git(wt, "checkout", "--", ".claude/loop.yml")
    ok = cli("checkpoint", "--session", "S1", "--role", "builder")
    assert ok["committed_paths"] == ["src/foo.py"] and ok["readiness"]["next_action"] == "machine"
    # 手工 commit 绕过 checkpoint → 身份不一致
    (wt / "src" / "foo.py").write_text("x = 1\n")
    git(wt, "commit", "-q", "-am", "chore(x): [cr_id_skip] Bypass")
    err = cli("status", "--session", "S1", expect=0)  # status 只读，不校验
    err = cli("machine", "--session", "S1", expect=2)
    assert err["code"] == "CANDIDATE_HEAD_MISMATCH"
    # abandon 终态 + 解绑
    ab = cli("abandon", "--session", "S1", "--reason", "test")
    assert ab["terminal"]["status"] == "abandoned" and Path(ab["worktree_kept"]).is_dir()
    assert cli("abandon", "--session", "S1", "--reason", "again", expect=2)["code"] in ("SESSION_UNBOUND", "RUN_TERMINAL")


def test_contract_revise_requires_authorization(started, cli):
    repo = started["repo"]
    c = json.loads(json.dumps(CONTRACT))
    c["authority"]["builder_write"].append("lib/**")
    write_plan(repo.root, c, "plan2.md")
    err = cli("contract", "--session", "S1", "revise", "--plan", str(repo.root / "plan2.md"), expect=3)
    assert err["details"]["changes"] == ["AUTHORITY_EXPAND"]
    ok = cli("contract", "--session", "S1", "revise", "--plan", str(repo.root / "plan2.md"), "--authorize")
    assert ok["applied"] and ok["changes"] == ["AUTHORITY_EXPAND"]
    # mission 变化必须 revision+1
    c["mission"]["objective"] = "other"
    write_plan(repo.root, c, "plan3.md")
    err = cli("contract", "--session", "S1", "revise", "--plan", str(repo.root / "plan3.md"), "--authorize", expect=2)
    assert err["code"] == "MISSION_REVISION_INVALID"
    c["mission"]["revision"] = 2
    write_plan(repo.root, c, "plan3.md")
    ok = cli("contract", "--session", "S1", "revise", "--plan", str(repo.root / "plan3.md"), "--authorize")
    assert "MISSION_REVISION" in ok["changes"]
    # loop.yml 变化 = assurance 降级
    (repo.root / ".claude" / "loop.yml").write_text("pass_cmd:\n  - stage: test\n    cmd: \"true\"\n    timeout: 5\n")
    err = cli("contract", "--session", "S1", "revise", "--plan", str(repo.root / "plan3.md"), expect=3)
    assert "ASSURANCE_DOWNGRADE" in err["details"]["changes"]


def test_locate_without_session_uses_single_active_run(started, cli):
    assert cli("status")["run_id"] == started["run_id"]
    assert cli("runs")["runs"][0]["run_id"] == started["run_id"]
