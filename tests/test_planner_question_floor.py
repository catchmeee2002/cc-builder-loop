from __future__ import annotations

import unittest

from planner_sources import builder_planner_instructions

FLOOR_DIMENSIONS = (
    "目标与用户可见变化",
    "不能碰什么与必须兼容什么",
    "实施边界",
)


class PlannerQuestionFloorTest(unittest.TestCase):
    def test_planner_declares_question_floor_dimensions(self) -> None:
        planner = builder_planner_instructions()

        self.assertIn("下限维度", planner)
        for dimension in FLOOR_DIMENSIONS:
            self.assertIn(dimension, planner)

    def test_floor_omission_requires_declared_task_specific_facts(self) -> None:
        planner = builder_planner_instructions()

        for anchor in ("声明跳过", "具体事实", "追问范围"):
            self.assertIn(anchor, planner)
        self.assertIn("不写「任务简单」", planner)
        self.assertIn("不得整体静默省略", planner)


if __name__ == "__main__":
    unittest.main()
