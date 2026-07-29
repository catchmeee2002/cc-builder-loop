from __future__ import annotations

import hashlib
import json
import re
import unittest

from harness import ROOT, git


SKILL_DIR = ROOT / "skills" / "file-github-issue"
SKILL_PATH = SKILL_DIR / "SKILL.md"
METADATA_PATH = SKILL_DIR / "agents" / "openai.yaml"
SPEC_HEAD = "492db76a1f3fb4a59532c2dfffce61850c9d66ac"


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def contract(text: str, name: str) -> dict:
    start = f"<!-- {name}:v1 -->"
    end = f"<!-- /{name}:v1 -->"
    block = text.split(start, 1)[1].split(end, 1)[0]
    payload = re.search(r"```json\s*(.*?)\s*```", block, flags=re.DOTALL)
    if payload is None:
        raise AssertionError(f"missing JSON payload for {name}")
    return json.loads(payload.group(1))


class FileGithubIssueSkillTest(unittest.TestCase):
    def test_skill_is_prompt_only_and_implicitly_discoverable(self) -> None:
        skill = SKILL_PATH.read_text()
        metadata = METADATA_PATH.read_text()

        self.assertFalse((SKILL_DIR / "scripts").exists())
        self.assertIn("提 Issue", skill)
        self.assertIn("记录 bug", skill)
        self.assertIn("gh issue create", skill)
        self.assertIn("gh issue comment", skill)
        self.assertIn("gh issue close --comment", skill)
        self.assertRegex(metadata, r"(?m)^\s*allow_implicit_invocation:\s*true\s*$")

    def test_explicit_issue_action_is_authorization_without_second_confirmation(self) -> None:
        skill = compact(SKILL_PATH.read_text())

        self.assertIn(compact("用户已经要求相应动作时不得再次请求确认"), skill)
        self.assertIn(compact("用户已要求创建、补充或关闭 Issue"), skill)
        self.assertIn(compact("直接执行范围内的评论和标签更新"), skill)
        self.assertIn(compact("关闭仍需用户明确要求或任务明确要求"), skill)
        self.assertIn(compact("没有 GitHub 写入授权时，只向用户报告"), skill)
        self.assertIn(compact("仓库归属不明确"), skill)
        self.assertIn(compact("证据可能含凭据或个人数据"), skill)
        self.assertIn(compact("改变产品目标或设计原则"), skill)
        self.assertIn("request_user_input", skill)
        self.assertEqual(
            git(ROOT, "rev-parse", "HEAD:skills/file-github-issue/SKILL.md"),
            git(ROOT, "rev-parse", f"{SPEC_HEAD}:skills/file-github-issue/SKILL.md"),
        )
        self.assertEqual(
            hashlib.sha256(SKILL_PATH.read_bytes()).hexdigest(),
            "48cd3142d8d14cb003863198b8f35e7b01af4bcb08c8ecd49dc1ab8a47dc179c",
        )

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

    def test_capture_and_resolution_contracts_are_separated(self) -> None:
        skill = SKILL_PATH.read_text()

        self.assertIn("<!-- issue-capture:v1 -->", skill)
        self.assertIn("<!-- /issue-capture:v1 -->", skill)
        self.assertIn("<!-- issue-resolution:v1 -->", skill)
        self.assertIn("<!-- /issue-resolution:v1 -->", skill)
        self.assertIn("incident_head", skill)
        self.assertIn("resolved_head", skill)
        self.assertIn("human_decision", skill)
        self.assertIn("acceptance", skill)
        self.assertIn("创建后不要改写原始正文", skill)
        self.assertEqual(
            set(contract(skill, "issue-capture")),
            {"captured_at", "repository", "incident_head", "branch", "dirty", "root_cause_status"},
        )
        self.assertEqual(
            set(contract(skill, "issue-resolution")),
            {
                "resolved_at",
                "outcome",
                "incident_head",
                "resolved_head",
                "fix_commits",
                "root_cause_status",
                "root_cause",
                "violated_invariant",
                "human_decision",
                "acceptance",
                "residual_uncertainty",
            },
        )

    def test_resolution_records_facts_without_self_assigning_shadow_route(self) -> None:
        skill = compact(SKILL_PATH.read_text())

        for kind in (
            "scope_approval",
            "goal_or_principle",
            "root_cause_correction",
            "tradeoff",
        ):
            self.assertIn(kind, skill)
        self.assertIn(compact("不要在结案评论中填写 `shadow_route`"), skill)
        self.assertIn("batch_approval", skill)
        self.assertIn("needs_first_principles", skill)


if __name__ == "__main__":
    unittest.main()
