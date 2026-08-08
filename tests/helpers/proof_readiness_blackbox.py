from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests"
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from harness import (  # noqa: E402
    CLI,
    cleanup_repo,
    commit_all,
    fixture_runtime_env,
    head,
    init_repo,
    run_process,
)
from proof_harness import (  # noqa: E402
    baseline_group,
    create_proof_fixture,
    mutation_group,
    prove_groups,
)
from jsonschema import Draft202012Validator  # noqa: E402


CHILD_ENV = {"PYTHONDONTWRITEBYTECODE": "1"}


def require(condition: bool, message: str, value: Any = None) -> None:
    if not condition:
        suffix = "" if value is None else f": {value!r}"
        raise AssertionError(message + suffix)


def marker_wrapper(marker: Path) -> str:
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"printf 'invoked\\n' >> {shlex.quote(str(marker))}\n"
        'exec "$@"\n'
    )


def unittest_module(class_name: str, *, expected: int) -> str:
    return (
        "import unittest\n"
        "from src.calc import add\n\n"
        f"class {class_name}(unittest.TestCase):\n"
        "    def test_value(self):\n"
        f"        self.assertEqual(add(1, 2), {expected})\n"
    )


def parse_json_output(completed: Any) -> dict[str, Any]:
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    require(bool(lines), "command produced no JSON", completed.stderr)
    value = json.loads(lines[-1])
    require(isinstance(value, dict), "command JSON is not an object", value)
    return value


def assurance(
    command: str,
    *args: str | Path,
    input_value: Any = None,
) -> tuple[int, dict[str, Any]]:
    env = dict(CHILD_ENV)
    values = [str(value) for value in args]
    if "--repo" in values:
        env.update(fixture_runtime_env(Path(values[values.index("--repo") + 1])))
    completed = run_process(
        [
            sys.executable,
            CLI,
            "assurance",
            "--experimental-v4",
            command,
            *args,
        ],
        cwd=ROOT,
        env=env,
        input_text=(
            json.dumps(input_value, ensure_ascii=False)
            if input_value is not None
            else None
        ),
    )
    return completed.returncode, parse_json_output(completed)


def require_ready(command: str, *args: str | Path, input_value: Any = None) -> dict:
    returncode, value = assurance(command, *args, input_value=input_value)
    require(returncode == 0, f"{command} failed", value)
    return value


def scan_if_required(repo: Path, run_id: str) -> None:
    action = require_ready("driver-next", "--repo", repo, "--run", run_id)
    if action.get("action") != "scan_doc_references":
        return
    require_ready(
        "scan-doc-references",
        "--repo",
        repo,
        "--run",
        run_id,
        "--action-id",
        str(action["action_id"]),
    )


def normalize_v4_failure(value: Mapping[str, Any]) -> dict[str, Any]:
    details = value.get("details")
    if not isinstance(details, Mapping):
        details = {
            key: item
            for key, item in value.items()
            if key not in {"status", "code", "message"}
        }
    return {
        "status": value.get("status"),
        "code": value.get("code"),
        "message": value.get("message"),
        **dict(details),
    }


def validate_failure_schema(value: dict[str, Any]) -> None:
    schema = json.loads(
        (ROOT / "schema" / "codex-test-proof.schema.json").read_text(
            encoding="utf-8"
        )
    )
    validator = Draft202012Validator(
        {
            "$schema": schema["$schema"],
            "$defs": schema["$defs"],
            "$ref": "#/$defs/proofFailure",
        }
    )
    errors = sorted(validator.iter_errors(value), key=lambda error: list(error.path))
    require(not errors, "published proof failure schema rejected runtime output", errors)


