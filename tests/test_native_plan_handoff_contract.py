from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from harness import ROOT, run_process


MARKER = "BUILDER_HANDOFF_READY"
MANAGED_START = "<!-- BEGIN cc-builder-loop-codex -->"
MANAGED_END = "<!-- END cc-builder-loop-codex -->"
LEGACY_EXPLICIT_ONLY = (
    r"只有用户显式输入\s*`?\$builder`?\s*时才启动",
    r"仅当用户明确输入\s*`?\$builder`?\s*时使用",
    r"后续必须由用户显式调用\s*`?\$builder`?",
    r"随后显式调用\s*`?\$builder`?",
)


def read(relative: str) -> str:
    return (ROOT / relative).read_text()


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def has_all(text: str, *term_groups: tuple[str, ...]) -> bool:
    normalized = compact(text)
    return all(any(compact(term) in normalized for term in group) for group in term_groups)


def fenced_blocks(text: str) -> list[str]:
    return re.findall(r"```[^\n]*\n(.*?)```", text, flags=re.DOTALL)


def managed_block(text: str) -> str:
    start = text.index(MANAGED_START)
    end = text.index(MANAGED_END, start) + len(MANAGED_END)
    return text[start:end]


class NativePlanHandoffContractTest(unittest.TestCase):
    def assertHasAll(
        self, text: str, *term_groups: tuple[str, ...], message: str
    ) -> None:
        self.assertTrue(has_all(text, *term_groups), message)

    def assertPlannerContract(self, planner: str) -> None:
        self.assertHasAll(
            planner,
            (MARKER,),
            ("plan-validate",),
            ("READY",),
            ("只有", "仅当", "只能"),
            ("方案正文之外", "计划正文之外"),
            ("独立行", "单独一行"),
            message="Planner 必须把就绪标记约束在验证 READY 后的方案正文之外",
        )
        self.assertHasAll(
            planner,
            (MARKER,),
            ("不输出", "不得输出", "不能输出"),
            ("NEEDS_USER", "FATAL", "验证失败", "非 READY"),
            message="Planner 必须明确禁止未验证或失败方案输出就绪标记",
        )
        for block in fenced_blocks(planner):
            if "unit-test-spec" in block or "documentation-spec" in block:
                self.assertNotIn(MARKER, block, "就绪标记不得成为冻结计划模板的一部分")

    def assertBuilderAuthorizationContract(self, builder: str, metadata: str) -> None:
        self.assertRegex(
            metadata,
            r"(?m)^\s*allow_implicit_invocation:\s*true\s*$",
            "Builder metadata 必须允许受 Skill 自身授权检查约束的隐式发现",
        )
        self.assertHasAll(
            builder,
            (MARKER,),
            ("同一 session", "同一会话"),
            ("紧邻", "下一轮"),
            ("Default mode", "Default 模式", "默认模式"),
            ("Implement the plan", "实施计划"),
            message="Builder 必须完整识别原生实施动作的一次性交接上下文",
        )
        self.assertHasAll(
            builder,
            ("$builder",),
            ("等价授权", "兼容入口", "手工入口", "手工回退"),
            (MARKER,),
            message="显式 $builder 与满足条件的原生实施动作必须是等价授权入口",
        )
        self.assertHasAll(
            builder,
            ("一次", "单次"),
            ("最多", "仅"),
            ("一个 run", "一个运行"),
            message="一次原生实施动作最多只能启动一个 run",
        )

    def assertFailClosedContract(self, builder: str) -> None:
        self.assertHasAll(
            builder,
            ("缺少", "不完整", "不满足"),
            ("停止", "不得继续", "fail closed"),
            ("runtime 调用", "runtime"),
            ("计划物化", "物化计划"),
            ("仓库写入", "文件写入", "写入仓库"),
            message="授权条件不完整时必须在所有副作用之前停止",
        )
        self.assertHasAll(
            builder,
            ("不解析", "不得解析"),
            ("transcript", "自由文本"),
            message="Builder 不得从自由 transcript 猜测授权",
        )

    def test_planner_emits_one_shot_marker_only_after_ready(self) -> None:
        self.assertPlannerContract(read("skills/builder-loop-planner/SKILL.md"))

    def test_native_action_and_explicit_builder_are_equivalent_authorizations(self) -> None:
        builder = read("skills/builder/SKILL.md")
        metadata = read("skills/builder/agents/openai.yaml")
        agents = read("agents/AGENTS.md.block")

        self.assertBuilderAuthorizationContract(builder, metadata)
        self.assertHasAll(
            agents,
            (MARKER,),
            ("同一 session", "同一会话"),
            ("紧邻", "下一轮"),
            ("Default mode", "Default 模式", "默认模式"),
            ("Implement the plan", "实施计划"),
            ("$builder",),
            message="全局托管规则必须公开原生实施动作与显式入口的严格等价授权",
        )
        for pattern in LEGACY_EXPLICIT_ONLY:
            self.assertNotRegex(
                builder + "\n" + agents,
                pattern,
                "公共入口不得保留“只有显式 $builder 才能启动”的旧授权语义",
            )

    def test_misloaded_builder_fails_closed_before_any_side_effect(self) -> None:
        builder = read("skills/builder/SKILL.md")
        metadata = read("skills/builder/agents/openai.yaml")

        self.assertRegex(
            metadata,
            r"(?m)^\s*allow_implicit_invocation:\s*true\s*$",
            "必须允许 Builder 被受约束地发现，才能验证误加载时的 fail-closed 行为",
        )
        self.assertHasAll(
            builder,
            ("隐式加载", "隐式调用", "误加载", "自动加载"),
            ("授权条件", "交接条件"),
            ("停止", "不得继续", "fail closed"),
            message="Builder 被误加载时必须先检查完整授权条件并停止",
        )
        self.assertFailClosedContract(builder)

    def test_stale_and_unbound_actions_are_rejected(self) -> None:
        builder = read("skills/builder/SKILL.md")
        agents = read("agents/AGENTS.md.block")
        combined = builder + "\n" + agents

        for terms, label in (
            (("标记缺失", "缺少标记", "没有标记"), "缺标记"),
            (("验证失败", "未验证", "非 READY"), "验证失败"),
            (("非紧邻", "中间消息", "被其他消息打断"), "非紧邻"),
            (("计划修订", "修改计划", "方案修订"), "计划修订"),
            (
                ("session 变化", "不同 session", "会话变化", "不同会话", "跨 session"),
                "session 变化",
            ),
            (("非 Default mode", "不是 Default mode", "仍在 Plan mode"), "非 Default mode"),
            (("Codex 原生 Plan", "原生 Plan"), "Codex 原生 Plan"),
            (("普通 Implement the plan", "普通实施请求", "普通改码请求"), "普通实施请求"),
        ):
            self.assertTrue(
                any(compact(term) in compact(combined) for term in terms),
                f"公共授权契约缺少拒绝场景：{label}",
            )
        self.assertFailClosedContract(builder)

    def test_marker_does_not_expand_hook_runtime_or_plan_state(self) -> None:
        hooks = json.loads(read("hooks/hooks.json"))["hooks"]
        self.assertEqual(
            set(hooks),
            {"SessionStart", "SubagentStart", "SubagentStop", "Stop"},
            "原生交接不得新增 UserPromptSubmit 或其他编排 Hook",
        )
        self.assertNotIn("UserPromptSubmit", hooks)

        for directory in ("hooks", "runtime", "schema"):
            for path in (ROOT / directory).rglob("*"):
                if path.is_file():
                    self.assertNotIn(
                        MARKER,
                        path.read_text(errors="replace"),
                        f"就绪标记不得进入 {directory} 状态或运行实现：{path}",
                    )

    def test_installed_native_handoff_contract_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="native-handoff-install-") as raw_home:
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
            self.assertEqual(installed_agents.count(MANAGED_END), 1)
            self.assertEqual(
                managed_block(installed_agents),
                managed_block(read("agents/AGENTS.md.block")),
            )

            installed_planner = (
                home / ".agents" / "skills" / "builder-loop-planner" / "SKILL.md"
            ).read_text()
            installed_builder = (
                home / ".agents" / "skills" / "builder" / "SKILL.md"
            ).read_text()
            installed_metadata = (
                home / ".agents" / "skills" / "builder" / "agents" / "openai.yaml"
            ).read_text()
            self.assertPlannerContract(installed_planner)
            self.assertBuilderAuthorizationContract(installed_builder, installed_metadata)
            self.assertFailClosedContract(installed_builder)

            installed_hooks = json.loads((codex_home / "hooks.json").read_text())["hooks"]
            self.assertEqual(
                set(installed_hooks),
                {"SessionStart", "SubagentStart", "SubagentStop", "Stop"},
            )
            self.assertNotIn("UserPromptSubmit", installed_hooks)

            installed_files = {
                path.relative_to(home).as_posix()
                for path in home.rglob("*")
                if path.is_file() or path.is_symlink()
            }
            self.assertEqual(
                installed_files,
                {
                    ".agents/skills/builder",
                    ".agents/skills/builder-loop-planner",
                    ".codex/AGENTS.md",
                    ".codex/agents/reviewer.toml",
                    ".codex/agents/tester.toml",
                    ".codex/builder-loop/doc-policy.md",
                    ".codex/hooks.json",
                    ".codex/hooks/builder-loop.py",
                    ".local/bin/codex-builder-loop",
                },
                "重复安装不得增加 handoff 文件、runtime 状态或用户配置",
            )

    def test_readme_architecture_and_skills_describe_the_same_authorization(self) -> None:
        public_contracts = {
            "README": read("README.md"),
            "architecture": read("docs/architecture.md"),
            "Planner": read("skills/builder-loop-planner/SKILL.md"),
            "Builder": read("skills/builder/SKILL.md"),
            "AGENTS": read("agents/AGENTS.md.block"),
        }
        for label, text in public_contracts.items():
            with self.subTest(contract=label):
                self.assertHasAll(
                    text,
                    (MARKER,),
                    ("Implement the plan", "实施计划", "原生实施"),
                    ("$builder",),
                    message=f"{label} 未同时说明原生实施与显式 $builder 入口",
                )
                for pattern in LEGACY_EXPLICIT_ONLY:
                    self.assertNotRegex(
                        text,
                        pattern,
                        f"{label} 仍保留显式 $builder 独占启动的旧语义",
                    )

        docs = public_contracts["README"] + "\n" + public_contracts["architecture"]
        self.assertHasAll(
            docs,
            ("一次", "无需再次", "不再要求重复"),
            ("$builder",),
            ("手工回退", "兼容入口", "显式入口"),
            message="用户文档必须把原生实施作为正常路径、$builder 作为兼容手工入口",
        )
        self.assertHasAll(
            read("CHANGELOG.md"),
            (MARKER,),
            ("Implement the plan", "实施计划", "原生实施"),
            ("$builder",),
            message="CHANGELOG 必须记录公共授权入口变化",
        )


if __name__ == "__main__":
    unittest.main()
