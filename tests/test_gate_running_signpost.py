"""门禁在跑时 readiness 不再按它的旧结论推动作（B1/B2），
且 SKILL.md / brief 的相关文案与新规则对齐（B3/B4）。"""

from __future__ import annotations

import re
from pathlib import Path

from builder_loop import ledger as L

from builder_loop import evidence

from conftest import (
    PROOF_RUNNER_CMD,
    PYTEST_CMD,
    ROOT,
    contract_with,
    drive_to_proof_pass,
    implement_mul,
    make_tester_result,
    mutation_patch,
    role_turn,
    write_mul_test,
    write_plan,
)
from test_machine_evidence import _hold_gate

SKILL = ROOT / "skills" / "builder" / "SKILL.md"


def _start_lite(repo, cli, session="S1"):
    write_plan(repo.root, contract_with(**{"assurance.required": ["machine", "reviewer"]}), "lite.md")
    out = cli("start", "--plan", str(repo.root / "lite.md"), "--session", session)
    return Path(out["worktree"]), Path(out["ledger"])


# ---------------------------------------------------------------- B1: machine 门禁在跑时的 resume_tester


def test_awaiting_gate_while_machine_gate_running_for_resume_tester(started, cli, hook):
    wt, twt, lp = started["worktree"], started["tester_worktree"], started["ledger"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt, "assert mul(3, 4) == 13  # tester 写错了")
    role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    cli("integrate", "--session", "S1")
    res = cli("machine", "--session", "S1", expect=1)
    assert res["failure"]["tester_files_mentioned"] == ["tests/test_mul.py"]
    # tester 没在跑、也没在这次失败之后答复过 → 此刻是 resume_tester
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "resume_tester"

    proc = _hold_gate(lp, "machine")
    try:
        st = cli("status", "--session", "S1")
        assert st["readiness"]["next_action"] == "awaiting_gate"
        assert st["readiness"]["gate_running"] == "machine"
    finally:
        proc.kill()
        proc.wait()
    # 锁释放后恢复 resume_tester
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "resume_tester"


def test_machine_fail_owner_boundary_no_tester_files_still_gates(repo, cli, hook):
    """边界：machine FAIL 且 tester_files_mentioned 为空时，machine 门禁在跑 → awaiting_gate；锁释放后为 machine。

    用一个跟 pytest / tests/** 完全无关的 guard stage 来制造这类失败：候选实现与测试都是对的，
    只是 guard stage 本身必炸，失败日志里不会出现任何 tests/** 路径。
    """
    (repo.root / ".claude" / "loop.yml").write_text(
        f"pass_cmd:\n  - stage: unit\n    cmd: \"{PYTEST_CMD}\"\n    timeout: 60\n"
        "  - stage: guard\n    cmd: \"python3 -c 'import sys; sys.exit(1)'\"\n    timeout: 5\n"
        f"max_iterations: 3\nproof_runner:\n  framework: pytest\n  cmd: \"{PROOF_RUNNER_CMD}\"\n"
    )
    repo.commit_all()
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")
    wt, twt, lp = Path(out["worktree"]), Path(out["tester_worktree"]), Path(out["ledger"])
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    cli("integrate", "--session", "S1")
    res = cli("machine", "--session", "S1", expect=1)
    assert res["failure"]["stage"] == "guard"
    assert not res["failure"].get("tester_files_mentioned")
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "machine"

    proc = _hold_gate(lp, "machine")
    try:
        st = cli("status", "--session", "S1")
        assert st["readiness"]["next_action"] == "awaiting_gate"
        assert st["readiness"]["gate_running"] == "machine"
    finally:
        proc.kill()
        proc.wait()
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "machine"


