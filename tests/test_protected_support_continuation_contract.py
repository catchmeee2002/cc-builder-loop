from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator

from harness import (
    ROOT,
    assert_status,
    cleanup_repo,
    commit_all,
    fixture_runtime_env,
    git,
    head,
    init_repo,
    load_ledger,
    plan_markdown,
    problem_report,
    problem_snapshot,
    record_evidence,
    record_problems,
    register_agent,
    repo_session_id,
    revised_plan_with_prior_problems,
    run_cli,
    worktrees_from,
    write_plan,
)


CONTRACT_PATH = ROOT / "schema" / "codex-protected-support-continuation.schema.json"
PLANNER_PATH = ROOT / "skills" / "builder-loop-planner" / "SKILL.md"


def contract() -> dict[str, Any]:
    return json.loads(CONTRACT_PATH.read_text())


def validate_contract_ref(instance: Any, ref: str) -> None:
    schema = contract()
    validator = Draft202012Validator(schema)
    errors = sorted(validator.evolve(schema={"$ref": ref}).iter_errors(instance), key=str)
    if errors:
        raise AssertionError("\n".join(error.message for error in errors))


def issue(key: str) -> dict[str, str]:
    return {
        "key": key,
        "summary": f"Summary for {key}",
        "details": f"Observable details for {key}",
        "owner": "builder",
    }


