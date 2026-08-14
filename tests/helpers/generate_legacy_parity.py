#!/usr/bin/env python3
"""Materialize the frozen legacy shell-fixture corpus at behavior-case granularity."""

from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SOURCE_COMMIT = "185ee02a259f6a0d7917523a4e20481e6246e0b2"
SOURCE_TREE = "2bcc4c4b0adf83f0d1ca1a282a41ba3584ee87e2"
SOURCE_PREFIX = "skills/builder-loop/fixtures/e2e"
DISPOSITION_COMMIT = "8fe1e13230b2310d21bb9e3d95f8f62563cda836"
DISPOSITION_PATH = "tests/fixtures/legacy_feature_parity.json"
ASSERTION = re.compile(r"\s*(?:assert|assert_[A-Za-z0-9_]+)\s")
SECTION = re.compile(r'\s*section\s+"([^"]+)"')


REPRESENTATIVE_TEST_IDS = {
    "test_abandon_contract": "tests.test_abandon_contract.AbandonContractTest.test_abandon_preserves_role_worktrees_and_is_idempotent",
    "test_agent_thread_resume": "tests.test_agent_thread_resume.AgentThreadResumeContractTest.test_tester_and_reviewer_each_create_once_then_follow_up_same_thread",
    "test_assurance_v4_lineage_contract": "tests.test_assurance_v4_lineage_contract.AssuranceV4LineageContractTest.test_supersession_never_inherits_role_or_evidence_state",
    "test_conflict_safe_stop": "tests.test_conflict_safe_stop.ConflictSafeStopTest.test_conflict_preserves_main_branches_worktrees_and_ledger",
    "test_diagnostics_progress": "tests.test_diagnostics_progress.DiagnosticsAndProgressContractTest.test_doctor_is_read_only_and_reports_orphan_loop_worktree",
    "test_doc_audit_gate": "tests.test_doc_audit_gate.DocAuditGateTest.test_stale_doc_review_head_blocks_finalize_until_re_reviewed",
    "test_evidence_head_contract": "tests.test_evidence_head_contract.EvidenceHeadContractTest.test_blackbox_evidence_requires_author_integration_and_replay_details",
    "test_evidence_scope": "tests.test_evidence_scope.EvidenceScopeContractTest.test_affecting_change_invalidates_scoped_evidence",
    "test_finalize_lifecycle": "tests.test_finalize_lifecycle.FinalizeLifecycleTest.test_finalization_squashes_all_role_commits_and_cleans_worktrees",
    "test_full_driver_v4_contract": "tests.test_full_driver_v4_contract.FullDriverV4ContractTest.test_skill_automatically_loops_over_the_complete_action_surface",
    "test_hook_runtime_integration": "tests.test_hook_runtime_integration.HookRuntimeIntegrationTest.test_real_hook_finds_run_from_tester_and_builder_worktrees",
    "test_install_contract": "tests.test_install_contract.InstallContractTest.test_install_is_idempotent_and_uninstall_preserves_foreign_content",
    "test_parallel_role_isolation": "tests.test_parallel_role_isolation.ParallelRoleIsolationTest.test_builder_and_tester_start_from_same_spec_head_and_integrate_explicitly",
    "test_plan_contract": "tests.test_plan_contract.PlanContractTest.test_valid_parallel_plan_from_path_and_stdin",
    "test_planning_v3_e2e_contract": "tests.test_planning_v3_e2e_contract.PlanningV3E2EContractTest.test_blackbox_evidence_is_proof_first_and_case_complete",
    "test_reviewer_prerequisite_order": "tests.test_reviewer_prerequisite_order.ReviewerPrerequisiteOrderTest.test_review_completion_must_still_see_verified_candidate",
    "test_role_ownership": "tests.test_role_ownership.RoleOwnershipTest.test_owned_writes_are_ready",
    "test_start_contract": "tests.test_start_contract.StartContractTest.test_task_start_generates_run_id_and_public_string_worktrees",
    "test_stop_hook_gate": "tests.test_stop_hook_gate.StopHookGateTest.test_active_run_blocks_root_stop",
    "test_verify_contract": "tests.test_verify_contract.VerifyContractTest.test_pass_records_verified_head",
    "test_workspace_intake": "tests.test_workspace_intake.WorkspaceIntakeContractTest.test_snapshot_injects_exact_dirty_paths_without_changing_target",
}

DOC_REFERENCE_TEST_IDS = {
    "mechanical": "tests.test_doc_reference_scan.DocReferenceScanTest.test_qualified_pointer_move_is_a_mechanical_finding",
    "clean": "tests.test_doc_reference_scan.DocReferenceScanTest.test_updated_pointer_signature_change_and_history_are_clean",
    "semantic": "tests.test_doc_reference_scan.DocReferenceScanTest.test_symbol_only_reference_is_semantic_not_mechanical",
    "failure": "tests.test_doc_reference_scan.DocReferenceScanTest.test_scan_failure_is_fail_closed",
}

