from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

from harness import ROOT, run_process


MARKER = "BUILDER_HANDOFF_READY"
MANAGED_START = "<!-- BEGIN cc-builder-loop-codex -->"
MANAGED_END = "<!-- END cc-builder-loop-codex -->"
LEGACY_ROUTING = (
    r"Plan\s*mode\s*\u4f7f\u7528\s*\$builder-loop-planner",
    r"\u53ea\u6709\u7528\u6237\u663e\u5f0f\u8c03\u7528\s*\$builder\s*\u624d\u542f\u52a8",
    r"\u4ec5\u5f53\u7528\u6237\u660e\u786e\u8f93\u5165\s*`?\$builder`?\s*\u65f6\u4f7f\u7528",
)


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def has_any(text: str, terms: tuple[str, ...]) -> bool:
    normalized = compact(text)
    return any(compact(term) in normalized for term in terms)


def read(relative: str) -> str:
    return (ROOT / relative).read_text()


def managed_block(text: str) -> str:
    start = text.index(MANAGED_START)
    end = text.index(MANAGED_END, start) + len(MANAGED_END)
    return text[start:end]


class SessionStartAuthorizationNeutralityTest(unittest.TestCase):
    def invoke_hook(
        self,
        hook: Path,
        event: dict[str, object],
        *,
        cwd: Path,
        path: str,
    ) -> dict[str, object]:
        completed = run_process(
            [sys.executable, hook],
            cwd=cwd,
            env={"PATH": path, "BUILDER_LOOP_CLI": ""},
            input_text=json.dumps(event),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, completed.stdout)
        payload = json.loads(lines[0])
        self.assertEqual(
            payload.get("hookSpecificOutput", {}).get("hookEventName"),
            "SessionStart",
            payload,
        )
        return payload

    def context_from(self, payload: dict[str, object]) -> str:
        hook_output = payload.get("hookSpecificOutput")
        self.assertIsInstance(hook_output, dict, payload)
        context = hook_output.get("additionalContext")
        self.assertIsInstance(context, str, payload)
        return context

    def assert_neutral_context(self, context: str) -> None:
        self.assertIn("Builder-loop Codex adapter", context)
        self.assertTrue(
            has_any(context, ("根线程", "root thread"))
            and has_any(context, ("tester",))
            and has_any(context, ("reviewer",)),
            f"SessionStart 必须保留根线程、Tester、Reviewer 角色边界：{context}",
        )
        self.assertTrue(
            has_any(context, ("不构成", "不是"))
            and has_any(context, ("启动授权", "Builder 授权", "Builder-loop 授权")),
            f"SessionStart 必须明确声明其上下文本身不构成 Builder 授权：{context}",
        )
        delegates_authorization = (
            has_any(context, ("服从适用 AGENTS", "以适用 AGENTS 为准"))
            and has_any(context, ("授权契约", "授权规则"))
        )
        explicitly_chooses_no_route = has_any(
            context, ("不选择 Plan 路线", "不选择 Planner", "不决定 Plan 路线")
        )
        self.assertTrue(
            delegates_authorization or explicitly_chooses_no_route,
            f"SessionStart 必须把路线与启动授权交还适用 AGENTS：{context}",
        )
        for pattern in LEGACY_ROUTING:
            self.assertNotRegex(context, pattern, "SessionStart 仍包含旧的强制路由语义")
        self.assertNotIn(MARKER, context, "Hook 不得输出或消费 Planner 就绪标记")

    def test_session_start_never_emits_handoff_marker(self) -> None:
        hook = ROOT / "hooks" / "builder-loop.py"
        with tempfile.TemporaryDirectory(prefix="marker-free-session-start-") as raw:
            cwd = Path(raw)
            context = self.context_from(
                self.invoke_hook(
                    hook,
                    {
                        "hook_event_name": "SessionStart",
                        "session_id": "fixed-session-marker-free",
                        "cwd": str(cwd),
                    },
                    cwd=cwd,
                    path=os.environ.get("PATH", ""),
                )
            )
            self.assertNotIn(
                MARKER,
                context,
                "SessionStart 不得发布或消费 Planner 就绪标记",
            )

    def test_session_start_does_not_implicitly_authorize_native_plan(self) -> None:
        hook = ROOT / "hooks" / "builder-loop.py"
        with tempfile.TemporaryDirectory(prefix="native-plan-isolation-") as raw:
            cwd = Path(raw)
            context = self.context_from(
                self.invoke_hook(
                    hook,
                    {
                        "hook_event_name": "SessionStart",
                        "session_id": "fixed-session-native-plan",
                        "cwd": str(cwd),
                    },
                    cwd=cwd,
                    path=os.environ.get("PATH", ""),
                )
            )
            self.assertNotRegex(
                compact(context),
                r"codex原生plan.*(?:普通实施请求)?.*(?:隐式|自动|已经|已).*(?:builder-loop)?启动授权",
                "SessionStart 不得将 Codex 原生 Plan 或普通实施请求声明为隐式 Builder 授权",
            )

    def test_session_start_with_session_id_is_non_authorizing(self) -> None:
        hook = ROOT / "hooks" / "builder-loop.py"
        with tempfile.TemporaryDirectory(prefix="neutral-session-start-") as raw:
            cwd = Path(raw)
            before = tuple(cwd.rglob("*"))
            payload = self.invoke_hook(
                hook,
                {
                    "hook_event_name": "SessionStart",
                    "session_id": "fixed-session-neutrality-42",
                    "cwd": str(cwd),
                },
                cwd=cwd,
                path=os.environ.get("PATH", ""),
            )
            context = self.context_from(payload)
            self.assert_neutral_context(context)
            self.assertIn("session_id=fixed-session-neutrality-42", context)
            self.assertEqual(before, tuple(cwd.rglob("*")), "SessionStart 不得创建 run 或 ledger")

    def test_invalid_session_start_inputs_fail_closed_without_side_effects(self) -> None:
        hook = ROOT / "hooks" / "builder-loop.py"
        with tempfile.TemporaryDirectory(prefix="invalid-session-start-") as raw:
            cwd = Path(raw)
            empty_path = cwd / "empty-path"
            empty_path.mkdir()

            missing_session = self.context_from(
                self.invoke_hook(
                    hook,
                    {"hook_event_name": "SessionStart", "cwd": str(cwd)},
                    cwd=cwd,
                    path=os.environ.get("PATH", ""),
                )
            )
            self.assert_neutral_context(missing_session)
            self.assertTrue(
                has_any(missing_session, ("缺少 session_id", "缺 session_id"))
                and has_any(missing_session, ("不要启动", "禁止启动", "不得启动")),
                missing_session,
            )

            missing_cli = self.context_from(
                self.invoke_hook(
                    hook,
                    {
                        "hook_event_name": "SessionStart",
                        "session_id": "fixed-session-no-cli",
                        "cwd": str(cwd),
                    },
                    cwd=cwd,
                    path=str(empty_path),
                )
            )
            self.assert_neutral_context(missing_cli)
            self.assertIn("session_id=fixed-session-no-cli", missing_cli)
            self.assertTrue(
                has_any(missing_cli, ("找不到 codex-builder-loop CLI", "CLI 不可用"))
                and has_any(missing_cli, ("禁止声称", "不得声称")),
                missing_cli,
            )
            self.assertEqual(
                [path.relative_to(cwd).as_posix() for path in cwd.rglob("*")],
                ["empty-path"],
                "失败分支不得调用 runtime、创建 ledger 或写入工作区",
            )

    def test_installed_contract_keeps_neutral_hook_and_both_authorized_routes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neutral-installed-contract-") as raw:
            home = Path(raw)
            codex_home = home / ".codex"
            codex_home.mkdir(parents=True)
            existing_agents = "# Existing user guidance\n\nKeep this exact text.\n"
            (codex_home / "AGENTS.md").write_text(existing_agents)
            env = {
                **os.environ,
                "HOME": str(home),
                "CODEX_HOME": str(codex_home),
                "PATH": f"{home / '.local' / 'bin'}:{os.environ.get('PATH', '')}",
            }

            for _ in range(2):
                installed = run_process(["bash", ROOT / "install.sh"], cwd=ROOT, env=env)
                self.assertEqual(installed.returncode, 0, installed.stderr)

            installed_agents = (codex_home / "AGENTS.md").read_text()
            self.assertIn(existing_agents.strip(), installed_agents)
            self.assertEqual(installed_agents.count(MANAGED_START), 1)
            self.assertEqual(installed_agents.count(MANAGED_END), 1)
            self.assertEqual(
                managed_block(installed_agents), managed_block(read("agents/AGENTS.md.block"))
            )

            installed_hook = codex_home / "hooks" / "builder-loop.py"
            payload = self.invoke_hook(
                installed_hook,
                {
                    "hook_event_name": "SessionStart",
                    "session_id": "installed-fixed-session",
                    "cwd": str(home),
                },
                cwd=home,
                path=env["PATH"],
            )
            context = self.context_from(payload)
            self.assert_neutral_context(context)
            self.assertIn("session_id=installed-fixed-session", context)

            planner = (home / ".agents/skills/builder-loop-planner/SKILL.md").read_text()
            builder = (home / ".agents/skills/builder/SKILL.md").read_text()
            managed_agents = managed_block(installed_agents)
            combined = compact(builder + "\n" + managed_agents)

            self.assertIn(compact(MARKER), compact(planner))
            self.assertTrue(
                all(
                    has_any(planner, terms)
                    for terms in (
                        ("plan-validate",),
                        ("READY",),
                        ("方案正文之外", "计划正文之外"),
                        ("独立行", "单独一行"),
                    )
                ),
                "Planner 必须只在验证 READY 后于冻结方案外发布 marker",
            )
            for required in (
                MARKER,
                "同一session",
                "紧邻",
                "Defaultmode",
                "Implementtheplan",
                "$builder",
                "Codex原生Plan",
            ):
                self.assertIn(compact(required), combined, f"安装态缺少双入口条件：{required}")
            for rejected in (
                ("标记缺失", "缺少就绪标记"),
                ("非紧邻", "不是紧邻下一轮"),
                ("session变化", "不同session", "跨session"),
                ("仍在Planmode", "非Defaultmode"),
                ("普通实施请求", "普通改码请求"),
                ("不得解析transcript", "不解析transcript"),
            ):
                self.assertTrue(
                    any(compact(term) in combined for term in rejected),
                    f"安装态缺少 fail-closed 边界：{rejected}",
                )

            hooks = json.loads((codex_home / "hooks.json").read_text())["hooks"]
            self.assertEqual(set(hooks), {"SessionStart", "SubagentStart", "SubagentStop", "Stop"})
            self.assertNotIn("UserPromptSubmit", hooks)
            self.assertNotIn(MARKER, installed_hook.read_text())

            installed_paths = {
                path.relative_to(home).as_posix()
                for path in home.rglob("*")
                if path.is_file() or path.is_symlink()
            }
            self.assertEqual(
                installed_paths,
                {
                    ".agents/skills/builder",
                    ".agents/skills/builder-loop-planner",
                    ".agents/skills/file-github-issue",
                    ".codex/AGENTS.md",
                    ".codex/agents/reviewer.toml",
                    ".codex/agents/tester.toml",
                    ".codex/builder-loop/doc-policy.md",
                    ".codex/hooks.json",
                    ".codex/hooks/builder-loop.py",
                    ".local/bin/codex-builder-loop",
                },
                "安装不得新增 handoff 文件、runtime 状态或额外 Hook",
            )


if __name__ == "__main__":
    unittest.main()
