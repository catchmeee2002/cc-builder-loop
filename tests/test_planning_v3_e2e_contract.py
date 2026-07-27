from __future__ import annotations

import copy
import hashlib
import json
import re
import shlex
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from harness import (
    _canonical_case_results,
    assert_status,
    assert_status_one_of,
    blackbox_report_details,
    cleanup_repo,
    commit_all,
    git,
    head,
    init_repo,
    load_ledger,
    plan_markdown,
    record_evidence,
    register_agent,
    run_cli,
    run_process,
    write_plan,
)
from proof_harness import baseline_group, create_proof_fixture, prove


ROOT = Path(__file__).resolve().parents[1]
LEGACY_SPEC_HEAD = "1b512b120a673470a4ee154b7c8dd8ac3c3f7e1f"


def rejection(test: unittest.TestCase, result) -> None:
    test.assertNotEqual(result.returncode, 0, result.data)
    test.assertNotIn(result.data.get("status"), {"READY", "PASS"}, result.data)
    test.assertNotIn("Traceback", result.stdout + result.stderr)


class PlanningV3E2EContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repos: list[Path] = []
        self.tempdirs: list[Path] = []

    def tearDown(self) -> None:
        for repo in self.repos:
            cleanup_repo(repo)
        for path in self.tempdirs:
            shutil.rmtree(path, ignore_errors=True)

    def test_started_v3_freezes_effectiveness_and_structured_e2e_targets(self) -> None:
        repo = init_repo()
        self.repos.append(repo)
        text = plan_markdown(head(repo), include_e2e=True)
        plan = write_plan(repo, text)
        started = run_cli(
            "start",
            "--repo",
            repo,
            "--plan",
            plan,
            "--task",
            "v3 structured plan",
            "--session-id",
            "v3-plan-session",
        )
        assert_status(started, "READY", rc=0)
        ledger = load_ledger(Path(started.data["run_path"]))
        self.assertEqual(ledger["plan"]["behavior_ids"], ["add-positive"])
        self.assertIs(ledger["plan"]["has_e2e_cases"], True)
        self.assertEqual(
            ledger["plan"]["sha256"], hashlib.sha256(text.encode()).hexdigest()
        )
        frozen = Path(ledger["plan"]["path"]).read_text()
        self.assertEqual(frozen, text)
        self.assertIn("schema_version: 3", frozen)
        self.assertIn(
            "test_effectiveness:\n  requirements:\n"
            "    - behavior_id: add-positive\n      minimum: strong",
            frozen,
        )
        self.assertIn("schema_version: 1\ncases:\n  - id: add-cli", frozen)
        self.assertIn("covers: [add-positive]", frozen)
        self.assertIn("verify:\n      must:", frozen)
        self.assertIn("must_not:", frozen)
        self.assertIn("quality:\n      criteria:", frozen)

    def test_canonical_case_results_distinguish_full_and_fast_levels(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="canonical-e2e-cases-"))
        self.tempdirs.append(root)
        plan = root / "plan.md"
        plan.write_text(
            "<!-- e2e-cases -->\n"
            "schema_version: 1\n"
            "cases:\n"
            "  - id: full-case\n"
            "    level: full\n"
            "  - id: fast-case\n"
            "    level: fast\n"
            "<!-- /e2e-cases -->\n"
        )
        results = _canonical_case_results(
            {"plan": {"path": str(plan)}}, passed=True
        )
        self.assertEqual(
            results,
            [
                {
                    "case_id": "full-case",
                    "level": "full",
                    "mechanical": "pass",
                    "verify": "pass",
                    "quality": "pass",
                    "outcome": "pass",
                },
                {
                    "case_id": "fast-case",
                    "level": "fast",
                    "mechanical": "pass",
                    "verify": "not_applicable",
                    "quality": "not_applicable",
                    "outcome": "pass",
                },
            ],
        )

    def test_blackbox_evidence_is_proof_first_and_case_complete(self) -> None:
        fixture = create_proof_fixture(include_e2e=True)
        self.repos.append(fixture.repo)
        command = [
            "python3",
            "-m",
            "unittest",
            "tests.test_proof_target",
        ]
        head_before = head(fixture.builder)
        executed = run_process(
            command,
            cwd=fixture.builder,
            env={"PYTHONDONTWRITEBYTECODE": "1"},
        )
        head_after = head(fixture.builder)
        self.assertEqual(executed.returncode, 0, executed.stderr)
        candidate_dirty = bool(
            git(
                fixture.builder,
                "status",
                "--porcelain",
                "--untracked-files=all",
            )
            or git(
                fixture.builder,
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
            )
        )
        self.assertFalse(candidate_dirty)
        base = blackbox_report_details(
            load_ledger(fixture.run_path),
            candidate_worktree=fixture.builder,
            head_before=head_before,
            head_after=head_after,
            command=shlex.join(command),
            returncode=executed.returncode,
            candidate_dirty=candidate_dirty,
        )
        before = run_cli(
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
            json.dumps(base),
        )
        rejection(self, before)
        self.assertIn("proof", str(before.data).lower())

        proof = prove(fixture, baseline_group())
        self.assertEqual(proof.data.get("status"), "READY", proof.data)
        assert_status(run_cli("verify", "--run", fixture.run_path), "PASS", rc=0)
        register_agent(
            fixture.run_path,
            "tester",
            agent_id=fixture.tester_agent_id,
            result="pass",
        )

        missing_cases = copy.deepcopy(base)
        missing_cases.pop("cases")
        duplicate_cases = copy.deepcopy(base)
        duplicate_cases["cases"].append(copy.deepcopy(duplicate_cases["cases"][0]))
        unknown_case = copy.deepcopy(base)
        unknown_case["cases"][0]["case_id"] = "unknown"
        inconsistent_outcome = copy.deepcopy(base)
        inconsistent_outcome["cases"][0]["verify"] = {
            "status": "fail",
            "observation": "The frozen behavior failed.",
        }
        malformed = (
            missing_cases,
            duplicate_cases,
            unknown_case,
            inconsistent_outcome,
        )
        for details in malformed:
            with self.subTest(details=details):
                result = run_cli(
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
                rejection(self, result)
                self.assertIsInstance(result.data.get("code"), str, result.data)

    def test_existing_active_v2_run_completes_without_identity_replacement(self) -> None:
        repo = init_repo()
        self.repos.append(repo)
        legacy_checkout = Path(tempfile.mkdtemp(prefix="builder-loop-legacy-v2-"))
        self.tempdirs.append(legacy_checkout)
        cloned = run_process(
            ["git", "clone", "-q", "--shared", str(ROOT), str(legacy_checkout)]
        )
        self.assertEqual(cloned.returncode, 0, cloned.stderr)
        checked_out = run_process(
            ["git", "checkout", "-q", "--detach", LEGACY_SPEC_HEAD],
            cwd=legacy_checkout,
        )
        self.assertEqual(checked_out.returncode, 0, checked_out.stderr)

        v3 = plan_markdown(head(repo))
        v2 = v3.replace("schema_version: 3", "schema_version: 2", 1)
        v2 = re.sub(
            r"test_effectiveness:\n  requirements:\n"
            r"    - behavior_id: add-positive\n      minimum: strong\n",
            "",
            v2,
            count=1,
        )
        plan = write_plan(repo, v2, name="legacy-active-v2.md")
        legacy_cli = legacy_checkout / "scripts" / "codex-builder-loop.py"
        created = run_process(
            [
                sys.executable,
                legacy_cli,
                "start",
                "--repo",
                repo,
                "--plan",
                plan,
                "--task",
                "legacy active v2 continuation",
                "--session-id",
                "legacy-v2-session",
            ]
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        payload = json.loads(created.stdout.splitlines()[-1])
        self.assertEqual(payload["status"], "READY", payload)
        run_path = Path(payload["run_path"])
        original = load_ledger(run_path)
        run_id = original["run_id"]
        session_id = original["owner_session_id"]
        builder = Path(original["worktrees"]["builder"]["path"])

        (builder / "src" / "legacy_continuation.py").write_text("READY = True\n")
        commit_all(builder, "continue legacy candidate")
        tester_id = register_agent(run_path, "tester", agent_id="legacy-v2-tester")
        integrated = run_cli("integrate-tests", "--run", run_path)
        assert_status_one_of(integrated, {"READY", "NOOP"}, rc=0)
        assert_status(run_cli("verify", "--run", run_path), "PASS", rc=0)
        candidate = head(builder)
        register_agent(run_path, "tester", agent_id=tester_id, result="pass")
        record_evidence(run_path, "e2e_verified", candidate, agent_id=tester_id)
        reviewer_id = register_agent(
            run_path, "reviewer", agent_id="legacy-v2-reviewer", result="pass"
        )
        for kind in ("reviewed", "doc_reviewed"):
            record_evidence(run_path, kind, candidate, agent_id=reviewer_id)

        ready = run_cli("status", "--run", run_path)
        assert_status(ready, "ACTIVE", rc=0)
        self.assertIs(ready.data.get("ready_to_finalize"), True, ready.data)
        finalized = run_cli("finalize", "--run", run_path)
        assert_status(finalized, "COMPLETE", rc=0)
        continued = load_ledger(run_path)
        self.assertEqual(continued["run_id"], run_id)
        self.assertEqual(continued["owner_session_id"], session_id)
        self.assertEqual(continued["agents"]["tester"]["agent_id"], tester_id)
        self.assertEqual(continued["agents"]["reviewer"]["agent_id"], reviewer_id)
        self.assertEqual(continued["finalize_intent"]["candidate_head"], candidate)


if __name__ == "__main__":
    unittest.main()
