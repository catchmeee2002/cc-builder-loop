from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import issue_triage_eval as evaluator  # noqa: E402


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pilot_cases.json"


def assessment(**overrides):
    value = {
        "issue_id": "case-1",
        "principle_ids": ["P1"],
        "invariant": "不变量",
        "root_cause": "根因",
        "root_cause_status": "established",
        "surviving_alternatives": [],
        "diagnostic_missing_evidence": [],
        "scope_notes": [],
        "flags": {
            "goal_or_taste": False,
            "new_or_changed_principle": False,
            "principle_conflict": False,
            "public_contract_or_role_boundary": False,
            "wide_scope": False,
            "hard_to_reverse": False,
            "deterministic_acceptance": True,
        },
        "proposed_cluster_id": "cluster-one",
    }
    for key, item in overrides.items():
        if key.startswith("flag_"):
            value["flags"][key.removeprefix("flag_")] = item
        else:
            value[key] = item
    return value


def attack(**overrides):
    value = {
        "issue_id": "case-1",
        "diagnosis_verdict": "stands",
        "cluster_verdict": "stands",
        "cluster_reason": "同根",
        "human_attention_escalation": "none",
        "reason": "站得住",
        "surviving_alternative": "none",
        "surviving_alternative_reason": "没有竞争根因",
        "diagnostic_missing_evidence": [],
        "scope_notes": [],
        "scope_inventory_required": False,
        "principle_conflict": False,
    }
    value.update(overrides)
    return value