def legacy_observations(artifacts: Path) -> dict[str, Any]:
    repos: list[Path] = []
    try:
        first_id = "tests.test_first_group.FirstGroupTest.test_value"
        second_id = "tests.test_second_group.SecondGroupTest.test_value"
        machine_id = "tests.test_machine_gate.MachineGateTest.test_candidate"
        aggregate_marker = artifacts / "legacy-aggregate-invocations"
        aggregate = create_proof_fixture(
            test_files={
                "tests/test_first_group.py": unittest_module(
                    "FirstGroupTest", expected=99
                ),
                "tests/test_second_group.py": unittest_module(
                    "SecondGroupTest", expected=99
                ),
                "tests/test_machine_gate.py": unittest_module(
                    "MachineGateTest", expected=3
                ).replace("def test_value", "def test_candidate"),
            },
            initial_files={"verify.sh": marker_wrapper(aggregate_marker)},
            runner=f"bash verify.sh python3 -m unittest {machine_id}",
            requirement_minima={
                "first-proof-group": "strong",
                "second-proof-group": "strong",
            },
        )
        repos.append(aggregate.repo)
        aggregate_marker.unlink()
        aggregate_result = prove_groups(
            aggregate,
            [
                baseline_group(
                    argv=[
                        "bash",
                        "verify.sh",
                        "python3",
                        "-m",
                        "unittest",
                        first_id,
                    ],
                    test_ids=[first_id],
                    behavior_id="first-proof-group",
                ),
                baseline_group(
                    argv=[
                        "bash",
                        "verify.sh",
                        "python3",
                        "-m",
                        "unittest",
                        second_id,
                    ],
                    test_ids=[second_id],
                    behavior_id="second-proof-group",
                ),
            ],
            env=CHILD_ENV,
        )
        failure = aggregate_result.data
        require(aggregate_result.returncode == 1, "legacy aggregate did not fail", failure)
        require(
            failure.get("code") == "TEST_PROOF_CANDIDATE_FAILED",
            "legacy aggregate returned wrong code",
            failure,
        )
        failures = failure.get("failures")
        require(isinstance(failures, list), "legacy aggregate omitted failures", failure)
        require(
            [item["group"] for item in failures] == [0, 1],
            "legacy aggregate order drifted",
            failures,
        )
        require(
            failure.get("group") == 0 and failure.get("result") == failures[0]["result"],
            "legacy first-failure compatibility fields drifted",
            failure,
        )
        require(
            aggregate_marker.read_text(encoding="utf-8").splitlines()
            == ["invoked", "invoked"],
            "legacy counterexample ran before candidate readiness completed",
        )
        validate_failure_schema(failure)

        single = create_proof_fixture(
            test_files={
                "tests/test_proof_target.py": unittest_module(
                    "ProofTargetTest", expected=99
                ),
                "tests/test_machine_gate.py": unittest_module(
                    "MachineGateTest", expected=3
                ).replace("def test_value", "def test_candidate"),
            },
            initial_files={"verify.sh": "#!/usr/bin/env bash\nexec \"$@\"\n"},
            runner=f"bash verify.sh python3 -m unittest {machine_id}",
        )
        repos.append(single.repo)
        single_result = prove_groups(single, [baseline_group()], env=CHILD_ENV)
        single_failure = single_result.data
        require(single_result.returncode == 1, "legacy single failure did not fail")
        require(
            single_failure.get("group") == 0
            and isinstance(single_failure.get("result"), dict),
            "legacy single failure lost compatibility fields",
            single_failure,
        )
        validate_failure_schema(single_failure)

        baseline_id = "tests.test_baseline_group.BaselineGroupTest.test_value"
        mutation_id = "tests.test_mutation_group.MutationGroupTest.test_value"
        success_marker = artifacts / "legacy-success-invocations"
        success = create_proof_fixture(
            test_files={
                "tests/test_baseline_group.py": unittest_module(
                    "BaselineGroupTest", expected=3
                ),
                "tests/test_mutation_group.py": unittest_module(
                    "MutationGroupTest", expected=3
                ),
                "tests/test_machine_gate.py": unittest_module(
                    "MachineGateTest", expected=3
                ).replace("def test_value", "def test_candidate"),
            },
            initial_files={"verify.sh": marker_wrapper(success_marker)},
            runner=f"bash verify.sh python3 -m unittest {machine_id}",
            requirement_minima={
                "baseline-proof-group": "strong",
                "mutation-proof-group": "strong",
            },
        )
        repos.append(success.repo)
        success_marker.unlink()
        success_result = prove_groups(
            success,
            [
                baseline_group(
                    argv=[
                        "bash",
                        "verify.sh",
                        "python3",
                        "-m",
                        "unittest",
                        baseline_id,
                    ],
                    test_ids=[baseline_id],
                    behavior_id="baseline-proof-group",
                ),
                mutation_group(
                    (
                        "diff --git a/src/calc.py b/src/calc.py\n"
                        "--- a/src/calc.py\n"
                        "+++ b/src/calc.py\n"
                        "@@ -1,2 +1,2 @@\n"
                        " def add(a, b):\n"
                        "-    return a + b\n"
                        "+    return a + b + 1\n"
                    ),
                    argv=[
                        "bash",
                        "verify.sh",
                        "python3",
                        "-m",
                        "unittest",
                        mutation_id,
                    ],
                    test_ids=[mutation_id],
                    behavior_id="mutation-proof-group",
                ),
            ],
            env=CHILD_ENV,
        )
        require(success_result.returncode == 0, "legacy success proof failed", success_result.data)
        require(
            success_marker.read_text(encoding="utf-8").splitlines()
            == ["invoked"] * 4,
            "legacy success path did not run both counterexamples",
        )
        groups = success_result.data["groups"]
        require(
            groups[0]["baseline"]["test_result"]["classification"]
            == "assertion-failure",
            "legacy baseline evidence changed",
            groups[0],
        )
        require(
            groups[1]["mutation"]["test_result"]["classification"]
            == "assertion-failure",
            "legacy mutation evidence changed",
            groups[1],
        )
        return {
            "aggregate_groups": [item["group"] for item in failures],
            "single_group": single_failure["group"],
            "success_methods": ["baseline-red", "mutation"],
        }
    finally:
        for repo in repos:
            cleanup_repo(repo)


