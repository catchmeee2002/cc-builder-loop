import json
from pathlib import Path

from builder_loop import ledger as L
from conftest import drive_to_proof_pass, git, implement_mul, mutation_patch, reviewer_pass, role_turn, make_tester_result, send_message, write_mul_test


def _finalize(cli, *extra):
    return cli("finalize", "--session", "S1", "-m", "feat(foo): [cr_id_skip] Add mul", *extra)


def test_finalize_requires_all_evidence(started, cli):
    assert cli("finalize", "--session", "S1", expect=1)["code"] == "EVIDENCE_NOT_READY"


def test_full_loop_finalize_cas_then_retro_gate(started, cli, hook, tmp_path):
    repo = started["repo"]
    drive_to_proof_pass(started, cli, hook)
    # reviewer：实现问题 → builder 修 → 全部重验 → 续接同一 reviewer
    r = role_turn(hook, "reviewer", "R1", {"role": "reviewer", "verdict": "changes_requested", "findings": [{"severity": "major", "owner": "builder", "file": "src/foo.py", "line": 5, "summary": "x"}]})
    assert r["code"] == 0
    st = cli("status", "--session", "S1")["readiness"]
    assert st["states"]["reviewer"] == "fail" and st["next_action"] == "resume_reviewer"
    reviewer_pass(hook)
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "finalize"

    before = git(repo.root, "rev-parse", "HEAD")
    cand_tree = git(started["worktree"], "rev-parse", "HEAD^{tree}")
    out = _finalize(cli)
    assert out["terminal"] == "finalized" and out["next"] == "retro"
    assert git(repo.root, "rev-parse", "HEAD") == out["final_head"] and git(repo.root, "rev-parse", "HEAD^{tree}") == cand_tree
    assert git(repo.root, "rev-parse", "HEAD^") == before  # 单亲提交，候选里的 checkpoint / integrate 提交不进目标分支
    assert git(repo.root, "status", "--porcelain") == "" and "def mul" in (repo.root / "src" / "foo.py").read_text()
    assert not started["worktree"].exists() and not started["tester_worktree"].exists() and not started["worktree"].parent.exists()
    assert git(repo.root, "branch", "--list", "builder-loop/*") == ""

    # 复盘硬闸门：session 仍绑定，Stop 拦住，也不能开下一个 run
    assert L.lookup_session("S1") is not None
    r = hook("Stop", {"session_id": "S1", "stop_hook_active": False})
    assert r["code"] == 2 and "尚未复盘" in r["stderr"] and "bl retro signals" in r["stderr"]
    assert cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1", expect=3)["code"] == "RETRO_PENDING"
    sig = cli("retro", "signals", "--session", "S1")
    ids = [s["id"] for s in sig["signals"]]
    assert ids == ["S-tester-rounds", "S-reviewer-rounds", "S-review-rejected"] and sig["runtime_identity"]["version"]
    assert cli("retro", "record", "--session", "S1", "--no-incident", expect=1)["code"] == "RETRO_SIGNALS_PRESENT"
    f = tmp_path / "retro.json"
    f.write_text(json.dumps({"dispositions": [{"signal_id": ids[0], "route": "not_incident", "reason": "两段式补 patch 的正常一轮"}]}))
    assert cli("retro", "record", "--session", "S1", "--file", str(f), expect=1)["code"] == "RETRO_COVERAGE"
    f.write_text(json.dumps({
        "dispositions": [
            {"signal_id": ids[0], "route": "not_incident", "reason": "两段式补 patch 的正常一轮"},
            {"signal_id": ids[1], "route": "not_incident", "reason": "一次正常返工"},
            {"signal_id": ids[2], "route": "builder_loop_issue", "issue_url": "https://example.test/issues/1"},
        ],
        "observations": [{"summary": "文档没说 proof_runner 要写 -p no:html", "route": "business_issue", "declined_by_user": True}],
    }))
    # 复盘阶段问用户要不要立项：等待期间 Stop 放行
    hook("PreToolUse", {"session_id": "S1", "tool_name": "AskUserQuestion", "tool_use_id": "q"})
    assert hook("Stop", {"session_id": "S1", "stop_hook_active": False})["code"] == 0
    hook("PostToolUse", {"session_id": "S1", "tool_name": "AskUserQuestion"})
    rec = cli("retro", "record", "--session", "S1", "--file", str(f))
    assert rec["recorded"] and rec["issues"] == ["https://example.test/issues/1"]
    assert L.lookup_session("S1") is None and L.load(started["ledger"])["retrospective"]["observations"]
    assert hook("Stop", {"session_id": "S1", "stop_hook_active": False})["code"] == 0
    assert cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")["run_id"]


