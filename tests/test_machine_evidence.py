import os
import subprocess
import sys
import time
from pathlib import Path

from builder_loop import evidence, ledger as L, machine
from conftest import contract_with, implement_mul, role_turn, make_tester_result, write_mul_test, write_plan


def _start_lite(repo, cli, session="S1"):
    """只要 machine + reviewer 的 run：测 machine 自身行为时不需要 tester。"""
    write_plan(repo.root, contract_with(**{"assurance.required": ["machine", "reviewer"]}), "lite.md")
    out = cli("start", "--plan", str(repo.root / "lite.md"), "--session", session)
    return Path(out["worktree"]), Path(out["ledger"])


def test_machine_requires_checkpoint_and_clean(repo, cli):
    wt, _ = _start_lite(repo, cli)
    assert cli("machine", "--session", "S1", expect=2)["code"] == "CANDIDATE_NOT_CHECKPOINTED"
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    (wt / "src" / "scratch.py").write_text("")
    assert cli("machine", "--session", "S1", expect=2)["code"] == "WORKTREE_DIRTY"
    (wt / "src" / "scratch.py").unlink()
    out = cli("machine", "--session", "S1")
    assert out["result"] == "PASS" and out["readiness"]["next_action"] == "spawn_reviewer"
    assert Path(out["stages"][0]["log"]).is_file()


