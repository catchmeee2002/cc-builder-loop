from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from harness import CLI, cleanup_repo, commit_all, git, init_repo, run_process
from test_assurance_v4_contract import contract_for
from runtime.codex_builder_loop.assurance_v4.models import digest


class DriverFailureContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo()
        self.tempdir = tempfile.TemporaryDirectory(prefix="driver-failure-contract-")
        self.artifacts = Path(self.tempdir.name)

    def tearDown(self) -> None:
        cleanup_repo(self.repo)
        self.tempdir.cleanup()

    def invoke(
        self,
        command: str,
        *args: str | Path,
        input_value: object | None = None,
    ) -> tuple[int, dict[str, Any]]:
        completed = run_process(
            [
                sys.executable,
                CLI,
                "assurance",
                "--experimental-v4",
                command,
                *args,
            ],
            input_text=(
                json.dumps(input_value, ensure_ascii=False)
                if input_value is not None
                else None
            ),
        )
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        self.assertTrue(
            lines,
            f"command={command!r} rc={completed.returncode} "
            f"stdout={completed.stdout!r} stderr={completed.stderr!r}",
        )
        value = json.loads(lines[-1])
        self.assertIsInstance(value, dict, value)
        return completed.returncode, value

    def start(
        self,
        run_id: str,
        *,
        native: bool = False,
    ) -> tuple[dict[str, Any], Path]:
        contract = contract_for(self.repo)
        contract["execution"]["driver_enforced"] = native
        contract_path = self.artifacts / f"{run_id}-contract.json"
        contract_path.write_text(
            json.dumps(contract, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        args: list[str | Path] = [
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--session-id",
            f"{run_id}-session",
            "--contract",
            contract_path,
        ]
        if native:
            args.extend(
                [
                    "--driver-kind",
                    "native",
                    "--driver-transport",
                    "codex_app_server",
                    "--driver-runtime-version",
                    "codex-test",
                    "--driver-protocol-schema-digest",
                    "a" * 64,
                ]
            )
        rc, started = self.invoke("start", *args)
        self.assertEqual(rc, 0, started)
        return started, Path(started["candidate_worktree"]).parent

    @staticmethod
    def failure(action: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "source": "native_driver",
            "status": "FATAL",
            "code": "NATIVE_FIXTURE_FATAL",
            "message": "The Native Driver fixture stopped unexpectedly.",
            "details": {"fixture": "driver-failure-contract"},
            "action": action,
        }

    def record_problem(self, run_id: str, owner: str) -> None:
        rc, recorded = self.invoke(
            "record-problems",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--report",
            "-",
            "--role",
            "builder",
            "--agent-id",
            f"{run_id}-builder",
            "--thread-id",
            f"{run_id}-builder-thread",
            input_value={
                "schema_version": 1,
                "problems": [
                    {
                        "key": f"{owner.replace('_', '-')}-problem",
                        "summary": f"A {owner} problem is open.",
                        "details": "The Driver must route this owner without guessing.",
                        "owner": owner,
                    }
                ],
            },
        )
        self.assertEqual(rc, 0, recorded)

    def test_problem_owners_are_exhaustive_and_blocking_owners_never_dispatch_agents(
        self,
    ) -> None:
        expected = {
            "builder": ("CONTINUE", "builder_fix"),
            "tester": ("CONTINUE", "tester_fix"),
            "plan": ("NEEDS_USER", "contract_decision"),
            "external_platform": ("NEEDS_USER", "external_problem_decision"),
            "builder_loop": ("NEEDS_USER", "builder_loop_problem_decision"),
            "current_project": ("NEEDS_USER", "current_project_problem_decision"),
        }
        for owner, (status, action) in expected.items():
            with self.subTest(owner=owner):
                run_id = f"owner-route-{owner.replace('_', '-')}"
                started, _run_path = self.start(run_id)
                rc, checkpointed = self.invoke(
                    "checkpoint-builder", "--repo", self.repo, "--run", run_id
                )
                self.assertEqual(rc, 0, checkpointed)
                self.record_problem(run_id, owner)

                if owner == "builder_loop":
                    candidate = Path(started["candidate_worktree"])
                    (candidate / "src" / "calc.py").write_text(
                        "def add(a, b):\n    return a + b\n\nCHECKPOINT_FIRST = True\n",
                        encoding="utf-8",
                    )
                    commit_all(candidate, "checkpoint before builder-loop decision")
                    rc, pending_checkpoint = self.invoke(
                        "driver-next", "--repo", self.repo, "--run", run_id
                    )
                    self.assertEqual(rc, 0, pending_checkpoint)
                    self.assertEqual(
                        pending_checkpoint.get("action"), "checkpoint_builder"
                    )
                    rc, checkpointed = self.invoke(
                        "checkpoint-builder", "--repo", self.repo, "--run", run_id
                    )
                    self.assertEqual(rc, 0, checkpointed)

                rc, decision = self.invoke(
                    "driver-next", "--repo", self.repo, "--run", run_id
                )
                self.assertEqual(rc, 0, decision)
                self.assertEqual(decision.get("status"), status, decision)
                self.assertEqual(decision.get("action"), action, decision)
                if owner in {"builder_loop", "current_project"}:
                    self.assertNotIn(
                        decision.get("action"),
                        {"builder_implement", "builder_fix", "tester_author", "tester_fix"},
                    )

    def test_driver_failure_replay_terminal_status_and_cleanup_are_fail_closed(
        self,
    ) -> None:
        run_id = "driver-failure-terminal"
        started, run_path = self.start(run_id, native=True)
        current = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )[1]
        failure = self.failure(
            {
                "action_id": current["action_id"],
                "action": current["action"],
                "reason": current["reason"],
            }
        )

        rc, recorded = self.invoke(
            "record-driver-failure",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--failure",
            "-",
            "--driver-runtime-kind",
            "native",
            input_value=failure,
        )
        self.assertEqual(rc, 0, recorded)
        self.assertEqual(recorded["phase"], "active")
        self.assertEqual(recorded["driver_failure"]["state"], "recorded")
        self.assertEqual(recorded["driver_failure"]["recovery"], "none")
        self.assertEqual(
            recorded["driver_failure"]["observation"]["candidate_worktree"],
            started["candidate_worktree"],
        )
        recorded_bytes = (run_path / "ledger.json").read_bytes()

        rc, replayed = self.invoke(
            "record-driver-failure",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--failure",
            "-",
            "--driver-runtime-kind",
            "native",
            input_value=failure,
        )
        self.assertEqual(rc, 0, replayed)
        self.assertEqual((run_path / "ledger.json").read_bytes(), recorded_bytes)

        conflict = dict(failure)
        conflict["message"] = "A conflicting fatal replay."
        rc, rejected = self.invoke(
            "record-driver-failure",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--failure",
            "-",
            "--driver-runtime-kind",
            "native",
            input_value=conflict,
        )
        self.assertNotEqual(rc, 0, rejected)
        self.assertEqual(rejected.get("code"), "DRIVER_FAILURE_CONFLICT")
        self.assertEqual((run_path / "ledger.json").read_bytes(), recorded_bytes)

        rc, recovery = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, recovery)
        self.assertEqual(recovery.get("action"), "complete_driver_failure")

        rc, failed = self.invoke(
            "complete-driver-failure",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--driver-runtime-kind",
            "native",
        )
        self.assertEqual(rc, 0, failed)
        self.assertEqual(failed.get("status"), "FATAL", failed)
        self.assertEqual(failed.get("phase"), "failed", failed)
        self.assertEqual(failed["driver_failure"]["state"], "terminal")

        rc, stopped = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, stopped)
        self.assertEqual(stopped.get("status"), "STOP", stopped)
        self.assertEqual(stopped.get("reason"), "failed", stopped)

        rc, retrospective = self.invoke(
            "retrospective-status",
            "--repo",
            self.repo,
            "--session-id",
            f"{run_id}-session",
        )
        self.assertEqual(rc, 0, retrospective)
        self.assertEqual(retrospective.get("status"), "REQUIRED", retrospective)
        run_fact = retrospective["snapshot"]["runs"][0]
        self.assertEqual(run_fact["phase"], "failed")
        self.assertEqual(run_fact["terminal_status"], "fatal")

        successor = contract_for(self.repo)
        successor["mission"]["revision"] = 2
        successor["mission"]["supersedes"] = {
            "run_id": run_id,
            "revision": failed["mission_revision"],
            "mission_digest": failed["digests"]["mission"],
            "candidate_head": git(Path(started["candidate_worktree"]), "rev-parse", "HEAD"),
        }
        successor["execution"]["revision_transition"] = {
            "category": "execution_contract",
            "predecessor_pressure_digest": failed["lineage"]["pressure_digest"],
        }
        successor["execution"]["prior_problem_dispositions"] = {
            "source_snapshot_digest": failed["lineage"][
                "open_problem_snapshot_digest"
            ],
            "items": [],
        }
        successor_path = self.artifacts / "failed-successor-contract.json"
        successor_path.write_text(
            json.dumps(successor, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        rc, rejected_successor = self.invoke(
            "start",
            "--repo",
            self.repo,
            "--run",
            "failed-successor",
            "--session-id",
            "failed-successor-session",
            "--contract",
            successor_path,
        )
        self.assertNotEqual(rc, 0, rejected_successor)
        self.assertEqual(
            rejected_successor.get("code"), "SUPERSEDED_RUN_NOT_ACTIVE"
        )

        rc, abandoned = self.invoke(
            "abandon",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--reason",
            "must not rewrite failed",
        )
        self.assertNotEqual(rc, 0, abandoned)
        self.assertEqual(abandoned.get("code"), "ASSURANCE_RUN_FAILED")

        candidate = Path(started["candidate_worktree"])
        rc, cleaned = self.invoke(
            "cleanup", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, cleaned)
        self.assertFalse(candidate.exists())
        rc, replayed_cleanup = self.invoke(
            "cleanup", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, replayed_cleanup)

    def test_failed_cleanup_preserves_dirty_failure_observation(self) -> None:
        run_id = "driver-failure-dirty-cleanup"
        started, _run_path = self.start(run_id, native=True)
        candidate = Path(started["candidate_worktree"])
        residue = candidate / "fatal-residue.tmp"
        residue.write_text("preserve me\n", encoding="utf-8")

        rc, recorded = self.invoke(
            "record-driver-failure",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--failure",
            "-",
            "--driver-runtime-kind",
            "native",
            input_value=self.failure(),
        )
        self.assertEqual(rc, 0, recorded)
        self.assertEqual(
            recorded["driver_failure"]["observation"]["candidate_dirty_paths"],
            ["fatal-residue.tmp"],
        )
        rc, failed = self.invoke(
            "complete-driver-failure",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--driver-runtime-kind",
            "native",
        )
        self.assertEqual(rc, 0, failed)

        rc, blocked = self.invoke(
            "cleanup", "--repo", self.repo, "--run", run_id
        )
        self.assertNotEqual(rc, 0, blocked)
        self.assertEqual(blocked.get("code"), "ASSURANCE_CLEANUP_DRIFT")
        self.assertTrue(residue.is_file())

    def test_builder_side_effect_blocks_retry_and_persists_exact_manifest(self) -> None:
        run_id = "builder-side-effect-failure"
        started, run_path = self.start(run_id, native=True)
        first = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)[1]
        self.invoke(
            "prepare-builder",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--agent-id",
            "native-builder",
            "--thread-id",
            "native-builder-thread",
            "--action-id",
            first["action_id"],
            "--driver-runtime-kind",
            "native",
        )
        action = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)[1]
        self.invoke(
            "begin-dispatch",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action["action_id"],
            "--action",
            action["action"],
            "--role",
            "builder",
            "--thread-id",
            "native-builder-thread",
            "--prompt-digest",
            "b" * 64,
            "--output-schema-digest",
            "c" * 64,
            "--driver-runtime-kind",
            "native",
        )
        self.invoke(
            "bind-dispatch-turn",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action["action_id"],
            "--turn-id",
            "builder-turn-1",
        )

        candidate = Path(started["candidate_worktree"])
        changed = candidate / "src" / "changed.py"
        changed.write_text("SIDE_EFFECT = True\n", encoding="utf-8")
        retry_rc, retry = self.invoke(
            "retry-dispatch",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action["action_id"],
            "--failure-code",
            "responseStreamDisconnected",
        )
        self.assertNotEqual(retry_rc, 0, retry)
        self.assertEqual(retry["code"], "NATIVE_BUILDER_SIDE_EFFECT_RETRY_BLOCKED")

        content = changed.read_bytes()
        receipt = {
            "schema_version": 1,
            "failure_code": "responseStreamDisconnected",
            "transport_generation": "transport-builder-1",
            "stderr_bytes": 9,
            "stderr_sha256": hashlib.sha256(b"stderr\\n").hexdigest(),
            "stderr_summary": "stderr",
            "stderr_truncated": False,
            "redaction_count": 0,
            "turn_error": {"codexErrorInfo": {"responseStreamDisconnected": {}}},
            "cleanup_observation": None,
        }
        receipt["receipt_digest"] = digest(receipt)
        failure = self.failure(
            {
                "action_id": action["action_id"],
                "action": action["action"],
                "reason": action["reason"],
            }
        )
        failure["diagnostic_receipt"] = receipt
        record_rc, recorded = self.invoke(
            "record-driver-failure",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--failure",
            "-",
            "--driver-runtime-kind",
            "native",
            input_value=failure,
        )
        self.assertEqual(record_rc, 0, recorded)
        observation = recorded["driver_failure"]["observation"]
        manifest = observation["candidate_manifest"]
        entry = next(item for item in manifest["entries"] if item["path"] == "src/changed.py")
        self.assertEqual(entry["status"], "present")
        self.assertEqual(entry["size_bytes"], len(content))
        self.assertEqual(entry["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(
            manifest["manifest_digest"],
            digest({key: value for key, value in manifest.items() if key != "manifest_digest"}),
        )
        self.assertEqual(
            recorded["driver_failure"]["diagnostic_receipt"]["receipt_digest"],
            receipt["receipt_digest"],
        )

        terminal_rc, terminal = self.invoke(
            "complete-driver-failure",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--driver-runtime-kind",
            "native",
        )
        self.assertEqual(terminal_rc, 0, terminal)
        self.assertEqual(terminal["phase"], "failed")
        ledger = json.loads((run_path / "ledger.json").read_text())
        self.assertEqual(ledger["driver_failure"]["state"], "terminal")


if __name__ == "__main__":
    unittest.main()
