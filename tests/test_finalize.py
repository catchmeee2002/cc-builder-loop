from pathlib import Path

from builder_loop import ledger as L
from conftest import drive_to_proof_pass, git, implement_mul, marker, reviewer_pass


def test_finalize_requires_all_evidence(started, cli):
    assert cli("finalize", "--session", "S1", expect=1)["code"] == "EVIDENCE_NOT_READY"


def test_full_loop_finalize_cas(started, cli, hook):
    repo = started["repo"]
    drive_to_proof_pass(started, cli, hook)
    # reviewer blocked → fail → resume → pass
    hook("SubagentStart", {"session_id": "S1", "agent_id": "R1", "agent_type": "reviewer"})
    r = hook("SubagentStop", {"session_id": "S1", "agent_id": "R1", "agent_type": "reviewer", "last_assistant_message": marker({"role": "reviewer", "verdict": "changes_requested", "findings": [{"severity": "major", "file": "src/foo.py", "line": 5, "summary": "x"}]})})
    assert r["code"] == 0
    st = cli("status", "--session", "S1")["readiness"]
    assert st["states"]["reviewer"] == "fail" and st["next_action"] == "resume_reviewer"
    reviewer_pass(hook)
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "finalize"

    before = git(repo.root, "rev-parse", "HEAD")
    cand_tree = git(started["worktree"], "rev-parse", "HEAD^{tree}")
    out = cli("finalize", "--session", "S1", "-m", "feat(foo): [cr_id_skip] Add mul")
    assert out["terminal"] == "finalized"
    head = git(repo.root, "rev-parse", "HEAD")
    assert head == out["final_head"] and git(repo.root, "rev-parse", "HEAD^{tree}") == cand_tree
    assert git(repo.root, "rev-parse", "HEAD^") == before  # 单亲提交
    assert git(repo.root, "status", "--porcelain") == ""  # 主仓工作区已同步
    assert "def mul" in (repo.root / "src" / "foo.py").read_text()
    assert not started["worktree"].exists()
    assert git(repo.root, "branch", "--list", "builder-loop/*") == ""
    assert L.lookup_session("S1") is None
    assert L.load(started["ledger"])["terminal"]["status"] == "finalized"


def test_finalize_runs_commit_hook_and_detects_tree_rewrite(started, cli, hook):
    repo = started["repo"]
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook)
    hooks_dir = repo.root / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    pre = hooks_dir / "pre-commit"
    pre.write_text("#!/bin/sh\necho tampered >> src/foo.py\ngit add src/foo.py\n")
    pre.chmod(0o755)
    err = cli("finalize", "--session", "S1", "--run-commit-hook", "-m", "feat(x): [cr_id_skip] X", expect=1)
    assert err["code"] == "FINAL_COMMIT_TREE_MISMATCH"
    pre.unlink()
    out = cli("finalize", "--session", "S1", "--run-commit-hook", "-m", "feat(x): [cr_id_skip] X")
    assert out["terminal"] == "finalized"


def test_target_drift_then_rebase(started, cli, hook):
    repo = started["repo"]
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook)
    # 目标分支前进（不冲突的文件）
    (repo.root / "README.md").write_text("hi\n")
    repo.commit_all("docs(x): [cr_id_skip] Readme")
    err = cli("finalize", "--session", "S1", "-m", "feat(x): [cr_id_skip] X", expect=1)
    assert err["code"] == "TARGET_DRIFT"
    rb = cli("rebase", "--session", "S1")
    assert rb["rebased"] and rb["conflicts"] == []
    st = cli("status", "--session", "S1")["readiness"]
    assert st["states"]["machine"] == "stale" and st["next_action"] == "machine"
    cli("machine", "--session", "S1")
    cli("proof", "--session", "S1")
    reviewer_pass(hook)
    out = cli("finalize", "--session", "S1", "-m", "feat(x): [cr_id_skip] X")
    assert out["terminal"] == "finalized" and (repo.root / "README.md").exists()


def test_rebase_conflict_reported(started, cli, hook):
    repo = started["repo"]
    implement_mul(started["worktree"])
    cli("checkpoint", "--session", "S1", "--role", "builder")
    (repo.root / "src" / "foo.py").write_text("def add(a, b):\n    return b + a\n")
    repo.commit_all("fix(x): [cr_id_skip] Conflict")
    rb = cli("rebase", "--session", "S1", expect=1)
    assert rb["conflicts"] == ["src/foo.py"]


def test_dirty_overlap_blocks_finalize(started, cli, hook):
    repo = started["repo"]
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook)
    (repo.root / "src" / "foo.py").write_text("# local edit\n")
    err = cli("finalize", "--session", "S1", "-m", "feat(x): [cr_id_skip] X", expect=1)
    assert err["code"] == "DIRTY_OVERLAP" and err["details"]["paths"] == ["src/foo.py"]
    (repo.root / "other.txt").write_text("unrelated\n")
    git(repo.root, "checkout", "--", "src/foo.py")
    out = cli("finalize", "--session", "S1", "-m", "feat(x): [cr_id_skip] X")
    assert out["terminal"] == "finalized" and (repo.root / "other.txt").exists()


def test_finalize_intent_recovery(started, cli, hook):
    """CAS 成功后进程中断（未完成收尾）→ 再次 finalize 沿 intent 完成。"""
    repo = started["repo"]
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook)
    from builder_loop import finalize as F

    def boom(*a, **k):
        raise RuntimeError("crash after CAS")

    original = F._complete
    F._complete = boom
    try:
        cli("finalize", "--session", "S1", "-m", "feat(x): [cr_id_skip] X")
    except RuntimeError:
        pass
    finally:
        F._complete = original
    lg = L.load(started["ledger"])
    assert lg["finalize_intent"] and lg["terminal"] is None
    assert git(repo.root, "rev-parse", "HEAD") == lg["finalize_intent"]["final_head"]
    out = cli("finalize", "--session", "S1")
    assert out["terminal"] == "finalized" and out["final_head"] == lg["finalize_intent"]["final_head"]
