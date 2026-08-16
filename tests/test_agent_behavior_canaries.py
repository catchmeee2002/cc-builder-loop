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
    "proof-source-real-inputs",
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
    def test_planner_canaries_freeze_builder_loop_activation_context(self) -> None:
        scenarios = json.loads(SCENARIOS.read_text(encoding="utf-8"))
        planner_canaries = {
            item["id"]: item
            for item in scenarios["scenarios"]
            if item.get("role") == "planner" and "canary_case_id" in item
        }

        self.assertEqual(
            set(planner_canaries),
            {
                "planner-positive-outcome-presence",
                "planner-feature-content-density",
            },
        )
        for scenario in planner_canaries.values():
            prompt = scenario["prompt"]
            self.assertIn("用户已经选择「Builder-loop 实验」", prompt)
            self.assertIn("只回答下述行为场景中的规划判据", prompt)
            self.assertIn("不要再次询问 Codex 原生 Plan 或 Builder-loop 实验", prompt)
            self.assertIn("不生成完整 Assurance v4 contract", prompt)
            self.assertIn("不执行 admission 验证", prompt)
            self.assertIn("不要启动 run", prompt)

    def test_manifest_and_scenarios_cover_each_declared_fixture_role(self) -> None:
        manifest = json.loads(CANARIES.read_text(encoding="utf-8"))
        scenarios = json.loads(SCENARIOS.read_text(encoding="utf-8"))
        cases = {item["id"]: item for item in manifest["cases"]}
        references = [
            item
            for item in scenarios["scenarios"]
            if "canary_case_id" in item
        ]

        self.assertEqual(len(cases), len(manifest["cases"]))
        by_case = {
            case_id: [
                item for item in references if item["canary_case_id"] == case_id
            ]
            for case_id in FIXTURE_CASES
        }
        self.assertEqual(
            {item["canary_case_id"] for item in references},
            set(FIXTURE_CASES),
        )
        self.assertEqual(set(by_case), set(FIXTURE_CASES))
        self.assertEqual(len(references), 10)
        for case_id, case_references in by_case.items():
            case = cases[case_id]
            self.assertEqual(case["mode"], "fixture")
            self.assertEqual(
                {item["role"] for item in case_references},
                set(case["roles"]),
            )
            self.assertIn(
                case["scenario_id"],
                {item["id"] for item in case_references},
            )
            self.assertEqual(
                len(case_references),
                len({item["id"] for item in case_references}),
            )
            self.assertGreater(case["minimum_fresh_samples"], 0)

        builder = next(
            item for item in references if item["id"] == "builder-document-ground-truth"
        )
        self.assertEqual(builder["variant_id"], "builder-agent-current")

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
        self.assertEqual(len(listed["cases"]), 7)
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

            producer = prepared["producer-consumer-chain"]
            self.assertEqual(
                producer["facts"]["status_policy"],
                {"visible": ["active", "forged"], "hidden": ["archived"]},
            )
            proof = producer["proof_mutation"]
            self.assertEqual(proof["target_paths"], ["world.py"])
            self.assertEqual(proof["changed_paths"], ["world.py"])
            self.assertEqual(proof["baseline_check"]["returncode"], 0)
            self.assertEqual(proof["mutation_command"]["returncode"], 0)
            self.assertEqual(proof["mutated_check"]["returncode"], 1)
            self.assertEqual(proof["restored_check"]["returncode"], 0)
            self.assertTrue(proof["tree_restored"])
            producer_root = Path(producer["fixture_root"])
            contract = json.loads(
                (producer_root / "contract.json").read_text(encoding="utf-8")
            )
            self.assertEqual(contract["status_policy"], producer["facts"]["status_policy"])
            self.assertEqual(
                git(producer_root, "status", "--porcelain", "--untracked-files=all"),
                "",
            )

            proof_source = prepared["proof-source-real-inputs"]
            self.assertEqual(
                proof_source["facts"]["seeded_defects"],
                [
                    "wrong-bound-call-site",
                    "wrong-public-failure-semantics",
                    "ambient-proof-dependency",
                ],
            )
            self.assertEqual(proof_source["weak_check"]["returncode"], 0)
            self.assertEqual(
                proof_source["discriminating_check"]["returncode"], 1
            )
            proof_source_root = Path(proof_source["fixture_root"])
            self.assertEqual(
                git(
                    proof_source_root,
                    "status",
                    "--porcelain",
                    "--untracked-files=all",
                ),
                "",
            )

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

            proof_repeat_root = parent / "proof-source-repeat"
            returncode, proof_repeat, stderr = run_canary(
                "prepare",
                "--case-id",
                "proof-source-real-inputs",
                "--output",
                str(proof_repeat_root),
            )
            self.assertEqual(returncode, 0, stderr)
            self.assertEqual(proof_repeat["facts"], proof_source["facts"])
            self.assertEqual(
                proof_repeat["fixture_manifest"]["digest"],
                proof_source["fixture_manifest"]["digest"],
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
