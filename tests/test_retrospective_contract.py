from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILDER_SKILL = (
    ROOT / "skills" / "full-driver-v4-experiment" / "SKILL.md"
).read_text()
RETROSPECTIVE = (
    ROOT / "skills" / "builder" / "references" / "post-delivery-retrospective.md"
).read_text()


class RetrospectiveContractTest(unittest.TestCase):
    def test_builder_delegates_memory_screening_without_copying_old_scoring(self) -> None:
        self.assertIn("post-delivery-retrospective.md", BUILDER_SKILL)
        self.assertIn("$memory-review", BUILDER_SKILL)
        self.assertIn("builder-loop delegated", BUILDER_SKILL)
        self.assertNotIn("①源码不直观", BUILDER_SKILL + RETROSPECTIVE)
        self.assertNotIn("≥4/5", BUILDER_SKILL + RETROSPECTIVE)
        self.assertIn("不得复制旧版五问", BUILDER_SKILL)

    def test_incidents_have_one_owner_and_cross_boundary_chains_split(self) -> None:
        for owner in ("current_project", "builder_loop", "external_platform"):
            self.assertIn(f"`{owner}`", RETROSPECTIVE)
        self.assertIn("`both` 不是合法 owner", RETROSPECTIVE)
        self.assertIn("拆成两个原子事故", RETROSPECTIVE)
        self.assertIn("修复其中一条不能作为", RETROSPECTIVE)

    def test_issue_contract_is_fact_only_versioned_and_user_authorized(self) -> None:
        for field in (
            "归属与版本",
            "触发场景",
            "现场过程",
            "观察到的现象",
            "已确认事实",
            "根因状态",
            "复现条件",
            "runtime_identity.adapter_commit",
        ):
            self.assertIn(field, RETROSPECTIVE)
        self.assertIn("禁止写建议、修复方向、设计方案", RETROSPECTIVE)
        self.assertIn("request_user_input", RETROSPECTIVE)
        self.assertIn("已有同类 issue 时优先追加", RETROSPECTIVE)
        self.assertIn("finalized target 不得被复盘静默改脏", RETROSPECTIVE)


if __name__ == "__main__":
    unittest.main()
