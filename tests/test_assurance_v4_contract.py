from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from harness import CLI, cleanup_repo, commit_all, git, head, init_repo, run_process


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def contract_for(
    repo: Path,
    *,
    machine_argv: list[str] | None = None,
) -> dict[str, Any]:
    machine_argv = machine_argv or ["bash", "verify.sh"]
    return {
        "schema_version": 4,
        "mission": {
            "revision": 1,
            "objective": "Deliver an independently assured calculator change.",
            "behaviors": [
                {"id": "add-values", "description": "Addition returns the sum."}
            ],
            "interfaces": [
                {"id": "calc-api", "description": "src.calc.add(a, b) -> int"}
            ],
            "acceptance_cases": [
                {"id": "add-positive", "description": "add(1, 2) returns 3."}
            ],
            "trust_boundaries": [
                {
                    "id": "independent-review",
                    "description": "Reviewer evidence is independently recorded.",
                }
            ],
        },
        "authority": {
            "target_branch": "main",
            "builder_write": ["src/**"],
            "tester_write": ["tests/**"],
            "dirty_intake": [],
        },
        "assurance": {
            "required": ["machine", "tester", "blackbox", "reviewer"],
            "machine_commands": [
                {
                    "id": "fixture-tests",
                    "argv": machine_argv,
                    "timeout_seconds": 30,
                }
            ],
        },
        "execution": {
            "version": 1,
            "candidate_head": None,
            "builder_files": [],
            "tester_files": [],
            "tester_source": None,
            "dirty_snapshot": [],
            "commands": [
                {
                    "id": "fixture-blackbox",
                    "argv": ["bash", "verify.sh"],
                    "timeout_seconds": 30,
                }
            ],
            "agents": {
                "tester": {
                    "agent_id": "assurance-v4-tester",
                    "thread_id": "assurance-v4-tester-thread",
                },
                "reviewer": {
                    "agent_id": "assurance-v4-reviewer",
                    "thread_id": "assurance-v4-reviewer-thread",
                },
            },
        },
    }


