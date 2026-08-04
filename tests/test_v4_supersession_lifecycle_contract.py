from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from harness import CLI, cleanup_repo, commit_all, git, init_repo, run_process


def base_contract() -> dict[str, Any]:
    return {
        "schema_version": 4,
        "mission": {
            "delivery_kind": "code",
            "revision": 1,
            "supersedes": None,
            "objective": "Deliver a calculator through an auditable v4 lifecycle.",
            "behaviors": [
                {"id": "add-values", "description": "Addition returns the sum."}
            ],
            "interfaces": [
                {
                    "id": "calc-api",
                    "description": "src.calc.add(a, b) returns a number.",
                }
            ],
            "acceptance_cases": [
                {"id": "add-positive", "description": "add(1, 2) returns 3."}
            ],
            "trust_boundaries": [
                {
                    "id": "ledger-truth",
                    "description": "Persisted ledgers and Git objects define continuity.",
                }
            ],
        },
        "authority": {
            "target_branch": "main",
            "builder_write": ["src/**"],
            "tester_write": ["tests/**"],
            "dirty_intake": [],
            "public_prerequisites": [],
            "protected_support_paths": [],
            "external_targets": [],
        },
        "assurance": {
            "required": ["machine"],
            "machine_commands": [
                {
                    "id": "fixture-tests",
                    "argv": ["bash", "verify.sh"],
                    "timeout_seconds": 30,
                    "expected_returncodes": [0],
                }
            ],
        },
        "execution": {
            "version": 1,
            "candidate_head": None,
            "builder_files": [],
            "tester_files": [],
            "tester_source": None,
            "driver_enforced": False,
            "continuation": None,
            "carryover": None,
            "deployment": None,
            "dirty_snapshot": [],
            "commands": [],
            "agents": {},
        },
    }


