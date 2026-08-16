from __future__ import annotations

import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any

from harness import ROOT, git, run_process


CANARY = ROOT / "experiments" / "agent-behavior" / "canary.py"
CANARIES = ROOT / "experiments" / "agent-behavior" / "canaries.json"
SCENARIOS = ROOT / "experiments" / "agent-behavior" / "scenarios.json"
FIXTURE_CASES = (
    "positive-outcome-presence",
    "large-diff-review-depth",
    "document-ground-truth",
    "feature-content-density",
    "producer-consumer-chain",
)


def run_canary(*args: str) -> tuple[int, dict[str, Any], str]:
    completed = run_process(
        [sys.executable, CANARY, *args],
        cwd=ROOT,
        env={"PYTHONDONTWRITEBYTECODE": "1"},
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise AssertionError(
            "canary must emit exactly one JSON line\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return completed.returncode, json.loads(lines[0]), completed.stderr


class AgentBehaviorCanaryTest(unittest.TestCase):
    def test_manifest_and_scenarios_have_one_to_one_fixture_bindings(self) -> None:
        manifest = json.loads(CANARIES.read_text(encoding="utf-8"))
        scenarios = json.loads(SCENARIOS.read_text(encoding="utf-8"))
        cases = {item["id"]: item for item in manifest["cases"]}
        references = [
            item
            for item in scenarios["scenarios"]
            if "canary_case_id" in item
        ]

        self.assertEqual(len(cases), len(manifest["cases"]))
        self.assertEqual(
            {item["canary_case_id"] for item in references},
            set(FIXTURE_CASES),
        )
        self.assertEqual(len(references), len(FIXTURE_CASES))
        for scenario in references:
            case = cases[scenario["canary_case_id"]]
            self.assertEqual(case["mode"], "fixture")
            self.assertEqual(case["scenario_id"], scenario["id"])
            self.assertIn(scenario["role"], case["roles"])
            self.assertGreater(case["minimum_fresh_samples"], 0)

        probe = cases["host-background-contention"]
        self.assertEqual(probe["mode"], "operational_probe")
        self.assertNotIn("scenario_id", probe)

    def test_list_is_stable_and_complete(self) -> None:
        first = run_process(
            [sys.executable, CANARY, "list"],
            cwd=ROOT,
            env={"PYTHONDONTWRITEBYTECODE": "1"},
        )
        second = run_process(
            [sys.executable, CANARY, "list"],
            cwd=ROOT,
            env={"PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        listed = json.loads(first.stdout)
        self.assertEqual(len(listed["cases"]), 6)
        self.assertEqual(
            {item["id"] for item in listed["cases"]},
            set(FIXTURE_CASES) | {"host-background-contention"},
        )

    def test_fixture_preconditions_are_discriminating_and_ephemeral(self) -> None:
        before_status = git(ROOT, "status", "--porcelain", "--untracked-files=all")
        before_ignored = git(
            ROOT, "ls-files", "--others", "--ignored", "--exclude-standard"
        )
        with tempfile.TemporaryDirectory(prefix="agent-behavior-canaries-") as raw:
            parent = Path(raw)
            prepared: dict[str, dict[str, Any]] = {}
            for case_id in FIXTURE_CASES:
                output = parent / case_id
                returncode, result, stderr = run_canary(
                    "prepare", "--case-id", case_id, "--output", str(output)
                )
                self.assertEqual(returncode, 0, stderr)
                self.assertEqual(result["status"], "READY")
                self.assertEqual(result["case_id"], case_id)
                self.assertEqual(Path(result["fixture_root"]), output.resolve())
                self.assertEqual(result["weak_check"]["returncode"], 0)
                self.assertTrue(result["weak_check"]["matched_expectation"])
                self.assertEqual(result["discriminating_check"]["returncode"], 1)
                self.assertTrue(
                    result["discriminating_check"]["matched_expectation"]
                )
                self.assertRegex(
                    result["fixture_manifest"]["digest"], r"^[0-9a-f]{64}$"
                )
                self.assertTrue((output / "REQUEST.md").is_file())
                prepared[case_id] = result

            large = prepared["large-diff-review-depth"]
            facts = large["facts"]
            self.assertGreater(facts["diff_lines"], 8000)
            self.assertEqual(
                facts["seeded_defects"],
                ["feature_0042", "feature_0077", "consumer-binding"],
            )
            large_root = Path(large["fixture_root"])
            expected = json.loads(
                (large_root / "expected_symbols.json").read_text(encoding="utf-8")
            )
            modules = "".join(
                path.read_text(encoding="utf-8")
                for path in sorted((large_root / "modules").glob("*.py"))
            )
            self.assertIn("feature_0042", expected)
            self.assertNotIn("def feature_0042(", modules)
            self.assertNotIn(
                "feature_0077",
                (large_root / "exports.py").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "feature_0001 as selected_feature",
                (large_root / "consumer.py").read_text(encoding="utf-8"),
            )
            numstat = git(
                large_root,
                "diff",
                "--numstat",
                facts["spec_head"],
                facts["candidate_head"],
            )
            changed_lines = sum(
                int(added) + int(deleted)
                for added, deleted, _path in (
                    line.split("\t", 2) for line in numstat.splitlines()
                )
            )
            self.assertGreater(changed_lines, 8000)

            repeat_root = parent / "large-diff-repeat"
            returncode, repeat, stderr = run_canary(
                "prepare",
                "--case-id",
                "large-diff-review-depth",
                "--output",
                str(repeat_root),
            )
            self.assertEqual(returncode, 0, stderr)
            self.assertEqual(repeat["facts"], facts)
            self.assertEqual(
                repeat["fixture_manifest"]["digest"],
                large["fixture_manifest"]["digest"],
            )

        self.assertEqual(
            git(ROOT, "status", "--porcelain", "--untracked-files=all"),
            before_status,
        )
        self.assertEqual(
            git(ROOT, "ls-files", "--others", "--ignored", "--exclude-standard"),
            before_ignored,
        )

    def test_probe_reproduces_timeout_and_reaps_the_process_group(self) -> None:
        returncode, result, stderr = run_canary(
            "probe", "--case-id", "host-background-contention"
        )
        self.assertEqual(returncode, 0, stderr)
        self.assertEqual(result["status"], "REPRODUCED")
        self.assertTrue(result["foreground_timed_out"])
        self.assertIsNone(result["foreground_returncode"])
        self.assertTrue(result["holder_alive_at_timeout"])
        self.assertEqual(result["holder_pid"], result["holder_pgid"])
        self.assertTrue(result["holder_cleanup"]["reaped"])
        self.assertTrue(result["holder_cleanup"]["process_group_gone"])
        self.assertEqual(len(result["unproven_boundaries"]), 3)

    def test_prepare_rejects_repository_internal_output(self) -> None:
        forbidden = ROOT / f".canary-forbidden-{uuid.uuid4().hex}"
        returncode, result, _stderr = run_canary(
            "prepare",
            "--case-id",
            "positive-outcome-presence",
            "--output",
            str(forbidden),
        )
        self.assertEqual(returncode, 2)
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("仓库外", result["message"])
        self.assertFalse(forbidden.exists())


if __name__ == "__main__":
    unittest.main()
