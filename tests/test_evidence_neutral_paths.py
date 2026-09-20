"""evidence_neutral_paths：loop.yml 声明的中性路径不参与 machine / proof 的输入投影，
但对 reviewer 的投影始终无效（原则一：reviewer 必须绑定完整 candidate_head）。

B1: 字段缺失 / 空数组时，projection() 的结构不受影响（candidate_head 仍是 ledger.candidate.head 的直接投影）。
B2: 只改中性路径 → machine / proof evidence 仍 pass（不 stale）。
B3: 同样的改动 → reviewer 的 evidence 仍 stale（不豁免）。
B4: 改了非中性路径（含与中性路径混合改动）→ machine / proof 仍 stale。
B5: loop.yml 冻结进 contract.assurance.evidence_neutral_paths，参与 assurance 面 digest；
    非数组类型报 CONFIG_* 错误；planner 在 contract 里写的值不是第二来源，以 loop.yml 为准。
"""

from __future__ import annotations

import copy
from pathlib import Path

from builder_loop import contract as contract_mod
from builder_loop import evidence
from builder_loop import ledger as L
from conftest import PROOF_RUNNER_CMD, PYTEST_CMD, contract_with, mutation_patch, role_turn, write_mul_test, write_plan


def _write_loop_yml(repo_root: Path, extra: str = "") -> None:
    (repo_root / ".claude" / "loop.yml").write_text(
        f"pass_cmd:\n  - stage: test\n    cmd: \"{PYTEST_CMD}\"\n    timeout: 60\nmax_iterations: 3\n"
        f"proof_runner:\n  framework: pytest\n  cmd: \"{PROOF_RUNNER_CMD}\"\n" + extra,
        encoding="utf-8",
    )


# ---------------------------------------------------------------- B1


def test_b1_projection_shape_unaffected_by_field_declaration(started, repo):
    lg = L.load(started["ledger"])
    before_machine = evidence.projection(lg, "machine", repo.root)
    before_proof = evidence.projection(lg, "proof", repo.root)
    assert before_machine["candidate_head"] == lg["candidate"]["head"]
    assert before_proof["candidate_head"] == lg["candidate"]["head"]

    # 边界：字段缺失（当前 ledger 本就没有这个 key，上面已覆盖）与字段为空数组——两种情形都不该有
    # 「豁免中性路径」的效果（那是非空数组才有的行为，见 B2），投影必须逐字节不变。
    # 注意：不测"字段声明为非空数组"时投影是否不变——那正是 B2 要求它必须变的地方，不能在 B1 里预设相反结论。
    empty = copy.deepcopy(lg)
    empty["contract"]["assurance"]["evidence_neutral_paths"] = []
    assert evidence.projection(empty, "machine", repo.root) == before_machine
    assert evidence.projection(empty, "proof", repo.root) == before_proof


