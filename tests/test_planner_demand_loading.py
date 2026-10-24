from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

from harness import ROOT, run_process


def runner_module():
    spec = importlib.util.spec_from_file_location("planner_behavior_runner", ROOT / "experiments/agent-behavior/runner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PlannerDemandLoadingTest(unittest.TestCase):
    def source(self, path):
        return {"path": path, "sha256": hashlib.sha256((ROOT / path).read_bytes()).hexdigest(), "revision": "WORKTREE"}

    def variant(self):
        return {"kind": "instruction", "instruction_source": self.source("skills/planner/SKILL.md"),
                "instruction_references": [self.source("skills/planner/references/builder-contract.md")]}

    def test_native_and_builder_inputs_have_distinct_loading_surfaces(self):
        runner = runner_module()
        variants = json.loads((ROOT / "experiments/agent-behavior/variants.json").read_text())["variants"]
        by_id = {v["id"]: v for v in variants}
        native = runner.instruction_input(by_id["planner-native-current"])
        builder = runner.instruction_input(by_id["planner-current"])
        self.assertNotIn("references", native)
        self.assertNotIn("execution.work_units", native["content"])
        self.assertNotIn("predecessor_pressure_digest", native["content"])
        self.assertEqual([r["path"] for r in builder["references"]], ["skills/planner/references/builder-contract.md"])
        self.assertIn("work_units", builder["content"])
        self.assertNotIn("predecessor_pressure_digest", builder["content"])
        self.assertNotIn("prior_problem_dispositions", builder["content"])
        self.assertFalse((ROOT / "skills/builder-loop-planner").exists())

    def test_reference_drift_is_rejected_even_when_primary_is_unchanged(self):
        runner = runner_module()
        variant = self.variant()
        variant["instruction_references"][0]["sha256"] = "0" * 64
        with self.assertRaises(runner.ExperimentError):
            runner.instruction_input(variant)

    def test_order_and_every_reference_are_bound_to_input_digest(self):
        runner = runner_module()
        variant = self.variant()
        variant["instruction_references"].append(self.source("skills/planner/references/builder-lifecycle.md"))
        first = runner.instruction_input(variant)
        variant["instruction_references"].reverse()
        second = runner.instruction_input(variant)
        self.assertNotEqual(first["input_sha256"], second["input_sha256"])
        self.assertNotEqual(first["content"], second["content"])
        variant["instruction_references"].append(variant["instruction_source"])
        with self.assertRaises(runner.ExperimentError):
            runner.instruction_input(variant)

    def test_invalid_reference_sources_are_rejected(self):
        runner = runner_module()
        for references in (None, {}, [{"path": "../outside", "sha256": "0" * 64}],
                           [{"path": "missing.md", "sha256": "0" * 64}]):
            with self.subTest(references=references):
                variant = self.variant()
                variant["instruction_references"] = references
                with self.assertRaises(runner.ExperimentError):
                    runner.instruction_input(variant)
        with self.assertRaises(runner.ExperimentError):
            runner.instruction_input({"kind": "baseline", "instruction_source": None,
                                      "instruction_references": self.variant()["instruction_references"]})

    def environment(self, home):
        return {**os.environ, "HOME": str(home), "CODEX_HOME": str(home / ".codex"),
                "PATH": f"{home / '.local/bin'}:{os.environ['PATH']}"}

    def retired(self, home):
        path = home / ".agents/skills/builder-loop-planner"
        path.parent.mkdir(parents=True)
        return path

    def install(self, home, env=None):
        return run_process(["bash", ROOT / "install.sh"], cwd=ROOT, env=env or self.environment(home))

    def test_upgrade_retires_owned_dangling_link_and_installed_references_resolve(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            retired = self.retired(home)
            retired.symlink_to(ROOT / "skills/builder-loop-planner")
            self.assertTrue(retired.is_symlink())
            self.assertFalse(retired.exists())
            for _ in range(2):
                result = self.install(home)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(retired.is_symlink())
            planner = home / ".agents/skills/planner"
            for file in [planner / "SKILL.md", * (planner / "references").glob("*.md")]:
                for relative in re.findall(r"\]\(([^)]+\.md)\)", file.read_text()):
                    self.assertTrue((file.resolve().parent / relative).is_file(), (file, relative))
            contract = planner / "references/builder-contract.md"
            self.assertTrue((contract.resolve().parent / "../../../schema/assurance-v4-contract.schema.json").is_file())
            result = run_process(["bash", ROOT / "uninstall.sh"], cwd=ROOT, env=self.environment(home))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(planner.is_symlink())

    def test_upgrade_preserves_foreign_link_and_ordinary_file(self):
        for kind in ("foreign", "ordinary"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as raw:
                home = Path(raw)
                retired = self.retired(home)
                if kind == "foreign":
                    retired.symlink_to(home / "unrelated-missing-skill")
                else:
                    retired.write_text("user content")
                result = self.install(home)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("leaving", result.stderr)
                result = run_process(["bash", ROOT / "uninstall.sh"], cwd=ROOT, env=self.environment(home))
                self.assertEqual(result.returncode, 0, result.stderr)
                if kind == "foreign":
                    self.assertEqual(os.readlink(retired), str(home / "unrelated-missing-skill"))
                else:
                    self.assertEqual(retired.read_text(), "user content")

    def test_failed_config_commit_restores_exact_retired_link(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            retired = self.retired(home)
            target = os.path.relpath(ROOT / "skills/builder-loop-planner", retired.parent)
            retired.symlink_to(target)
            tools = home / ".local/bin"
            tools.mkdir(parents=True)
            wrapper = tools / "python3"
            wrapper.write_text(f"#!{sys.executable}\nimport os,sys\n"
                               "if len(sys.argv)>2 and sys.argv[1].endswith('codex-builder-loop-config.py') and sys.argv[2]=='install': sys.exit(73)\n"
                               f"os.execv({sys.executable!r}, [{sys.executable!r}]+sys.argv[1:])\n")
            wrapper.chmod(0o755)
            result = self.install(home)
            self.assertEqual(result.returncode, 73, result.stderr)
            self.assertTrue(retired.is_symlink())
            self.assertEqual(os.readlink(retired), target)
            self.assertFalse((home / ".agents/skills/planner").is_symlink())
            self.assertFalse((home / ".codex/AGENTS.md").exists())

    def test_uninstall_retires_owned_link_without_existing_source(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            retired = self.retired(home)
            retired.symlink_to(ROOT / "skills/builder-loop-planner")
            result = run_process(["bash", ROOT / "uninstall.sh"], cwd=ROOT, env=self.environment(home))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(retired.is_symlink())
