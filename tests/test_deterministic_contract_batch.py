from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator

from harness import (
    ROOT,
    _canonical_case_results,
    assert_status,
    assert_status_one_of,
    cleanup_repo,
    commit_all,
    git,
    head,
    init_repo,
    l1_plan_markdown,
    load_ledger,
    plan_markdown,
    problem_report,
    record_problems,
    run_cli,
    start_agent_turn,
    start_run,
    worktrees_from,
    write_plan,
)
from proof_harness import (
    DEFAULT_BASELINE,
    DEFAULT_CANDIDATE,
    ProofFixture,
    baseline_group,
    prove,
    unittest_source,
)


BLACKBOX_SCHEMA = ROOT / "schema" / "codex-blackbox-report.schema.json"
BUILDER_SKILL = ROOT / "skills" / "builder" / "SKILL.md"
REVIEWER_AGENT = ROOT / "agents" / "reviewer.toml"
STABLE_CLI = Path("scripts/codex-builder-loop.py")


def _replace_e2e_cases(plan: str, body: str) -> str:
    start = "<!-- e2e-cases -->"
    end = "<!-- /e2e-cases -->"
    before, remainder = plan.split(start, 1)
    _old, after = remainder.split(end, 1)
    return before + start + "\n" + body.rstrip() + "\n" + end + after


def _dimension(status: str, observation: str) -> dict[str, str]:
    return {"status": status, "observation": observation}


def _passing_case(case_id: str = "add-cli") -> dict[str, Any]:
    return {
        "case_id": case_id,
        "mechanical": _dimension("pass", "The frozen command returned zero."),
        "verify": _dimension("pass", "The frozen behavior was observed."),
        "quality": _dimension("pass", "The result met the frozen quality criteria."),
        "outcome": "pass",
    }


def _v2_details(
    fixture: ProofFixture,
    executions: list[dict[str, Any]],
    *,
    cases: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": 2,
        "candidate_worktree": str(fixture.builder),
        "head_before": fixture.integrated_head,
        "head_after": fixture.integrated_head,
        "candidate_dirty": False,
        "executions": executions,
    }
    if cases is not None:
        value["cases"] = cases
    return value


def _execution(
    command: str, *, returncode: int | float = 0, timed_out: bool = False
) -> dict[str, Any]:
    return {
        "method": "command",
        "command": command,
        "returncode": returncode,
        "timed_out": timed_out,
    }


def _method_error(method: str = "browser", reason: str = "Method unavailable.") -> dict[str, str]:
    return {"method": method, "reason": reason}


def _blackbox_version(value: Any) -> int | None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = key.lower().replace("-", "_")
            if "blackbox" in normalized and "version" in normalized and isinstance(item, int):
                return item
        for item in value.values():
            found = _blackbox_version(item)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _blackbox_version(item)
            if found is not None:
                return found
    return None


def _remove_blackbox_version(value: Any) -> None:
    if isinstance(value, dict):
        for key in list(value):
            normalized = key.lower().replace("-", "_")
            if "blackbox" in normalized and "version" in normalized:
                value.pop(key)
            else:
                _remove_blackbox_version(value[key])
    elif isinstance(value, list):
        for item in value:
            _remove_blackbox_version(item)


def _write_legacy_ledger(run_path: Path) -> None:
    ledger_path = run_path / "ledger.json"
    ledger = load_ledger(run_path)
    _remove_blackbox_version(ledger)
    ledger_path.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n")


def _finish_blackbox_turn(fixture: ProofFixture) -> dict[str, Any]:
    prepared = run_cli(
        "prepare-follow-up",
        "--run",
        fixture.run_path,
        "--role",
        "tester",
        "--agent-id",
        fixture.tester_agent_id,
        "--purpose",
        "blackbox",
    )
    assert_status(prepared, "READY", rc=0)
    ledger = load_ledger(fixture.run_path)
    completed = run_cli(
        "agent-event",
        "--repo",
        str(fixture.repo),
        "--session-id",
        str(ledger["owner_session_id"]),
        "--role",
        "tester",
        "--agent-id",
        fixture.tester_agent_id,
        "--turn-id",
        f"{fixture.tester_agent_id}-blackbox-{len(ledger.get('events', []))}",
        "--event",
        "idle",
        "--result",
        "pass",
        env={"BUILDER_LOOP_HOOK_EVENT": "1"},
    )
    assert_status(completed, "READY", rc=0)
    return prepared.data