def test_reviewer_findings_route_by_owner_and_candidate_move(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    # 缺 owner 的 major finding 不合规
    r = role_turn(hook, "reviewer", "R1", {"role": "reviewer", "verdict": "changes_requested", "findings": [{"severity": "major", "summary": "x"}]})
    assert r["code"] == 2 and "owner" in r["stderr"]
    # 测试问题 → 回到 tester，而不是让 builder 干瞪眼
    role_turn(hook, "reviewer", "R1", {"role": "reviewer", "verdict": "changes_requested", "findings": [{"severity": "major", "owner": "tester", "file": "tests/test_mul.py", "line": 4, "summary": "缺 0 边界"}]}, start=False)
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "resume_tester"
    twt = started["tester_worktree"]
    (twt / "tests" / "test_mul.py").write_text((twt / "tests" / "test_mul.py").read_text() + "\n\ndef test_mul_zero():\n    assert mul(0, 5) == 0\n")
    role_turn(hook, "tester", "T1", make_tester_result("mutation", mutation_patch(started["worktree"]), test_ids=["tests/test_mul.py::test_mul", "tests/test_mul.py::test_mul_zero"]))
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "integrate"
    cli("integrate", "--session", "S1")
    cli("machine", "--session", "S1")
    cli("proof", "--session", "S1")
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "resume_reviewer"
    # 审查期间候选变了 → 本次结论无效（真续接：先 SendMessage 再 SubagentStart）
    send_message(hook, "R1")
    hook("SubagentStart", {"session_id": "S1", "agent_id": "R1", "agent_type": "reviewer"})
    wt = started["worktree"]
    (wt / "src" / "foo.py").write_text((wt / "src" / "foo.py").read_text() + "\n# late edit\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    role_turn(hook, "reviewer", "R1", {"role": "reviewer", "verdict": "pass", "findings": []}, start=False)
    rec = L.load(started["ledger"])["evidence"]["reviewer"]
    assert rec["status"] == "fail" and "审查期间" in rec["details"]["reason"]
    # builder 改过实现后，旧 mutation patch 的上下文对不上了 → proof 判无效并指回 tester 刷新 patch
    cli("machine", "--session", "S1")
    stale = cli("proof", "--session", "S1", expect=1)
    assert stale["failure"]["code"] == "TEST_MUTATION_INVALID" and stale["readiness"]["next_action"] == "resume_tester"
    ids = ["tests/test_mul.py::test_mul", "tests/test_mul.py::test_mul_zero"]
    role_turn(hook, "tester", "T1", make_tester_result("mutation", mutation_patch(wt), test_ids=ids))
    assert cli("proof", "--session", "S1")["result"] == "PASS"
    # 需要改契约的 finding → blocker，交还用户
    role_turn(hook, "reviewer", "R1", {"role": "reviewer", "verdict": "blocked", "findings": [{"severity": "blocking", "owner": "contract", "summary": "验收标准需要调整"}]})
    st = cli("status", "--session", "S1")["readiness"]
    assert st["next_action"] == "needs_user" and st["blockers"][0]["code"] == "REVIEW_CONTRACT"


def test_finalize_runs_commit_hook_and_detects_tree_rewrite(started, cli, hook):
    repo = started["repo"]
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook)
    hooks_dir = repo.root / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    pre = hooks_dir / "pre-commit"
    pre.write_text("#!/bin/sh\necho tampered >> src/foo.py\ngit add src/foo.py\n")
    pre.chmod(0o755)
    assert cli("finalize", "--session", "S1", "--run-commit-hook", "-m", "feat(x): [cr_id_skip] X", expect=1)["code"] == "FINAL_COMMIT_TREE_MISMATCH"
    pre.unlink()
    assert cli("finalize", "--session", "S1", "--run-commit-hook", "-m", "feat(x): [cr_id_skip] X")["terminal"] == "finalized"


def test_target_drift_rebase_then_tester_change_still_integrates(started, cli, hook):
    """rebase 之后 tester 再改已集成的文件：路径叠加不会像 merge 那样 add/add 冲突。"""
    repo = started["repo"]
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook)
    (repo.root / "README.md").write_text("hi\n")
    repo.commit_all("docs(x): [cr_id_skip] Readme")
    assert _finalize_err(cli) == "TARGET_DRIFT"
    rb = cli("rebase", "--session", "S1")
    assert rb["rebased"] and rb["conflicts"] == []
    lg = L.load(started["ledger"])
    assert lg["tester"]["base"] == started["target_start_head"] != lg["repo"]["target_start_head"]  # tester 基线不随 rebase 动
    assert lg["evidence"]["tester"]["details"]["files"]["present"] == ["tests/test_mul.py"]  # 漂移的 README 不会被算成 tester 文件
    st = cli("status", "--session", "S1")["readiness"]
    assert st["states"]["tester"] == "pass" and st["states"]["machine"] == "stale" and st["next_action"] == "machine"

    twt = started["tester_worktree"]
    (twt / "tests" / "test_mul.py").write_text((twt / "tests" / "test_mul.py").read_text() + "\n\ndef test_mul_neg():\n    assert mul(-2, 3) == -6\n")
    role_turn(hook, "tester", "T1", make_tester_result("mutation", mutation_patch(started["worktree"])))
    integ = cli("integrate", "--session", "S1")
    assert integ["integrated_paths"] == ["tests/test_mul.py"]
    cli("machine", "--session", "S1")
    cli("proof", "--session", "S1")
    reviewer_pass(hook)
    out = _finalize(cli)
    assert out["terminal"] == "finalized" and (repo.root / "README.md").exists() and "test_mul_neg" in (repo.root / "tests" / "test_mul.py").read_text()


