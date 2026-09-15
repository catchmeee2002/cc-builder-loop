import json

from builder_loop import ledger as L
from conftest import implement_mul, marker, mutation_patch, make_tester_payload, write_mul_test


def _ready_for_tester(started, cli):
    implement_mul(started["worktree"])
    cli("checkpoint", "--session", "S1", "--role", "builder")
    assert cli("machine", "--session", "S1")["result"] == "PASS"


def test_tester_hook_rejects_out_of_scope_then_records(started, cli, hook):
    wt = started["worktree"]
    _ready_for_tester(started, cli)
    start = hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    ctx = start["json"]["hookSpecificOutput"]["additionalContext"]
    assert str(wt) in ctx and "B1" in ctx and "BUILDER_LOOP_RESULT" in ctx
    write_mul_test(wt)
    (wt / "src" / "foo.py").write_text((wt / "src" / "foo.py").read_text() + "# hack\n")
    payload = make_tester_payload("baseline-red")
    r = hook("SubagentStop", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester", "last_assistant_message": marker(payload)})
    assert r["code"] == 2 and "builder_owned" in r["stderr"]
    (wt / "src" / "foo.py").write_text((wt / "src" / "foo.py").read_text().replace("# hack\n", ""))
    # 缺标记 → exit 2
    r = hook("SubagentStop", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester", "last_assistant_message": "forgot"})
    assert r["code"] == 2
    # 第三次不合规 → 记 fail 放行
    r = hook("SubagentStop", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester", "last_assistant_message": "forgot again"})
    assert r["code"] == 0 and "已记为 fail" in r["stderr"]
    assert L.load(started["ledger"])["evidence"]["tester"]["status"] == "fail"
    # 合规 → 记 pass，machine 因测试文件变化而 stale
    r = hook("SubagentStop", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester", "last_assistant_message": marker(payload)})
    assert r["code"] == 0
    st = cli("status", "--session", "S1")["readiness"]
    assert st["states"]["tester"] == "pass" and st["states"]["machine"] == "stale" and st["next_action"] == "machine"
    # 冒充：未登记 agent_id
    r = hook("SubagentStop", {"session_id": "S1", "agent_id": "FAKE", "agent_type": "tester", "last_assistant_message": marker({"role": "tester", "status": "insufficient_spec"})})
    assert r["code"] == 0 and L.load(started["ledger"])["evidence"]["tester"]["status"] == "pass"


def test_proof_baseline_red_fails_for_new_interface_then_mutation_passes(started, cli, hook):
    wt = started["worktree"]
    _ready_for_tester(started, cli)
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    write_mul_test(wt)
    hook("SubagentStop", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester", "last_assistant_message": marker(make_tester_payload("baseline-red"))})
    cli("machine", "--session", "S1")
    res = cli("proof", "--session", "S1", expect=1)
    assert res["failure"]["code"] == "TEST_BASELINE_RED_NOT_PROVEN" and res["failure"]["suggested_owner"] == "tester"
    assert res["readiness"]["next_action"] == "resume_tester"
    # tester 改用 mutation
    patch = mutation_patch(wt)
    hook("SubagentStop", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester", "last_assistant_message": marker(make_tester_payload("mutation", patch))})
    cli("machine", "--session", "S1")
    res = cli("proof", "--session", "S1")
    assert res["result"] == "PASS" and res["groups"][0]["counterexample"]["classification"] == "assertion-failure"
    assert res["readiness"]["next_action"] == "spawn_reviewer"


def test_proof_mutation_survives_weak_test(started, cli, hook):
    wt = started["worktree"]
    _ready_for_tester(started, cli)
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    (wt / "tests" / "test_mul.py").write_text("from src.foo import mul\n\n\ndef test_mul():\n    assert mul(0, 5) == 0\n")  # a*b 与 a+b... 0+5=5 会失败；改用 mul(2,2)==4 才是弱测试
    (wt / "tests" / "test_mul.py").write_text("from src.foo import mul\n\n\ndef test_mul():\n    assert mul(2, 2) == 4\n")
    patch = mutation_patch(wt)
    hook("SubagentStop", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester", "last_assistant_message": marker(make_tester_payload("mutation", patch))})
    cli("machine", "--session", "S1")
    res = cli("proof", "--session", "S1", expect=1)
    assert res["failure"]["code"] == "TEST_MUTATION_SURVIVED"


def test_proof_spec_validation(started, cli, hook):
    wt = started["worktree"]
    _ready_for_tester(started, cli)
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    write_mul_test(wt)
    bad = make_tester_payload("mutation", "diff --git a/tests/test_mul.py b/tests/test_mul.py\n--- a/tests/test_mul.py\n+++ b/tests/test_mul.py\n@@ -1 +1 @@\n-x\n+y\n")
    r = hook("SubagentStop", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester", "last_assistant_message": marker(bad)})
    assert r["code"] == 2 and "非 builder_write" in r["stderr"]
    two = make_tester_payload("reviewed-boundaries")
    two["proof_spec"]["groups"].append(dict(two["proof_spec"]["groups"][0]))
    r = hook("SubagentStop", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester", "last_assistant_message": marker(two)})
    assert r["code"] == 2 and "一一对应" in r["stderr"]
    unsupported = make_tester_payload("reviewed-boundaries")
    unsupported["proof_spec"]["groups"][0]["argv"] = ["make", "test"]
    r = hook("SubagentStop", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester", "last_assistant_message": marker(unsupported)})
    assert r["code"] == 0  # 第三次不合规已记 fail
    assert L.load(started["ledger"])["evidence"]["tester"]["status"] == "fail"


def test_proof_stall_after_three_same_failures(started, cli, hook):
    wt = started["worktree"]
    _ready_for_tester(started, cli)
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    write_mul_test(wt)
    hook("SubagentStop", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester", "last_assistant_message": marker(make_tester_payload("baseline-red"))})
    cli("machine", "--session", "S1")
    cli("proof", "--session", "S1", expect=1)
    cli("proof", "--session", "S1", expect=1)
    res = cli("proof", "--session", "S1", expect=3)
    assert any(b["code"] == "PROOF_STALL" for b in res["readiness"]["blockers"])


def test_pre_tool_use_write_guard_for_roles(started, hook):
    wt = str(started["worktree"])
    deny = hook("PreToolUse", {"session_id": "S1", "agent_type": "tester", "agent_id": "T1", "tool_name": "Write", "tool_input": {"file_path": f"{wt}/src/foo.py"}})
    assert deny["json"]["hookSpecificOutput"]["permissionDecision"] == "deny"
    ok = hook("PreToolUse", {"session_id": "S1", "agent_type": "tester", "agent_id": "T1", "tool_name": "Write", "tool_input": {"file_path": f"{wt}/tests/test_x.py"}})
    assert ok["json"] is None
    deny = hook("PreToolUse", {"session_id": "S1", "agent_type": "reviewer", "agent_id": "R1", "tool_name": "Edit", "tool_input": {"file_path": f"{wt}/tests/test_x.py"}})
    assert deny["json"]["hookSpecificOutput"]["permissionDecision"] == "deny"
    main_session = hook("PreToolUse", {"session_id": "S1", "tool_name": "Write", "tool_input": {"file_path": f"{wt}/src/foo.py"}})
    assert main_session["json"] is None