def _create_fixture(
    test: unittest.TestCase,
    *,
    include_e2e: bool = True,
    e2e_body: str | None = None,
) -> tuple[ProofFixture, dict[str, Any]]:
    initial_files = {
        "src/calc.py": DEFAULT_BASELINE,
        "tests.py": "ROOT_COLLISION = True\n",
        "tests/pytest.ini": "[pytest]\n",
        "tests/test_widget.py": (
            "import unittest\n\n"
            "class WidgetTest(unittest.TestCase):\n"
            "    def test_widget(self):\n"
            "        self.assertEqual(1 + 1, 2)\n"
        ),
        "package/__init__.py": "",
        "package/tests.py": "MODULE_COLLISION = True\n",
        "package/tests/__init__.py": "",
        "package/tests/test_inside.py": "INSIDE_PACKAGE = True\n",
        "tools/one.py": "raise SystemExit(0)\n",
        "tools/two.py": "raise SystemExit(0)\n",
        "tools/blackbox_runner.py": "raise SystemExit(0)\n",
    }
    repo = init_repo(initial_files)
    test.addCleanup(cleanup_repo, repo)
    scopes = {
        "machine": {"affects": ["src/**"], "exempt": ["docs/**"]},
        "blackbox": {"affects": [], "exempt": ["src/**", "docs/**"]},
    }
    plan_text = plan_markdown(
        head(repo),
        include_e2e=include_e2e,
        evidence_scopes=scopes,
    )
    if e2e_body is not None:
        plan_text = _replace_e2e_cases(plan_text, e2e_body)
    plan = write_plan(repo, plan_text)
    started, run_path = start_run(repo, plan)
    builder, tester = worktrees_from(started, run_path)

    (builder / "src" / "calc.py").write_text(DEFAULT_CANDIDATE)
    commit_all(builder, "implement candidate behavior")

    tester_agent_id, tester_turn_id = start_agent_turn(
        run_path, "tester", agent_id="deterministic-contract-tester"
    )
    (tester / "tests" / "__init__.py").write_text("")
    (tester / "tests" / "test_proof_target.py").write_text(unittest_source())
    commit_all(tester, "author independent proof tests")
    from harness import finish_agent_turn

    finish_agent_turn(
        run_path,
        "tester",
        agent_id=tester_agent_id,
        turn_id=tester_turn_id,
        result="tests_ready",
    )
    assert_status_one_of(
        run_cli("integrate-tests", "--run", run_path),
        {"READY", "NOOP"},
        rc=0,
    )
    assert_status(run_cli("verify", "--run", run_path), "PASS", rc=0)
    fixture = ProofFixture(
        repo=repo,
        run_path=run_path,
        run_id=str(started.data["run_id"]),
        builder=builder,
        tester=tester,
        tester_agent_id=tester_agent_id,
        tester_author_head=head(tester),
        integrated_head=head(builder),
    )
    proof = prove(fixture, baseline_group())
    assert_status(proof, "READY", rc=0)
    prepared = _finish_blackbox_turn(fixture)
    return fixture, prepared


def _record_details(fixture: ProofFixture, details: dict[str, Any]):
    return run_cli(
        "record-evidence",
        "--run",
        fixture.run_path,
        "--kind",
        "e2e_verified",
        "--head",
        fixture.integrated_head,
        "--agent-id",
        fixture.tester_agent_id,
        "--details",
        json.dumps(details),
    )


def _record_legacy_command(fixture: ProofFixture, command: str):
    _write_legacy_ledger(fixture.run_path)
    details = {
        "candidate_worktree": str(fixture.builder),
        "head_before": fixture.integrated_head,
        "head_after": fixture.integrated_head,
        "command": command,
        "returncode": 0,
        "candidate_dirty": False,
        "cases": _canonical_case_results(load_ledger(fixture.run_path), passed=True),
    }
    return _record_details(fixture, details)


def _case_contracts(value: Any) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            case_id = item.get("case_id", item.get("id"))
            if isinstance(case_id, str) and any(
                key in item for key in ("applicability", "mechanical", "verify", "quality")
            ):
                current = found.get(case_id)
                if current is None or len(item) > len(current):
                    found[case_id] = item
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return found


def _is_applicable(case: dict[str, Any], dimension: str) -> bool | None:
    source = case.get("applicability")
    value = source.get(dimension) if isinstance(source, dict) else case.get(dimension)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.lower()
        if normalized in {"required", "applicable", "pass", "true"}:
            return True
        if normalized in {"not_applicable", "not-applicable", "false"}:
            return False
    if isinstance(value, dict):
        for key in ("applicable", "required"):
            nested = value.get(key)
            if isinstance(nested, bool):
                return nested
        for key in ("status", "applicability"):
            nested = value.get(key)
            if isinstance(nested, str):
                return _is_applicable({dimension: nested}, dimension)
    return None