def test_integrate_still_wins_while_machine_gate_running(started, cli, hook):
    """不变量：需要 integrate 时，即使 machine 门禁在跑，next_action 仍为 integrate。"""
    wt, twt, lp = started["worktree"], started["tester_worktree"], started["ledger"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "integrate"

    proc = _hold_gate(lp, "machine")
    try:
        assert cli("status", "--session", "S1")["readiness"]["next_action"] == "integrate"
    finally:
        proc.kill()
        proc.wait()


def test_tester_running_wins_over_gate_regardless_of_holder(started, cli, hook):
    """不变量：tester 在跑（role_running）时 next_action 为 awaiting_tester，不论哪个门禁在跑。"""
    wt, lp = started["worktree"], started["ledger"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "awaiting_tester"

    proc = _hold_gate(lp, "machine")
    try:
        assert cli("status", "--session", "S1")["readiness"]["next_action"] == "awaiting_tester"
    finally:
        proc.kill()
        proc.wait()


def test_preflight_running_does_not_gate_machine(repo, cli):
    """不变量：只有 preflight 在跑（gate_running == "preflight"）且 machine 未过时，next_action 仍为 machine。"""
    wt, lp = _start_lite(repo, cli)
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "machine"

    proc = _hold_gate(lp, "preflight")
    try:
        st = cli("status", "--session", "S1")
        assert st["readiness"]["gate_running"] == "preflight"
        assert st["readiness"]["next_action"] == "machine"
    finally:
        proc.kill()
        proc.wait()


# ---------------------------------------------------------------- B2: proof 门禁在跑时的 resume_tester


def test_awaiting_gate_while_proof_gate_running_for_resume_tester(started, cli, hook):
    wt, twt, lp = started["worktree"], started["tester_worktree"], started["ledger"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt, "assert mul(2, 2) == 4  # 弱：a+b 也等于 4")
    role_turn(hook, "tester", "T1", make_tester_result("baseline-red"), start=False)
    cli("integrate", "--session", "S1")
    cli("machine", "--session", "S1")
    res = cli("proof", "--session", "S1", expect=1)
    assert res["failure"]["code"] == "TEST_BASELINE_RED_NOT_PROVEN"
    role_turn(hook, "tester", "T1", make_tester_result("mutation", mutation_patch(wt)))
    res = cli("proof", "--session", "S1", expect=1)
    assert res["failure"]["code"] == "TEST_MUTATION_SURVIVED" and res["failure"]["suggested_owner"] == "tester"
    # tester 还没在这次失败之后答复过、也不在跑 → resume_tester
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "resume_tester"

    proc = _hold_gate(lp, "proof")
    try:
        st = cli("status", "--session", "S1")
        assert st["readiness"]["next_action"] == "awaiting_gate"
        assert st["readiness"]["gate_running"] == "proof"
    finally:
        proc.kill()
        proc.wait()
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "resume_tester"


def test_proof_fail_owner_builder_boundary_still_gates(started, cli, hook):
    """边界：proof FAIL 且 suggested_owner == "builder"，proof 门禁在跑 → awaiting_gate；锁释放后为 proof。

    用 mock 策略里允许的方式（`ledger.mutate` 直接写 evidence.proof 的 result 与 details.failure）
    把已经跑通的一轮 proof PASS 改记成 owner=builder 的 FAIL：只改 status/details，其余投影输入
    （候选、proof_spec、tester_files）不变，dependency_digest 用 `evidence.record` 按当前 ledger
    真实重算，state() 不会判它 stale。"""
    lp = started["ledger"]
    drive_to_proof_pass(started, cli, hook)
    with L.mutate(lp) as lg:
        assert lg["evidence"]["proof"]["status"] == "pass"
        evidence.record(lg, "proof", "fail",
                         {"failure": {"code": "TEST_PROOF_CANDIDATE_FAILED", "suggested_owner": "builder",
                                       "message": "候选实现没让测试过"}},
                         Path(lg["repo"]["root"]))
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "proof"

    proc = _hold_gate(lp, "proof")
    try:
        st = cli("status", "--session", "S1")
        assert st["readiness"]["next_action"] == "awaiting_gate"
        assert st["readiness"]["gate_running"] == "proof"
    finally:
        proc.kill()
        proc.wait()
    st = cli("status", "--session", "S1")
    assert st["readiness"]["next_action"] == "proof"


def test_proof_gate_running_but_tester_also_running_is_awaiting_tester(started, cli, hook):
    """边界：proof 门禁在跑但 tester 也在跑 → awaiting_tester。"""
    wt, twt, lp = started["worktree"], started["tester_worktree"], started["ledger"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt, "assert mul(2, 2) == 4  # 弱")
    role_turn(hook, "tester", "T1", make_tester_result("baseline-red"), start=False)
    cli("integrate", "--session", "S1")
    cli("machine", "--session", "S1")
    cli("proof", "--session", "S1", expect=1)
    role_turn(hook, "tester", "T1", make_tester_result("mutation", mutation_patch(wt)))
    cli("proof", "--session", "S1", expect=1)
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "resume_tester"
    # 续接 tester，让它重新处于「在跑」
    from conftest import send_message
    send_message(hook, "T1")
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})

    proc = _hold_gate(lp, "proof")
    try:
        st = cli("status", "--session", "S1")
        assert st["readiness"]["next_action"] == "awaiting_tester"
    finally:
        proc.kill()
        proc.wait()


