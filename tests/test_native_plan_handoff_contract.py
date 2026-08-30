from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from harness import CLI, ROOT, init_repo, run_cli, run_process


MANAGED_START = "<!-- BEGIN cc-builder-loop-codex -->"
MANAGED_END = "<!-- END cc-builder-loop-codex -->"


def read(relative: str) -> str:
    return (ROOT / relative).read_text()


def managed_block(text: str) -> str:
    start = text.index(MANAGED_START)
    end = text.index(MANAGED_END, start) + len(MANAGED_END)
    return text[start:end]


class ExperimentalEntryContractTest(unittest.TestCase):
    def test_default_start_is_rejected_before_repo_or_plan_access(self) -> None:
        with tempfile.TemporaryDirectory(prefix="builder-maintenance-") as raw:
            root = Path(raw)
            missing_repo = root / "missing-repo"
            missing_plan = root / "missing-plan.md"
            env = os.environ.copy()
            env.pop("CODEX_BUILDER_LOOP_ENABLE_LEGACY_START", None)

            result = run_process(
                [
                    os.environ.get("PYTHON", "python3"),
                    CLI,
                    "start",
                    "--repo",
                    missing_repo,
                    "--run",
                    "disabled",
                    "--session-id",
                    "maintenance-session",
                    "--plan",
                    missing_plan,
                ],
                env=env,
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout.splitlines()[-1])
            self.assertEqual(payload["status"], "FATAL")
            self.assertEqual(payload["code"], "BUILDER_MAINTENANCE_DISABLED")
            self.assertFalse(missing_repo.exists())
            self.assertFalse(missing_plan.exists())

    def test_fixture_switch_keeps_legacy_contracts_executable(self) -> None:
        repo = init_repo()
        result = run_cli(
            "start",
            "--repo",
            repo,
            "--run",
            "legacy-fixture",
            "--session-id",
            "legacy-fixture-session",
            "--plan",
            repo / "missing-plan.md",
        )
        self.assertEqual(result.data["code"], "PLAN_READ_ERROR")
        self.assertNotEqual(result.data["code"], "BUILDER_MAINTENANCE_DISABLED")

    def test_public_skills_open_only_as_v4_experiment(self) -> None:
        for skill in ("planner", "builder", "builder-loop-planner"):
            body = read(f"skills/{skill}/SKILL.md")
            metadata = read(f"skills/{skill}/agents/openai.yaml")
            self.assertNotIn("维护门禁", body)
            self.assertRegex(
                metadata,
                r"(?m)^\s*allow_implicit_invocation:\s*true\s*$",
            )
        builder = read("skills/builder/SKILL.md")
        self.assertIn("full-driver-v4-experiment", builder)
        self.assertIn("不得调用公共 legacy `start`", builder)
        self.assertIn("native-driver start", builder)
        self.assertIn("validate-decision", builder)
        self.assertIn("native-driver resume", builder)
        self.assertIn("assurance-v4-decision", builder)
        planner = read("skills/builder-loop-planner/SKILL.md")
        native_planner = read("skills/planner/SKILL.md")
        self.assertIn("native_codex", native_planner)
        self.assertIn("builder_loop", native_planner)
        self.assertIn("$builder-loop-planner", native_planner)
        self.assertIn("BUILDER_HANDOFF_READY", native_planner)
        self.assertIn("不生成", native_planner)
        self.assertIn("Planner Adapter", planner)
        self.assertIn("assurance-v4-contract", planner)
        self.assertIn("--experimental-v4 validate", planner)
        self.assertIn("validate-decision", planner)
        self.assertIn("assurance-v4-decision", planner)
        self.assertIn("人类可读正文只展示", planner)
        self.assertIn("`reviewer_preflight:true`", planner)
        self.assertIn("`preflight_before_proof:true`", planner)
        self.assertIn("BUILDER_HANDOFF_READY", planner)
        admission = native_planner[
            native_planner.index("## 路线准入") : native_planner.index("## 建立计划")
        ]
        self.assertIn("只问这一个路线问题", admission)
        self.assertIn("不得输出结论", admission)
        self.assertIn("任何领域取舍", admission)
        self.assertIn("路线卡失败或用户退出时立即停止", admission)

    def test_managed_agents_restore_experimental_route_choice_and_handoff(self) -> None:
        agents = read("agents/AGENTS.md.block")
        self.assertIn("Planner + Codex 原生执行", agents)
        self.assertIn("$planner", agents)
        self.assertIn("Builder-loop 实验", agents)
        self.assertIn("$builder-loop-planner", agents)
        self.assertIn("request_user_input", agents)
        self.assertIn("Implement the plan.", agents)
        self.assertIn("BUILDER_HANDOFF_READY", agents)
        self.assertIn("普通 `$planner` 计划继续由 Codex 原生能力执行", agents)
        self.assertIn("legacy v2/v3 新 run", agents)

    def test_install_is_idempotent_without_builder_hooks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="maintenance-install-") as raw_home:
            home = Path(raw_home)
            codex_home = home / ".codex"
            codex_home.mkdir(parents=True)
            (codex_home / "AGENTS.md").write_text("# Existing guidance\n")
            env = {
                **os.environ,
                "HOME": str(home),
                "CODEX_HOME": str(codex_home),
                "PATH": f"{home / '.local' / 'bin'}:{os.environ.get('PATH', '')}",
            }

            for _ in range(2):
                result = run_process(["bash", ROOT / "install.sh"], cwd=ROOT, env=env)
                self.assertEqual(result.returncode, 0, result.stderr)

            installed_agents = (codex_home / "AGENTS.md").read_text()
            self.assertEqual(installed_agents.count(MANAGED_START), 1)
            self.assertEqual(
                managed_block(installed_agents),
                managed_block(read("agents/AGENTS.md.block")),
            )
            self.assertTrue((home / ".agents" / "skills" / "planner").is_symlink())
            hooks = json.loads((codex_home / "hooks.json").read_text())["hooks"]
            self.assertEqual(hooks, {})


if __name__ == "__main__":
    unittest.main()
