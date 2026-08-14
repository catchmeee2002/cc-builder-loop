from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from harness import CLI, ROOT, cleanup_repo, git, init_repo, run_process
from tests.test_v4_supersession_lifecycle_contract import base_contract


class AssuranceV4AdmissionContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo()
        self.tempdir = tempfile.TemporaryDirectory(prefix="assurance-v4-admission-")
        self.artifacts = Path(self.tempdir.name)

    def tearDown(self) -> None:
        cleanup_repo(self.repo)
        self.tempdir.cleanup()

    def write_json(self, name: str, value: Any) -> Path:
        path = self.artifacts / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def invoke(
        self,
        command: str,
        *args: str | Path,
        env: dict[str, str] | None = None,
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
            env=env,
        )
        lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, (completed.returncode, completed.stdout, completed.stderr))
        value = json.loads(lines[-1])
        self.assertIsInstance(value, dict)
        return completed.returncode, value

    def executable(self, name: str, *, body: str = "#!/bin/sh\nexit 0\n") -> Path:
        path = self.artifacts / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_validate_aggregates_host_commands_missing_from_trusted_path(self) -> None:
        ambient = self.executable("ambient-only-tool")
        contract = base_contract()
        contract["assurance"]["machine_commands"] = [
            {
                "id": "ambient-command",
                "argv": [ambient.name],
                "timeout_seconds": 30,
                "expected_returncodes": [0],
            },
            {
                "id": "missing-machine-command",
                "argv": ["missing-machine-admission-tool"],
                "timeout_seconds": 30,
                "expected_returncodes": [0],
            },
        ]
        contract["execution"]["commands"] = [
            {
                "id": "missing-blackbox-command",
                "argv": ["missing-blackbox-admission-tool"],
                "timeout_seconds": 30,
                "expected_returncodes": [0],
            }
        ]

        rc, blocked = self.invoke(
            "validate",
            "--repo",
            self.repo,
            "--contract",
            self.write_json("aggregate.json", contract),
            env={"PATH": f"{self.artifacts}:{os.environ.get('PATH', '')}"},
        )

        self.assertEqual(rc, 1, blocked)
        self.assertEqual(blocked["status"], "FAIL")
        self.assertEqual(blocked["code"], "ASSURANCE_ADMISSION_BLOCKED")
        admission = blocked["admission"]
        self.assertEqual(admission["status"], "BLOCKED")
        self.assertEqual(
            [item["command_id"] for item in admission["commands"]],
            [
                "ambient-command",
                "missing-machine-command",
                "missing-blackbox-command",
            ],
        )
        self.assertTrue(all(item["status"] == "blocked" for item in admission["commands"]))
        self.assertEqual(
            {item["identity"]["reason"] for item in admission["commands"]},
            {"not_found"},
        )
        self.assertFalse((self.repo / ".git" / "builder-loop-assurance-v4").exists())

    def test_absolute_executable_is_ready_and_bound_by_sha256(self) -> None:
        executable = self.executable("absolute-tool")
        contract = base_contract()
        contract["assurance"]["machine_commands"][0]["argv"] = [str(executable)]

        rc, ready = self.invoke(
            "validate",
            "--repo",
            self.repo,
            "--contract",
            self.write_json("absolute.json", contract),
        )

        self.assertEqual(rc, 0, ready)
        self.assertEqual(ready["status"], "READY")
        command = ready["admission"]["commands"][0]
        self.assertEqual(command["status"], "ready")
        self.assertEqual(command["identity"]["resolution"], "explicit_absolute")
        self.assertEqual(command["identity"]["path"], str(executable.resolve()))
        self.assertEqual(
            command["identity"]["sha256"],
            hashlib.sha256(executable.read_bytes()).hexdigest(),
        )

    def test_repository_executable_is_candidate_bound_and_deferred(self) -> None:
        contract = base_contract()
        contract["assurance"]["machine_commands"][0]["argv"] = [
            "tools/run-machine-check"
        ]

        rc, ready = self.invoke(
            "validate",
            "--repo",
            self.repo,
            "--contract",
            self.write_json("repository.json", contract),
        )

        self.assertEqual(rc, 0, ready)
        command = ready["admission"]["commands"][0]
        self.assertEqual(command["status"], "deferred")
        self.assertEqual(command["reason"], "candidate_bound")
        self.assertEqual(
            command["identity"],
            {
                "kind": "repository",
                "requested": "tools/run-machine-check",
                "path": "tools/run-machine-check",
            },
        )

    def test_start_rechecks_disappeared_host_executable_before_run_creation(self) -> None:
        executable = self.executable("disappearing-before-start")
        contract = base_contract()
        contract["assurance"]["machine_commands"][0]["argv"] = [str(executable)]
        contract_path = self.write_json("disappearing.json", contract)
        rc, ready = self.invoke(
            "validate", "--repo", self.repo, "--contract", contract_path
        )
        self.assertEqual(rc, 0, ready)
        executable.unlink()

        run_id = "admission-disappeared-before-start"
        rc, blocked = self.invoke(
            "start",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--session-id",
            "admission-disappeared-session",
            "--contract",
            contract_path,
        )

        self.assertEqual(rc, 1, blocked)
        self.assertEqual(blocked["code"], "ASSURANCE_ADMISSION_BLOCKED")
        self.assertFalse(
            (
                self.repo
                / ".git"
                / "builder-loop-assurance-v4"
                / "runs"
                / run_id
            ).exists()
        )
        self.assertEqual(
            git(
                self.repo,
                "show-ref",
                "--verify",
                f"refs/heads/assurance-v4/{run_id}/candidate",
                check=False,
            ),
            "",
        )

    def test_machine_execution_rechecks_executable_after_start(self) -> None:
        executable = self.executable("disappearing-before-machine")
        contract = copy.deepcopy(base_contract())
        contract["assurance"]["machine_commands"][0]["argv"] = [str(executable)]
        run_id = "admission-final-recheck"
        contract_path = self.write_json("final-recheck.json", contract)
        rc, started = self.invoke(
            "start",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--session-id",
            "admission-final-recheck-session",
            "--contract",
            contract_path,
        )
        self.assertEqual(rc, 0, started)
        rc, checkpointed = self.invoke(
            "checkpoint-builder", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, checkpointed)
        executable.unlink()

        rc, verified = self.invoke(
            "verify-machine", "--repo", self.repo, "--run", run_id
        )

        self.assertEqual(rc, 0, verified)
        self.assertEqual(verified["readiness"]["states"]["machine"], "failed")
        ledger_path = Path(started["candidate_worktree"]).parent / "ledger.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        result = ledger["evidence"]["machine"]["details"]["commands"][0]
        self.assertIsNone(result["returncode"])
        self.assertEqual(result["executable_identity"]["reason"], "not_found")

    def test_public_cli_blackbox_helper_observes_admission_contract(self) -> None:
        completed = run_process(
            [
                sys.executable,
                ROOT / "tests" / "helpers" / "assurance_admission_blackbox.py",
                "--cli",
                CLI,
            ]
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        result = json.loads(completed.stdout.splitlines()[-1])
        self.assertEqual(result["result"], "pass")
        self.assertEqual(
            [item["case"] for item in result["cases"]],
            ["trusted-path-blocked", "absolute-ready"],
        )


if __name__ == "__main__":
    unittest.main()
