from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from harness import ROOT, run_process


class InstallContractTest(unittest.TestCase):
    def installed_links(self, home: Path) -> dict[Path, Path]:
        codex_home = home / ".codex"
        return {
            home / ".agents" / "skills" / "builder-loop-planner": ROOT
            / "skills"
            / "builder-loop-planner",
            home / ".agents" / "skills" / "builder": ROOT / "skills" / "builder",
            codex_home / "agents" / "tester.toml": ROOT / "agents" / "tester.toml",
            codex_home / "agents" / "reviewer.toml": ROOT / "agents" / "reviewer.toml",
            codex_home / "hooks" / "builder-loop.py": ROOT
            / "hooks"
            / "builder-loop.py",
            home / ".local" / "bin" / "codex-builder-loop": ROOT
            / "scripts"
            / "codex-builder-loop.py",
            codex_home / "builder-loop" / "doc-policy.md": ROOT
            / "policies"
            / "doc-policy.md",
        }

    def environment(self, home: Path) -> dict[str, str]:
        return {
            **os.environ,
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PATH": f"{home / '.local' / 'bin'}:{os.environ.get('PATH', '')}",
        }

    def test_install_is_idempotent_and_uninstall_preserves_foreign_content(self) -> None:
        with tempfile.TemporaryDirectory(prefix="builder-loop-install-") as raw_home:
            home = Path(raw_home)
            codex_home = home / ".codex"
            codex_home.mkdir(parents=True)
            hooks_path = codex_home / "hooks.json"
            hooks_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {"type": "command", "command": "python3 /foreign/hook.py"}
                                    ]
                                }
                            ]
                        }
                    }
                )
            )
            agents_path = codex_home / "AGENTS.md"
            agents_path.write_text("# User guidance\n")
            env = self.environment(home)

            for _ in range(2):
                installed = run_process(["bash", ROOT / "install.sh"], cwd=ROOT, env=env)
                self.assertEqual(installed.returncode, 0, installed.stderr)

            hooks = json.loads(hooks_path.read_text())["hooks"]
            self.assertIn("python3 /foreign/hook.py", json.dumps(hooks))
            for event in ("SessionStart", "SubagentStart", "SubagentStop", "Stop"):
                managed = [
                    entry
                    for entry in hooks.get(event, [])
                    if "builder-loop.py" in json.dumps(entry)
                ]
                self.assertEqual(len(managed), 1, (event, hooks))
            agents = agents_path.read_text()
            self.assertIn("# User guidance", agents)
            self.assertEqual(agents.count("BEGIN cc-builder-loop-codex"), 1)
            self.assertIn("request_user_input", agents)
            self.assertIn("Codex 原生 Plan", agents)
            self.assertIn("Builder-loop Planner", agents)
            self.assertNotIn("使用 `/plan` 为后续交付制定方案时，必须调用", agents)
            policy = codex_home / "builder-loop" / "doc-policy.md"
            self.assertTrue(policy.is_symlink())
            self.assertEqual(policy.resolve(), (ROOT / "policies" / "doc-policy.md").resolve())

            uninstalled = run_process(["bash", ROOT / "uninstall.sh"], cwd=ROOT, env=env)
            self.assertEqual(uninstalled.returncode, 0, uninstalled.stderr)
            remaining_hooks = json.loads(hooks_path.read_text())
            self.assertIn("python3 /foreign/hook.py", json.dumps(remaining_hooks))
            self.assertEqual(agents_path.read_text(), "# User guidance\n")
            self.assertFalse((home / ".agents" / "skills" / "builder").exists())
            self.assertFalse(policy.exists())

    def test_install_refuses_foreign_builder_symlink(self) -> None:
        with tempfile.TemporaryDirectory(prefix="builder-loop-foreign-") as raw_home:
            home = Path(raw_home)
            foreign = home / "foreign-builder"
            foreign.mkdir(parents=True)
            target = home / ".agents" / "skills" / "builder"
            target.parent.mkdir(parents=True)
            target.symlink_to(foreign)

            installed = run_process(
                ["bash", ROOT / "install.sh"],
                cwd=ROOT,
                env=self.environment(home),
            )
            self.assertNotEqual(installed.returncode, 0)
            self.assertIn("foreign symlink", installed.stderr)
            self.assertEqual(target.resolve(), foreign.resolve())

    def test_uninstall_does_not_remove_registration_after_hook_takeover(self) -> None:
        with tempfile.TemporaryDirectory(prefix="builder-loop-takeover-") as raw_home:
            home = Path(raw_home)
            env = self.environment(home)
            installed = run_process(["bash", ROOT / "install.sh"], cwd=ROOT, env=env)
            self.assertEqual(installed.returncode, 0, installed.stderr)

            hook_link = home / ".codex" / "hooks" / "builder-loop.py"
            foreign_hook = home / "foreign-hook.py"
            foreign_hook.write_text("print('foreign')\n")
            hook_link.unlink()
            hook_link.symlink_to(foreign_hook)

            uninstalled = run_process(["bash", ROOT / "uninstall.sh"], cwd=ROOT, env=env)
            self.assertEqual(uninstalled.returncode, 0, uninstalled.stderr)
            self.assertTrue(hook_link.is_symlink())
            self.assertEqual(hook_link.resolve(), foreign_hook.resolve())
            hooks = json.loads((home / ".codex" / "hooks.json").read_text())
            self.assertIn("builder-loop.py", json.dumps(hooks))

    def test_install_invalid_hooks_json_creates_no_partial_links(self) -> None:
        with tempfile.TemporaryDirectory(prefix="builder-loop-invalid-install-") as raw_home:
            home = Path(raw_home)
            codex_home = home / ".codex"
            codex_home.mkdir(parents=True)
            hooks_path = codex_home / "hooks.json"
            hooks_path.write_text("{not-json\n")

            installed = run_process(
                ["bash", ROOT / "install.sh"],
                cwd=ROOT,
                env=self.environment(home),
            )

            self.assertNotEqual(installed.returncode, 0)
            self.assertIn("invalid Codex hooks JSON", installed.stderr)
            self.assertEqual(hooks_path.read_text(), "{not-json\n")
            for target in self.installed_links(home):
                self.assertFalse(target.is_symlink(), target)
                self.assertFalse(target.exists(), target)

    def test_install_rejects_nonempty_global_agents_override_before_linking(self) -> None:
        with tempfile.TemporaryDirectory(prefix="builder-loop-agents-override-") as raw_home:
            home = Path(raw_home)
            codex_home = home / ".codex"
            codex_home.mkdir(parents=True)
            agents_path = codex_home / "AGENTS.md"
            agents_path.write_text("# Base guidance\n")
            override_path = codex_home / "AGENTS.override.md"
            override_path.write_text("# Temporary override\n")

            installed = run_process(
                ["bash", ROOT / "install.sh"],
                cwd=ROOT,
                env=self.environment(home),
            )

            self.assertNotEqual(installed.returncode, 0)
            self.assertIn("shadows AGENTS.md", installed.stderr)
            self.assertEqual(agents_path.read_text(), "# Base guidance\n")
            self.assertEqual(override_path.read_text(), "# Temporary override\n")
            self.assertFalse((codex_home / "hooks.json").exists())
            for target in self.installed_links(home):
                self.assertFalse(target.is_symlink(), target)
                self.assertFalse(target.exists(), target)

    def test_empty_global_agents_override_does_not_shadow_install(self) -> None:
        with tempfile.TemporaryDirectory(prefix="builder-loop-empty-override-") as raw_home:
            home = Path(raw_home)
            codex_home = home / ".codex"
            codex_home.mkdir(parents=True)
            (codex_home / "AGENTS.override.md").write_text(" \n")

            installed = run_process(
                ["bash", ROOT / "install.sh"],
                cwd=ROOT,
                env=self.environment(home),
            )

            self.assertEqual(installed.returncode, 0, installed.stderr)
            agents = (codex_home / "AGENTS.md").read_text()
            self.assertIn("BEGIN cc-builder-loop-codex", agents)

    def test_uninstall_invalid_hooks_json_removes_no_links(self) -> None:
        with tempfile.TemporaryDirectory(prefix="builder-loop-invalid-uninstall-") as raw_home:
            home = Path(raw_home)
            env = self.environment(home)
            installed = run_process(["bash", ROOT / "install.sh"], cwd=ROOT, env=env)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            hooks_path = home / ".codex" / "hooks.json"
            hooks_path.write_text("{not-json\n")

            uninstalled = run_process(
                ["bash", ROOT / "uninstall.sh"], cwd=ROOT, env=env
            )

            self.assertNotEqual(uninstalled.returncode, 0)
            self.assertIn("invalid Codex hooks JSON", uninstalled.stderr)
            for target, expected in self.installed_links(home).items():
                self.assertTrue(target.is_symlink(), target)
                self.assertEqual(target.resolve(), expected.resolve(), target)
            agents = (home / ".codex" / "AGENTS.md").read_text()
            self.assertIn("BEGIN cc-builder-loop-codex", agents)

    def test_foreign_checkout_uninstall_preserves_managed_agents_block(self) -> None:
        with tempfile.TemporaryDirectory(prefix="builder-loop-foreign-checkout-") as raw_home:
            home = Path(raw_home)
            env = self.environment(home)
            installed = run_process(["bash", ROOT / "install.sh"], cwd=ROOT, env=env)
            self.assertEqual(installed.returncode, 0, installed.stderr)

            foreign_checkout = home / "foreign-checkout"
            foreign_checkout.mkdir()
            shutil.copy2(ROOT / "uninstall.sh", foreign_checkout / "uninstall.sh")
            for expected in (
                "skills/builder-loop-planner",
                "skills/builder",
            ):
                (foreign_checkout / expected).mkdir(parents=True)
            for expected in (
                "agents/tester.toml",
                "agents/reviewer.toml",
                "hooks/builder-loop.py",
                "scripts/codex-builder-loop.py",
                "policies/doc-policy.md",
            ):
                path = foreign_checkout / expected
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("foreign checkout fixture\n")

            uninstalled = run_process(
                ["bash", foreign_checkout / "uninstall.sh"],
                cwd=foreign_checkout,
                env=env,
            )

            self.assertEqual(uninstalled.returncode, 0, uninstalled.stderr)
            agents = (home / ".codex" / "AGENTS.md").read_text()
            self.assertIn("BEGIN cc-builder-loop-codex", agents)
            for target, expected in self.installed_links(home).items():
                self.assertTrue(target.is_symlink(), target)
                self.assertEqual(target.resolve(), expected.resolve(), target)

    def test_symlinked_config_files_remain_symlinks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="builder-loop-config-symlinks-") as raw_home:
            home = Path(raw_home)
            codex_home = home / ".codex"
            config_home = codex_home / "config"
            config_home.mkdir(parents=True)
            hooks_real = config_home / "hooks-real.json"
            hooks_real.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "python3 /foreign/hook.py",
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                )
            )
            agents_real = config_home / "AGENTS-real.md"
            agents_real.write_text("")
            hooks_path = codex_home / "hooks.json"
            agents_path = codex_home / "AGENTS.md"
            hooks_path.symlink_to(Path("config/hooks-real.json"))
            agents_path.symlink_to(Path("config/AGENTS-real.md"))
            hooks_link_text = os.readlink(hooks_path)
            agents_link_text = os.readlink(agents_path)
            env = self.environment(home)

            installed = run_process(["bash", ROOT / "install.sh"], cwd=ROOT, env=env)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            self.assertTrue(hooks_path.is_symlink())
            self.assertTrue(agents_path.is_symlink())
            self.assertEqual(os.readlink(hooks_path), hooks_link_text)
            self.assertEqual(os.readlink(agents_path), agents_link_text)
            self.assertIn("builder-loop.py", hooks_real.read_text())
            self.assertIn("BEGIN cc-builder-loop-codex", agents_real.read_text())

            uninstalled = run_process(
                ["bash", ROOT / "uninstall.sh"], cwd=ROOT, env=env
            )
            self.assertEqual(uninstalled.returncode, 0, uninstalled.stderr)
            self.assertTrue(hooks_path.is_symlink())
            self.assertTrue(agents_path.is_symlink())
            self.assertEqual(os.readlink(hooks_path), hooks_link_text)
            self.assertEqual(os.readlink(agents_path), agents_link_text)
            self.assertIn("python3 /foreign/hook.py", hooks_real.read_text())
            self.assertNotIn("builder-loop.py", hooks_real.read_text())
            self.assertEqual(agents_real.read_text(), "")

    def test_install_rejects_dangling_config_symlink_before_linking(self) -> None:
        with tempfile.TemporaryDirectory(prefix="builder-loop-dangling-config-") as raw_home:
            home = Path(raw_home)
            codex_home = home / ".codex"
            codex_home.mkdir(parents=True)
            hooks_path = codex_home / "hooks.json"
            hooks_path.symlink_to("missing-hooks.json")

            installed = run_process(
                ["bash", ROOT / "install.sh"],
                cwd=ROOT,
                env=self.environment(home),
            )

            self.assertNotEqual(installed.returncode, 0)
            self.assertIn("dangling Codex hooks symlink", installed.stderr)
            self.assertTrue(hooks_path.is_symlink())
            for target in self.installed_links(home):
                self.assertFalse(target.is_symlink(), target)

    def test_companion_handler_in_managed_entry_survives_reinstall_and_uninstall(self) -> None:
        with tempfile.TemporaryDirectory(prefix="builder-loop-companion-hook-") as raw_home:
            home = Path(raw_home)
            env = self.environment(home)
            installed = run_process(["bash", ROOT / "install.sh"], cwd=ROOT, env=env)
            self.assertEqual(installed.returncode, 0, installed.stderr)

            hooks_path = home / ".codex" / "hooks.json"
            config = json.loads(hooks_path.read_text())
            managed_entry = next(
                entry
                for entry in config["hooks"]["Stop"]
                if "builder-loop.py" in json.dumps(entry)
            )
            managed_entry["hooks"].append(
                {"type": "command", "command": "python3 /foreign/keep-me.py"}
            )
            hooks_path.write_text(json.dumps(config))

            reinstalled = run_process(["bash", ROOT / "install.sh"], cwd=ROOT, env=env)
            self.assertEqual(reinstalled.returncode, 0, reinstalled.stderr)
            after_reinstall = json.loads(hooks_path.read_text())
            self.assertIn("python3 /foreign/keep-me.py", json.dumps(after_reinstall))

            uninstalled = run_process(["bash", ROOT / "uninstall.sh"], cwd=ROOT, env=env)
            self.assertEqual(uninstalled.returncode, 0, uninstalled.stderr)
            after_uninstall = json.loads(hooks_path.read_text())
            self.assertIn("python3 /foreign/keep-me.py", json.dumps(after_uninstall))
            self.assertNotIn("builder-loop.py", json.dumps(after_uninstall))

    def test_install_and_uninstall_do_not_overwrite_foreign_fixed_backups(self) -> None:
        with tempfile.TemporaryDirectory(prefix="builder-loop-foreign-backups-") as raw_home:
            home = Path(raw_home)
            codex_home = home / ".codex"
            codex_home.mkdir(parents=True)
            (codex_home / "hooks.json").write_text('{"hooks": {}}\n')
            (codex_home / "AGENTS.md").write_text("# User guidance\n")
            hooks_backup = codex_home / "hooks.json.bak"
            agents_backup = codex_home / "AGENTS.md.bak"
            hooks_backup.write_text("foreign hooks backup\n")
            agents_backup.write_text("foreign agents backup\n")
            env = self.environment(home)

            installed = run_process(["bash", ROOT / "install.sh"], cwd=ROOT, env=env)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            uninstalled = run_process(["bash", ROOT / "uninstall.sh"], cwd=ROOT, env=env)
            self.assertEqual(uninstalled.returncode, 0, uninstalled.stderr)
            self.assertEqual(hooks_backup.read_text(), "foreign hooks backup\n")
            self.assertEqual(agents_backup.read_text(), "foreign agents backup\n")

    def test_install_rolls_back_hooks_when_agents_write_fails(self) -> None:
        with tempfile.TemporaryDirectory(prefix="builder-loop-install-transaction-") as raw_home:
            home = Path(raw_home)
            codex_home = home / ".codex"
            referent_dir = home / "read-only-config"
            referent_dir.mkdir(parents=True)
            agents_real = referent_dir / "AGENTS.md"
            agents_real.write_text("# Original agents\n")
            codex_home.mkdir(parents=True)
            (codex_home / "AGENTS.md").symlink_to(agents_real)
            hooks_path = codex_home / "hooks.json"
            original_hooks = '{"hooks": {"Stop": []}}\n'
            hooks_path.write_text(original_hooks)
            referent_dir.chmod(0o500)
            try:
                installed = run_process(
                    ["bash", ROOT / "install.sh"], cwd=ROOT, env=self.environment(home)
                )
                self.assertNotEqual(installed.returncode, 0)
                self.assertEqual(hooks_path.read_text(), original_hooks)
                self.assertEqual(agents_real.read_text(), "# Original agents\n")
                for target in self.installed_links(home):
                    self.assertFalse(target.is_symlink(), target)
            finally:
                referent_dir.chmod(0o700)

    def test_uninstall_rolls_back_hooks_and_links_when_agents_write_fails(self) -> None:
        with tempfile.TemporaryDirectory(prefix="builder-loop-uninstall-transaction-") as raw_home:
            home = Path(raw_home)
            codex_home = home / ".codex"
            referent_dir = home / "config-referent"
            referent_dir.mkdir(parents=True)
            agents_real = referent_dir / "AGENTS.md"
            agents_real.write_text("# Original agents\n")
            codex_home.mkdir(parents=True)
            (codex_home / "AGENTS.md").symlink_to(agents_real)
            env = self.environment(home)
            installed = run_process(["bash", ROOT / "install.sh"], cwd=ROOT, env=env)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            hooks_path = codex_home / "hooks.json"
            installed_hooks = hooks_path.read_text()
            installed_agents = agents_real.read_text()

            referent_dir.chmod(0o500)
            try:
                uninstalled = run_process(["bash", ROOT / "uninstall.sh"], cwd=ROOT, env=env)
                self.assertNotEqual(uninstalled.returncode, 0)
                self.assertEqual(hooks_path.read_text(), installed_hooks)
                self.assertEqual(agents_real.read_text(), installed_agents)
                for target, expected in self.installed_links(home).items():
                    self.assertTrue(target.is_symlink(), target)
                    self.assertEqual(target.resolve(), expected.resolve(), target)
            finally:
                referent_dir.chmod(0o700)

    def test_agents_content_round_trips_without_whitespace_normalization(self) -> None:
        with tempfile.TemporaryDirectory(prefix="builder-loop-agents-roundtrip-") as raw_home:
            home = Path(raw_home)
            codex_home = home / ".codex"
            codex_home.mkdir(parents=True)
            agents_path = codex_home / "AGENTS.md"
            original = "    indented instruction\n\nTrailing spaces stay.  \n\n"
            agents_path.write_text(original)
            env = self.environment(home)

            installed = run_process(["bash", ROOT / "install.sh"], cwd=ROOT, env=env)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            uninstalled = run_process(["bash", ROOT / "uninstall.sh"], cwd=ROOT, env=env)
            self.assertEqual(uninstalled.returncode, 0, uninstalled.stderr)
            self.assertEqual(agents_path.read_text(), original)

    def test_uninstall_separates_content_appended_after_managed_agents_block(self) -> None:
        with tempfile.TemporaryDirectory(prefix="builder-loop-agents-appended-") as raw_home:
            home = Path(raw_home)
            codex_home = home / ".codex"
            codex_home.mkdir(parents=True)
            agents_path = codex_home / "AGENTS.md"
            agents_path.write_text("Existing guidance without newline")
            env = self.environment(home)

            installed = run_process(["bash", ROOT / "install.sh"], cwd=ROOT, env=env)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            with agents_path.open("a") as stream:
                stream.write("# Guidance added after install\n")

            uninstalled = run_process(["bash", ROOT / "uninstall.sh"], cwd=ROOT, env=env)
            self.assertEqual(uninstalled.returncode, 0, uninstalled.stderr)
            self.assertEqual(
                agents_path.read_text(),
                "Existing guidance without newline\n# Guidance added after install\n",
            )

    def test_uninstall_removes_separator_when_empty_agents_file_gains_content(self) -> None:
        with tempfile.TemporaryDirectory(prefix="builder-loop-agents-empty-appended-") as raw_home:
            home = Path(raw_home)
            codex_home = home / ".codex"
            codex_home.mkdir(parents=True)
            agents_path = codex_home / "AGENTS.md"
            agents_path.write_text("")
            env = self.environment(home)

            installed = run_process(["bash", ROOT / "install.sh"], cwd=ROOT, env=env)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            with agents_path.open("a") as stream:
                stream.write("# Guidance added after install\n")

            uninstalled = run_process(["bash", ROOT / "uninstall.sh"], cwd=ROOT, env=env)
            self.assertEqual(uninstalled.returncode, 0, uninstalled.stderr)
            self.assertEqual(agents_path.read_text(), "# Guidance added after install\n")
            self.assertNotIn("managed separator", agents_path.read_text())


if __name__ == "__main__":
    unittest.main()