class IssueTriageEvalTests(unittest.TestCase):
    def test_pilot_suite_loads_and_has_balanced_attention_labels(self):
        suite = evaluator.load_suite(FIXTURE)

        self.assertEqual(suite.suite_id, "pilot-axes-2026-07-27")
        self.assertEqual(sum(len(project.cases) for project in suite.projects), 17)
        attention = [case.gold.human_attention for project in suite.projects for case in project.cases]
        self.assertGreaterEqual(attention.count("none"), 5)
        self.assertGreaterEqual(attention.count("batch_approval"), 2)
        self.assertGreaterEqual(attention.count("first_principles"), 4)

    def test_model_prompt_strips_gold_and_source_url(self):
        project = evaluator.load_suite(FIXTURE).projects[0]
        prompt = evaluator._project_prompt_data(project)

        serialized = evaluator.json.dumps(prompt, ensure_ascii=False)
        self.assertNotIn("gold", serialized)
        self.assertNotIn("source_url", serialized)
        self.assertIn("cc-101", serialized)
        self.assertIn("CC1", serialized)

    def test_select_project_is_exact(self):
        suite = evaluator.load_suite(FIXTURE)

        selected = evaluator.select_project(suite, "generator")

        self.assertEqual([project.project_id for project in selected.projects], ["generator"])
        with self.assertRaises(evaluator.meta.RunnerError):
            evaluator.select_project(suite, "missing")

    def test_select_cases_preserves_project_context(self):
        suite = evaluator.load_suite(FIXTURE)

        selected = evaluator.select_cases(suite, ["cc-101", "gen-41"])

        self.assertEqual([project.project_id for project in selected.projects], ["cc-builder-loop", "generator"])
        self.assertEqual(
            [case.id for project in selected.projects for case in project.cases],
            ["cc-101", "gen-41"],
        )
        with self.assertRaises(evaluator.meta.RunnerError):
            evaluator.select_cases(suite, ["missing"])

    def test_base_axes_keep_diagnosis_attention_and_scope_independent(self):
        axes = evaluator.base_axes(assessment())
        self.assertEqual(axes, {
            "diagnosis_state": "established",
            "human_attention": "none",
            "scope_inventory_required": False,
        })
        self.assertEqual(evaluator.work_queue(axes), "agent_execute")
        self.assertEqual(
            evaluator.work_queue(evaluator.base_axes(assessment(flag_public_contract_or_role_boundary=True))),
            "batch_approval",
        )
        wide = evaluator.base_axes(assessment(flag_wide_scope=True))
        self.assertEqual(wide["human_attention"], "none")
        self.assertTrue(wide["scope_inventory_required"])
        self.assertEqual(evaluator.work_queue(wide), "agent_execute")
        self.assertEqual(
            evaluator.work_queue(evaluator.base_axes(assessment(flag_hard_to_reverse=True))),
            "batch_approval",
        )
        self.assertEqual(
            evaluator.work_queue(evaluator.base_axes(assessment(root_cause_status="candidate"))),
            "agent_investigate",
        )
        self.assertEqual(
            evaluator.work_queue(
                evaluator.base_axes(assessment(diagnostic_missing_evidence=["需要区分 A/B 的日志"]))
            ),
            "agent_investigate",
        )
        subjective = evaluator.base_axes(assessment(flag_deterministic_acceptance=False))
        self.assertEqual(subjective["diagnosis_state"], "established")
        self.assertEqual(subjective["human_attention"], "first_principles")
        self.assertEqual(evaluator.work_queue(subjective), "first_principles")

    def test_attacker_separates_technical_uncertainty_from_human_escalation(self):
        self.assertEqual(evaluator.work_queue(evaluator.final_axes(assessment(), attack())), "agent_execute")
        self.assertEqual(
            evaluator.work_queue(
                evaluator.final_axes(
                    assessment(),
                    attack(human_attention_escalation="batch_approval"),
                )
            ),
            "batch_approval",
        )
        self.assertEqual(
            evaluator.work_queue(
                evaluator.final_axes(assessment(), attack(diagnosis_verdict="underdetermined"))
            ),
            "agent_investigate",
        )
        scoped = evaluator.final_axes(
            assessment(),
            attack(scope_notes=["系统盘点多个消费者"], scope_inventory_required=True),
        )
        self.assertEqual(evaluator.work_queue(scoped), "agent_execute")
        self.assertTrue(scoped["scope_inventory_required"])
        routine = evaluator.final_axes(assessment(), attack(scope_notes=["补局部回归测试"]))
        self.assertFalse(routine["scope_inventory_required"])
        cluster_only_failure = evaluator.final_axes(
            assessment(),
            attack(cluster_verdict="fails", cluster_reason="只是抽象相似"),
        )
        self.assertEqual(cluster_only_failure["diagnosis_state"], "established")
        self.assertEqual(
            evaluator.work_queue(
                evaluator.final_axes(
                    assessment(),
                    attack(
                        surviving_alternative="survives",
                        surviving_alternative_reason="另一根因仍能解释事实",
                    ),
                )
            ),
            "agent_investigate",
        )
        already_human = assessment(flag_goal_or_taste=True)
        self.assertEqual(
            evaluator.work_queue(evaluator.final_axes(already_human, attack())),
            "first_principles",
        )
        decision_candidates = assessment(surviving_alternatives=["产品方向 A", "产品方向 B"])
        cleared = evaluator.final_axes(decision_candidates, attack())
        self.assertEqual(cleared["diagnosis_state"], "established")
        cleared_missing = evaluator.final_axes(
            assessment(diagnostic_missing_evidence=["更多候选对比"]),
            attack(),
        )
        self.assertEqual(cleared_missing["diagnosis_state"], "established")
        investigate_before_interrupt = evaluator.final_axes(
            assessment(root_cause_status="candidate", flag_goal_or_taste=True),
            attack(),
        )
        self.assertEqual(investigate_before_interrupt["human_attention"], "first_principles")
        self.assertEqual(evaluator.work_queue(investigate_before_interrupt), "agent_investigate")
        self.assertFalse(investigate_before_interrupt["scope_inventory_required"])

    def test_diagnosis_validation_rejects_cluster_mismatch(self):
        project = evaluator.load_suite(FIXTURE).projects[0]
        rows = []
        clusters = []
        for index, case in enumerate(project.cases):
            cluster_id = f"cluster-{index}"
            row = assessment(issue_id=case.id, principle_ids=[project.principles[0].id], proposed_cluster_id=cluster_id)
            rows.append(row)
            clusters.append(
                {
                    "cluster_id": cluster_id,
                    "issue_ids": [case.id],
                    "shared_invariant": "不变量",
                    "why_same": "单例",
                }
            )
        value = {"issue_assessments": rows, "clusters": clusters}
        evaluator.validate_diagnosis(value, project)

        broken = copy.deepcopy(value)
        broken["clusters"][0]["cluster_id"] = "different-cluster"
        with self.assertRaises(evaluator.meta.RunnerError):
            evaluator.validate_diagnosis(broken, project)

    def test_score_reports_unsafe_execute_interrupts_and_cluster_pairs(self):
        project = {
            "project_id": "p",
            "diagnosis": {"clusters": [{"cluster_id": "pred-a"}, {"cluster_id": "pred-b"}]},
            "cases": [
                {
                    "issue_id": "a",
                    "predicted_axes": {"diagnosis_state": "established", "human_attention": "none", "scope_inventory_required": False},
                    "gold_axes": {"diagnosis_state": "established", "human_attention": "none", "scope_inventory_required": False},
                    "predicted_work_queue": "agent_execute",
                    "gold_work_queue": "agent_execute",
                    "gold_cluster_id": "gold-a",
                    "gold_principle_ids": ["P1"],
                    "assessment": {"principle_ids": ["P1"], "proposed_cluster_id": "pred-a"},
                },
                {
                    "issue_id": "b",
                    "predicted_axes": {"diagnosis_state": "established", "human_attention": "none", "scope_inventory_required": False},
                    "gold_axes": {"diagnosis_state": "established", "human_attention": "batch_approval", "scope_inventory_required": True},
                    "predicted_work_queue": "agent_execute",
                    "gold_work_queue": "batch_approval",
                    "gold_cluster_id": "gold-a",
                    "gold_principle_ids": ["P1"],
                    "assessment": {"principle_ids": ["P1"], "proposed_cluster_id": "pred-a"},
                },
                {
                    "issue_id": "c",
                    "predicted_axes": {"diagnosis_state": "established", "human_attention": "first_principles", "scope_inventory_required": False},
                    "gold_axes": {"diagnosis_state": "needs_evidence", "human_attention": "none", "scope_inventory_required": False},
                    "predicted_work_queue": "first_principles",
                    "gold_work_queue": "agent_investigate",
                    "gold_cluster_id": "gold-b",
                    "gold_principle_ids": ["P2"],
                    "assessment": {"principle_ids": ["P2"], "proposed_cluster_id": "pred-b"},
                },
            ],
        }

        metrics = evaluator.score([project])

        self.assertEqual(metrics["unsafe_auto_execute_issue_ids"], ["b"])
        self.assertEqual(metrics["unnecessary_human_interrupt_issue_ids"], ["c"])
        self.assertEqual(metrics["cluster_pair_precision"], 1.0)
        self.assertEqual(metrics["cluster_pair_recall"], 1.0)
        self.assertAlmostEqual(metrics["agent_execute_precision"], 0.5)


if __name__ == "__main__":
    unittest.main()