def test_iteration_limit_is_a_real_stop_and_needs_user_authorization(repo, cli, hook):
    """#231：上限触发后 machine 不再执行；续跑需要一次被记录的、来自用户的决定。"""
    wt, ledger = _start_lite(repo, cli)
    (wt / "src" / "foo.py").write_text("def add(a, b):\n    return a - b\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    first = cli("machine", "--session", "S1", expect=1)
    assert first["repeat_count"] == 1 and first["remaining_iterations"] == 2
    second = cli("machine", "--session", "S1", expect=1)
    assert second["failure"]["signature"] == first["failure"]["signature"]
    third = cli("machine", "--session", "S1", expect=3)
    assert {"MAX_ITERATIONS", "NO_PROGRESS"} <= {b["code"] for b in third["readiness"]["blockers"]}
    # 修好了也不能直接再跑：上限是真的停止点
    (wt / "src" / "foo.py").write_text("def add(a, b):\n    return a + b\n\n\nX = 1\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    assert cli("machine", "--session", "S1", expect=3)["code"] == "MACHINE_BLOCKED"
    assert L.load(ledger)["counters"]["machine_iter"] == 3
    r = hook("Stop", {"session_id": "S1", "stop_hook_active": False})
    assert r["code"] == 2 and "needs_user" in r["stderr"] and "bl resume" in r["stderr"]
    # 模型不能自己给自己授权：blocker 之后必须有过用户输入
    assert cli("resume", "--session", "S1", "--reason", "自己决定继续", expect=3)["code"] == "USER_DECISION_REQUIRED"
    # 后台 subagent 结束的任务通知也会触发 UserPromptSubmit（CC 实测）——它不是真人，不能当授权依据
    hook("UserPromptSubmit", {"session_id": "S1", "prompt": "<task-notification>…</task-notification>"})
    assert cli("resume", "--session", "S1", "--reason", "自己决定继续", expect=3)["code"] == "USER_DECISION_REQUIRED"
    hook("PreToolUse", {"session_id": "S1", "tool_name": "AskUserQuestion", "tool_use_id": "q1"})
    hook("PostToolUse", {"session_id": "S1", "tool_name": "AskUserQuestion"})
    ok = cli("resume", "--session", "S1", "--reason", "用户：已定位根因，再给 3 次")
    assert ok["authorized"]["blockers"] == ["MAX_ITERATIONS", "NO_PROGRESS"] and ok["readiness"]["next_action"] == "machine"
    out = cli("machine", "--session", "S1")
    assert out["result"] == "PASS" and out["iter"] == 4
    assert cli("resume", "--session", "S1", "--reason", "x", expect=1)["code"] == "NOTHING_TO_RESUME"
    assert [a["reason"] for a in L.load(ledger)["authorizations"]] == ["用户：已定位根因，再给 3 次"]


def test_machine_worktree_mutation_detected(repo, cli):
    (repo.root / ".claude" / "loop.yml").write_text("pass_cmd:\n  - stage: mut\n    cmd: \"echo x >> src/foo.py\"\n    timeout: 5\n")
    repo.commit_all()
    wt, _ = _start_lite(repo, cli)
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    res = cli("machine", "--session", "S1", expect=1)
    assert res["failure"]["worktree_mutated"]["paths"] == ["src/foo.py"]


def test_evidence_stale_on_candidate_change(repo, cli):
    wt, ledger = _start_lite(repo, cli)
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    cli("machine", "--session", "S1")
    assert evidence.state(L.load(ledger), "machine", repo.root) == "pass"
    (wt / "src" / "foo.py").write_text((wt / "src" / "foo.py").read_text() + "\n# touch\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    lg = L.load(ledger)
    assert evidence.state(lg, "machine", repo.root) == "stale"
    assert evidence.readiness(lg, repo.root)["next_action"] == "machine"


def test_machine_failure_points_at_tester_files(started, cli, hook):
    """测试本身写错时 builder 改不了它：失败输出标出涉及的 tester 文件，builder 据此回到 tester。"""
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt, "assert mul(3, 4) == 13  # tester 写错了")
    role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    cli("integrate", "--session", "S1")
    res = cli("machine", "--session", "S1", expect=1)
    assert res["failure"]["tester_files_mentioned"] == ["tests/test_mul.py"]
    # builder SendMessage 续接 tester → 在跑期间 next_action 是 awaiting，不是 machine
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "awaiting_tester"
    write_mul_test(twt)
    role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "integrate"
    cli("integrate", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"


def test_stop_hook_stall_waiting_and_awaiting(started, cli, hook):
    # 首次 Stop：阻断，先把 tester 放出去
    r = hook("Stop", {"session_id": "S1", "stop_hook_active": False})
    assert r["code"] == 2 and "next_action=spawn_tester" in r["stderr"]
    for _ in range(2):
        assert hook("Stop", {"session_id": "S1", "stop_hook_active": True})["code"] == 2
    r = hook("Stop", {"session_id": "S1", "stop_hook_active": True})
    assert r["code"] == 0 and "没有任何 runtime 进展" in r["stderr"]
    assert [e["kind"] for e in L.load(started["ledger"])["events"]] == ["stall_escape"]

    # tester 在后台跑、builder 还没干完 → 拦住让 builder 继续
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    r = hook("Stop", {"session_id": "S1", "stop_hook_active": False})
    assert r["code"] == 2 and "next_action=checkpoint" in r["stderr"]
    # builder 干完了、tester 还在跑 → #228：放行，不计 stall，不产生无进展往返
    implement_mul(started["worktree"])
    cli("checkpoint", "--session", "S1", "--role", "builder")
    for _ in range(5):
        assert hook("Stop", {"session_id": "S1", "stop_hook_active": True})["code"] == 0
    assert L.load(started["ledger"])["counters"]["stall"]["count"] == 0

    # AskUserQuestion 挂起 → 放行；用户回答后恢复
    hook("PreToolUse", {"session_id": "S1", "tool_name": "AskUserQuestion", "tool_use_id": "t1"})
    assert L.load(started["ledger"])["waiting_for_user"]
    hook("UserPromptSubmit", {"session_id": "S1"})  # 用户没答题直接打字：清等待，但不记为 user_input
    lg = L.load(started["ledger"])
    assert lg["waiting_for_user"] is None and lg["events"][-1]["kind"] != "user_input"
    hook("PreToolUse", {"session_id": "S1", "tool_name": "AskUserQuestion", "tool_use_id": "t2"})
    hook("PostToolUse", {"session_id": "S1", "tool_name": "AskUserQuestion"})
    lg = L.load(started["ledger"])
    assert lg["waiting_for_user"] is None and lg["events"][-1] | {"at": 0} == {"at": 0, "kind": "user_input", "source": "AskUserQuestion"}
    assert hook("Stop", {"session_id": "nobody"})["code"] == 0
    r = hook("PreToolUse", {"session_id": "S1", "tool_name": "EnterWorktree", "tool_input": {}})
    assert r["json"]["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_role_running_is_derived_with_lease(started, cli, hook):
    """不存 running 布尔：由事件流 + 心跳租约派生。agent 失联（租约过期）后回到 resume。"""
    implement_mul(started["worktree"])
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    cli("checkpoint", "--session", "S1", "--role", "builder")
    lg = L.load(started["ledger"])
    assert "running" not in lg["agents"]["tester"] and evidence.role_running(lg, "tester")
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "awaiting_tester"
    # 该 agent 的工具调用续租：只碰心跳文件，不写 ledger
    seq = lg["seq"]
    hook("PreToolUse", {"session_id": "S1", "agent_type": "tester", "agent_id": "T1", "tool_name": "Read", "tool_input": {"file_path": str(started["tester_worktree"] / "tests" / "test_foo.py")}})
    assert L.load(started["ledger"])["seq"] == seq
    hb = started["ledger"].parent / "heartbeat-tester"
    assert hb.exists()
    # 让心跳与 role_start 都落到租约之外
    old = time.time() - evidence.ROLE_LEASE_SECONDS - 60
    os.utime(hb, (old, old))
    with L.mutate(started["ledger"]) as x:
        for e in x["events"]:
            e["at"] = "2000-01-01T00:00:00.000000+00:00"
    assert not evidence.role_running(L.load(started["ledger"]), "tester")
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "resume_tester"


def _hold_gate(ledger_path: Path, holder: str) -> subprocess.Popen:
    """另一个进程持有门禁锁（flock 是 per open-file-description，必须真的另起进程）。"""
    code = (
        "import fcntl,os,sys,time\n"
        f"fd=os.open({str(ledger_path.parent / f'gate-{holder}.lock')!r}, os.O_RDWR|os.O_CREAT, 0o644)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "os.ftruncate(fd,0); os.write(fd, str(os.getpid()).encode())\n"
        "sys.stdout.write('held\\n'); sys.stdout.flush()\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    return proc


def test_stop_waits_while_machine_gate_runs(repo, cli, hook):
    """后台跑 machine 时 Stop 不能再催一遍 `bl machine`（#242）。"""
    wt, lp = _start_lite(repo, cli)
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "machine"
    assert hook("Stop", {"session_id": "S1"})["code"] == 2  # 没在跑：照常拉回

    proc = _hold_gate(lp, "machine")
    try:
        st = cli("status", "--session", "S1")
        assert st["readiness"]["next_action"] == "awaiting_gate" and st["readiness"]["gate_running"] == "machine"
        assert hook("Stop", {"session_id": "S1"})["code"] == 0  # 在跑：放行，等后台任务唤醒
        assert cli("machine", "--session", "S1", expect=1)["code"] == "GATE_BUSY"  # 也不许重复启动
    finally:
        proc.kill(); proc.wait()
    # 进程没了锁自动释放，没有残留状态要清理
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "machine"


def test_preflight_marks_baseline_red_stage(repo, cli):
    """基线上就红的 stage 不该让 builder 白查（#241）。"""
    (repo.root / ".claude" / "loop.yml").write_text(
        "pass_cmd:\n  - stage: unit\n    cmd: python3 -c \"import sys; sys.exit(0)\"\n"
        "  - stage: legacy\n    cmd: python3 -c \"import missing_module\"\n", encoding="utf-8")
    repo.commit_all()
    wt, lp = _start_lite(repo, cli)
    assert cli("status", "--session", "S1")["preflight"]["baseline_red"] is None

    out = cli("preflight", "--session", "S1")
    assert out["result"] == "RED" and out["baseline_red"] == ["legacy"]
    assert cli("status", "--session", "S1")["preflight"]["baseline_red"] == ["legacy"]
    lg = L.load(lp)
    assert not any(lg["evidence"].values()) and lg["counters"]["machine_iter"] == 0  # 只记 event，不碰判据

    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    failure = cli("machine", "--session", "S1", expect=1)["failure"]
    assert failure["stage"] == "legacy" and failure["baseline_red"] is True and "与候选无关" in failure["baseline"]

    # pass_cmd 改了 → 那次预跑的结论作废，不再张冠李戴
    with L.mutate(lp) as x:
        x["contract"]["assurance"]["machine_commands"][1]["cmd"] = "python3 -c \"import sys; sys.exit(1)\""
    assert machine.baseline_red(L.load(lp)) is None


def test_start_rejects_unrunnable_proof_runner(repo, cli):
    """跑不起来的 proof_runner 在 start 就拦住：此时改 loop.yml 不需要 contract revise（#241）。"""
    cfg = (repo.root / ".claude" / "loop.yml")
    cfg.write_text(cfg.read_text(encoding="utf-8") + "proof_runner:\n  framework: pytest\n  cmd: definitely-not-here -m pytest\n", encoding="utf-8")
    repo.commit_all()
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S9", expect=1)
    assert out["code"] == "PROOF_RUNNER_UNAVAILABLE"
    assert L.list_runs(repo.root) == [] and not (repo.root.parent / "builder-loop-worktrees").exists()

    # 不要 proof 的 run 不受影响
    write_plan(repo.root, contract_with(**{"assurance.required": ["machine", "reviewer"]}), "lite.md")
    assert cli("start", "--plan", str(repo.root / "lite.md"), "--session", "S9")["run_id"]
