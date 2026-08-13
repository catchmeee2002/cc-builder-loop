from __future__ import annotations

import copy
import hashlib
import json
import re
import unittest
from pathlib import Path
from typing import Any, Mapping

from harness import (
    ROOT,
    assert_ledger_schema,
    assert_status,
    cleanup_repo,
    commit_all,
    finish_agent_turn,
    git,
    head,
    init_repo,
    l1_plan_markdown,
    load_ledger,
    plan_markdown,
    problem_report,
    problem_snapshot,
    record_problems,
    repo_session_id,
    revised_plan_with_prior_problems,
    run_cli,
    start_agent_turn,
    start_run,
    worktrees_from,
    write_plan,
)


def issue(key: str, *, owner: str = "builder", detail: str | None = None) -> dict[str, str]:
    return {
        "key": key,
        "summary": f"Summary for {key}",
        "details": detail or f"Observable details for {key}",
        "owner": owner,
    }


def without_problem_snapshot(ledger: dict[str, Any]) -> dict[str, Any]:
    legacy = copy.deepcopy(ledger)
    for key in list(legacy):
        if key == "problems" or key.startswith("problem_"):
            legacy.pop(key)
    if isinstance(legacy.get("plan"), dict):
        legacy["plan"].pop("prior_problems", None)
    return legacy


