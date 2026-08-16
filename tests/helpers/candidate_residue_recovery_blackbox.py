from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence


sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[2]


def require(condition: bool, message: str, value: Any = None) -> None:
    if not condition:
        suffix = "" if value is None else f": {value!r}"
        raise AssertionError(message + suffix)


def run(
    argv: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path,
    input_text: str | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[int, str, str]:
    import subprocess

    child_env = os.environ.copy()
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    if env:
        child_env.update({str(key): str(value) for key, value in env.items()})
    completed = subprocess.run(
        [str(value) for value in argv],
        cwd=cwd,
        env=child_env,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return completed.returncode, completed.stdout, completed.stderr


def git(repo: Path, *args: str) -> str:
    returncode, stdout, stderr = run(["git", "-C", repo, *args], cwd=repo)
    require(returncode == 0, f"git {' '.join(args)} failed", stderr or stdout)
    return stdout.strip()


def clone_snapshot(destination: Path) -> str:
    returncode, stdout, stderr = run(
        ["git", "clone", "--no-hardlinks", "--quiet", ROOT, destination], cwd=ROOT
    )
    require(returncode == 0, "snapshot clone failed", stderr or stdout)
    hooks = destination / ".git/blackbox-hooks"
    hooks.mkdir(parents=True)
    git(destination, "config", "core.hooksPath", str(hooks))
    patch = git(ROOT, "diff", "--binary", "HEAD")
    if patch:
        returncode, stdout, stderr = run(
            ["git", "-C", destination, "apply", "--whitespace=nowarn", "-"],
            cwd=destination,
            input_text=patch,
        )
        require(returncode == 0, "snapshot patch failed", stderr or stdout)
    for relative in (
        item
        for item in git(ROOT, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
        if item
    ):
        source = ROOT / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    git(destination, "config", "user.email", "candidate-residue@test.local")
    git(destination, "config", "user.name", "Candidate Residue Blackbox")
    git(destination, "add", "-A")
    if git(destination, "status", "--porcelain=v1"):
        git(
            destination,
            "commit",
            "-q",
            "-m",
            "test(assurance): [cr_id_skip] Freeze Candidate Residue Snapshot",
        )
    return git(destination, "rev-parse", "HEAD")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="candidate-residue-blackbox-") as raw:
        repo = Path(raw) / "repo"
        snapshot_head = clone_snapshot(repo)
        returncode, stdout, stderr = run(
            [
                sys.executable,
                "-m",
                "unittest",
                "tests.test_candidate_residue_recovery_contract",
                "tests.test_native_app_server_cache_contract",
                "-v",
            ],
            cwd=repo,
        )
        require(returncode == 0, "public residue/cache scenarios failed", stderr or stdout)
        require(git(repo, "rev-parse", "HEAD") == snapshot_head, "snapshot HEAD moved")
        require(not git(repo, "status", "--porcelain=v1"), "snapshot became dirty")
        print(
            json.dumps(
                {
                    "status": "pass",
                    "observations": {
                        "candidate_residue": "exact recovery, rejection, replay, and terminal cleanup passed",
                        "native_cache": "explicit py_compile stayed outside the role worktree",
                    },
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
