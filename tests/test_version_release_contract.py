from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from harness import CLI, cleanup_repo, init_repo, run_process
from runtime.codex_builder_loop.assurance_v4 import core
from tests.test_compact_profile_contract import compact_contract


class VersionReleaseContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo()

    def tearDown(self) -> None:
        cleanup_repo(self.repo)

    def test_version_json_reports_semver_and_runtime_identity(self) -> None:
        completed = run_process([sys.executable, CLI, "version", "--json"])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, completed.stdout)
        value = json.loads(lines[-1])
        self.assertEqual(value.get("version") or value.get("builder_loop_version"), "0.1.0")
        identity = value.get("runtime_identity")
        self.assertIsInstance(identity, dict, value)
        self.assertEqual(identity.get("builder_loop_version"), "0.1.0")
        self.assertIn(identity.get("capture_status"), {"captured", "partial", "unavailable"})
        self.assertIn("compatibility", value)

    def test_new_assurance_ledger_freezes_runtime_version(self) -> None:
        started = core.start(
            self.repo,
            "version-runtime-run",
            "version-runtime-session",
            compact_contract(),
        )
        identity = started["runtime_identity"]
        self.assertEqual(identity["builder_loop_version"], "0.1.0")
        self.assertRegex(identity["adapter_commit"], r"^[0-9a-f]{40}$")

    def test_version_command_survives_missing_git_metadata(self) -> None:
        with tempfile.TemporaryDirectory(prefix="builder-loop-version-home-") as raw:
            home = Path(raw)
            env = {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "GIT_DIR": str(home / "missing-git-dir"),
            }
            completed = run_process(
                [sys.executable, CLI, "version", "--json"],
                cwd=home,
                env=env,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        value = json.loads([line for line in completed.stdout.splitlines() if line.strip()][-1])
        self.assertEqual(value.get("version") or value.get("builder_loop_version"), "0.1.0")
        identity = value["runtime_identity"]
        self.assertEqual(identity["builder_loop_version"], "0.1.0")
        self.assertIn(identity["capture_status"], {"partial", "unavailable"})
        self.assertIsNone(identity.get("adapter_commit"))

    def test_install_smoke_reads_back_same_version(self) -> None:
        with tempfile.TemporaryDirectory(prefix="builder-loop-install-version-") as raw:
            home = Path(raw)
            env = {
                **os.environ,
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PATH": f"{home / '.local' / 'bin'}:{os.environ.get('PATH', '')}",
            }
            installed = run_process(["bash", Path(__file__).parents[1] / "install.sh"], env=env)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            cli = home / ".local" / "bin" / "codex-builder-loop"
            version = run_process([str(cli), "version", "--json"], env=env)
            self.assertEqual(version.returncode, 0, version.stderr)
            value = json.loads([line for line in version.stdout.splitlines() if line.strip()][-1])
            self.assertEqual(value.get("version") or value.get("builder_loop_version"), "0.1.0")


if __name__ == "__main__":
    unittest.main()