def test_b1_upgrade_does_not_stale_existing_pass_evidence(repo, cli):
    """已经记录的 evidence（老 run，ledger 里没有 evidence_neutral_paths 概念）在新 runtime 上重算 state 仍是 pass。"""
    write_plan(repo.root, contract_with(**{"assurance.required": ["machine", "reviewer"]}), "lite.md")
    out = cli("start", "--plan", str(repo.root / "lite.md"), "--session", "S1")
    wt, lp = Path(out["worktree"]), Path(out["ledger"])
    (wt / "src" / "foo.py").write_text("def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    assert evidence.state(L.load(lp), "machine", repo.root) == evidence.STATE_PASS


# ---------------------------------------------------------------- B2 / B3 / B4 helpers


def _neutral_contract():
    return contract_with(**{
        "authority.builder_write": ["src/**", "docs/**"],
        "assurance.required": ["machine", "tester", "proof", "reviewer"],
    })


def _start_neutral_run(repo, cli, hook):
    _write_loop_yml(repo.root, "evidence_neutral_paths:\n  - \"docs/**\"\n")
    repo.commit_all()
    write_plan(repo.root, _neutral_contract(), "neutral.md")
    out = cli("start", "--plan", str(repo.root / "neutral.md"), "--session", "S1")
    wt, twt, lp = Path(out["worktree"]), Path(out["tester_worktree"]), Path(out["ledger"])
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    (wt / "src" / "foo.py").write_text("def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    from conftest import make_tester_result

    role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    cli("integrate", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    role_turn(hook, "tester", "T1", make_tester_result("mutation", mutation_patch(wt)))
    proof_out = cli("proof", "--session", "S1")
    assert proof_out["result"] == "PASS", proof_out
    r = role_turn(hook, "reviewer", "R1", {"role": "reviewer", "verdict": "pass", "findings": [], "behaviors_verified": ["B1"]})
    assert r["code"] == 0, r
    return wt, lp


def test_b2_neutral_doc_change_keeps_machine_and_proof_pass(repo, cli, hook):
    wt, lp = _start_neutral_run(repo, cli, hook)
    lg = L.load(lp)
    head0 = lg["candidate"]["head"]
    assert evidence.state(lg, "machine", repo.root) == evidence.STATE_PASS
    assert evidence.state(lg, "proof", repo.root) == evidence.STATE_PASS

    (wt / "docs").mkdir(exist_ok=True)
    (wt / "docs" / "notes.md").write_text("# notes\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    lg2 = L.load(lp)
    assert lg2["candidate"]["head"] != head0  # 不变量：candidate.head 确实前进了
    assert evidence.state(lg2, "machine", repo.root) == evidence.STATE_PASS
    assert evidence.state(lg2, "proof", repo.root) == evidence.STATE_PASS


def test_b2_boundary_delete_neutral_file_and_multiple_neutral_files(repo, cli, hook):
    wt, lp = _start_neutral_run(repo, cli, hook)
    (wt / "docs").mkdir(exist_ok=True)
    (wt / "docs" / "a.md").write_text("a\n")
    (wt / "docs" / "b.md").write_text("b\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    lg = L.load(lp)
    assert evidence.state(lg, "machine", repo.root) == evidence.STATE_PASS
    assert evidence.state(lg, "proof", repo.root) == evidence.STATE_PASS

    (wt / "docs" / "a.md").unlink()
    cli("checkpoint", "--session", "S1", "--role", "builder")
    lg2 = L.load(lp)
    assert evidence.state(lg2, "machine", repo.root) == evidence.STATE_PASS
    assert evidence.state(lg2, "proof", repo.root) == evidence.STATE_PASS


def test_b3_neutral_doc_change_still_stales_reviewer(repo, cli, hook):
    """不变量：reviewer 的输入投影必须始终绑定完整 candidate_head，不受 evidence_neutral_paths 影响。"""
    wt, lp = _start_neutral_run(repo, cli, hook)
    assert evidence.state(L.load(lp), "reviewer", repo.root) == evidence.STATE_PASS
    (wt / "docs").mkdir(exist_ok=True)
    (wt / "docs" / "notes.md").write_text("# notes\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    assert evidence.state(L.load(lp), "reviewer", repo.root) == evidence.STATE_STALE


def test_b4_non_neutral_change_still_stales_machine_and_proof(repo, cli, hook):
    wt, lp = _start_neutral_run(repo, cli, hook)
    (wt / "src" / "foo.py").write_text((wt / "src" / "foo.py").read_text() + "\n# touch\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    lg = L.load(lp)
    assert evidence.state(lg, "machine", repo.root) == evidence.STATE_STALE
    assert evidence.state(lg, "proof", repo.root) == evidence.STATE_STALE


def test_b4_boundary_mixed_neutral_and_non_neutral_change_still_stales(repo, cli, hook):
    """边界：同一次 checkpoint 既改中性路径又改非中性路径时仍 stale。"""
    wt, lp = _start_neutral_run(repo, cli, hook)
    (wt / "docs").mkdir(exist_ok=True)
    (wt / "docs" / "notes.md").write_text("# notes\n")
    (wt / "src" / "foo.py").write_text((wt / "src" / "foo.py").read_text() + "\n# touch\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    lg = L.load(lp)
    assert evidence.state(lg, "machine", repo.root) == evidence.STATE_STALE
    assert evidence.state(lg, "proof", repo.root) == evidence.STATE_STALE


# ---------------------------------------------------------------- B5


def test_b5_frozen_into_contract_and_participates_in_assurance_digest(repo, cli):
    _write_loop_yml(repo.root, 'evidence_neutral_paths:\n  - "docs/**"\n')
    repo.commit_all()
    write_plan(repo.root, contract_with(**{"assurance.required": ["machine", "reviewer"]}), "lite.md")
    out = cli("start", "--plan", str(repo.root / "lite.md"), "--session", "S1")
    lg = L.load(Path(out["ledger"]))
    assert lg["contract"]["assurance"]["evidence_neutral_paths"] == ["docs/**"]

    # 参与 assurance 面 digest：改动它必须让 digests.assurance 变化
    a1 = copy.deepcopy(lg["contract"]["assurance"])
    a2 = copy.deepcopy(a1)
    a2["evidence_neutral_paths"] = ["other/**"]
    from builder_loop.jsonutil import digest as jdigest

    assert jdigest(a1) != jdigest(a2)
    assert lg["contract"]["digests"]["assurance"] == jdigest(lg["contract"]["assurance"])

    # 不变量：machine_commands 与 proof_runner 的冻结行为不变
    assert lg["contract"]["assurance"]["machine_commands"][0]["cmd"] == PYTEST_CMD
    assert lg["contract"]["assurance"]["proof_runner"]["cmd"] == PROOF_RUNNER_CMD


def test_b5_planner_declared_value_is_overridden_by_loop_yml(repo, cli):
    """planner 不得成为第二来源：contract 里写的 evidence_neutral_paths 必须被 loop.yml 的冻结值覆盖。"""
    _write_loop_yml(repo.root, 'evidence_neutral_paths:\n  - "docs/**"\n')
    repo.commit_all()
    contract = contract_with(**{
        "assurance.required": ["machine", "reviewer"],
        "assurance.evidence_neutral_paths": ["custom/**"],
    })
    write_plan(repo.root, contract, "lite.md")
    out = cli("start", "--plan", str(repo.root / "lite.md"), "--session", "S2")
    lg = L.load(Path(out["ledger"]))
    assert lg["contract"]["assurance"]["evidence_neutral_paths"] == ["docs/**"]


def test_b5_boundary_non_array_value_is_a_config_error(repo, cli):
    """`bl start` 的既有约定是 0 / 2 配置错 / 3 已有 run（skills/builder-loop/SKILL.md）：
    配置错报 CONFIG_* 且退出码 2，不是静默忽略，也不必是 1。"""
    _write_loop_yml(repo.root, "evidence_neutral_paths: 5\n")
    repo.commit_all()
    write_plan(repo.root, contract_with(**{"assurance.required": ["machine", "reviewer"]}), "lite.md")
    out = cli("start", "--plan", str(repo.root / "lite.md"), "--session", "S3", expect=2)
    assert str(out.get("code", "")).startswith("CONFIG_")
