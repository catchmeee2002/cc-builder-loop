from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from harness import CLI, cleanup_repo, git, head, init_repo, run_process
from test_assurance_v4_contract import contract_for


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "builder-loop.py"
FALSE_NEGATIVE_FIXTURE = json.loads(
    (
        ROOT
        / "tests"
        / "fixtures"
        / "retrospective_false_negative_chain.json"
    ).read_text(encoding="utf-8")
)


class RetrospectiveCompletionGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo()
        self.tempdir = tempfile.TemporaryDirectory(
            prefix="retrospective-completion-gate-"
        )
        self.artifacts = Path(self.tempdir.name)

    def tearDown(self) -> None:
        cleanup_repo(self.repo)
        self.tempdir.cleanup()

    def invoke(
        self,
        command: str,
        *args: str | Path,
        input_value: object | None = None,
        raw_input: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        self.assertFalse(
            input_value is not None and raw_input is not None,
            "input_value and raw_input are mutually exclusive",
        )
        completed = run_process(
            [
                sys.executable,
                CLI,
                "assurance",
                "--experimental-v4",
                command,
                *args,
            ],
            input_text=(
                raw_input
                if raw_input is not None
                else json.dumps(input_value, ensure_ascii=False)
                if input_value is not None
                else None
            ),
        )
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        self.assertTrue(
            lines,
            f"command={command!r} rc={completed.returncode} "
            f"stdout={completed.stdout!r} stderr={completed.stderr!r}",
        )
        value = json.loads(lines[-1])
        self.assertIsInstance(value, dict, value)
        return completed.returncode, value

    def write_json(self, name: str, value: object) -> Path:
        path = self.artifacts / name
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        return path

    def start_run(self, run_id: str, session_id: str) -> Path:
        contract_path = self.write_json(
            f"{run_id}-contract.json", contract_for(self.repo)
        )
        rc, result = self.invoke(
            "start",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--session-id",
            session_id,
            "--contract",
            contract_path,
        )
        self.assertEqual(rc, 0, result)
        return Path(result["candidate_worktree"]).parent

    def abandon_run(self, run_id: str) -> None:
        rc, result = self.invoke(
            "abandon",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--reason",
            f"terminal fixture {run_id}",
        )
        self.assertEqual(rc, 0, result)

    def record_problems(
        self, run_id: str, problems: list[dict[str, str]]
    ) -> None:
        if not problems:
            return
        rc, result = self.invoke(
            "record-problems",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--report",
            "-",
            "--role",
            "builder",
            "--agent-id",
            f"{run_id}-builder",
            "--thread-id",
            f"{run_id}-builder-thread",
            input_value={"schema_version": 1, "problems": problems},
        )
        self.assertEqual(rc, 0, result)

    def retrospective_status(self, session_id: str) -> dict[str, Any]:
        _rc, result = self.invoke(
            "retrospective-status",
            "--repo",
            self.repo,
            "--session-id",
            session_id,
        )
        return result

    def record_report(
        self,
        session_id: str,
        report: dict[str, Any],
        *,
        replace: bool = False,
        raw_input: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        args: list[str | Path] = [
            "--repo",
            self.repo,
            "--session-id",
            session_id,
            "--report",
            "-",
        ]
        if replace:
            args.append("--replace")
        return self.invoke(
            "record-retrospective",
            *args,
            input_value=None if raw_input is not None else report,
            raw_input=raw_input,
        )

    def install_false_negative_fixture(self) -> list[Path]:
        session_id = FALSE_NEGATIVE_FIXTURE["owner_session_id"]
        run_paths: list[Path] = []
        for item in FALSE_NEGATIVE_FIXTURE["runs"]:
            run_id = item["run_id"]
            run_path = self.start_run(run_id, session_id)
            self.record_problems(run_id, item.get("problems", []))
            self.abandon_run(run_id)
            ledger_path = run_path / "ledger.json"
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            terminal_index = next(
                (
                    index
                    for index, event in enumerate(ledger["events"])
                    if event.get("kind") == "run_abandoned"
                ),
                len(ledger["events"]),
            )
            retained_events = copy.deepcopy(item.get("events", []))
            anchor_at = (
                ledger["events"][terminal_index - 1]["at"]
                if terminal_index
                else ledger["created_at"]
            )
            for event in retained_events:
                event["at"] = anchor_at
            ledger["events"][terminal_index:terminal_index] = retained_events
            ledger["revision_transitions"] = copy.deepcopy(
                item.get("revision_transitions", [])
            )
            if "runtime_identity" in item:
                ledger["runtime_identity"] = copy.deepcopy(
                    item["runtime_identity"]
                )
            ledger_path.write_text(
                json.dumps(ledger, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            run_paths.append(run_path)
        return run_paths

    def repository_state(self, run_paths: list[Path]) -> dict[str, Any]:
        ledgers = {
            path.name: (path / "ledger.json").read_bytes() for path in run_paths
        }
        parsed = {name: json.loads(content) for name, content in ledgers.items()}
        candidate_refs = {
            name: git(self.repo, "rev-parse", ledger["candidate_branch"])
            for name, ledger in parsed.items()
        }
        candidate_heads = {
            name: head(Path(ledger["candidate_worktree"]))
            for name, ledger in parsed.items()
        }
        return {
            "ledgers": ledgers,
            "target_head": head(self.repo),
            "candidate_refs": candidate_refs,
            "candidate_heads": candidate_heads,
            "worktrees": git(self.repo, "worktree", "list", "--porcelain"),
        }

    @staticmethod
    def complete_dispositions(signals: list[dict[str, Any]]) -> list[dict[str, str]]:
        dispositions: list[dict[str, str]] = []
        for signal in signals:
            if signal["severity"] == "mandatory":
                dispositions.append(
                    {
                        "signal_id": signal["signal_id"],
                        "disposition": "issue",
                        "owner": "builder_loop",
                        "reference": (
                            "https://example.invalid/issues/"
                            f"{signal['signal_id']}"
                        ),
                    }
                )
            else:
                dispositions.append(
                    {
                        "signal_id": signal["signal_id"],
                        "disposition": "not-incident",
                        "reason": (
                            "Reviewed retained facts for advisory signal "
                            f"{signal['signal_id']}."
                        ),
                    }
                )
        return dispositions

    def test_noop_active_and_terminal_statuses_are_distinct_and_read_only(self) -> None:
        session_id = "retrospective-status-session"
        noop = self.retrospective_status(session_id)
        self.assertEqual(noop.get("status"), "NOOP", noop)
        self.assertEqual(noop.get("owner_session_id"), session_id, noop)

        run_path = self.start_run("retrospective-status-run", session_id)
        active_before = (run_path / "ledger.json").read_bytes()
        active = self.retrospective_status(session_id)
        self.assertEqual(active.get("status"), "ACTIVE", active)
        self.assertEqual((run_path / "ledger.json").read_bytes(), active_before)

        self.abandon_run("retrospective-status-run")
        terminal_before = (run_path / "ledger.json").read_bytes()
        required = self.retrospective_status(session_id)
        self.assertEqual(required.get("status"), "REQUIRED", required)
        snapshot = required.get("snapshot")
        self.assertIsInstance(snapshot, dict, required)
        self.assertEqual(
            [item["run_id"] for item in snapshot["runs"]],
            ["retrospective-status-run"],
        )
        self.assertRegex(snapshot["snapshot_digest"], r"^[0-9a-f]{64}$")
        self.assertNotIn("required_block", required)
        self.assertEqual((run_path / "ledger.json").read_bytes(), terminal_before)

    def test_false_negative_chain_inventory_is_deterministic_and_honest(self) -> None:
        run_paths = self.install_false_negative_fixture()
        session_id = FALSE_NEGATIVE_FIXTURE["owner_session_id"]
        first = self.retrospective_status(session_id)
        second = self.retrospective_status(session_id)

        self.assertEqual(first.get("status"), "REQUIRED", first)
        self.assertEqual(first.get("snapshot"), second.get("snapshot"))
        snapshot = first["snapshot"]
        self.assertEqual(
            {item["run_id"] for item in snapshot["runs"]},
            {path.name for path in run_paths},
        )
        self.assertEqual(
            {item["root_run_id"] for item in snapshot["runs"]},
            {path.name for path in run_paths},
        )
        signal_ids = [item["signal_id"] for item in snapshot["signals"]]
        self.assertEqual(len(signal_ids), len(set(signal_ids)), snapshot)
        kinds = {item["kind"] for item in snapshot["signals"]}
        self.assertTrue(
            set(FALSE_NEGATIVE_FIXTURE["expected_signal_kinds"]).issubset(kinds),
            snapshot,
        )
        self.assertTrue(
            any(item["severity"] == "mandatory" for item in snapshot["signals"]),
            snapshot,
        )
        self.assertTrue(
            any(item["severity"] == "advisory" for item in snapshot["signals"]),
            snapshot,
        )

        manual_facts = json.dumps(
            [
                item["facts"]
                for item in snapshot["signals"]
                if item["kind"] == "manual-dispatch-recovery"
            ],
            sort_keys=True,
        )
        self.assertIn("operator_recovery", manual_facts)
        self.assertIn("d" * 64, manual_facts)
        self.assertNotIn("5" * 64, manual_facts)

        rc, rejected = self.record_report(
            session_id,
            {
                "schema_version": 1,
                "snapshot_digest": snapshot["snapshot_digest"],
                "dispositions": [],
            },
        )
        self.assertNotEqual(rc, 0, rejected)
        self.assertEqual(
            self.retrospective_status(session_id).get("status"), "REQUIRED"
        )

    def test_report_coverage_visibility_replay_and_staleness_are_fail_closed(self) -> None:
        run_paths = self.install_false_negative_fixture()
        session_id = FALSE_NEGATIVE_FIXTURE["owner_session_id"]
        required = self.retrospective_status(session_id)
        snapshot = required["snapshot"]
        signals = snapshot["signals"]
        digest = snapshot["snapshot_digest"]
        complete = self.complete_dispositions(signals)
        mandatory = next(
            item for item in signals if item["severity"] == "mandatory"
        )
        advisory = next(item for item in signals if item["severity"] == "advisory")

        invalid_reports = []
        invalid_reports.append(
            {
                "schema_version": 1,
                "snapshot_digest": digest,
                "dispositions": complete[:-1],
            }
        )
        invalid_reports.append(
            {
                "schema_version": 1,
                "snapshot_digest": digest,
                "dispositions": [*complete, copy.deepcopy(complete[0])],
            }
        )
        invalid_reports.append(
            {
                "schema_version": 1,
                "snapshot_digest": digest,
                "dispositions": [
                    *complete,
                    {
                        "signal_id": "unknown-signal-0123456789abcdef",
                        "disposition": "needs-user",
                        "reason": "No matching signal exists.",
                    },
                ],
            }
        )
        invalid_reports.append(
            {
                "schema_version": 1,
                "snapshot_digest": "f" * 64,
                "dispositions": complete,
            }
        )
        mandatory_not_incident = copy.deepcopy(complete)
        mandatory_index = next(
            index
            for index, item in enumerate(mandatory_not_incident)
            if item["signal_id"] == mandatory["signal_id"]
        )
        mandatory_not_incident[mandatory_index] = {
            "signal_id": mandatory["signal_id"],
            "disposition": "not-incident",
            "reason": "This mandatory incident cannot be waived.",
        }
        invalid_reports.append(
            {
                "schema_version": 1,
                "snapshot_digest": digest,
                "dispositions": mandatory_not_incident,
            }
        )
        advisory_blank = copy.deepcopy(complete)
        advisory_index = next(
            index
            for index, item in enumerate(advisory_blank)
            if item["signal_id"] == advisory["signal_id"]
        )
        advisory_blank[advisory_index] = {
            "signal_id": advisory["signal_id"],
            "disposition": "not-incident",
            "reason": "   ",
        }
        invalid_reports.append(
            {
                "schema_version": 1,
                "snapshot_digest": digest,
                "dispositions": advisory_blank,
            }
        )

        for index, report in enumerate(invalid_reports):
            with self.subTest(invalid_report=index):
                rc, rejected = self.record_report(session_id, report)
                self.assertNotEqual(rc, 0, rejected)
                current = self.retrospective_status(session_id)
                self.assertEqual(current.get("status"), "REQUIRED", current)
                self.assertFalse(current.get("report"), current)

        needs_user_dispositions = copy.deepcopy(complete)
        needs_user_reason = "User authorization is required before issue routing."
        needs_user_dispositions[mandatory_index] = {
            "signal_id": mandatory["signal_id"],
            "disposition": "needs-user",
            "reason": needs_user_reason,
        }
        needs_user_report = {
            "schema_version": 1,
            "snapshot_digest": digest,
            "dispositions": needs_user_dispositions,
        }

        immutable_before = self.repository_state(run_paths)
        rc, recorded = self.record_report(session_id, needs_user_report)
        self.assertEqual(rc, 0, recorded)
        pending = self.retrospective_status(session_id)
        self.assertEqual(pending.get("status"), "NEEDS_USER", pending)
        pending_block = pending["required_block"]
        pending_user_block = pending["required_user_block"]
        self.assertIn("BUILDER_INPUT_REQUIRED", pending_block)
        self.assertIn(session_id, pending_block)
        self.assertIn(digest, pending_block)
        self.assertIn(needs_user_reason, pending_block)
        for signal in signals:
            self.assertIn(signal["signal_id"], pending_block)
        self.assertIn("Runs:", pending_user_block)
        self.assertIn("Signals:", pending_user_block)
        self.assertIn("Pending:", pending_user_block)
        self.assertIn(needs_user_reason, pending_user_block)
        self.assertIn(mandatory["signal_id"], pending_user_block)
        for signal in signals:
            if signal["signal_id"] != mandatory["signal_id"]:
                self.assertNotIn(signal["signal_id"], pending_user_block)

        first_report = copy.deepcopy(pending["report"])
        rc, replayed = self.record_report(session_id, needs_user_report)
        self.assertEqual(rc, 0, replayed)
        replay_status = self.retrospective_status(session_id)
        self.assertEqual(replay_status.get("report"), first_report)
        self.assertEqual(replay_status.get("required_block"), pending_block)
        self.assertEqual(
            replay_status.get("required_user_block"), pending_user_block
        )

        conflicting = copy.deepcopy(needs_user_report)
        conflicting["dispositions"][mandatory_index]["reason"] = (
            "A different same-snapshot decision."
        )
        rc, rejected = self.record_report(session_id, conflicting)
        self.assertNotEqual(rc, 0, rejected)
        self.assertEqual(
            self.retrospective_status(session_id).get("report"), first_report
        )

        ready_report = {
            "schema_version": 1,
            "snapshot_digest": digest,
            "dispositions": complete,
        }
        rc, replaced = self.record_report(
            session_id, ready_report, replace=True
        )
        self.assertEqual(rc, 0, replaced)
        ready = self.retrospective_status(session_id)
        self.assertEqual(ready.get("status"), "READY", ready)
        ready_block = ready["required_block"]
        ready_user_block = ready["required_user_block"]
        report_digest = ready["report"]["report_digest"]
        self.assertIn("BUILDER_RETROSPECTIVE_READY", ready_block)
        self.assertIn(digest, ready_block)
        self.assertIn(report_digest, ready_block)
        for disposition in complete:
            self.assertIn(disposition["signal_id"], ready_block)
            if disposition["disposition"] == "not-incident":
                self.assertIn(disposition["reason"], ready_block)
            else:
                self.assertIn(disposition["reference"], ready_block)
        self.assertEqual(
            ready_user_block.splitlines(),
            [
                "Builder-loop retrospective complete.",
                (
                    f"Runs: {len(snapshot['runs'])}; Signals: {len(signals)}; "
                    f"Issue routes: {sum(item['disposition'] == 'issue' for item in complete)}."
                ),
                f"Report: {report_digest}",
                f"BUILDER_RETROSPECTIVE_READY:{digest}:{report_digest}",
            ],
        )
        for disposition in complete:
            self.assertNotIn(disposition["signal_id"], ready_user_block)
            self.assertNotIn(disposition.get("reference", "<missing>"), ready_user_block)

        ready_report_value = copy.deepcopy(ready["report"])
        rc, malformed = self.record_report(
            session_id,
            ready_report,
            replace=True,
            raw_input="{not-json",
        )
        self.assertNotEqual(rc, 0, malformed)
        self.assertEqual(
            self.retrospective_status(session_id).get("report"),
            ready_report_value,
        )
        self.assertEqual(self.repository_state(run_paths), immutable_before)

        first_ledger = run_paths[0] / "ledger.json"
        changed = json.loads(first_ledger.read_text(encoding="utf-8"))
        changed["events"].append(
            {
                "at": "2026-08-02T00:00:00+00:00",
                "kind": "machine_verified",
                "details": {
                    "status": "fail",
                    "failure_signature": "7" * 64,
                    "duration_ms": 1,
                },
            }
        )
        first_ledger.write_text(
            json.dumps(changed, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        stale_after_fact = self.retrospective_status(session_id)
        self.assertEqual(stale_after_fact.get("status"), "STALE", stale_after_fact)
        fact_digest = stale_after_fact["snapshot"]["snapshot_digest"]
        self.assertNotEqual(fact_digest, digest)
        self.assertNotEqual(stale_after_fact.get("required_block"), ready_block)

        self.start_run("false-negative-root-c", session_id)
        self.abandon_run("false-negative-root-c")
        stale_after_run = self.retrospective_status(session_id)
        self.assertEqual(stale_after_run.get("status"), "STALE", stale_after_run)
        self.assertNotEqual(
            stale_after_run["snapshot"]["snapshot_digest"], fact_digest
        )

    def test_issue_routing_does_not_hide_an_active_run_decision(self) -> None:
        session_id = "retrospective-active-decision-session"
        run_id = "retrospective-active-decision-run"
        self.start_run(run_id, session_id)
        self.record_problems(
            run_id,
            [
                {
                    "key": "builder-loop-routing-gap",
                    "summary": "Builder-loop owns the blocking orchestration problem.",
                    "details": "The active run must wait for an explicit disposition.",
                    "owner": "builder_loop",
                }
            ],
        )

        required = self.retrospective_status(session_id)
        self.assertEqual(required.get("status"), "REQUIRED", required)
        snapshot = required["snapshot"]
        dispositions = self.complete_dispositions(snapshot["signals"])
        rc, recorded = self.record_report(
            session_id,
            {
                "schema_version": 1,
                "snapshot_digest": snapshot["snapshot_digest"],
                "dispositions": dispositions,
            },
        )
        self.assertEqual(rc, 0, recorded)

        pending = self.retrospective_status(session_id)
        self.assertEqual(pending.get("status"), "NEEDS_USER", pending)
        self.assertTrue(
            all(item["disposition"] != "needs-user" for item in pending["report"]["dispositions"]),
            pending,
        )
        user_block = pending["required_user_block"]
        self.assertIn(run_id, user_block)
        self.assertIn("builder_loop_problem_decision", user_block)
        self.assertIn("BUILDER_INPUT_REQUIRED", user_block)
        for item in dispositions:
            if item["disposition"] == "issue":
                self.assertNotIn(item["reference"], user_block)

    def test_real_stop_hook_accepts_only_the_fresh_compact_core_block(self) -> None:
        session_id = "retrospective-real-hook-session"
        run_id = "retrospective-real-hook-run"
        self.start_run(run_id, session_id)
        self.abandon_run(run_id)
        required = self.retrospective_status(session_id)
        snapshot = required["snapshot"]
        rc, recorded = self.record_report(
            session_id,
            {
                "schema_version": 1,
                "snapshot_digest": snapshot["snapshot_digest"],
                "dispositions": self.complete_dispositions(snapshot["signals"]),
            },
        )
        self.assertEqual(rc, 0, recorded)
        ready = self.retrospective_status(session_id)
        self.assertEqual(ready["status"], "READY")
        home = self.artifacts / "real-hook-home"
        home.mkdir()

        def call(last_message: str) -> dict[str, Any]:
            completed = run_process(
                [sys.executable, HOOK],
                cwd=self.repo,
                env={
                    "HOME": str(home),
                    "BUILDER_LOOP_CLI": str(CLI),
                },
                input_text=json.dumps(
                    {
                        "hook_event_name": "Stop",
                        "cwd": str(self.repo),
                        "session_id": session_id,
                        "stop_hook_active": False,
                        "last_assistant_message": last_message,
                    }
                ),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            lines = [line for line in completed.stdout.splitlines() if line.strip()]
            self.assertTrue(lines, completed.stderr)
            return json.loads(lines[-1])

        full_only = call(ready["required_block"])
        self.assertEqual(full_only.get("decision"), "block", full_only)
        self.assertEqual(full_only.get("reason"), ready["required_user_block"])

        compact = call(ready["required_user_block"])
        self.assertNotIn("decision", compact, compact)

    def test_malformed_matching_ledger_is_reported_instead_of_disappearing(self) -> None:
        session_id = "retrospective-malformed-session"
        run_path = self.start_run("retrospective-malformed-run", session_id)
        self.abandon_run("retrospective-malformed-run")
        ledger_path = run_path / "ledger.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        ledger["runtime_identity"]["capture_status"] = "not-a-runtime-status"
        ledger_path.write_text(
            json.dumps(ledger, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        before = ledger_path.read_bytes()

        fatal = self.retrospective_status(session_id)

        self.assertEqual(fatal.get("status"), "FATAL", fatal)
        self.assertIn("retrospective-malformed-run", json.dumps(fatal))
        self.assertNotIn("snapshot", fatal)
        self.assertEqual(
            fatal.get("malformed_ledgers", [{}])[0].get("code"),
            "ASSURANCE_LEDGER_INVALID",
        )
        self.assertEqual(ledger_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