def test_preflight_running_does_not_gate_proof_or_resume_tester(started, cli, hook):
    """不变量：只有 preflight 在跑且 proof 未过、tester 不在跑时，next_action 仍为 proof（或原本应给的 resume_tester），不是 awaiting_gate。"""
    wt, twt, lp = started["worktree"], started["tester_worktree"], started["ledger"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    cli("integrate", "--session", "S1")
    cli("machine", "--session", "S1")
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "resume_tester"  # 缺 patch

    proc = _hold_gate(lp, "preflight")
    try:
        st = cli("status", "--session", "S1")
        assert st["readiness"]["gate_running"] == "preflight"
        assert st["readiness"]["next_action"] == "resume_tester"
    finally:
        proc.kill()
        proc.wait()

    role_turn(hook, "tester", "T1", make_tester_result("mutation", mutation_patch(wt)))
    proc = _hold_gate(lp, "preflight")
    try:
        st = cli("status", "--session", "S1")
        assert st["readiness"]["gate_running"] == "preflight"
        assert st["readiness"]["next_action"] == "proof"
    finally:
        proc.kill()
        proc.wait()


def test_reviewer_action_unaffected_by_machine_proof_gate_locks(started, cli, hook):
    """不变量：proof 已 PASS 后，reviewer 相关的 next_action 不受 machine/proof 门禁锁影响。"""
    drive_to_proof_pass(started, cli, hook)
    baseline = cli("status", "--session", "S1")["readiness"]["next_action"]
    assert baseline == "spawn_reviewer"

    for holder in ("machine", "proof"):
        proc = _hold_gate(started["ledger"], holder)
        try:
            assert cli("status", "--session", "S1")["readiness"]["next_action"] == "spawn_reviewer"
        finally:
            proc.kill()
            proc.wait()


# ---------------------------------------------------------------- B3: skills/builder/SKILL.md 第 1 节第 4 条


ANCHOR_B3 = ("这条纪律只约束你手工起的测试命令和其他重负载任务："
             "`bl machine` / `bl proof` 遇到 preflight 在跑会自动排队等它跑完，"
             "`next_action` 给出它们时照常后台跑。")
ANCHOR_B3_INVARIANT = ("preflight、machine、proof 运行期间，不要在本机另起全量测试或长时间占用 "
                        "CPU / IO 的后台任务：它们会把门禁挤成假超时。")


def _normalize(s: str) -> str:
    s = s.replace("**", "").replace("`", "")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _item4_text() -> str:
    text = SKILL.read_text(encoding="utf-8")
    m = re.search(r"\n4\. \*\*顺手后台跑一次基线预检\*\*.*?(?=\n5\. )", text, re.S)
    assert m, "找不到第 1 节第 4 条"
    return m.group(0)


def test_skill_item4_contains_gate_running_signpost_anchor():
    text = SKILL.read_text(encoding="utf-8")
    full_norm = _normalize(text)
    anchor_norm = _normalize(ANCHOR_B3)
    assert full_norm.count(anchor_norm) == 1
    item4_norm = _normalize(_item4_text())
    assert anchor_norm in item4_norm


def test_skill_item4_keeps_existing_discipline_and_commands():
    text = SKILL.read_text(encoding="utf-8")
    full_norm = _normalize(text)
    assert full_norm.count(_normalize(ANCHOR_B3_INVARIANT)) == 1
    item4_norm = _normalize(_item4_text())
    assert _normalize(ANCHOR_B3_INVARIANT) in item4_norm
    assert "bl preflight --session ${CLAUDE_SESSION_ID}" in _item4_text()
    assert "run_in_background" in _item4_text()


# ---------------------------------------------------------------- B4: bl brief 文本形态的锚句


ANCHOR_B4 = "本机只跑与你这一轮相关的测试文件，不要跑全量测试套件：全量由 machine 跑，与门禁并发会把它挤成假超时。"


def test_brief_text_contains_no_full_suite_anchor_before_integrate(started, cli):
    tester_text = cli("brief", "--session", "S1", "--role", "tester")
    assert _normalize(tester_text).count(_normalize(ANCHOR_B4)) == 1
    # brief 原有事实仍在：写边界、RESULT 规则、ledger 路径
    assert "tests/**" in tester_text
    assert "BUILDER_LOOP_RESULT" in tester_text
    assert str(started["ledger"]) in tester_text


def test_brief_text_contains_no_full_suite_anchor_after_integrate(started, cli, hook):
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    cli("integrate", "--session", "S1")
    tester_text = cli("brief", "--session", "S1", "--role", "tester")
    assert _normalize(tester_text).count(_normalize(ANCHOR_B4)) == 1


def test_reviewer_brief_text_contains_full_suite_anchor(started, cli, hook):
    from conftest import drive_to_proof_pass

    drive_to_proof_pass(started, cli, hook)
    hook("SubagentStart", {"session_id": "S1", "agent_id": "R1", "agent_type": "reviewer"})
    reviewer_text = cli("brief", "--session", "S1", "--role", "reviewer")
    assert _normalize(reviewer_text).count(_normalize(ANCHOR_B4)) == 1


def test_anchor_not_in_role_md_files():
    """不变量：agents/tester.md 与 agents/reviewer.md 不包含该锚句（角色事实只在 brief 一处）。"""
    for name in ("tester.md", "reviewer.md"):
        text = (ROOT / "agents" / name).read_text(encoding="utf-8")
        assert _normalize(ANCHOR_B4) not in _normalize(text)
