from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from harness import (
    CLI,
    assert_status,
    cleanup_repo,
    commit_all,
    finish_agent_turn,
    head,
    load_ledger,
    run_cli,
    run_process,
    start_agent_turn,
)
from proof_harness import (
    DEFAULT_BASELINE,
    DEFAULT_CANDIDATE,
    PYTEST_ID,
    UNITTEST_ID,
    baseline_group,
    create_proof_fixture,
    mutation_group,
    prove,
    pytest_source,
    unittest_source,
)


def assert_rejected(test: unittest.TestCase, result) -> None:
    test.assertNotEqual(result.data.get("status"), "READY", result.data)
    test.assertNotEqual(result.returncode, 0, result.data)
    test.assertIsInstance(result.data.get("code"), str, result.data)
    test.assertIsInstance(result.data.get("message"), str, result.data)
    test.assertNotIn("evidence", result.data, result.data)
    test.assertNotIn("Traceback", result.stderr)


def assert_spec_rejected(test: unittest.TestCase, result) -> None:
    assert_rejected(test, result)
    test.assertEqual(result.returncode, 1, result.data)
    test.assertEqual(result.data.get("status"), "NEEDS_USER", result.data)
    test.assertEqual(result.data.get("code"), "TEST_PROOF_SPEC_INVALID", result.data)
    test.assertTrue(result.data.get("errors"), result.data)
    test.assertNotIn("result", result.data, result.data)
    test.assertNotIn("executable_identity", result.data, result.data)


def assert_ready_wrapper_proof(
    test: unittest.TestCase,
    result,
    *,
    framework: str,
    test_id: str,
) -> dict:
    test.assertEqual(result.returncode, 0, result.data)
    test.assertEqual(result.data.get("status"), "READY", result.data)
    test.assertIn("evidence", result.data, result.data)
    group = result.data["groups"][0]
    test.assertEqual(group["framework"], framework)
    test.assertEqual(group["test_ids"], [test_id])
    test.assertEqual(
        group["baseline"]["test_result"]["matched_test_ids"],
        [test_id],
    )
    test.assertEqual(group["candidate"]["test_result"]["classification"], "pass")
    test.assertIn(
        group["executable_identity"]["kind"],
        {"trusted-system-launcher", "absolute-launcher"},
    )
    test.assertIn(
        "verify.sh",
        [item["path"] for item in group["executable_identity"]["repository_paths"]],
    )
    return group


def failure_counts(result) -> dict:
    payload = result.data.get("result")
    if not isinstance(payload, dict):
        raise AssertionError(f"proof failure omitted structured result: {result.data!r}")
    test_result = payload.get("test_result")
    if not isinstance(test_result, dict):
        raise AssertionError(f"proof failure omitted test_result: {result.data!r}")
    counts = test_result.get("counts")
    if not isinstance(counts, dict):
        raise AssertionError(f"proof failure omitted counts: {result.data!r}")
    return counts


class ProveTestsContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repos: list[Path] = []

    def tearDown(self) -> None:
        for repo in self.repos:
            cleanup_repo(repo)

    def fixture(self, **kwargs):
        value = create_proof_fixture(**kwargs)
        self.repos.append(value.repo)
        return value

    def test_public_cli_exposes_proof_but_not_internal_supervisor(self) -> None:
        help_result = run_process([sys.executable, CLI, "prove-tests", "--help"])
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        output = help_result.stdout + help_result.stderr
        for option in ("--repo", "--run", "--spec"):
            self.assertIn(option, output)
        self.assertNotIn("supervisor", output.lower())

        top_result = run_process([sys.executable, CLI, "--help"])
        self.assertEqual(top_result.returncode, 0, top_result.stderr)
        top = top_result.stdout + top_result.stderr
        self.assertIn("prove-tests", top)
        self.assertNotIn("supervisor", top.lower())

    def test_unittest_baseline_red_binds_full_id_and_tester_source(self) -> None:
        fixture = self.fixture(explicit_run_id="A.b_C-1")
        result = prove(fixture, baseline_group(timeout_seconds=30.0))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.data.get("status"), "READY", result.data)
        self.assertEqual(result.data.get("run_id"), fixture.run_id)
        self.assertEqual(result.data.get("head"), fixture.integrated_head)
        self.assertEqual(
            result.data.get("test_effectiveness_head"), fixture.integrated_head
        )
        self.assertEqual(
            result.data.get("tester_source_head"), fixture.tester_author_head
        )
        self.assertRegex(str(result.data.get("tester_manifest_sha256")), r"^[0-9a-f]{64}$")
        group = result.data["groups"][0]
        self.assertEqual(group["test_ids"], [UNITTEST_ID])
        self.assertEqual(group["framework"], "unittest")
        self.assertEqual(group["executable_identity"]["kind"], "trusted-python")
        self.assertEqual(
            group["baseline"]["test_result"]["matched_test_ids"], [UNITTEST_ID]
        )

    def test_mutation_proof_records_real_patch_and_rejects_test_source_mutation(self) -> None:
        fixture = self.fixture()
        source_patch = (
            "diff --git a/src/calc.py b/src/calc.py\n"
            "--- a/src/calc.py\n"
            "+++ b/src/calc.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def add(a, b):\n"
            "-    return a + b\n"
            "+    return a + b - 1\n"
        )
        result = prove(fixture, mutation_group(source_patch))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.data.get("status"), "READY", result.data)
        mutation = result.data["groups"][0]["mutation"]
        self.assertEqual(mutation["changed_paths"], ["src/calc.py"])
        self.assertEqual(mutation["head_before"], mutation["head_after"])
        self.assertRegex(mutation["patch_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(mutation["applied_diff_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(mutation["returncode"], 0)
        self.assertEqual(
            mutation["test_result"]["classification"], "assertion-failure"
        )
        self.assertEqual(
            mutation["test_result"]["matched_test_ids"], [UNITTEST_ID]
        )

        test_patch = (
            "diff --git a/tests/test_proof_target.py b/tests/test_proof_target.py\n"
            "--- a/tests/test_proof_target.py\n"
            "+++ b/tests/test_proof_target.py\n"
            "@@ -4,2 +4,2 @@ class ProofTargetTest(unittest.TestCase):\n"
            "     def test_add(self):\n"
            "-        self.assertEqual(add(1, 2), 3)\n"
            "+        self.assertEqual(add(1, 2), 2)\n"
        )
        assert_rejected(self, prove(fixture, mutation_group(test_patch)))

    def test_source_head_or_blob_drift_invalidates_existing_proof(self) -> None:
        fixture = self.fixture()
        accepted = prove(fixture, baseline_group())
        self.assertEqual(accepted.data.get("status"), "READY", accepted.data)
        old_manifest = accepted.data["tester_manifest_sha256"]

        tester_file = fixture.tester / "tests" / "test_proof_target.py"
        tester_file.write_text(tester_file.read_text() + "\n# post-author drift\n")
        correction_head = commit_all(fixture.tester, "correct tester source")
        self.assertNotEqual(correction_head, fixture.tester_author_head)

        noop = prove(fixture, baseline_group())
        assert_status(noop, "NOOP", rc=0)
        self.assertEqual(
            load_ledger(fixture.run_path)["tester_integration"]["source_head"],
            fixture.tester_author_head,
        )

        prepared = run_cli(
            "prepare-follow-up",
            "--run",
            fixture.run_path,
            "--role",
            "tester",
            "--agent-id",
            fixture.tester_agent_id,
            "--purpose",
            "author",
        )
        assert_status(prepared, "READY", rc=0)
        agent_id, turn_id = start_agent_turn(
            fixture.run_path, "tester", agent_id=fixture.tester_agent_id
        )
        finish_agent_turn(
            fixture.run_path,
            "tester",
            agent_id=agent_id,
            turn_id=turn_id,
            result="tests_ready",
        )
        integrated = run_cli("integrate-tests", "--run", fixture.run_path)
        assert_status(integrated, "READY", rc=0)
        new_integrated_head = head(fixture.builder)
        self.assertNotEqual(new_integrated_head, fixture.integrated_head)
        after_integration = load_ledger(fixture.run_path)
        self.assertIsNone(
            after_integration.get("evidence", {}).get("test_effectiveness")
        )

        renewed = prove(fixture, baseline_group())
        assert_status(renewed, "READY", rc=0)
        self.assertEqual(renewed.data["head"], new_integrated_head)
        self.assertEqual(renewed.data["tester_source_head"], correction_head)
        self.assertNotEqual(renewed.data["tester_manifest_sha256"], old_manifest)

        verified = run_cli("verify", "--run", fixture.run_path)
        assert_status(verified, "PASS", rc=0)
        final_status = run_cli("status", "--run", fixture.run_path)
        self.assertEqual(final_status.data.get("verified_head"), new_integrated_head)
        self.assertEqual(
            final_status.data.get("test_effectiveness_head"), new_integrated_head
        )

    def test_only_tester_manifest_tests_and_frozen_wrappers_are_accepted(self) -> None:
        fixture = self.fixture(
            test_files={
                "tests/test_proof_target.py": unittest_source(),
                "tests/run-proof.sh": "#!/usr/bin/env bash\nexec python3 -m unittest \"$@\"\n",
            }
        )
        with tempfile.TemporaryDirectory(prefix="proof-outside-") as tmp:
            outside = Path(tmp) / "test_outside.py"
            outside.write_text(
                "from pathlib import Path\n\n"
                "def test_outside():\n"
                "    assert Path(__file__).is_file()\n"
            )
            cases = (
                baseline_group(
                    argv=["python3", "-m", "pytest", str(outside)],
                    test_ids=[f"{outside}::test_outside"],
                ),
                baseline_group(
                    argv=["python3", "-m", "pytest", "tests/../test_outside.py"],
                    test_ids=["tests/../test_outside.py::test_outside"],
                ),
                baseline_group(
                    argv=["bash", "tests/run-proof.sh", UNITTEST_ID],
                ),
                baseline_group(
                    argv=[
                        "bash",
                        "-c",
                        f"python3 -m unittest {UNITTEST_ID} > proof.log",
                    ],
                ),
            )
            for group in cases:
                with self.subTest(argv=group["argv"]):
                    assert_rejected(self, prove(fixture, group))

            ordinary_function = prove(
                fixture,
                baseline_group(
                    argv=[
                        "python3",
                        "-m",
                        "unittest",
                        "tests.test_calc.test_add",
                    ],
                    test_ids=["tests.test_calc.test_add"],
                ),
            )
            assert_rejected(self, ordinary_function)
            self.assertEqual(
                ordinary_function.data.get("code"), "TEST_PROOF_CANDIDATE_FAILED"
            )
            self.assertEqual(
                ordinary_function.data["result"]["test_result"]["classification"],
                "unclassified-failure",
            )
            self.assertIn("Traceback", ordinary_function.data["result"]["log_tail"])

    def test_full_ids_prevent_same_name_extra_and_unexecuted_tests(self) -> None:
        source = (
            "import unittest\n"
            "from src.calc import add\n\n"
            "class Declared(unittest.TestCase):\n"
            "    def test_same(self):\n"
            "        self.assertEqual(add(1, 2), 3)\n\n"
            "class Other(unittest.TestCase):\n"
            "    def test_same(self):\n"
            "        self.assertEqual(add(2, 3), 5)\n"
        )
        declared = "tests.test_collision.Declared.test_same"
        other = "tests.test_collision.Other.test_same"
        fixture = self.fixture(test_files={"tests/test_collision.py": source})

        exact = prove(
            fixture,
            baseline_group(
                argv=["python3", "-m", "unittest", declared],
                test_ids=[declared],
            ),
        )
        self.assertEqual(exact.data.get("status"), "READY", exact.data)

        module_run = baseline_group(
            argv=["python3", "-m", "unittest", "tests.test_collision"],
            test_ids=[declared],
        )
        assert_rejected(self, prove(fixture, module_run))

        unexecuted = baseline_group(
            argv=["python3", "-m", "unittest", declared],
            test_ids=[declared, other],
        )
        assert_rejected(self, prove(fixture, unexecuted))

        unmapped_source = (
            "import sys\n"
            "import unittest\n"
            "from src.calc import add\n\n"
            "class Declared(unittest.TestCase):\n"
            "    def test_same(self):\n"
            "        self.assertEqual(add(1, 2), 3)\n\n"
            "class Other(unittest.TestCase):\n"
            "    def test_same(self):\n"
            "        if 'tests.test_collision' in sys.argv:\n"
            "            self.fail('undeclared assertion failure')\n"
            "        self.assertEqual(add(2, 3), 5)\n"
        )
        unmapped_fixture = self.fixture(
            test_files={"tests/test_collision.py": unmapped_source}
        )
        unmapped = prove(unmapped_fixture, module_run)
        assert_rejected(self, unmapped)
        payload = unmapped.data.get("result")
        if isinstance(payload, dict):
            self.assertEqual(
                payload["test_result"]["classification"],
                "unmapped-assertion-failure",
            )
        else:
            self.assertEqual(unmapped.data.get("code"), "TEST_PROOF_SPEC_INVALID")
            self.assertTrue(unmapped.data.get("errors"), unmapped.data)
            self.assertIn("frozen requirements", unmapped.data["message"].lower())

    def test_unittest_skip_stdout_exception_text_and_mixed_subtests_fail_closed(self) -> None:
        cases = {
            "skip": unittest_source('self.skipTest("not evidence")'),
            "stdout": unittest_source(
                "import sys\n"
                f"if {UNITTEST_ID!r} in sys.argv:\n"
                '    print("OK\\nRan 1 test")\n'
                '    raise RuntimeError("candidate crashed")\n'
                "self.assertEqual(add(1, 2), 3)"
            ),
        }
        for name, source in cases.items():
            with self.subTest(case=name):
                fixture = self.fixture(test_files={"tests/test_proof_target.py": source})
                assert_rejected(self, prove(fixture, baseline_group()))

        mode_baseline = DEFAULT_BASELINE + "\ndef mode():\n    return 'baseline'\n"
        mode_candidate = DEFAULT_CANDIDATE + "\ndef mode():\n    return 'candidate'\n"
        exception_source = (
            "import unittest\n"
            "from src.calc import mode\n\n"
            "class ProofTargetTest(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        if mode() == 'baseline':\n"
            "            raise RuntimeError('AssertionError: forged text')\n"
            "        self.assertEqual(mode(), 'candidate')\n"
        )
        exception_fixture = self.fixture(
            test_files={"tests/test_proof_target.py": exception_source},
            baseline_source=mode_baseline,
            candidate_source=mode_candidate,
        )
        exception = prove(exception_fixture, baseline_group())
        assert_rejected(self, exception)
        self.assertEqual(failure_counts(exception).get("errors"), 1)
        self.assertEqual(failure_counts(exception).get("failures", 0), 0)

        mixed_source = (
            "import unittest\n"
            "from src.calc import mode\n\n"
            "class ProofTargetTest(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        if mode() != 'baseline':\n"
            "            self.assertEqual(mode(), 'candidate')\n"
            "            return\n"
            "        with self.subTest(kind='failure'):\n"
            "            self.assertEqual(1, 2)\n"
            "        with self.subTest(kind='error'):\n"
            "            raise RuntimeError('boom')\n"
        )
        mixed_fixture = self.fixture(
            test_files={"tests/test_proof_target.py": mixed_source},
            baseline_source=mode_baseline,
            candidate_source=mode_candidate,
        )
        mixed = prove(mixed_fixture, baseline_group())
        assert_rejected(self, mixed)
        self.assertEqual(failure_counts(mixed).get("errors"), 1)
        self.assertEqual(failure_counts(mixed).get("failures"), 1)

    def test_pytest_full_id_xfail_xpass_deselect_and_teardown_skip(self) -> None:
        positive = self.fixture(test_files={"tests/test_pyproof.py": pytest_source()})
        ready = prove(
            positive,
            baseline_group(
                argv=["python3", "-m", "pytest", PYTEST_ID],
                test_ids=[PYTEST_ID],
            ),
        )
        self.assertEqual(ready.data.get("status"), "READY", ready.data)
        self.assertEqual(ready.data["groups"][0]["framework"], "pytest")

        xfail_mark = "getattr(pytest.mark, 'x' + 'fail')"
        cases = {
            "xfail": (
                "import os\n"
                "import pytest\n"
                "from src.calc import add\n"
                f"pytestmark = {xfail_mark}(reason='not a pass')\n"
                "def test_add():\n"
                "    if 'PYTEST_CURRENT_TEST' in os.environ:\n"
                "        assert add(1, 2) == 4\n"
                "    assert add(1, 2) == 3\n"
            ),
            "xpass": (
                "import pytest\n"
                "from src.calc import add\n"
                f"pytestmark = {xfail_mark}(strict=True, reason='unexpected pass')\n"
                "def test_add():\n"
                "    assert add(1, 2) == 3\n"
            ),
            "teardown-skip": (
                "import os\n"
                "import pytest\n"
                "from src.calc import add\n"
                "@pytest.fixture\n"
                "def late_skip():\n"
                "    yield\n"
                "    getattr(pytest, 'sk' + 'ip')('teardown skip')\n"
                "def test_add(late_skip):\n"
                "    assert add(1, 2) == 3\n"
            ),
        }
        for name, source in cases.items():
            with self.subTest(case=name):
                fixture_kwargs = {}
                if name == "teardown-skip":
                    fixture_kwargs["initial_files"] = {
                        "verify.sh": (
                            "#!/usr/bin/env bash\n"
                            "set -euo pipefail\n"
                            "python3 - <<'PY'\n"
                            "from src.calc import add\n"
                            "assert add(1, 2) == 3\n"
                            "PY\n"
                        )
                    }
                fixture = self.fixture(
                    test_files={"tests/test_pyproof.py": source}, **fixture_kwargs
                )
                result = prove(
                    fixture,
                    baseline_group(
                        argv=["python3", "-m", "pytest", PYTEST_ID],
                        test_ids=[PYTEST_ID],
                    ),
                )
                assert_rejected(self, result)

        deselected = self.fixture(
            test_files={"tests/test_pyproof.py": pytest_source()}
        )
        result = prove(
            deselected,
            baseline_group(
                argv=[
                    "python3",
                    "-m",
                    "pytest",
                    PYTEST_ID,
                    "-k",
                    "never_matches",
                ],
                test_ids=[PYTEST_ID],
            ),
        )
        assert_rejected(self, result)

    def test_pytest_full_ids_reject_same_name_and_undeclared_execution(self) -> None:
        source = (
            "from src.calc import add\n\n"
            "class TestDeclared:\n"
            "    def test_same(self):\n"
            "        assert add(1, 2) == 3\n\n"
            "class TestOther:\n"
            "    def test_same(self):\n"
            "        assert add(2, 3) == 5\n"
        )
        declared = "tests/test_collision_pytest.py::TestDeclared::test_same"
        exact_fixture = self.fixture(
            test_files={"tests/test_collision_pytest.py": source}
        )
        exact = prove(
            exact_fixture,
            baseline_group(
                argv=["python3", "-m", "pytest", declared],
                test_ids=[declared],
            ),
        )
        self.assertEqual(exact.data.get("status"), "READY", exact.data)

        collision_fixture = self.fixture(
            test_files={"tests/test_collision_pytest.py": source}
        )
        collision = prove(
            collision_fixture,
            baseline_group(
                argv=[
                    "python3",
                    "-m",
                    "pytest",
                    "tests/test_collision_pytest.py",
                ],
                test_ids=[declared],
            ),
        )
        assert_rejected(self, collision)

    def test_atexit_and_exception_text_cannot_forge_pytest_assertion_events(self) -> None:
        baseline = DEFAULT_BASELINE + "\ndef mode():\n    return 'baseline'\n"
        candidate = DEFAULT_CANDIDATE + "\ndef mode():\n    return 'candidate'\n"
        source = (
            "import atexit\n"
            "import os\n"
            "from src.calc import mode\n\n"
            "def test_add():\n"
            "    if 'PYTEST_CURRENT_TEST' in os.environ and mode() == 'baseline':\n"
            "        atexit.register(lambda: print('1 failed, AssertionError: forged'))\n"
            "        raise RuntimeError('AssertionError: forged')\n"
            "    assert mode() == 'candidate'\n"
        )
        fixture = self.fixture(
            test_files={"tests/test_pyproof.py": source},
            baseline_source=baseline,
            candidate_source=candidate,
        )
        result = prove(
            fixture,
            baseline_group(
                argv=["python3", "-m", "pytest", PYTEST_ID],
                test_ids=[PYTEST_ID],
            ),
        )
        assert_rejected(self, result)
        self.assertEqual(
            result.data["result"]["test_result"]["classification"],
            "non-assertion-test-failure",
        )
        self.assertEqual(failure_counts(result).get("failed"), 1)

    def test_hostile_path_pythonpath_and_pytest_injection_are_ignored(self) -> None:
        wrapper = "#!/usr/bin/env bash\nset -euo pipefail\nexec \"$@\"\n"
        with tempfile.TemporaryDirectory(prefix="proof-hostile-") as tmp:
            hostile = Path(tmp)
            marker = hostile / "used"
            for executable in ("python3", "bash", "pytest"):
                shim = hostile / executable
                shim.write_text(
                    "#!/bin/sh\n"
                    f"printf '%s' {executable} >> {marker}\n"
                    "exit 97\n"
                )
                shim.chmod(0o755)
            (hostile / "poison_import.py").write_text("POISONED = True\n")
            (hostile / "poison_plugin.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('plugin')\n"
            )
            env = {
                "PATH": f"{hostile}:{os.environ.get('PATH', '')}",
                "PYTHONPATH": str(hostile),
                "PYTEST_ADDOPTS": f"--deselect={PYTEST_ID}",
                "PYTEST_PLUGINS": "poison_plugin",
            }
            guarded_unittest = unittest_source(
                "import importlib.util\n"
                "self.assertIsNone(importlib.util.find_spec('poison_import'))\n"
                "self.assertEqual(add(1, 2), 3)"
            )
            fixture = self.fixture(
                test_files={"tests/test_proof_target.py": guarded_unittest},
                initial_files={"verify.sh": wrapper},
                runner=f"bash verify.sh python3 -m unittest {UNITTEST_ID}",
            )
            result = prove(
                fixture,
                baseline_group(
                    argv=[
                        "bash",
                        "verify.sh",
                        "python3",
                        "-m",
                        "unittest",
                        UNITTEST_ID,
                    ],
                    test_ids=[UNITTEST_ID],
                ),
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.data)
            self.assertEqual(result.data.get("status"), "READY", result.data)
            self.assertFalse(marker.exists(), marker.read_text() if marker.exists() else "")
            identity = result.data["groups"][0]["executable_identity"]
            self.assertIn(
                identity["kind"], {"trusted-system-launcher", "absolute-launcher"}
            )
            self.assertIn(
                "verify.sh", [item["path"] for item in identity["repository_paths"]]
            )
            self.assertNotIn(str(hostile), " ".join(result.data["groups"][0]["execution_argv"]))

            pytest_fixture = self.fixture(
                test_files={
                    "tests/test_pyproof.py": (
                        "import importlib.util\n"
                        "from src.calc import add\n\n"
                        "def test_add():\n"
                        "    assert importlib.util.find_spec('poison_import') is None\n"
                        "    assert add(1, 2) == 3\n"
                    )
                }
            )
            pytest_result = prove(
                pytest_fixture,
                baseline_group(
                    argv=["python3", "-m", "pytest", PYTEST_ID],
                    test_ids=[PYTEST_ID],
                ),
                env=env,
            )
            self.assertEqual(pytest_result.returncode, 0, pytest_result.stderr)
            self.assertEqual(pytest_result.data.get("status"), "READY", pytest_result.data)
            self.assertNotIn(
                str(hostile),
                " ".join(pytest_result.data["groups"][0]["execution_argv"]),
            )
            self.assertFalse(marker.exists(), marker.read_text() if marker.exists() else "")

    def test_trusted_launcher_may_execute_only_a_frozen_wrapper(self) -> None:
        wrapper = "#!/usr/bin/env bash\nset -euo pipefail\nexec \"$@\"\n"
        fixture = self.fixture(
            initial_files={"verify.sh": wrapper},
            runner=f"bash verify.sh python3 -m unittest {UNITTEST_ID}",
        )
        result = prove(
            fixture,
            baseline_group(
                argv=[
                    "bash",
                    "verify.sh",
                    "python3",
                    "-m",
                    "unittest",
                    UNITTEST_ID,
                ],
                test_ids=[UNITTEST_ID],
            ),
        )
        self.assertEqual(result.returncode, 0, result.data)
        self.assertEqual(result.data.get("status"), "READY", result.data)
        identity = result.data["groups"][0]["executable_identity"]
        self.assertIn(
            identity["kind"], {"trusted-system-launcher", "absolute-launcher"}
        )
        self.assertEqual(identity["repository_paths"][0]["path"], "verify.sh")
        self.assertTrue(Path(result.data["groups"][0]["execution_argv"][0]).is_absolute())
        self.assertEqual(result.data["groups"][0]["framework"], "unittest")
        self.assertEqual(result.data["groups"][0]["test_ids"], [UNITTEST_ID])
        self.assertEqual(
            result.data["groups"][0]["baseline"]["test_result"]["matched_test_ids"],
            [UNITTEST_ID],
        )

        pytest_fixture = self.fixture(
            test_files={
                "tests/test_proof_target.py": unittest_source(),
                "tests/test_pyproof.py": pytest_source(),
            },
            initial_files={"verify.sh": wrapper},
            runner=f"bash verify.sh python3 -m unittest {UNITTEST_ID}",
        )
        pytest_result = prove(
            pytest_fixture,
            baseline_group(
                argv=[
                    "bash",
                    "verify.sh",
                    "python3",
                    "-m",
                    "pytest",
                    PYTEST_ID,
                ],
                test_ids=[PYTEST_ID],
            ),
        )
        self.assertEqual(pytest_result.returncode, 0, pytest_result.data)
        self.assertEqual(pytest_result.data.get("status"), "READY", pytest_result.data)
        pytest_group = pytest_result.data["groups"][0]
        self.assertEqual(pytest_group["framework"], "pytest")
        self.assertEqual(pytest_group["test_ids"], [PYTEST_ID])
        self.assertEqual(
            pytest_group["baseline"]["test_result"]["matched_test_ids"],
            [PYTEST_ID],
        )
        self.assertIn(
            pytest_group["executable_identity"]["kind"],
            {"trusted-system-launcher", "absolute-launcher"},
        )
        self.assertIn(
            "verify.sh",
            [
                item["path"]
                for item in pytest_group["executable_identity"]["repository_paths"]
            ],
        )

    def test_frozen_wrapper_suffix_normalizes_pytest_and_rejects_dispatchers(self) -> None:
        wrapper = "#!/usr/bin/env bash\nset -euo pipefail\nexec \"$@\"\n"
        fixture = self.fixture(
            test_files={
                "tests/test_proof_target.py": unittest_source(),
                "tests/test_pyproof.py": pytest_source(),
            },
            initial_files={"verify.sh": wrapper},
            runner=f"bash verify.sh python3 -m unittest {UNITTEST_ID}",
        )
        normalized_pytest = prove(
            fixture,
            baseline_group(
                argv=["bash", "verify.sh", "pytest", PYTEST_ID],
                test_ids=[PYTEST_ID],
            ),
        )
        pytest_group = assert_ready_wrapper_proof(
            self,
            normalized_pytest,
            framework="pytest",
            test_id=PYTEST_ID,
        )
        execution_argv = pytest_group["execution_argv"]
        self.assertNotEqual(execution_argv[2], "pytest")
        self.assertTrue(Path(execution_argv[2]).is_absolute())
        self.assertEqual(execution_argv[3:5], ["-m", "pytest"])

        cases = {
            "nested-env": (
                [
                    "bash",
                    "verify.sh",
                    "env",
                    "PATH=/usr/bin:/bin",
                    "python3",
                    "-m",
                    "unittest",
                    UNITTEST_ID,
                ],
                [UNITTEST_ID],
            ),
            "absolute-python": (
                [
                    "bash",
                    "verify.sh",
                    "/usr/bin/python3",
                    "-m",
                    "unittest",
                    UNITTEST_ID,
                ],
                [UNITTEST_ID],
            ),
            "unknown-dispatcher": (
                ["bash", "verify.sh", "dispatch-tests", UNITTEST_ID],
                [UNITTEST_ID],
            ),
            "mismatched-test-id": (
                [
                    "bash",
                    "verify.sh",
                    "python3",
                    "-m",
                    "unittest",
                    UNITTEST_ID,
                ],
                ["tests.test_proof_target.ProofTargetTest.test_missing"],
            ),
        }
        for name, (argv, test_ids) in cases.items():
            with self.subTest(case=name):
                rejected = prove(
                    fixture,
                    baseline_group(argv=argv, test_ids=test_ids),
                )
                assert_spec_rejected(self, rejected)

    def test_hostile_startup_environment_is_removed_before_frozen_entry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="proof-startup-environment-") as tmp:
            hostile = Path(tmp)
            bash_env_marker = hostile / "bash-env-used"
            bash_env = hostile / "bash-env.sh"
            bash_env.write_text(f"printf injected > {bash_env_marker}\n")
            function_marker = hostile / "function-used"
            pythonhome_marker = hostile / "pythonhome-used"
            loader_marker = hostile / "loader-used"
            loader = hostile / "loader.so"
            cases = {
                "bash-env": (
                    {"BASH_ENV": str(bash_env)},
                    bash_env_marker,
                    None,
                ),
                "exported-shell-function": (
                    {
                        "BASH_FUNC_python3%%": (
                            "() { printf injected > "
                            f"{function_marker}; command /usr/bin/python3 \"$@\"; }}"
                        )
                    },
                    function_marker,
                    None,
                ),
                "pythonhome": (
                    {"PYTHONHOME": sys.prefix},
                    pythonhome_marker,
                    "PYTHONHOME",
                ),
                "loader": (
                    {"LD_PRELOAD": str(loader)},
                    loader_marker,
                    "LD_PRELOAD",
                ),
            }
            for name, (environment, marker, guarded_variable) in cases.items():
                with self.subTest(case=name):
                    wrapper = "#!/usr/bin/env bash\nset -euo pipefail\n"
                    test_files = None
                    if guarded_variable is not None:
                        wrapper += (
                            f'if [ "${{{guarded_variable}+present}}" = present ]; then\n'
                            f"  printf injected > {str(marker)!r}\n"
                            "  exit 97\n"
                            "fi\n"
                        )
                        test_files = {
                            "tests/test_proof_target.py": unittest_source(
                                "import os\n"
                                f"self.assertNotIn({guarded_variable!r}, os.environ)\n"
                                "self.assertEqual(add(1, 2), 3)"
                            )
                        }
                    wrapper += 'exec "$@"\n'
                    fixture = self.fixture(
                        test_files=test_files,
                        initial_files={"verify.sh": wrapper},
                        runner=f"bash verify.sh python3 -m unittest {UNITTEST_ID}",
                    )
                    accepted = prove(
                        fixture,
                        baseline_group(
                            argv=[
                                "bash",
                                "verify.sh",
                                "python3",
                                "-m",
                                "unittest",
                                UNITTEST_ID,
                            ],
                            test_ids=[UNITTEST_ID],
                        ),
                        env=environment,
                    )
                    self.assertFalse(marker.exists(), marker)
                    group = assert_ready_wrapper_proof(
                        self,
                        accepted,
                        framework="unittest",
                        test_id=UNITTEST_ID,
                    )
                    serialized = json.dumps(group, sort_keys=True)
                    for variable in environment:
                        self.assertNotIn(variable, serialized)

    def test_nan_infinity_boolean_and_invalid_run_id_fail_without_evidence(self) -> None:
        fixture = self.fixture()
        for raw_timeout in (True, float("nan"), float("inf"), 30.5):
            result = prove(
                fixture,
                baseline_group(timeout_seconds=raw_timeout),
            )
            with self.subTest(timeout=raw_timeout):
                assert_rejected(self, result)
                self.assertEqual(result.data.get("code"), "TEST_PROOF_SPEC_INVALID")

        result = run_cli(
            "prove-tests",
            "--repo",
            fixture.repo,
            "--run",
            "../invalid",
            "--spec",
            "-",
            input_text=json.dumps(
                {"schema_version": 1, "groups": [baseline_group()]}
            ),
        )
        assert_rejected(self, result)


if __name__ == "__main__":
    unittest.main()