ADMISSION_TEST_IDS = {
    "candidate": "tests.test_assurance_v4_admission_contract.AssuranceV4AdmissionContractTest.test_repository_executable_is_candidate_bound_and_deferred",
    "execution": "tests.test_assurance_v4_admission_contract.AssuranceV4AdmissionContractTest.test_machine_execution_rechecks_executable_after_start",
}

ORPHAN_WORKTREE_CASE_OVERRIDES = {
    "A1:": {
        "status": "rescue",
        "replacement": "native development-worktree inventory and intent recovery",
        "rationale": "Unknown worktrees are diagnosed without mutation, while only exact persisted create or finish intents are recoverable.",
        "test_ids": [
            "tests.test_dev_worktree_lifecycle.DevWorktreeLifecycleTest.test_status_classifies_unknown_missing_unregistered_and_branch_only",
            "tests.test_dev_worktree_lifecycle.DevWorktreeLifecycleTest.test_state_schema_and_doctor_recovery_signal",
        ],
    },
    "A2:": {
        "status": "rescue",
        "replacement": "identity-bound create-intent recovery",
        "rationale": "Path-only adoption is retired; a worktree is resumed only when its repository, path, branch and HEAD match a persisted create intent.",
        "test_ids": [
            "tests.test_dev_worktree_lifecycle.DevWorktreeLifecycleTest.test_create_intent_recovers_before_and_after_git_add",
        ],
    },
    "A3:": {
        "status": "rescue",
        "replacement": "localized collision checks",
        "rationale": "Unrelated unknown worktrees remain visible but no longer require a global ignore flag before a collision-free managed create.",
        "test_ids": [
            "tests.test_dev_worktree_lifecycle.DevWorktreeLifecycleTest.test_status_classifies_unknown_missing_unregistered_and_branch_only",
        ],
    },
    "A4:": {
        "status": "rescue",
        "replacement": "absolute managed-root and state identity validation",
        "rationale": "Invalid, repository-internal or symlinked roots are rejected before any worktree intent is written.",
        "test_ids": [
            "tests.test_dev_worktree_lifecycle.DevWorktreeLifecycleTest.test_root_rejects_repository_internal_and_symlink_paths",
        ],
    },
    "A5:": {
        "status": "rescue",
        "replacement": "preserved unknown-residue inventory",
        "rationale": "Unknown worktree changes are reported with residue facts but never imported into a new delivery branch.",
        "test_ids": [
            "tests.test_dev_worktree_lifecycle.DevWorktreeLifecycleTest.test_status_classifies_unknown_missing_unregistered_and_branch_only",
            "tests.test_dev_worktree_lifecycle.DevWorktreeLifecycleTest.test_status_does_not_mutate_unknown_worktrees",
        ],
    },
    "A6:": {
        "status": "rescue",
        "replacement": "complete read-only native worktree inventory",
        "rationale": "All managed-root, registered, unregistered and branch-only findings are returned in one read-only report.",
        "test_ids": [
            "tests.test_dev_worktree_lifecycle.DevWorktreeLifecycleTest.test_status_classifies_unknown_missing_unregistered_and_branch_only",
        ],
    },
    "A7:": {
        "status": "rescue",
        "replacement": "absolute managed-root validation",
        "rationale": "Relative roots are rejected rather than interpreted against the caller's current directory.",
        "test_ids": [
            "tests.test_dev_worktree_lifecycle.DevWorktreeLifecycleTest.test_root_rejects_repository_internal_and_symlink_paths",
        ],
    },
    "A8:": {
        "status": "retired",
        "replacement": "single managed worktree create surface",
        "rationale": "Bare mode and path-only reuse flags are absent, so their legacy mutual-exclusion branch has no current behavior to preserve.",
        "test_ids": [
            "tests.test_dev_worktree_lifecycle.DevWorktreeLifecycleTest.test_create_uses_neutral_managed_root_and_excludes_target_dirty",
        ],
    },
}


def git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True)


def method_exists(test_id: str) -> bool:
    parts = test_id.split(".")
    if len(parts) < 4 or parts[0] != "tests":
        return False
    path = ROOT.joinpath(*parts[:2]).with_suffix(".py")
    if not path.is_file():
        return False
    tree = ast.parse(path.read_text(encoding="utf-8"))
    class_name, method_name = parts[-2:]
    return any(
        isinstance(node, ast.ClassDef)
        and node.name == class_name
        and any(
            isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            and child.name == method_name
            for child in node.body
        )
        for node in tree.body
    )


def reference_override(fixture: str, title: str) -> tuple[str, str] | None:
    match = re.search(r"Case\s+(\d+)", title)
    number = int(match.group(1)) if match else None
    if fixture == "test-diff-level-check.sh" and number in range(22, 28):
        kind = {
            22: "mechanical",
            23: "clean",
            24: "semantic",
            25: "clean",
            26: "clean",
            27: "failure",
        }[number]
        return kind, "Assurance v4 ledger-bound documentation reference scan"
    if fixture == "test-doc-lint.sh" and number in range(10, 14):
        kind = {10: "mechanical", 11: "semantic", 12: "failure", 13: "clean"}[
            number
        ]
        return kind, "Assurance v4 ledger-bound documentation reference scan"
    return None