def v4_contract(behavior_ids: list[str], execution_id: str) -> dict[str, Any]:
    return {
        "schema_version": 4,
        "mission": {
            "revision": 1,
            "objective": "Exercise public Assurance v4 proof readiness.",
            "behaviors": [
                {
                    "id": behavior_id,
                    "description": f"{behavior_id} remains independently observable.",
                }
                for behavior_id in behavior_ids
            ],
            "interfaces": [
                {
                    "id": "proof-cli",
                    "description": "The public Assurance v4 prove-tests CLI.",
                }
            ],
            "acceptance_cases": [
                {
                    "id": "proof-readiness",
                    "description": "Proof readiness is reported through the public CLI.",
                    "observation": {
                        "surface_id": "proof-cli",
                        "surface_description": "The public Assurance v4 prove-tests CLI.",
                        "execution_ids": [execution_id],
                        "required_dimensions": ["mechanical", "verify"],
                    },
                }
            ],
            "trust_boundaries": [
                {
                    "id": "candidate-before-counterexample",
                    "description": "All candidate groups run before counterexamples.",
                }
            ],
        },
        "authority": {
            "target_branch": "main",
            "builder_write": ["src/calc.py"],
            "tester_write": ["tests/**"],
            "dirty_intake": [],
            "external_targets": [],
            "protected_support_paths": [],
            "public_prerequisites": [],
        },
        "assurance": {
            "required": ["tester", "proof", "machine", "blackbox", "reviewer"],
            "machine_commands": [
                {
                    "id": "fixture-machine",
                    "argv": ["/usr/bin/python3", "-m", "unittest"],
                    "timeout_seconds": 30,
                    "expected_returncodes": [0],
                    "run_before_full_suite": False,
                }
            ],
        },
        "execution": {
            "version": 1,
            "driver_enforced": True,
            "candidate_head": None,
            "builder_files": [],
            "tester_files": [],
            "tester_source": None,
            "dirty_snapshot": [],
            "commands": [
                {
                    "id": execution_id,
                    "argv": ["/usr/bin/python3", "-m", "unittest"],
                    "timeout_seconds": 30,
                    "expected_returncodes": [0],
                    "run_before_full_suite": False,
                }
            ],
            "agents": {
                "tester": {
                    "agent_id": "proof-readiness-tester",
                    "thread_id": "proof-readiness-tester-thread",
                },
                "reviewer": {
                    "agent_id": "proof-readiness-reviewer",
                    "thread_id": "proof-readiness-reviewer-thread",
                },
            },
        },
    }


