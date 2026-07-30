from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from harness import (
    assert_status,
    cleanup_repo,
    git,
    load_ledger,
    run_cli,
    run_process,
)
from proof_harness import (
    UNITTEST_ID,
    baseline_group,
    create_proof_fixture,
    mutation_group,
    prove,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schema" / "codex-test-proof.schema.json").read_text())
BEHAVIORS = ("atomic-coverage", "replayable-mutation", "active-fail-closed")


def proof_input(*groups: dict) -> str:
    return json.dumps({"schema_version": 1, "groups": list(groups)})


def atomic_groups() -> list[dict]:
    return [baseline_group(behavior_id=behavior_id) for behavior_id in BEHAVIORS]


def write_ledger(run_path: Path, ledger: dict) -> None:
    (run_path / "ledger.json").write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2) + "\n"
    )


class ProofEvidenceIntegrityContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repos: list[Path] = []

    def tearDown(self) -> None:
        for repo in self.repos:
            cleanup_repo(repo)

    def fixture(self, **kwargs):
        fixture = create_proof_fixture(**kwargs)
        self.repos.append(fixture.repo)
        return fixture

    def test_public_schema_requires_atomic_groups_and_canonical_applied_diff(self) -> None:
        Draft202012Validator.check_schema(SCHEMA)
        validator = Draft202012Validator(SCHEMA)
        for example in SCHEMA["examples"]:
            validator.validate(example)

        for definition in (
            "baselineRedInput",
            "mutationInput",
            "reviewedBoundariesInput",
            "proofGroupCommon",
        ):
            behavior_ids = SCHEMA["$defs"][definition]["properties"]["behavior_ids"]
            resolved = SCHEMA["$defs"][behavior_ids["$ref"].split("/")[-1]]
            self.assertEqual(resolved.get("maxItems"), 1, definition)

        mutation_run = SCHEMA["$defs"]["mutationTestRun"]["allOf"][1]
        self.assertIn("applied_diff", mutation_run["required"])
        self.assertIn("applied_diff", mutation_run["properties"])
        self.assertEqual(
            mutation_run["properties"].get("applied_diff", {}).get("type"), "string"
        )

    def test_cli_rejects_non_atomic_or_inexact_coverage_before_execution(self) -> None:
        variants = {
            "multi": [
                baseline_group(behavior_id=BEHAVIORS[0]),
                baseline_group(behavior_id=BEHAVIORS[2]),
            ],
            "duplicate": atomic_groups()
            + [baseline_group(behavior_id=BEHAVIORS[0])],
            "missing": atomic_groups()[:-1],
            "unknown": atomic_groups()
            + [baseline_group(behavior_id="unknown-behavior")],
        }
        variants["multi"][0]["behavior_ids"] = list(BEHAVIORS[:2])

        for label, groups in variants.items():
            with self.subTest(label=label):
                fixture = self.fixture(
                    verify_machine=False,
                    requirement_minima={behavior: "strong" for behavior in BEHAVIORS},
                )
                before = copy.deepcopy(load_ledger(fixture.run_path))
                result = run_cli(
                    "prove-tests",
                    "--repo",
                    fixture.repo,
                    "--run",
                    fixture.run_path,
                    "--spec",
                    "-",
                    input_text=proof_input(*groups),
                )
                self.assertEqual(result.returncode, 1, result.data)
                self.assertEqual(result.data.get("status"), "NEEDS_USER", result.data)
                self.assertEqual(
                    result.data.get("code"), "TEST_PROOF_SPEC_INVALID", result.data
                )
                self.assertNotIn("evidence", result.data, result.data)
                after = load_ledger(fixture.run_path)
                self.assertEqual(after["verification"]["attempts"], [])
                self.assertIsNone(after.get("evidence", {}).get("test_effectiveness"))
                self.assertEqual(after, before)

    def test_atomic_groups_may_reuse_the_same_test_and_cover_each_behavior_once(self) -> None:
        fixture = self.fixture(
            verify_machine=False,
            requirement_minima={behavior: "strong" for behavior in BEHAVIORS},
        )
        result = run_cli(
            "prove-tests",
            "--repo",
            fixture.repo,
            "--run",
            fixture.run_path,
            "--spec",
            "-",
            input_text=proof_input(*atomic_groups()),
        )
        assert_status(result, "READY", rc=0)
        covered = [group["behavior_ids"][0] for group in result.data["groups"]]
        self.assertCountEqual(covered, BEHAVIORS)
        self.assertEqual(len(covered), len(set(covered)))
        for group in result.data["groups"]:
            self.assertEqual(group["test_ids"], [UNITTEST_ID])

    def test_mutation_evidence_is_self_contained_and_replayable(self) -> None:
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
        assert_status(result, "READY", rc=0)

        cli_group = result.data["groups"][0]
        ledger_group = load_ledger(fixture.run_path)["evidence"][
            "test_effectiveness"
        ]["provenance"]["groups"][0]
        self.assertEqual(cli_group, ledger_group)
        self.assertNotIn("patch", cli_group)

        mutation = cli_group["mutation"]
        self.assertIn("applied_diff", mutation)
        applied_diff = mutation["applied_diff"]
        self.assertRegex(applied_diff, r"(?m)^diff --git a/src/calc\.py b/src/calc\.py$")
        self.assertRegex(applied_diff, r"(?m)^index [0-9a-f]+\.\.[0-9a-f]+ 100644$")
        self.assertEqual(
            hashlib.sha256(applied_diff.encode()).hexdigest(),
            mutation["applied_diff_sha256"],
        )
        self.assertEqual(mutation["changed_paths"], ["src/calc.py"])
        self.assertEqual(
            mutation["test_result"]["classification"], "assertion-failure"
        )

        with tempfile.TemporaryDirectory(prefix="proof-replay-") as temp_dir:
            replay = Path(temp_dir) / "candidate"
            cloned = run_process(
                ["git", "clone", "-q", "--shared", fixture.builder, replay]
            )
            self.assertEqual(cloned.returncode, 0, cloned.stderr)
            applied = run_process(
                ["git", "apply", "--index", "--binary", "-"],
                cwd=replay,
                input_text=applied_diff,
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertEqual(git(replay, "diff", "--cached", "--name-only"), "src/calc.py")
            replayed = run_process(
                ["python3", "-m", "unittest", UNITTEST_ID], cwd=replay
            )
            self.assertNotEqual(replayed.returncode, 0, replayed.stdout + replayed.stderr)
            self.assertIn("FAILED", replayed.stdout + replayed.stderr)

    def test_canonical_mutation_diff_disables_textconv_and_replays(self) -> None:
        converter_source = (
            "import pathlib\n"
            "import sys\n\n"
            "source = pathlib.Path(sys.argv[-1]).read_text()\n"
            "label = 'mutated' if 'a + b - 1' in source else 'candidate'\n"
            "print(f'textconv-view={label}')\n"
        )
        fixture = self.fixture(
            initial_files={
                ".gitattributes": "src/calc.py diff=proof-converter\n",
                "textconv.py": converter_source,
            }
        )
        converter = fixture.repo / "textconv.py"
        git(
            fixture.repo,
            "config",
            "diff.proof-converter.textconv",
            f"python3 {converter}",
        )
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
        assert_status(result, "READY", rc=0)

        mutation = result.data["groups"][0]["mutation"]
        self.assertIn("applied_diff", mutation)
        applied_diff = mutation["applied_diff"]
        self.assertNotIn("textconv-view=", applied_diff)
        self.assertRegex(applied_diff, r"(?m)^index [0-9a-f]+\.\.[0-9a-f]+ 100644$")
        self.assertEqual(
            hashlib.sha256(applied_diff.encode()).hexdigest(),
            mutation["applied_diff_sha256"],
        )
        self.assertEqual(mutation["changed_paths"], ["src/calc.py"])

        with tempfile.TemporaryDirectory(prefix="proof-textconv-replay-") as temp_dir:
            replay = Path(temp_dir) / "candidate"
            cloned = run_process(
                ["git", "clone", "-q", "--shared", fixture.builder, replay]
            )
            self.assertEqual(cloned.returncode, 0, cloned.stderr)
            applied = run_process(
                ["git", "apply", "--index", "--binary", "-"],
                cwd=replay,
                input_text=applied_diff,
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertEqual(git(replay, "diff", "--cached", "--name-only"), "src/calc.py")
            replayed = run_process(
                ["python3", "-m", "unittest", UNITTEST_ID], cwd=replay
            )
            self.assertNotEqual(replayed.returncode, 0, replayed.stdout + replayed.stderr)
            self.assertIn("FAILED", replayed.stdout + replayed.stderr)

    def test_unsafe_active_evidence_cannot_unlock_blackbox_or_mutate_history(self) -> None:
        cases = ("multi-behavior", "missing-diff", "digest-mismatch")
        for case in cases:
            with self.subTest(case=case):
                fixture = self.fixture()
                if case == "multi-behavior":
                    proof = prove(fixture, baseline_group())
                else:
                    patch = (
                        "diff --git a/src/calc.py b/src/calc.py\n"
                        "--- a/src/calc.py\n"
                        "+++ b/src/calc.py\n"
                        "@@ -1,2 +1,2 @@\n"
                        " def add(a, b):\n"
                        "-    return a + b\n"
                        "+    return a + b - 1\n"
                    )
                    proof = prove(fixture, mutation_group(patch))
                assert_status(proof, "READY", rc=0)
                assert_status(run_cli("verify", "--run", fixture.run_path), "PASS", rc=0)

                ledger = load_ledger(fixture.run_path)
                group = ledger["evidence"]["test_effectiveness"]["provenance"][
                    "groups"
                ][0]
                if case == "multi-behavior":
                    group["behavior_ids"].append("second-behavior")
                elif case == "missing-diff":
                    group["mutation"].pop("applied_diff", None)
                else:
                    group["mutation"]["applied_diff"] = patch
                    group["mutation"]["applied_diff_sha256"] = "0" * 64
                write_ledger(fixture.run_path, ledger)
                frozen = (fixture.run_path / "ledger.json").read_bytes()
                attempts = copy.deepcopy(ledger["verification"]["attempts"])

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
                self.assertEqual(prepared.returncode, 1, prepared.data)
                self.assertEqual(
                    prepared.data.get("code"),
                    "TEST_EFFECTIVENESS_MISSING",
                    prepared.data,
                )
                self.assertIsNone(
                    prepared.data.get("test_effectiveness_head"), prepared.data
                )
                after = load_ledger(fixture.run_path)
                self.assertEqual(after["verification"]["attempts"], attempts)
                self.assertEqual((fixture.run_path / "ledger.json").read_bytes(), frozen)

    def test_atomic_active_and_terminal_history_controls_remain_readable(self) -> None:
        active = self.fixture()
        assert_status(prove(active, baseline_group()), "READY", rc=0)
        assert_status(run_cli("verify", "--run", active.run_path), "PASS", rc=0)
        prepared = run_cli(
            "prepare-follow-up",
            "--run",
            active.run_path,
            "--role",
            "tester",
            "--agent-id",
            active.tester_agent_id,
            "--purpose",
            "blackbox",
        )
        assert_status(prepared, "READY", rc=0)

        terminal = self.fixture()
        accepted = prove(terminal, baseline_group())
        assert_status(accepted, "READY", rc=0)
        ledger = load_ledger(terminal.run_path)
        ledger["evidence"]["test_effectiveness"]["provenance"]["groups"][0][
            "behavior_ids"
        ].append("legacy-ambiguous")
        write_ledger(terminal.run_path, ledger)
        abandoned = run_cli(
            "abandon", "--run", terminal.run_path, "--reason", "legacy history control"
        )
        assert_status(abandoned, "COMPLETE", rc=0)
        status = run_cli("status", "--run", terminal.run_path)
        assert_status(status, "COMPLETE", rc=0)
        self.assertEqual(status.data.get("phase"), "abandoned")
        persisted = load_ledger(terminal.run_path)
        self.assertNotIn(
            "applied_diff",
            persisted["evidence"]["test_effectiveness"]["provenance"]["groups"][0],
        )


if __name__ == "__main__":
    unittest.main()
