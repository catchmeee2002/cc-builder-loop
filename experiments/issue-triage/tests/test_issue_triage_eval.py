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
        "decision_missing_evidence": [],
        "scope_notes": [],
        "flags": {
            "goal_or_taste": False,
            "new_or_changed_principle": False,
            "principle_conflict": False,
            "public_contract_or_role_boundary": False,
            "wide_or_hard_to_reverse": False,
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
        "verdict": "stands",
        "escalation": "none",
        "reason": "站得住",
        "surviving_alternative": "none",
        "surviving_alternative_reason": "没有竞争根因",
        "decision_missing_evidence": [],
        "scope_notes": [],
        "principle_conflict": False,
    }
    value.update(overrides)
    return value


class IssueTriageEvalTests(unittest.TestCase):
    def test_pilot_suite_loads_and_has_balanced_routes(self):
        suite = evaluator.load_suite(FIXTURE)

        self.assertEqual(suite.suite_id, "pilot-2026-07-26")
        self.assertEqual(sum(len(project.cases) for project in suite.projects), 17)
        routes = [case.gold.route for project in suite.projects for case in project.cases]
        self.assertGreaterEqual(routes.count("derived"), 5)
        self.assertGreaterEqual(routes.count("batch_approval"), 4)
        self.assertGreaterEqual(routes.count("needs_first_principles"), 4)

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

    def test_base_route_is_derived_only_for_closed_derivation(self):
        self.assertEqual(evaluator.base_route(assessment()), "derived")
        self.assertEqual(
            evaluator.base_route(assessment(flag_public_contract_or_role_boundary=True)),
            "batch_approval",
        )
        self.assertEqual(
            evaluator.base_route(assessment(flag_wide_or_hard_to_reverse=True)),
            "batch_approval",
        )
        self.assertEqual(
            evaluator.base_route(assessment(root_cause_status="candidate")),
            "needs_first_principles",
        )
        self.assertEqual(
            evaluator.base_route(assessment(decision_missing_evidence=["需要区分 A/B 的日志"])),
            "needs_first_principles",
        )
        self.assertEqual(
            evaluator.base_route(assessment(flag_wide_or_hard_to_reverse=True, scope_notes=["盘点消费者"])),
            "batch_approval",
        )
        self.assertEqual(
            evaluator.base_route(assessment(flag_deterministic_acceptance=False)),
            "needs_first_principles",
        )

    def test_attacker_can_only_escalate(self):
        self.assertEqual(evaluator.final_route(assessment(), attack()), "derived")
        self.assertEqual(
            evaluator.final_route(assessment(), attack(escalation="batch_approval")),
            "batch_approval",
        )
        self.assertEqual(
            evaluator.final_route(assessment(), attack(verdict="underdetermined")),
            "needs_first_principles",
        )
        self.assertEqual(
            evaluator.final_route(
                assessment(),
                attack(escalation="batch_approval", scope_notes=["实施前核对消费者"]),
            ),
            "batch_approval",
        )
        self.assertEqual(
            evaluator.final_route(
                assessment(),
                attack(
                    surviving_alternative="survives",
                    surviving_alternative_reason="另一根因仍能解释事实",
                ),
            ),
            "needs_first_principles",
        )
        already_human = assessment(flag_goal_or_taste=True)
        self.assertEqual(
            evaluator.final_route(already_human, attack(escalation="none")),
            "needs_first_principles",
        )

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

    def test_score_reports_under_escalation_and_cluster_pairs(self):
        project = {
            "project_id": "p",
            "diagnosis": {"clusters": [{"cluster_id": "pred-a"}, {"cluster_id": "pred-b"}]},
            "cases": [
                {
                    "issue_id": "a",
                    "predicted_route": "derived",
                    "gold_route": "derived",
                    "gold_cluster_id": "gold-a",
                    "gold_principle_ids": ["P1"],
                    "assessment": {"principle_ids": ["P1"], "proposed_cluster_id": "pred-a"},
                },
                {
                    "issue_id": "b",
                    "predicted_route": "derived",
                    "gold_route": "batch_approval",
                    "gold_cluster_id": "gold-a",
                    "gold_principle_ids": ["P1"],
                    "assessment": {"principle_ids": ["P1"], "proposed_cluster_id": "pred-a"},
                },
                {
                    "issue_id": "c",
                    "predicted_route": "needs_first_principles",
                    "gold_route": "derived",
                    "gold_cluster_id": "gold-b",
                    "gold_principle_ids": ["P2"],
                    "assessment": {"principle_ids": ["P2"], "proposed_cluster_id": "pred-b"},
                },
            ],
        }

        metrics = evaluator.score([project])

        self.assertEqual(metrics["unsafe_under_escalation_issue_ids"], ["b"])
        self.assertEqual(metrics["over_escalation_issue_ids"], ["c"])
        self.assertEqual(metrics["cluster_pair_precision"], 1.0)
        self.assertEqual(metrics["cluster_pair_recall"], 1.0)
        self.assertAlmostEqual(metrics["hands_off_precision"], 0.5)


if __name__ == "__main__":
    unittest.main()
