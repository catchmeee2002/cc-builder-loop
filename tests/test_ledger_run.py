import json
import threading
from pathlib import Path

from builder_loop import ledger as L
from builder_loop.jsonutil import atomic_write_json
from conftest import CONTRACT, contract_with, git, implement_mul, write_plan


def test_ledger_mutate_bumps_seq_and_serializes(tmp_path, monkeypatch):
    monkeypatch.setenv("BUILDER_LOOP_HOME", str(tmp_path / "home"))
    p = tmp_path / "ledger.json"
    L.create(p, L.new_ledger(run_id="r", contract={}, loop_config={}))

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
    L.create(L.ledger_path(repo, "r1"), L.new_ledger(run_id="r1", session={"owner_session_id": "A"}, contract={}, loop_config={}))
    L.bind_session("A", repo, "r1")
    assert L.lookup_session("A")["ledger"]["run_id"] == "r1"
    L.bind_session("B", repo, "r1")  # 指针指向别人的 run
    assert L.lookup_session("B") is None and L.lookup_session(None) is None


def test_start_creates_two_worktrees_on_frozen_baseline(repo, cli):
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")
    wt, twt = Path(out["worktree"]), Path(out["tester_worktree"])
    assert wt.is_dir() and twt.is_dir() and wt.parent == twt.parent and not str(wt).startswith(str(repo.root) + "/")
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD") == f"builder-loop/{out['run_id']}/candidate"
    assert git(twt, "rev-parse", "--abbrev-ref", "HEAD") == f"builder-loop/{out['run_id']}/tester"
    lg = L.load(Path(out["ledger"]))
    assert lg["tester"]["base"] == lg["tester"]["head"] == out["target_start_head"]
    assert lg["runtime_identity"]["version"] and "commit" in lg["runtime_identity"]
    assert lg["contract"]["authority"]["control_basenames"] and lg["contract"]["assurance"]["proof_runner"]["framework"] == "pytest"
    # tester 是第一件事：先放出去（后台），builder 再干活
    assert out["readiness"]["next_action"] == "spawn_tester"
    assert cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1", expect=3)["code"] == "RUN_ALREADY_ACTIVE"


def test_start_without_tester_has_single_worktree(repo, cli):
    write_plan(repo.root, contract_with(**{"assurance.required": ["machine", "reviewer"]}), "lite.md")
    out = cli("start", "--plan", str(repo.root / "lite.md"), "--session", "S1")
    assert out["tester_worktree"] is None and out["readiness"]["next_action"] == "checkpoint"
    assert L.load(Path(out["ledger"]))["tester"] is None


def test_checkpoint_is_per_role_worktree_with_dry_run(started, cli):
    """#227：builder 与 tester 各看各的 worktree，未提交改动互不可见，不会互相整单拒绝。"""
    wt, twt = started["worktree"], started["tester_worktree"]
    assert cli("checkpoint", "--session", "S1", "--role", "builder")["noop"] is True
    implement_mul(wt)
    (wt / "tests" / "hack.py").write_text("x")
    (wt / ".claude" / "loop.yml").write_text("pass_cmd: []\n")
    (twt / "tests" / "test_new.py").write_text("def test_x():\n    assert True\n")

    dry = cli("checkpoint", "--session", "S1", "--role", "builder", "--dry-run")
    assert dry["would_commit"] == ["src/foo.py"] and "contract revise" in dry["hint"]
    assert {r["path"]: r["reason"] for r in dry["rejected"]} == {"tests/hack.py": "tester_owned", ".claude/loop.yml": "protected"}
    assert git(wt, "status", "--porcelain") != ""  # dry-run 不提交

    err = cli("checkpoint", "--session", "S1", "--role", "builder", expect=1)
    assert err["code"] == "CHECKPOINT_REJECTED" and "dry-run" in err["message"]
    assert [e["kind"] for e in L.load(started["ledger"])["events"]] == ["checkpoint_rejected"]
    # tester 的 checkpoint 不受 builder 未提交改动影响
    ok = cli("checkpoint", "--session", "S1", "--role", "tester")
    assert ok["committed_paths"] == ["tests/test_new.py"]
    assert L.load(started["ledger"])["tester"]["head"] == ok["head"]

    (wt / "tests" / "hack.py").unlink()
    git(wt, "checkout", "--", ".claude/loop.yml")
    ok = cli("checkpoint", "--session", "S1", "--role", "builder")
    assert ok["committed_paths"] == ["src/foo.py"]
    # tester 写了 builder 的地盘 → 拒
    (twt / "src" / "foo.py").write_text("# nope\n")
    err = cli("checkpoint", "--session", "S1", "--role", "tester", expect=1)
    assert err["details"]["rejected"] == [{"path": "src/foo.py", "reason": "builder_owned"}]


