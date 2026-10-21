"""Public-transaction delivery fixtures; model outputs are explicitly synthetic."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

from harness import ROOT, git
from runtime.codex_builder_loop.assurance_v4 import core
from runtime.codex_builder_loop.assurance_v4.store import read_ledger
from tests.test_assurance_v4_contract import blackbox_case_results


def kill_after_saved_event(fixture, event: str) -> None:
    # Only the interruption is injected. The subprocess runs the public Core
    # operation and its real atomic store; it never constructs or edits a ledger.
    program = """
import os, signal, sys
from runtime.codex_builder_loop.assurance_v4 import core
from runtime.codex_builder_loop.assurance_v4.store import read_ledger
original = core._store_save_ledger
initial_events = len(read_ledger(core.resolve_repo(sys.argv[1]), sys.argv[2])["events"])
def save(repo, ledger):
    original(repo, ledger)
    if any(e["kind"] == sys.argv[4] for e in ledger["events"][initial_events:]):
        os.kill(os.getpid(), signal.SIGKILL)
core._store_save_ledger = save
core.apply_dispatch_result(sys.argv[1], sys.argv[2], action_id=sys.argv[3], driver_runtime_kind="native")
"""
    process = subprocess.run(
        [sys.executable, "-c", program, str(fixture.repo), fixture.run_id,
         fixture.action["action_id"], event],
        cwd=ROOT, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=30,
    )
    fixture.assertEqual(process.returncode, -signal.SIGKILL, process.stderr)


def complete_delivery(fixture, *, kill_reviewer: bool = False) -> dict:
    repo, run_id = fixture.repo, fixture.run_id
    core.verify_machine(repo, run_id)
    fixture.action = fixture.begin("tester")
    fixture.assertEqual(fixture.action["action"], "tester_blackbox")
    ledger = read_ledger(repo, run_id)
    candidate = ledger["facets"]["execution"]["candidate_head"]
    executions = []
    for command in ledger["facets"]["execution"]["commands"]:
        observed = subprocess.run(
            command["argv"], cwd=ledger["candidate_worktree"],
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, timeout=command["timeout_seconds"],
        )
        fixture.assertEqual(observed.returncode, 0, observed.stderr)
        executions.append({"id": command["id"], "argv": command["argv"],
                           "returncode": observed.returncode, "timed_out": False})
    fixture.assertEqual(git(fixture.candidate, "rev-parse", "HEAD"), candidate)
    fixture.result = {
        "result": "pass", "problem_report": None, "proof_spec": None,
        "evidence_report": {
            "schema_version": 1, "kind": "blackbox", "status": "pass",
            "candidate_head": candidate,
            "producer": {"role": "tester", **ledger["facets"]["execution"]["agents"]["tester"]},
            "details": {
                "result": "pass", "worktree": str(fixture.candidate),
                "before_head": candidate, "after_head": candidate,
                "executions": executions, "cases": blackbox_case_results(ledger),
            },
        },
    }
    core.complete_dispatch(repo, run_id, action_id=fixture.action["action_id"],
                           result_value=fixture.result)
    fixture.apply()
    core.prepare_reviewer(repo, run_id, "reviewer", "reviewer-thread")
    fixture.action = fixture.begin("reviewer")
    fixture.assertEqual(fixture.action["action"], "reviewer_final")
    fixture.result = {
        "result": "pass", "problem_report": None, "proof_spec": None,
        "evidence_report": {
            "schema_version": 1, "kind": "reviewer", "status": "pass",
            "candidate_head": candidate,
            "producer": {"role": "reviewer", "agent_id": "reviewer", "thread_id": "reviewer-thread"},
            "details": {"result": "pass", "reviewed_head": candidate},
        },
    }
    core.complete_dispatch(repo, run_id, action_id=fixture.action["action_id"],
                           result_value=fixture.result)
    if kill_reviewer:
        kill_after_saved_event(fixture, "evidence_recorded")
    fixture.apply()
    result = core.finalize(repo, run_id, "fix(fixture): [cr_id_skip] Deliver calculator recovery")
    fixture.assertEqual(result["phase"], "finalized")
    final_head = read_ledger(repo, run_id)["final_head"]
    fixture.assertEqual(git(repo, "rev-parse", "HEAD"), final_head)
    fixture.assertEqual(git(repo, "status", "--porcelain"), "")
    return {"phase": result["phase"], "final_head": final_head, "executions": executions}
