from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from harness import CLI, cleanup_repo, fixture_runtime_env, init_repo, run_process  # noqa: E402
from tests.test_compact_profile_contract import compact_contract  # noqa: E402


def _run_assurance(repo: Path, *args: str, input_text: str | None = None):
    return run_process(
        [sys.executable, CLI, "assurance", "--experimental-v4", *args],
        cwd=repo,
        input_text=input_text,
        env=fixture_runtime_env(repo),
    )


def main() -> int:
    repo = init_repo()
    artifact = Path(tempfile.mkdtemp(prefix="nonsemantic-recovery-blackbox-"))
    try:
        contract = compact_contract()
        contract["assurance"]["profile"] = "full"
        contract["assurance"]["reviewer_preflight"] = True
        contract_path = artifact / "contract.json"
        contract_path.write_text(json.dumps(contract), encoding="utf-8")
        started = _run_assurance(
            repo,
            "start",
            "--repo",
            str(repo),
            "--run",
            "recovery-blackbox-run",
            "--session-id",
            "recovery-blackbox-session",
            "--contract",
            str(contract_path),
        )
        if started.returncode != 0:
            raise AssertionError(started.stderr or started.stdout)

        initial_next = _run_assurance(
            repo,
            "driver-next",
            "--repo",
            str(repo),
            "--run",
            "recovery-blackbox-run",
        )
        if initial_next.returncode != 0:
            raise AssertionError(initial_next.stderr or initial_next.stdout)
        initial_action = json.loads(
            [line for line in initial_next.stdout.splitlines() if line.strip()][-1]
        )
        if initial_action.get("action") == "prepare_builder":
            prepared = _run_assurance(
                repo,
                "prepare-builder",
                "--repo",
                str(repo),
                "--run",
                "recovery-blackbox-run",
                "--action-id",
                str(initial_action["action_id"]),
                "--agent-id",
                "recovery-builder",
                "--thread-id",
                "recovery-builder-thread",
            )
            if prepared.returncode != 0:
                raise AssertionError(prepared.stderr or prepared.stdout)

        report = {
            "schema_version": 1,
            "problems": [
                {
                    "key": "mechanical-recovery-fixture",
                    "summary": "The frozen assurance command needs a mechanical correction.",
                    "details": "Only one timeout value changes; mission and authority remain unchanged.",
                    "owner": "plan",
                    "decision_request": {
                        "kind": "engineering_correction",
                        "facet": "assurance",
                        "changes": [
                            {
                                "operation": "replace",
                                "pointer": "/assurance/machine_commands/0/timeout_seconds",
                                "value": 60,
                            }
                        ],
                        "question": "Apply the exact mechanical correction once?",
                    },
                }
            ],
        }
        problem_path = artifact / "problem.json"
        problem_path.write_text(json.dumps(report), encoding="utf-8")
        recorded = _run_assurance(
            repo,
            "record-problems",
            "--repo",
            str(repo),
            "--run",
            "recovery-blackbox-run",
            "--report",
            str(problem_path),
            "--role",
            "builder",
            "--agent-id",
            "recovery-builder",
            "--thread-id",
            "recovery-builder-thread",
        )
        if recorded.returncode != 0:
            raise AssertionError(recorded.stderr or recorded.stdout)

        next_action = _run_assurance(
            repo,
            "driver-next",
            "--repo",
            str(repo),
            "--run",
            "recovery-blackbox-run",
        )
        if next_action.returncode != 0:
            raise AssertionError(next_action.stderr or next_action.stdout)
        decision = json.loads([line for line in next_action.stdout.splitlines() if line.strip()][-1])
        if decision.get("status") == "NEEDS_USER" and decision.get("action") == "contract_decision":
            raise AssertionError(decision)
        if decision.get("reason") in {"mission_change", "authority_change", "assurance_downgrade"}:
            raise AssertionError(decision)
        return 0
    finally:
        cleanup_repo(repo)
        for child in sorted(artifact.glob("*"), reverse=True):
            child.unlink(missing_ok=True)
        artifact.rmdir()


if __name__ == "__main__":
    raise SystemExit(main())