def make_uv_launcher(path: Path, marker: Path) -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import sys\n\n"
        f"open({str(marker)!r}, 'a', encoding='utf-8').write('invoked\\n')\n"
        "if sys.argv[1:5] != ['run', '--frozen', '--offline', '--no-env-file']:\n"
        "    raise SystemExit(92)\n"
        "command = sys.argv[5:]\n"
        "if not command:\n"
        "    raise SystemExit(93)\n"
        "if command[0] == 'python':\n"
        "    command[0] = sys.executable\n"
        "os.execvpe(command[0], command, os.environ)\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path.resolve()


def prepare_v4(
    artifacts: Path,
    *,
    run_id: str,
    behavior_ids: list[str],
    test_files: Mapping[str, str],
    marker: Path,
) -> dict[str, Any]:
    repo = init_repo(
        {
            "src/calc.py": "def add(a, b):\n    return a + b - 1\n",
            "pyproject.toml": (
                "[project]\n"
                "name = 'proof-readiness-fixture'\n"
                "version = '0.0.0'\n"
                "requires-python = '>=3.11'\n"
            ),
            "uv.lock": "version = 1\nrevision = 3\nrequires-python = '>=3.11'\n",
        }
    )
    contract_path = artifacts / f"{run_id}-contract.json"
    contract_path.write_text(
        json.dumps(v4_contract(behavior_ids, "proof-readiness-blackbox")),
        encoding="utf-8",
    )
    started = require_ready(
        "start",
        "--repo",
        repo,
        "--run",
        run_id,
        "--session-id",
        f"{run_id}-session",
        "--contract",
        contract_path,
    )
    candidate = Path(started["candidate_worktree"])
    (candidate / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    commit_all(candidate, f"implement {run_id} candidate")
    require_ready("checkpoint-builder", "--repo", repo, "--run", run_id)
    scan_if_required(repo, run_id)

    require_ready(
        "prepare-tester",
        "--repo",
        repo,
        "--run",
        run_id,
        "--agent-id",
        "proof-readiness-tester",
        "--thread-id",
        "proof-readiness-tester-thread",
    )
    context = require_ready("driver-context", "--repo", repo, "--run", run_id)
    tester_source = context["facets"]["execution"]["tester_source"]
    tester_worktree = Path(tester_source["worktree"])
    (tester_worktree / "tests" / "__init__.py").write_text("", encoding="utf-8")
    for relative, content in test_files.items():
        path = tester_worktree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    commit_all(tester_worktree, f"author {run_id} proof tests")
    require_ready("integrate-tester", "--repo", repo, "--run", run_id)
    scan_if_required(repo, run_id)

    context = require_ready("driver-context", "--repo", repo, "--run", run_id)
    execution = context["facets"]["execution"]
    source = execution["tester_source"]
    report_path = artifacts / f"{run_id}-tester.json"
    report_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "tester",
                "status": "pass",
                "candidate_head": execution["candidate_head"],
                "producer": {
                    "role": "tester",
                    "agent_id": "proof-readiness-tester",
                    "thread_id": "proof-readiness-tester-thread",
                },
                "details": {
                    "result": "tests_ready",
                    "source_head": source["head"],
                    "files": source["files"],
                },
            }
        ),
        encoding="utf-8",
    )
    require_ready(
        "record-evidence",
        "--repo",
        repo,
        "--run",
        run_id,
        "--kind",
        "tester",
        "--report",
        report_path,
    )
    action = require_ready("driver-next", "--repo", repo, "--run", run_id)
    require(action.get("action") == "tester_proof", "v4 proof action missing", action)
    launcher_root = artifacts / run_id
    launcher_root.mkdir(parents=True, exist_ok=True)
    launcher = make_uv_launcher(launcher_root / "uv", marker)
    return {
        "repo": repo,
        "run_id": run_id,
        "action": action,
        "tester": execution["agents"]["tester"],
        "launcher": launcher,
    }