def _finalize_err(cli) -> str:
    return cli("finalize", "--session", "S1", "-m", "feat(x): [cr_id_skip] X", expect=1)["code"]


def test_rebase_conflict_reported(started, cli, hook):
    repo = started["repo"]
    implement_mul(started["worktree"])
    cli("checkpoint", "--session", "S1", "--role", "builder")
    (repo.root / "src" / "foo.py").write_text("def add(a, b):\n    return b + a\n")
    repo.commit_all("fix(x): [cr_id_skip] Conflict")
    assert cli("rebase", "--session", "S1", expect=1)["conflicts"] == ["src/foo.py"]


def test_dirty_overlap_blocks_finalize(started, cli, hook):
    repo = started["repo"]
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook)
    (repo.root / "src" / "foo.py").write_text("# local edit\n")
    err = cli("finalize", "--session", "S1", "-m", "feat(x): [cr_id_skip] X", expect=1)
    assert err["code"] == "DIRTY_OVERLAP" and err["details"]["paths"] == ["src/foo.py"]
    (repo.root / "other.txt").write_text("unrelated\n")
    git(repo.root, "checkout", "--", "src/foo.py")
    assert _finalize(cli)["terminal"] == "finalized" and (repo.root / "other.txt").exists()


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
        _finalize(cli)
    except RuntimeError:
        pass
    finally:
        F._complete = original
    lg = L.load(started["ledger"])
    assert lg["finalize_intent"] and lg["terminal"] is None
    assert git(repo.root, "rev-parse", "HEAD") == lg["finalize_intent"]["final_head"]
    out = cli("finalize", "--session", "S1")
    assert out["terminal"] == "finalized" and out["final_head"] == lg["finalize_intent"]["final_head"]


def test_abandon_retro_then_cleanup(started, cli, hook):
    wt, twt = started["worktree"], started["tester_worktree"]
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    cli("checkpoint", "--session", "S1", "--role", "tester")
    (twt / "scratch.txt").write_text("leftover")  # tester worktree 留了未提交文件
    cli("abandon", "--session", "S1", "--reason", "方向错了")
    assert cli("cleanup", "--run", started["run_id"])["cleaned"] == [{"run_id": started["run_id"], "skipped": "尚未复盘"}]
    assert hook("Stop", {"session_id": "S1", "stop_hook_active": False})["code"] == 2
    sig = cli("retro", "signals", "--session", "S1")
    assert [s["id"] for s in sig["signals"]] == ["S-abandoned"]
    import json, tempfile
    f = Path(tempfile.mkdtemp()) / "r.json"
    f.write_text(json.dumps({"dispositions": [{"signal_id": "S-abandoned", "route": "not_incident", "reason": "需求取消"}]}))
    cli("retro", "record", "--run", started["run_id"], "--file", str(f))
    out = cli("cleanup")["cleaned"][0]
    assert out["removed"] == [str(wt)] and out["kept"] == [{"worktree": str(twt), "reason": "有未提交改动"}]
    assert not wt.exists() and twt.exists()