class AssuranceV4ContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo()
        self.tempdir = tempfile.TemporaryDirectory(prefix="assurance-v4-contract-")
        self.artifacts = Path(self.tempdir.name)

    def tearDown(self) -> None:
        cleanup_repo(self.repo)
        self.tempdir.cleanup()

    def write_json(self, name: str, value: Any) -> Path:
        path = self.artifacts / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def invoke(
        self,
        command: str,
        *args: str | Path,
        experimental: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> tuple[int, dict[str, Any], str, str]:
        argv: list[str | Path] = [sys.executable, CLI, "assurance"]
        if experimental:
            argv.append("--experimental-v4")
        argv.extend([command, *args])
        completed = run_process(argv, env=env)
        lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        self.assertTrue(
            lines,
            f"assurance CLI produced no JSON\nrc={completed.returncode}\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}",
        )
        data = json.loads(lines[-1])
        self.assertIsInstance(data, dict)
        return completed.returncode, data, completed.stdout, completed.stderr

    def start(
        self,
        run_id: str = "assurance-v4-fixture",
        *,
        contract: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], Path]:
        contract_path = self.write_json(
            f"{run_id}-contract.json", contract or contract_for(self.repo)
        )
        rc, data, stdout, stderr = self.invoke(
            "start",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--session-id",
            "assurance-v4-test-session",
            "--contract",
            contract_path,
        )
        self.assertEqual(rc, 0, (data, stdout, stderr))
        self.assertEqual(data.get("status"), "ACTIVE", data)
        candidate_worktree = data.get("candidate_worktree")
        self.assertIsInstance(candidate_worktree, str, data)
        run_path = Path(candidate_worktree).parent
        self.assertTrue(run_path.is_dir(), data)
        return data, run_path

    def record_role_evidence(
        self,
        run_id: str,
        run_path: Path,
        kind: str,
    ) -> dict[str, Any]:
        if kind == "tester":
            self.prepare_tester_source(run_id, run_path)
        ledger = self.load_ledger(run_path)
        role = "tester" if kind in {"tester", "blackbox"} else "reviewer"
        agent = ledger["facets"]["execution"]["agents"][role]
        candidate = Path(ledger["candidate_worktree"])
        candidate_head = ledger["facets"]["execution"]["candidate_head"]
        if kind == "tester":
            tester_source = ledger["facets"]["execution"]["tester_source"]
            details = {
                "result": "tests_ready",
                "source_head": tester_source["head"],
                "files": tester_source["files"],
            }
        elif kind == "blackbox":
            command = ledger["facets"]["execution"]["commands"][0]
            details = {
                "result": "pass",
                "worktree": str(candidate),
                "before_head": candidate_head,
                "after_head": candidate_head,
                "executions": [
                    {
                        "id": command["id"],
                        "argv": command["argv"],
                        "returncode": 0,
                        "timed_out": False,
                    }
                ],
            }
        else:
            details = {"result": "pass", "reviewed_head": candidate_head}
        report = {
            "schema_version": 1,
            "kind": kind,
            "status": "pass",
            "candidate_head": candidate_head,
            "producer": {"role": role, **agent},
            "details": details,
        }
        report_path = self.write_json(f"{run_id}-{kind}-report.json", report)
        rc, data, _stdout, _stderr = self.invoke(
            "record-evidence",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--kind",
            kind,
            "--report",
            report_path,
        )
        self.assertEqual(rc, 0, data)
        return data

    def prepare_tester_source(self, run_id: str, run_path: Path) -> None:
        ledger = self.load_ledger(run_path)
        execution = ledger["facets"]["execution"]
        if execution["tester_files"]:
            return
        rc, prepared, _stdout, _stderr = self.invoke(
            "prepare-tester",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--agent-id",
            execution["agents"]["tester"]["agent_id"],
            "--thread-id",
            execution["agents"]["tester"]["thread_id"],
        )
        self.assertEqual(rc, 0, prepared)
        ledger = self.load_ledger(run_path)
        tester_source = ledger["facets"]["execution"]["tester_source"]
        tester_worktree = Path(tester_source["worktree"])
        path = "tests/test_assurance_fixture.py"
        (tester_worktree / path).write_text(
            "from src.calc import add\n\n"
            "def test_assurance_fixture():\n    assert add(2, 3) == 5\n",
            encoding="utf-8",
        )
        commit_all(tester_worktree, "add independent tester fixture")
        rc, result, _stdout, _stderr = self.invoke(
            "integrate-tester", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, result)
        integrated = self.load_ledger(run_path)["facets"]["execution"]
        self.assertEqual(integrated["tester_files"], [path])

    def prepare_required_gates(self, run_id: str, run_path: Path) -> None:
        self.record_role_evidence(run_id, run_path, "tester")
        rc, machine, _stdout, _stderr = self.invoke(
            "verify-machine", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, machine)
        self.record_role_evidence(run_id, run_path, "blackbox")
        self.record_role_evidence(run_id, run_path, "reviewer")

    def commit_candidate_change(
        self,
        run_id: str,
        run_path: Path,
        candidate: Path,
        *,
        path: str = "src/calc.py",
        content: str = "def add(a, b):\n    return a + b\n\nVALUE = 2\n",
    ) -> str:
        changed_path = candidate / path
        changed_path.parent.mkdir(parents=True, exist_ok=True)
        changed_path.write_text(content, encoding="utf-8")
        candidate_head = commit_all(candidate, "fixture candidate update")
        ledger = self.load_ledger(run_path)
        execution = deepcopy(ledger["facets"]["execution"])
        execution["version"] += 1
        execution["candidate_head"] = candidate_head
        execution["builder_files"] = sorted(
            set([*execution["builder_files"], path])
        )
        rc, updated = self.update_facet(run_id, "execution", execution)
        self.assertEqual(rc, 0, updated)
        return candidate_head

    def load_ledger(self, run_path: Path) -> dict[str, Any]:
        ledger_path = run_path / "ledger.json"
        self.assertTrue(ledger_path.is_file(), ledger_path)
        return json.loads(ledger_path.read_text(encoding="utf-8"))

    def update_facet(
        self,
        run_id: str,
        facet: str,
        value: dict[str, Any],
        *authorization: str,
    ) -> tuple[int, dict[str, Any]]:
        value_path = self.write_json(f"{facet}.json", value)
        rc, data, _stdout, _stderr = self.invoke(
            "update-facet",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--facet",
            facet,
            "--value",
            value_path,
            *authorization,
        )
        return rc, data

    def test_namespace_requires_experimental_switch_without_side_effects(self) -> None:
        contract_path = self.write_json("contract.json", contract_for(self.repo))
        before_status = git(self.repo, "status", "--porcelain=v1", "--untracked-files=all")
        before_worktrees = git(self.repo, "worktree", "list", "--porcelain")

        rc, data, _stdout, _stderr = self.invoke(
            "start",
            "--repo",
            self.repo,
            "--run",
            "missing-experimental-switch",
            "--session-id",
            "assurance-v4-test-session",
            "--contract",
            contract_path,
            experimental=False,
        )

        self.assertEqual(rc, 2, data)
        self.assertEqual(data.get("status"), "FATAL", data)
        self.assertEqual(data.get("code"), "ASSURANCE_V4_EXPERIMENTAL_REQUIRED", data)
        self.assertEqual(
            git(self.repo, "status", "--porcelain=v1", "--untracked-files=all"),
            before_status,
        )
        self.assertEqual(git(self.repo, "worktree", "list", "--porcelain"), before_worktrees)
        self.assertFalse((self.repo / ".builder-loop").exists())

    def test_start_creates_isolated_candidate_and_preserves_target_dirty_state(self) -> None:
        original_head = head(self.repo)
        (self.repo / "README.md").write_text("fixture\ntarget dirty\n", encoding="utf-8")
        (self.repo / "local.tmp").write_text("untracked target state\n", encoding="utf-8")
        dirty_before = git(self.repo, "status", "--porcelain=v1", "--untracked-files=all")

        data, run_path = self.start("isolated-candidate")
        ledger = self.load_ledger(run_path)
        candidate = Path(str(ledger.get("candidate_worktree")))

        self.assertTrue(candidate.is_dir(), ledger)
        self.assertNotEqual(candidate.resolve(), self.repo.resolve())
        self.assertEqual(head(candidate), original_head)
        self.assertEqual(ledger.get("target_start_head"), original_head)
        self.assertEqual(ledger.get("repo_root"), str(self.repo.resolve()))
        self.assertEqual(data.get("candidate_worktree"), str(candidate))
        self.assertEqual(
            git(self.repo, "status", "--porcelain=v1", "--untracked-files=all"),
            dirty_before,
        )
        self.assertEqual((self.repo / "README.md").read_text(), "fixture\ntarget dirty\n")
        self.assertEqual((self.repo / "local.tmp").read_text(), "untracked target state\n")

    def test_ledger_records_four_independent_canonical_facet_digests(self) -> None:
        _data, run_path = self.start("independent-digests")
        ledger = self.load_ledger(run_path)

        self.assertEqual(set(ledger["digests"]), {"mission", "authority", "assurance", "execution"})
        for facet in ("mission", "authority", "assurance", "execution"):
            self.assertEqual(ledger["digests"][facet], canonical_digest(ledger["facets"][facet]))
        self.assertEqual(len(set(ledger["digests"].values())), 4)
        self.assertEqual(ledger["facets"]["execution"]["dirty_snapshot"], [])

    def test_mission_change_requires_semantic_revision_and_exact_increment(self) -> None:
        run_id = "mission-revision"
        _data, run_path = self.start(run_id)
        original = self.load_ledger(run_path)
        changed = deepcopy(original["facets"]["mission"])
        changed["objective"] = "Deliver a changed calculator mission."

        rc, rejected = self.update_facet(run_id, "mission", changed)
        self.assertEqual(rc, 1, rejected)
        self.assertEqual(rejected.get("status"), "NEEDS_USER", rejected)
        self.assertEqual(self.load_ledger(run_path)["facets"]["mission"], original["facets"]["mission"])

        rc, rejected = self.update_facet(run_id, "mission", changed, "--semantic-revision")
        self.assertEqual(rc, 1, rejected)
        self.assertEqual(rejected.get("status"), "NEEDS_USER", rejected)

        changed["revision"] = original["facets"]["mission"]["revision"] + 1
        rc, accepted = self.update_facet(run_id, "mission", changed, "--semantic-revision")
        self.assertEqual(rc, 0, accepted)
        self.assertEqual(accepted.get("status"), "ACTIVE", accepted)
        self.assertEqual(self.load_ledger(run_path)["facets"]["mission"], changed)

    def test_authority_expansion_requires_explicit_authorization(self) -> None:
        run_id = "authority-expansion"
        _data, run_path = self.start(run_id)
        original = self.load_ledger(run_path)
        expanded = deepcopy(original["facets"]["authority"])
        expanded["builder_write"].append("docs/**")

        rc, rejected = self.update_facet(run_id, "authority", expanded)
        self.assertEqual(rc, 1, rejected)
        self.assertEqual(rejected.get("status"), "NEEDS_USER", rejected)
        self.assertEqual(self.load_ledger(run_path)["facets"]["authority"], original["facets"]["authority"])

        rc, accepted = self.update_facet(
            run_id, "authority", expanded, "--authorize-expansion"
        )
        self.assertEqual(rc, 0, accepted)
        self.assertEqual(accepted.get("status"), "ACTIVE", accepted)
        self.assertEqual(self.load_ledger(run_path)["facets"]["authority"], expanded)

    def test_authority_target_branch_is_immutable(self) -> None:
        run_id = "authority-target-immutable"
        _data, run_path = self.start(run_id)
        git(self.repo, "branch", "other-target", head(self.repo))
        original = self.load_ledger(run_path)
        changed = deepcopy(original["facets"]["authority"])
        changed["target_branch"] = "other-target"

        rc, rejected = self.update_facet(
            run_id, "authority", changed, "--authorize-expansion"
        )
        self.assertNotEqual(rc, 0, rejected)
        self.assertEqual(
            self.load_ledger(run_path)["facets"]["authority"],
            original["facets"]["authority"],
        )

    def test_same_machine_command_id_cannot_weaken_argv_without_authorization(self) -> None:
        run_id = "machine-command-downgrade"
        _data, run_path = self.start(run_id)
        original = self.load_ledger(run_path)
        weakened = deepcopy(original["facets"]["assurance"])
        weakened["machine_commands"][0]["argv"] = ["true"]

        rc, rejected = self.update_facet(run_id, "assurance", weakened)
        self.assertNotEqual(rc, 0, rejected)
        self.assertEqual(
            self.load_ledger(run_path)["facets"]["assurance"],
            original["facets"]["assurance"],
        )

        rc, accepted = self.update_facet(
            run_id, "assurance", weakened, "--authorize-downgrade"
        )
        self.assertEqual(rc, 0, accepted)
        self.assertEqual(
            self.load_ledger(run_path)["facets"]["assurance"], weakened
        )

    def test_builder_files_cannot_classify_tester_owned_paths(self) -> None:
        run_id = "builder-tester-path"
        data, run_path = self.start(run_id)
        candidate = Path(data["candidate_worktree"])
        tester_path = "tests/test_calc.py"
        (candidate / tester_path).write_text(
            "from src.calc import add\n\n"
            "def test_add():\n    assert add(-1, 1) == 0\n",
            encoding="utf-8",
        )
        candidate_head = commit_all(candidate, "change tester-owned path")
        original = self.load_ledger(run_path)
        execution = deepcopy(original["facets"]["execution"])
        execution["version"] += 1
        execution["candidate_head"] = candidate_head
        execution["builder_files"] = [tester_path]

        rc, rejected = self.update_facet(run_id, "execution", execution)
        self.assertNotEqual(rc, 0, rejected)
        self.assertEqual(
            self.load_ledger(run_path)["facets"]["execution"],
            original["facets"]["execution"],
        )

    def test_dirty_intake_is_rehashed_and_snapshotted_by_blob_after_copy(self) -> None:
        path = "src/intake.py"
        original_content = "INTAKE_VALUE = 1\n"
        target_path = self.repo / path
        target_path.write_text(original_content, encoding="utf-8")
        digest = hashlib.sha256(original_content.encode()).hexdigest()
        contract = contract_for(self.repo)
        contract["authority"]["dirty_intake"] = [{"path": path, "sha256": digest}]

        data, run_path = self.start("dirty-intake-snapshot", contract=contract)
        candidate = Path(data["candidate_worktree"])
        ledger = self.load_ledger(run_path)
        snapshot = ledger["facets"]["execution"]["dirty_snapshot"]
        self.assertEqual(len(snapshot), 1, snapshot)
        self.assertEqual(snapshot[0]["path"], path)
        self.assertEqual(snapshot[0]["sha256"], digest)
        self.assertEqual((candidate / path).read_text(), original_content)
        self.assertEqual(snapshot[0]["blob"], git(candidate, "hash-object", path))

        target_path.write_text("INTAKE_VALUE = 2\n", encoding="utf-8")
        unchanged = self.load_ledger(run_path)["facets"]["execution"][
            "dirty_snapshot"
        ]
        self.assertEqual(unchanged, snapshot)
        self.assertEqual((candidate / path).read_text(), original_content)

    def test_assurance_downgrade_requires_authorization_but_enhancement_only_adds_missing_gate(self) -> None:
        run_id = "assurance-monotonicity"
        _data, run_path = self.start(run_id)
        recorded = self.record_role_evidence(run_id, run_path, "tester")
        self.assertEqual(recorded["readiness"]["states"]["tester"], "pass")
        original = self.load_ledger(run_path)
        downgraded = deepcopy(original["facets"]["assurance"])
        downgraded["required"].remove("reviewer")

        rc, rejected = self.update_facet(run_id, "assurance", downgraded)
        self.assertEqual(rc, 1, rejected)
        self.assertEqual(rejected.get("status"), "NEEDS_USER", rejected)
        self.assertEqual(self.load_ledger(run_path)["facets"]["assurance"], original["facets"]["assurance"])

        rc, accepted = self.update_facet(
            run_id, "assurance", downgraded, "--authorize-downgrade"
        )
        self.assertEqual(rc, 0, accepted)
        self.assertEqual(accepted.get("status"), "ACTIVE", accepted)

        enhanced = deepcopy(downgraded)
        enhanced["required"].append("doc_review")
        evidence_before = deepcopy(self.load_ledger(run_path)["evidence"])
        rc, accepted = self.update_facet(run_id, "assurance", enhanced)
        self.assertEqual(rc, 0, accepted)
        self.assertEqual(accepted.get("status"), "ACTIVE", accepted)
        ledger = self.load_ledger(run_path)
        self.assertEqual(ledger["evidence"], evidence_before)
        self.assertEqual(
            set(accepted.get("readiness", {}).get("missing", [])),
            {"machine", "blackbox", "doc_review"},
            accepted,
        )
        self.assertEqual(accepted["readiness"]["states"]["tester"], "pass")

    def test_execution_update_does_not_revise_mission_and_stales_only_dependent_evidence(self) -> None:
        run_id = "execution-update"
        data, run_path = self.start(run_id)
        original = self.load_ledger(run_path)
        assurance = deepcopy(original["facets"]["assurance"])
        assurance["required"].append("doc_review")
        rc, enhanced = self.update_facet(run_id, "assurance", assurance)
        self.assertEqual(rc, 0, enhanced)
        self.assertEqual(
            set(enhanced["readiness"]["missing"]),
            {"machine", "tester", "blackbox", "reviewer", "doc_review"},
        )
        recorded = self.record_role_evidence(run_id, run_path, "tester")
        self.assertEqual(recorded.get("status"), "ACTIVE", recorded)
        rc, machine, _stdout, _stderr = self.invoke(
            "verify-machine",
            "--repo",
            self.repo,
            "--run",
            run_id,
        )
        self.assertEqual(rc, 0, machine)
        self.assertEqual(machine["readiness"]["states"]["machine"], "pass")

        for kind in ("blackbox", "reviewer", "doc_review"):
            evidence = self.record_role_evidence(run_id, run_path, kind)
            self.assertEqual(evidence["readiness"]["states"][kind], "pass")

        original = self.load_ledger(run_path)
        self.assertEqual(
            set(original["evidence"]),
            {"machine", "tester", "blackbox", "reviewer", "doc_review"},
        )
        candidate = Path(data["candidate_worktree"])
        (candidate / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\nVALUE = 2\n",
            encoding="utf-8",
        )
        candidate_head = commit_all(candidate, "fixture candidate update")
        execution = deepcopy(original["facets"]["execution"])
        execution["version"] += 1
        execution["candidate_head"] = candidate_head
        execution["builder_files"] = ["src/calc.py"]

        rc, accepted = self.update_facet(run_id, "execution", execution)
        self.assertEqual(rc, 0, accepted)
        self.assertEqual(accepted.get("status"), "ACTIVE", accepted)
        updated = self.load_ledger(run_path)
        self.assertEqual(updated["facets"]["mission"], original["facets"]["mission"])
        self.assertEqual(updated["digests"]["mission"], original["digests"]["mission"])
        self.assertEqual(updated["facets"]["execution"], execution)
        self.assertEqual(updated["evidence"]["tester"]["status"], "pass")
        self.assertEqual(accepted["readiness"]["states"]["tester"], "pass")
        for kind in ("machine", "blackbox", "reviewer", "doc_review"):
            self.assertEqual(
                accepted["readiness"]["states"][kind],
                "stale",
                accepted,
            )

    def test_reviewer_evidence_is_rejected_until_formal_prerequisites_are_complete(self) -> None:
        run_id = "reviewer-ordering"
        _data, run_path = self.start(run_id)
        ledger = self.load_ledger(run_path)
        report = {
            "schema_version": 1,
            "kind": "reviewer",
            "status": "pass",
            "candidate_head": ledger["facets"]["execution"]["candidate_head"],
            "producer": {
                "role": "reviewer",
                "agent_id": "assurance-v4-reviewer",
                "thread_id": "assurance-v4-reviewer-thread",
            },
            "details": {
                "result": "pass",
                "reviewed_head": ledger["facets"]["execution"]["candidate_head"],
            },
        }
        report_path = self.write_json("reviewer-report.json", report)

        rc, rejected, _stdout, _stderr = self.invoke(
            "record-evidence",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--kind",
            "reviewer",
            "--report",
            report_path,
        )
        self.assertEqual(rc, 1, rejected)
        self.assertEqual(rejected.get("status"), "NEEDS_USER", rejected)
        self.assertNotIn("reviewer", self.load_ledger(run_path)["evidence"])

    def test_evidence_kind_role_and_manifest_identity_are_enforced(self) -> None:
        run_id = "evidence-identity"
        _data, run_path = self.start(run_id)
        ledger = self.load_ledger(run_path)
        candidate_head = ledger["facets"]["execution"]["candidate_head"]

        mismatched_kind = {
            "schema_version": 1,
            "kind": "blackbox",
            "status": "pass",
            "candidate_head": candidate_head,
            "producer": {
                "role": "reviewer",
                "agent_id": "assurance-v4-reviewer",
                "thread_id": "assurance-v4-reviewer-thread",
            },
            "details": {
                "result": "pass",
                "worktree": ledger["candidate_worktree"],
                "before_head": candidate_head,
                "after_head": candidate_head,
                "executions": [
                    {
                        "id": ledger["facets"]["execution"]["commands"][0]["id"],
                        "argv": ledger["facets"]["execution"]["commands"][0]["argv"],
                        "returncode": 0,
                        "timed_out": False,
                    }
                ],
            },
        }
        mismatch_path = self.write_json("kind-role-mismatch.json", mismatched_kind)
        rc, rejected, _stdout, _stderr = self.invoke(
            "record-evidence",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--kind",
            "blackbox",
            "--report",
            mismatch_path,
        )
        self.assertNotEqual(rc, 0, rejected)
        self.assertNotIn("blackbox", self.load_ledger(run_path)["evidence"])

        self.prepare_tester_source(run_id, run_path)
        current = self.load_ledger(run_path)
        tester_source = current["facets"]["execution"]["tester_source"]
        tester_head = tester_source["head"]
        wrong_tester = {
            "schema_version": 1,
            "kind": "tester",
            "status": "pass",
            "candidate_head": tester_head,
            "producer": {
                "role": "tester",
                "agent_id": "different-tester",
                "thread_id": "different-thread",
            },
            "details": {
                "result": "tests_ready",
                "source_head": tester_head,
                "files": tester_source["files"],
            },
        }
        wrong_tester_path = self.write_json("wrong-tester-identity.json", wrong_tester)
        rc, rejected, _stdout, _stderr = self.invoke(
            "record-evidence",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--kind",
            "tester",
            "--report",
            wrong_tester_path,
        )
        self.assertNotEqual(rc, 0, rejected)
        self.assertNotIn("tester", self.load_ledger(run_path)["evidence"])

        self.record_role_evidence(run_id, run_path, "tester")
        rc, machine, _stdout, _stderr = self.invoke(
            "verify-machine", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, machine)
        self.record_role_evidence(run_id, run_path, "blackbox")
        reviewer_head = self.load_ledger(run_path)["facets"]["execution"][
            "candidate_head"
        ]
        wrong_reviewer = {
            "schema_version": 1,
            "kind": "reviewer",
            "status": "pass",
            "candidate_head": reviewer_head,
            "producer": {
                "role": "reviewer",
                "agent_id": "different-reviewer",
                "thread_id": "different-thread",
            },
            "details": {"result": "pass", "reviewed_head": reviewer_head},
        }
        wrong_reviewer_path = self.write_json(
            "wrong-reviewer-identity.json", wrong_reviewer
        )
        rc, rejected, _stdout, _stderr = self.invoke(
            "record-evidence",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--kind",
            "reviewer",
            "--report",
            wrong_reviewer_path,
        )
        self.assertNotEqual(rc, 0, rejected)
        self.assertNotIn("reviewer", self.load_ledger(run_path)["evidence"])

    def test_role_evidence_rejects_empty_details_and_forged_tester_blob(self) -> None:
        run_id = "evidence-content-integrity"
        _data, run_path = self.start(run_id)
        self.prepare_tester_source(run_id, run_path)
        ledger = self.load_ledger(run_path)
        execution = ledger["facets"]["execution"]
        source = execution["tester_source"]
        producer = {"role": "tester", **execution["agents"]["tester"]}

        empty = {
            "schema_version": 1,
            "kind": "tester",
            "status": "pass",
            "candidate_head": execution["candidate_head"],
            "producer": producer,
            "details": {},
        }
        empty_path = self.write_json("empty-tester-details.json", empty)
        rc, rejected, _stdout, _stderr = self.invoke(
            "record-evidence",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--kind",
            "tester",
            "--report",
            empty_path,
        )
        self.assertNotEqual(rc, 0, rejected)
        self.assertNotIn("tester", self.load_ledger(run_path)["evidence"])

        forged = deepcopy(empty)
        forged["details"] = {
            "result": "tests_ready",
            "source_head": source["head"],
            "files": [
                {"path": source["files"][0]["path"], "blob": "0" * 40}
            ],
        }
        forged_path = self.write_json("forged-tester-blob.json", forged)
        rc, rejected, _stdout, _stderr = self.invoke(
            "record-evidence",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--kind",
            "tester",
            "--report",
            forged_path,
        )
        self.assertNotEqual(rc, 0, rejected)
        self.assertNotIn("tester", self.load_ledger(run_path)["evidence"])

    def test_prepare_tester_identity_replacement_preserves_continuity_and_old_incidents(self) -> None:
        run_id = "tester-identity-replace"
        _data, run_path = self.start(run_id)
        self.prepare_required_gates(run_id, run_path)
        before = self.load_ledger(run_path)
        mission_before = deepcopy(before["facets"]["mission"])
        mission_digest_before = before["digests"]["mission"]
        old_source = deepcopy(before["facets"]["execution"]["tester_source"])

        rc, rejected, _stdout, _stderr = self.invoke(
            "prepare-tester",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--agent-id",
            "replacement-tester",
            "--thread-id",
            "replacement-thread",
        )
        self.assertEqual(rc, 1, rejected)
        self.assertEqual(rejected.get("status"), "NEEDS_USER", rejected)
        self.assertEqual(
            self.load_ledger(run_path)["facets"]["execution"]["tester_source"],
            old_source,
        )

        rc, replaced, _stdout, _stderr = self.invoke(
            "prepare-tester",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--agent-id",
            "replacement-tester",
            "--thread-id",
            "replacement-thread",
            "--replace",
        )
        self.assertEqual(rc, 0, replaced)
        after = self.load_ledger(run_path)
        new_source = after["facets"]["execution"]["tester_source"]
        self.assertEqual(
            new_source["agent"],
            {"agent_id": "replacement-tester", "thread_id": "replacement-thread"},
        )
        self.assertNotEqual(new_source["branch"], old_source["branch"])
        self.assertNotEqual(new_source["worktree"], old_source["worktree"])
        self.assertEqual(after["facets"]["mission"], mission_before)
        self.assertEqual(after["digests"]["mission"], mission_digest_before)
        self.assertEqual(replaced["readiness"]["states"]["machine"], "pass")
        for kind in ("tester", "blackbox", "reviewer"):
            self.assertEqual(replaced["readiness"]["states"][kind], "stale")

        dirty_run = "tester-replace-dirty"
        _data, dirty_path = self.start(dirty_run)
        rc, prepared, _stdout, _stderr = self.invoke(
            "prepare-tester",
            "--repo",
            self.repo,
            "--run",
            dirty_run,
            "--agent-id",
            "assurance-v4-tester",
            "--thread-id",
            "assurance-v4-tester-thread",
        )
        self.assertEqual(rc, 0, prepared)
        dirty_before = self.load_ledger(dirty_path)
        dirty_source = dirty_before["facets"]["execution"]["tester_source"]
        dirty_worktree = Path(dirty_source["worktree"])
        dirty_marker = dirty_worktree / "tests" / "replacement-residue.tmp"
        dirty_marker.write_text("preserve tester residue\n", encoding="utf-8")
        rc, rejected, _stdout, _stderr = self.invoke(
            "prepare-tester",
            "--repo",
            self.repo,
            "--run",
            dirty_run,
            "--agent-id",
            "replacement-tester",
            "--thread-id",
            "replacement-thread",
            "--replace",
        )
        self.assertEqual(rc, 1, rejected)
        self.assertEqual(rejected.get("status"), "NEEDS_USER", rejected)
        self.assertTrue(dirty_worktree.is_dir())
        self.assertEqual(dirty_marker.read_text(), "preserve tester residue\n")
        self.assertEqual(
            self.load_ledger(dirty_path)["facets"]["execution"]["tester_source"],
            dirty_source,
        )

        drift_run = "tester-replace-drift"
        _data, drift_path = self.start(drift_run)
        rc, prepared, _stdout, _stderr = self.invoke(
            "prepare-tester",
            "--repo",
            self.repo,
            "--run",
            drift_run,
            "--agent-id",
            "assurance-v4-tester",
            "--thread-id",
            "assurance-v4-tester-thread",
        )
        self.assertEqual(rc, 0, prepared)
        drift_before = self.load_ledger(drift_path)
        drift_source = drift_before["facets"]["execution"]["tester_source"]
        drift_worktree = Path(drift_source["worktree"])
        (drift_worktree / "tests" / "test_drift_fixture.py").write_text(
            "from src.calc import add\n\n"
            "def test_drift_fixture():\n    assert add(4, 5) == 9\n",
            encoding="utf-8",
        )
        drift_head = commit_all(drift_worktree, "drift tester source")
        self.assertNotEqual(drift_head, drift_source["head"])
        rc, rejected, _stdout, _stderr = self.invoke(
            "prepare-tester",
            "--repo",
            self.repo,
            "--run",
            drift_run,
            "--agent-id",
            "replacement-tester",
            "--thread-id",
            "replacement-thread",
            "--replace",
        )
        self.assertEqual(rc, 1, rejected)
        self.assertEqual(rejected.get("status"), "NEEDS_USER", rejected)
        self.assertTrue(drift_worktree.is_dir())
        self.assertEqual(head(drift_worktree), drift_head)
        self.assertEqual(
            self.load_ledger(drift_path)["facets"]["execution"]["tester_source"],
            drift_source,
        )

    def test_prepare_tester_creation_failure_preserves_old_source(self) -> None:
        run_id = "tester-replace-create-failure"
        _data, run_path = self.start(run_id)
        self.prepare_tester_source(run_id, run_path)
        before = self.load_ledger(run_path)
        old_source = deepcopy(before["facets"]["execution"]["tester_source"])
        old_worktree = Path(old_source["worktree"])
        old_branch_head = git(self.repo, "rev-parse", old_source["branch"])

        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        wrapper_dir = self.artifacts / "tester-create-failure-wrapper"
        wrapper_dir.mkdir()
        marker = wrapper_dir / "failed"
        wrapper = wrapper_dir / "git"
        wrapper.write_text(
            "#!/bin/sh\n"
            "case \" $* \" in\n"
            "  *\" worktree add \"*)\n"
            "    : > \"$FAIL_MARKER\"\n"
            "    exit 73\n"
            "    ;;\n"
            "esac\n"
            "exec \"$REAL_GIT\" \"$@\"\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        rc, rejected, _stdout, _stderr = self.invoke(
            "prepare-tester",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--agent-id",
            "replacement-tester",
            "--thread-id",
            "replacement-thread",
            "--replace",
            env={
                "PATH": f"{wrapper_dir}:{os.environ.get('PATH', '')}",
                "REAL_GIT": str(real_git),
                "FAIL_MARKER": str(marker),
            },
        )
        self.assertNotEqual(rc, 0, rejected)
        self.assertTrue(marker.is_file())
        self.assertTrue(old_worktree.is_dir())
        self.assertEqual(head(old_worktree), old_source["head"])
        self.assertEqual(git(self.repo, "rev-parse", old_source["branch"]), old_branch_head)
        after = self.load_ledger(run_path)
        self.assertEqual(after["facets"]["execution"]["tester_source"], old_source)
        self.assertEqual(after["retired_tester_sources"], before["retired_tester_sources"])

    def test_retired_tester_cleanup_recovers_idempotently_after_interruption(self) -> None:
        run_id = "retired-tester-cleanup"
        _data, run_path = self.start(run_id)
        self.prepare_tester_source(run_id, run_path)
        old_source = deepcopy(
            self.load_ledger(run_path)["facets"]["execution"]["tester_source"]
        )
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        wrapper_dir = self.artifacts / "retired-cleanup-wrapper"
        wrapper_dir.mkdir()
        marker = wrapper_dir / "failed"
        wrapper = wrapper_dir / "git"
        wrapper.write_text(
            "#!/bin/sh\n"
            "case \" $* \" in\n"
            "  *\" worktree remove \"*)\n"
            "    case \"$*\" in\n"
            "      *\"$FAIL_WORKTREE\"*)\n"
            "        if [ ! -e \"$FAIL_MARKER\" ]; then\n"
            "          : > \"$FAIL_MARKER\"\n"
            "          exit 74\n"
            "        fi\n"
            "        ;;\n"
            "    esac\n"
            "    ;;\n"
            "esac\n"
            "exec \"$REAL_GIT\" \"$@\"\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        rc, interrupted, _stdout, _stderr = self.invoke(
            "prepare-tester",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--agent-id",
            "replacement-tester",
            "--thread-id",
            "replacement-thread",
            "--replace",
            env={
                "PATH": f"{wrapper_dir}:{os.environ.get('PATH', '')}",
                "REAL_GIT": str(real_git),
                "FAIL_MARKER": str(marker),
                "FAIL_WORKTREE": old_source["worktree"],
            },
        )
        self.assertEqual(rc, 1, interrupted)
        self.assertEqual(interrupted.get("status"), "NEEDS_USER", interrupted)
        self.assertTrue(marker.is_file())
        self.assertTrue(Path(old_source["worktree"]).is_dir())
        interrupted_ledger = self.load_ledger(run_path)
        self.assertEqual(
            interrupted_ledger["facets"]["execution"]["tester_source"]["agent"],
            {"agent_id": "replacement-tester", "thread_id": "replacement-thread"},
        )
        self.assertIn(old_source, interrupted_ledger["retired_tester_sources"])

        rc, abandoned, _stdout, _stderr = self.invoke(
            "abandon",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--reason",
            "exercise terminal retired tester cleanup",
        )
        self.assertEqual(rc, 0, abandoned)

        cleanup_marker = wrapper_dir / "terminal-cleanup-failed"
        rc, cleanup_failed, _stdout, _stderr = self.invoke(
            "cleanup",
            "--repo",
            self.repo,
            "--run",
            run_id,
            env={
                "PATH": f"{wrapper_dir}:{os.environ.get('PATH', '')}",
                "REAL_GIT": str(real_git),
                "FAIL_MARKER": str(cleanup_marker),
                "FAIL_WORKTREE": old_source["worktree"],
            },
        )
        self.assertEqual(rc, 1, cleanup_failed)
        self.assertEqual(cleanup_failed.get("status"), "NEEDS_USER", cleanup_failed)
        self.assertTrue(cleanup_marker.is_file())
        self.assertIn(
            old_source, self.load_ledger(run_path)["retired_tester_sources"]
        )

        rc, cleaned, _stdout, _stderr = self.invoke(
            "cleanup", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, cleaned)
        self.assertFalse(Path(old_source["worktree"]).exists())
        self.assertEqual(self.load_ledger(run_path)["retired_tester_sources"], [])
        rc, repeated, _stdout, _stderr = self.invoke(
            "cleanup", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, repeated)

    def test_blackbox_rejects_report_missing_one_of_two_frozen_commands(self) -> None:
        run_id = "blackbox-two-commands"
        contract = contract_for(self.repo)
        contract["execution"]["commands"].append(
            {
                "id": "fixture-blackbox-second",
                "argv": [sys.executable, "-m", "unittest", "tests.test_calc"],
                "timeout_seconds": 30,
            }
        )
        _data, run_path = self.start(run_id, contract=contract)
        self.record_role_evidence(run_id, run_path, "tester")
        rc, machine, _stdout, _stderr = self.invoke(
            "verify-machine", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, machine)
        ledger = self.load_ledger(run_path)
        execution = ledger["facets"]["execution"]
        command = execution["commands"][0]
        report = {
            "schema_version": 1,
            "kind": "blackbox",
            "status": "pass",
            "candidate_head": execution["candidate_head"],
            "producer": {"role": "tester", **execution["agents"]["tester"]},
            "details": {
                "result": "pass",
                "worktree": ledger["candidate_worktree"],
                "before_head": execution["candidate_head"],
                "after_head": execution["candidate_head"],
                "executions": [
                    {
                        "id": command["id"],
                        "argv": command["argv"],
                        "returncode": 0,
                        "timed_out": False,
                    }
                ],
            },
        }
        report_path = self.write_json("incomplete-blackbox-report.json", report)
        rc, rejected, _stdout, _stderr = self.invoke(
            "record-evidence",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--kind",
            "blackbox",
            "--report",
            report_path,
        )
        self.assertNotEqual(rc, 0, rejected)
        self.assertNotIn("blackbox", self.load_ledger(run_path)["evidence"])

    def test_blackbox_requires_current_tester_and_machine_evidence(self) -> None:
        run_id = "blackbox-prerequisites"
        data, run_path = self.start(run_id)

        ledger = self.load_ledger(run_path)
        tester_agent = ledger["facets"]["execution"]["agents"]["tester"]
        report = {
            "schema_version": 1,
            "kind": "blackbox",
            "status": "pass",
            "candidate_head": ledger["facets"]["execution"]["candidate_head"],
            "producer": {"role": "tester", **tester_agent},
            "details": {
                "result": "pass",
                "worktree": ledger["candidate_worktree"],
                "before_head": ledger["facets"]["execution"]["candidate_head"],
                "after_head": ledger["facets"]["execution"]["candidate_head"],
                "executions": [
                    {
                        "id": ledger["facets"]["execution"]["commands"][0]["id"],
                        "argv": ledger["facets"]["execution"]["commands"][0]["argv"],
                        "returncode": 0,
                        "timed_out": False,
                    }
                ],
            },
        }
        report_path = self.write_json("early-blackbox.json", report)

        def assert_rejected() -> None:
            rc, rejected, _stdout, _stderr = self.invoke(
                "record-evidence",
                "--repo",
                self.repo,
                "--run",
                run_id,
                "--kind",
                "blackbox",
                "--report",
                report_path,
            )
            self.assertNotEqual(rc, 0, rejected)
            self.assertNotIn("blackbox", self.load_ledger(run_path)["evidence"])

        assert_rejected()
        self.record_role_evidence(run_id, run_path, "tester")
        current = self.load_ledger(run_path)
        report["candidate_head"] = current["facets"]["execution"]["candidate_head"]
        report["details"]["before_head"] = report["candidate_head"]
        report["details"]["after_head"] = report["candidate_head"]
        report_path.write_text(json.dumps(report), encoding="utf-8")
        assert_rejected()
        rc, machine, _stdout, _stderr = self.invoke(
            "verify-machine", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, machine)

        candidate = Path(data["candidate_worktree"])
        self.commit_candidate_change(run_id, run_path, candidate)
        current = self.load_ledger(run_path)
        report["candidate_head"] = current["facets"]["execution"]["candidate_head"]
        report["details"]["before_head"] = report["candidate_head"]
        report["details"]["after_head"] = report["candidate_head"]
        report_path.write_text(json.dumps(report), encoding="utf-8")
        self.assertEqual(current["evidence"]["tester"]["status"], "pass")
        assert_rejected()

    def test_execution_candidate_must_match_live_branch_worktree_and_target_lineage(self) -> None:
        detached_run = "candidate-live-identity"
        data, run_path = self.start(detached_run)
        candidate = Path(data["candidate_worktree"])
        ledger_before = self.load_ledger(run_path)
        candidate_branch = ledger_before["candidate_branch"]
        git(candidate, "checkout", "--detach", "-q")
        (candidate / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b + 10\n", encoding="utf-8"
        )
        detached_head = commit_all(candidate, "detached candidate")
        self.assertNotEqual(git(self.repo, "rev-parse", candidate_branch), detached_head)
        execution = deepcopy(ledger_before["facets"]["execution"])
        execution["version"] += 1
        execution["candidate_head"] = detached_head
        execution["builder_files"] = ["src/calc.py"]
        rc, rejected = self.update_facet(detached_run, "execution", execution)
        self.assertNotEqual(rc, 0, rejected)
        self.assertEqual(
            self.load_ledger(run_path)["facets"]["execution"],
            ledger_before["facets"]["execution"],
        )

        lineage_run = "candidate-lineage"
        data, lineage_path = self.start(lineage_run)
        lineage_candidate = Path(data["candidate_worktree"])
        lineage_ledger = self.load_ledger(lineage_path)
        lineage_branch = lineage_ledger["candidate_branch"]
        git(lineage_candidate, "switch", "--orphan", "orphan-fixture")
        git(lineage_candidate, "rm", "-rf", ".", check=False)
        (lineage_candidate / "src").mkdir(parents=True, exist_ok=True)
        (lineage_candidate / "src" / "calc.py").write_text(
            "def add(a, b):\n    return 123\n", encoding="utf-8"
        )
        orphan_head = commit_all(lineage_candidate, "orphan candidate")
        git(lineage_candidate, "branch", "-D", lineage_branch)
        git(lineage_candidate, "branch", "-m", lineage_branch)
        self.assertNotEqual(
            run_process(
                [
                    "git",
                    "-C",
                    lineage_candidate,
                    "merge-base",
                    "--is-ancestor",
                    lineage_ledger["target_start_head"],
                    orphan_head,
                ]
            ).returncode,
            0,
        )
        execution = deepcopy(lineage_ledger["facets"]["execution"])
        execution["version"] += 1
        execution["candidate_head"] = orphan_head
        execution["builder_files"] = ["src/calc.py"]
        rc, rejected = self.update_facet(lineage_run, "execution", execution)
        self.assertNotEqual(rc, 0, rejected)
        self.assertEqual(
            self.load_ledger(lineage_path)["facets"]["execution"],
            lineage_ledger["facets"]["execution"],
        )

    def test_verify_machine_runs_in_candidate_and_never_turns_failure_into_pass(self) -> None:
        isolated_run = "machine-isolated"
        isolated_contract = contract_for(
            self.repo,
            machine_argv=[
                sys.executable,
                "-c",
                (
                    "import pathlib,sys;"
                    f"sys.exit(pathlib.Path.cwd().resolve() == pathlib.Path({str(self.repo)!r}).resolve())"
                ),
            ],
        )
        data, run_path = self.start(isolated_run, contract=isolated_contract)
        candidate = Path(data["candidate_worktree"])
        rc, verified, _stdout, _stderr = self.invoke(
            "verify-machine", "--repo", self.repo, "--run", isolated_run
        )
        self.assertEqual(rc, 0, verified)
        ledger = self.load_ledger(run_path)
        self.assertEqual(verified["readiness"]["states"]["machine"], "pass")
        self.assertEqual(ledger["evidence"]["machine"]["status"], "pass")
        self.assertEqual(ledger["evidence"]["machine"]["candidate_head"], head(candidate))

        failed_run = "machine-failure"
        failed_contract = contract_for(
            self.repo,
            machine_argv=[sys.executable, "-c", "raise SystemExit(7)"],
        )
        _data, failed_path = self.start(failed_run, contract=failed_contract)
        rc, failed, _stdout, _stderr = self.invoke(
            "verify-machine", "--repo", self.repo, "--run", failed_run
        )
        self.assertEqual(rc, 0, failed)
        self.assertEqual(failed.get("status"), "ACTIVE", failed)
        failed_ledger = self.load_ledger(failed_path)
        self.assertEqual(failed_ledger["evidence"]["machine"]["status"], "fail")
        self.assertEqual(failed["readiness"]["states"]["machine"], "failed")

    def test_verify_machine_ignores_path_injected_bash_and_records_trusted_identity(self) -> None:
        run_id = "machine-path-injection"
        contract = contract_for(
            self.repo,
            machine_argv=["bash", "-c", "exit 9"],
        )
        _data, run_path = self.start(run_id, contract=contract)
        fake_dir = self.artifacts / "fake-path-bin"
        fake_dir.mkdir()
        fake_bash = fake_dir / "bash"
        fake_bash.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_bash.chmod(0o755)

        rc, verified, _stdout, _stderr = self.invoke(
            "verify-machine",
            "--repo",
            self.repo,
            "--run",
            run_id,
            env={"PATH": f"{fake_dir}:{os.environ.get('PATH', '')}"},
        )
        self.assertEqual(rc, 0, verified)
        self.assertEqual(verified["readiness"]["states"]["machine"], "failed")
        evidence = self.load_ledger(run_path)["evidence"]["machine"]
        self.assertEqual(evidence["status"], "fail")
        command = evidence["details"]["commands"][0]
        executable = Path(command["executable"])
        self.assertNotEqual(executable.parent, fake_dir)
        self.assertEqual(command["returncode"], 9)
        identity = command["executable_identity"]
        self.assertEqual(identity["requested"], "bash")
        self.assertEqual(identity["path"], str(executable))
        self.assertEqual(identity["kind"], "system")
        self.assertRegex(identity["sha256"], r"^[0-9a-f]{64}$")

    def test_repository_relative_machine_runner_is_bound_and_escape_or_symlink_is_rejected(self) -> None:
        positive_run = "repository-runner"
        contract = contract_for(self.repo, machine_argv=["./verify.sh"])
        data, run_path = self.start(positive_run, contract=contract)
        candidate = Path(data["candidate_worktree"])
        rc, verified, _stdout, _stderr = self.invoke(
            "verify-machine", "--repo", self.repo, "--run", positive_run
        )
        self.assertEqual(rc, 0, verified)
        evidence = self.load_ledger(run_path)["evidence"]["machine"]
        self.assertEqual(evidence["status"], "pass")
        command = evidence["details"]["commands"][0]
        executable = Path(command["executable"])
        self.assertNotEqual(executable, candidate / "verify.sh")
        self.assertEqual(executable.name, "verify.sh")
        identity = command["executable_identity"]
        self.assertEqual(identity["kind"], "repository")
        self.assertEqual(identity["requested"], "./verify.sh")
        self.assertEqual(identity["path"], "verify.sh")
        self.assertRegex(identity["blob"], r"^[0-9a-f]{40}$")

        escape_contract = contract_for(self.repo, machine_argv=["../outside.sh"])
        escape_path = self.write_json("escape-runner-contract.json", escape_contract)
        rc, started, _stdout, _stderr = self.invoke(
            "start",
            "--repo",
            self.repo,
            "--run",
            "escape-runner",
            "--session-id",
            "assurance-v4-test-session",
            "--contract",
            escape_path,
        )
        self.assertNotEqual(rc, 0, started)

        (self.repo / "runner-link").symlink_to("verify.sh")
        commit_all(self.repo, "add repository runner symlink fixture")
        symlink_contract = contract_for(self.repo, machine_argv=["./runner-link"])
        _data, symlink_path = self.start("symlink-runner", contract=symlink_contract)
        rc, rejected, _stdout, _stderr = self.invoke(
            "verify-machine", "--repo", self.repo, "--run", "symlink-runner"
        )
        self.assertNotEqual(rc, 0, rejected)
        evidence = self.load_ledger(symlink_path)["evidence"].get("machine")
        self.assertTrue(evidence is None or evidence["status"] != "pass")

    def test_driver_next_derives_gate_order_without_persisting_dispatch_state(self) -> None:
        run_id = "driver-order"
        data, run_path = self.start(run_id)
        self.commit_candidate_change(
            run_id, run_path, Path(data["candidate_worktree"])
        )

        def assert_next(expected_action: str) -> None:
            before = (run_path / "ledger.json").read_bytes()
            rc, decision, _stdout, _stderr = self.invoke(
                "driver-next", "--repo", self.repo, "--run", run_id
            )
            self.assertEqual(rc, 0, decision)
            self.assertEqual(decision.get("action"), expected_action, decision)
            self.assertEqual((run_path / "ledger.json").read_bytes(), before)

        assert_next("tester_author")
        self.record_role_evidence(run_id, run_path, "tester")
        assert_next("verify_machine")
        rc, machine, _stdout, _stderr = self.invoke(
            "verify-machine", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, machine)
        assert_next("tester_blackbox")
        self.record_role_evidence(run_id, run_path, "blackbox")
        assert_next("reviewer_final")
        self.record_role_evidence(run_id, run_path, "reviewer")
        assert_next("finalize")

    def test_repeated_failure_signature_only_requests_architecture_review(self) -> None:
        run_id = "failure-signature"
        contract = contract_for(
            self.repo,
            machine_argv=[sys.executable, "-c", "raise SystemExit(9)"],
        )
        data, run_path = self.start(run_id, contract=contract)
        self.commit_candidate_change(
            run_id, run_path, Path(data["candidate_worktree"])
        )
        self.record_role_evidence(run_id, run_path, "tester")
        mission_before = deepcopy(self.load_ledger(run_path)["facets"]["mission"])

        for _attempt in range(3):
            rc, failed, _stdout, _stderr = self.invoke(
                "verify-machine", "--repo", self.repo, "--run", run_id
            )
            self.assertEqual(rc, 0, failed)
            self.assertEqual(failed["readiness"]["states"]["machine"], "failed")

        rc, decision, _stdout, _stderr = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, decision)
        self.assertEqual(decision.get("action"), "architecture_review", decision)
        self.assertEqual(
            self.load_ledger(run_path)["facets"]["mission"], mission_before
        )

    def test_nonconflicting_target_drift_rematerializes_without_mission_change(self) -> None:
        run_id = "rematerialize-clean"
        data, run_path = self.start(run_id)
        candidate = Path(data["candidate_worktree"])
        candidate_before = self.commit_candidate_change(run_id, run_path, candidate)
        self.prepare_required_gates(run_id, run_path)
        before = self.load_ledger(run_path)

        (self.repo / "README.md").write_text("fixture\ntarget advance\n", encoding="utf-8")
        target_advanced = commit_all(self.repo, "advance target independently")
        rc, rematerialized, _stdout, _stderr = self.invoke(
            "rematerialize-target", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, rematerialized)
        after = self.load_ledger(run_path)
        candidate_after = head(candidate)
        self.assertEqual(after["target_start_head"], target_advanced)
        self.assertNotEqual(candidate_after, candidate_before)
        self.assertEqual(after["facets"]["execution"]["candidate_head"], candidate_after)
        self.assertEqual(after["facets"]["mission"], before["facets"]["mission"])
        for kind in ("tester", "machine", "blackbox", "reviewer"):
            self.assertEqual(rematerialized["readiness"]["states"][kind], "stale")

    def test_conflicting_target_drift_stops_without_moving_heads(self) -> None:
        run_id = "rematerialize-conflict"
        data, run_path = self.start(run_id)
        candidate = Path(data["candidate_worktree"])
        self.commit_candidate_change(
            run_id,
            run_path,
            candidate,
            content="def add(a, b):\n    return a + b + 1\n",
        )
        ledger_before = self.load_ledger(run_path)
        candidate_before = head(candidate)
        (self.repo / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b - 1\n", encoding="utf-8"
        )
        target_advanced = commit_all(self.repo, "conflicting target advance")

        rc, stopped, _stdout, _stderr = self.invoke(
            "rematerialize-target", "--repo", self.repo, "--run", run_id
        )
        self.assertNotEqual(rc, 0, stopped)
        self.assertIn(stopped.get("status"), {"NEEDS_USER", "FATAL"}, stopped)
        self.assertEqual(head(self.repo), target_advanced)
        self.assertEqual(head(candidate), candidate_before)
        after = self.load_ledger(run_path)
        self.assertEqual(after["target_start_head"], ledger_before["target_start_head"])
        self.assertEqual(after["facets"]["execution"], ledger_before["facets"]["execution"])

    def test_finalize_squashes_candidate_and_preserves_nonoverlapping_dirty(self) -> None:
        run_id = "finalize-clean"
        data, run_path = self.start(run_id)
        candidate = Path(data["candidate_worktree"])
        target_start = head(self.repo)
        self.commit_candidate_change(run_id, run_path, candidate)
        self.prepare_required_gates(run_id, run_path)
        (self.repo / "README.md").write_text(
            "fixture\nkeep local target edit\n", encoding="utf-8"
        )

        rc, finalized, _stdout, _stderr = self.invoke(
            "finalize",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--message",
            "test(assurance): [cr_id_skip] Finalize fixture candidate",
        )
        self.assertEqual(rc, 0, finalized)
        final_head = head(self.repo)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD^"), target_start)
        self.assertEqual((self.repo / "src" / "calc.py").read_text().splitlines()[-1], "VALUE = 2")
        self.assertEqual(
            (self.repo / "README.md").read_text(),
            "fixture\nkeep local target edit\n",
        )
        self.assertIn("README.md", git(self.repo, "status", "--porcelain=v1"))
        ledger = self.load_ledger(run_path)
        self.assertEqual(ledger["phase"], "finalized")
        self.assertEqual(ledger["final_head"], final_head)

    def test_finalize_stops_on_overlapping_dirty(self) -> None:
        run_id = "finalize-overlap"
        data, run_path = self.start(run_id)
        candidate = Path(data["candidate_worktree"])
        self.commit_candidate_change(run_id, run_path, candidate)
        self.prepare_required_gates(run_id, run_path)
        target_before = head(self.repo)
        (self.repo / "src" / "calc.py").write_text(
            "def add(a, b):\n    return 99\n", encoding="utf-8"
        )
        rc, stopped, _stdout, _stderr = self.invoke(
            "finalize",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--message",
            "test(assurance): [cr_id_skip] Reject overlap",
        )
        self.assertNotEqual(rc, 0, stopped)
        self.assertEqual(head(self.repo), target_before)
        self.assertNotEqual(self.load_ledger(run_path)["phase"], "finalized")

    def test_finalize_stops_on_target_drift(self) -> None:
        drift_run = "finalize-drift"
        data, drift_path = self.start(drift_run)
        drift_candidate = Path(data["candidate_worktree"])
        self.commit_candidate_change(drift_run, drift_path, drift_candidate)
        self.prepare_required_gates(drift_run, drift_path)
        (self.repo / "README.md").write_text("fixture\ntarget drift\n", encoding="utf-8")
        drifted_head = commit_all(self.repo, "target drift before finalize")
        rc, stopped, _stdout, _stderr = self.invoke(
            "finalize",
            "--repo",
            self.repo,
            "--run",
            drift_run,
            "--message",
            "test(assurance): [cr_id_skip] Reject target drift",
        )
        self.assertNotEqual(rc, 0, stopped)
        self.assertEqual(head(self.repo), drifted_head)
        self.assertNotEqual(self.load_ledger(drift_path)["phase"], "finalized")

    def interrupt_finalize(
        self,
        run_id: str,
        *,
        after_cas: bool,
    ) -> tuple[Path, Path, str]:
        data, run_path = self.start(run_id)
        candidate = Path(data["candidate_worktree"])
        self.commit_candidate_change(run_id, run_path, candidate)
        self.prepare_required_gates(run_id, run_path)
        target_start = head(self.repo)

        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        wrapper_dir = self.artifacts / f"{run_id}-git-wrapper"
        wrapper_dir.mkdir()
        marker = wrapper_dir / "interrupted"
        cas_marker = wrapper_dir / "cas-complete"
        wrapper = wrapper_dir / "git"
        if after_cas:
            script = (
                "#!/bin/sh\n"
                "has_update_ref=0\n"
                "for arg in \"$@\"; do\n"
                "  [ \"$arg\" = update-ref ] && has_update_ref=1\n"
                "done\n"
                "if [ \"$has_update_ref\" = 1 ]; then\n"
                "  \"$REAL_GIT\" \"$@\"\n"
                "  rc=$?\n"
                "  [ \"$rc\" = 0 ] && : > \"$CAS_MARKER\"\n"
                "  exit \"$rc\"\n"
                "fi\n"
                "if [ -e \"$CAS_MARKER\" ] && [ ! -e \"$CRASH_MARKER\" ]; then\n"
                "  : > \"$CRASH_MARKER\"\n"
                "  kill -9 \"$PPID\"\n"
                "  exit 137\n"
                "fi\n"
                "exec \"$REAL_GIT\" \"$@\"\n"
            )
        else:
            script = (
                "#!/bin/sh\n"
                "for arg in \"$@\"; do\n"
                "  if [ \"$arg\" = update-ref ] && [ ! -e \"$CRASH_MARKER\" ]; then\n"
                "    : > \"$CRASH_MARKER\"\n"
                "    kill -9 \"$PPID\"\n"
                "    exit 137\n"
                "  fi\n"
                "done\n"
                "exec \"$REAL_GIT\" \"$@\"\n"
            )
        wrapper.write_text(script, encoding="utf-8")
        wrapper.chmod(0o755)
        crashed = run_process(
            [
                sys.executable,
                CLI,
                "assurance",
                "--experimental-v4",
                "finalize",
                "--repo",
                self.repo,
                "--run",
                run_id,
                "--message",
                "test(assurance): [cr_id_skip] Recover interrupted finalize",
            ],
            env={
                "PATH": f"{wrapper_dir}:{os.environ.get('PATH', '')}",
                "REAL_GIT": str(real_git),
                "CRASH_MARKER": str(marker),
                "CAS_MARKER": str(cas_marker),
            },
        )
        self.assertNotEqual(crashed.returncode, 0)
        self.assertTrue(marker.is_file())
        interrupted = self.load_ledger(run_path)
        self.assertIsInstance(interrupted["finalize_intent"], dict)
        if after_cas:
            self.assertEqual(head(self.repo), interrupted["finalize_intent"]["final_head"])
        else:
            self.assertEqual(head(self.repo), target_start)
        return run_path, candidate, target_start

    def test_recover_finalize_is_idempotent_before_and_after_cas_interruptions(self) -> None:
        for after_cas in (False, True):
            with self.subTest(after_cas=after_cas):
                run_id = f"recover-{'after' if after_cas else 'before'}-cas"
                run_path, _candidate, _target_start = self.interrupt_finalize(
                    run_id, after_cas=after_cas
                )
                rc, recovered, _stdout, _stderr = self.invoke(
                    "recover-finalize", "--repo", self.repo, "--run", run_id
                )
                self.assertEqual(rc, 0, recovered)
                finalized = self.load_ledger(run_path)
                self.assertEqual(finalized["phase"], "finalized")
                self.assertEqual(head(self.repo), finalized["final_head"])
                final_head = head(self.repo)

                rc, repeated, _stdout, _stderr = self.invoke(
                    "recover-finalize", "--repo", self.repo, "--run", run_id
                )
                self.assertEqual(rc, 0, repeated)
                self.assertEqual(head(self.repo), final_head)
                self.assertEqual(self.load_ledger(run_path)["final_head"], final_head)

                if not after_cas:
                    cleanup_repo(self.repo)
                    self.repo = init_repo()

    def test_recover_finalize_preserves_intent_when_target_diverged(self) -> None:
        run_id = "recover-target-diverged"
        run_path, _candidate, _target_start = self.interrupt_finalize(
            run_id, after_cas=False
        )
        intent_before = deepcopy(self.load_ledger(run_path)["finalize_intent"])
        (self.repo / "README.md").write_text(
            "fixture\ntarget diverged after intent\n", encoding="utf-8"
        )
        diverged_head = commit_all(self.repo, "diverge target after finalize intent")

        rc, stopped, _stdout, _stderr = self.invoke(
            "recover-finalize", "--repo", self.repo, "--run", run_id
        )
        self.assertNotEqual(rc, 0, stopped)
        self.assertEqual(head(self.repo), diverged_head)
        ledger = self.load_ledger(run_path)
        self.assertEqual(ledger["finalize_intent"], intent_before)
        self.assertNotEqual(ledger["phase"], "finalized")

    def test_recover_finalize_preserves_candidate_residue_and_intent(self) -> None:
        run_id = "recover-candidate-residue"
        run_path, candidate, target_start = self.interrupt_finalize(
            run_id, after_cas=False
        )
        intent_before = deepcopy(self.load_ledger(run_path)["finalize_intent"])
        residue = candidate / "recovery-residue.tmp"
        residue.write_text("preserve interrupted candidate residue\n", encoding="utf-8")

        rc, stopped, _stdout, _stderr = self.invoke(
            "recover-finalize", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 1, stopped)
        self.assertEqual(stopped.get("status"), "NEEDS_USER", stopped)
        self.assertEqual(head(self.repo), target_start)
        self.assertTrue(candidate.is_dir())
        self.assertEqual(
            residue.read_text(), "preserve interrupted candidate residue\n"
        )
        ledger = self.load_ledger(run_path)
        self.assertEqual(ledger["finalize_intent"], intent_before)
        self.assertNotEqual(ledger["phase"], "finalized")

    def test_legacy_start_remains_disabled_by_default(self) -> None:
        completed = run_process(
            [
                sys.executable,
                CLI,
                "start",
                "--repo",
                self.repo,
                "--plan",
                self.artifacts / "missing-plan.md",
                "--task",
                "legacy start must remain disabled",
                "--session-id",
                "legacy-default-refusal",
            ]
        )
        lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, completed)
        data = json.loads(lines[-1])
        self.assertEqual(completed.returncode, 2, data)
        self.assertEqual(data.get("status"), "FATAL", data)
        self.assertIn(
            data.get("code"),
            {"BUILDER_START_DISABLED", "BUILDER_MAINTENANCE_DISABLED"},
            data,
        )
        self.assertFalse((self.repo / ".builder-loop").exists())


if __name__ == "__main__":
    unittest.main()
