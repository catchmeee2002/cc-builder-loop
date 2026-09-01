from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

from harness import CLI, run_process  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="version-identity-blackbox-") as raw:
        home = Path(raw)
        environment = {
            **os.environ,
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "GIT_DIR": str(home / "missing-git-dir"),
        }
        completed = run_process(
            [sys.executable, CLI, "version", "--json"],
            cwd=home,
            env=environment,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr or completed.stdout)
        value = json.loads([line for line in completed.stdout.splitlines() if line.strip()][-1])
        if (value.get("version") or value.get("builder_loop_version")) != "0.1.2":
            raise AssertionError(value)
        identity = value.get("runtime_identity")
        if not isinstance(identity, dict) or identity.get("builder_loop_version") != "0.1.2":
            raise AssertionError(value)
        if identity.get("capture_status") not in {"partial", "unavailable"}:
            raise AssertionError(value)
        if identity.get("adapter_commit") is not None:
            raise AssertionError(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
