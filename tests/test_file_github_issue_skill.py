from __future__ import annotations

import re
import unittest

from harness import ROOT


SKILL_DIR = ROOT / "skills" / "file-github-issue"
SKILL_PATH = SKILL_DIR / "SKILL.md"
METADATA_PATH = SKILL_DIR / "agents" / "openai.yaml"


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


class FileGithubIssueSkillTest(unittest.TestCase):
    def test_skill_is_prompt_only_and_implicitly_discoverable(self) -> None:
        skill = SKILL_PATH.read_text()
        metadata = METADATA_PATH.read_text()

        self.assertFalse((SKILL_DIR / "scripts").exists())
        self.assertIn("提 Issue", skill)
        self.assertIn("记录 bug", skill)
        self.assertIn("gh issue create", skill)
        self.assertIn("gh issue comment", skill)
        self.assertRegex(metadata, r"(?m)^\s*allow_implicit_invocation:\s*true\s*$")

    def test_explicit_file_request_is_authorization_without_second_confirmation(self) -> None:
        skill = compact(SKILL_PATH.read_text())

        self.assertIn(compact("这条指令已经授权创建或补充 Issue"), skill)
        self.assertIn(compact("不要再次询问是否创建"), skill)
        self.assertIn(compact("没有创建授权时，只向用户报告"), skill)
        self.assertIn("request_user_input", skill)

    def test_issue_preserves_facts_without_anchoring_a_fix(self) -> None:
        skill = compact(SKILL_PATH.read_text())

        for term in (
            "触发场景",
            "观察到的现象",
            "预期契约",
            "已确认事实",
            "unknown",
            "candidate",
            "confirmed",
            "默认不写修法",
            "查重",
            "清洗",
        ):
            self.assertIn(compact(term), skill)


if __name__ == "__main__":
    unittest.main()