def repository_state(repo: Path) -> dict[str, Any]:
    run_root = repo / ".builder-loop"
    run_files = {
        str(path.relative_to(repo)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(run_root.rglob("*"))
        if path.is_file()
    } if run_root.exists() else {}
    return {
        "head": head(repo),
        "tree": git(repo, "rev-parse", "HEAD^{tree}"),
        "index": git(repo, "write-tree"),
        "status": git(repo, "status", "--porcelain=v1", "--untracked-files=all"),
        "worktrees": git(repo, "worktree", "list", "--porcelain"),
        "refs": git(repo, "for-each-ref", "--format=%(refname) %(objectname)"),
        "run_files": run_files,
    }


class ProtectedSupportContinuationContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repos: list[Path] = []

    def tearDown(self) -> None:
        for repo in reversed(self.repos):
            cleanup_repo(repo)

    def repo(self) -> Path:
        repo = init_repo()
        self.repos.append(repo)
        self._configure_runner(repo)
        return repo

    def repo_at(self, path: Path) -> Path:
        repo = path.resolve()
        repo.mkdir(parents=True, exist_ok=True)
        fixture_runtime_env(repo)
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "builder-loop@test.local")
        git(repo, "config", "user.name", "builder-loop fixture")
        files = {
            "README.md": "fixture\n",
            "src/calc.py": "def add(a, b):\n    return a + b\n",
            "src/proof_fixture.py": "VALUE = 1\n",
            "tests/test_calc.py": (
                "from src.calc import add\n\n"
                "def test_add():\n"
                "    assert add(1, 2) == 3\n"
            ),
            "verify.sh": "#!/usr/bin/env bash\nset -euo pipefail\npython3 -m unittest discover -s tests -p 'test_*.py'\n",
        }
        for relative, content in files.items():
            target = repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        (repo / "verify.sh").chmod(0o755)
        commit_all(repo, "fixture seed")
        self.repos.append(repo)
        self._configure_runner(repo)
        return repo

    def _configure_runner(self, repo: Path) -> None:
        loop = repo / ".claude" / "loop.yml"
        loop.parent.mkdir(parents=True, exist_ok=True)
        loop.write_text(
            "pass_cmd:\n"
            "  - stage: test\n"
            "    cmd: bash verify.sh\n"
            "    timeout: 120\n"
        )
        old_support = repo / "old-verify.sh"
        old_support.write_text("#!/usr/bin/env bash\nset -euo pipefail\nbash verify.sh\n")
        old_support.chmod(0o755)
        commit_all(repo, "add repository runner and old support")

    def _business_plan(self, repo: Path) -> Path:
        text = plan_markdown(head(repo), runner=None).replace(
            '  support_paths: ["verify.sh"]',
            '  support_paths: ["old-verify.sh", "verify.sh"]',
        )
        return write_plan(repo, text, name="business-plan.md")

    def _abandoned_business(self, repo: Path) -> tuple[Path, dict[str, Any], str]:
        session_id = repo_session_id(repo, "protected-support")
        started = run_cli(
            "start",
            "--repo",
            repo,
            "--plan",
            self._business_plan(repo),
            "--run",
            "business-r1",
            "--session-id",
            session_id,
        )
        assert_status(started, "READY", rc=0)
        run_path = Path(started.data["run_path"])
        recorded = record_problems(
            run_path,
            source="coordinator",
            source_id=f"problem-{repo_session_id(repo, 'source')}",
            manifest=problem_report(issue("protected-support")),
        )
        assert_status(recorded, "READY", rc=0)
        abandoned = run_cli("abandon", "--run", run_path, "--reason", "prepare old support")
        assert_status(abandoned, "COMPLETE", rc=0)
        return run_path, problem_snapshot(run_path, abandoned.data), session_id

    def _preflight(
        self,
        repo: Path,
        *paths: str,
        run_path: Path | None = None,
    ):
        argv: list[str | Path] = ["plan-preflight", "--repo", repo]
        if run_path is not None:
            argv.extend(["--run", run_path])
        for path in paths:
            argv.extend(["--path", path])
        return run_cli(*argv)

    def _marker(self, response: Mapping[str, Any]) -> dict[str, Any]:
        rich = response["verification_preparation"]
        return {
            "schema_version": 1,
            "business_run_id": rich["business_run_id"],
            "business_plan_sha256": rich["business_plan_sha256"],
            "problem_snapshot_sha256": rich["problem_snapshot_sha256"],
            "problem_ids": rich["problem_ids"],
            "support_paths": rich["support_paths"],
        }

    def _preparation_plan(self, repo: Path, marker: Mapping[str, Any]) -> Path:
        base = plan_markdown(
            head(repo),
            runner=None,
            builder_write=["old-verify.sh"],
        ).replace("minimum: strong", "minimum: reviewed-boundaries")
        marker_lines = [
            "<!-- verification-preparation -->",
            "schema_version: 1",
            f'business_run_id: {json.dumps(marker["business_run_id"])}',
            f'business_plan_sha256: {json.dumps(marker["business_plan_sha256"])}',
            f'problem_snapshot_sha256: {json.dumps(marker["problem_snapshot_sha256"])}',
            f'problem_ids: {json.dumps(marker["problem_ids"])}',
            f'support_paths: {json.dumps(marker["support_paths"])}',
            "<!-- /verification-preparation -->",
            "",
        ]
        text = base.replace("<!-- unit-test-spec -->", "\n".join(marker_lines) + "<!-- unit-test-spec -->", 1)
        return write_plan(repo, text, name="preparation-plan.md")

    def _start_preparation(
        self,
        repo: Path,
    ) -> tuple[Path, Path, Path, Path, dict[str, Any], str]:
        business, snapshot, session_id = self._abandoned_business(repo)
        preflight = self._preflight(repo, "old-verify.sh", run_path=business)
        assert_status(preflight, "NEEDS_USER", rc=1)
        self.assertEqual(preflight.data.get("code"), "VERIFICATION_PREPARATION_REQUIRED")
        validate_contract_ref(preflight.data, "#/$defs/preflightPreparationRequired")
        marker = self._marker(preflight.data)
        validate_contract_ref(marker, "#/$defs/verificationPreparationSourceMarker")
        plan = self._preparation_plan(repo, marker)
        started = run_cli(
            "start",
            "--repo",
            repo,
            "--plan",
            plan,
            "--run",
            "preparation-r1",
            "--session-id",
            session_id,
        )
        assert_status(started, "READY", rc=0)
        run_path = Path(started.data["run_path"])
        builder, tester = worktrees_from(started, run_path)
        return business, run_path, builder, tester, snapshot, session_id

    def _finalized_preparation(
        self,
        repo: Path,
    ) -> tuple[Path, Path, dict[str, Any], str, dict[str, Any]]:
        business, preparation, builder, _tester, snapshot, session_id = self._start_preparation(repo)
        finalized = self._complete_preparation(repo, preparation, builder)
        return business, preparation, snapshot, session_id, finalized

    def _complete_preparation(
        self,
        repo: Path,
        preparation: Path,
        builder: Path,
    ) -> dict[str, Any]:
        support = builder / "old-verify.sh"
        support.write_text("#!/usr/bin/env bash\nset -euo pipefail\nbash verify.sh\n# prepared\n")
        support.chmod(0o755)
        commit_all(builder, "prepare old verification support")

        tester_id = register_agent(preparation, "tester", agent_id="preparation-tester")
        assert_status(run_cli("integrate-tests", "--run", preparation), "READY", rc=0)
        node = "tests.test_effectiveness_contract.TestEffectivenessContract.test_frozen_invariant"
        proof = {
            "schema_version": 1,
            "groups": [
                {
                    "behavior_ids": ["add-positive"],
                    "method": "reviewed-boundaries",
                    "argv": ["python3", "-m", "unittest", node],
                    "test_ids": [node],
                    "timeout_seconds": 120,
                    "reason": "The preparation changes a shell support entry while preserving its public forwarding boundary.",
                    "reviewed_boundaries": {
                        "positive_test_ids": [node],
                        "negative_test_ids": [node],
                        "boundary_test_ids": [node],
                        "invariant_test_ids": [node],
                    },
                }
            ],
        }
        assert_status(run_cli("verify", "--run", preparation), "PASS", rc=0)
        proved = run_cli(
            "prove-tests",
            "--repo",
            repo,
            "--run",
            preparation,
            "--spec",
            "-",
            input_text=json.dumps(proof),
        )
        self.assertIn(proved.data.get("status"), {"READY", "NOOP"}, proved.data)
        self.assertEqual(proved.returncode, 0, proved.stderr)
        candidate = head(builder)
        register_agent(preparation, "tester", agent_id=tester_id, result="pass")
        record_evidence(preparation, "e2e_verified", candidate, agent_id=tester_id)
        reviewer_id = register_agent(preparation, "reviewer", agent_id="preparation-reviewer")
        record_evidence(preparation, "reviewed", candidate, agent_id=reviewer_id)
        record_evidence(preparation, "doc_reviewed", candidate, agent_id=reviewer_id)
        finalized = run_cli("finalize", "--run", preparation, "--message", "test(loop): [cr_id_skip] Prepare verification support")
        assert_status(finalized, "COMPLETE", rc=0)
        return finalized.data

    def _continued_plan(
        self,
        repo: Path,
        business: Path,
        snapshot: Mapping[str, Any],
        preparation_run_id: str,
        preparation_final_head: str,
        *,
        support_paths: list[str] | None = None,
    ) -> Path:
        old = load_ledger(business)
        base = plan_markdown(preparation_final_head, runner=None)
        if support_paths is not None:
            base = base.replace(
                '  support_paths: ["verify.sh"]',
                f"  support_paths: {json.dumps(support_paths)}",
            )
        items = [
            {
                "problem_id": problem_id,
                "handling": "handled_elsewhere",
                "reference": f"preparation commit {preparation_final_head}",
            }
            for problem_id in snapshot["problem_ids"]
        ]
        revised = revised_plan_with_prior_problems(
            base,
            supersedes_run_id=str(old["run_id"]),
            supersedes_plan_sha256=str(old["plan"]["sha256"]),
            snapshot_sha256=str(snapshot["snapshot_sha256"]),
            items=items,
        )
        marker = (
            "<!-- continuation-from -->\n"
            "schema_version: 1\n"
            f"preparation_run_id: {json.dumps(preparation_run_id)}\n"
            "<!-- /continuation-from -->\n\n"
        )
        return write_plan(
            repo,
            revised.replace("<!-- prior-problems -->", marker + "<!-- prior-problems -->", 1),
            name=f"continued-{preparation_run_id}.md",
        )

    def test_published_contract_schema_and_scenarios_are_valid(self) -> None:
        schema = contract()
        Draft202012Validator.check_schema(schema)
        for example in schema["examples"]:
            Draft202012Validator(schema).validate(example)
        scenarios = {scenario["id"]: scenario for scenario in schema["x-scenarios"]}
        expected = {
            "preflight-ready",
            "preflight-old-support-same-head",
            "preflight-current-runner-bootstrap",
            "preflight-target-drift",
            "preflight-invalid-absolute-run-selector",
            "preflight-invalid-path",
            "preparation-marker-subset",
            "preparation-plan-ledger-projection",
            "continuation-stale-runner-support",
            "continuation-missing-preparation",
            "continuation-cross-repository",
            "continuation-active-preparation",
            "continuation-cross-session",
            "continuation-replay-after-terminal-consumer",
            "continuation-target-drift",
            "continuation-valid-start",
            "continuation-valid-ledger-problem-decisions",
            "continuation-ready-projection",
        }
        self.assertEqual(set(scenarios), expected)
        source_marker = schema["$defs"]["verificationPreparationSourceMarker"]
        stored_marker = schema["$defs"]["verificationPreparationStoredMarker"]
        with self.subTest(contract="source-marker-response-separation"):
            self.assertEqual(
                set(source_marker["required"]),
                {"schema_version", "business_run_id", "business_plan_sha256", "problem_snapshot_sha256", "problem_ids", "support_paths"},
            )
            self.assertNotIn("repo_root", source_marker["properties"])
            self.assertNotIn("target_branch", source_marker["properties"])
        with self.subTest(contract="stored-marker-normalization"):
            self.assertEqual(
                set(stored_marker["required"]),
                {"business_run_id", "business_plan_sha256", "problem_snapshot_sha256", "problem_ids", "support_paths"},
            )
            self.assertNotIn("schema_version", stored_marker["properties"])
        with self.subTest(contract="ledger-plan-projection-path"):
            projection = schema["$defs"]["preparationPlanProjection"]
            self.assertIn("verification_preparation", projection["properties"])
            self.assertNotIn("ownership", projection["properties"])
            self.assertEqual(
                projection["properties"]["verification_preparation"]["$ref"],
                "#/$defs/verificationPreparationStoredMarker",
            )
        with self.subTest(contract="invalid-run-and-path-exits"):
            self.assertEqual(scenarios["preflight-invalid-absolute-run-selector"]["exit_code"], 2)
            self.assertEqual(scenarios["preflight-invalid-path"]["exit_code"], 1)
        with self.subTest(contract="runner-command-and-path"):
            stale = scenarios["continuation-stale-runner-support"]["fixture"]
            self.assertIn("runner is 'bash verify.sh'", stale)
            self.assertIn("path is 'verify.sh'", stale)
        with self.subTest(contract="replay-terminal-prerequisite"):
            replay = scenarios["continuation-replay-after-terminal-consumer"]["fixture"]
            self.assertIn("explicitly abandon it", replay)

    def test_preflight_ready_for_unprotected_tracked_file(self) -> None:
        repo = self.repo()
        result = self._preflight(repo, "src/calc.py")
        assert_status(result, "READY", rc=0)
        validate_contract_ref(result.data, "#/$defs/preflightReady")
        self.assertEqual(result.data["paths"], ["src/calc.py"])

    def test_preflight_requires_preparation_for_old_support(self) -> None:
        repo = self.repo()
        business, snapshot, _session = self._abandoned_business(repo)
        result = self._preflight(repo, "old-verify.sh", run_path=business)
        assert_status(result, "NEEDS_USER", rc=1)
        self.assertEqual(result.data.get("code"), "VERIFICATION_PREPARATION_REQUIRED")
        validate_contract_ref(result.data, "#/$defs/preflightPreparationRequired")
        rich = result.data["verification_preparation"]
        self.assertEqual(rich["problem_snapshot_sha256"], snapshot["snapshot_sha256"])
        self.assertEqual(rich["problem_ids"], snapshot["problem_ids"])
        self.assertEqual(rich["support_paths"], ["old-verify.sh"])
        self.assertEqual(Path(rich["repo_root"]), repo)
        self.assertEqual(rich["target_branch"], "main")

    def test_preflight_requires_bootstrap_for_current_runner(self) -> None:
        repo = self.repo()
        result = self._preflight(repo, "verify.sh")
        assert_status(result, "NEEDS_USER", rc=1)
        self.assertEqual(result.data.get("code"), "VERIFICATION_BOOTSTRAP_REQUIRED")
        validate_contract_ref(result.data, "#/$defs/preflightBootstrapRequired")
        self.assertEqual(result.data["bootstrap_paths"], ["verify.sh"])
        self.assertIn(".claude/loop.yml", result.data["machine_runner_control_paths"])
        self.assertIn("verify.sh", result.data["machine_runner_control_paths"])

    def test_preflight_rejects_invalid_run_and_path_inputs(self) -> None:
        repo = self.repo()
        missing = (repo / ".builder-loop" / "codex" / "runs" / "missing-run").resolve()
        invalid_run = self._preflight(repo, "src/calc.py", run_path=missing)
        assert_status(invalid_run, "FATAL", rc=2)
        self.assertEqual(invalid_run.data.get("code"), "RUN_NOT_FOUND")
        validate_contract_ref(invalid_run.data, "#/$defs/runNotFound")
        invalid_path = self._preflight(repo, "../outside.py")
        assert_status(invalid_path, "NEEDS_USER", rc=1)
        self.assertEqual(invalid_path.data.get("code"), "PLAN_TEST_PATH_INVALID")
        validate_contract_ref(invalid_path.data, "#/$defs/planTestPathInvalid")

    def test_preflight_is_repository_read_only(self) -> None:
        repo = self.repo()
        business, _snapshot, _session = self._abandoned_business(repo)
        before = repository_state(repo)
        cases = (
            (self._preflight(repo, "src/calc.py"), "READY", 0),
            (self._preflight(repo, "old-verify.sh", run_path=business), "NEEDS_USER", 1),
            (self._preflight(repo, "verify.sh"), "NEEDS_USER", 1),
            (self._preflight(repo, "../outside.py"), "NEEDS_USER", 1),
        )
        for result, status, rc in cases:
            assert_status(result, status, rc=rc)
        self.assertEqual(repository_state(repo), before)
        (repo / "README.md").write_text("target advanced\n")
        commit_all(repo, "advance target for preflight drift")
        drift_before = repository_state(repo)
        drift = self._preflight(repo, "old-verify.sh", run_path=business)
        assert_status(drift, "NEEDS_USER", rc=1)
        self.assertEqual(drift.data.get("code"), "PREFLIGHT_TARGET_DRIFT")
        validate_contract_ref(drift.data, "#/$defs/preflightTargetDrift")
        self.assertEqual(repository_state(repo), drift_before)

    def test_preparation_plan_freezes_existing_marker_schema(self) -> None:
        repo = self.repo()
        _business, preparation, _builder, _tester, _snapshot, _session = self._start_preparation(repo)
        ledger = load_ledger(preparation)
        projection = {
            "verification_preparation": ledger["plan"]["verification_preparation"],
            "plan_revision": ledger["plan"]["plan_revision"],
            "supersedes_run_id": ledger["plan"]["supersedes_run_id"],
            "builder_write": ledger["plan"]["builder_write"],
        }
        validate_contract_ref(projection, "#/$defs/preparationPlanProjection")
        self.assertEqual(projection["plan_revision"], 1)
        self.assertIsNone(projection["supersedes_run_id"])
        self.assertEqual(projection["builder_write"], ["old-verify.sh"])
        self.assertNotIn("repo_root", projection["verification_preparation"])
        self.assertNotIn("target_branch", projection["verification_preparation"])

    def test_preparation_finalize_derives_ready_continuation(self) -> None:
        repo = self.repo()
        _business, preparation, _snapshot, _session, finalized = self._finalized_preparation(repo)
        status = run_cli("status", "--run", preparation)
        assert_status(status, "COMPLETE", rc=0)
        for response in (finalized, status.data):
            validate_contract_ref(response["continuation"], "#/$defs/continuationProjection")
            validate_contract_ref(response["marker"], "#/$defs/readyMarker")
            self.assertEqual(response["continuation"]["preparation_run_id"], "preparation-r1")

    def test_preparation_preserves_abandoned_run_bytes_and_has_no_sidecar(self) -> None:
        repo = self.repo()
        business, preparation, builder, _tester, _snapshot, _session = self._start_preparation(repo)
        before = {str(path.relative_to(business)): path.read_bytes() for path in business.rglob("*") if path.is_file()}
        self._complete_preparation(repo, preparation, builder)
        after = {str(path.relative_to(business)): path.read_bytes() for path in business.rglob("*") if path.is_file()}
        self.assertEqual(after, before)
        names = {path.name for path in business.rglob("*")}
        self.assertFalse(any("mission" in name or "continuation" in name for name in names), names)

    def test_continuation_valid_same_session_start_and_metadata(self) -> None:
        repo = self.repo()
        business, preparation, snapshot, session_id, finalized = self._finalized_preparation(repo)
        final_head = finalized["final_head"]
        plan = self._continued_plan(repo, business, snapshot, "preparation-r1", final_head)
        result = run_cli("start", "--repo", repo, "--plan", plan, "--run", "business-r2", "--session-id", session_id)
        assert_status(result, "READY", rc=0)
        metadata = {key: result.data[key] for key in ("continuation_from_run_id", "supersedes_run_id")}
        validate_contract_ref(metadata, "#/$defs/startMetadata")
        self.assertEqual(metadata, {"continuation_from_run_id": "preparation-r1", "supersedes_run_id": "business-r1"})
        ledger = load_ledger(Path(result.data["run_path"]))
        validate_contract_ref(ledger["plan"]["prior_problem_items"], "#/$defs/priorProblemItems")
        self.assertTrue(all(final_head in item["reference"] for item in ledger["plan"]["prior_problem_items"]))
        self.assertEqual(preparation.name, "preparation-r1")

    def test_continuation_rejects_stale_runner_support(self) -> None:
        repo = self.repo()
        business, _preparation, snapshot, _session_id, finalized = self._finalized_preparation(repo)
        plan = self._continued_plan(repo, business, snapshot, "preparation-r1", finalized["final_head"], support_paths=["old-verify.sh"])
        result = run_cli("plan-validate", "--repo", repo, "--plan", plan)
        assert_status(result, "NEEDS_USER", rc=1)
        self.assertEqual(result.data.get("code"), "RUNNER_SUPPORT_PATH_MISSING")
        validate_contract_ref(result.data, "#/$defs/runnerSupportPathMissing")
        self.assertEqual(result.data["runner"], "bash verify.sh")
        self.assertEqual(result.data["path"], "verify.sh")

    def test_continuation_rejects_missing_preparation_marker(self) -> None:
        repo = self.repo()
        business, snapshot, _session_id = self._abandoned_business(repo)
        plan = self._continued_plan(repo, business, snapshot, "missing-preparation", head(repo))
        result = run_cli("plan-validate", "--repo", repo, "--plan", plan)
        assert_status(result, "NEEDS_USER", rc=1)
        self.assertEqual(result.data.get("code"), "PLAN_LINKED_RUN_INVALID")
        validate_contract_ref(result.data, "#/$defs/linkedRunInvalid")

    def test_continuation_rejects_cross_session_start(self) -> None:
        repo = self.repo()
        business, _preparation, snapshot, _session_id, finalized = self._finalized_preparation(repo)
        plan = self._continued_plan(repo, business, snapshot, "preparation-r1", finalized["final_head"])
        result = run_cli("start", "--repo", repo, "--plan", plan, "--run", "cross-session-r2", "--session-id", repo_session_id(repo, "different-session"))
        assert_status(result, "NEEDS_USER", rc=1)
        self.assertEqual(result.data.get("code"), "CONTINUATION_SESSION_MISMATCH")
        validate_contract_ref(result.data, "#/$defs/continuationSessionMismatch")

    def test_continuation_rejects_replay(self) -> None:
        repo = self.repo()
        business, _preparation, snapshot, session_id, finalized = self._finalized_preparation(repo)
        plan = self._continued_plan(repo, business, snapshot, "preparation-r1", finalized["final_head"])
        first = run_cli("start", "--repo", repo, "--plan", plan, "--run", "consumer-one", "--session-id", session_id)
        assert_status(first, "READY", rc=0)
        abandoned = run_cli("abandon", "--run", Path(first.data["run_path"]), "--reason", "terminalize first consumer before replay")
        assert_status(abandoned, "COMPLETE", rc=0)
        replay = run_cli("start", "--repo", repo, "--plan", plan, "--run", "consumer-two", "--session-id", session_id)
        assert_status(replay, "NEEDS_USER", rc=1)
        self.assertEqual(replay.data.get("code"), "BUSINESS_CONTINUATION_REPLAYED")
        validate_contract_ref(replay.data, "#/$defs/businessContinuationReplayed")

    def test_continuation_rejects_cross_repository(self) -> None:
        source = self.repo()
        business, snapshot, _session_id = self._abandoned_business(source)
        with tempfile.TemporaryDirectory(prefix="continuation-cross-repo-") as temporary:
            other = self.repo_at(Path(temporary) / "other")
            plan = self._continued_plan(other, business, snapshot, "preparation-r1", head(other))
            result = run_cli("plan-validate", "--repo", other, "--plan", plan)
            assert_status(result, "NEEDS_USER", rc=1)
            self.assertEqual(result.data.get("code"), "PLAN_SUPERSESSION_INVALID")
            validate_contract_ref(result.data, "#/$defs/supersessionInvalid")

    def test_continuation_rejects_non_finalized_preparation(self) -> None:
        repo = self.repo()
        business, preparation, _builder, _tester, snapshot, _session_id = self._start_preparation(repo)
        plan = self._continued_plan(repo, business, snapshot, preparation.name, head(repo))
        result = run_cli("plan-validate", "--repo", repo, "--plan", plan)
        assert_status(result, "NEEDS_USER", rc=1)
        self.assertEqual(result.data.get("code"), "BUSINESS_CONTINUATION_INVALID")
        validate_contract_ref(result.data, "#/$defs/businessContinuationInvalid")
        self.assertIsNone(result.data["preparation_final_head"])

    def test_continuation_rejects_target_drift(self) -> None:
        repo = self.repo()
        business, _preparation, snapshot, _session_id, finalized = self._finalized_preparation(repo)
        plan = self._continued_plan(repo, business, snapshot, "preparation-r1", finalized["final_head"])
        (repo / "README.md").write_text("target drift\n")
        commit_all(repo, "advance target after preparation")
        result = run_cli("plan-validate", "--repo", repo, "--plan", plan)
        assert_status(result, "NEEDS_USER", rc=1)
        self.assertEqual(result.data.get("code"), "TARGET_SPEC_MISMATCH")
        validate_contract_ref(result.data, "#/$defs/targetSpecMismatch")
        self.assertEqual(result.data["spec_head"], finalized["final_head"])
        self.assertEqual(result.data["target_head"], head(repo))

    def test_experimental_planner_uses_v4_continuation_without_transcript_state(self) -> None:
        text = PLANNER_PATH.read_text()
        self.assertIn("execution.continuation", text)
        self.assertIn("Core 已验证的 finalized run 事实", text)
        self.assertIn("不得解析", text)
        self.assertIn("transcript", text)
        self.assertIn("不得让 preparation supersede 业务 run", text)
        self.assertIn("不得创建\n或启动 run", text)


if __name__ == "__main__":
    unittest.main()