def uv_group(
    launcher: Path,
    *,
    behavior_id: str,
    test_id: str,
    method: str = "baseline-red",
    patch: str | None = None,
) -> dict[str, Any]:
    group: dict[str, Any] = {
        "behavior_ids": [behavior_id],
        "method": method,
        "argv": [
            str(launcher),
            "run",
            "--frozen",
            "--offline",
            "--no-env-file",
            "python",
            "-m",
            "unittest",
            test_id,
        ],
        "test_ids": [test_id],
        "timeout_seconds": 30,
    }
    if method == "baseline-red":
        group["claimed_failure_kind"] = "assertion-failure"
    elif method == "mutation":
        require(isinstance(patch, str), "mutation group omitted patch")
        group["patch"] = patch
    return group


def prove_v4(fixture: Mapping[str, Any], spec: dict[str, Any], artifacts: Path) -> tuple[int, dict]:
    spec_path = artifacts / f"{fixture['run_id']}-spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    tester = fixture["tester"]
    return assurance(
        "prove-tests",
        "--repo",
        fixture["repo"],
        "--run",
        fixture["run_id"],
        "--spec",
        spec_path,
        "--agent-id",
        tester["agent_id"],
        "--thread-id",
        tester["thread_id"],
        "--action-id",
        fixture["action"]["action_id"],
    )


