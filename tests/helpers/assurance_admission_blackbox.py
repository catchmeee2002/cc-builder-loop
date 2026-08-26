#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from harness import add_v4_progress_contract  # noqa: E402


def run(
    argv: Sequence[str | Path], *, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(item) for item in argv],
        cwd=str(cwd) if cwd is not None else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def git(repo: Path, *args: str) -> str:
    completed = run(["git", "-C", repo, *args])
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    return completed.stdout.strip()


def contract(executable: str) -> dict[str, Any]:
    value = {
        "schema_version": 4,
        "mission": {
            "delivery_kind": "code",
            "revision": 1,
            "supersedes": None,
            "objective": "Observe Assurance admission through the public CLI.",
            "behaviors": [
                {
                    "id": "admission",
                    "description": "Unavailable host tools block before run creation.",
                }
            ],
            "interfaces": [],
            "acceptance_cases": [
                {
                    "id": "host-tool",
                    "description": "The public validate command reports executable identity.",
                }
            ],
            "trust_boundaries": [
                {
                    "id": "trusted-path",
                    "description": "Ambient PATH is not an executable authority.",
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
                    "id": "admission-command",
                    "argv": [executable],
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
    return add_v4_progress_contract(value)


def invoke(
    cli: Path,
    repo: Path,
    value: dict[str, Any],
    path: Path,
) -> tuple[int, dict[str, Any]]:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    completed = run(
        [
            cli,
            "assurance",
            "--experimental-v4",
            "validate",
            "--repo",
            repo,
            "--contract",
            path,
        ]
    )
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(completed.stderr.strip() or "public CLI returned no JSON")
    return completed.returncode, json.loads(lines[-1])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", default=str(ROOT / "scripts" / "codex-builder-loop.py"))
    args = parser.parse_args(argv)
    cli = Path(args.cli).expanduser().resolve()
    observations: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="assurance-admission-blackbox-") as raw:
        root = Path(raw)
        repo = root / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "admission-blackbox@test.local")
        git(repo, "config", "user.name", "admission blackbox")
        (repo / "README.md").write_text("admission blackbox\n", encoding="utf-8")
        git(repo, "add", "README.md")
        git(
            repo,
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-q",
            "-m",
            "test(admission): [cr_id_skip] Seed Fixture",
        )

        missing = "assurance-admission-blackbox-missing-tool"
        rc, blocked = invoke(cli, repo, contract(missing), root / "blocked.json")
        if (
            rc != 1
            or blocked.get("status") != "FAIL"
            or blocked.get("code") != "ASSURANCE_ADMISSION_BLOCKED"
            or blocked.get("admission", {}).get("commands", [{}])[0].get("status")
            != "blocked"
        ):
            raise RuntimeError(f"missing executable admission mismatch: {blocked}")
        observations.append({"case": "trusted-path-blocked", "code": blocked["code"]})

        executable = root / "absolute-tool"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        rc, ready = invoke(cli, repo, contract(str(executable)), root / "ready.json")
        command = ready.get("admission", {}).get("commands", [{}])[0]
        expected_sha256 = hashlib.sha256(executable.read_bytes()).hexdigest()
        if (
            rc != 0
            or ready.get("status") != "READY"
            or command.get("status") != "ready"
            or command.get("identity", {}).get("resolution") != "explicit_absolute"
            or command.get("identity", {}).get("sha256") != expected_sha256
        ):
            raise RuntimeError(f"absolute executable admission mismatch: {ready}")
        observations.append(
            {
                "case": "absolute-ready",
                "sha256": expected_sha256,
                "path": str(executable),
            }
        )

        state_root = repo / ".git" / "builder-loop-assurance-v4"
        if state_root.exists():
            raise RuntimeError("validate admission created persistent run state")

    print(
        json.dumps(
            {
                "result": "pass",
                "cases": observations,
                "trusted_path": os.pathsep.join(
                    ["/usr/local/bin", "/usr/bin", "/bin"]
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