def admission_test_id(fixture: str, title: str) -> str | None:
    if fixture == "test-pass-cmd-runs-worktree.sh" and title.startswith("Step 4:"):
        return ADMISSION_TEST_IDS["candidate"]
    if fixture == "test-run-pass-cmd-args.sh" and title.startswith("Case 5:"):
        return ADMISSION_TEST_IDS["execution"]
    return None


def case_override(fixture: str, title: str) -> dict[str, Any] | None:
    if fixture != "test-orphan-worktree-reuse.sh":
        return None
    for prefix, value in ORPHAN_WORKTREE_CASE_OVERRIDES.items():
        if title.startswith(prefix):
            return value
    return None


def fixture_blob(path: str) -> str:
    line = git("ls-tree", SOURCE_COMMIT, "--", path).strip()
    parts = line.split()
    if len(parts) < 3 or parts[1] != "blob":
        raise RuntimeError(f"legacy fixture blob is missing: {path}")
    return parts[2]


def cases_for(fixture: str, text: str, disposition: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
    sections: list[dict[str, Any]] = []
    preconditions = 0
    current: dict[str, Any] | None = None
    for line_number, line in enumerate(text.splitlines(), 1):
        match = SECTION.match(line)
        if match:
            current = {
                "id": f"case-{len(sections) + 1:03d}",
                "title": match.group(1),
                "source_line": line_number,
                "assertion_count": 0,
            }
            sections.append(current)
            continue
        if ASSERTION.match(line):
            if current is None:
                preconditions += 1
            else:
                current["assertion_count"] += 1
    replacement_ids = [REPRESENTATIVE_TEST_IDS[item] for item in disposition["test_ids"]]
    for test_id in replacement_ids:
        if not method_exists(test_id):
            raise RuntimeError(f"replacement test id is missing: {test_id}")
    for case in sections:
        explicit = case_override(fixture, case["title"])
        if explicit is not None:
            for test_id in explicit["test_ids"]:
                if not method_exists(test_id):
                    raise RuntimeError(f"case override test id is missing: {test_id}")
            case.update(explicit)
            continue
        override = reference_override(fixture, case["title"])
        if override is None:
            test_ids = list(replacement_ids)
            admission_id = admission_test_id(fixture, case["title"])
            if admission_id is not None:
                if not method_exists(admission_id):
                    raise RuntimeError(f"admission replacement test id is missing: {admission_id}")
                test_ids.append(admission_id)
            case.update(
                status=disposition["status"],
                replacement=disposition["replacement"],
                rationale=disposition["rationale"],
                test_ids=test_ids,
            )
            continue
        kind, replacement = override
        case.update(
            status="rescue",
            replacement=replacement,
            rationale=(
                "The legacy qualified-pointer behavior was lost during the v4 migration and is "
                "restored as a Git-object-bound runtime scan before final review."
            ),
            test_ids=[DOC_REFERENCE_TEST_IDS[kind]],
        )
    return preconditions, sections


def main() -> int:
    old_path = ROOT / "tests" / "fixtures" / "legacy_feature_parity.json"
    dispositions = json.loads(git("show", f"{DISPOSITION_COMMIT}:{DISPOSITION_PATH}"))
    if not isinstance(dispositions, list):
        raise RuntimeError("frozen legacy fixture dispositions are not a list")
    fixtures: list[dict[str, Any]] = []
    assertion_total = 0
    case_total = 0
    for disposition in dispositions:
        fixture = disposition["fixture"]
        source_path = f"{SOURCE_PREFIX}/{fixture}"
        text = git("show", f"{SOURCE_COMMIT}:{source_path}")
        preconditions, cases = cases_for(fixture, text, disposition)
        case_total += len(cases)
        assertion_total += preconditions + sum(item["assertion_count"] for item in cases)
        fixtures.append(
            {
                "fixture": fixture,
                "source_blob": fixture_blob(source_path),
                "precondition_assertion_count": preconditions,
                "cases": cases,
            }
        )
    if (len(fixtures), case_total, assertion_total) != (51, 252, 824):
        raise RuntimeError(
            "legacy corpus inventory drifted: "
            f"fixtures={len(fixtures)} cases={case_total} assertions={assertion_total}"
        )
    result = {
        "schema_version": 2,
        "source": {
            "commit": SOURCE_COMMIT,
            "tree": SOURCE_TREE,
            "path_prefix": SOURCE_PREFIX,
            "disposition_commit": DISPOSITION_COMMIT,
            "fixture_count": len(fixtures),
            "case_count": case_total,
            "assertion_count": assertion_total,
        },
        "fixtures": fixtures,
    }
    old_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
