"""B5：mutation patch 因 contract 层面原因被拒时，归属指向 contract 并给 tester 出路；
tester_owned 的同类失败仍归 tester。"""

from __future__ import annotations

from pathlib import Path

from builder_loop import ledger as L
from conftest import (contract_with, implement_mul, make_tester_result, mutation_patch,
                       role_turn, write_mul_test, write_plan)


def _diff_for(path: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "index 0000000..1111111 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )


def _start(repo, cli, session: str, **patches):
    contract = contract_with(**patches) if patches else None
    from conftest import CONTRACT
    import json, copy

    c = copy.deepcopy(CONTRACT) if contract is None else contract
    plan_name = f"plan-{session}.md"
    write_plan(repo.root, c, plan_name)
    out = cli("start", "--plan", str(repo.root / plan_name), "--session", session)
    return {
        "worktree": Path(out["worktree"]), "tester_worktree": Path(out["tester_worktree"]),
        "ledger": Path(out["ledger"]),
    }


def test_mutation_patch_on_protected_path_rejected_at_handback(repo, cli, hook):
    ctx = _start(repo, cli, "S1", **{"authority.protected_paths": [".claude/loop.yml", "src/foo.py"]})
    write_mul_test(ctx["tester_worktree"])
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation", _diff_for("src/foo.py")), start=False)
    assert r["code"] == 2
    stderr = r["stderr"]
    for token in ("PROOF_SPEC_INVALID", "src/foo.py", "protected", '"suggested_owner": "contract"', "status=insufficient_spec"):
        assert token in stderr, (token, stderr)


def test_mutation_patch_outside_authority_rejected_at_handback(repo, cli, hook):
    ctx = _start(repo, cli, "S1")
    write_mul_test(ctx["tester_worktree"])
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation", _diff_for("README.md")), start=False)
    assert r["code"] == 2
    stderr = r["stderr"]
    for token in ("PROOF_SPEC_INVALID", "README.md", "outside_authority", '"suggested_owner": "contract"', "status=insufficient_spec"):
        assert token in stderr, (token, stderr)


def test_mutation_patch_on_tester_owned_path_rejected_without_contract_owner(repo, cli, hook):
    ctx = _start(repo, cli, "S1")
    write_mul_test(ctx["tester_worktree"])
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation", _diff_for("tests/test_mul.py")), start=False)
    assert r["code"] == 2
    stderr = r["stderr"]
    assert "PROOF_SPEC_INVALID" in stderr and "tests/test_mul.py" in stderr and "tester_owned" in stderr
    assert "status=insufficient_spec" not in stderr
    assert '"suggested_owner": "contract"' not in stderr


def _ready_for_proof(ctx, cli, hook):
    wt, twt = ctx["worktree"], ctx["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    patch = mutation_patch(wt)
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation", patch))
    assert r["code"] == 0, r
    return patch


def test_proof_entry_check_owner_is_contract_after_contract_protects_path(repo, cli, hook):
    """交卷之后 contract 把 src/foo.py 加入 protected，再执行 `bl proof`：proof 入口的结构检查拦下，
    不进入实际执行——不记 proof 失败（ledger.failures.proof 不增加）。"""
    ctx = _start(repo, cli, "S1")
    _ready_for_proof(ctx, cli, hook)
    failures_before = len(L.load(ctx["ledger"])["failures"]["proof"])
    with L.mutate(ctx["ledger"]) as lg2:
        lg2["contract"]["authority"]["protected_paths"].append("src/foo.py")
    out = cli("proof", "--session", "S1", expect=1)
    assert out["code"] == "PROOF_SPEC_INVALID"
    assert out["details"]["suggested_owner"] == "contract"
    assert out["details"]["reason"] == "protected"
    assert len(L.load(ctx["ledger"])["failures"]["proof"]) == failures_before
