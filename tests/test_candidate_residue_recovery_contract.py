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

    def test_repeated_identical_residue_uses_separate_transaction_tombstones(self):
        run = "repeated-residue"
        started, root = self.start(run)
        candidate = Path(started["candidate_worktree"])
        for key in ("first-cleanup", "second-cleanup"):
            files = self.make_residue(candidate, "cache/one.pyc")
            request = self.request(self.record_problem(run, root, key=key), files, key=key)
            self.resolve(run, request)
        saved = list((root / "candidate-residue-quarantine").glob("*.delete"))
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[0].read_bytes(), saved[1].read_bytes())
        self.assertIsNone(read_ledger(self.repo, run).get("candidate_residue_intent"))

    def test_legacy_tombstone_collision_resumes_existing_deleting_intent(self):
        run = "legacy-collision"
        started, root = self.start(run)
        candidate = Path(started["candidate_worktree"])
        files = self.make_residue(candidate, "cache/one.pyc")
        request = self.request(self.record_problem(run, root), files)
        content = (candidate / files[0]["path"]).read_bytes()
        directory = root / "candidate-residue-quarantine"
        directory.mkdir()
        name = core._candidate_residue_quarantine_name(files[0]["path"], files[0]["sha256"])
        legacy = directory / f".{name}.delete"
        legacy.write_bytes(content)
        with patch.object(core, "_remove_quarantined_candidate_residue", side_effect=RuntimeError("legacy collision")):
            with self.assertRaises(RuntimeError):
                self.resolve(run, request)
        pending = read_ledger(self.repo, run)
        self.assertEqual(pending["candidate_residue_intent"]["state"], "deleting")
        self.assertFalse((candidate / files[0]["path"]).exists())
        self.assertEqual((directory / name).read_bytes(), content)
        self.resolve(run, request)
        after = read_ledger(self.repo, run)
        self.assertEqual(legacy.read_bytes(), content)
        self.assertEqual(after["facets"], pending["facets"])
        self.assertEqual(sum(e["kind"] == "candidate_residue_intent_written" for e in after["events"]), 1)

    def test_interrupted_exchange_resumes_without_losing_preserved_content(self):
        self._exercise_interrupted_exchange(tamper=False)

    def test_interrupted_exchange_rejects_modified_preserved_content(self):
        self._exercise_interrupted_exchange(tamper=True)

    def _exercise_interrupted_exchange(self, *, tamper):
        run = "exchange-tamper" if tamper else "exchange-resume"
        started, root = self.start(run)
        candidate = Path(started["candidate_worktree"])
        files = self.make_residue(candidate, "cache/one.pyc")
        request = self.request(self.record_problem(run, root), files)
        content = (candidate / files[0]["path"]).read_bytes()
        with patch.object(core, "_candidate_residue_unlink_fd_entry", side_effect=RuntimeError("process interrupted")):
            with self.assertRaises(RuntimeError):
                self.resolve(run, request)
        directory = root / "candidate-residue-quarantine"
        saved = next(directory.glob("*.delete"))
        self.assertEqual(saved.read_bytes(), content)
        if tamper:
            saved.write_bytes(b"external replacement")
            with self.assertRaises(core.AssuranceError) as raised:
                self.resolve(run, request)
            self.assertEqual(raised.exception.code, "CANDIDATE_RESIDUE_REPLAY_DRIFT")
            self.assertEqual(saved.read_bytes(), b"external replacement")
            self.assertEqual(read_ledger(self.repo, run)["candidate_residue_intent"]["state"], "deleting")
        else:
            self.resolve(run, request)
            self.assertEqual(saved.read_bytes(), content)
            self.assertIsNone(read_ledger(self.repo, run).get("candidate_residue_intent"))

    def test_residue_runtime_transition_requires_exact_owner(self):
        run = "residue-owner"
        started, root = self.start(run)
        candidate = Path(started["candidate_worktree"])
        files = self.make_residue(candidate, "cache/one.pyc")
        request = self.request(self.record_problem(run, root), files)
        before = (root / "ledger.json").read_bytes()
        with self.assertRaises(core.AssuranceError) as raised:
            core.resolve_candidate_residue(self.repo, run, request,
                allow_runtime_transition=True, owner_session_id="foreign", driver_runtime_kind="native")
        self.assertEqual(raised.exception.code, "CANDIDATE_RESIDUE_OWNER_MISMATCH")
        self.assertEqual((root / "ledger.json").read_bytes(), before)

    def test_pending_residue_runtime_migration_preserves_original_intent(self):
        run = "residue-migration"
        started = core.start(self.repo, run, "owner", contract_for(self.repo), driver_runtime={
            "kind": "native", "protocol_version": 1, "transport": "codex_app_server",
            "runtime_version": "fixture", "protocol_schema_digest": "a" * 64})
        candidate = Path(started["candidate_worktree"])
        root = candidate.parent
        files = self.make_residue(candidate, "cache/one.pyc")
        request = self.request(self.record_problem(run, root), files)
        with patch.object(core, "_remove_quarantined_candidate_residue", side_effect=RuntimeError("old runtime failure")):
            with self.assertRaises(RuntimeError): self.resolve(run, request)
        before = read_ledger(self.repo, run)
        identity = {**before["runtime_identity"], "adapter_commit": "b" * 40,
                    "adapter_dirty": False, "capture_status": "captured"}
        args = dict(allow_runtime_transition=True, owner_session_id="owner", driver_runtime_kind="native")
        with patch("runtime.codex_builder_loop.core.capture_runtime_identity", return_value={**identity, "adapter_dirty": True}):
            with self.assertRaises(core.AssuranceError):
                core.resolve_candidate_residue(self.repo, run, request, **args)
        self.assertEqual(read_ledger(self.repo, run), before)
        from contextlib import redirect_stdout
        from io import StringIO
        from runtime.codex_builder_loop.assurance_v4.cli import main
        request_path = root / "original-request.json"
        request_path.write_text(json.dumps(request))
        with patch("runtime.codex_builder_loop.core.capture_runtime_identity", return_value=identity), redirect_stdout(StringIO()) as output:
            result = main(["--experimental-v4", "resolve-candidate-residue", "--repo", str(self.repo), "--run", run,
                           "--request", str(request_path), "--allow-runtime-transition",
                           "--owner-session-id", "owner", "--driver-runtime-kind", "native"])
        self.assertEqual(result, 0, output.getvalue())
        self.assertEqual(json.loads(request_path.read_text()), request)
        after = read_ledger(self.repo, run)
        self.assertEqual(after["runtime_identity"], identity)
        self.assertEqual(after["facets"], before["facets"])
        self.assertEqual(after["evidence"], before["evidence"])
        for kind in ("candidate_residue_intent_written", "runtime_identity_transitioned", "candidate_residue_resolved"):
            self.assertEqual(sum(e["kind"] == kind for e in after["events"]), 1)

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
