from pathlib import Path

from builder_loop import evidence, ledger as L
from conftest import git, implement_mul


def test_machine_requires_checkpoint_and_clean(started, cli):
    wt = started["worktree"]
    assert cli("machine", "--session", "S1", expect=2)["code"] == "CANDIDATE_NOT_CHECKPOINTED"
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    (wt / "src" / "scratch.py").write_text("")
    assert cli("machine", "--session", "S1", expect=2)["code"] == "CANDIDATE_DIRTY"
    (wt / "src" / "scratch.py").unlink()
    out = cli("machine", "--session", "S1")
    assert out["result"] == "PASS" and out["readiness"]["states"]["machine"] == "pass"
    assert out["readiness"]["next_action"] == "spawn_tester"
    assert Path(out["stages"][0]["log"]).is_file()


def test_machine_fail_signature_repeats_and_limits(started, cli):
    wt = started["worktree"]
    (wt / "src" / "foo.py").write_text("def add(a, b):\n    return a - b\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    first = cli("machine", "--session", "S1", expect=1)
    assert first["result"] == "FAIL" and first["repeat_count"] == 1 and first["remaining_iterations"] == 2
    second = cli("machine", "--session", "S1", expect=1)
    assert second["failure"]["signature"] == first["failure"]["signature"] and second["repeat_count"] == 2
    third = cli("machine", "--session", "S1", expect=3)
    codes = {b["code"] for b in third["readiness"]["blockers"]}
    assert {"MAX_ITERATIONS", "NO_PROGRESS"} <= codes
    assert third["readiness"]["next_action"] == "needs_user"
    # Stop hook 在 blocker 下仍阻断但提示 needs_user
    from builder_loop.hooks import handle_hook
    import json
    (_, stderr), code = handle_hook("Stop", json.dumps({"session_id": "S1", "stop_hook_active": False}))
    assert code == 2 and "needs_user" in stderr


def test_machine_worktree_mutation_detected(started, cli):
    repo = started["repo"]
    wt = started["worktree"]
    (repo.root / ".claude" / "loop.yml").write_text("pass_cmd:\n  - stage: mut\n    cmd: \"echo x >> src/foo.py\"\n    timeout: 5\n")
    # loop.yml 在 start 时已冻结进 ledger；重新 start 一个新 run 来使用新命令
    cli("abandon", "--session", "S1", "--reason", "x")
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S2")
    wt2 = Path(out["worktree"])
    implement_mul(wt2)
    cli("checkpoint", "--session", "S2", "--role", "builder")
    res = cli("machine", "--session", "S2", expect=1)
    assert res["failure"]["worktree_mutated"]["paths"] == ["src/foo.py"]


def test_evidence_stale_on_candidate_change(started, cli):
    wt = started["worktree"]
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    cli("machine", "--session", "S1")
    lg = L.load(started["ledger"])
    assert evidence.state(lg, "machine", started["repo"].root) == "pass"
    (wt / "src" / "foo.py").write_text((wt / "src" / "foo.py").read_text() + "\n# touch\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    lg = L.load(started["ledger"])
    assert evidence.state(lg, "machine", started["repo"].root) == "stale"
    assert evidence.readiness(lg, started["repo"].root)["next_action"] == "machine"


def test_stop_hook_stall_and_waiting(started, cli, hook):
    # 首次 Stop：阻断
    r = hook("Stop", {"session_id": "S1", "stop_hook_active": False})
    assert r["code"] == 2 and "next_action=checkpoint" in r["stderr"]
    # 连续三次无进展（stop_hook_active=True 且 seq 没变）
    for _ in range(2):
        assert hook("Stop", {"session_id": "S1", "stop_hook_active": True})["code"] == 2
    r = hook("Stop", {"session_id": "S1", "stop_hook_active": True})
    assert r["code"] == 0 and "没有任何 runtime 进展" in r["stderr"]
    # 有进展后恢复阻断
    implement_mul(started["worktree"])
    cli("checkpoint", "--session", "S1", "--role", "builder")
    assert hook("Stop", {"session_id": "S1", "stop_hook_active": True})["code"] == 2
    # AskUserQuestion 挂起 → 放行；回答后恢复
    hook("PreToolUse", {"session_id": "S1", "tool_name": "AskUserQuestion", "tool_use_id": "t1"})
    assert hook("Stop", {"session_id": "S1", "stop_hook_active": False})["code"] == 0
    hook("UserPromptSubmit", {"session_id": "S1"})
    assert hook("Stop", {"session_id": "S1", "stop_hook_active": False})["code"] == 2
    # 未绑定 session 静默
    assert hook("Stop", {"session_id": "nobody"})["code"] == 0
    # EnterWorktree 被拒
    r = hook("PreToolUse", {"session_id": "S1", "tool_name": "EnterWorktree", "tool_input": {}})
    assert r["json"]["hookSpecificOutput"]["permissionDecision"] == "deny"
