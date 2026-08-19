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


def main() -> int:
    repo = init_repo()
    artifact = Path(tempfile.mkdtemp(prefix="compact-profile-blackbox-"))
    try:
        contract_path = artifact / "compact.json"
        contract_path.write_text(json.dumps(compact_contract()), encoding="utf-8")
        command = [
            sys.executable,
            CLI,
            "assurance",
            "--experimental-v4",
            "validate",
            "--repo",
            str(repo),
            "--contract",
            str(contract_path),
        ]
        environment = fixture_runtime_env(repo)
        validated = run_process(command, env=environment)
        if validated.returncode != 0:
            raise AssertionError(validated.stderr or validated.stdout)
        value = json.loads([line for line in validated.stdout.splitlines() if line.strip()][-1])
        if value.get("status") != "READY" or value.get("admission", {}).get("status") != "READY":
            raise AssertionError(value)

        started = run_process(
            [
                sys.executable,
                CLI,
                "assurance",
                "--experimental-v4",
                "start",
                "--repo",
                str(repo),
                "--run",
                "compact-blackbox-run",
                "--session-id",
                "compact-blackbox-session",
                "--contract",
                str(contract_path),
            ],
            env=environment,
        )
        if started.returncode != 0:
            raise AssertionError(started.stderr or started.stdout)
        started_value = json.loads([line for line in started.stdout.splitlines() if line.strip()][-1])
        profile = started_value.get("telemetry", {}).get("profile")
        if profile != {"requested": "compact", "effective": "compact", "escalation_reason": None}:
            raise AssertionError(started_value)
        return 0
    finally:
        cleanup_repo(repo)
        for child in sorted(artifact.glob("*"), reverse=True):
            child.unlink(missing_ok=True)
        artifact.rmdir()


if __name__ == "__main__":
    raise SystemExit(main())