def _source(*parts: str) -> str:
    return "".join(parts)


class DeterministicContractBatchTest(unittest.TestCase):
    def test_versioned_blackbox_execution_report(self) -> None:
        empty_fixture, _prepared = _create_fixture(self, include_e2e=False)
        self.assertEqual(
            _blackbox_version(load_ledger(empty_fixture.run_path)),
            2,
            "new runtime-created runs must freeze blackbox report version 2",
        )

        schema = json.loads(BLACKBOX_SCHEMA.read_text())
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        failures: list[str] = []

        zero_accepted = _v2_details(
            empty_fixture,
            [_execution("python3 tools/one.py", returncode=1)],
        )
        all_method_error = _v2_details(
            empty_fixture,
            [_method_error("browser", "Browser method unavailable.")],
        )
        for name, details in (
            ("zero-accepted", zero_accepted),
            ("all-method-error", all_method_error),
        ):
            if list(validator.iter_errors(details)):
                failures.append(f"{name}: public schema rejected the frozen report shape")
                continue
            result = _record_details(empty_fixture, details)
            if result.returncode == 0 or result.data.get("status") in {"READY", "PASS", "NOOP"}:
                failures.append(f"{name}: report formed evidence: {result.data!r}")

        contaminated_method_error = _v2_details(
            empty_fixture,
            [
                {
                    "method": "browser",
                    "reason": "Browser method unavailable.",
                    "command": "python3 tools/one.py",
                    "returncode": 0,
                    "timed_out": False,
                }
            ],
        )
        if not list(validator.iter_errors(contaminated_method_error)):
            failures.append("schema accepted a method-error carrying command result fields")
        contaminated_result = _record_details(empty_fixture, contaminated_method_error)
        if (
            contaminated_result.returncode == 0
            or contaminated_result.data.get("code") != "E2E_DETAILS_INVALID"
        ):
            failures.append(
                "runtime did not reject the same contaminated method-error object: "
                f"{contaminated_result.data!r}"
            )

        invented_cases = _v2_details(
            empty_fixture,
            [_execution("python3 tools/one.py")],
            cases=[_passing_case("invented-case")],
        )
        if list(validator.iter_errors(invented_cases)):
            failures.append("schema rejected a structurally valid invented case report")
        invented_result = _record_details(empty_fixture, invented_cases)
        if (
            invented_result.returncode == 0
            or invented_result.data.get("code") != "E2E_CASE_RESULTS_INVALID"
        ):
            failures.append(
                "runtime did not reject non-empty cases without frozen targets: "
                f"{invented_result.data!r}"
            )

        integral_float = _v2_details(
            empty_fixture,
            [_execution("python3 tools/one.py", returncode=0.0)],
            cases=[],
        )
        if list(validator.iter_errors(integral_float)):
            failures.append("schema rejected integral-float returncode 0.0")
        integral_float_result = _record_details(empty_fixture, integral_float)
        if (
            integral_float_result.returncode != 0
            or integral_float_result.data.get("status") != "READY"
        ):
            failures.append(
                "runtime disagreed with Draft 2020-12 integer parity for returncode 0.0: "
                f"{integral_float_result.data!r}"
            )

        _finish_blackbox_turn(empty_fixture)
        omitted_cases = _v2_details(
            empty_fixture,
            [_execution("python3 tools/one.py")],
        )
        if list(validator.iter_errors(omitted_cases)):
            failures.append("schema rejected omitted cases without frozen targets")
        omitted_result = _record_details(empty_fixture, omitted_cases)
        if omitted_result.returncode != 0 or omitted_result.data.get("status") != "READY":
            failures.append(
                "runtime rejected omitted cases without frozen targets: "
                f"{omitted_result.data!r}"
            )

        e2e_fixture, _prepared = _create_fixture(self)
        valid = _v2_details(
            e2e_fixture,
            [_execution("python3 tools/one.py")],
            cases=[_passing_case()],
        )
        whitespace_mutations: list[tuple[str, Callable[[dict[str, Any]], None]]] = [
            ("candidate_worktree", lambda value: value.__setitem__("candidate_worktree", " \t")),
            ("command", lambda value: value["executions"][0].__setitem__("command", " \n")),
            (
                "observation",
                lambda value: value["cases"][0]["mechanical"].__setitem__("observation", "  "),
            ),
        ]
        for name, mutate in whitespace_mutations:
            invalid = copy.deepcopy(valid)
            mutate(invalid)
            if not list(validator.iter_errors(invalid)):
                failures.append(f"schema accepted whitespace-only {name}")
            result = _record_details(e2e_fixture, invalid)
            if result.returncode == 0:
                failures.append(f"runtime accepted whitespace-only {name}")
        invalid_reason = _v2_details(
            e2e_fixture,
            [_method_error("browser", " \t")],
            cases=[_passing_case()],
        )
        if not list(validator.iter_errors(invalid_reason)):
            failures.append("schema accepted whitespace-only method-error reason")
        if _record_details(e2e_fixture, invalid_reason).returncode == 0:
            failures.append("runtime accepted whitespace-only method-error reason")

        accepted = _v2_details(
            e2e_fixture,
            [
                _execution("python3 tools/one.py"),
                _method_error("browser", "Browser method unavailable."),
                _execution("python3 tools/two.py"),
            ],
            cases=[_passing_case()],
        )
        if list(validator.iter_errors(accepted)):
            failures.append("accepted: public schema rejected a valid multi-execution report")
        else:
            recorded = _record_details(e2e_fixture, accepted)
            scope = recorded.data.get("evidence", {}).get("scope")
            if recorded.returncode != 0 or recorded.data.get("status") != "READY":
                failures.append(f"accepted: runtime rejected report: {recorded.data!r}")
            elif sorted(scope or []) != ["tools/one.py", "tools/two.py"]:
                failures.append(f"accepted: incomplete dependency union: {scope!r}")

        _finish_blackbox_turn(e2e_fixture)
        unresolved = _v2_details(
            e2e_fixture,
            [
                _execution("python3 tools/one.py"),
                _execution("opaque-runner --cannot-resolve"),
            ],
            cases=[_passing_case()],
        )
        unresolved_result = _record_details(e2e_fixture, unresolved)
        unresolved_scope = unresolved_result.data.get("evidence", {}).get("scope")
        if unresolved_result.returncode != 0 or unresolved_scope != ["**"]:
            failures.append(
                "unresolved accepted command did not force complete full-tree scope: "
                f"rc={unresolved_result.returncode} scope={unresolved_scope!r}"
            )

        _finish_blackbox_turn(e2e_fixture)
        legacy = _record_legacy_command(e2e_fixture, "python3 tools/one.py")
        if legacy.returncode != 0 or legacy.data.get("status") != "READY":
            failures.append(f"missing-version active ledger did not continue as v1: {legacy.data!r}")

        self.assertEqual(failures, [])

    def test_fail_closed_unittest_target_resolution(self) -> None:
        cases = (
            ("python3 -m unittest tests", ["**"]),
            ("python3 -m unittest package.tests", ["**"]),
            ("python3 -m unittest package/tests", ["**"]),
            ("python3 -m unittest", ["**"]),
            ("python3 -m unittest discover", ["**"]),
            ("python3 -m unittest -v", ["**"]),
            ("python3 -m unittest -q", ["**"]),
            ("python3 -m unittest -k pattern", ["**"]),
            ("python3 -m tools.blackbox_runner", ["**"]),
            ("python3 -B -m unittest tests", ["**"]),
            (
                "python3 -B -m unittest tests/test_widget.py",
                ["tests/test_widget.py"],
            ),
            ("python3 -B -m unittest discover -s tests", ["tests/**"]),
            ("python3 -m pytest", ["**"]),
            ("pytest --maxfail 1", ["**"]),
            ("python3 -m pytest -p package.plugin", ["**"]),
            ("pytest --junitxml report.xml", ["**"]),
            ("pytest tests", ["tests/**"]),
            (
                "pytest -c tests/pytest.ini tests/test_widget.py",
                ["tests/pytest.ini", "tests/test_widget.py"],
            ),
            ("python3 -m unittest tests/test_widget.py", ["tests/test_widget.py"]),
            ("python3 -m unittest discover -s tests", ["tests/**"]),
        )
        observed: list[tuple[str, int, str | None, list[str] | None]] = []
        fixture, _prepared = _create_fixture(self)
        for index, (command, expected_scope) in enumerate(cases):
            if index:
                _finish_blackbox_turn(fixture)
            result = _record_legacy_command(fixture, command)
            scope = result.data.get("evidence", {}).get("scope")
            row = (command, result.returncode, result.data.get("status"), scope)
            if index == 0:
                self.assertEqual(
                    row,
                    (command, 0, "READY", expected_scope),
                    "bare unittest target must independently force full-tree scope",
                )
            observed.append(row)
        expected = [(command, 0, "READY", scope) for command, scope in cases]
        self.assertEqual(observed, expected)

    def test_derived_e2e_case_dimensions(self) -> None:
        e2e_body = """schema_version: 1
cases:
  - id: full-no-rules
    covers: [add-positive]
    input: "inspect full behavior"
    level: full
    verify:
      must: ["The value is correct."]
      must_not: ["An error is emitted."]
    quality:
      criteria: ["The response is clear."]
  - id: full-with-rules
    covers: [add-positive]
    input: "inspect full mechanical behavior"
    level: full
    hard_rules:
      response_contains: ["3"]
    verify:
      must: ["The value is correct."]
      must_not: ["An error is emitted."]
    quality:
      criteria: ["The response is clear."]
  - id: fast-with-rules
    covers: [add-positive]
    input: "inspect fast mechanical behavior"
    level: fast
    hard_rules:
      response_contains: ["3"]
"""
        fixture, prepared = _create_fixture(self, e2e_body=e2e_body)
        contracts = _case_contracts(prepared)
        applicability = {
            case_id: {
                name: _is_applicable(contract, name)
                for name in ("mechanical", "verify", "quality")
            }
            for case_id, contract in contracts.items()
            if case_id in {"full-no-rules", "full-with-rules", "fast-with-rules"}
        }
        self.assertEqual(
            applicability,
            {
                "full-no-rules": {
                    "mechanical": False,
                    "verify": True,
                    "quality": True,
                },
                "full-with-rules": {
                    "mechanical": True,
                    "verify": True,
                    "quality": True,
                },
                "fast-with-rules": {
                    "mechanical": True,
                    "verify": False,
                    "quality": False,
                },
            },
            "prepare-follow-up must derive applicability from the frozen cases only",
        )

        valid_cases = [
            {
                "case_id": "full-no-rules",
                "mechanical": _dimension("not_applicable", "No hard rules are frozen."),
                "verify": _dimension("pass", "The full behavior was observed."),
                "quality": _dimension("pass", "The full response was clear."),
                "outcome": "pass",
            },
            {
                "case_id": "full-with-rules",
                "mechanical": _dimension("pass", "The hard rule matched."),
                "verify": _dimension("pass", "The full behavior was observed."),
                "quality": _dimension("pass", "The full response was clear."),
                "outcome": "pass",
            },
            {
                "case_id": "fast-with-rules",
                "mechanical": _dimension("pass", "The hard rule matched."),
                "verify": _dimension("not_applicable", "Fast verification is not applicable."),
                "quality": _dimension("not_applicable", "Fast quality is not applicable."),
                "outcome": "pass",
            },
        ]
        base = _v2_details(
            fixture,
            [_execution("python3 tools/one.py")],
            cases=valid_cases,
        )
        failures: list[str] = []
        mutations: list[tuple[str, Callable[[dict[str, Any]], None]]] = [
            (
                "full-no-rules-mechanical",
                lambda value: value["cases"][0].__setitem__(
                    "mechanical", _dimension("pass", "Improper mechanical pass.")
                ),
            ),
            (
                "fast-verify",
                lambda value: value["cases"][2].__setitem__(
                    "verify", _dimension("pass", "Improper fast verification pass.")
                ),
            ),
            (
                "blank-observation",
                lambda value: value["cases"][1]["verify"].__setitem__("observation", " \t"),
            ),
            (
                "fail-coerced-to-pass",
                lambda value: value["cases"][1].update(
                    verify=_dimension("fail", "The behavior failed."), outcome="pass"
                ),
            ),
            (
                "failed-outcome-recorded",
                lambda value: value["cases"][1].update(
                    verify=_dimension("fail", "The behavior failed."), outcome="fail"
                ),
            ),
        ]
        for name, mutate in mutations:
            invalid = copy.deepcopy(base)
            mutate(invalid)
            result = _record_details(fixture, invalid)
            if result.returncode == 0:
                failures.append(f"{name}: invalid dimensions formed evidence")
        accepted = _record_details(fixture, base)
        if accepted.returncode != 0 or accepted.data.get("status") != "READY":
            failures.append(f"valid derived dimensions were rejected: {accepted.data!r}")
        self.assertEqual(failures, [])

    def test_python_cli_bytecode_isolation(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="builder-loop-bytecode-contract-"))
        self.addCleanup(shutil.rmtree, root, True)
        checkout = root / "checkout"
        cloned = subprocess.run(
            ["git", "clone", "-q", "--shared", str(ROOT), str(checkout)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(cloned.returncode, 0, cloned.stderr)

        env = os.environ.copy()
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        env.pop("PYTHONPYCACHEPREFIX", None)
        before = sorted(path.relative_to(checkout) for path in checkout.rglob("*.pyc"))
        invoked = subprocess.run(
            [sys.executable, str(STABLE_CLI), "--help"],
            cwd=checkout,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        after = sorted(path.relative_to(checkout) for path in checkout.rglob("*.pyc"))
        self.assertEqual(invoked.returncode, 0, invoked.stderr)
        self.assertEqual(
            after,
            before,
            "stable CLI must disable bytecode before importing runtime modules",
        )

        runtime_init = checkout / "runtime" / "codex_builder_loop" / "__init__.py"
        compiled = subprocess.run(
            [sys.executable, "-m", "py_compile", str(runtime_init)],
            cwd=checkout,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        explicit_residue = sorted(path.relative_to(checkout) for path in checkout.rglob("*.pyc"))
        self.assertNotEqual(explicit_residue, before, "explicit py_compile is outside the CLI guarantee")

        sentinel = next(checkout.rglob("*.pyc"))
        sentinel_bytes = sentinel.read_bytes()
        repeated = subprocess.run(
            [sys.executable, str(STABLE_CLI), "--help"],
            cwd=checkout,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(sentinel.read_bytes(), sentinel_bytes, "pre-existing residue must be preserved")

    def test_syntax_aware_test_weakening_detection(self) -> None:
        marker = _source("pytest", ".mark", ".skip")
        xfail = _source("x", "fail")
        sources = (
            (
                "pytest-module-alias",
                _source(
                    "import pytest as pt\n",
                    "@pt.mark.",
                    "skip(reason='disabled')\n",
                    "from src.calc import add\n\n",
                    "def test_value():\n    assert add(1, 2) == 3\n",
                ),
                "NEEDS_USER",
            ),
            (
                "unittest-module-alias",
                _source(
                    "import unittest as ut\n",
                    "@ut.",
                    "skip('disabled')\n",
                    "from src.calc import add\n\n",
                    "def test_value():\n    assert add(1, 2) == 3\n",
                ),
                "NEEDS_USER",
            ),
            (
                "subprocess-module-alias",
                _source(
                    "import subprocess as sp\nfrom src.calc import add\n\n",
                    "def test_value():\n",
                    "    sp.run(['python3', '-m', 'pytest', 'tests'])\n",
                    "    assert add(1, 2) == 3\n",
                ),
                "NEEDS_USER",
            ),
            (
                "subprocess-import-alias",
                _source(
                    "from subprocess import run as execute\nfrom src.calc import add\n\n",
                    "def test_value():\n",
                    "    execute(['python3', '-m', 'pytest', 'tests'])\n",
                    "    assert add(1, 2) == 3\n",
                ),
                "NEEDS_USER",
            ),
            (
                "bare-pytestmark-container",
                _source(
                    "from pytest import mark\n",
                    "pytest", "mark = (mark.", "skip(reason='disabled'), mark.", xfail,
                    "(reason='disabled'), mark.", "fla", "ky())\n",
                    "from src.calc import add\n\n",
                    "def test_value():\n    assert add(1, 2) == 3\n",
                ),
                "NEEDS_USER",
            ),
            (
                "module-shadow",
                _source(
                    "class Local:\n    class mark:\n        skip = 'local'\n",
                    "pytest = Local()\nfrom src.calc import add\n\n",
                    "def test_value():\n",
                    "    assert pytest", ".mark", ".skip == 'local' and add(1, 2) == 3\n",
                ),
                "READY",
            ),
            (
                "local-parameter-shadow",
                _source(
                    "from src.calc import add\n\n",
                    "def helper(pytest):\n    return pytest", ".mark", ".skip\n\n",
                    "def test_value():\n    assert add(1, 2) == 3\n",
                ),
                "READY",
            ),
            (
                "container-shadow",
                _source(
                    "class Holder:\n    pass\n",
                    "holder = Holder()\nholder.pytest = Holder()\nholder.pytest.mark = Holder()\n",
                    "holder.pytest", ".mark", ".skip = 'local'\nfrom src.calc import add\n\n",
                    "def test_value():\n    assert add(1, 2) == 3\n",
                ),
                "READY",
            ),
            (
                "comments-and-strings",
                _source(
                    "# ", marker, "(reason='comment only')\n",
                    "TEXT = ", repr(marker + "(reason='string only')"), "\n",
                    "from src.calc import add\n\n",
                    "def test_value():\n    assert TEXT and add(1, 2) == 3\n",
                ),
                "READY",
            ),
        )
        observed: list[tuple[str, str | None, list[str]]] = []
        for index, (name, source, expected_status) in enumerate(sources):
            repo = init_repo()
            self.addCleanup(cleanup_repo, repo)
            plan = write_plan(repo, plan_markdown(head(repo)))
            started, run_path = start_run(repo, plan)
            _builder, tester = worktrees_from(started, run_path)
            (tester / "tests" / "test_calc.py").write_text(source)
            commit_all(tester, "author weakening detector fixture")
            result = run_cli("role-check", "--run", run_path, "--role", "tester")
            reward = [
                str(item.get("reason", ""))
                for item in result.data.get("violations", [])
                if item.get("kind") == "reward_hacking"
            ]
            row = (name, result.data.get("status"), reward)
            if index == 0:
                self.assertEqual(
                    result.data.get("status"),
                    expected_status,
                    "a real pytest module alias must be rejected",
                )
                self.assertTrue(reward, result.data)
            observed.append(row)

        failures = []
        for (name, _source_text, expected_status), (_name, status, reward) in zip(sources, observed):
            if status != expected_status:
                failures.append(f"{name}: status={status!r}, expected={expected_status!r}")
            if expected_status == "NEEDS_USER" and not reward:
                failures.append(f"{name}: no reward_hacking finding")
            if expected_status == "READY" and reward:
                failures.append(f"{name}: shadow/comment/string was rejected: {reward!r}")

        control_repo = init_repo({"pytest.ini": "[pytest]\n"})
        self.addCleanup(cleanup_repo, control_repo)
        control_head = head(control_repo)
        for runner in (
            "python3 -m pytest",
            "python3 -B -m pytest",
            "python3.11 -I -m pytest",
        ):
            result = run_cli(
                "plan-validate",
                "--repo",
                control_repo,
                input_text=plan_markdown(
                    control_head,
                    runner=runner,
                    builder_write=["src/**", "pytest.ini"],
                ),
            )
            actual = (
                result.returncode,
                result.data.get("status"),
                result.data.get("code"),
                result.data.get("path"),
            )
            expected = (1, "NEEDS_USER", "RUNNER_CONTROL_OWNED", "pytest.ini")
            if actual != expected:
                failures.append(
                    f"runner-control ownership mismatch for {runner!r}: "
                    f"actual={actual!r} expected={expected!r}"
                )
        self.assertEqual(failures, [])

    def test_stable_frozen_plan_identity(self) -> None:
        repo = init_repo()
        self.addCleanup(cleanup_repo, repo)
        base = plan_markdown(head(repo))
        header = (
            "[保质期: run 完成, owner: builder-loop, 正向归宿: "
            ".builder-loop/codex/runs/frozen/ledger.json]\r\n"
        )
        managed = header + base.replace("\n", "\r\n").rstrip("\r\n")
        base_plan = write_plan(repo, base, "base.md")
        managed_plan = write_plan(repo, managed, "managed.md")
        base_result = run_cli("plan-validate", "--repo", repo, "--plan", base_plan)
        managed_result = run_cli("plan-validate", "--repo", repo, "--plan", managed_plan)
        assert_status(base_result, "READY", rc=0)
        assert_status(managed_result, "READY", rc=0)

        base_contract = base_result.data.get("contract", {})
        managed_contract = managed_result.data.get("contract", {})
        base_raw = hashlib.sha256(base_plan.read_bytes()).hexdigest()
        managed_raw = hashlib.sha256(managed_plan.read_bytes()).hexdigest()
        self.assertEqual(
            (
                base_contract.get("digest_kind"),
                managed_contract.get("digest_kind"),
                base_contract.get("sha256"),
                managed_contract.get("sha256"),
                base_contract.get("raw_sha256"),
                managed_contract.get("raw_sha256"),
            ),
            (
                "canonical-v2",
                "canonical-v2",
                base_contract.get("sha256"),
                base_contract.get("sha256"),
                base_raw,
                managed_raw,
            ),
            "managed header, line endings and terminal newline must share canonical identity",
        )
        self.assertNotEqual(base_raw, managed_raw)

        changed = base.replace("Implement and independently verify", "Implement and carefully verify", 1)
        changed_plan = write_plan(repo, changed, "changed.md")
        changed_result = run_cli("plan-validate", "--repo", repo, "--plan", changed_plan)
        assert_status(changed_result, "READY", rc=0)
        self.assertNotEqual(
            changed_result.data.get("contract", {}).get("sha256"),
            base_contract.get("sha256"),
            "non-lifecycle prose changes must alter canonical identity",
        )

        started, run_path = start_run(repo, base_plan)
        ledger_path = run_path / "ledger.json"
        ledger = load_ledger(run_path)
        plan_record = ledger["plan"]
        plan_record.pop("digest_kind", None)
        plan_record.pop("raw_sha256", None)
        plan_record["sha256"] = base_raw
        ledger_path.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n")
        continued = run_cli("status", "--run", run_path)
        self.assertEqual(continued.returncode, 0, continued.data)
        self.assertEqual(load_ledger(run_path)["plan"]["sha256"], base_raw)
        self.assertEqual(started.data.get("plan_sha256"), base_contract.get("sha256"))

    def test_reviewer_terminal_contract_continuity(self) -> None:
        invalid_repo = init_repo()
        self.addCleanup(cleanup_repo, invalid_repo)
        invalid_plan = write_plan(invalid_repo, l1_plan_markdown(head(invalid_repo)))
        invalid_started, invalid_run = start_run(invalid_repo, invalid_plan)
        invalid_builder, _tester = worktrees_from(invalid_started, invalid_run)
        (invalid_builder / "README.md").write_text("reviewable documentation\n")
        commit_all(invalid_builder, "prepare reviewer terminal fixture")
        assert_status(
            run_cli("role-check", "--run", invalid_run, "--role", "builder"),
            "READY",
            rc=0,
        )

        reviewer_id, reviewer_turn = start_agent_turn(
            invalid_run, "reviewer", agent_id="terminal-contract-reviewer"
        )
        invalid_ledger = load_ledger(invalid_run)
        invalid = run_cli(
            "agent-event",
            "--repo",
            invalid_repo,
            "--session-id",
            str(invalid_ledger["owner_session_id"]),
            "--role",
            "reviewer",
            "--agent-id",
            reviewer_id,
            "--turn-id",
            reviewer_turn,
            "--event",
            "idle",
            "--result",
            "fail",
            env={"BUILDER_LOOP_HOOK_EVENT": "1"},
        )
        self.assertNotEqual(
            invalid.returncode,
            0,
            "Reviewer must reject the parent-defined fail terminal",
        )

        repo = init_repo()
        self.addCleanup(cleanup_repo, repo)
        plan = write_plan(repo, l1_plan_markdown(head(repo)))
        started, run_path = start_run(repo, plan)
        builder, _tester = worktrees_from(started, run_path)
        (builder / "README.md").write_text("reviewable documentation\n")
        commit_all(builder, "prepare reviewer continuity fixture")
        assert_status(
            run_cli("role-check", "--run", run_path, "--role", "builder"),
            "READY",
            rc=0,
        )
        ledger = load_ledger(run_path)
        findings_id, findings_turn = start_agent_turn(
            run_path, "reviewer", agent_id="continuity-reviewer"
        )
        findings = run_cli(
            "agent-event",
            "--repo",
            repo,
            "--session-id",
            str(ledger["owner_session_id"]),
            "--role",
            "reviewer",
            "--agent-id",
            findings_id,
            "--turn-id",
            findings_turn,
            "--event",
            "idle",
            "--result",
            "findings",
            env={"BUILDER_LOOP_HOOK_EVENT": "1"},
        )
        assert_status(findings, "READY", rc=0)
        assert_status(
            record_problems(
                run_path,
                source="reviewer",
                source_id=findings_turn,
                manifest=problem_report(
                    {
                        "key": "reviewer-continuity-finding",
                        "summary": "Reviewer requested follow-up",
                        "details": "The deterministic Reviewer fixture has one outstanding finding.",
                        "owner": "builder",
                    }
                ),
            ),
            "READY",
            rc=0,
        )
        prepared = run_cli(
            "prepare-follow-up",
            "--run",
            run_path,
            "--role",
            "reviewer",
            "--agent-id",
            findings_id,
            "--purpose",
            "review",
        )
        assert_status(prepared, "READY", rc=0)
        replacement = run_cli(
            "prepare-follow-up",
            "--run",
            run_path,
            "--role",
            "reviewer",
            "--agent-id",
            "replacement-reviewer",
            "--purpose",
            "review",
        )
        self.assertNotEqual(replacement.returncode, 0, replacement.data)

        builder_text = BUILDER_SKILL.read_text().lower()
        reviewer_text = REVIEWER_AGENT.read_text().lower()
        failures = []
        for terminal in ("pass", "findings", "blocked"):
            if f"review_result: {terminal}" not in reviewer_text:
                failures.append(f"Reviewer custom agent missing terminal {terminal}")
            if f"review_result: {terminal}" not in builder_text:
                failures.append(f"Builder review flow does not consume terminal {terminal}")
        if "review_result: fail" in builder_text or "review_result: fail" in reviewer_text:
            failures.append("parent-defined Reviewer fail terminal is exposed")
        if "follow-up 同一 reviewer thread" not in builder_text:
            failures.append("Builder does not require same-thread re-review")
        if "不新建 reviewer" not in builder_text:
            failures.append("Builder allows replacement Reviewer")
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
