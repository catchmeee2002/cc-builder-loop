from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC_HEAD = "1b512b120a673470a4ee154b7c8dd8ac3c3f7e1f"


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def read(path: str) -> str:
    return (ROOT / path).read_text()


def git_lines(*args: str) -> list[str]:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=True, check=False
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return [line for line in result.stdout.splitlines() if line]


class DeliveryDisciplineContractTest(unittest.TestCase):
    def test_root_cause_minimum_sufficient_and_real_tradeoff_rules_are_explicit(self) -> None:
        planner = compact(read("skills/builder-loop-planner/SKILL.md"))
        builder = compact(read("skills/builder/SKILL.md"))
        philosophy = compact(read("docs/design-philosophy.md"))
        combined = planner + builder + philosophy

        self.assertRegex(combined, r"根因.{0,100}(?:修复|解决)")
        self.assertIn("证据", combined)
        self.assertIn("停止", combined)
        self.assertIn("猜测", combined)
        self.assertTrue("最小充分" in combined or "minimumsufficient" in combined)
        self.assertRegex(
            planner,
            r"(?:高影响|重大|范式).{0,100}(?:真实分叉|真实.*分叉).{0,100}(?:取舍|比较|用户决定)",
        )
        self.assertRegex(planner, r"(?:只有一条可信路径|不存在真实分叉).{0,100}(?:约束|排除)")
        self.assertNotRegex(
            planner,
            r"(?:所有|每个|任何).{0,30}(?:任务|计划).{0,30}(?:必须|始终).{0,30}(?:两套|两个|多套).{0,20}(?:方案|备选)",
        )

    def test_segmented_self_checks_are_lightweight_and_never_replace_gates(self) -> None:
        builder = compact(read("skills/builder/SKILL.md"))
        architecture = compact(read("docs/architecture.md"))
        combined = builder + architecture
        self.assertIn("自检", combined)
        self.assertTrue("局部" in combined and ("跨模块" in combined or "阶段" in combined))
        self.assertRegex(combined, r"自检.{0,160}(?:不写|不得写|不会写).{0,30}ledger")
        self.assertRegex(
            combined,
            r"自检.{0,180}(?:不替代|不能替代|不得替代).{0,80}(?:machine|tester|reviewer|正式gate|正式门禁)",
        )
        self.assertRegex(
            combined,
            r"(?:不建立|不得建立|不创建|不得创建).{0,50}(?:诊断状态副本|第二份诊断状态|第二状态源)",
        )

    def test_r6_does_not_add_runtime_modules_or_dependencies(self) -> None:
        before_modules = set(
            git_lines("ls-tree", "-r", "--name-only", SPEC_HEAD, "--", "runtime/codex_builder_loop")
        )
        after_modules = set(git_lines("ls-tree", "-r", "--name-only", "HEAD", "--", "runtime/codex_builder_loop"))
        self.assertLessEqual(before_modules, after_modules)
        self.assertLessEqual(
            after_modules - before_modules,
            {"runtime/codex_builder_loop/lifecycle.py"},
        )
        before_requirements = subprocess.run(
            ["git", "show", f"{SPEC_HEAD}:requirements-dev.txt"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        self.assertEqual((ROOT / "requirements-dev.txt").read_text(), before_requirements)


if __name__ == "__main__":
    unittest.main()