class V4SupersessionLifecycleContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo()
        self.tempdir = tempfile.TemporaryDirectory(prefix="v4-supersession-lifecycle-")
        self.artifacts = Path(self.tempdir.name)

    def tearDown(self) -> None:
        cleanup_repo(self.repo)
        self.tempdir.cleanup()

    def invoke(self, command: str, *args: str | Path) -> tuple[int, dict[str, Any]]:
        completed = run_process(
            [
                "python3",
                CLI,
                "assurance",
                "--experimental-v4",
                command,
                *args,
            ]
        )
        lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, (completed.returncode, completed.stdout, completed.stderr))
        value = json.loads(lines[-1])
        self.assertIsInstance(value, dict)
        return completed.returncode, value

    def write_json(self, name: str, value: Any) -> Path:
        path = self.artifacts / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def start(self, run_id: str, contract: dict[str, Any]) -> dict[str, Any]:
        rc, value = self.invoke(
            "start",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--session-id",
            f"session-{run_id}",
            "--contract",
            self.write_json(f"{run_id}.json", contract),
        )
        self.assertEqual(rc, 0, value)
        return value

    def status(self, run_id: str) -> dict[str, Any]:
        rc, value = self.invoke("status", "--repo", self.repo, "--run", run_id)
        self.assertEqual(rc, 0, value)
        return value

    def context(self, run_id: str) -> dict[str, Any]:
        rc, value = self.invoke(
            "driver-context", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, value)
        return value

    def next_contract(
        self,
        source: dict[str, Any],
        *,
        dispositions: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        candidate_head = git(source["candidate_worktree"], "rev-parse", "HEAD")
        contract = base_contract()
        contract["mission"]["revision"] = source["mission_revision"] + 1
        contract["mission"]["supersedes"] = {
            "run_id": source["run_id"],
            "revision": source["mission_revision"],
            "mission_digest": source["digests"]["mission"],
            "candidate_head": candidate_head,
        }
        contract["execution"]["revision_transition"] = {
            "category": "execution_contract",
            "predecessor_pressure_digest": source["lineage"]["pressure_digest"],
        }
        contract["execution"]["prior_problem_dispositions"] = {
            "source_snapshot_digest": source["lineage"][
                "open_problem_snapshot_digest"
            ],
            "items": dispositions or [],
        }
        return contract

    def test_active_source_handoff_is_atomic_and_rebinds_run_local_state(self) -> None:
        source_run = "active-source"
        source = self.start(source_run, base_contract())
        candidate = Path(source["candidate_worktree"])
        (candidate / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\nCANDIDATE_REVISION = 2\n",
            encoding="utf-8",
        )
        source_candidate = commit_all(candidate, "record source candidate")

        for command, agent_id, thread_id in (
            ("prepare-builder", "source-builder", "source-builder-thread"),
            ("prepare-tester", "source-tester", "source-tester-thread"),
        ):
            rc, prepared = self.invoke(
                command,
                "--repo",
                self.repo,
                "--run",
                source_run,
                "--agent-id",
                agent_id,
                "--thread-id",
                thread_id,
            )
            self.assertEqual(rc, 0, prepared)

        rc, checkpointed = self.invoke(
            "checkpoint-builder", "--repo", self.repo, "--run", source_run
        )
        self.assertEqual(rc, 0, checkpointed)
        rc, verified = self.invoke(
            "verify-machine", "--repo", self.repo, "--run", source_run
        )
        self.assertEqual(rc, 0, verified)

        report = {
            "schema_version": 1,
            "problems": [
                {
                    "key": "carry-forward",
                    "summary": "Builder work remains",
                    "details": "The successor must retain this open implementation problem.",
                    "owner": "builder",
                },
                {
                    "key": "handled-elsewhere",
                    "summary": "External record owns this problem",
                    "details": "An authoritative project issue already tracks this item.",
                    "owner": "current_project",
                },
                {
                    "key": "discard-finding",
                    "summary": "Tester finding was invalid",
                    "details": "The observation does not apply to the frozen behavior.",
                    "owner": "tester",
                },
            ],
        }
        rc, recorded = self.invoke(
            "record-problems",
            "--repo",
            self.repo,
            "--run",
            source_run,
            "--report",
            self.write_json("source-problems.json", report),
            "--role",
            "tester",
            "--agent-id",
            "source-tester",
            "--thread-id",
            "source-tester-thread",
        )
        self.assertEqual(rc, 0, recorded)

        source = self.status(source_run)
        source_context = self.context(source_run)
        self.assertEqual(source["phase"], "active")
        self.assertEqual(
            source_context["facets"]["execution"]["candidate_head"], source_candidate
        )
        self.assertEqual(set(source_context["evidence"]), {"machine"})
        self.assertEqual(
            set(source_context["facets"]["execution"]["agents"]),
            {"builder", "tester"},
        )

        dispositions = [
            {"key": "carry-forward", "disposition": "included"},
            {"key": "handled-elsewhere", "disposition": "handled_elsewhere"},
            {"key": "discard-finding", "disposition": "discarded"},
        ]
        target_contract = self.next_contract(source, dispositions=dispositions)
        rc, validated = self.invoke(
            "validate",
            "--contract",
            self.write_json("active-target-validated.json", target_contract),
        )
        self.assertEqual(rc, 0, validated)

        target_run = "active-target"
        target = self.start(target_run, target_contract)
        source_after = self.status(source_run)
        target_context = self.context(target_run)
        target_ledger_path = Path(target["candidate_worktree"]).parent / "ledger.json"
        target_ledger = json.loads(target_ledger_path.read_text(encoding="utf-8"))

        self.assertEqual(source_after["phase"], "superseded")
        self.assertEqual(
            source_after["supersede_intent"],
            {
                "source_run_id": source_run,
                "target_run_id": target_run,
                "state": "received",
            },
        )
        source_ledger_path = Path(source["candidate_worktree"]).parent / "ledger.json"
        superseded_bytes = source_ledger_path.read_bytes()
        rc, terminal_noop = self.invoke(
            "abandon",
            "--repo",
            self.repo,
            "--run",
            source_run,
            "--reason",
            "must not rewrite a superseded source",
        )
        self.assertEqual(rc, 0, terminal_noop)
        self.assertEqual(terminal_noop["phase"], "superseded")
        self.assertEqual(source_ledger_path.read_bytes(), superseded_bytes)
        self.assertEqual(target_context["phase"], "active")
        target_execution = target_context["facets"]["execution"]
        self.assertEqual(target_execution["candidate_head"], source_candidate)
        self.assertEqual(
            git(target["candidate_worktree"], "rev-parse", "HEAD"), source_candidate
        )
        self.assertEqual(
            target_execution["carryover"]["source_candidate_head"], source_candidate
        )
        carryover_paths = {
            item["path"] for item in target_execution["carryover"]["files"]
        }
        self.assertIn("src/calc.py", carryover_paths)
        self.assertEqual(target_execution["agents"], {})
        self.assertIsNone(target_execution["tester_source"])
        self.assertEqual(target_execution["tester_files"], [])
        self.assertEqual(target_context["evidence"], {})
        self.assertEqual(len(target_ledger["retired_tester_sources"]), 1)
        self.assertEqual(
            target_ledger["retired_tester_sources"][0]["agent"],
            {"agent_id": "source-tester", "thread_id": "source-tester-thread"},
        )
        self.assertEqual(target_context["lineage"]["root_run_id"], source_run)
        self.assertEqual(target_context["lineage"]["current_run_id"], target_run)
        self.assertEqual(target_context["lineage"]["revision_count"], 2)
        self.assertEqual(
            target_context["lineage"]["problem_disposition_counts"],
            {"included": 1, "handled_elsewhere": 1, "discarded": 1},
        )
        self.assertEqual(
            target_context["lineage"]["open_problem_keys"], ["carry-forward"]
        )
        self.assertEqual(
            [item["key"] for item in target_context["problems"]], ["carry-forward"]
        )
        self.assertEqual(
            {
                item["key"]: item["disposition"]
                for item in target_ledger["problem_dispositions"]
            },
            {item["key"]: item["disposition"] for item in dispositions},
        )

    def test_abandoned_source_is_rejected_before_target_or_source_mutation(self) -> None:
        source_run = "abandoned-source"
        source = self.start(source_run, base_contract())
        proposed = self.next_contract(source)

        rc, abandoned = self.invoke(
            "abandon",
            "--repo",
            self.repo,
            "--run",
            source_run,
            "--reason",
            "user cancelled without a successor",
        )
        self.assertEqual(rc, 0, abandoned)
        self.assertEqual(abandoned["phase"], "abandoned")

        source_run_path = Path(source["candidate_worktree"]).parent
        source_ledger_path = source_run_path / "ledger.json"
        source_bytes = source_ledger_path.read_bytes()
        repo_head = git(self.repo, "rev-parse", "HEAD")
        target_run = "rejected-target"
        target_run_path = source_run_path.parent / target_run
        target_worktree = target_run_path / "candidate"
        target_branch = f"refs/heads/assurance-v4/{target_run}/candidate"

        rc, rejected = self.invoke(
            "start",
            "--repo",
            self.repo,
            "--run",
            target_run,
            "--session-id",
            f"session-{target_run}",
            "--contract",
            self.write_json("rejected-target.json", proposed),
        )
        self.assertEqual(rc, 1, rejected)
        self.assertEqual(rejected["status"], "NEEDS_USER")
        self.assertEqual(rejected["code"], "SUPERSEDED_RUN_NOT_ACTIVE")
        self.assertEqual(source_ledger_path.read_bytes(), source_bytes)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), repo_head)
        self.assertFalse(target_run_path.exists())
        self.assertFalse(target_worktree.exists())

        branch = run_process(
            ["git", "-C", self.repo, "show-ref", "--verify", target_branch]
        )
        self.assertNotEqual(branch.returncode, 0, branch.stdout)
        worktrees = run_process(
            ["git", "-C", self.repo, "worktree", "list", "--porcelain"]
        )
        self.assertEqual(worktrees.returncode, 0, worktrees.stderr)
        self.assertNotIn(str(target_worktree), worktrees.stdout)


if __name__ == "__main__":
    unittest.main()