def test_internal_commits_skip_repo_hooks(started, cli):
    """#232：目标仓库的 commit hooks 不应作用于 runtime 的内部提交。"""
    hooks_dir = started["repo"].root / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    for name in ("pre-commit", "commit-msg", "post-commit"):
        h = hooks_dir / name
        h.write_text("#!/bin/sh\necho rejected-by-repo-policy >&2\nexit 1\n")
        h.chmod(0o755)
    implement_mul(started["worktree"])
    ok = cli("checkpoint", "--session", "S1", "--role", "builder")
    assert ok["committed_paths"] == ["src/foo.py"]


def test_bypassing_checkpoint_is_detected(started, cli):
    wt = started["worktree"]
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    (wt / "src" / "foo.py").write_text("x = 1\n")
    git(wt, "commit", "-q", "-am", "bypass")
    assert cli("machine", "--session", "S1", expect=2)["code"] == "WORKTREE_HEAD_MISMATCH"


def test_abandon_keeps_binding_until_retro(started, cli, repo):
    ab = cli("abandon", "--session", "S1", "--reason", "test")
    assert ab["terminal"]["status"] == "abandoned" and ab["next"] == "retro" and Path(ab["worktree_kept"]).is_dir()
    assert L.lookup_session("S1") is not None
    assert cli("abandon", "--session", "S1", "--reason", "again", expect=2)["code"] == "RUN_TERMINAL"
    assert cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1", expect=3)["code"] == "RETRO_PENDING"


def test_legacy_ledger_can_be_listed_and_abandoned(repo, cli):
    lp = L.ledger_path(repo.root, "old-run")
    atomic_write_json(lp, {"schema": "builder-loop/ledger@1", "run_id": "old-run", "seq": 3, "session": {"owner_session_id": "OLD"}, "candidate": {"worktree": "/x", "branch": "b"}, "terminal": None})
    L.bind_session("OLD", repo.root, "old-run")
    assert L.lookup_session("OLD") is None  # hook 对旧版 ledger 静默
    row = cli("runs")["runs"][0]
    assert row["legacy"] is True and row["terminal"] is None
    assert cli("status", "--run", "old-run", expect=2)["code"] == "LEDGER_LEGACY"
    ab = cli("abandon", "--run", "old-run", "--reason", "upgrade")
    assert ab["terminal"]["status"] == "abandoned" and ab["next"] is None
    assert not (repo.home / "sessions" / "OLD.json").exists()


def test_contract_revise_requires_authorization(started, cli):
    repo = started["repo"]
    c = json.loads(json.dumps(CONTRACT))
    c["authority"]["builder_write"].append("lib/**")
    write_plan(repo.root, c, "plan2.md")
    # #230：--session 写在子命令后面也要认
    err = cli("contract", "revise", "--plan", str(repo.root / "plan2.md"), "--session", "S1", expect=3)
    assert err["details"]["changes"] == ["AUTHORITY_EXPAND"]
    ok = cli("contract", "--session", "S1", "revise", "--plan", str(repo.root / "plan2.md"), "--authorize")
    assert ok["applied"] and ok["changes"] == ["AUTHORITY_EXPAND"]
    c["mission"]["objective"] = "other"
    write_plan(repo.root, c, "plan3.md")
    err = cli("contract", "revise", "--session", "S1", "--plan", str(repo.root / "plan3.md"), "--authorize", expect=2)
    assert err["code"] == "MISSION_REVISION_INVALID"
    c["mission"]["revision"] = 2
    write_plan(repo.root, c, "plan3.md")
    assert "MISSION_REVISION" in cli("contract", "revise", "--session", "S1", "--plan", str(repo.root / "plan3.md"), "--authorize")["changes"]
    (repo.root / ".claude" / "loop.yml").write_text("pass_cmd:\n  - stage: test\n    cmd: \"true\"\n    timeout: 5\n")
    err = cli("contract", "revise", "--session", "S1", "--plan", str(repo.root / "plan3.md"), expect=3)
    assert "ASSURANCE_DOWNGRADE" in err["details"]["changes"]
    c["assurance"]["required"] = ["machine", "reviewer"]
    write_plan(repo.root, c, "plan4.md")
    assert cli("contract", "revise", "--session", "S1", "--plan", str(repo.root / "plan4.md"), "--authorize", expect=2)["code"] == "TESTER_REQUIREMENT_FIXED"


def test_locate_without_session_uses_single_active_run(started, cli):
    assert cli("status")["run_id"] == started["run_id"]
    assert cli("runs")["runs"][0]["run_id"] == started["run_id"]
