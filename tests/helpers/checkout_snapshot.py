from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def _run(
    argv: list[str | os.PathLike[str]],
    *,
    cwd: Path,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [str(value) for value in argv],
        cwd=cwd,
        env=environment,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _require_ok(completed: subprocess.CompletedProcess[str], message: str) -> str:
    if completed.returncode != 0:
        detail = completed.stderr or completed.stdout
        raise AssertionError(f"{message}: {detail!r}")
    return completed.stdout


def _git(repo: Path, *args: str, input_text: str | None = None) -> str:
    return _require_ok(
        _run(["git", "-C", repo, *args], cwd=repo, input_text=input_text),
        f"git {' '.join(args)} failed",
    )


def clone_checkout_snapshot(
    source: Path,
    destination: Path,
    *,
    user_email: str,
    user_name: str,
    commit_message: str,
    branch_name: str | None = None,
    disable_gc: bool = False,
) -> str:
    source = source.resolve()
    _require_ok(
        _run(
            ["git", "clone", "--no-hardlinks", "--quiet", source, destination],
            cwd=source,
        ),
        "snapshot clone failed",
    )
    hooks = destination / ".git" / "blackbox-hooks"
    hooks.mkdir(parents=True)
    _git(destination, "config", "core.hooksPath", str(hooks))
    _git(destination, "config", "user.email", user_email)
    _git(destination, "config", "user.name", user_name)
    if disable_gc:
        _git(destination, "config", "gc.auto", "0")
        _git(destination, "config", "maintenance.auto", "false")

    patch = _git(source, "diff", "--binary", "--no-ext-diff", "HEAD", "--")
    if patch:
        _require_ok(
            _run(
                ["git", "-C", destination, "apply", "--whitespace=nowarn", "-"],
                cwd=destination,
                input_text=patch,
            ),
            "snapshot patch failed",
        )

    untracked = _git(
        source,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
    )
    for relative in (item for item in untracked.split("\0") if item):
        source_path = source / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source_path.is_symlink():
            target.symlink_to(os.readlink(source_path))
        else:
            shutil.copy2(source_path, target)

    _git(destination, "add", "-A")
    if _git(destination, "status", "--porcelain=v1"):
        _git(
            destination,
            "-c",
            "commit.gpgSign=false",
            "commit",
            "-q",
            "-m",
            commit_message,
        )
    if branch_name is not None:
        _git(destination, "branch", "-M", branch_name)
    if _git(destination, "status", "--porcelain=v1"):
        raise AssertionError("snapshot clone is dirty after commit")
    return _git(destination, "rev-parse", "HEAD").strip()
