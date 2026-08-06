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
from unittest.mock import patch

from harness import CLI, cleanup_repo, commit_all, git, head, init_repo, run_process
from runtime.codex_builder_loop.assurance_v4 import core as assurance_core


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
                {
                    "id": "add-positive",
                    "description": "add(1, 2) returns 3.",
                    "observation": {
                        "surface_id": "public-calculator",
                        "surface_description": "The public calculator behavior observed by the blackbox command.",
                        "execution_ids": ["fixture-blackbox"],
                        "required_dimensions": ["verify"],
                    },
                }
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


def blackbox_case_results(
    ledger: Mapping[str, Any], *, passed: bool = True
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for case in ledger["facets"]["mission"]["acceptance_cases"]:
        observation = case.get("observation")
        if not isinstance(observation, dict):
            continue
        required = set(observation["required_dimensions"])
        dimensions = {
            name: {
                "status": (
                    "pass"
                    if passed and name in required
                    else "fail"
                    if not passed and name in required
                    else "not_applicable"
                ),
                "observation": f"fixture {name} observation",
            }
            for name in ("mechanical", "verify", "quality")
        }
        result = {
            "case_id": case["id"],
            "surface_id": observation["surface_id"],
            "execution_ids": list(observation["execution_ids"]),
            **dimensions,
            "outcome": "pass" if passed else "fail",
        }
        if "target_id" in observation:
            result["target_id"] = observation["target_id"]
        results.append(result)
    return results


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
        auto_doc_scan: bool = True,
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
        if (
            auto_doc_scan
            and completed.returncode == 0
            and command
            in {
                "checkpoint-builder",
                "integrate-tester",
                "recompose-candidate",
                "rematerialize-target",
                "update-facet",
            }
        ):
            values = list(args)
            run_id = str(values[values.index("--run") + 1])
            repo = str(values[values.index("--repo") + 1])
            next_rc, next_action, _next_stdout, _next_stderr = self.invoke(
                "driver-next",
                "--repo",
                repo,
                "--run",
                run_id,
                auto_doc_scan=False,
            )
            self.assertEqual(next_rc, 0, next_action)
            if next_action.get("action") == "scan_doc_references":
                scan_args: list[str | Path] = [
                    "--repo",
                    repo,
                    "--run",
                    run_id,
                    "--action-id",
                    str(next_action["action_id"]),
                ]
                runtime_kind = next_action.get("driver_runtime_kind")
                if isinstance(runtime_kind, str):
                    scan_args.extend(["--driver-runtime-kind", runtime_kind])
                scan_rc, scanned, _scan_stdout, _scan_stderr = self.invoke(
                    "scan-doc-references",
                    *scan_args,
                    auto_doc_scan=False,
                )
                self.assertEqual(scan_rc, 0, scanned)
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

    def bind_native_runtime(self, run_path: Path) -> None:
        ledger_path = run_path / "ledger.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        ledger["driver_runtime"] = {
            "kind": "native",
            "protocol_version": 1,
            "transport": "codex_app_server",
            "runtime_version": "codex-test",
            "protocol_schema_digest": "a" * 64,
        }
        ledger_path.write_text(
            json.dumps(ledger, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def record_role_evidence(
        self,
        run_id: str,
        run_path: Path,
        kind: str,
    ) -> dict[str, Any]:
        if kind == "tester":
            self.prepare_tester_source(run_id, run_path)
        if kind in {"reviewer_preflight", "reviewer", "doc_review"}:
            ledger_before_review = self.load_ledger(run_path)
            if assurance_core.doc_reference_scan_state(ledger_before_review) in {
                "missing",
                "stale",
            }:
                assurance_core.scan_doc_references(self.repo, run_id)
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
                "cases": blackbox_case_results(ledger),
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

    def prepare_publication_refresh(
        self, run_id: str
    ) -> tuple[Path, Path, dict[str, Any]]:
        contract = contract_for(self.repo)
        contract["authority"]["public_prerequisites"] = ["src/calc.py"]
        started, run_path = self.start(run_id, contract=contract)
        candidate = Path(started["candidate_worktree"])
        (candidate / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\nPUBLIC_API = 1\n",
            encoding="utf-8",
        )
        commit_all(candidate, "publish prerequisite")
        rc, checkpointed, _stdout, _stderr = self.invoke(
            "checkpoint-builder", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, checkpointed)
        rc, published, _stdout, _stderr = self.invoke(
            "publish-prerequisites", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, published)
        self.prepare_tester_source(run_id, run_path)
        before = self.load_ledger(run_path)
        (candidate / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\nPUBLIC_API = 2\n",
            encoding="utf-8",
        )
        commit_all(candidate, "refresh prerequisite")
        rc, refreshing, _stdout, _stderr = self.invoke(
            "checkpoint-builder", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, refreshing)
        self.assertEqual(
            refreshing.get("recomposition_intent", {}).get("kind"),
            "publication_refresh",
        )
        return run_path, candidate, before

    def test_tester_source_deletion_is_rejected_before_candidate_integration(self) -> None:
        run_id = "tester-deletion-rejected"
        _started, run_path = self.start(run_id)
        ledger = self.load_ledger(run_path)
        execution = ledger["facets"]["execution"]
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
        tester_worktree = Path(
            self.load_ledger(run_path)["facets"]["execution"]["tester_source"]["worktree"]
        )
        existing_test = tester_worktree / "tests" / "test_calc.py"
        self.assertTrue(existing_test.is_file())
        existing_test.unlink()
        commit_all(tester_worktree, "delete existing test")
        candidate_before = head(Path(ledger["candidate_worktree"]))

        rc, rejected, _stdout, _stderr = self.invoke(
            "integrate-tester", "--repo", self.repo, "--run", run_id
        )

        self.assertEqual(rc, 2, rejected)
        self.assertEqual(rejected.get("code"), "TESTER_SOURCE_ENTRY_UNSUPPORTED")
        self.assertEqual(head(Path(ledger["candidate_worktree"])), candidate_before)

    def test_tester_integration_rejects_generator_style_nested_proof_wrapper(self) -> None:
        run_id = "tester-proof-wrapper-rejected"
        _started, run_path = self.start(run_id)
        ledger = self.load_ledger(run_path)
        execution = ledger["facets"]["execution"]
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
        before = self.load_ledger(run_path)
        source_before = deepcopy(before["facets"]["execution"]["tester_source"])
        candidate = Path(before["candidate_worktree"])
        candidate_head_before = head(candidate)
        evidence_before = deepcopy(before["evidence"])
        tester = Path(source_before["worktree"])
        wrapper = tester / "tests" / "test_generator_wrapper.py"
        wrapper.write_text(
            "import os\n"
            "import subprocess\n\n"
            "def test_generator_wrapper():\n"
            "    env = os.environ.copy()\n"
            "    env.pop('CODEX_BUILDER_PROOF', None)\n"
            "    env.pop('PYTEST_PLUGINS', None)\n"
            "    result = subprocess.run(\n"
            "        ['/tmp/uv', 'run', 'pytest', 'tests/test_target.py'],\n"
            "        env=env,\n"
            "        check=False,\n"
            "    )\n"
            "    assert result.returncode == 0\n",
            encoding="utf-8",
        )
        commit_all(tester, "author generator-style nested proof wrapper")

        rc, rejected, _stdout, _stderr = self.invoke(
            "integrate-tester", "--repo", self.repo, "--run", run_id
        )

        self.assertNotEqual(rc, 0, rejected)
        self.assertEqual(
            rejected.get("code"), "TESTER_ROLE_BOUNDARY_VIOLATION", rejected
        )
        self.assertTrue(
            any(
                item.get("path") == "tests/test_generator_wrapper.py"
                and str(item.get("marker", "")).strip()
                for item in rejected.get("findings", [])
            ),
            rejected,
        )
        after = self.load_ledger(run_path)
        self.assertEqual(head(candidate), candidate_head_before)
        self.assertEqual(
            after["facets"]["execution"]["candidate_head"], candidate_head_before
        )
        self.assertEqual(
            after["facets"]["execution"]["tester_source"], source_before
        )
        self.assertEqual(after["facets"]["execution"]["tester_files"], [])
        self.assertEqual(after["evidence"], evidence_before)

    def test_status_derives_stage_retry_and_replay_telemetry_from_events(self) -> None:
        run_id = "telemetry-derived"
        _started, run_path = self.start(run_id)
        ledger = self.load_ledger(run_path)
        ledger["events"].extend(
            [
                {
                    "at": "2026-08-01T00:00:01+00:00",
                    "kind": "dispatch_prepared",
                    "details": {
                        "action_id": "a" * 64,
                        "action": "tester_author",
                    },
                },
                {
                    "at": "2026-08-01T00:00:03+00:00",
                    "kind": "dispatch_retry_scheduled",
                    "details": {
                        "action_id": "a" * 64,
                        "failure_code": "responseStreamDisconnected",
                    },
                },
                {
                    "at": "2026-08-01T00:00:05+00:00",
                    "kind": "dispatch_completed",
                    "details": {"action_id": "a" * 64},
                },
                {
                    "at": "2026-08-01T00:00:06+00:00",
                    "kind": "machine_verified",
                    "details": {"status": "fail", "duration_ms": 1250},
                },
                {
                    "at": "2026-08-01T00:00:07+00:00",
                    "kind": "machine_verified",
                    "details": {"status": "pass", "duration_ms": 750},
                },
                {
                    "at": "2026-08-01T00:00:08+00:00",
                    "kind": "builder_checkpointed",
                    "details": {},
                },
            ]
        )
        (run_path / "ledger.json").write_text(
            json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
        )

        rc, data, _stdout, _stderr = self.invoke(
            "status", "--repo", self.repo, "--run", run_id
        )

        self.assertEqual(rc, 0, data)
        telemetry = data["telemetry"]
        self.assertEqual(telemetry["candidate_changes"], 1)
        self.assertEqual(telemetry["evidence_attempts"]["machine"], 2)
        self.assertEqual(telemetry["evidence_replays"], 1)
        self.assertEqual(
            telemetry["retries"],
            {
                "total": 1,
                "by_failure_code": {"responseStreamDisconnected": 1},
            },
        )
        stages = {item["name"]: item for item in telemetry["stages"]}
        self.assertEqual(stages["tester_author"]["total_duration_ms"], 4000)
        self.assertEqual(stages["tester_author"]["retry_count"], 1)
        self.assertEqual(stages["verify_machine"]["attempts"], 2)
        self.assertEqual(stages["verify_machine"]["failed_attempts"], 1)
        self.assertEqual(stages["verify_machine"]["total_duration_ms"], 2000)

    def test_early_machine_command_stops_before_expensive_full_suite(self) -> None:
        marker = self.artifacts / "expensive-command-ran"
        contract = contract_for(self.repo)
        contract["assurance"]["machine_commands"] = [
            {
                "id": "expensive-suite",
                "argv": [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
                ],
                "timeout_seconds": 30,
            },
            {
                "id": "delivery-test",
                "argv": [sys.executable, "-c", "raise SystemExit(7)"],
                "timeout_seconds": 30,
                "run_before_full_suite": True,
            },
        ]
        _data, run_path = self.start("early-machine-stop", contract=contract)

        rc, result, _stdout, _stderr = self.invoke(
            "verify-machine", "--repo", self.repo, "--run", "early-machine-stop"
        )

        self.assertEqual(rc, 0, result)
        self.assertFalse(marker.exists())
        commands = self.load_ledger(run_path)["evidence"]["machine"]["details"]["commands"]
        self.assertEqual([item["id"] for item in commands], ["delivery-test"])
        self.assertEqual(commands[0]["returncode"], 7)

    def test_blackbox_pass_uses_frozen_expected_returncodes(self) -> None:
        run_id = "expected-nonzero-blackbox"
        contract = contract_for(self.repo)
        contract["execution"]["commands"][0]["expected_returncodes"] = [1]
        _data, run_path = self.start(run_id, contract=contract)
        self.record_role_evidence(run_id, run_path, "tester")
        rc, machine, _stdout, _stderr = self.invoke(
            "verify-machine", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, machine)
        ledger = self.load_ledger(run_path)
        command = ledger["facets"]["execution"]["commands"][0]
        candidate = ledger["facets"]["execution"]["candidate_head"]
        agent = ledger["facets"]["execution"]["agents"]["tester"]
        report = {
            "schema_version": 1,
            "kind": "blackbox",
            "status": "pass",
            "candidate_head": candidate,
            "producer": {"role": "tester", **agent},
            "details": {
                "result": "pass",
                "worktree": ledger["candidate_worktree"],
                "before_head": candidate,
                "after_head": candidate,
                "executions": [
                    {
                        "id": command["id"],
                        "argv": command["argv"],
                        "returncode": 1,
                        "timed_out": False,
                    }
                ],
                "cases": blackbox_case_results(ledger),
            },
        }
        report_path = self.write_json("expected-nonzero-report.json", report)

        rc, accepted, _stdout, _stderr = self.invoke(
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
        self.assertEqual(rc, 0, accepted)
        self.assertEqual(accepted["readiness"]["states"]["blackbox"], "pass")

        report["details"]["executions"][0]["returncode"] = 0
        report_path.write_text(json.dumps(report), encoding="utf-8")
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
        self.assertEqual(rc, 2, rejected)
        self.assertEqual(rejected.get("code"), "BLACKBOX_EXECUTION_FAILED")

    def test_new_blackbox_contract_requires_valid_observation_bindings(self) -> None:
        for name, mutate, expected_code in (
            (
                "missing",
                lambda contract: contract["mission"]["acceptance_cases"][0].pop(
                    "observation"
                ),
                "ACCEPTANCE_OBSERVATION_REQUIRED",
            ),
            (
                "command",
                lambda contract: contract["mission"]["acceptance_cases"][0][
                    "observation"
                ].update(execution_ids=["unknown-blackbox"]),
                "ACCEPTANCE_OBSERVATION_COMMAND_INVALID",
            ),
            (
                "target",
                lambda contract: contract["mission"]["acceptance_cases"][0][
                    "observation"
                ].update(target_id="unknown-target"),
                "ACCEPTANCE_OBSERVATION_TARGET_INVALID",
            ),
        ):
            with self.subTest(name=name):
                contract = contract_for(self.repo)
                mutate(contract)
                path = self.write_json(f"invalid-observation-{name}.json", contract)
                rc, rejected, _stdout, _stderr = self.invoke(
                    "start",
                    "--repo",
                    self.repo,
                    "--run",
                    f"invalid-observation-{name}",
                    "--session-id",
                    "assurance-v4-test-session",
                    "--contract",
                    path,
                )
                self.assertNotEqual(rc, 0, rejected)
                self.assertEqual(rejected.get("code"), expected_code, rejected)

    def test_blackbox_case_surface_command_and_dimensions_are_bound(self) -> None:
        run_id = "bound-blackbox-cases"
        _data, run_path = self.start(run_id)
        self.record_role_evidence(run_id, run_path, "tester")
        self.invoke("verify-machine", "--repo", self.repo, "--run", run_id)
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
                "cases": blackbox_case_results(ledger),
            },
        }

        def record(name: str, value: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            path = self.write_json(f"bound-blackbox-{name}.json", value)
            rc, data, _stdout, _stderr = self.invoke(
                "record-evidence",
                "--repo",
                self.repo,
                "--run",
                run_id,
                "--kind",
                "blackbox",
                "--report",
                path,
            )
            return rc, data

        missing = deepcopy(report)
        missing["details"].pop("cases")
        rc, rejected = record("missing", missing)
        self.assertNotEqual(rc, 0, rejected)
        self.assertEqual(rejected.get("code"), "BLACKBOX_CASE_COVERAGE_MISMATCH")

        proxy = deepcopy(report)
        proxy["details"]["cases"][0]["surface_id"] = "internal-api"
        rc, rejected = record("proxy", proxy)
        self.assertNotEqual(rc, 0, rejected)
        self.assertEqual(
            rejected.get("code"), "BLACKBOX_OBSERVATION_BINDING_MISMATCH"
        )

        inapplicable = deepcopy(report)
        inapplicable["details"]["cases"][0]["verify"]["status"] = "not_applicable"
        rc, rejected = record("inapplicable", inapplicable)
        self.assertNotEqual(rc, 0, rejected)
        self.assertEqual(rejected.get("code"), "BLACKBOX_CASE_RESULT_MISMATCH")

        rc, accepted = record("accepted", report)
        self.assertEqual(rc, 0, accepted)
        self.assertEqual(accepted["readiness"]["states"]["blackbox"], "pass")

    def test_retained_legacy_v4_ledger_accepts_command_only_blackbox(self) -> None:
        run_id = "legacy-command-only-blackbox"
        _data, run_path = self.start(run_id)
        ledger = self.load_ledger(run_path)
        ledger["facets"]["mission"]["acceptance_cases"][0].pop("observation")
        ledger["digests"]["mission"] = canonical_digest(ledger["facets"]["mission"])
        (run_path / "ledger.json").write_text(
            json.dumps(ledger, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        self.record_role_evidence(run_id, run_path, "tester")
        self.invoke("verify-machine", "--repo", self.repo, "--run", run_id)
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
        path = self.write_json("legacy-command-only-blackbox.json", report)
        rc, accepted, _stdout, _stderr = self.invoke(
            "record-evidence",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--kind",
            "blackbox",
            "--report",
            path,
        )
        self.assertEqual(rc, 0, accepted)
        self.assertEqual(accepted["readiness"]["states"]["blackbox"], "pass")

    def deployment_fixture(
        self, *, retention: str = "lease"
    ) -> tuple[dict[str, Any], Path, Path]:
        state = self.artifacts / "revision-shared-environment.json"
        operations = self.artifacts / "revision-deployment-operations.log"
        state.write_text(json.dumps({"artifact": None, "value": "stable"}), encoding="utf-8")
        operations.write_text("", encoding="utf-8")
        artifact_version = self.repo / "src" / "artifact-version.txt"
        artifact_version.write_text("revision-candidate-artifact", encoding="utf-8")
        script = self.repo / "revision_deploy_fixture.py"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import hashlib, json, os, pathlib, sys\n"
            f"state = pathlib.Path({str(state)!r})\n"
            f"operations = pathlib.Path({str(operations)!r})\n"
            "mode = sys.argv[1]\n"
            "if mode == 'build':\n"
            "    path = pathlib.Path(os.environ['BUILDER_LOOP_ARTIFACT_PATH'])\n"
            "    path.parent.mkdir(parents=True, exist_ok=True)\n"
            "    path.write_bytes(pathlib.Path('src/artifact-version.txt').read_bytes())\n"
            "elif mode == 'deploy':\n"
            "    operations.write_text(operations.read_text() + 'deploy\\n')\n"
            "    state.write_text(json.dumps({'artifact': os.environ['BUILDER_LOOP_ARTIFACT_SHA256'], 'value': 'candidate'}))\n"
            "elif mode == 'restore':\n"
            "    operations.write_text(operations.read_text() + 'restore\\n')\n"
            "    state.write_text(json.dumps({'artifact': None, 'value': 'stable'}))\n"
            "elif mode == 'probe':\n"
            "    value = json.loads(state.read_text())\n"
            "    digest = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()\n"
            "    print(json.dumps({'schema_version': 1, 'target_id': 'revision-fixture', 'state_digest': digest, 'deployed_artifact_sha256': value['artifact']}))\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        commit_all(self.repo, "add revision deployment fixture")
        contract = contract_for(self.repo)
        contract["authority"]["external_targets"] = [
            {"id": "revision-fixture", "description": "Revision continuity fixture."}
        ]
        contract["mission"]["acceptance_cases"][0]["observation"][
            "target_id"
        ] = "revision-fixture"
        command = lambda command_id, mode: {
            "id": command_id,
            "argv": ["./revision_deploy_fixture.py", mode],
            "timeout_seconds": 30,
        }
        contract["execution"]["deployment"] = {
            "target_id": "revision-fixture",
            "artifact_path": "dist/revision-app.bin",
            "build_command": command("build-revision", "build"),
            "deploy_command": command("deploy-revision", "deploy"),
            "probe_command": command("probe-revision", "probe"),
            "restore_command": command("restore-revision", "restore"),
            "revision_retention": retention,
        }
        return contract, state, operations

    def stage_deployment_blackbox(self, run_id: str, run_path: Path) -> None:
        ledger = self.load_ledger(run_path)
        command = ledger["facets"]["execution"]["commands"][0]
        candidate = ledger["facets"]["execution"]["candidate_head"]
        agent = ledger["facets"]["execution"]["agents"]["tester"]
        report = {
            "schema_version": 1,
            "kind": "blackbox",
            "status": "pass",
            "candidate_head": candidate,
            "producer": {"role": "tester", **agent},
            "details": {
                "result": "pass",
                "worktree": ledger["candidate_worktree"],
                "before_head": candidate,
                "after_head": candidate,
                "executions": [
                    {
                        "id": command["id"],
                        "argv": command["argv"],
                        "returncode": 0,
                        "timed_out": False,
                    }
                ],
                "cases": blackbox_case_results(ledger),
            },
        }
        path = self.write_json(f"{run_id}-staged-blackbox.json", report)
        rc, staged, _stdout, _stderr = self.invoke(
            "stage-blackbox", "--repo", self.repo, "--run", run_id, "--report", path
        )
        self.assertEqual(rc, 0, staged)

    def test_environment_lease_survives_same_run_revision_and_releases_once(self) -> None:
        contract, _state, operations = self.deployment_fixture()
        run_id = "same-run-environment-lease"
        _started, run_path = self.start(run_id, contract=contract)
        self.invoke("checkpoint-builder", "--repo", self.repo, "--run", run_id)
        self.record_role_evidence(run_id, run_path, "tester")
        self.invoke("verify-machine", "--repo", self.repo, "--run", run_id)
        rc, deployed, _stdout, _stderr = self.invoke(
            "prepare-deployment", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, deployed)
        self.stage_deployment_blackbox(run_id, run_path)
        rc, completed, _stdout, _stderr = self.invoke(
            "complete-blackbox", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, completed)
        self.assertEqual(completed["environment_lease"]["state"], "held")
        self.assertEqual(operations.read_text(), "deploy\n")

        ledger = self.load_ledger(run_path)
        revised = deepcopy(ledger["facets"]["mission"])
        revised["revision"] += 1
        revised["objective"] = "Deliver the revised assured calculator mission."
        revised["supersedes"] = {
            "run_id": run_id,
            "revision": ledger["facets"]["mission"]["revision"],
            "mission_digest": ledger["digests"]["mission"],
            "candidate_head": ledger["facets"]["execution"]["candidate_head"],
        }
        revision_path = self.write_json("same-run-revised-mission.json", revised)
        rc, revision, _stdout, _stderr = self.invoke(
            "revise-mission", "--repo", self.repo, "--run", run_id, "--mission", revision_path
        )
        self.assertEqual(rc, 0, revision)
        self.assertEqual(revision["mission_revision"], 2)
        self.record_role_evidence(run_id, run_path, "tester")
        self.invoke("verify-machine", "--repo", self.repo, "--run", run_id)
        self.stage_deployment_blackbox(run_id, run_path)
        self.invoke("complete-blackbox", "--repo", self.repo, "--run", run_id)
        self.assertEqual(operations.read_text(), "deploy\n")
        self.record_role_evidence(run_id, run_path, "reviewer")
        rc, decision, _stdout, _stderr = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, decision)
        self.assertEqual(decision["action"], "restore_deployment")
        rc, restored, _stdout, _stderr = self.invoke(
            "restore-deployment", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, restored)
        self.assertEqual(restored["environment_lease"]["state"], "released")
        self.assertEqual(operations.read_text(), "deploy\nrestore\n")
        self.assertEqual(restored["readiness"]["states"]["blackbox"], "pass")

    def test_driver_failure_restores_deployment_before_entering_failed_phase(self) -> None:
        contract, _state, operations = self.deployment_fixture()
        run_id = "driver-failure-deployment-recovery"
        _started, run_path = self.start(run_id, contract=contract)
        self.invoke("checkpoint-builder", "--repo", self.repo, "--run", run_id)
        self.record_role_evidence(run_id, run_path, "tester")
        self.invoke("verify-machine", "--repo", self.repo, "--run", run_id)
        rc, deployed, _stdout, _stderr = self.invoke(
            "prepare-deployment", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, deployed)
        self.assertEqual(deployed["deployment_transaction"]["state"], "deployed")
        self.bind_native_runtime(run_path)
        failure_path = self.write_json(
            "driver-failure-deployment.json",
            {
                "source": "native_driver",
                "status": "FATAL",
                "code": "NATIVE_DEPLOYMENT_FIXTURE_FATAL",
                "message": "Native Driver stopped while the environment lease was held.",
                "details": {"fixture": run_id},
                "action": None,
            },
        )
        rc, recorded, _stdout, _stderr = self.invoke(
            "record-driver-failure",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--failure",
            failure_path,
            "--driver-runtime-kind",
            "native",
        )
        self.assertEqual(rc, 0, recorded)
        self.assertEqual(recorded["phase"], "active")
        self.assertEqual(recorded["driver_failure"]["recovery"], "deployment")

        rc, failed, _stdout, _stderr = self.invoke(
            "complete-driver-failure",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--driver-runtime-kind",
            "native",
        )

        self.assertEqual(rc, 0, failed)
        self.assertEqual(failed["phase"], "failed")
        self.assertEqual(failed["status"], "FATAL")
        self.assertEqual(failed["driver_failure"]["state"], "terminal")
        self.assertEqual(failed["deployment_transaction"]["state"], "restored")
        self.assertEqual(failed["environment_lease"]["state"], "released")
        self.assertEqual(operations.read_text(), "deploy\nrestore\n")

    def test_driver_failure_restore_failure_stays_recoverable_not_failed(self) -> None:
        contract, _state, operations = self.deployment_fixture()
        run_id = "driver-failure-deployment-blocked"
        _started, run_path = self.start(run_id, contract=contract)
        self.invoke("checkpoint-builder", "--repo", self.repo, "--run", run_id)
        self.record_role_evidence(run_id, run_path, "tester")
        self.invoke("verify-machine", "--repo", self.repo, "--run", run_id)
        self.invoke("prepare-deployment", "--repo", self.repo, "--run", run_id)
        self.bind_native_runtime(run_path)
        operations.unlink()
        operations.mkdir()
        failure_path = self.write_json(
            "driver-failure-deployment-blocked.json",
            {
                "source": "native_driver",
                "status": "FATAL",
                "code": "NATIVE_DEPLOYMENT_RESTORE_FIXTURE_FATAL",
                "message": "Native Driver stopped before deployment recovery.",
                "details": {"fixture": run_id},
                "action": None,
            },
        )
        rc, recorded, _stdout, _stderr = self.invoke(
            "record-driver-failure",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--failure",
            failure_path,
            "--driver-runtime-kind",
            "native",
        )
        self.assertEqual(rc, 0, recorded)

        rc, blocked, _stdout, _stderr = self.invoke(
            "complete-driver-failure",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--driver-runtime-kind",
            "native",
        )

        self.assertEqual(rc, 1, blocked)
        self.assertEqual(blocked.get("status"), "NEEDS_USER", blocked)
        ledger = self.load_ledger(run_path)
        self.assertEqual(ledger["phase"], "active")
        self.assertEqual(ledger["driver_failure"]["state"], "recovering")
        self.assertEqual(ledger["deployment_transaction"]["state"], "restore_failed")
        rc, decision, _stdout, _stderr = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, decision)
        self.assertEqual(decision["action"], "complete_driver_failure")

    def test_superseding_artifact_change_restores_old_lease_before_deploy(self) -> None:
        contract, _state, operations = self.deployment_fixture()
        source_run = "changed-artifact-source"
        _started, source_path = self.start(source_run, contract=contract)
        self.invoke("checkpoint-builder", "--repo", self.repo, "--run", source_run)
        self.record_role_evidence(source_run, source_path, "tester")
        self.invoke("verify-machine", "--repo", self.repo, "--run", source_run)
        self.invoke("prepare-deployment", "--repo", self.repo, "--run", source_run)
        self.stage_deployment_blackbox(source_run, source_path)
        self.invoke("complete-blackbox", "--repo", self.repo, "--run", source_run)
        source = self.load_ledger(source_path)
        target_contract = deepcopy(contract)
        target_contract["mission"]["revision"] = 2
        target_contract["mission"]["objective"] = "Deliver a changed artifact revision."
        target_contract["mission"]["supersedes"] = {
            "run_id": source_run,
            "revision": 1,
            "mission_digest": source["digests"]["mission"],
            "candidate_head": source["facets"]["execution"]["candidate_head"],
        }
        target_run = "changed-artifact-target"
        _target, target_path = self.start(target_run, contract=target_contract)
        candidate = Path(self.load_ledger(target_path)["candidate_worktree"])
        (candidate / "src" / "artifact-version.txt").write_text(
            "revision-candidate-artifact-v2", encoding="utf-8"
        )
        commit_all(candidate, "change revision artifact")
        self.invoke("checkpoint-builder", "--repo", self.repo, "--run", target_run)
        self.record_role_evidence(target_run, target_path, "tester")
        self.invoke("verify-machine", "--repo", self.repo, "--run", target_run)
        rc, mismatch, _stdout, _stderr = self.invoke(
            "prepare-deployment", "--repo", self.repo, "--run", target_run
        )
        self.assertEqual(rc, 0, mismatch)
        self.assertEqual(mismatch["supersede_intent"]["state"], "artifact_mismatch")
        rc, decision, _stdout, _stderr = self.invoke(
            "driver-next", "--repo", self.repo, "--run", target_run
        )
        self.assertEqual(rc, 0, decision)
        self.assertEqual(decision["action"], "restore_superseded_environment")
        rc, restored, _stdout, _stderr = self.invoke(
            "restore-superseded-environment", "--repo", self.repo, "--run", target_run
        )
        self.assertEqual(rc, 0, restored)
        rc, deployed, _stdout, _stderr = self.invoke(
            "prepare-deployment", "--repo", self.repo, "--run", target_run
        )
        self.assertEqual(rc, 0, deployed)
        self.assertEqual(deployed["deployment_transaction"]["deploy_action"], "executed")
        self.assertEqual(operations.read_text(), "deploy\nrestore\ndeploy\n")

    def test_abandon_with_held_environment_restores_before_terminal(self) -> None:
        contract, _state, operations = self.deployment_fixture()
        run_id = "abandon-held-environment"
        _started, run_path = self.start(run_id, contract=contract)
        self.invoke("checkpoint-builder", "--repo", self.repo, "--run", run_id)
        self.record_role_evidence(run_id, run_path, "tester")
        self.invoke("verify-machine", "--repo", self.repo, "--run", run_id)
        self.invoke("prepare-deployment", "--repo", self.repo, "--run", run_id)
        rc, abandoning, _stdout, _stderr = self.invoke(
            "abandon", "--repo", self.repo, "--run", run_id, "--reason", "revision cancelled"
        )
        self.assertEqual(rc, 0, abandoning)
        self.assertEqual(abandoning["phase"], "active")
        self.assertEqual(abandoning["deployment_transaction"]["state"], "restore_required")
        rc, restored, _stdout, _stderr = self.invoke(
            "restore-deployment", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, restored)
        self.assertEqual(restored["phase"], "abandoned")
        self.assertEqual(operations.read_text(), "deploy\nrestore\n")

    def test_superseding_run_carries_candidate_and_transfers_environment_lease(self) -> None:
        contract, _state, operations = self.deployment_fixture()
        source_run = "lease-source-revision"
        _started, source_path = self.start(source_run, contract=contract)
        self.invoke("checkpoint-builder", "--repo", self.repo, "--run", source_run)
        self.record_role_evidence(source_run, source_path, "tester")
        self.invoke("verify-machine", "--repo", self.repo, "--run", source_run)
        self.invoke("prepare-deployment", "--repo", self.repo, "--run", source_run)
        self.stage_deployment_blackbox(source_run, source_path)
        self.invoke("complete-blackbox", "--repo", self.repo, "--run", source_run)
        source = self.load_ledger(source_path)

        target_contract = deepcopy(contract)
        target_contract["mission"]["revision"] = 2
        target_contract["mission"]["objective"] = "Deliver a superseding calculator mission."
        target_contract["mission"]["supersedes"] = {
            "run_id": source_run,
            "revision": source["facets"]["mission"]["revision"],
            "mission_digest": source["digests"]["mission"],
            "candidate_head": source["facets"]["execution"]["candidate_head"],
        }
        target_run = "lease-target-revision"
        _target, target_path = self.start(target_run, contract=target_contract)
        target = self.load_ledger(target_path)
        self.assertEqual(
            target["facets"]["execution"]["carryover"]["source_candidate_head"],
            source["facets"]["execution"]["candidate_head"],
        )
        self.assertEqual(target["evidence"], {})
        self.invoke("checkpoint-builder", "--repo", self.repo, "--run", target_run)
        self.record_role_evidence(target_run, target_path, "tester")
        self.invoke("verify-machine", "--repo", self.repo, "--run", target_run)
        rc, transferred, _stdout, _stderr = self.invoke(
            "prepare-deployment", "--repo", self.repo, "--run", target_run
        )
        self.assertEqual(rc, 0, transferred)
        self.assertEqual(transferred["environment_lease"]["owner_run_id"], target_run)
        self.assertEqual(transferred["deployment_transaction"]["deploy_action"], "skipped_existing")
        self.assertEqual(operations.read_text(), "deploy\n")
        source_after = self.load_ledger(source_path)
        target_after = self.load_ledger(target_path)
        source_after["phase"] = "active"
        source_after["environment_lease"]["state"] = "transfer_prepared"
        source_after["supersede_intent"] = {
            "source_run_id": source_run,
            "target_run_id": target_run,
            "state": "prepared",
        }
        target_after["supersede_intent"] = {
            "source_run_id": source_run,
            "target_run_id": target_run,
            "state": "received",
        }
        (source_path / "ledger.json").write_text(json.dumps(source_after), encoding="utf-8")
        (target_path / "ledger.json").write_text(json.dumps(target_after), encoding="utf-8")
        rc, decision, _stdout, _stderr = self.invoke(
            "driver-next", "--repo", self.repo, "--run", target_run
        )
        self.assertEqual(rc, 0, decision)
        self.assertEqual(decision["action"], "complete_supersede_transfer")
        rc, recovered, _stdout, _stderr = self.invoke(
            "complete-supersede-transfer", "--repo", self.repo, "--run", target_run
        )
        self.assertEqual(rc, 0, recovered)
        self.assertEqual(self.load_ledger(source_path)["phase"], "superseded")
        self.assertIsNone(self.load_ledger(target_path)["supersede_intent"])
        rc, restored, _stdout, _stderr = self.invoke(
            "restore-deployment", "--repo", self.repo, "--run", target_run
        )
        self.assertEqual(rc, 0, restored)
        self.assertEqual(operations.read_text(), "deploy\nrestore\n")

    def test_deployment_transaction_restores_before_blackbox_evidence(self) -> None:
        state = self.artifacts / "shared-environment.json"
        fail_restore = self.artifacts / "fail-restore"
        operations = self.artifacts / "deployment-operations.log"
        state.write_text(json.dumps({"artifact": None, "value": "stable"}), encoding="utf-8")
        script = self.repo / "deploy_fixture.py"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import hashlib, json, os, pathlib, sys\n"
            f"state = pathlib.Path({str(state)!r})\n"
            f"fail_restore = pathlib.Path({str(fail_restore)!r})\n"
            f"operations = pathlib.Path({str(operations)!r})\n"
            "mode = sys.argv[1]\n"
            "if mode == 'build':\n"
            "    path = pathlib.Path(os.environ['BUILDER_LOOP_ARTIFACT_PATH'])\n"
            "    path.parent.mkdir(parents=True, exist_ok=True)\n"
            "    path.write_bytes(b'candidate-artifact')\n"
            "elif mode == 'deploy':\n"
            "    operations.write_text(operations.read_text() + 'deploy\\n' if operations.exists() else 'deploy\\n')\n"
            "    state.write_text(json.dumps({'artifact': os.environ['BUILDER_LOOP_ARTIFACT_SHA256'], 'value': 'candidate'}))\n"
            "elif mode == 'restore':\n"
            "    operations.write_text(operations.read_text() + 'restore\\n' if operations.exists() else 'restore\\n')\n"
            "    if fail_restore.exists(): raise SystemExit(9)\n"
            "    state.write_text(json.dumps({'artifact': None, 'value': 'stable'}))\n"
            "elif mode == 'probe':\n"
            "    value = json.loads(state.read_text())\n"
            "    digest = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()\n"
            "    print(json.dumps({'schema_version': 1, 'target_id': 'shared-fixture', 'state_digest': digest, 'deployed_artifact_sha256': value['artifact']}))\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        commit_all(self.repo, "add deployment fixture")
        contract = contract_for(self.repo)
        contract["authority"]["external_targets"] = [
            {"id": "shared-fixture", "description": "Disposable shared fixture."}
        ]
        contract["mission"]["acceptance_cases"][0]["observation"][
            "target_id"
        ] = "shared-fixture"
        command = lambda command_id, mode: {
            "id": command_id,
            "argv": ["./deploy_fixture.py", mode],
            "timeout_seconds": 30,
        }
        contract["execution"]["deployment"] = {
            "target_id": "shared-fixture",
            "artifact_path": "dist/app.bin",
            "build_command": command("build-candidate", "build"),
            "deploy_command": command("deploy-candidate", "deploy"),
            "probe_command": command("probe-environment", "probe"),
            "restore_command": command("restore-environment", "restore"),
        }
        run_id = "deployment-transaction"
        target_before = head(self.repo)
        _data, run_path = self.start(run_id, contract=contract)
        rc, checkpointed, _stdout, _stderr = self.invoke(
            "checkpoint-builder", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, checkpointed)
        self.record_role_evidence(run_id, run_path, "tester")
        rc, machine, _stdout, _stderr = self.invoke(
            "verify-machine", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, machine)
        rc, decision, _stdout, _stderr = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, decision)
        self.assertEqual(decision["action"], "prepare_deployment")

        rc, deployed, _stdout, _stderr = self.invoke(
            "prepare-deployment", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, deployed)
        transaction = deployed["deployment_transaction"]
        self.assertEqual(transaction["state"], "deployed")
        self.assertEqual(json.loads(state.read_text())["artifact"], transaction["artifact_sha256"])
        rc, decision, _stdout, _stderr = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, decision)
        self.assertEqual(decision["action"], "tester_blackbox")
        ledger = self.load_ledger(run_path)
        command_spec = ledger["facets"]["execution"]["commands"][0]
        candidate = ledger["facets"]["execution"]["candidate_head"]
        agent = ledger["facets"]["execution"]["agents"]["tester"]
        report = {
            "schema_version": 1,
            "kind": "blackbox",
            "status": "pass",
            "candidate_head": candidate,
            "producer": {"role": "tester", **agent},
            "details": {
                "result": "pass",
                "worktree": ledger["candidate_worktree"],
                "before_head": candidate,
                "after_head": candidate,
                "executions": [{"id": command_spec["id"], "argv": command_spec["argv"], "returncode": 0, "timed_out": False}],
                "cases": blackbox_case_results(ledger),
            },
        }
        report_path = self.write_json("deployment-blackbox.json", report)
        rc, staged, _stdout, _stderr = self.invoke(
            "stage-blackbox", "--repo", self.repo, "--run", run_id, "--report", report_path
        )
        self.assertEqual(rc, 0, staged)
        self.assertNotIn("blackbox", self.load_ledger(run_path)["evidence"])
        rc, decision, _stdout, _stderr = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, decision)
        self.assertEqual(decision["action"], "restore_deployment")
        rc, restored, _stdout, _stderr = self.invoke(
            "restore-deployment", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, restored)
        self.assertEqual(json.loads(state.read_text()), {"artifact": None, "value": "stable"})
        rc, decision, _stdout, _stderr = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, decision)
        self.assertEqual(decision["action"], "complete_blackbox")
        rc, completed, _stdout, _stderr = self.invoke(
            "complete-blackbox", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, completed)
        evidence = self.load_ledger(run_path)["evidence"]["blackbox"]
        self.assertEqual(evidence["details"]["deployment"]["target_id"], "shared-fixture")
        self.assertEqual(evidence["details"]["deployment"]["deploy_action"], "executed")
        self.assertEqual(head(self.repo), target_before)

        reused_run = "deployment-reused-across-revision"
        operations.write_text("", encoding="utf-8")
        state.write_text(
            json.dumps({"artifact": transaction["artifact_sha256"], "value": "candidate"}),
            encoding="utf-8",
        )
        _data, reused_path = self.start(reused_run, contract=contract)
        rc, checkpointed, _stdout, _stderr = self.invoke(
            "checkpoint-builder", "--repo", self.repo, "--run", reused_run
        )
        self.assertEqual(rc, 0, checkpointed)
        self.record_role_evidence(reused_run, reused_path, "tester")
        rc, machine, _stdout, _stderr = self.invoke(
            "verify-machine", "--repo", self.repo, "--run", reused_run
        )
        self.assertEqual(rc, 0, machine)
        rc, reused, _stdout, _stderr = self.invoke(
            "prepare-deployment", "--repo", self.repo, "--run", reused_run
        )
        self.assertEqual(rc, 0, reused)
        reused_transaction = reused["deployment_transaction"]
        self.assertEqual(reused_transaction["state"], "deployed")
        self.assertEqual(reused_transaction["deploy_action"], "skipped_existing")
        self.assertEqual(operations.read_text(), "")
        reused_ledger = self.load_ledger(reused_path)
        reused_candidate = reused_ledger["facets"]["execution"]["candidate_head"]
        reused_agent = reused_ledger["facets"]["execution"]["agents"]["tester"]
        reused_command = reused_ledger["facets"]["execution"]["commands"][0]
        reused_report = deepcopy(report)
        reused_report["candidate_head"] = reused_candidate
        reused_report["producer"] = {"role": "tester", **reused_agent}
        reused_report["details"]["worktree"] = reused_ledger["candidate_worktree"]
        reused_report["details"]["before_head"] = reused_candidate
        reused_report["details"]["after_head"] = reused_candidate
        reused_report["details"]["executions"][0]["id"] = reused_command["id"]
        reused_report["details"]["executions"][0]["argv"] = reused_command["argv"]
        reused_report_path = self.write_json("reused-deployment-blackbox.json", reused_report)
        rc, staged, _stdout, _stderr = self.invoke(
            "stage-blackbox", "--repo", self.repo, "--run", reused_run, "--report", reused_report_path
        )
        self.assertEqual(rc, 0, staged)
        rc, released, _stdout, _stderr = self.invoke(
            "restore-deployment", "--repo", self.repo, "--run", reused_run
        )
        self.assertEqual(rc, 0, released)
        self.assertEqual(released["deployment_transaction"]["state"], "restored")
        self.assertEqual(operations.read_text(), "")
        rc, completed, _stdout, _stderr = self.invoke(
            "complete-blackbox", "--repo", self.repo, "--run", reused_run
        )
        self.assertEqual(rc, 0, completed)
        reused_evidence = self.load_ledger(reused_path)["evidence"]["blackbox"]
        self.assertEqual(
            reused_evidence["details"]["deployment"]["deploy_action"],
            "skipped_existing",
        )

        drift_run = "deployment-reuse-state-drift"
        _data, drift_path = self.start(drift_run, contract=contract)
        rc, checkpointed, _stdout, _stderr = self.invoke(
            "checkpoint-builder", "--repo", self.repo, "--run", drift_run
        )
        self.assertEqual(rc, 0, checkpointed)
        self.record_role_evidence(drift_run, drift_path, "tester")
        rc, machine, _stdout, _stderr = self.invoke(
            "verify-machine", "--repo", self.repo, "--run", drift_run
        )
        self.assertEqual(rc, 0, machine)
        rc, reused, _stdout, _stderr = self.invoke(
            "prepare-deployment", "--repo", self.repo, "--run", drift_run
        )
        self.assertEqual(rc, 0, reused)
        self.assertEqual(reused["deployment_transaction"]["deploy_action"], "skipped_existing")
        state.write_text(
            json.dumps({"artifact": transaction["artifact_sha256"], "value": "drifted"}),
            encoding="utf-8",
        )
        rc, drifted, _stdout, _stderr = self.invoke(
            "restore-deployment", "--repo", self.repo, "--run", drift_run
        )
        self.assertEqual(rc, 1, drifted)
        self.assertEqual(drifted.get("code"), "DEPLOYMENT_REUSE_STATE_DRIFT")
        self.assertEqual(operations.read_text(), "")

        state.write_text(json.dumps({"artifact": None, "value": "stable"}), encoding="utf-8")
        operations.write_text("", encoding="utf-8")

        failed_run = "deployment-restore-failure"
        _data, failed_path = self.start(failed_run, contract=contract)
        rc, checkpointed, _stdout, _stderr = self.invoke(
            "checkpoint-builder", "--repo", self.repo, "--run", failed_run
        )
        self.assertEqual(rc, 0, checkpointed)
        self.record_role_evidence(failed_run, failed_path, "tester")
        rc, machine, _stdout, _stderr = self.invoke(
            "verify-machine", "--repo", self.repo, "--run", failed_run
        )
        self.assertEqual(rc, 0, machine)
        rc, deployed, _stdout, _stderr = self.invoke(
            "prepare-deployment", "--repo", self.repo, "--run", failed_run
        )
        self.assertEqual(rc, 0, deployed)
        failed_ledger = self.load_ledger(failed_path)
        failed_candidate = failed_ledger["facets"]["execution"]["candidate_head"]
        failed_agent = failed_ledger["facets"]["execution"]["agents"]["tester"]
        failed_command = failed_ledger["facets"]["execution"]["commands"][0]
        failed_report = deepcopy(report)
        failed_report["candidate_head"] = failed_candidate
        failed_report["producer"] = {"role": "tester", **failed_agent}
        failed_report["details"]["worktree"] = failed_ledger["candidate_worktree"]
        failed_report["details"]["before_head"] = failed_candidate
        failed_report["details"]["after_head"] = failed_candidate
        failed_report["details"]["executions"][0]["id"] = failed_command["id"]
        failed_report["details"]["executions"][0]["argv"] = failed_command["argv"]
        failed_report_path = self.write_json("failed-restore-blackbox.json", failed_report)
        rc, staged, _stdout, _stderr = self.invoke(
            "stage-blackbox",
            "--repo",
            self.repo,
            "--run",
            failed_run,
            "--report",
            failed_report_path,
        )
        self.assertEqual(rc, 0, staged)
        fail_restore.write_text("fail", encoding="utf-8")
        rc, restore_failed, _stdout, _stderr = self.invoke(
            "restore-deployment", "--repo", self.repo, "--run", failed_run
        )
        self.assertEqual(rc, 1, restore_failed)
        self.assertEqual(restore_failed.get("code"), "DEPLOYMENT_RESTORE_FAILED")
        failed_ledger = self.load_ledger(failed_path)
        self.assertEqual(failed_ledger["deployment_transaction"]["state"], "restore_failed")
        self.assertNotIn("blackbox", failed_ledger["evidence"])

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

    def record_plan_problem(
        self,
        run_id: str,
        *,
        key: str,
        decision_request: dict[str, Any] | None,
    ) -> None:
        problem = {
            "key": key,
            "summary": "A frozen contract decision is required.",
            "details": "The user must approve the exact replacement delta.",
            "owner": "plan",
        }
        if decision_request is not None:
            problem["decision_request"] = decision_request
        report_path = self.write_json(
            f"{run_id}-{key}-problem.json",
            {"schema_version": 1, "problems": [problem]},
        )
        rc, value, _stdout, _stderr = self.invoke(
            "record-problems",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--report",
            report_path,
            "--role",
            "builder",
            "--agent-id",
            "decision-builder",
            "--thread-id",
            "decision-builder-thread",
        )
        self.assertEqual(rc, 0, value)

    def validate_contract_decision(
        self,
        run_id: str,
        *,
        key: str,
        action_id: str,
        facet: str,
        facet_digest: str,
        contract: dict[str, Any],
        session_id: str = "assurance-v4-test-session",
    ) -> tuple[int, dict[str, Any]]:
        contract_path = self.write_json(f"{run_id}-{key}-replacement.json", contract)
        rc, value, _stdout, _stderr = self.invoke(
            "validate-decision",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--session-id",
            session_id,
            "--problem-key",
            key,
            "--action-id",
            action_id,
            "--facet",
            facet,
            "--facet-digest",
            facet_digest,
            "--contract",
            contract_path,
        )
        return rc, value

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

    def test_pre_revision_continuity_ledger_remains_readable(self) -> None:
        run_id = "pre-revision-continuity-ledger"
        _data, run_path = self.start(run_id)
        ledger = self.load_ledger(run_path)
        ledger.pop("environment_lease", None)
        ledger.pop("supersede_intent", None)
        ledger.pop("abandon_intent", None)
        ledger["facets"]["mission"].pop("supersedes", None)
        ledger["facets"]["execution"].pop("carryover", None)
        (run_path / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
        rc, status, _stdout, _stderr = self.invoke(
            "status", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, status)
        self.assertEqual(status["phase"], "active")

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

    def test_validate_decision_binds_exact_delta_and_rejects_stale_handoff(self) -> None:
        run_id = "validated-authority-decision"
        _data, run_path = self.start(run_id)
        key = "expand-generated-authority"
        self.record_plan_problem(
            run_id,
            key=key,
            decision_request={
                "kind": "facet_change",
                "facet": "authority",
                "changes": [
                    {
                        "pointer": "/builder_write/-",
                        "operation": "add",
                        "value": "generated/**",
                    }
                ],
                "question": "Allow Builder to update generated sources?",
            },
        )
        rc, action, _stdout, _stderr = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, action)
        self.assertEqual(action.get("action"), "contract_decision", action)
        ledger = self.load_ledger(run_path)
        replacement = deepcopy(ledger["facets"])
        replacement["authority"]["builder_write"].append("generated/**")

        rc, validated = self.validate_contract_decision(
            run_id,
            key=key,
            action_id=action["action_id"],
            facet="authority",
            facet_digest=ledger["digests"]["authority"],
            contract=replacement,
        )

        self.assertEqual(rc, 0, validated)
        self.assertEqual(validated.get("status"), "READY", validated)
        self.assertEqual(validated.get("apply", {}).get("command"), "update-facet")
        self.assertEqual(
            validated.get("apply", {}).get("authorization_flags"),
            ["authorize_expansion"],
        )

        hidden = deepcopy(replacement)
        hidden["assurance"]["reviewer_preflight"] = True
        rc, rejected = self.validate_contract_decision(
            run_id,
            key=key,
            action_id=action["action_id"],
            facet="authority",
            facet_digest=ledger["digests"]["authority"],
            contract=hidden,
        )
        self.assertNotEqual(rc, 0, rejected)
        self.assertEqual(rejected.get("code"), "DECISION_REPLACEMENT_DRIFT", rejected)

        rc, applied = self.update_facet(
            run_id,
            "authority",
            replacement["authority"],
            "--authorize-expansion",
            "--resolve-plan-problem-key",
            key,
            "--decision-action-id",
            action["action_id"],
            "--expected-facet-digest",
            ledger["digests"]["authority"],
            "--session-id",
            "assurance-v4-test-session",
        )
        self.assertEqual(rc, 0, applied)
        resolved = self.load_ledger(run_path)
        self.assertEqual(resolved["problems"][0]["status"], "resolved")

        rc, stale = self.validate_contract_decision(
            run_id,
            key=key,
            action_id=action["action_id"],
            facet="authority",
            facet_digest=ledger["digests"]["authority"],
            contract=replacement,
        )
        self.assertNotEqual(rc, 0, stale)
        self.assertEqual(stale.get("code"), "DECISION_ACTION_STALE", stale)

    def test_validate_decision_supports_legacy_problem_fallback_and_session_binding(self) -> None:
        run_id = "legacy-decision-fallback"
        _data, run_path = self.start(run_id)
        key = "legacy-assurance-choice"
        self.record_plan_problem(run_id, key=key, decision_request=None)
        rc, action, _stdout, _stderr = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, action)
        ledger = self.load_ledger(run_path)
        replacement = deepcopy(ledger["facets"])
        replacement["assurance"]["reviewer_preflight"] = True

        rc, wrong_session = self.validate_contract_decision(
            run_id,
            key=key,
            action_id=action["action_id"],
            facet="assurance",
            facet_digest=ledger["digests"]["assurance"],
            contract=replacement,
            session_id="different-session",
        )
        self.assertNotEqual(rc, 0, wrong_session)
        self.assertEqual(wrong_session.get("code"), "DECISION_SESSION_MISMATCH")

        rc, validated = self.validate_contract_decision(
            run_id,
            key=key,
            action_id=action["action_id"],
            facet="assurance",
            facet_digest=ledger["digests"]["assurance"],
            contract=replacement,
        )
        self.assertEqual(rc, 0, validated)
        self.assertEqual(validated.get("apply", {}).get("command"), "update-facet")

    def test_decision_mutation_rechecks_live_driver_action_under_repo_lock(self) -> None:
        run_id = "stale-decision-mutation"
        _data, run_path = self.start(run_id)
        key = "enable-review-preflight"
        self.record_plan_problem(
            run_id,
            key=key,
            decision_request={
                "kind": "facet_change",
                "facet": "assurance",
                "changes": [
                    {
                        "pointer": "/reviewer_preflight",
                        "operation": "replace",
                        "value": True,
                    }
                ],
                "question": "Enable Reviewer preflight?",
            },
        )
        rc, action, _stdout, _stderr = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, action)
        ledger = self.load_ledger(run_path)
        replacement = deepcopy(ledger["facets"]["assurance"])
        replacement["reviewer_preflight"] = True
        (self.repo / "README.md").write_text(
            "fixture\ntarget moved after decision validation\n", encoding="utf-8"
        )
        commit_all(self.repo, "move target after decision validation")

        before = (run_path / "ledger.json").read_bytes()
        rc, rejected = self.update_facet(
            run_id,
            "assurance",
            replacement,
            "--resolve-plan-problem-key",
            key,
            "--decision-action-id",
            action["action_id"],
            "--expected-facet-digest",
            ledger["digests"]["assurance"],
            "--session-id",
            "assurance-v4-test-session",
        )
        self.assertNotEqual(rc, 0, rejected)
        self.assertEqual(rejected.get("code"), "DECISION_ACTION_STALE", rejected)
        self.assertEqual((run_path / "ledger.json").read_bytes(), before)

    def test_validate_decision_revises_mission_and_resolves_problem_atomically(self) -> None:
        run_id = "validated-mission-decision"
        _data, run_path = self.start(run_id)
        key = "change-objective"
        objective = "Deliver the revised calculator behavior."
        self.record_plan_problem(
            run_id,
            key=key,
            decision_request={
                "kind": "facet_change",
                "facet": "mission",
                "changes": [
                    {
                        "pointer": "/objective",
                        "operation": "replace",
                        "value": objective,
                    }
                ],
                "question": "Approve the revised objective?",
            },
        )
        rc, action, _stdout, _stderr = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, action)
        ledger = self.load_ledger(run_path)
        replacement = deepcopy(ledger["facets"])
        replacement["mission"]["revision"] += 1
        replacement["mission"]["objective"] = objective
        replacement["mission"]["supersedes"] = {
            "run_id": run_id,
            "revision": ledger["facets"]["mission"]["revision"],
            "mission_digest": ledger["digests"]["mission"],
            "candidate_head": ledger["facets"]["execution"]["candidate_head"],
        }
        replacement["execution"]["revision_transition"] = {
            "category": "mission_change",
            "predecessor_pressure_digest": action.get("lineage", {}).get(
                "pressure_digest"
            ),
            "architecture_review": None,
        }
        if replacement["execution"]["revision_transition"][
            "predecessor_pressure_digest"
        ] is None:
            rc, current_status, _stdout, _stderr = self.invoke(
                "status", "--repo", self.repo, "--run", run_id
            )
            self.assertEqual(rc, 0, current_status)
            replacement["execution"]["revision_transition"][
                "predecessor_pressure_digest"
            ] = current_status["lineage"]["pressure_digest"]

        rc, validated = self.validate_contract_decision(
            run_id,
            key=key,
            action_id=action["action_id"],
            facet="mission",
            facet_digest=ledger["digests"]["mission"],
            contract=replacement,
        )
        self.assertEqual(rc, 0, validated)
        self.assertEqual(validated.get("apply", {}).get("command"), "revise-mission")

        mission_path = self.write_json("validated-mission.json", replacement["mission"])
        transition_path = self.write_json(
            "validated-mission-transition.json",
            replacement["execution"]["revision_transition"],
        )
        rc, applied, _stdout, _stderr = self.invoke(
            "revise-mission",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--mission",
            mission_path,
            "--transition",
            transition_path,
            "--resolve-plan-problem-key",
            key,
            "--decision-action-id",
            action["action_id"],
            "--expected-facet-digest",
            ledger["digests"]["mission"],
            "--session-id",
            "assurance-v4-test-session",
        )
        self.assertEqual(rc, 0, applied)
        revised = self.load_ledger(run_path)
        self.assertEqual(revised["facets"]["mission"], replacement["mission"])
        self.assertEqual(revised["problems"][0]["status"], "resolved")
        self.assertEqual(
            revised["problems"][0]["resolution"],
            f"plan-decision:mission:{revised['digests']['mission']}",
        )

    def test_plan_problem_decision_updates_facet_and_resolves_only_the_bound_key_atomically(self) -> None:
        run_id = "plan-decision-atomic"
        _data, run_path = self.start(run_id)
        report = {
            "schema_version": 1,
            "problems": [
                {
                    "key": "expand-builder-authority",
                    "summary": "Builder authority must include generated sources.",
                    "details": "The user must explicitly authorize the exact additional path.",
                    "owner": "plan",
                },
                {
                    "key": "choose-release-policy",
                    "summary": "Release policy still needs a separate decision.",
                    "details": "This decision is independent from source ownership.",
                    "owner": "plan",
                },
            ],
        }
        report_path = self.write_json("plan-decision-atomic-problems.json", report)
        rc, recorded, _stdout, _stderr = self.invoke(
            "record-problems",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--report",
            report_path,
            "--role",
            "builder",
            "--agent-id",
            "plan-decision-builder",
            "--thread-id",
            "plan-decision-builder-thread",
        )
        self.assertEqual(rc, 0, recorded)
        original = self.load_ledger(run_path)
        expanded = deepcopy(original["facets"]["authority"])
        expanded["builder_write"].append("generated/**")
        before = (run_path / "ledger.json").read_bytes()

        rc, rejected = self.update_facet(
            run_id,
            "authority",
            expanded,
            "--resolve-plan-problem-key",
            "expand-builder-authority",
        )

        self.assertNotEqual(rc, 0, rejected)
        self.assertEqual(
            rejected.get("code"), "AUTHORITY_EXPANSION_REQUIRES_USER", rejected
        )
        self.assertEqual((run_path / "ledger.json").read_bytes(), before)

        rc, applied = self.update_facet(
            run_id,
            "authority",
            expanded,
            "--authorize-expansion",
            "--resolve-plan-problem-key",
            "expand-builder-authority",
        )

        self.assertEqual(rc, 0, applied)
        ledger = self.load_ledger(run_path)
        self.assertEqual(ledger["facets"]["authority"], expanded)
        digest_after = ledger["digests"]["authority"]
        by_key = {item["key"]: item for item in ledger["problems"]}
        self.assertEqual(by_key["expand-builder-authority"]["status"], "resolved")
        self.assertEqual(
            by_key["expand-builder-authority"]["resolution"],
            f"plan-decision:authority:{digest_after}",
        )
        self.assertEqual(by_key["choose-release-policy"]["status"], "open")
        event = next(
            item
            for item in reversed(ledger["events"])
            if item.get("kind") == "plan_problem_decision_applied"
        )
        self.assertEqual(
            event["details"],
            {
                "key": "expand-builder-authority",
                "facet": "authority",
                "old_digest": original["digests"]["authority"],
                "new_digest": digest_after,
                "facet_changed": True,
            },
        )
        rc, decision, _stdout, _stderr = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, decision)
        self.assertEqual(decision.get("action"), "contract_decision", decision)
        self.assertEqual(decision.get("problem", {}).get("key"), "choose-release-policy")

    def test_plan_problem_decision_recovery_ambiguity_and_conflicts_are_fail_closed(self) -> None:
        def record(
            run_id: str,
            name: str,
            *,
            key: str,
            owner: str = "plan",
            role: str = "builder",
            agent_id: str = "plan-recovery-builder",
            thread_id: str = "plan-recovery-builder-thread",
            details: str = "The frozen decision requires an atomic update.",
        ) -> None:
            path = self.write_json(
                name,
                {
                    "schema_version": 1,
                    "problems": [
                        {
                            "key": key,
                            "summary": "Apply one frozen plan decision.",
                            "details": details,
                            "owner": owner,
                        }
                    ],
                },
            )
            rc, value, _stdout, _stderr = self.invoke(
                "record-problems",
                "--repo",
                self.repo,
                "--run",
                run_id,
                "--report",
                path,
                "--role",
                role,
                "--agent-id",
                agent_id,
                "--thread-id",
                thread_id,
            )
            self.assertEqual(rc, 0, value)

        run_id = "plan-decision-recovery"
        _data, run_path = self.start(run_id)
        record(run_id, "plan-recovery-problem.json", key="recover-authority")
        ledger = self.load_ledger(run_path)
        expanded = deepcopy(ledger["facets"]["authority"])
        expanded["builder_write"].append("generated/**")
        rc, updated = self.update_facet(
            run_id, "authority", expanded, "--authorize-expansion"
        )
        self.assertEqual(rc, 0, updated)
        open_after_update = self.load_ledger(run_path)["problems"][0]
        self.assertEqual(open_after_update["status"], "open")

        rc, closed = self.update_facet(
            run_id,
            "authority",
            expanded,
            "--authorize-expansion",
            "--resolve-plan-problem-key",
            "recover-authority",
        )
        self.assertEqual(rc, 0, closed)
        closed_ledger = self.load_ledger(run_path)
        self.assertEqual(closed_ledger["problems"][0]["status"], "resolved")
        applied_event = next(
            item
            for item in reversed(closed_ledger["events"])
            if item.get("kind") == "plan_problem_decision_applied"
        )
        self.assertIs(applied_event["details"]["facet_changed"], False)

        before_replay = (run_path / "ledger.json").read_bytes()
        rc, replay = self.update_facet(
            run_id,
            "authority",
            expanded,
            "--authorize-expansion",
            "--resolve-plan-problem-key",
            "recover-authority",
        )
        self.assertEqual(rc, 0, replay)
        self.assertEqual((run_path / "ledger.json").read_bytes(), before_replay)

        conflicting = deepcopy(expanded)
        conflicting["builder_write"].append("another-generated/**")
        before_conflict = (run_path / "ledger.json").read_bytes()
        rc, conflict = self.update_facet(
            run_id,
            "authority",
            conflicting,
            "--authorize-expansion",
            "--resolve-plan-problem-key",
            "recover-authority",
        )
        self.assertNotEqual(rc, 0, conflict)
        self.assertEqual(
            conflict.get("code"), "PLAN_PROBLEM_DECISION_CONFLICT", conflict
        )
        self.assertEqual((run_path / "ledger.json").read_bytes(), before_conflict)

        assurance = deepcopy(closed_ledger["facets"]["assurance"])
        assurance["required"].append("doc_review")
        before_facet_conflict = (run_path / "ledger.json").read_bytes()
        rc, facet_conflict = self.update_facet(
            run_id,
            "assurance",
            assurance,
            "--resolve-plan-problem-key",
            "recover-authority",
        )
        self.assertNotEqual(rc, 0, facet_conflict)
        self.assertEqual(
            facet_conflict.get("code"),
            "PLAN_PROBLEM_DECISION_CONFLICT",
            facet_conflict,
        )
        self.assertEqual(
            (run_path / "ledger.json").read_bytes(), before_facet_conflict
        )

        ambiguous_run = "plan-decision-ambiguous"
        _data, ambiguous_path = self.start(ambiguous_run)
        record(ambiguous_run, "plan-ambiguous-builder.json", key="duplicate-plan-key")
        record(
            ambiguous_run,
            "plan-ambiguous-reviewer.json",
            key="duplicate-plan-key",
            role="reviewer",
            agent_id="assurance-v4-reviewer",
            thread_id="assurance-v4-reviewer-thread",
            details="A second producer reported the same unresolved plan key.",
        )
        ambiguous_authority = deepcopy(
            self.load_ledger(ambiguous_path)["facets"]["authority"]
        )
        before_ambiguous = (ambiguous_path / "ledger.json").read_bytes()
        rc, ambiguous = self.update_facet(
            ambiguous_run,
            "authority",
            ambiguous_authority,
            "--resolve-plan-problem-key",
            "duplicate-plan-key",
        )
        self.assertNotEqual(rc, 0, ambiguous)
        self.assertEqual(ambiguous.get("code"), "PLAN_PROBLEM_AMBIGUOUS", ambiguous)
        self.assertEqual(
            (ambiguous_path / "ledger.json").read_bytes(), before_ambiguous
        )

        missing_run = "plan-decision-missing"
        _data, missing_path = self.start(missing_run)
        record(
            missing_run,
            "plan-non-plan-problem.json",
            key="builder-owned-key",
            owner="builder",
        )
        unchanged_authority = self.load_ledger(missing_path)["facets"]["authority"]
        for key in ("missing-plan-key", "builder-owned-key"):
            before_missing = (missing_path / "ledger.json").read_bytes()
            rc, missing = self.update_facet(
                missing_run,
                "authority",
                unchanged_authority,
                "--resolve-plan-problem-key",
                key,
            )
            self.assertNotEqual(rc, 0, missing)
            self.assertEqual(missing.get("code"), "PLAN_PROBLEM_NOT_FOUND", missing)
            self.assertEqual(
                (missing_path / "ledger.json").read_bytes(), before_missing
            )

        execution_run = "plan-decision-execution-rejected"
        _data, execution_path = self.start(execution_run)
        record(
            execution_run,
            "plan-execution-problem.json",
            key="execution-decision",
        )
        execution = self.load_ledger(execution_path)["facets"]["execution"]
        before_execution = (execution_path / "ledger.json").read_bytes()
        rc, rejected_execution = self.update_facet(
            execution_run,
            "execution",
            execution,
            "--resolve-plan-problem-key",
            "execution-decision",
        )
        self.assertNotEqual(rc, 0, rejected_execution)
        self.assertNotEqual(rejected_execution.get("code"), "CLI_USAGE_ERROR")
        self.assertEqual(
            (execution_path / "ledger.json").read_bytes(), before_execution
        )

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

    def test_v4_runtime_identity_is_frozen_and_legacy_missing_values_normalize_read_only(self) -> None:
        runtime_fields = {
            "adapter",
            "adapter_commit",
            "adapter_dirty",
            "capture_status",
        }
        for index, driver_kind in enumerate(("native", "full_driver_skill")):
            run_id = f"runtime-identity-{driver_kind.replace('_', '-')}"
            contract_path = self.write_json(
                f"{run_id}-contract.json", contract_for(self.repo)
            )
            rc, started, _stdout, _stderr = self.invoke(
                "start",
                "--repo",
                self.repo,
                "--run",
                run_id,
                "--session-id",
                f"runtime-identity-session-{index}",
                "--contract",
                contract_path,
                "--driver-kind",
                driver_kind,
                "--driver-transport",
                "codex_app_server" if driver_kind == "native" else "native_tools",
                "--driver-runtime-version",
                "fixture-runtime",
                "--driver-protocol-schema-digest",
                "a" * 64,
            )
            self.assertEqual(rc, 0, started)
            run_path = Path(started["candidate_worktree"]).parent
            ledger = self.load_ledger(run_path)
            identity = ledger.get("runtime_identity")
            self.assertIsInstance(identity, dict, ledger)
            self.assertEqual(set(identity), runtime_fields)
            self.assertIn(identity["capture_status"], {"captured", "partial", "unavailable"})
            self.assertEqual(started.get("runtime_identity"), identity)

        legacy_run = "runtime-identity-legacy"
        _started, run_path = self.start(legacy_run)
        ledger_path = run_path / "ledger.json"
        original = self.load_ledger(run_path)
        for label, raw_value, expected_status in (
            ("missing", object(), "legacy-unavailable"),
            ("null", None, "unavailable"),
        ):
            with self.subTest(label=label):
                raw = deepcopy(original)
                if label == "missing":
                    raw.pop("runtime_identity", None)
                else:
                    raw["runtime_identity"] = raw_value
                ledger_path.write_text(
                    json.dumps(raw, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8",
                )
                before = ledger_path.read_bytes()
                rc, status_value, _stdout, _stderr = self.invoke(
                    "status", "--repo", self.repo, "--run", legacy_run
                )
                self.assertEqual(rc, 0, status_value)
                normalized = status_value.get("runtime_identity")
                self.assertIsInstance(normalized, dict, status_value)
                self.assertEqual(normalized.get("capture_status"), expected_status)
                self.assertEqual(ledger_path.read_bytes(), before)

        ledger_path.write_text(
            json.dumps(original, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    def test_v4_runtime_identity_captures_dirty_and_unavailable_git_truthfully(self) -> None:
        marker = Path(__file__).resolve().parent / ".runtime-identity-dirty-marker"
        marker.write_text("dirty during identity capture\n", encoding="utf-8")
        try:
            _started, run_path = self.start("runtime-identity-dirty")
            identity = self.load_ledger(run_path)["runtime_identity"]
            self.assertEqual(identity["adapter_dirty"], True)
            self.assertEqual(identity["capture_status"], "captured")
        finally:
            marker.unlink(missing_ok=True)

        with tempfile.TemporaryDirectory(prefix="runtime-identity-unavailable-") as raw:
            copied = Path(raw) / "codex_builder_loop"
            shutil.copytree(
                Path(__file__).resolve().parents[1]
                / "runtime"
                / "codex_builder_loop",
                copied,
            )
            completed = run_process(
                [
                    sys.executable,
                    "-c",
                    (
                        "import json, sys; "
                        f"sys.path.insert(0, {raw!r}); "
                        "from codex_builder_loop.core import capture_runtime_identity; "
                        "print(json.dumps(capture_runtime_identity(), sort_keys=True))"
                    ),
                ],
                cwd=raw,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            unavailable = json.loads(completed.stdout.splitlines()[-1])
            self.assertEqual(unavailable["adapter"], "codex")
            self.assertIsNone(unavailable["adapter_commit"])
            self.assertIsNone(unavailable["adapter_dirty"])
            self.assertEqual(unavailable["capture_status"], "unavailable")

    def test_legacy_v4_ledger_normalizes_new_optional_reliability_fields_read_only(self) -> None:
        run_id = "legacy-reliability-fields"
        _started, run_path = self.start(run_id)
        ledger_path = run_path / "ledger.json"
        legacy = self.load_ledger(run_path)
        legacy.pop("machine_failure", None)
        legacy.pop("recomposition_intent", None)
        legacy["publication"].pop("generation", None)
        legacy["facets"]["assurance"].pop("preflight_before_proof", None)
        legacy["facets"]["assurance"].pop("reviewer_preflight", None)
        legacy["digests"] = {
            facet: canonical_digest(legacy["facets"][facet])
            for facet in ("mission", "authority", "assurance", "execution")
        }
        ledger_path.write_text(
            json.dumps(legacy, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        before = ledger_path.read_bytes()

        rc, current, _stdout, _stderr = self.invoke(
            "status", "--repo", self.repo, "--run", run_id
        )

        self.assertEqual(rc, 0, current)
        self.assertIsNone(current["machine_failure"])
        self.assertIsNone(current["recomposition_intent"])
        self.assertEqual(current["publication"]["generation"], 0)
        self.assertEqual(ledger_path.read_bytes(), before)

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
            {"machine", "blackbox", "doc_review", "doc_reference_scan"},
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
            {
                "machine",
                "tester",
                "blackbox",
                "reviewer",
                "doc_review",
                "doc_reference_scan",
            },
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

    def test_tester_evidence_manifest_mismatch_is_rejected(self) -> None:
        run_id = "tester-evidence-manifest-mismatch"
        _data, run_path = self.start(run_id)
        self.prepare_tester_source(run_id, run_path)
        before = self.load_ledger(run_path)
        execution_before = deepcopy(before["facets"]["execution"])
        source = execution_before["tester_source"]
        candidate = Path(before["candidate_worktree"])
        candidate_head_before = head(candidate)
        target_head_before = head(self.repo)
        target_start_head_before = before["target_start_head"]
        evidence_before = deepcopy(before["evidence"])

        extra_path = "tests/test_calc.py"
        self.assertNotIn(extra_path, {item["path"] for item in source["files"]})
        extra_blob = git(self.repo, "rev-parse", f"{source['head']}:{extra_path}")
        self.assertEqual(
            extra_blob,
            git(
                self.repo,
                "rev-parse",
                f"{execution_before['candidate_head']}:{extra_path}",
            ),
        )
        report = {
            "schema_version": 1,
            "kind": "tester",
            "status": "pass",
            "candidate_head": execution_before["candidate_head"],
            "producer": {
                "role": "tester",
                **execution_before["agents"]["tester"],
            },
            "details": {
                "result": "tests_ready",
                "source_head": source["head"],
                "files": [
                    *source["files"],
                    {"path": extra_path, "blob": extra_blob},
                ],
            },
        }
        report_path = self.write_json("tester-extra-manifest-path.json", report)

        rc, rejected, _stdout, _stderr = self.invoke(
            "record-evidence",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--kind",
            "tester",
            "--report",
            report_path,
        )

        self.assertNotEqual(rc, 0, rejected)
        self.assertEqual(
            rejected.get("code"), "TESTER_SOURCE_MANIFEST_MISMATCH", rejected
        )
        after = self.load_ledger(run_path)
        self.assertNotIn("tester", after["evidence"])
        self.assertEqual(after["evidence"], evidence_before)
        self.assertEqual(after["facets"]["execution"], execution_before)
        self.assertEqual(after["target_start_head"], target_start_head_before)
        self.assertEqual(head(candidate), candidate_head_before)
        self.assertEqual(head(self.repo), target_head_before)

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
                "cases": blackbox_case_results(ledger),
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
        for kind in ("machine", "tester", "blackbox", "reviewer"):
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
                "cases": blackbox_case_results(ledger),
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
                "cases": blackbox_case_results(ledger),
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
        machine_events = [
            item for item in ledger["events"] if item.get("kind") == "machine_verified"
        ]
        self.assertEqual(len(machine_events), 1)
        self.assertGreaterEqual(machine_events[0]["details"]["duration_ms"], 0)
        machine_stage = next(
            item for item in verified["telemetry"]["stages"] if item["name"] == "verify_machine"
        )
        self.assertEqual(machine_stage["attempts"], 1)
        self.assertEqual(
            machine_stage["total_duration_ms"],
            machine_events[0]["details"]["duration_ms"],
        )

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

    def test_open_external_problem_requires_user_recovery_instead_of_builder_fix(
        self,
    ) -> None:
        run_id = "external-problem-needs-user"
        _data, run_path = self.start(run_id)
        report_path = self.write_json(
            "external-problem-needs-user.json",
            {
                "schema_version": 1,
                "problems": [
                    {
                        "key": "external-probe-unavailable",
                        "summary": "The external probe is unavailable.",
                        "details": "A fresh environment probe must succeed before assurance can resume.",
                        "owner": "external_platform",
                    }
                ],
            },
        )
        rc, recorded, _stdout, _stderr = self.invoke(
            "record-problems",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--report",
            report_path,
            "--role",
            "builder",
            "--agent-id",
            "external-problem-builder",
            "--thread-id",
            "external-problem-builder-thread",
        )
        self.assertEqual(rc, 0, recorded)
        before = (run_path / "ledger.json").read_bytes()

        rc, decision, _stdout, _stderr = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )

        self.assertIn(rc, (0, 1), decision)
        self.assertEqual(decision.get("status"), "NEEDS_USER", decision)
        self.assertNotEqual(decision.get("action"), "builder_fix", decision)
        self.assertEqual(
            decision.get("problem", {}).get("key"),
            "external-probe-unavailable",
        )
        self.assertEqual((run_path / "ledger.json").read_bytes(), before)

    def test_external_problem_resolution_is_atomic_idempotent_and_reenters_machine_gate(
        self,
    ) -> None:
        run_id = "external-problem-resolution"
        external_ready = self.artifacts / "external-ready"
        contract = contract_for(
            self.repo,
            machine_argv=[
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).read_text()",
                str(external_ready),
            ],
        )
        data, run_path = self.start(run_id, contract=contract)
        candidate = Path(data["candidate_worktree"])
        self.commit_candidate_change(run_id, run_path, candidate)
        self.record_role_evidence(run_id, run_path, "tester")
        rc, failed, _stdout, _stderr = self.invoke(
            "verify-machine", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, failed)
        failed_ledger = self.load_ledger(run_path)
        self.assertEqual(failed_ledger["evidence"]["machine"]["status"], "fail")
        machine_failure = next(
            event
            for event in failed_ledger["events"]
            if event.get("kind") == "machine_verified"
            and event.get("details", {}).get("status") == "fail"
        )
        failed_ledger["events"].extend(
            [deepcopy(machine_failure), deepcopy(machine_failure)]
        )
        (run_path / "ledger.json").write_text(
            json.dumps(failed_ledger, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        report_path = self.write_json(
            "external-problem-resolution.json",
            {
                "schema_version": 1,
                "problems": [
                    {
                        "key": "external-network-restored",
                        "summary": "The external network probe failed.",
                        "details": "The user authorized a fresh probe after the environment recovered.",
                        "owner": "external_platform",
                    }
                ],
            },
        )
        rc, recorded, _stdout, _stderr = self.invoke(
            "record-problems",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--report",
            report_path,
            "--role",
            "builder",
            "--agent-id",
            "external-resolution-builder",
            "--thread-id",
            "external-resolution-builder-thread",
        )
        self.assertEqual(rc, 0, recorded)
        rc, blocked, _stdout, _stderr = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertIn(rc, (0, 1), blocked)
        self.assertEqual(blocked.get("status"), "NEEDS_USER", blocked)

        before = self.load_ledger(run_path)
        ledger_bytes_before = (run_path / "ledger.json").read_bytes()
        target_before = head(self.repo)
        candidate_before = head(candidate)
        candidate_ref_before = git(
            self.repo, "rev-parse", before["candidate_branch"]
        )
        worktrees_before = git(self.repo, "worktree", "list", "--porcelain")
        evidence_before = deepcopy(before["evidence"])
        reason = "User authorized continuation after a fresh external probe succeeded."

        rc, resolved, _stdout, _stderr = self.invoke(
            "resolve-external-problem",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--problem-key",
            "external-network-restored",
            "--reason",
            reason,
        )

        self.assertEqual(rc, 0, resolved)
        after = self.load_ledger(run_path)
        problem = next(
            item
            for item in after["problems"]
            if item["key"] == "external-network-restored"
        )
        self.assertEqual(problem["status"], "resolved")
        self.assertIn(reason, problem["resolution"])
        events = after["events"][len(before["events"]) :]
        self.assertEqual(len(events), 1, after["events"])
        event_payload = json.dumps(events[0], ensure_ascii=False, sort_keys=True)
        self.assertIn("external-network-restored", event_payload)
        self.assertIn(reason, event_payload)
        self.assertEqual(after["evidence"], evidence_before)
        self.assertEqual(after["evidence"]["machine"]["status"], "fail")
        self.assertEqual(head(self.repo), target_before)
        self.assertEqual(head(candidate), candidate_before)
        self.assertEqual(
            git(self.repo, "rev-parse", after["candidate_branch"]),
            candidate_ref_before,
        )
        self.assertEqual(
            git(self.repo, "worktree", "list", "--porcelain"),
            worktrees_before,
        )
        self.assertNotEqual(
            (run_path / "ledger.json").read_bytes(), ledger_bytes_before
        )

        resolved_bytes = (run_path / "ledger.json").read_bytes()
        rc, replayed, _stdout, _stderr = self.invoke(
            "resolve-external-problem",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--problem-key",
            "external-network-restored",
            "--reason",
            reason,
        )
        self.assertEqual(rc, 0, replayed)
        self.assertEqual((run_path / "ledger.json").read_bytes(), resolved_bytes)

        rc, conflict, _stdout, _stderr = self.invoke(
            "resolve-external-problem",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--problem-key",
            "external-network-restored",
            "--reason",
            "A conflicting replay reason.",
        )
        self.assertNotEqual(rc, 0, conflict)
        self.assertEqual((run_path / "ledger.json").read_bytes(), resolved_bytes)

        rc, decision, _stdout, _stderr = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, decision)
        self.assertEqual(decision.get("action"), "verify_machine", decision)

        external_ready.write_text("ready\n", encoding="utf-8")
        rc, verified, _stdout, _stderr = self.invoke(
            "verify-machine", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, verified)
        self.assertEqual(
            verified.get("readiness", {}).get("states", {}).get("machine"),
            "pass",
            verified,
        )
        rc, after_recovery, _stdout, _stderr = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, after_recovery)
        self.assertEqual(
            after_recovery.get("action"), "tester_blackbox", after_recovery
        )

    def test_external_recovery_does_not_hide_a_fresh_machine_failure(self) -> None:
        run_id = "external-problem-recovery-fails"
        contract = contract_for(
            self.repo,
            machine_argv=[sys.executable, "-c", "raise SystemExit(9)"],
        )
        data, run_path = self.start(run_id, contract=contract)
        candidate = Path(data["candidate_worktree"])
        self.commit_candidate_change(run_id, run_path, candidate)
        self.record_role_evidence(run_id, run_path, "tester")
        rc, failed, _stdout, _stderr = self.invoke(
            "verify-machine", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, failed)
        failed_ledger = self.load_ledger(run_path)
        machine_failure = next(
            event
            for event in failed_ledger["events"]
            if event.get("kind") == "machine_verified"
            and event.get("details", {}).get("status") == "fail"
        )
        failed_ledger["events"].extend(
            [deepcopy(machine_failure), deepcopy(machine_failure)]
        )
        (run_path / "ledger.json").write_text(
            json.dumps(failed_ledger, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        report_path = self.write_json(
            "external-problem-recovery-fails.json",
            {
                "schema_version": 1,
                "problems": [
                    {
                        "key": "external-still-unavailable",
                        "summary": "The external dependency appeared to recover.",
                        "details": "The next machine observation must decide whether recovery succeeded.",
                        "owner": "external_platform",
                    }
                ],
            },
        )
        rc, recorded, _stdout, _stderr = self.invoke(
            "record-problems",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--report",
            report_path,
            "--role",
            "builder",
            "--agent-id",
            "external-recovery-failure-builder",
            "--thread-id",
            "external-recovery-failure-builder-thread",
        )
        self.assertEqual(rc, 0, recorded)
        rc, resolved, _stdout, _stderr = self.invoke(
            "resolve-external-problem",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--problem-key",
            "external-still-unavailable",
            "--reason",
            "User authorized one fresh machine observation after the external probe recovered.",
        )
        self.assertEqual(rc, 0, resolved)
        rc, retry, _stdout, _stderr = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, retry)
        self.assertEqual(retry.get("action"), "verify_machine", retry)

        rc, fresh_failure, _stdout, _stderr = self.invoke(
            "verify-machine", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, fresh_failure)
        self.assertEqual(
            fresh_failure.get("readiness", {}).get("states", {}).get("machine"),
            "failed",
            fresh_failure,
        )
        rc, blocked, _stdout, _stderr = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, blocked)
        self.assertEqual(blocked.get("action"), "architecture_review", blocked)

    def test_external_recovery_only_bypasses_its_bound_machine_signature(self) -> None:
        run_id = "external-problem-signature-boundary"
        contract = contract_for(
            self.repo,
            machine_argv=[sys.executable, "-c", "raise SystemExit(9)"],
        )
        data, run_path = self.start(run_id, contract=contract)
        candidate = Path(data["candidate_worktree"])
        self.commit_candidate_change(run_id, run_path, candidate)
        self.record_role_evidence(run_id, run_path, "tester")
        rc, failed, _stdout, _stderr = self.invoke(
            "verify-machine", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, failed)
        failed_ledger = self.load_ledger(run_path)
        machine_index = next(
            index
            for index, event in enumerate(failed_ledger["events"])
            if event.get("kind") == "machine_verified"
            and event.get("details", {}).get("status") == "fail"
        )
        machine_failure = failed_ledger["events"][machine_index]
        bound_signature = machine_failure["details"]["failure_signature"]
        unrelated_signature = "f" * 64
        unrelated_failure = deepcopy(machine_failure)
        unrelated_failure["details"]["failure_signature"] = unrelated_signature
        failed_ledger["events"][machine_index:machine_index] = [
            deepcopy(unrelated_failure),
            deepcopy(unrelated_failure),
            deepcopy(unrelated_failure),
        ]
        failed_ledger["events"].extend(
            [deepcopy(machine_failure), deepcopy(machine_failure)]
        )
        (run_path / "ledger.json").write_text(
            json.dumps(failed_ledger, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        report_path = self.write_json(
            "external-problem-signature-boundary.json",
            {
                "schema_version": 1,
                "problems": [
                    {
                        "key": "external-bound-signature",
                        "summary": "One exact machine failure was attributed externally.",
                        "details": "An unrelated repeated machine failure must remain blocking.",
                        "owner": "external_platform",
                    }
                ],
            },
        )
        rc, recorded, _stdout, _stderr = self.invoke(
            "record-problems",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--report",
            report_path,
            "--role",
            "builder",
            "--agent-id",
            "external-signature-builder",
            "--thread-id",
            "external-signature-builder-thread",
        )
        self.assertEqual(rc, 0, recorded)
        rc, resolved, _stdout, _stderr = self.invoke(
            "resolve-external-problem",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--problem-key",
            "external-bound-signature",
            "--reason",
            "User authorized recovery for the currently bound external failure only.",
        )
        self.assertEqual(rc, 0, resolved)

        rc, blocked, _stdout, _stderr = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, blocked)
        self.assertEqual(blocked.get("action"), "architecture_review", blocked)
        signatures = {
            item["failure_signature"] for item in blocked.get("failures", [])
        }
        self.assertIn(unrelated_signature, signatures)
        self.assertNotIn(bound_signature, signatures)

    def test_external_problem_resolution_rejects_wrong_or_stale_problem_without_bypass(
        self,
    ) -> None:
        run_id = "external-problem-boundaries"
        _data, run_path = self.start(run_id)
        report_path = self.write_json(
            "external-problem-boundaries.json",
            {
                "schema_version": 1,
                "problems": [
                    {
                        "key": "external-only",
                        "summary": "An external dependency is unavailable.",
                        "details": "Only this exact problem may use external recovery.",
                        "owner": "external_platform",
                    },
                    {
                        "key": "builder-owned",
                        "summary": "Builder work remains.",
                        "details": "External recovery must not close Builder work.",
                        "owner": "builder",
                    },
                    {
                        "key": "tester-owned",
                        "summary": "Tester work remains.",
                        "details": "External recovery must not close Tester work.",
                        "owner": "tester",
                    },
                    {
                        "key": "plan-owned",
                        "summary": "A plan decision remains.",
                        "details": "External recovery must not close plan work.",
                        "owner": "plan",
                    },
                ],
            },
        )
        rc, recorded, _stdout, _stderr = self.invoke(
            "record-problems",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--report",
            report_path,
            "--role",
            "builder",
            "--agent-id",
            "external-boundary-builder",
            "--thread-id",
            "external-boundary-builder-thread",
        )
        self.assertEqual(rc, 0, recorded)
        reason = "User authorized recovery after the external dependency returned."
        rc, resolved, _stdout, _stderr = self.invoke(
            "resolve-external-problem",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--problem-key",
            "external-only",
            "--reason",
            reason,
        )
        self.assertEqual(rc, 0, resolved)
        statuses = {
            item["key"]: item["status"]
            for item in self.load_ledger(run_path)["problems"]
        }
        self.assertEqual(statuses["external-only"], "resolved")
        for key in ("builder-owned", "tester-owned", "plan-owned"):
            self.assertEqual(statuses[key], "open")
            before = (run_path / "ledger.json").read_bytes()
            rc, rejected, _stdout, _stderr = self.invoke(
                "resolve-external-problem",
                "--repo",
                self.repo,
                "--run",
                run_id,
                "--problem-key",
                key,
                "--reason",
                reason,
            )
            self.assertNotEqual(rc, 0, rejected)
            self.assertEqual((run_path / "ledger.json").read_bytes(), before)

        before_missing = (run_path / "ledger.json").read_bytes()
        rc, missing, _stdout, _stderr = self.invoke(
            "resolve-external-problem",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--problem-key",
            "missing-external-problem",
            "--reason",
            reason,
        )
        self.assertNotEqual(rc, 0, missing)
        self.assertEqual((run_path / "ledger.json").read_bytes(), before_missing)

        stale_run = "external-problem-stale"
        stale_data, stale_path = self.start(stale_run)
        stale_report = self.write_json(
            "external-problem-stale.json",
            {
                "schema_version": 1,
                "problems": [
                    {
                        "key": "stale-external",
                        "summary": "The external problem belongs to an older candidate.",
                        "details": "Changing the candidate must make recovery stale.",
                        "owner": "external_platform",
                    }
                ],
            },
        )
        rc, recorded, _stdout, _stderr = self.invoke(
            "record-problems",
            "--repo",
            self.repo,
            "--run",
            stale_run,
            "--report",
            stale_report,
            "--role",
            "builder",
            "--agent-id",
            "stale-external-builder",
            "--thread-id",
            "stale-external-builder-thread",
        )
        self.assertEqual(rc, 0, recorded)
        self.commit_candidate_change(
            stale_run, stale_path, Path(stale_data["candidate_worktree"])
        )
        before_stale = (stale_path / "ledger.json").read_bytes()
        rc, stale, _stdout, _stderr = self.invoke(
            "resolve-external-problem",
            "--repo",
            self.repo,
            "--run",
            stale_run,
            "--problem-key",
            "stale-external",
            "--reason",
            reason,
        )
        self.assertNotEqual(rc, 0, stale)
        self.assertEqual((stale_path / "ledger.json").read_bytes(), before_stale)

        terminal_run = "external-problem-terminal"
        _terminal_data, terminal_path = self.start(terminal_run)
        terminal_report = self.write_json(
            "external-problem-terminal.json",
            {
                "schema_version": 1,
                "problems": [
                    {
                        "key": "terminal-external",
                        "summary": "The run is already terminal.",
                        "details": "Terminal delivery facts cannot be reopened by recovery.",
                        "owner": "external_platform",
                    }
                ],
            },
        )
        rc, recorded, _stdout, _stderr = self.invoke(
            "record-problems",
            "--repo",
            self.repo,
            "--run",
            terminal_run,
            "--report",
            terminal_report,
            "--role",
            "builder",
            "--agent-id",
            "terminal-external-builder",
            "--thread-id",
            "terminal-external-builder-thread",
        )
        self.assertEqual(rc, 0, recorded)
        rc, abandoned, _stdout, _stderr = self.invoke(
            "abandon",
            "--repo",
            self.repo,
            "--run",
            terminal_run,
            "--reason",
            "terminal recovery rejection fixture",
        )
        self.assertEqual(rc, 0, abandoned)
        before_terminal = (terminal_path / "ledger.json").read_bytes()
        rc, terminal, _stdout, _stderr = self.invoke(
            "resolve-external-problem",
            "--repo",
            self.repo,
            "--run",
            terminal_run,
            "--problem-key",
            "terminal-external",
            "--reason",
            reason,
        )
        self.assertNotEqual(rc, 0, terminal)
        self.assertEqual(
            (terminal_path / "ledger.json").read_bytes(), before_terminal
        )

        ambiguous_run = "external-problem-ambiguous"
        _ambiguous_data, ambiguous_path = self.start(ambiguous_run)
        duplicate_report = {
            "schema_version": 1,
            "problems": [
                {
                    "key": "duplicate-external",
                    "summary": "The same external key has two producers.",
                    "details": "Ambiguous recovery must not choose one producer.",
                    "owner": "external_platform",
                }
            ],
        }
        report_path = self.write_json(
            "duplicate-external-builder.json", duplicate_report
        )
        rc, recorded, _stdout, _stderr = self.invoke(
            "record-problems",
            "--repo",
            self.repo,
            "--run",
            ambiguous_run,
            "--report",
            report_path,
            "--role",
            "builder",
            "--agent-id",
            "duplicate-builder",
            "--thread-id",
            "duplicate-builder-thread",
        )
        self.assertEqual(rc, 0, recorded)
        ambiguous_ledger = self.load_ledger(ambiguous_path)
        duplicate_problem = deepcopy(ambiguous_ledger["problems"][0])
        duplicate_problem["producer"] = {
            "role": "reviewer",
            "agent_id": "retained-reviewer",
            "thread_id": "retained-reviewer-thread",
        }
        ambiguous_ledger["problems"].append(duplicate_problem)
        (ambiguous_path / "ledger.json").write_text(
            json.dumps(ambiguous_ledger, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        before_ambiguous = (ambiguous_path / "ledger.json").read_bytes()
        rc, ambiguous, _stdout, _stderr = self.invoke(
            "resolve-external-problem",
            "--repo",
            self.repo,
            "--run",
            ambiguous_run,
            "--problem-key",
            "duplicate-external",
            "--reason",
            reason,
        )
        self.assertNotEqual(rc, 0, ambiguous)
        self.assertEqual(
            (ambiguous_path / "ledger.json").read_bytes(), before_ambiguous
        )

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

    def test_conflicting_target_drift_routes_to_builder_without_moving_canonical_heads(self) -> None:
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
        self.assertEqual(rc, 0, stopped)
        self.assertEqual(stopped["recomposition_intent"]["state"], "waiting_builder")
        self.assertEqual(stopped["recomposition_intent"]["conflict_owner"], "builder")
        self.assertEqual(stopped["recomposition_intent"]["conflict_paths"], ["src/calc.py"])
        self.assertEqual(head(self.repo), target_advanced)
        self.assertEqual(head(candidate), candidate_before)
        after = self.load_ledger(run_path)
        self.assertEqual(after["target_start_head"], ledger_before["target_start_head"])
        self.assertEqual(after["facets"]["execution"], ledger_before["facets"]["execution"])

    def test_builder_conflict_repair_completes_same_recomposition_transaction(self) -> None:
        run_id = "recomposition-builder-repair"
        data, run_path = self.start(run_id)
        candidate = Path(data["candidate_worktree"])
        self.commit_candidate_change(
            run_id,
            run_path,
            candidate,
            content="def add(a, b):\n    return a + b + 1\n",
        )
        (self.repo / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b - 1\n", encoding="utf-8"
        )
        target_advanced = commit_all(self.repo, "conflicting target advance")
        rc, waiting, _stdout, _stderr = self.invoke(
            "recompose-candidate", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, waiting)
        intent = waiting["recomposition_intent"]
        self.assertEqual(intent["state"], "waiting_builder")
        staging = Path(intent["builder_worktree"])
        (staging / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b + 2\n", encoding="utf-8"
        )
        git(staging, "add", "--", "src/calc.py")
        commit_all(staging, "resolve builder recomposition conflict")

        rc, completed, _stdout, _stderr = self.invoke(
            "recompose-candidate", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, completed)
        self.assertIsNone(completed.get("recomposition_intent"))
        after = self.load_ledger(run_path)
        self.assertEqual(after["target_start_head"], target_advanced)
        self.assertIn(
            "return a + b + 2",
            (candidate / "src" / "calc.py").read_text(encoding="utf-8"),
        )
        self.assertTrue(
            any(
                event.get("kind") == "recomposition_conflict_resolved"
                and event.get("details", {}).get("owner") == "builder"
                for event in after["events"]
            )
        )

    def test_tester_conflict_repair_completes_without_builder_owning_tests(self) -> None:
        run_id = "recomposition-tester-repair"
        data, run_path = self.start(run_id)
        candidate = Path(data["candidate_worktree"])
        self.prepare_tester_source(run_id, run_path)
        target_test = self.repo / "tests" / "test_assurance_fixture.py"
        target_test.write_text(
            "def test_target_version():\n    assert True\n", encoding="utf-8"
        )
        target_advanced = commit_all(self.repo, "conflicting tester target advance")

        rc, waiting, _stdout, _stderr = self.invoke(
            "recompose-candidate", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, waiting)
        intent = waiting["recomposition_intent"]
        self.assertEqual(intent["state"], "waiting_tester")
        self.assertEqual(intent["conflict_owner"], "tester")
        staging = Path(intent["tester_worktree"])
        (staging / "tests" / "test_assurance_fixture.py").write_text(
            "from src.calc import add\n\n"
            "def test_assurance_fixture():\n    assert add(2, 3) == 5\n\n"
            "def test_target_version():\n    assert True\n",
            encoding="utf-8",
        )
        git(staging, "add", "--", "tests/test_assurance_fixture.py")
        commit_all(staging, "resolve tester recomposition conflict")

        rc, completed, _stdout, _stderr = self.invoke(
            "recompose-candidate", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, completed)
        self.assertIsNone(completed.get("recomposition_intent"))
        after = self.load_ledger(run_path)
        self.assertEqual(after["target_start_head"], target_advanced)
        self.assertEqual(
            after["facets"]["execution"]["tester_files"],
            ["tests/test_assurance_fixture.py"],
        )
        self.assertNotIn(
            "tests/test_assurance_fixture.py",
            after["facets"]["execution"]["builder_files"],
        )
        self.assertIn(
            "test_target_version",
            (candidate / "tests" / "test_assurance_fixture.py").read_text(
                encoding="utf-8"
            ),
        )

    def test_target_advancing_mid_recomposition_restarts_on_latest_head(self) -> None:
        run_id = "recomposition-target-advances"
        run_path, _candidate, _before = self.prepare_publication_refresh(run_id)
        original = assurance_core._apply_recomposition_delta
        advanced_head: str | None = None
        advanced = False

        def advance_after_builder(*args: Any, **kwargs: Any):
            nonlocal advanced, advanced_head
            result = original(*args, **kwargs)
            if not advanced:
                advanced = True
                (self.repo / "README.md").write_text(
                    "fixture\nadvanced during recomposition\n", encoding="utf-8"
                )
                advanced_head = commit_all(
                    self.repo, "advance target during recomposition"
                )
            return result

        with patch.object(
            assurance_core,
            "_apply_recomposition_delta",
            side_effect=advance_after_builder,
        ):
            completed = assurance_core.recompose_candidate(self.repo, run_id)

        self.assertIsNone(completed.get("recomposition_intent"))
        after = self.load_ledger(run_path)
        self.assertEqual(after["target_start_head"], advanced_head)
        event = next(
            event
            for event in after["events"]
            if event.get("kind") == "prerequisites_republished"
        )
        self.assertEqual(event["details"]["target_restart_count"], 1)

    def test_status_lists_other_active_runs_on_the_same_target(self) -> None:
        first_run = "target-contender-first"
        second_run = "target-contender-second"
        self.start(first_run)
        self.start(second_run)

        rc, current, _stdout, _stderr = self.invoke(
            "status", "--repo", self.repo, "--run", first_run
        )
        self.assertEqual(rc, 0, current)
        self.assertEqual(
            [item["run_id"] for item in current["target_contenders"]],
            [second_run],
        )

    def test_recomposition_recovers_idempotently_after_each_staging_side_effect(self) -> None:
        for crash_after in (1, 2, 3):
            with self.subTest(crash_after=crash_after):
                run_id = f"recomposition-stage-crash-{crash_after}"
                run_path, _candidate, before = self.prepare_publication_refresh(run_id)
                original = assurance_core._apply_recomposition_delta
                calls = 0

                def crash_after_side_effect(*args: Any, **kwargs: Any):
                    nonlocal calls
                    result = original(*args, **kwargs)
                    calls += 1
                    if calls == crash_after:
                        raise RuntimeError("injected recomposition crash")
                    return result

                with patch.object(
                    assurance_core,
                    "_apply_recomposition_delta",
                    side_effect=crash_after_side_effect,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "injected recomposition crash"
                    ):
                        assurance_core.recompose_candidate(self.repo, run_id)

                interrupted = self.load_ledger(run_path)
                self.assertIsInstance(interrupted.get("recomposition_intent"), dict)
                recovered = assurance_core.recompose_candidate(self.repo, run_id)
                self.assertIsNone(recovered.get("recomposition_intent"))
                after = self.load_ledger(run_path)
                self.assertEqual(after["publication"]["generation"], 2)
                self.assertEqual(
                    sum(
                        1
                        for event in after["events"]
                        if event.get("kind") == "prerequisites_republished"
                    ),
                    1,
                )
                self.assertEqual(
                    len(after["retired_tester_sources"]),
                    len(before["retired_tester_sources"]) + 1,
                )

    def test_recomposition_recovers_after_formal_commit_before_intent_cleanup(self) -> None:
        run_id = "recomposition-final-commit-crash"
        run_path, _candidate, before = self.prepare_publication_refresh(run_id)
        original_cleanup = assurance_core._cleanup_recomposition_staging
        crashed = False

        def crash_after_cleanup(repo: Path, intent: Mapping[str, Any]) -> None:
            nonlocal crashed
            original_cleanup(repo, intent)
            if not crashed:
                crashed = True
                raise RuntimeError("injected cleanup crash")

        with patch.object(
            assurance_core,
            "_cleanup_recomposition_staging",
            side_effect=crash_after_cleanup,
        ):
            with self.assertRaisesRegex(RuntimeError, "injected cleanup crash"):
                assurance_core.recompose_candidate(self.repo, run_id)

        committed = self.load_ledger(run_path)
        self.assertIsInstance(committed.get("recomposition_intent"), dict)
        self.assertEqual(committed["publication"]["generation"], 2)
        version = committed["facets"]["execution"]["version"]
        retired = len(committed["retired_tester_sources"])

        recovered = assurance_core.recompose_candidate(self.repo, run_id)
        self.assertIsNone(recovered.get("recomposition_intent"))
        after = self.load_ledger(run_path)
        self.assertEqual(after["facets"]["execution"]["version"], version)
        self.assertEqual(len(after["retired_tester_sources"]), retired)
        self.assertEqual(retired, len(before["retired_tester_sources"]) + 1)
        self.assertEqual(
            sum(
                1
                for event in after["events"]
                if event.get("kind") == "prerequisites_republished"
            ),
            1,
        )

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

    def test_driver_failure_recovers_persisted_finalize_intent_before_sealing(self) -> None:
        run_id = "driver-failure-finalize-recovery"
        run_path, _candidate, _target_start = self.interrupt_finalize(
            run_id, after_cas=False
        )
        self.bind_native_runtime(run_path)
        failure_path = self.write_json(
            "driver-failure-finalize.json",
            {
                "source": "native_driver",
                "status": "FATAL",
                "code": "NATIVE_FINALIZE_FIXTURE_FATAL",
                "message": "Native Driver stopped after finalize intent persistence.",
                "details": {"fixture": run_id},
                "action": {
                    "action_id": "b" * 64,
                    "action": "finalize",
                    "reason": "all_gates_pass",
                },
            },
        )
        rc, recorded, _stdout, _stderr = self.invoke(
            "record-driver-failure",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--failure",
            failure_path,
            "--driver-runtime-kind",
            "native",
        )
        self.assertEqual(rc, 0, recorded)
        self.assertEqual(recorded["phase"], "finalizing")
        self.assertEqual(recorded["driver_failure"]["recovery"], "finalize")

        rc, decision, _stdout, _stderr = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, decision)
        self.assertEqual(decision["action"], "complete_driver_failure")

        rc, recovered, _stdout, _stderr = self.invoke(
            "complete-driver-failure",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--driver-runtime-kind",
            "native",
        )
        self.assertEqual(rc, 0, recovered)
        self.assertEqual(recovered["phase"], "finalized")
        self.assertEqual(recovered["driver_failure"]["state"], "recovered")
        self.assertEqual(head(self.repo), self.load_ledger(run_path)["final_head"])

        retrospective = self.invoke(
            "retrospective-status",
            "--repo",
            self.repo,
            "--session-id",
            "assurance-v4-test-session",
        )[1]
        self.assertEqual(retrospective["status"], "REQUIRED", retrospective)
        self.assertTrue(
            any(
                item["kind"] == "terminal-runtime-failure"
                and run_id in item["run_ids"]
                for item in retrospective["snapshot"]["signals"]
            ),
            retrospective,
        )

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