def nested_values(value: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        if key in value:
            found.append(value[key])
        for child in value.values():
            found.extend(nested_values(child, key))
    elif isinstance(value, list):
        for child in value:
            found.extend(nested_values(child, key))
    return found


class ProblemListContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repos: list[Path] = []

    def tearDown(self) -> None:
        for repo in self.repos:
            cleanup_repo(repo)

    def repo(self) -> Path:
        repo = init_repo()
        self.repos.append(repo)
        return repo

    def start_named(self, repo: Path, plan: Path, name: str):
        started = run_cli(
            "start",
            "--repo",
            repo,
            "--plan",
            plan,
            "--run",
            name,
            "--session-id",
            repo_session_id(repo, name),
        )
        assert_status(started, "READY", rc=0)
        return started, Path(started.data["run_path"])

    def reviewer_run(self, repo: Path, name: str):
        plan = write_plan(
            repo,
            l1_plan_markdown(head(repo)),
            name=f"{name}.md",
        )
        started, run_path = self.start_named(repo, plan, name)
        builder, _tester = worktrees_from(started, run_path)
        (builder / "README.md").write_text(f"reviewable {name}\n")
        commit_all(builder, f"prepare {name}")
        assert_status(
            run_cli("role-check", "--run", run_path, "--role", "builder"),
            "READY",
            rc=0,
        )
        return started, run_path

    def complete_problem_turn(
        self,
        run_path: Path,
        *,
        role: str,
        result: str,
        agent_id: str,
    ) -> tuple[str, str]:
        resolved_agent, turn_id = start_agent_turn(
            run_path, role, agent_id=agent_id
        )
        finish_agent_turn(
            run_path,
            role,
            agent_id=resolved_agent,
            turn_id=turn_id,
            result=result,
        )
        return resolved_agent, turn_id

    def abandon_with_report(
        self,
        repo: Path,
        *,
        name: str,
        manifest: Mapping[str, Any],
    ) -> tuple[Path, dict[str, Any], str, str]:
        _started, run_path = self.reviewer_run(repo, name)
        agent_id, turn_id = self.complete_problem_turn(
            run_path,
            role="reviewer",
            result="findings",
            agent_id=f"{name}-reviewer",
        )
        recorded = record_problems(
            run_path,
            source="reviewer",
            source_id=turn_id,
            manifest=manifest,
        )
        assert_status(recorded, "READY", rc=0)
        abandoned = run_cli(
            "abandon", "--run", run_path, "--reason", f"abandon {name}"
        )
        assert_status(abandoned, "COMPLETE", rc=0)
        snapshot = problem_snapshot(run_path, abandoned.data)
        self.assertRegex(snapshot["snapshot_sha256"], r"^[0-9a-f]{64}$")
        return run_path, snapshot, agent_id, turn_id

    def revised_text(
        self,
        repo: Path,
        old_run: Path,
        snapshot: Mapping[str, Any],
        items: list[Mapping[str, Any]],
        *,
        l1: bool = False,
    ) -> str:
        old = load_ledger(old_run)
        base = l1_plan_markdown(head(repo)) if l1 else plan_markdown(head(repo))
        return revised_plan_with_prior_problems(
            base,
            supersedes_run_id=str(old["run_id"]),
            supersedes_plan_sha256=str(old["plan"]["sha256"]),
            snapshot_sha256=str(snapshot["snapshot_sha256"]),
            items=items,
        )

    def assert_no_carried_evidence(self, run_path: Path) -> None:
        evidence = load_ledger(run_path)["evidence"]
        self.assertEqual(
            {key: value for key, value in evidence.items() if value is not None},
            {},
            evidence,
        )

    def test_completed_role_and_coordinator_reports_are_bound_and_replay_safe(self) -> None:
        repo = self.repo()
        _started, run_path = self.reviewer_run(repo, "role-report-binding")
        reviewer_id, turn_id = start_agent_turn(
            run_path, "reviewer", agent_id="problem-reviewer"
        )
        manifest = problem_report(issue("reviewer-finding"))

        active = record_problems(
            run_path,
            source="reviewer",
            source_id=turn_id,
            manifest=manifest,
        )
        self.assertNotEqual(active.returncode, 0, active.data)
        self.assertNotIn("Summary for reviewer-finding", json.dumps(load_ledger(run_path)))

        finish_agent_turn(
            run_path,
            "reviewer",
            agent_id=reviewer_id,
            turn_id=turn_id,
            result="findings",
        )
        self.assertNotIn("Summary for reviewer-finding", json.dumps(load_ledger(run_path)))
        recorded = record_problems(
            run_path,
            source="reviewer",
            source_id=turn_id,
            manifest=manifest,
        )
        assert_status(recorded, "READY", rc=0)
        replay = record_problems(
            run_path,
            source="reviewer",
            source_id=turn_id,
            manifest=manifest,
        )
        assert_status(replay, "NOOP", rc=0)

        changed = record_problems(
            run_path,
            source="reviewer",
            source_id=turn_id,
            manifest=problem_report(
                issue("reviewer-finding", detail="changed replay body")
            ),
        )
        self.assertNotEqual(changed.returncode, 0, changed.data)
        self.assertIn("CONFLICT", str(changed.data.get("code", "")))

        fake_turn = record_problems(
            run_path,
            source="reviewer",
            source_id="not-a-real-completed-turn",
            manifest=problem_report(issue("invented-turn")),
        )
        self.assertNotEqual(fake_turn.returncode, 0, fake_turn.data)

        coordinator = problem_report(issue("coordinator-observation", owner="plan"))
        coordinated = record_problems(
            run_path,
            source="coordinator",
            source_id="issue-141-observation",
            manifest=coordinator,
        )
        assert_status(coordinated, "READY", rc=0)
        coordinated_replay = record_problems(
            run_path,
            source="coordinator",
            source_id="issue-141-observation",
            manifest=coordinator,
        )
        assert_status(coordinated_replay, "NOOP", rc=0)
        coordinated_conflict = record_problems(
            run_path,
            source="coordinator",
            source_id="issue-141-observation",
            manifest=problem_report(
                issue("coordinator-observation", owner="plan", detail="changed")
            ),
        )
        self.assertNotEqual(coordinated_conflict.returncode, 0)
        self.assertIn("CONFLICT", str(coordinated_conflict.data.get("code", "")))
        assert_ledger_schema(run_path)

    def test_all_problem_terminals_require_completed_turn_but_success_does_not(self) -> None:
        terminals = (
            ("tester", "fail", "builder"),
            ("tester", "target_change_required", "plan"),
            ("tester", "blocked", "plan"),
        )
        for index, (role, result, owner) in enumerate(terminals):
            with self.subTest(result=result):
                repo = self.repo()
                plan = write_plan(repo, plan_markdown(head(repo)))
                _started, run_path = self.start_named(repo, plan, f"terminal-{index}")
                agent_id, turn_id = start_agent_turn(
                    run_path, role, agent_id=f"terminal-{index}-tester"
                )
                manifest = problem_report(
                    issue(f"{result.replace('_', '-')}-problem", owner=owner)
                )
                premature = record_problems(
                    run_path,
                    source=role,
                    source_id=turn_id,
                    manifest=manifest,
                )
                self.assertNotEqual(premature.returncode, 0, premature.data)
                finish_agent_turn(
                    run_path,
                    role,
                    agent_id=agent_id,
                    turn_id=turn_id,
                    result=result,
                )
                assert_status(
                    record_problems(
                        run_path,
                        source=role,
                        source_id=turn_id,
                        manifest=manifest,
                    ),
                    "READY",
                    rc=0,
                )

        for index, result in enumerate(("pass", "tests_ready")):
            with self.subTest(result=result):
                repo = self.repo()
                plan = write_plan(repo, plan_markdown(head(repo)))
                _started, run_path = self.start_named(repo, plan, f"success-{index}")
                agent_id, turn_id = start_agent_turn(
                    run_path, "tester", agent_id=f"success-{index}-tester"
                )
                finish_agent_turn(
                    run_path,
                    "tester",
                    agent_id=agent_id,
                    turn_id=turn_id,
                    result=result,
                )
                status = run_cli("status", "--run", run_path)
                self.assertNotEqual(
                    status.data.get("code"), "PROBLEM_REPORT_REQUIRED", status.data
                )

    def test_missing_role_report_blocks_abandon_without_mutation_then_seals_stably(self) -> None:
        repo = self.repo()
        started, run_path = self.reviewer_run(repo, "missing-role-report")
        _builder, _tester = worktrees_from(started, run_path)
        reviewer_id, turn_id = self.complete_problem_turn(
            run_path,
            role="reviewer",
            result="findings",
            agent_id="missing-report-reviewer",
        )
        before = load_ledger(run_path)
        before_phase = before["phase"]
        before_worktrees = copy.deepcopy(before["worktrees"])
        before_ref = git(repo, "rev-parse", "refs/heads/main")
        before_worktree_list = git(repo, "worktree", "list", "--porcelain")

        blocked = run_cli(
            "abandon", "--run", run_path, "--reason", "user abandons fixture"
        )
        assert_status(blocked, "NEEDS_USER", rc=1)
        self.assertEqual(blocked.data.get("code"), "PROBLEM_REPORT_REQUIRED")
        after = load_ledger(run_path)
        self.assertEqual(after["phase"], before_phase)
        self.assertEqual(after["worktrees"], before_worktrees)
        self.assertEqual(git(repo, "rev-parse", "refs/heads/main"), before_ref)
        self.assertEqual(
            git(repo, "worktree", "list", "--porcelain"), before_worktree_list
        )

        status = run_cli("status", "--run", run_path)
        self.assertEqual(status.data.get("phase"), before_phase, status.data)
        pending = status.data.get("pending_problem_sources")
        self.assertIsInstance(pending, list, status.data)
        self.assertIn(turn_id, json.dumps(pending, ensure_ascii=False))
        status_inventory = load_ledger(run_path).get("problem_inventory")
        self.assertIsInstance(status_inventory, dict)
        self.assertIsNone(status_inventory.get("snapshot"))

        doctor = run_cli("doctor", "--run", run_path)
        missing = nested_values(doctor.data, "missing_problem_sources")
        self.assertTrue(missing, doctor.data)
        self.assertIn(turn_id, json.dumps(missing, ensure_ascii=False))
        self.assertEqual(load_ledger(run_path)["phase"], before_phase)
        doctor_inventory = load_ledger(run_path).get("problem_inventory")
        self.assertIsInstance(doctor_inventory, dict)
        self.assertIsNone(doctor_inventory.get("snapshot"))

        manifest = problem_report(issue("abandon-blocker"))
        assert_status(
            record_problems(
                run_path,
                source="reviewer",
                source_id=turn_id,
                manifest=manifest,
            ),
            "READY",
            rc=0,
        )
        abandoned = run_cli(
            "abandon", "--run", run_path, "--reason", "user abandons fixture"
        )
        assert_status(abandoned, "COMPLETE", rc=0)
        first = problem_snapshot(run_path, abandoned.data)
        self.assertRegex(first["snapshot_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(len(first["problem_ids"]), 1)
        self.assertEqual(len(first["problem_ids"]), len(set(first["problem_ids"])))

        again = run_cli("abandon", "--run", run_path)
        assert_status(again, "COMPLETE", rc=0)
        second = problem_snapshot(run_path, again.data)
        self.assertEqual(second["snapshot_sha256"], first["snapshot_sha256"])
        self.assertEqual(second["problem_ids"], first["problem_ids"])
        self.assertEqual(load_ledger(run_path)["phase"], "abandoned")
        assert_ledger_schema(run_path)

    def test_continuity_failure_uses_the_same_report_gate(self) -> None:
        repo = self.repo()
        _started, run_path = self.reviewer_run(repo, "continuity-report-gate")
        _reviewer_id, turn_id = self.complete_problem_turn(
            run_path,
            role="reviewer",
            result="blocked",
            agent_id="continuity-blocked-reviewer",
        )
        (repo / "README.md").write_text("target moved after run start\n")
        commit_all(repo, "move target during continuity fixture")
        continuity = run_cli("status", "--run", run_path)
        assert_status(continuity, "CONTINUITY_FAILURE", rc=1)
        self.assertEqual(load_ledger(run_path)["phase"], "continuity_failure")

        blocked = run_cli("abandon", "--run", run_path, "--reason", "stop")
        assert_status(blocked, "NEEDS_USER", rc=1)
        self.assertEqual(blocked.data.get("code"), "PROBLEM_REPORT_REQUIRED")
        self.assertEqual(load_ledger(run_path)["phase"], "continuity_failure")

        assert_status(
            record_problems(
                run_path,
                source="reviewer",
                source_id=turn_id,
                manifest=problem_report(issue("continuity-blocker", owner="plan")),
            ),
            "READY",
            rc=0,
        )
        abandoned = run_cli("abandon", "--run", run_path, "--reason", "stop")
        assert_status(abandoned, "COMPLETE", rc=0)
        self.assertEqual(
            len(problem_snapshot(run_path, abandoned.data)["problem_ids"]), 1
        )

    def test_abandon_combines_inherited_and_current_problems(self) -> None:
        repo = self.repo()
        old_run, old_snapshot, _agent, _turn = self.abandon_with_report(
            repo,
            name="inherited-old",
            manifest=problem_report(issue("inherited-problem")),
        )
        inherited_id = old_snapshot["problem_ids"][0]
        revised = self.revised_text(
            repo,
            old_run,
            old_snapshot,
            [
                {
                    "problem_id": inherited_id,
                    "handling": "include",
                    "plan_refs": ["behavior:add-positive"],
                }
            ],
        )
        plan = write_plan(repo, revised, name="inherited-revision.md")
        _started, run_path = self.start_named(repo, plan, "inherited-revision")
        assert_status(
            record_problems(
                run_path,
                source="coordinator",
                source_id="new-current-problem",
                manifest=problem_report(issue("current-problem")),
            ),
            "READY",
            rc=0,
        )
        abandoned = run_cli("abandon", "--run", run_path, "--reason", "revise")
        assert_status(abandoned, "COMPLETE", rc=0)
        snapshot = problem_snapshot(run_path, abandoned.data)
        self.assertIn(inherited_id, snapshot["problem_ids"])
        self.assertEqual(len(snapshot["problem_ids"]), 2)

    def test_revision_must_handle_every_prior_problem_with_validate_start_parity(self) -> None:
        repo = self.repo()
        old_run, snapshot, _agent, _turn = self.abandon_with_report(
            repo,
            name="prior-problem-source",
            manifest=problem_report(
                issue("include-me"),
                issue("handled-elsewhere"),
                issue("discard-by-user", owner="plan"),
            ),
        )
        ids = snapshot["problem_ids"]
        self.assertEqual(len(ids), 3)
        complete = [
            {
                "problem_id": ids[0],
                "handling": "include",
                "plan_refs": ["behavior:add-positive"],
            },
            {
                "problem_id": ids[1],
                "handling": "handled_elsewhere",
                "reference": "https://github.com/catchmeee2002/cc-builder-loop/issues/1",
            },
            {
                "problem_id": ids[2],
                "handling": "discard",
                "reason": "User explicitly retired this compatibility behavior.",
            },
        ]
        invalid_cases = {
            "missing": complete[:-1],
            "duplicate": [complete[0], *complete],
            "unknown": [*complete, {**complete[0], "problem_id": "p-unknown"}],
        }
        old_bytes = (old_run / "ledger.json").read_bytes()
        before_worktrees = git(repo, "worktree", "list", "--porcelain")

        for label, items in invalid_cases.items():
            with self.subTest(label=label):
                text = self.revised_text(repo, old_run, snapshot, items)
                path = write_plan(repo, text, name=f"invalid-{label}.md")
                validated = run_cli("plan-validate", "--repo", repo, "--plan", path)
                self.assertNotEqual(validated.returncode, 0, validated.data)
                self.assertEqual((old_run / "ledger.json").read_bytes(), old_bytes)
                self.assertFalse((repo / ".builder-loop" / "codex" / "runs" / f"invalid-{label}").exists())
                started = run_cli(
                    "start",
                    "--repo",
                    repo,
                    "--plan",
                    path,
                    "--run",
                    f"invalid-{label}",
                    "--session-id",
                    repo_session_id(repo, f"invalid-{label}"),
                )
                self.assertNotEqual(started.returncode, 0, started.data)
                self.assertEqual(started.data.get("code"), validated.data.get("code"))
                self.assertEqual((old_run / "ledger.json").read_bytes(), old_bytes)
                self.assertFalse((repo / ".builder-loop" / "codex" / "runs" / f"invalid-{label}").exists())

        wrong_digest = self.revised_text(repo, old_run, snapshot, complete).replace(
            str(snapshot["snapshot_sha256"]), "0" * 64, 1
        )
        free_text = plan_markdown(head(repo)).replace(
            "plan_revision: 1",
            "plan_revision: 2\n"
            "supersedes:\n"
            f'  run_id: {json.dumps(load_ledger(old_run)["run_id"])}\n'
            f'  plan_sha256: {json.dumps(load_ledger(old_run)["plan"]["sha256"])}',
            1,
        ).replace(
            "Implement and independently verify addition behavior.",
            "Implement and independently verify addition behavior while mentioning "
            + ", ".join(ids)
            + " in free text.",
        )
        for label, text in (("wrong-digest", wrong_digest), ("free-text", free_text)):
            with self.subTest(label=label):
                path = write_plan(repo, text, name=f"invalid-{label}.md")
                validated = run_cli("plan-validate", "--repo", repo, "--plan", path)
                started = run_cli(
                    "start",
                    "--repo",
                    repo,
                    "--plan",
                    path,
                    "--run",
                    f"invalid-{label}",
                    "--session-id",
                    repo_session_id(repo, f"invalid-{label}"),
                )
                self.assertNotEqual(validated.returncode, 0, validated.data)
                self.assertNotEqual(started.returncode, 0, started.data)
                self.assertEqual(started.data.get("code"), validated.data.get("code"))
                self.assertEqual((old_run / "ledger.json").read_bytes(), old_bytes)

        bad_references = (
            [{**complete[0], "plan_refs": ["behavior:not-real"]}, *complete[1:]],
            [complete[0], {"problem_id": ids[1], "handling": "handled_elsewhere", "reference": ""}, complete[2]],
            [complete[0], complete[1], {"problem_id": ids[2], "handling": "discard", "reason": ""}],
        )
        for index, items in enumerate(bad_references):
            with self.subTest(invalid_reference=index):
                path = write_plan(
                    repo,
                    self.revised_text(repo, old_run, snapshot, items),
                    name=f"invalid-reference-{index}.md",
                )
                rejected = run_cli("plan-validate", "--repo", repo, "--plan", path)
                self.assertNotEqual(rejected.returncode, 0, rejected.data)
                self.assertEqual((old_run / "ledger.json").read_bytes(), old_bytes)

        valid_path = write_plan(
            repo,
            self.revised_text(repo, old_run, snapshot, complete),
            name="valid-prior-problems.md",
        )
        validated = run_cli("plan-validate", "--repo", repo, "--plan", valid_path)
        assert_status(validated, "READY", rc=0)
        self.assertEqual((old_run / "ledger.json").read_bytes(), old_bytes)
        started, new_run = self.start_named(repo, valid_path, "valid-prior-problems")
        self.assertEqual(started.data.get("status"), "READY")
        new_ledger = load_ledger(new_run)
        persisted_plan = new_ledger["plan"]
        self.assertEqual(
            persisted_plan["prior_problem_snapshot_sha256"],
            snapshot["snapshot_sha256"],
        )
        self.assertEqual(
            persisted_plan["prior_problem_items"],
            complete,
        )
        self.assertIn(
            ids[0],
            json.dumps(new_ledger["problem_inventory"], ensure_ascii=False),
        )
        self.assertEqual(
            git(repo, "worktree", "list", "--porcelain").count("worktree "),
            before_worktrees.count("worktree ") + 2,
        )
        assert_ledger_schema(new_run)

    def test_empty_snapshot_is_explicit_and_l1_include_uses_checklist_refs(self) -> None:
        repo = self.repo()
        plan = write_plan(repo, plan_markdown(head(repo)))
        _started, old_run = self.start_named(repo, plan, "empty-old")
        abandoned = run_cli("abandon", "--run", old_run, "--reason", "empty old run")
        assert_status(abandoned, "COMPLETE", rc=0)
        empty = problem_snapshot(old_run, abandoned.data)
        self.assertEqual(empty["problem_ids"], [])

        explicit = write_plan(
            repo,
            self.revised_text(repo, old_run, empty, []),
            name="explicit-empty-prior.md",
        )
        assert_status(
            run_cli("plan-validate", "--repo", repo, "--plan", explicit),
            "READY",
            rc=0,
        )

        second_repo = self.repo()
        source_run, source_snapshot, _agent, _turn = self.abandon_with_report(
            second_repo,
            name="l1-prior-source",
            manifest=problem_report(issue("l1-problem")),
        )
        problem_id = source_snapshot["problem_ids"][0]
        behavior_ref = write_plan(
            second_repo,
            self.revised_text(
                second_repo,
                source_run,
                source_snapshot,
                [{"problem_id": problem_id, "handling": "include", "plan_refs": ["behavior:add-positive"]}],
                l1=True,
            ),
            name="l1-behavior-ref.md",
        )
        rejected = run_cli(
            "plan-validate", "--repo", second_repo, "--plan", behavior_ref
        )
        self.assertNotEqual(rejected.returncode, 0, rejected.data)

        checklist_ref = write_plan(
            second_repo,
            self.revised_text(
                second_repo,
                source_run,
                source_snapshot,
                [{"problem_id": problem_id, "handling": "include", "plan_refs": ["checklist:1"]}],
                l1=True,
            ),
            name="l1-checklist-ref.md",
        )
        assert_status(
            run_cli("plan-validate", "--repo", second_repo, "--plan", checklist_ref),
            "READY",
            rc=0,
        )

    def test_included_problem_survives_multiple_abandons_without_old_evidence(self) -> None:
        repo = self.repo()
        plan = write_plan(repo, plan_markdown(head(repo)))
        _started, old_run = self.start_named(repo, plan, "many-revisions-old")
        assert_status(run_cli("verify", "--run", old_run), "PASS", rc=0)
        _tester_id, turn_id = self.complete_problem_turn(
            old_run,
            role="tester",
            result="fail",
            agent_id="many-revisions-tester",
        )
        manifest = problem_report(
            issue("carry-forward"),
            issue("handled-elsewhere"),
            issue("discarded", owner="plan"),
        )
        assert_status(
            record_problems(
                old_run,
                source="tester",
                source_id=turn_id,
                manifest=manifest,
            ),
            "READY",
            rc=0,
        )
        abandoned = run_cli("abandon", "--run", old_run, "--reason", "revision one")
        assert_status(abandoned, "COMPLETE", rc=0)
        old_snapshot = problem_snapshot(old_run, abandoned.data)
        carry_id, elsewhere_id, discard_id = old_snapshot["problem_ids"]

        revision_two_items = [
            {"problem_id": carry_id, "handling": "include", "plan_refs": ["behavior:add-positive"]},
            {"problem_id": elsewhere_id, "handling": "handled_elsewhere", "reference": "https://example.invalid/delivery/elsewhere"},
            {"problem_id": discard_id, "handling": "discard", "reason": "User explicitly discarded this item."},
        ]
        revision_two = write_plan(
            repo,
            self.revised_text(repo, old_run, old_snapshot, revision_two_items),
            name="revision-two.md",
        )
        _started_two, run_two = self.start_named(repo, revision_two, "revision-two")
        self.assert_no_carried_evidence(run_two)
        abandoned_two = run_cli("abandon", "--run", run_two, "--reason", "revision two")
        assert_status(abandoned_two, "COMPLETE", rc=0)
        snapshot_two = problem_snapshot(run_two, abandoned_two.data)
        self.assertEqual(snapshot_two["problem_ids"], [carry_id])
        rendered_two = json.dumps(snapshot_two, ensure_ascii=False)
        self.assertIn(turn_id, rendered_two)
        self.assertIn("tester", rendered_two)
        self.assertNotIn(elsewhere_id, snapshot_two["problem_ids"])
        self.assertNotIn(discard_id, snapshot_two["problem_ids"])

        revision_three_text = self.revised_text(
            repo,
            run_two,
            snapshot_two,
            [{"problem_id": carry_id, "handling": "include", "plan_refs": ["behavior:add-positive"]}],
        ).replace("plan_revision: 2", "plan_revision: 3", 1)
        revision_three = write_plan(
            repo, revision_three_text, name="revision-three.md"
        )
        _started_three, run_three = self.start_named(repo, revision_three, "revision-three")
        self.assert_no_carried_evidence(run_three)
        abandoned_three = run_cli(
            "abandon", "--run", run_three, "--reason", "revision three"
        )
        assert_status(abandoned_three, "COMPLETE", rc=0)
        snapshot_three = problem_snapshot(run_three, abandoned_three.data)
        self.assertEqual(snapshot_three["problem_ids"], [carry_id])
        rendered_three = json.dumps(snapshot_three, ensure_ascii=False)
        self.assertIn(turn_id, rendered_three)

    def test_legacy_abandoned_run_requires_explicit_conflict_safe_backfill(self) -> None:
        repo = self.repo()
        plan = write_plan(repo, plan_markdown(head(repo)))
        _started, run_path = self.start_named(repo, plan, "legacy-old")
        unknown = problem_report(
            issue(
                "legacy-problems-unknown",
                owner="plan",
                detail="The abandoned legacy run has no recoverable problem history.",
            )
        )
        active_backfill = run_cli(
            "backfill-problems",
            "--run",
            run_path,
            "--manifest",
            "-",
            input_text=json.dumps(unknown),
        )
        self.assertNotEqual(active_backfill.returncode, 0, active_backfill.data)
        assert_status(
            run_cli("abandon", "--run", run_path, "--reason", "legacy fixture"),
            "COMPLETE",
            rc=0,
        )
        snapshotted_backfill = run_cli(
            "backfill-problems",
            "--run",
            run_path,
            "--manifest",
            "-",
            input_text=json.dumps(unknown),
        )
        self.assertNotEqual(
            snapshotted_backfill.returncode, 0, snapshotted_backfill.data
        )
        path = run_path / "ledger.json"
        path.write_text(json.dumps(without_problem_snapshot(load_ledger(run_path)), indent=2, sort_keys=True) + "\n")
        legacy = load_ledger(run_path)
        protected = {
            key: copy.deepcopy(legacy[key])
            for key in ("plan", "worktrees", "evidence", "phase")
        }

        legacy_status = run_cli("status", "--run", run_path)
        self.assertIs(
            legacy_status.data.get("legacy_problem_snapshot_required"),
            True,
            legacy_status.data,
        )
        legacy_doctor = run_cli("doctor", "--run", run_path)
        self.assertIn(
            True,
            nested_values(legacy_doctor.data, "legacy_problem_snapshot_required"),
            legacy_doctor.data,
        )

        superseding_without_history = revised_plan_with_prior_problems(
            plan_markdown(head(repo)),
            supersedes_run_id=str(legacy["run_id"]),
            supersedes_plan_sha256=str(legacy["plan"]["sha256"]),
            snapshot_sha256="0" * 64,
            items=[],
        )
        missing_path = write_plan(
            repo, superseding_without_history, name="legacy-before-backfill.md"
        )
        rejected = run_cli("plan-validate", "--repo", repo, "--plan", missing_path)
        rejected_start = run_cli(
            "start",
            "--repo",
            repo,
            "--plan",
            missing_path,
            "--run",
            "legacy-before-backfill",
            "--session-id",
            repo_session_id(repo, "legacy-before-backfill"),
        )
        for result in (rejected, rejected_start):
            self.assertNotEqual(result.returncode, 0, result.data)
            self.assertEqual(
                result.data.get("code"),
                "LEGACY_PROBLEM_SNAPSHOT_REQUIRED",
                result.data,
            )

        backfilled = run_cli(
            "backfill-problems",
            "--run",
            run_path,
            "--manifest",
            "-",
            input_text=json.dumps(unknown),
        )
        assert_status(backfilled, "READY", rc=0)
        snapshot = problem_snapshot(run_path, backfilled.data)
        self.assertEqual(len(snapshot["problem_ids"]), 1)
        same = run_cli(
            "backfill-problems",
            "--run",
            run_path,
            "--manifest",
            "-",
            input_text=json.dumps(unknown),
        )
        assert_status(same, "NOOP", rc=0)
        changed = run_cli(
            "backfill-problems",
            "--run",
            run_path,
            "--manifest",
            "-",
            input_text=json.dumps(
                problem_report(
                    issue(
                        "legacy-problems-unknown",
                        owner="plan",
                        detail="conflicting reconstructed history",
                    )
                )
            ),
        )
        self.assertNotEqual(changed.returncode, 0, changed.data)
        self.assertIn("CONFLICT", str(changed.data.get("code", "")))
        current = load_ledger(run_path)
        for key, expected in protected.items():
            self.assertEqual(current[key], expected, key)

        revised = write_plan(
            repo,
            self.revised_text(
                repo,
                run_path,
                snapshot,
                [{"problem_id": snapshot["problem_ids"][0], "handling": "include", "plan_refs": ["behavior:add-positive"]}],
            ),
            name="legacy-after-backfill.md",
        )
        assert_status(
            run_cli("plan-validate", "--repo", repo, "--plan", revised),
            "READY",
            rc=0,
        )

        empty_repo = self.repo()
        empty_plan = write_plan(empty_repo, plan_markdown(head(empty_repo)))
        _empty_started, empty_run = self.start_named(
            empty_repo, empty_plan, "legacy-confirmed-empty"
        )
        assert_status(
            run_cli("abandon", "--run", empty_run, "--reason", "legacy empty"),
            "COMPLETE",
            rc=0,
        )
        empty_path = empty_run / "ledger.json"
        empty_path.write_text(
            json.dumps(
                without_problem_snapshot(load_ledger(empty_run)),
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        confirmed_empty = run_cli(
            "backfill-problems",
            "--run",
            empty_run,
            "--manifest",
            "-",
            input_text=json.dumps(problem_report()),
        )
        assert_status(confirmed_empty, "READY", rc=0)
        empty_snapshot = problem_snapshot(empty_run, confirmed_empty.data)
        self.assertEqual(empty_snapshot["problem_ids"], [])
        confirmed_empty_replay = run_cli(
            "backfill-problems",
            "--run",
            empty_run,
            "--manifest",
            "-",
            input_text=json.dumps(problem_report()),
        )
        assert_status(confirmed_empty_replay, "NOOP", rc=0)
        replayed_empty_snapshot = problem_snapshot(
            empty_run, confirmed_empty_replay.data
        )
        self.assertEqual(
            replayed_empty_snapshot["snapshot_sha256"],
            empty_snapshot["snapshot_sha256"],
        )
        self.assertEqual(replayed_empty_snapshot["problem_ids"], [])

    def test_problem_report_schema_is_the_public_manifest_contract(self) -> None:
        from jsonschema import Draft202012Validator

        schema_path = ROOT / "schema" / "codex-problem-report.schema.json"
        self.assertTrue(schema_path.is_file(), schema_path)
        schema = json.loads(schema_path.read_text())
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        validator.validate(problem_report(issue("valid-public-report")))
        validator.validate(
            {
                "schema_version": 1,
                "problems": [
                    {
                        "key": "tester-identity-invalid",
                        "summary": "Tester identity is invalid",
                        "details": "The current Tester lost independent author status.",
                        "owner": "tester",
                        "producer_continuity": "invalid",
                    }
                ],
            }
        )
        with self.assertRaises(Exception):
            validator.validate(
                {
                    "schema_version": 1,
                    "problems": [
                        {
                            "key": "builder-identity-invalid",
                            "summary": "Builder identity is invalid",
                            "details": "Only Tester may invalidate producer continuity.",
                            "owner": "builder",
                            "producer_continuity": "invalid",
                        }
                    ],
                }
            )
        with self.assertRaises(Exception):
            validator.validate(
                {
                    "schema_version": 1,
                    "problems": [
                        {
                            "key": "missing-owner",
                            "summary": "Missing owner",
                            "details": "This report omits a required owner.",
                        }
                    ],
                }
            )
        self.assertEqual(schema.get("additionalProperties"), False)

    def test_legacy_problem_inventory_preserves_continuity_without_v4_replacement(
        self,
    ) -> None:
        repo = self.repo()
        plan = write_plan(repo, plan_markdown(head(repo)))
        _started, run_path = self.start_named(
            repo, plan, "legacy-continuity-metadata"
        )
        agent_id, turn_id = self.complete_problem_turn(
            run_path,
            role="tester",
            result="fail",
            agent_id="legacy-continuity-tester",
        )
        recorded = record_problems(
            run_path,
            source="tester",
            source_id=turn_id,
            manifest={
                "schema_version": 1,
                "problems": [
                    {
                        "key": "legacy-tester-continuity",
                        "summary": "Legacy Tester continuity is invalid",
                        "details": (
                            "Legacy v2/v3 records the field but does not run the "
                            "Assurance v4 replacement transaction."
                        ),
                        "owner": "tester",
                        "producer_continuity": "invalid",
                    }
                ],
            },
        )
        assert_status(recorded, "READY", rc=0)
        ledger = load_ledger(run_path)
        problem = next(
            item
            for item in ledger["problem_inventory"]["items"]
            if item["key"] == "legacy-tester-continuity"
        )
        self.assertEqual(problem["producer_continuity"], "invalid")
        self.assertNotIn("tester_replacement_intent", ledger)
        status = run_cli("status", "--run", run_path)
        self.assertNotIn(
            "replace_tester",
            json.dumps(status.data, ensure_ascii=False),
        )
        self.assertEqual(ledger["agents"]["tester"]["agent_id"], agent_id)

    def test_active_pre_feature_v2_revision_rehydrates_without_new_plan_marker(self) -> None:
        repo = self.repo()
        parent_plan = write_plan(
            repo,
            plan_markdown(head(repo)),
            name="pre-feature-parent.md",
        )
        _parent_started, parent_run = self.start_named(
            repo, parent_plan, "pre-feature-parent"
        )
        parent_ledger = load_ledger(parent_run)
        assert_status(
            run_cli("abandon", "--run", parent_run, "--reason", "legacy parent"),
            "COMPLETE",
            rc=0,
        )

        plan = write_plan(
            repo,
            plan_markdown(head(repo)),
            name="pre-feature-active-v2.md",
        )
        _started, run_path = self.start_named(repo, plan, "pre-feature-active-v2")

        legacy_plan_text = plan_markdown(head(repo)).replace(
            "plan_revision: 1",
            "plan_revision: 2\n"
            "supersedes:\n"
            f'  run_id: {json.dumps(parent_ledger["run_id"])}\n'
            f'  plan_sha256: {json.dumps(parent_ledger["plan"]["sha256"])}',
            1,
        )
        self.assertIn("plan_revision: 2", legacy_plan_text)
        self.assertIn("supersedes:", legacy_plan_text)
        self.assertNotIn("<!-- prior-problems -->", legacy_plan_text)
        rejected_new_plan = run_cli(
            "plan-validate", "--repo", repo, input_text=legacy_plan_text
        )
        assert_status(rejected_new_plan, "NEEDS_USER", rc=1)
        self.assertEqual(
            rejected_new_plan.data.get("code"), "PLAN_CONTRACT_INVALID"
        )

        tester_id, tester_turn = start_agent_turn(
            run_path, "tester", agent_id="pre-feature-v2-tester"
        )
        finish_agent_turn(
            run_path,
            "tester",
            agent_id=tester_id,
            turn_id=tester_turn,
            result="pass",
        )
        ledger_path = run_path / "ledger.json"
        legacy = load_ledger(run_path)
        self.assertEqual(legacy["schema_version"], 2)
        frozen_path = Path(str(legacy["plan"]["path"]))
        source_path = Path(str(legacy["plan"]["source_path"]))
        frozen_path.write_text(legacy_plan_text)
        source_path.write_text(legacy_plan_text)
        identity_sha256 = hashlib.sha256(legacy_plan_text.encode()).hexdigest()
        legacy.pop("problem_inventory", None)
        legacy["plan"].pop("prior_problem_snapshot_sha256", None)
        legacy["plan"].pop("prior_problem_items", None)
        legacy["plan"]["plan_revision"] = 2
        legacy["plan"]["supersedes_run_id"] = parent_ledger["run_id"]
        legacy["plan"]["supersedes_plan_sha256"] = parent_ledger["plan"][
            "sha256"
        ]
        legacy["plan"]["digest_kind"] = "canonical-v2"
        legacy["plan"]["sha256"] = identity_sha256
        legacy["plan"]["source_sha256"] = identity_sha256
        legacy["plan"]["frozen_sha256"] = identity_sha256
        legacy["plan"].pop("raw_sha256", None)
        ledger_path.write_text(json.dumps(legacy, indent=2, sort_keys=True) + "\n")

        assert_ledger_schema(run_path)
        before = load_ledger(run_path)
        frozen_plan = Path(str(before["plan"]["path"])).read_text()
        source_plan = Path(str(before["plan"]["source_path"])).read_text()
        self.assertIn("plan_revision: 2", frozen_plan)
        self.assertIn("supersedes:", frozen_plan)
        self.assertNotIn("<!-- prior-problems -->", frozen_plan)
        self.assertEqual(source_plan, frozen_plan)
        self.assertEqual(
            {
                before["plan"]["sha256"],
                before["plan"]["source_sha256"],
                before["plan"]["frozen_sha256"],
            },
            {identity_sha256},
        )
        identity = {
            "run_id": before["run_id"],
            "owner_session_id": before["owner_session_id"],
            "plan_sha256": before["plan"]["sha256"],
            "tester": copy.deepcopy(before["agents"]["tester"]),
        }

        continued = run_cli("role-check", "--run", run_path, "--role", "builder")
        assert_status(continued, "READY", rc=0)
        after = load_ledger(run_path)
        self.assertEqual(after["phase"], "active")
        self.assertEqual(after["run_id"], identity["run_id"])
        self.assertEqual(after["owner_session_id"], identity["owner_session_id"])
        self.assertEqual(after["plan"]["sha256"], identity["plan_sha256"])
        self.assertEqual(after["agents"]["tester"], identity["tester"])
        assert_ledger_schema(run_path)

    def test_prior_problem_sha_and_checklist_refs_require_canonical_forms(self) -> None:
        repo = self.repo()
        old_run, snapshot, _agent, _turn = self.abandon_with_report(
            repo,
            name="canonical-prior-source",
            manifest=problem_report(issue("canonical-prior-problem")),
        )
        problem_id = snapshot["problem_ids"][0]
        self.assertEqual(problem_id, problem_id.lower())
        problem_sha_match = re.search(r"[0-9a-f]{64}", problem_id)
        self.assertIsNotNone(problem_sha_match, problem_id)
        problem_sha = problem_sha_match.group(0)
        uppercase_problem_id = (
            problem_id[: problem_sha_match.start()]
            + problem_sha.upper()
            + problem_id[problem_sha_match.end() :]
        )
        self.assertNotEqual(uppercase_problem_id, problem_id)
        snapshot_sha = str(snapshot["snapshot_sha256"])
        self.assertNotEqual(snapshot_sha.upper(), snapshot_sha)
        valid_text = self.revised_text(
            repo,
            old_run,
            snapshot,
            [
                {
                    "problem_id": problem_id,
                    "handling": "include",
                    "plan_refs": ["checklist:1"],
                }
            ],
            l1=True,
        )
        valid = write_plan(repo, valid_text, name="canonical-prior-valid.md")
        assert_status(
            run_cli("plan-validate", "--repo", repo, "--plan", valid),
            "READY",
            rc=0,
        )

        mutations = {
            "uppercase-snapshot-sha": valid_text.replace(
                snapshot_sha,
                snapshot_sha.upper(),
                1,
            ),
            "uppercase-problem-sha": valid_text.replace(
                problem_id,
                uppercase_problem_id,
                1,
            ),
            "noncanonical-checklist-index": valid_text.replace(
                "checklist:1", "checklist:01", 1
            ),
        }
        for label, text in mutations.items():
            with self.subTest(label=label):
                path = write_plan(repo, text, name=f"{label}.md")
                rejected = run_cli(
                    "plan-validate", "--repo", repo, "--plan", path
                )
                assert_status(rejected, "NEEDS_USER", rc=1)
                self.assertEqual(rejected.data.get("code"), "PLAN_CONTRACT_INVALID")


if __name__ == "__main__":
    unittest.main()