def v4_observations(artifacts: Path) -> dict[str, Any]:
    repos: list[Path] = []
    try:
        first_id = "tests.test_first_group.FirstGroupTest.test_value"
        second_id = "tests.test_second_group.SecondGroupTest.test_value"
        aggregate_marker = artifacts / "v4-aggregate-invocations"
        aggregate = prepare_v4(
            artifacts,
            run_id="v4-proof-readiness-aggregate",
            behavior_ids=["first-proof-group", "second-proof-group"],
            test_files={
                "tests/test_first_group.py": unittest_module(
                    "FirstGroupTest", expected=99
                ),
                "tests/test_second_group.py": unittest_module(
                    "SecondGroupTest", expected=99
                ),
            },
            marker=aggregate_marker,
        )
        repos.append(aggregate["repo"])
        aggregate_rc, aggregate_value = prove_v4(
            aggregate,
            {
                "schema_version": 1,
                "groups": [
                    uv_group(
                        aggregate["launcher"],
                        behavior_id="first-proof-group",
                        test_id=first_id,
                    ),
                    uv_group(
                        aggregate["launcher"],
                        behavior_id="second-proof-group",
                        test_id=second_id,
                    ),
                ],
            },
            artifacts,
        )
        require(aggregate_rc == 1, "v4 aggregate did not fail", aggregate_value)
        aggregate_failure = normalize_v4_failure(aggregate_value)
        failures = aggregate_failure.get("failures")
        require(isinstance(failures, list), "v4 aggregate omitted failures", aggregate_failure)
        require(
            [item["group"] for item in failures] == [0, 1],
            "v4 aggregate order drifted",
            failures,
        )
        require(
            aggregate_failure.get("group") == 0
            and aggregate_failure.get("result") == failures[0]["result"],
            "v4 first-failure compatibility fields drifted",
            aggregate_failure,
        )
        require(
            aggregate_marker.read_text(encoding="utf-8").splitlines()
            == ["invoked", "invoked"],
            "v4 counterexample ran before candidate readiness completed",
        )
        validate_failure_schema(aggregate_failure)

        single_marker = artifacts / "v4-single-invocations"
        single_id = "tests.test_single_group.SingleGroupTest.test_value"
        single = prepare_v4(
            artifacts,
            run_id="v4-proof-readiness-single",
            behavior_ids=["single-proof-group"],
            test_files={
                "tests/test_single_group.py": unittest_module(
                    "SingleGroupTest", expected=99
                )
            },
            marker=single_marker,
        )
        repos.append(single["repo"])
        single_rc, single_value = prove_v4(
            single,
            {
                "schema_version": 1,
                "groups": [
                    uv_group(
                        single["launcher"],
                        behavior_id="single-proof-group",
                        test_id=single_id,
                    )
                ],
            },
            artifacts,
        )
        require(single_rc == 1, "v4 single failure did not fail", single_value)
        single_failure = normalize_v4_failure(single_value)
        require(
            single_failure.get("group") == 0
            and isinstance(single_failure.get("result"), dict),
            "v4 single failure lost compatibility fields",
            single_failure,
        )
        validate_failure_schema(single_failure)

        baseline_id = "tests.test_baseline_group.BaselineGroupTest.test_value"
        mutation_id = "tests.test_mutation_group.MutationGroupTest.test_value"
        success_marker = artifacts / "v4-success-invocations"
        success = prepare_v4(
            artifacts,
            run_id="v4-proof-readiness-success",
            behavior_ids=["baseline-proof-group", "mutation-proof-group"],
            test_files={
                "tests/test_baseline_group.py": unittest_module(
                    "BaselineGroupTest", expected=3
                ),
                "tests/test_mutation_group.py": unittest_module(
                    "MutationGroupTest", expected=3
                ),
            },
            marker=success_marker,
        )
        repos.append(success["repo"])
        success_rc, success_value = prove_v4(
            success,
            {
                "schema_version": 1,
                "groups": [
                    uv_group(
                        success["launcher"],
                        behavior_id="baseline-proof-group",
                        test_id=baseline_id,
                    ),
                    uv_group(
                        success["launcher"],
                        behavior_id="mutation-proof-group",
                        test_id=mutation_id,
                        method="mutation",
                        patch=(
                            "diff --git a/src/calc.py b/src/calc.py\n"
                            "--- a/src/calc.py\n"
                            "+++ b/src/calc.py\n"
                            "@@ -1,2 +1,2 @@\n"
                            " def add(a, b):\n"
                            "-    return a + b\n"
                            "+    return a + b + 1\n"
                        ),
                    ),
                ],
            },
            artifacts,
        )
        require(success_rc == 0, "v4 success proof failed", success_value)
        require(
            success_marker.read_text(encoding="utf-8").splitlines()
            == ["invoked"] * 4,
            "v4 success path did not run both counterexamples",
        )
        context = require_ready(
            "driver-context",
            "--repo",
            success["repo"],
            "--run",
            success["run_id"],
        )
        results = context["evidence"]["proof"]["details"]["results"]
        require(
            results[0]["baseline"]["test_result"]["classification"]
            == "assertion-failure",
            "v4 baseline evidence changed",
            results[0],
        )
        require(
            results[1]["mutation"]["test_result"]["classification"]
            == "assertion-failure",
            "v4 mutation evidence changed",
            results[1],
        )
        return {
            "aggregate_groups": [item["group"] for item in failures],
            "single_group": single_failure["group"],
            "success_methods": ["baseline-red", "mutation"],
        }
    finally:
        for repo in repos:
            cleanup_repo(repo)


def main() -> int:
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    with tempfile.TemporaryDirectory(prefix="proof-readiness-blackbox-") as raw:
        artifacts = Path(raw)
        observations = {
            "legacy": legacy_observations(artifacts),
            "assurance_v4": v4_observations(artifacts),
        }
    print(json.dumps({"status": "pass", "observations": observations}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
