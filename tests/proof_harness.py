from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from harness import (
    ProcessResult,
    commit_all,
    finish_agent_turn,
    head,
    init_repo,
    plan_markdown,
    repo_session_id,
    run_cli,
    start_agent_turn,
    start_run,
    worktrees_from,
    write_plan,
)


DEFAULT_BASELINE = "def add(a, b):\n    return a + b - 1\n"
DEFAULT_CANDIDATE = "def add(a, b):\n    return a + b\n"
UNITTEST_ID = "tests.test_proof_target.ProofTargetTest.test_add"
PYTEST_ID = "tests/test_pyproof.py::test_add"


@dataclass(frozen=True)
class ProofFixture:
    repo: Path
    run_path: Path
    run_id: str
    builder: Path
    tester: Path
    tester_agent_id: str
    tester_author_head: str
    integrated_head: str


def unittest_source(body: str = "self.assertEqual(add(1, 2), 3)") -> str:
    return (
        "import unittest\n"
        "from src.calc import add\n\n"
        "class ProofTargetTest(unittest.TestCase):\n"
        "    def test_add(self):\n"
        + "\n".join(f"        {line}" for line in body.splitlines())
        + "\n"
    )


def pytest_source(body: str = "assert add(1, 2) == 3") -> str:
    return (
        "from src.calc import add\n\n"
        "def test_add():\n"
        + "\n".join(f"    {line}" for line in body.splitlines())
        + "\n"
    )


def create_proof_fixture(
    *,
    test_files: Mapping[str, str] | None = None,
    baseline_source: str = DEFAULT_BASELINE,
    candidate_source: str = DEFAULT_CANDIDATE,
    initial_files: Mapping[str, str] | None = None,
    runner: str = "bash verify.sh",
    include_e2e: bool = False,
    explicit_run_id: str | None = None,
    verify_machine: bool = True,
    requirement_minima: Mapping[str, str] | None = None,
) -> ProofFixture:
    seed = {"src/calc.py": baseline_source}
    if initial_files:
        seed.update(initial_files)
    repo = init_repo(seed)
    plan_text = plan_markdown(
        head(repo),
        builder_write=["src/**"],
        tester_write=["tests/**"],
        target_test_dirs=["tests"],
        runner=runner,
        include_e2e=include_e2e,
    )
    requirements = dict(requirement_minima or {"add-positive": "strong"})
    if requirements != {"add-positive": "strong"}:
        behaviors = []
        requirement_lines = []
        for behavior_id, minimum in requirements.items():
            behaviors.extend(
                [
                    f"  - id: {behavior_id}",
                    f'    what: "{behavior_id} observable behavior"',
                    '    boundaries: ["zero", "negative"]',
                    '    invariants: ["inputs are not mutated"]',
                ]
            )
            requirement_lines.extend(
                [
                    f"    - behavior_id: {behavior_id}",
                    f"      minimum: {minimum}",
                ]
            )
        old = (
            "behaviors:\n"
            "  - id: add-positive\n"
            '    what: "add returns the arithmetic sum"\n'
            '    boundaries: ["zero", "negative"]\n'
            '    invariants: ["inputs are not mutated"]\n'
            "test_effectiveness:\n"
            "  requirements:\n"
            "    - behavior_id: add-positive\n"
            "      minimum: strong\n"
        )
        new = "\n".join(["behaviors:", *behaviors, "test_effectiveness:", "  requirements:", *requirement_lines]) + "\n"
        if old not in plan_text:
            raise AssertionError("fixture plan test-effectiveness block drifted")
        plan_text = plan_text.replace(old, new, 1)
    plan = write_plan(repo, plan_text)
    if explicit_run_id is None:
        started, run_path = start_run(repo, plan, task="test-effectiveness proof")
    else:
        started = run_cli(
            "start",
            "--repo",
            repo,
            "--plan",
            plan,
            "--task",
            "test-effectiveness proof",
            "--run",
            explicit_run_id,
            "--session-id",
            repo_session_id(repo, "proof-fixture"),
        )
        if started.returncode != 0 or started.data.get("status") != "READY":
            raise AssertionError(
                f"fixture start failed: rc={started.returncode} data={started.data!r}"
            )
        run_path = Path(str(started.data["run_path"]))
    builder, tester = worktrees_from(started, run_path)

    (builder / "src" / "calc.py").write_text(candidate_source)
    commit_all(builder, "implement candidate behavior")

    tester_agent_id, tester_turn_id = start_agent_turn(
        run_path, "tester", agent_id="proof-tester"
    )
    authored = dict(test_files or {"tests/test_proof_target.py": unittest_source()})
    authored.setdefault("tests/__init__.py", "")
    for relative, content in authored.items():
        path = tester / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    commit_all(tester, "author independent proof tests")
    finish_agent_turn(
        run_path,
        "tester",
        agent_id=tester_agent_id,
        turn_id=tester_turn_id,
        result="tests_ready",
    )
    tester_author_head = head(tester)
    integrated = run_cli("integrate-tests", "--run", run_path)
    if integrated.returncode != 0 or integrated.data.get("status") != "READY":
        raise AssertionError(
            f"test integration failed: rc={integrated.returncode} data={integrated.data!r} "
            f"stderr={integrated.stderr}"
        )
    if verify_machine:
        verified = run_cli("verify", "--run", run_path)
        if verified.returncode != 0 or verified.data.get("status") != "PASS":
            raise AssertionError(
                f"fixture machine verification failed: rc={verified.returncode} "
                f"data={verified.data!r} stderr={verified.stderr}"
            )
    run_id = str(started.data["run_id"])
    return ProofFixture(
        repo=repo,
        run_path=run_path,
        run_id=run_id,
        builder=builder,
        tester=tester,
        tester_agent_id=tester_agent_id,
        tester_author_head=tester_author_head,
        integrated_head=head(builder),
    )


def baseline_group(
    *,
    argv: list[str] | None = None,
    test_ids: list[str] | None = None,
    behavior_id: str = "add-positive",
    timeout_seconds: int | float = 30,
) -> dict:
    return {
        "behavior_ids": [behavior_id],
        "method": "baseline-red",
        "argv": argv
        or ["python3", "-m", "unittest", UNITTEST_ID],
        "test_ids": test_ids or [UNITTEST_ID],
        "timeout_seconds": timeout_seconds,
        "claimed_failure_kind": "assertion-failure",
    }


def mutation_group(
    patch: str,
    *,
    argv: list[str] | None = None,
    test_ids: list[str] | None = None,
    behavior_id: str = "add-positive",
) -> dict:
    return {
        "behavior_ids": [behavior_id],
        "method": "mutation",
        "argv": argv
        or ["python3", "-m", "unittest", UNITTEST_ID],
        "test_ids": test_ids or [UNITTEST_ID],
        "timeout_seconds": 30,
        "patch": patch,
    }


def prove(
    fixture: ProofFixture,
    group: dict,
    *,
    env: Mapping[str, str] | None = None,
) -> ProcessResult:
    return prove_groups(fixture, [group], env=env)


def prove_groups(
    fixture: ProofFixture,
    groups: list[dict],
    *,
    env: Mapping[str, str] | None = None,
) -> ProcessResult:
    return run_cli(
        "prove-tests",
        "--repo",
        fixture.repo,
        "--run",
        fixture.run_path,
        "--spec",
        "-",
        input_text=json.dumps({"schema_version": 1, "groups": groups}),
        env=env,
    )
