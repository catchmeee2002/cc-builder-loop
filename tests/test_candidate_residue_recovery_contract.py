from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import patch

from harness import cleanup_repo, commit_all, git, head, init_repo
from runtime.codex_builder_loop.assurance_v4 import core
from runtime.codex_builder_loop.assurance_v4.models import ContractError
from runtime.codex_builder_loop.assurance_v4.store import read_ledger
from tests.test_assurance_v4_contract import contract_for


class CandidateResidueRecoveryContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo()

    def tearDown(self) -> None:
        cleanup_repo(self.repo)

    def start(self, run_id: str) -> tuple[dict[str, Any], Path]:
        started = core.start(
            self.repo,
            run_id,
            f"session-{run_id}",
            contract_for(self.repo),
        )
        return started, Path(started["candidate_worktree"]).parent

    def make_residue(self, candidate: Path, *relative_paths: str) -> list[dict[str, str]]:
        exclude = Path(git(candidate, "rev-parse", "--git-path", "info/exclude"))
        if not exclude.is_absolute():
            exclude = (candidate / exclude).resolve()
        exclude.parent.mkdir(parents=True, exist_ok=True)
        rules = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
        for relative in relative_paths:
            path = candidate / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"ignored residue for {relative}\n".encode())
            rules += f"\n/{relative}\n"
        exclude.write_text(rules, encoding="utf-8")
        return [
            {
                "path": relative,
                "sha256": hashlib.sha256((candidate / relative).read_bytes()).hexdigest(),
            }
            for relative in relative_paths
        ]

    def resolve(self, run_id: str, request: dict[str, Any]) -> dict[str, Any]:
        resolver = getattr(core, "resolve_candidate_residue")
        return resolver(self.repo, run_id, request)

    def test_public_residue_recovery_entrypoint_is_available(self) -> None:
        self.assertTrue(callable(getattr(core, "resolve_candidate_residue", None)))

    def record_problem(
        self,
        run_id: str,
        run_path: Path,
        *,
        key: str = "candidate-worktree-ignored-residue",
        owner: str = "builder_loop",
    ) -> dict[str, Any]:
        ledger = read_ledger(self.repo, run_id)
        candidate = ledger["facets"]["execution"]["candidate_head"]
        ledger["problems"].append(
            {
                "key": key,
                "summary": "Ignored residue blocks terminal cleanup",
                "details": "The exact ignored ordinary files require an explicit recovery request.",
                "owner": owner,
                "status": "open",
                "producer": None,
                "candidate_head": candidate,
                "recorded_at": "2026-08-12T00:00:00+00:00",
            }
        )
        (run_path / "ledger.json").write_text(
            json.dumps(ledger, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return core.driver_context(self.repo, run_id)

    def request(
        self,
        context: dict[str, Any],
        files: list[dict[str, str]],
        *,
        key: str = "candidate-worktree-ignored-residue",
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "problem_key": key,
            "problem_snapshot_digest": context["lineage"][
                "open_problem_snapshot_digest"
            ],
            "candidate_head": context["facets"]["execution"]["candidate_head"],
            "reason": "Remove only the exact authorized ignored residue.",
            "files": files,
        }

    def stable_facts(self, run_id: str, candidate: Path) -> dict[str, Any]:
        context = core.driver_context(self.repo, run_id)
        ledger = read_ledger(self.repo, run_id)
        return {
            "phase": context["phase"],
            "head": head(candidate),
            "candidate_branch": git(self.repo, "rev-parse", ledger["candidate_branch"]),
            "target_head": head(self.repo),
            "facets": deepcopy(context["facets"]),
            "digests": deepcopy(ledger["digests"]),
            "evidence": deepcopy(context["evidence"]),
            "agents": deepcopy(context["facets"]["execution"]["agents"]),
            "tester_source": deepcopy(
                context["facets"]["execution"].get("tester_source")
            ),
        }

    def assert_rejected_without_mutation(
        self,
        run_id: str,
        candidate: Path,
        request: dict[str, Any],
    ) -> Exception:
        ledger_path = candidate.parent / "ledger.json"
        before_ledger = ledger_path.read_bytes()
        before_files = {
            item["path"]: (candidate / item["path"]).read_bytes()
            for item in request.get("files", [])
            if isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and (candidate / item["path"]).is_file()
        }
        before_facts = self.stable_facts(run_id, candidate)
        with self.assertRaises((core.AssuranceError, ContractError)) as raised:
            self.resolve(run_id, request)
        self.assertEqual(ledger_path.read_bytes(), before_ledger)
        self.assertEqual(self.stable_facts(run_id, candidate), before_facts)
        for relative, content in before_files.items():
            self.assertEqual((candidate / relative).read_bytes(), content)
        return raised.exception

    def test_exact_manifest_removes_only_bound_residue_and_invalidates_blackbox(self) -> None:
        run_id = "exact-residue"
        started, run_path = self.start(run_id)
        candidate = Path(started["candidate_worktree"])
        files = self.make_residue(candidate, "cache/one.pyc", "cache/two.pyc")
        context = self.record_problem(run_id, run_path)
        request = self.request(context, files)
        ledger = read_ledger(self.repo, run_id)
        candidate_head = ledger["facets"]["execution"]["candidate_head"]
        dependency = "1" * 64
        recorded = "2026-08-12T00:00:00+00:00"
        ledger["evidence"]["blackbox"] = {
            "kind": "blackbox",
            "status": "pass",
            "dependency_digest": dependency,
            "candidate_head": candidate_head,
            "producer": {
                "role": "tester",
                "agent_id": "fixture-tester",
                "thread_id": "fixture-tester-thread",
            },
            "details": {"sentinel": "must be made stale"},
            "recorded_at": recorded,
        }
        ledger["evidence"]["machine"] = {
            "kind": "machine",
            "status": "pass",
            "dependency_digest": dependency,
            "candidate_head": candidate_head,
            "producer": {
                "role": "runtime",
                "agent_id": "fixture-runtime",
                "thread_id": "fixture-runtime-thread",
            },
            "details": {"sentinel": "must be preserved"},
            "recorded_at": recorded,
        }
        (run_path / "ledger.json").write_text(
            json.dumps(ledger, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        before = self.stable_facts(run_id, candidate)

        result = self.resolve(run_id, request)

        self.assertEqual(result["phase"], "active")
        self.assertFalse(any((candidate / item["path"]).exists() for item in files))
        after = self.stable_facts(run_id, candidate)
        for field in (
            "phase",
            "head",
            "candidate_branch",
            "target_head",
            "facets",
            "digests",
            "agents",
            "tester_source",
        ):
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(after["evidence"], before["evidence"])
        self.assertEqual(
            core.status(self.repo, run_id)["readiness"]["states"]["blackbox"],
            "stale",
        )
        ledger = read_ledger(self.repo, run_id)
        problem = next(item for item in ledger["problems"] if item["key"] == request["problem_key"])
        self.assertEqual(problem["status"], "resolved")
        self.assertIsNone(ledger["candidate_residue_intent"])
        self.assertEqual(
            [item["kind"] for item in ledger["events"]].count(
                "candidate_residue_resolved"
            ),
            1,
        )

        repeated = self.resolve(run_id, request)
        self.assertEqual(repeated["phase"], "active")
        ledger = read_ledger(self.repo, run_id)
        self.assertEqual(
            [item["kind"] for item in ledger["events"]].count(
                "candidate_residue_resolved"
            ),
            1,
        )

    def test_wrong_hash_incomplete_extra_escape_duplicate_symlink_and_wrong_owner_reject(self) -> None:
        variants = (
            "wrong-hash",
            "incomplete",
            "extra",
            "escape",
            "duplicate",
            "symlink",
            "wrong-owner",
        )
        for variant in variants:
            with self.subTest(variant=variant):
                run_id = f"residue-{variant}"
                started, run_path = self.start(run_id)
                candidate = Path(started["candidate_worktree"])
                files = self.make_residue(candidate, "cache/one.pyc", "cache/two.pyc")
                owner = "current_project" if variant == "wrong-owner" else "builder_loop"
                context = self.record_problem(run_id, run_path, owner=owner)
                request = self.request(context, deepcopy(files))
                if variant == "wrong-hash":
                    request["files"][0]["sha256"] = "0" * 64
                elif variant == "incomplete":
                    request["files"] = request["files"][:1]
                elif variant == "extra":
                    extra = candidate / "cache/extra.pyc"
                    extra.write_bytes(b"extra\n")
                elif variant == "escape":
                    request["files"][0]["path"] = "../outside.pyc"
                elif variant == "duplicate":
                    request["files"].append(deepcopy(request["files"][0]))
                elif variant == "symlink":
                    path = candidate / request["files"][0]["path"]
                    path.unlink()
                    path.symlink_to(candidate / request["files"][1]["path"])
                self.assert_rejected_without_mutation(run_id, candidate, request)

    def test_stale_problem_candidate_and_pending_transaction_reject(self) -> None:
        for variant in ("stale-problem", "stale-candidate", "pending-transaction"):
            with self.subTest(variant=variant):
                run_id = f"residue-{variant}"
                started, run_path = self.start(run_id)
                candidate = Path(started["candidate_worktree"])
                files = self.make_residue(candidate, "cache/only.pyc")
                context = self.record_problem(run_id, run_path)
                request = self.request(context, files)
                if variant == "stale-problem":
                    request["problem_snapshot_digest"] = "0" * 64
                elif variant == "stale-candidate":
                    request["candidate_head"] = "0" * 40
                else:
                    ledger = read_ledger(self.repo, run_id)
                    ledger["finalize_intent"] = {
                        "expected_target_head": head(self.repo),
                        "candidate_head": head(candidate),
                        "final_head": head(candidate),
                        "changed_files": [],
                    }
                    (run_path / "ledger.json").write_text(
                        json.dumps(ledger, ensure_ascii=False, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                self.assert_rejected_without_mutation(run_id, candidate, request)

    def test_crash_replay_finishes_identical_request_once_and_preserves_changed_file(self) -> None:
        run_id = "residue-crash-replay"
        started, run_path = self.start(run_id)
        candidate = Path(started["candidate_worktree"])
        files = self.make_residue(candidate, "cache/one.pyc", "cache/two.pyc")
        context = self.record_problem(run_id, run_path)
        request = self.request(context, files)
        original_unlink = Path.unlink
        calls = 0

        def crash_between_deletions(path: Path, *args: Any, **kwargs: Any) -> None:
            nonlocal calls
            calls += 1
            original_unlink(path, *args, **kwargs)
            if calls == 1:
                raise RuntimeError("injected residue crash")

        with patch.object(
            Path,
            "unlink",
            autospec=True,
            side_effect=crash_between_deletions,
        ):
            with self.assertRaisesRegex(RuntimeError, "injected residue crash"):
                self.resolve(run_id, request)
        ledger = read_ledger(self.repo, run_id)
        self.assertIsInstance(ledger["candidate_residue_intent"], dict)
        self.assertEqual(sum((candidate / item["path"]).exists() for item in files), 1)

        completed = self.resolve(run_id, request)
        self.assertEqual(completed["phase"], "active")
        self.assertFalse(any((candidate / item["path"]).exists() for item in files))
        ledger = read_ledger(self.repo, run_id)
        self.assertIsNone(ledger["candidate_residue_intent"])

        changed_run = "residue-crash-changed"
        started, changed_path = self.start(changed_run)
        changed_candidate = Path(started["candidate_worktree"])
        changed_files = self.make_residue(
            changed_candidate, "cache/one.pyc", "cache/two.pyc"
        )
        changed_context = self.record_problem(changed_run, changed_path)
        changed_request = self.request(changed_context, changed_files)
        calls = 0
        with patch.object(
            Path,
            "unlink",
            autospec=True,
            side_effect=crash_between_deletions,
        ):
            with self.assertRaisesRegex(RuntimeError, "injected residue crash"):
                self.resolve(changed_run, changed_request)
        remaining = next(
            changed_candidate / item["path"]
            for item in changed_files
            if (changed_candidate / item["path"]).exists()
        )
        remaining.write_bytes(b"changed after crash\n")
        with self.assertRaises(core.AssuranceError):
            self.resolve(changed_run, changed_request)
        self.assertEqual(remaining.read_bytes(), b"changed after crash\n")
        self.assertIsInstance(
            read_ledger(self.repo, changed_run)["candidate_residue_intent"], dict
        )

    def test_superseded_source_can_be_cleaned_without_reactivation_then_cleanup(self) -> None:
        source_run = "residue-terminal-source"
        source_started, source_path = self.start(source_run)
        source_candidate = Path(source_started["candidate_worktree"])
        files = self.make_residue(source_candidate, "cache/source.pyc")
        source_context = self.record_problem(source_run, source_path)
        source = read_ledger(self.repo, source_run)

        successor = contract_for(self.repo)
        successor["mission"]["revision"] = 2
        successor["mission"]["objective"] = "Carry the clean candidate into a successor."
        successor["mission"]["supersedes"] = {
            "run_id": source_run,
            "revision": source["facets"]["mission"]["revision"],
            "mission_digest": source["digests"]["mission"],
            "candidate_head": source["facets"]["execution"]["candidate_head"],
        }
        successor["execution"]["revision_transition"] = {
            "category": "mission_change",
            "predecessor_pressure_digest": source_context["lineage"][
                "pressure_digest"
            ],
        }
        successor["execution"]["prior_problem_dispositions"] = {
            "source_snapshot_digest": source_context["lineage"][
                "open_problem_snapshot_digest"
            ],
            "items": [
                {
                    "key": "candidate-worktree-ignored-residue",
                    "disposition": "handled_elsewhere",
                }
            ],
        }
        target = core.start(
            self.repo,
            "residue-terminal-target",
            "session-residue-terminal-target",
            successor,
        )
        self.assertEqual(core.status(self.repo, source_run)["phase"], "superseded")
        request = self.request(source_context, files)
        before = self.stable_facts(source_run, source_candidate)

        cleaned = self.resolve(source_run, request)

        self.assertEqual(cleaned["phase"], "superseded")
        after = self.stable_facts(source_run, source_candidate)
        for field in (
            "phase",
            "head",
            "candidate_branch",
            "target_head",
            "facets",
            "digests",
            "evidence",
            "agents",
            "tester_source",
        ):
            self.assertEqual(after[field], before[field], field)
        terminal = read_ledger(self.repo, source_run)
        problem = next(
            item
            for item in terminal["problems"]
            if item["key"] == "candidate-worktree-ignored-residue"
        )
        self.assertEqual(problem["status"], "resolved")
        cleaned_up = core.cleanup(self.repo, source_run)
        self.assertEqual(cleaned_up["phase"], "superseded")
        self.assertFalse(source_candidate.exists())
        self.assertTrue(Path(target["candidate_worktree"]).is_dir())


if __name__ == "__main__":
    unittest.main()
