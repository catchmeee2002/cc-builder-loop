from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness import (
    CLI,
    ROOT,
    add_v4_progress_contract,
    cleanup_repo,
    commit_all,
    init_repo,
    run_process,
)

sys.path.insert(0, str(ROOT / "runtime"))

from codex_builder_loop.assurance_v4 import core as assurance_core
from codex_builder_loop.assurance_v4 import driver as assurance_core_driver
from codex_builder_loop.native_driver.app_server import (
    AppServerError,
    AppServerTransport,
    INITIALIZE_TIMEOUT_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    TURN_IDLE_TIMEOUT_SECONDS,
    TurnResult,
    probe_app_server,
)
from codex_builder_loop.assurance_v4.models import digest, evidence_dependency, facet_digests
from codex_builder_loop.assurance_v4.driver_contract import AGENT_ACTION_CAPABILITIES
from codex_builder_loop.native_driver import cli as native_cli
from codex_builder_loop.native_driver.coordinator import NativeCoordinator, NativeDriverError
from codex_builder_loop.native_driver.core_port import CorePort, CorePortError
from codex_builder_loop.native_driver.transport_failures import classify_turn_failure
from codex_builder_loop.process import process_group_gone, read_proc_identity


def native_contract(repo: Path) -> dict:
    contract = {
        "schema_version": 4,
        "mission": {
            "revision": 1,
            "objective": "Deliver a native driver fixture.",
            "behaviors": [{"id": "native-loop", "description": "Native Driver advances one action."}],
            "interfaces": [],
            "acceptance_cases": [
                {
                    "id": "final",
                    "description": "The run can finalize.",
                    "observation": {
                        "surface_id": "driver-cli",
                        "surface_description": "The public driver behavior observed by the frozen fixture command.",
                        "execution_ids": ["blackbox"],
                        "required_dimensions": ["verify"],
                    },
                }
            ],
            "trust_boundaries": [{"id": "roles", "description": "Role threads remain distinct."}],
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
                {"id": "fixture", "argv": ["bash", "verify.sh"], "timeout_seconds": 30}
            ],
        },
        "execution": {
            "version": 1,
            "driver_enforced": True,
            "candidate_head": None,
            "builder_files": [],
            "tester_files": [],
            "tester_source": None,
            "dirty_snapshot": [],
            "commands": [
                {"id": "blackbox", "argv": ["bash", "verify.sh"], "timeout_seconds": 30}
            ],
            "agents": {},
        },
    }
    return add_v4_progress_contract(contract)


def root_native_contract(repo: Path) -> dict:
    contract = native_contract(repo)
    contract["execution"]["builder_runtime"] = {
        "schema_version": 1,
        "mode": "root_session",
    }
    return contract


class NativeDriverCoreContractTest(unittest.TestCase):
    def test_core_port_defaults_to_the_current_checkout_cli(self) -> None:
        previous = os.environ.pop("CODEX_BUILDER_LOOP_BIN", None)
        try:
            port = CorePort()
        finally:
            if previous is not None:
                os.environ["CODEX_BUILDER_LOOP_BIN"] = previous
        self.assertEqual(port.command[0], sys.executable)
        self.assertEqual(Path(port.command[1]).resolve(), CLI.resolve())

    def setUp(self) -> None:
        self.repo = init_repo()
        self.tmp = tempfile.TemporaryDirectory(prefix="native-driver-v1-")
        self.artifacts = Path(self.tmp.name)

    def tearDown(self) -> None:
        cleanup_repo(self.repo)
        self.tmp.cleanup()

    def invoke(self, command: str, *args: str | Path, stdin: object | None = None) -> dict:
        completed = run_process(
            [sys.executable, CLI, "assurance", "--experimental-v4", command, *args],
            env=None,
            input_text=json.dumps(stdin) if stdin is not None else None,
        )
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, completed.stderr)
        value = json.loads(lines[-1])
        self.assertEqual(completed.returncode, 0, value)
        return value

    def start_with_contract(
        self,
        run_id: str,
        contract: dict,
        *,
        session_id: str = "native-session",
        transport: str = "codex_app_server",
        runtime_version: str = "codex-test",
    ) -> tuple[dict, Path]:
        args = [
            "start",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--session-id",
            session_id,
            "--contract",
            "-",
            "--driver-kind",
            "native",
            "--driver-transport",
            transport,
            "--driver-runtime-version",
            runtime_version,
            "--driver-protocol-schema-digest",
            "a" * 64,
        ]
        if transport == "root_session":
            args.extend(
                [
                    "--driver-root-session-identity",
                    json.dumps(
                        native_cli._root_session_identity(session_id),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ]
            )
        result = self.invoke(*args, stdin=contract)
        return result, Path(result["candidate_worktree"]).parent

    def start(self) -> tuple[str, Path]:
        run_id = "native-driver-core"
        _result, run_path = self.start_with_contract(
            run_id,
            native_contract(self.repo),
        )
        return run_id, run_path

    def test_root_builder_dispatch_requires_owner_and_application_before_consume(
        self,
    ) -> None:
        run_id = "root-builder-transaction"
        started, _run_path = self.start_with_contract(
            run_id,
            root_native_contract(self.repo),
            transport="root_session",
            runtime_version="root-session",
        )
        self.assertEqual(started["driver_runtime"]["transport"], "root_session")
        action = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.assertEqual(action["action"], "builder_implement")

        self.invoke(
            "prepare-builder",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--agent-id",
            "codex-root-session:native-session",
            "--owner-mode",
            "root_session",
            "--owner-session-id",
            "native-session",
            "--action-id",
            action["action_id"],
            "--driver-runtime-kind",
            "native",
        )
        action = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.assertEqual(action["action"], "builder_implement")

        def failed(*args: str, stdin: object | None = None) -> dict:
            completed = run_process(
                [
                    sys.executable,
                    CLI,
                    "assurance",
                    "--experimental-v4",
                    *args,
                ],
                input_text=json.dumps(stdin) if stdin is not None else None,
            )
            lines = [line for line in completed.stdout.splitlines() if line.strip()]
            self.assertTrue(lines, completed.stderr)
            payload = json.loads(lines[-1])
            self.assertNotEqual(completed.returncode, 0, payload)
            return payload

        begin_args = (
            "begin-dispatch",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action["action_id"],
            "--action",
            action["action"],
            "--role",
            "builder",
            "--prompt-digest",
            "b" * 64,
            "--output-schema-digest",
            "c" * 64,
            "--driver-runtime-kind",
            "native",
        )
        wrong_begin = failed(
            *begin_args,
            "--owner-session-id",
            "wrong-session",
        )
        self.assertEqual(wrong_begin["code"], "BUILDER_OWNER_SESSION_MISMATCH")
        self.invoke(
            *begin_args,
            "--owner-session-id",
            "native-session",
        )
        context = self.invoke("driver-context", "--repo", self.repo, "--run", run_id)
        self.assertEqual(context["dispatch_intent"]["state"], "in_flight")
        self.assertIsNone(context["dispatch_intent"]["thread_id"])
        self.assertEqual(
            context["dispatch_intent"]["owner_session_id"], "native-session"
        )
        retry = failed(
            "retry-dispatch",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action["action_id"],
            "--failure-code",
            "interruptedNoOutput",
        )
        self.assertEqual(retry["code"], "NATIVE_ROOT_BUILDER_RETRY_FORBIDDEN")

        result = {
            "result": "implemented",
            "evidence_report": None,
            "proof_spec": None,
            "problem_report": None,
        }
        wrong_complete = failed(
            "complete-dispatch",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action["action_id"],
            "--result",
            "-",
            "--owner-session-id",
            "wrong-session",
            stdin=result,
        )
        self.assertEqual(wrong_complete["code"], "BUILDER_OWNER_SESSION_MISMATCH")
        self.invoke(
            "complete-dispatch",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action["action_id"],
            "--result",
            "-",
            "--owner-session-id",
            "native-session",
            stdin=result,
        )
        not_applied = failed(
            "consume-dispatch",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action["action_id"],
            "--owner-session-id",
            "native-session",
            "--consumer-source",
            "native_driver",
        )
        self.assertEqual(not_applied["code"], "ROOT_BUILDER_RESULT_NOT_APPLIED")

        self.invoke(
            "checkpoint-builder",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action["action_id"],
            "--owner-session-id",
            "native-session",
            "--driver-runtime-kind",
            "native",
        )
        applied = self.invoke("driver-context", "--repo", self.repo, "--run", run_id)
        self.assertEqual(
            applied["dispatch_intent"]["result_application"], "checkpoint_builder"
        )
        self.invoke(
            "consume-dispatch",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action["action_id"],
            "--owner-session-id",
            "native-session",
            "--consumer-source",
            "native_driver",
        )
        finished = self.invoke("driver-context", "--repo", self.repo, "--run", run_id)
        self.assertIsNone(finished["dispatch_intent"])

    def test_root_start_does_not_probe_app_server_until_transport_handoff(self) -> None:
        repo = self.repo

        class FakeCore:
            def start(self, **_kwargs):
                return {"candidate_worktree": str(repo / "candidate")}

        class FakeCoordinator:
            def __init__(self, **_kwargs):
                pass

            def run(self):
                return {
                    "status": "BUILDER_HANDOFF",
                    "run_id": "root-lazy-probe",
                    "action": "builder_implement",
                    "action_id": "a" * 64,
                }

        contract_path = self.artifacts / "root-lazy-contract.json"
        contract_path.write_text(
            json.dumps(root_native_contract(self.repo)),
            encoding="utf-8",
        )
        output = StringIO()
        with (
            patch.object(native_cli, "CorePort", return_value=FakeCore()),
            patch.object(
                native_cli,
                "NativeCoordinator",
                FakeCoordinator,
            ),
            patch.object(
                native_cli,
                "probe_app_server",
                side_effect=AssertionError("Builder handoff must not probe App Server"),
            ),
            redirect_stdout(output),
        ):
            rc = native_cli.main(
                [
                    "start",
                    "--repo",
                    str(self.repo),
                    "--run",
                    "root-lazy-probe",
                    "--session-id",
                    "root-lazy-session",
                    "--contract",
                    str(contract_path),
                ]
            )
        self.assertEqual(rc, 1)
        payload = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(payload["status"], "BUILDER_HANDOFF")

    def test_root_builder_problem_result_uses_session_producer_and_consumes_after_record(
        self,
    ) -> None:
        run_id = "root-builder-problem"
        self.start_with_contract(
            run_id,
            root_native_contract(self.repo),
            transport="root_session",
            runtime_version="root-session",
        )
        action = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.invoke(
            "prepare-builder",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--agent-id",
            "codex-root-session:native-session",
            "--owner-mode",
            "root_session",
            "--owner-session-id",
            "native-session",
            "--action-id",
            action["action_id"],
            "--driver-runtime-kind",
            "native",
        )
        action = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.invoke(
            "begin-dispatch",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action["action_id"],
            "--action",
            action["action"],
            "--role",
            "builder",
            "--prompt-digest",
            "b" * 64,
            "--output-schema-digest",
            "c" * 64,
            "--owner-session-id",
            "native-session",
            "--driver-runtime-kind",
            "native",
        )
        result = {
            "result": "blocked",
            "evidence_report": None,
            "proof_spec": None,
            "problem_report": {
                "schema_version": 1,
                "problems": [
                    {
                        "key": "builder-blocked",
                        "summary": "Builder is blocked by a fixture problem.",
                        "details": "The root Builder needs a user-visible decision.",
                        "owner": "builder",
                    }
                ],
            },
        }
        self.invoke(
            "complete-dispatch",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action["action_id"],
            "--result",
            "-",
            "--owner-session-id",
            "native-session",
            stdin=result,
        )
        self.invoke(
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
            "codex-root-session:native-session",
            "--action-id",
            action["action_id"],
            "--owner-session-id",
            "native-session",
            "--driver-runtime-kind",
            "native",
            stdin=result["problem_report"],
        )
        context = self.invoke("driver-context", "--repo", self.repo, "--run", run_id)
        self.assertEqual(
            context["dispatch_intent"]["result_application"], "record_problems"
        )
        self.assertEqual(
            context["problems"][-1]["producer"],
            {
                "role": "builder",
                "mode": "root_session",
                "agent_id": "codex-root-session:native-session",
                "session_id": "native-session",
            },
        )
        self.invoke(
            "consume-dispatch",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action["action_id"],
            "--owner-session-id",
            "native-session",
            "--consumer-source",
            "native_driver",
        )

    def prepare_replacement_fixture(
        self, run_id: str
    ) -> tuple[Path, dict, dict]:
        started, run_path = self.start_with_contract(
            run_id, native_contract(self.repo)
        )
        candidate = Path(started["candidate_worktree"])
        builder = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.invoke(
            "prepare-builder",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--agent-id",
            "builder-agent",
            "--thread-id",
            "builder-thread",
            "--action-id",
            builder["action_id"],
            "--driver-runtime-kind",
            "native",
        )
        (candidate / "src" / "native.py").write_text("VALUE = 1\n", encoding="utf-8")
        commit_all(candidate, "native replacement candidate")
        checkpoint = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.invoke(
            "checkpoint-builder",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            checkpoint["action_id"],
            "--driver-runtime-kind",
            "native",
        )
        tester_action = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        if tester_action["action"] == "scan_doc_references":
            self.invoke(
                "scan-doc-references",
                "--repo",
                self.repo,
                "--run",
                run_id,
                "--action-id",
                tester_action["action_id"],
                "--driver-runtime-kind",
                "native",
            )
            tester_action = self.invoke(
                "driver-next", "--repo", self.repo, "--run", run_id
            )
        self.assertEqual(tester_action["action"], "tester_author")
        self.invoke(
            "prepare-tester",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--agent-id",
            "tester-old",
            "--thread-id",
            "tester-old-thread",
            "--action-id",
            tester_action["action_id"],
            "--driver-runtime-kind",
            "native",
        )
        ledger = json.loads((run_path / "ledger.json").read_text())
        old_source = ledger["facets"]["execution"]["tester_source"]
        old_worktree = Path(old_source["worktree"])
        (old_worktree / "tests" / "test_native.py").write_text(
            "import unittest\n\nclass NativeTest(unittest.TestCase):\n"
            "    def test_value(self):\n        self.assertEqual(1, 1)\n",
            encoding="utf-8",
        )
        commit_all(old_worktree, "tester old source")
        assurance_core.integrate_tester(self.repo, run_id)
        next_action = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        if next_action["action"] == "scan_doc_references":
            self.invoke(
                "scan-doc-references",
                "--repo",
                self.repo,
                "--run",
                run_id,
                "--action-id",
                next_action["action_id"],
                "--driver-runtime-kind",
                "native",
            )
        ledger = json.loads((run_path / "ledger.json").read_text())
        return run_path, ledger["facets"]["execution"]["tester_source"], tester_action

    def prepare_tester_author_dispatch_fixture(
        self, run_id: str
    ) -> tuple[Path, dict]:
        started, run_path = self.start_with_contract(
            run_id, native_contract(self.repo)
        )
        candidate = Path(started["candidate_worktree"])
        builder = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.invoke(
            "prepare-builder",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--agent-id",
            "bootstrap-builder",
            "--thread-id",
            "bootstrap-builder-thread",
            "--action-id",
            builder["action_id"],
            "--driver-runtime-kind",
            "native",
        )
        (candidate / "src" / "bootstrap.py").write_text(
            "VALUE = 1\n", encoding="utf-8"
        )
        commit_all(candidate, "bootstrap candidate")
        checkpoint = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.invoke(
            "checkpoint-builder",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            checkpoint["action_id"],
            "--driver-runtime-kind",
            "native",
        )
        action = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        if action["action"] == "scan_doc_references":
            self.invoke(
                "scan-doc-references",
                "--repo",
                self.repo,
                "--run",
                run_id,
                "--action-id",
                action["action_id"],
                "--driver-runtime-kind",
                "native",
            )
            action = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.assertEqual(action["action"], "tester_author")
        self.invoke(
            "prepare-tester",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--agent-id",
            "bootstrap-tester",
            "--thread-id",
            "bootstrap-tester-thread",
            "--action-id",
            action["action_id"],
            "--driver-runtime-kind",
            "native",
        )
        action = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.assertEqual(action["action"], "tester_author")
        self.assertIsInstance(action.get("context_projection_digest"), str)
        self.invoke(
            "begin-dispatch",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action["action_id"],
            "--action",
            action["action"],
            "--role",
            "tester",
            "--thread-id",
            "bootstrap-tester-thread",
            "--prompt-digest",
            "b" * 64,
            "--output-schema-digest",
            "c" * 64,
            "--work-unit-id",
            action["work_unit_id"],
            "--context-projection-digest",
            action["context_projection_digest"],
            "--driver-runtime-kind",
            "native",
        )
        return run_path, action

    def test_pre_turn_no_rollout_records_tester_continuity_problem(self) -> None:
        run_id = "native-tester-bootstrap-no-rollout"
        run_path, action = self.prepare_tester_author_dispatch_fixture(run_id)
        coordinator = NativeCoordinator(
            repo=self.repo,
            run_id=run_id,
            core=CorePort(),
            transport=object(),
            project_root=ROOT,
        )
        coordinator.current_action = action
        error = AppServerError(
            "no rollout found for thread id bootstrap-tester-thread",
            code="NATIVE_APP_SERVER_REQUEST_FAILED",
            details={
                "method": "thread/resume",
                "error": {
                    "code": -32600,
                    "message": "no rollout found for thread id bootstrap-tester-thread",
                },
            },
        )

        payload = coordinator.retry_transport_failure(error)

        self.assertEqual(payload["status"], "ACTIVE")
        ledger = json.loads((run_path / "ledger.json").read_text())
        self.assertIsNone(ledger["dispatch_intent"])
        problem = next(
            item
            for item in ledger["problems"]
            if item["key"].startswith("tester-bootstrap-continuity-")
        )
        self.assertEqual(problem["owner"], "tester")
        self.assertEqual(problem["producer_continuity"], "invalid")
        self.assertEqual(problem["producer"]["thread_id"], "bootstrap-tester-thread")
        decision = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.assertEqual(decision["action"], "replace_tester")

    def test_unknown_pre_dispatch_activation_stays_active_until_explicit_rearm(
        self,
    ) -> None:
        run_id = "native-tester-bootstrap-unknown"
        run_path, action = self.prepare_tester_author_dispatch_fixture(run_id)
        before = json.loads((run_path / "ledger.json").read_text())["dispatch_intent"]

        assurance_core.record_dispatch_activation(
            self.repo,
            run_id,
            action_id=action["action_id"],
            activation_state="unknown",
            failure_code="NATIVE_THREAD_RESUME_FAILED",
            failure_details={"method": "thread/resume"},
        )
        unknown = json.loads((run_path / "ledger.json").read_text())["dispatch_intent"]
        self.assertEqual(unknown["activation_state"], "unknown")
        self.assertEqual(unknown["attempt"], before["attempt"])
        self.assertEqual(unknown["state"], "prepared")
        decision = assurance_core_driver.next_action(self.repo, run_id)
        self.assertEqual(decision["status"], "NEEDS_USER")
        self.assertEqual(decision["reason"], "dispatch_activation_unknown")
        with self.assertRaises(assurance_core.AssuranceError) as retry:
            assurance_core.retry_dispatch(
                self.repo,
                run_id,
                action_id=action["action_id"],
                failure_code="responseStreamDisconnected",
            )
        self.assertEqual(retry.exception.code, "DISPATCH_ACTIVATION_REARM_REQUIRED")
        with self.assertRaises(assurance_core.AssuranceError) as bootstrap:
            assurance_core.record_tester_bootstrap_failure(
                self.repo,
                run_id,
                action_id=action["action_id"],
                failure_code="NATIVE_APP_SERVER_REQUEST_FAILED",
                failure_message="activation outcome is unknown",
            )
        self.assertEqual(
            bootstrap.exception.code, "DISPATCH_ACTIVATION_REARM_REQUIRED"
        )

        assurance_core.record_dispatch_activation(
            self.repo,
            run_id,
            action_id=action["action_id"],
            activation_state="pending",
            reason="verified that no turn was started",
        )
        rearmed = json.loads((run_path / "ledger.json").read_text())["dispatch_intent"]
        self.assertEqual(rearmed["activation_state"], "pending")
        self.assertEqual(rearmed["attempt"], before["attempt"])
        self.assertEqual(
            assurance_core_driver.next_action(self.repo, run_id)["reason"],
            "dispatch_prepared",
        )

    def test_blackbox_only_prepares_tester_identity_without_source_gate(self) -> None:
        run_id = "native-blackbox-only"
        contract = native_contract(self.repo)
        contract["authority"]["tester_write"] = []
        contract["assurance"]["required"] = ["machine", "blackbox", "reviewer"]
        started, run_path = self.start_with_contract(run_id, contract)
        candidate = Path(started["candidate_worktree"])

        builder = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.invoke(
            "prepare-builder",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--agent-id",
            "native-builder",
            "--thread-id",
            "thread-builder",
            "--action-id",
            builder["action_id"],
            "--driver-runtime-kind",
            "native",
        )
        (candidate / "src" / "native.py").write_text("VALUE = 1\n", encoding="utf-8")
        commit_all(candidate, "native blackbox-only candidate")
        checkpoint = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.assertEqual(checkpoint["action"], "checkpoint_builder")
        self.invoke(
            "checkpoint-builder",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            checkpoint["action_id"],
            "--driver-runtime-kind",
            "native",
        )
        machine = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        if machine["action"] == "scan_doc_references":
            self.invoke(
                "scan-doc-references",
                "--repo",
                self.repo,
                "--run",
                run_id,
                "--action-id",
                machine["action_id"],
                "--driver-runtime-kind",
                "native",
            )
            machine = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.assertEqual(machine["action"], "verify_machine")
        self.invoke(
            "verify-machine",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            machine["action_id"],
            "--driver-runtime-kind",
            "native",
        )
        blackbox = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.assertEqual(blackbox["action"], "tester_blackbox")
        self.assertIsNone(blackbox["agent"])

        prepared = self.invoke(
            "prepare-tester",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--agent-id",
            "native-blackbox-tester",
            "--thread-id",
            "thread-blackbox",
            "--identity-only",
            "--action-id",
            blackbox["action_id"],
            "--driver-runtime-kind",
            "native",
        )
        self.assertEqual(prepared["readiness"]["states"]["machine"], "pass")
        ledger = json.loads((run_path / "ledger.json").read_text())
        self.assertEqual(
            ledger["facets"]["execution"]["agents"]["tester"],
            {
                "agent_id": "native-blackbox-tester",
                "thread_id": "thread-blackbox",
            },
        )
        self.assertIsNone(ledger["facets"]["execution"]["tester_source"])
        self.assertNotIn("tester", ledger["evidence"])
        self.assertFalse(any(run_path.glob("tester-*")))
        event = next(
            item
            for item in reversed(ledger["events"])
            if item["kind"] == "tester_identity_prepared"
        )
        self.assertEqual(event["details"]["agent"]["thread_id"], "thread-blackbox")
        resumed = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.assertEqual(resumed["action"], "tester_blackbox")
        self.assertEqual(resumed["agent"]["thread_id"], "thread-blackbox")

        wrong_mode = run_process(
            [
                sys.executable,
                CLI,
                "assurance",
                "--experimental-v4",
                "prepare-tester",
                "--repo",
                self.repo,
                "--run",
                run_id,
                "--agent-id",
                "native-blackbox-tester",
                "--thread-id",
                "thread-blackbox",
                "--action-id",
                resumed["action_id"],
                "--driver-runtime-kind",
                "native",
            ]
        )
        self.assertNotEqual(wrong_mode.returncode, 0)
        self.assertEqual(json.loads(wrong_mode.stdout)["code"], "DRIVER_ACTION_STALE")

        upgraded = assurance_core.prepare_tester(
            self.repo,
            run_id,
            "native-blackbox-tester",
            "thread-blackbox",
        )
        self.assertEqual(upgraded["readiness"]["states"]["machine"], "stale")
        upgraded_ledger = json.loads((run_path / "ledger.json").read_text())
        source = upgraded_ledger["facets"]["execution"]["tester_source"]
        self.assertEqual(source["agent"]["thread_id"], "thread-blackbox")
        self.assertEqual(
            upgraded_ledger["facets"]["execution"]["agents"]["tester"],
            source["agent"],
        )
        self.assertTrue(Path(source["worktree"]).is_dir())

    def test_driver_port_binds_builder_and_recovers_one_dispatch(self) -> None:
        run_id, run_path = self.start()
        first = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.assertEqual(first["driver_protocol_version"], 1)
        self.assertEqual(first["action"], "builder_implement")
        wrong_owner = run_process(
            [
                sys.executable,
                CLI,
                "assurance",
                "--experimental-v4",
                "prepare-builder",
                "--repo",
                self.repo,
                "--run",
                run_id,
                "--agent-id",
                "native-builder",
                "--thread-id",
                "thread-builder",
                "--action-id",
                first["action_id"],
            ]
        )
        self.assertNotEqual(wrong_owner.returncode, 0)
        self.assertEqual(
            json.loads(wrong_owner.stdout)["code"], "DRIVER_RUNTIME_OWNER_MISMATCH"
        )
        self.invoke(
            "prepare-builder",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--agent-id",
            "native-builder",
            "--thread-id",
            "thread-builder",
            "--action-id",
            first["action_id"],
            "--driver-runtime-kind",
            "native",
        )
        action = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.invoke(
            "begin-dispatch",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action["action_id"],
            "--action",
            action["action"],
            "--role",
            "builder",
            "--thread-id",
            "thread-builder",
            "--prompt-digest",
            "b" * 64,
            "--output-schema-digest",
            "c" * 64,
        )
        pending = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.assertEqual(pending["action_id"], action["action_id"])
        self.assertEqual(pending["reason"], "dispatch_prepared")
        self.invoke(
            "bind-dispatch-turn",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action["action_id"],
            "--turn-id",
            "turn-1",
        )
        self.invoke(
            "retry-dispatch",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action["action_id"],
            "--failure-code",
            "serverOverloaded",
        )
        retried = json.loads((run_path / "ledger.json").read_text())["dispatch_intent"]
        self.assertEqual(retried["attempt"], 2)
        self.assertEqual(retried["state"], "prepared")
        self.assertNotIn("turn_id", retried)
        telemetry = self.invoke("status", "--repo", self.repo, "--run", run_id)[
            "telemetry"
        ]
        self.assertEqual(telemetry["active_stage"], "builder_implement")
        self.assertEqual(telemetry["retries"]["total"], 1)
        self.assertEqual(
            telemetry["retries"]["by_failure_code"], {"serverOverloaded": 1}
        )
        builder_stage = next(
            item for item in telemetry["stages"] if item["name"] == "builder_implement"
        )
        self.assertEqual(builder_stage["attempts"], 1)
        self.assertEqual(builder_stage["retry_count"], 1)
        self.invoke(
            "bind-dispatch-turn",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action["action_id"],
            "--turn-id",
            "turn-2",
        )
        result = {
            "result": "implemented",
            "evidence_report": None,
            "proof_spec": None,
            "problem_report": None,
        }
        self.invoke(
            "complete-dispatch",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action["action_id"],
            "--result",
            "-",
            stdin=result,
        )
        ledger = json.loads((run_path / "ledger.json").read_text())
        artifact = Path(ledger["dispatch_intent"]["result_path"])
        self.assertEqual(json.loads(artifact.read_text()), result)
        self.invoke(
            "consume-dispatch",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action["action_id"],
            "--consumer-source",
            "native_driver",
        )
        consumed_ledger = json.loads((run_path / "ledger.json").read_text())
        self.assertIsNone(consumed_ledger["dispatch_intent"])
        consumed = next(
            item
            for item in reversed(consumed_ledger["events"])
            if item.get("kind") == "dispatch_consumed"
        )
        self.assertEqual(
            consumed.get("details", {}).get("consumer_source"), "native_driver"
        )

    def test_native_transport_wire_wait_and_cleanup_facts_are_ledger_bound(self) -> None:
        run_id, run_path = self.start()
        ledger_path = run_path / "ledger.json"
        ledger = json.loads(ledger_path.read_text())
        ledger["driver_runtime"]["native_transport"] = {
            "contract_version": 2,
            "generation": "transport-generation-1",
            "state": "ready",
            "executable_identity": {
                "requested": "/usr/bin/codex",
                "resolved": "/usr/bin/codex",
                "sha256": "a" * 64,
                "runtime_version": "codex-test",
                "protocol_schema_digest": "b" * 64,
                "protocol_canary_digest": "c" * 64,
            },
            "process_identity": {
                "pid": 1001,
                "pgid": 1001,
                "starttime": "1",
                "parent_pid": 1,
                "argv_digest": "d" * 64,
                "executable_identity_digest": "e" * 64,
                "started_at": "2026-08-20T00:00:00+00:00",
                "exited_at": None,
                "exit_code": None,
                "process_group_gone": None,
            },
        }
        ledger_path.write_text(
            json.dumps(ledger, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        first = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.invoke(
            "prepare-builder",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--agent-id",
            "native-builder",
            "--thread-id",
            "thread-builder",
            "--action-id",
            first["action_id"],
            "--driver-runtime-kind",
            "native",
        )
        action = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.invoke(
            "begin-dispatch",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action["action_id"],
            "--action",
            action["action"],
            "--role",
            "builder",
            "--thread-id",
            "thread-builder",
            "--prompt-digest",
            "f" * 64,
            "--output-schema-digest",
            "1" * 64,
            "--native-transport-generation",
            "transport-generation-1",
            "--timeout-profile-digest",
            "2" * 64,
        )
        self.invoke(
            "bind-dispatch-turn",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action["action_id"],
            "--turn-id",
            "turn-1",
        )
        self.invoke(
            "record-dispatch-wire",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action["action_id"],
            "--native-transport-generation",
            "transport-generation-1",
            "--sequence",
            "1",
            "--event-digest",
            "3" * 64,
            "--driver-runtime-kind",
            "native",
        )
        self.invoke(
            "record-deferred-wait",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--state",
            "active",
            "--action-id",
            action["action_id"],
            "--dispatch-generation",
            "1",
            "--transport-generation",
            "transport-generation-1",
            "--delivery-state",
            "waiting",
            "--last-heartbeat-at",
            "2026-08-20T00:00:01+00:00",
            "--driver-runtime-kind",
            "native",
        )
        self.invoke(
            "record-deferred-wait",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--state",
            "stalled",
            "--action-id",
            action["action_id"],
            "--dispatch-generation",
            "1",
            "--transport-generation",
            "transport-generation-1",
            "--delivery-state",
            "external_delivery_unknown",
            "--last-heartbeat-at",
            "2026-08-20T00:02:01+00:00",
            "--driver-runtime-kind",
            "native",
        )
        process_identity = {
            "pid": 1001,
            "pgid": 1001,
            "starttime": "1",
            "parent_pid": 1,
            "argv_digest": "d" * 64,
            "executable_identity_digest": "e" * 64,
            "started_at": "2026-08-20T00:00:00+00:00",
            "exited_at": "2026-08-20T00:02:02+00:00",
            "exit_code": -15,
            "process_group_gone": True,
        }
        self.invoke(
            "record-transport-cleanup",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--generation",
            "transport-generation-1",
            "--process-identity",
            "-",
            "--state",
            "completed",
            "--term-attempt",
            "1",
            "--kill-attempt",
            "0",
            "--process-group-gone",
            "--driver-runtime-kind",
            "native",
            stdin=process_identity,
        )
        observed = json.loads(ledger_path.read_text())
        self.assertEqual(
            observed["dispatch_intent"]["native_transport_generation"],
            "transport-generation-1",
        )
        self.assertEqual(observed["dispatch_intent"]["last_wire_sequence"], 1)
        self.assertEqual(observed["deferred_wait"]["state"], "stalled")
        self.assertEqual(
            observed["transport_cleanup_intent"]["state"],
            "completed",
        )
        self.assertEqual(
            observed["driver_runtime"]["native_transport"]["state"],
            "cleaned",
        )
        second_transport = copy.deepcopy(observed["driver_runtime"]["native_transport"])
        second_transport["generation"] = "transport-generation-2"
        second_transport["state"] = "ready"
        second_transport["process_identity"] = {
            **second_transport["process_identity"],
            "pid": 1002,
            "pgid": 1002,
            "starttime": "2",
            "started_at": "2026-08-20T00:03:00+00:00",
            "exited_at": None,
            "exit_code": None,
            "process_group_gone": None,
        }
        self.invoke(
            "bind-native-transport",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--transport",
            "-",
            "--driver-runtime-kind",
            "native",
            stdin=second_transport,
        )
        second_identity = {
            **second_transport["process_identity"],
            "exited_at": "2026-08-20T00:03:01+00:00",
            "exit_code": 0,
            "process_group_gone": True,
        }
        self.invoke(
            "record-transport-cleanup",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--generation",
            "transport-generation-2",
            "--process-identity",
            "-",
            "--state",
            "completed",
            "--term-attempt",
            "1",
            "--kill-attempt",
            "0",
            "--process-group-gone",
            "--driver-runtime-kind",
            "native",
            stdin=second_identity,
        )
        observed = json.loads(ledger_path.read_text())
        cleanup_events = [
            item
            for item in observed["events"]
            if item["kind"] == "transport_cleanup_completed"
        ]
        self.assertEqual(len(cleanup_events), 2)
        self.assertEqual(
            observed["transport_cleanup_intent"]["source_generation"],
            "transport-generation-2",
        )
        telemetry = self.invoke("status", "--repo", self.repo, "--run", run_id)[
            "telemetry"
        ]
        self.assertEqual(telemetry["wire_observation_checkpoints"], 1)
        self.assertEqual(telemetry["deferred_wait_stalls"], 1)

    def test_exhausted_dispatch_requires_authorized_new_generation(self) -> None:
        run_id, run_path = self.start()
        first = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.invoke(
            "prepare-builder",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--agent-id",
            "native-builder",
            "--thread-id",
            "thread-builder",
            "--action-id",
            first["action_id"],
            "--driver-runtime-kind",
            "native",
        )
        action = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.invoke(
            "begin-dispatch",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action["action_id"],
            "--action",
            action["action"],
            "--role",
            "builder",
            "--thread-id",
            "thread-builder",
            "--prompt-digest",
            "b" * 64,
            "--output-schema-digest",
            "c" * 64,
        )
        for attempt in (1, 2):
            self.invoke(
                "bind-dispatch-turn",
                "--repo",
                self.repo,
                "--run",
                run_id,
                "--action-id",
                action["action_id"],
                "--turn-id",
                f"turn-{attempt}",
            )
            self.invoke(
                "retry-dispatch",
                "--repo",
                self.repo,
                "--run",
                run_id,
                "--action-id",
                action["action_id"],
                "--failure-code",
                "serverOverloaded",
            )
        self.invoke(
            "bind-dispatch-turn",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action["action_id"],
            "--turn-id",
            "turn-3",
        )
        exhausted = run_process(
            [
                sys.executable,
                CLI,
                "assurance",
                "--experimental-v4",
                "retry-dispatch",
                "--repo",
                self.repo,
                "--run",
                run_id,
                "--action-id",
                action["action_id"],
                "--failure-code",
                "serverOverloaded",
            ]
        )
        self.assertNotEqual(exhausted.returncode, 0)
        self.assertEqual(
            json.loads(exhausted.stdout)["code"],
            "NATIVE_DISPATCH_RETRY_EXHAUSTED",
        )
        ledger_path = run_path / "ledger.json"
        exhausted_ledger = json.loads(ledger_path.read_text())
        exhausted_intent = exhausted_ledger["dispatch_intent"]
        self.assertEqual(exhausted_intent["state"], "exhausted")
        self.assertEqual(exhausted_intent["attempt"], 3)
        self.assertEqual(exhausted_intent["generation"], 1)
        self.assertEqual(exhausted_intent["failure_code"], "serverOverloaded")
        self.assertEqual(exhausted_intent["turn_id"], "turn-3")
        self.assertEqual(
            [
                event["kind"]
                for event in exhausted_ledger["events"]
                if event["kind"] == "dispatch_retry_exhausted"
            ],
            ["dispatch_retry_exhausted"],
        )

        wrong_runtime = copy.deepcopy(exhausted_ledger)
        wrong_runtime["runtime_identity"]["adapter_commit"] = "f" * 40
        ledger_path.write_text(
            json.dumps(wrong_runtime, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        rejected = run_process(
            [
                sys.executable,
                CLI,
                "assurance",
                "--experimental-v4",
                "renew-dispatch",
                "--repo",
                self.repo,
                "--run",
                run_id,
                "--action-id",
                action["action_id"],
                "--reason",
                "user authorized a new transport generation",
                "--driver-runtime-kind",
                "native",
            ]
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(
            json.loads(rejected.stdout)["code"],
            "DISPATCH_RUNTIME_IDENTITY_MISMATCH",
        )
        ledger_path.write_text(
            json.dumps(exhausted_ledger, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        empty_reason = run_process(
            [
                sys.executable,
                CLI,
                "assurance",
                "--experimental-v4",
                "renew-dispatch",
                "--repo",
                self.repo,
                "--run",
                run_id,
                "--action-id",
                action["action_id"],
                "--reason",
                "",
                "--driver-runtime-kind",
                "native",
            ]
        )
        self.assertNotEqual(empty_reason.returncode, 0)
        self.assertEqual(
            json.loads(empty_reason.stdout)["code"],
            "DISPATCH_RENEWAL_REASON_REQUIRED",
        )

        self.invoke(
            "renew-dispatch",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action["action_id"],
            "--reason",
            "user authorized a new transport generation",
            "--driver-runtime-kind",
            "native",
        )
        renewed_ledger = json.loads(ledger_path.read_text())
        renewed_intent = renewed_ledger["dispatch_intent"]
        self.assertEqual(renewed_intent["state"], "prepared")
        self.assertEqual(renewed_intent["attempt"], 1)
        self.assertEqual(renewed_intent["generation"], 2)
        self.assertEqual(renewed_intent["thread_id"], "thread-builder")
        self.assertEqual(
            renewed_intent["renewal_reason"],
            "user authorized a new transport generation",
        )
        self.assertNotIn("turn_id", renewed_intent)
        renewal = next(
            event
            for event in reversed(renewed_ledger["events"])
            if event["kind"] == "dispatch_renewed"
        )
        self.assertEqual(renewal["details"]["previous_generation"], 1)
        self.assertEqual(renewal["details"]["generation"], 2)
        self.assertEqual(renewal["details"]["previous_turn_id"], "turn-3")
        telemetry = self.invoke("status", "--repo", self.repo, "--run", run_id)[
            "telemetry"
        ]
        self.assertEqual(telemetry["lifecycle"]["dispatch_renewals"], 1)
        self.assertIn("dispatch_renewed", telemetry["warnings"])

    def test_auth_unavailable_retry_persists_exponential_deadlines(self) -> None:
        run_id, run_path = self.start()
        first = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.invoke(
            "prepare-builder",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--agent-id",
            "native-builder",
            "--thread-id",
            "thread-builder",
            "--action-id",
            first["action_id"],
            "--driver-runtime-kind",
            "native",
        )
        action = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.invoke(
            "begin-dispatch",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action["action_id"],
            "--action",
            action["action"],
            "--role",
            "builder",
            "--thread-id",
            "thread-builder",
            "--prompt-digest",
            "b" * 64,
            "--output-schema-digest",
            "c" * 64,
        )
        for attempt, expected_delay in ((1, 30), (2, 60)):
            self.invoke(
                "bind-dispatch-turn",
                "--repo",
                self.repo,
                "--run",
                run_id,
                "--action-id",
                action["action_id"],
                "--turn-id",
                f"auth-turn-{attempt}",
            )
            self.invoke(
                "retry-dispatch",
                "--repo",
                self.repo,
                "--run",
                run_id,
                "--action-id",
                action["action_id"],
                "--failure-code",
                "authUnavailable",
            )
            intent = json.loads((run_path / "ledger.json").read_text())[
                "dispatch_intent"
            ]
            scheduled = datetime.fromisoformat(intent["retry_scheduled_at"])
            deadline = datetime.fromisoformat(intent["retry_not_before"])
            self.assertEqual(int((deadline - scheduled).total_seconds()), expected_delay)
            self.assertEqual(intent["attempt"], attempt + 1)
            self.assertEqual(intent["failure_code"], "authUnavailable")

    def test_exhausted_reviewer_dispatch_compacts_once_and_renews(self) -> None:
        run_id, run_path = self.start()
        ledger_path = run_path / "ledger.json"
        ledger = json.loads(ledger_path.read_text())
        reviewer = {
            "agent_id": "codex-app-server:reviewer-thread",
            "thread_id": "reviewer-thread",
        }
        ledger["facets"]["execution"]["agents"]["reviewer"] = reviewer
        ledger["digests"] = facet_digests(ledger["facets"])
        action_id = "d" * 64
        ledger["dispatch_intent"] = {
            "action_id": action_id,
            "action": "reviewer_preflight",
            "role": "reviewer",
            "thread_id": reviewer["thread_id"],
            "prompt_digest": "e" * 64,
            "output_schema_digest": "f" * 64,
            "state": "exhausted",
            "attempt": 3,
            "generation": 1,
            "turn_id": "reviewer-turn-3",
            "failure_code": "responseStreamDisconnected",
            "created_at": "2026-08-01T00:00:00+00:00",
            "exhausted_at": "2026-08-01T00:00:03+00:00",
        }
        ledger_path.write_text(
            json.dumps(ledger, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        prior_turns = [
            {
                "id": f"reviewer-turn-{index}",
                "status": "failed",
                "items": [],
                "error": {"codexErrorInfo": {"responseStreamDisconnected": {}}},
            }
            for index in (1, 2, 3)
        ]
        self.invoke(
            "prepare-dispatch-compaction",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action_id,
            "--prior-turn-count",
            "3",
            "--prior-tail-turn-id",
            "reviewer-turn-3",
            "--prior-turns-digest",
            digest(prior_turns),
            "--driver-runtime-kind",
            "native",
        )
        prepared = json.loads(ledger_path.read_text())["dispatch_intent"]
        self.assertEqual(prepared["compaction_recovery"]["state"], "prepared")
        self.assertEqual(
            prepared["compaction_recovery"]["prior_turns_digest"],
            digest(prior_turns),
        )

        blocked_renewal = run_process(
            [
                sys.executable,
                CLI,
                "assurance",
                "--experimental-v4",
                "renew-dispatch",
                "--repo",
                self.repo,
                "--run",
                run_id,
                "--action-id",
                action_id,
                "--reason",
                "do not bypass the persisted compaction",
                "--driver-runtime-kind",
                "native",
            ]
        )
        self.assertNotEqual(blocked_renewal.returncode, 0)
        self.assertEqual(
            json.loads(blocked_renewal.stdout)["code"],
            "DISPATCH_COMPACTION_PENDING",
        )

        compaction_turn = {
            "id": "reviewer-compaction-turn",
            "status": "completed",
            "durationMs": 1250,
            "items": [{"id": "context-item", "type": "contextCompaction"}],
        }
        self.invoke(
            "complete-dispatch-compaction",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action_id,
            "--compaction-turn-id",
            compaction_turn["id"],
            "--context-item-id",
            "context-item",
            "--compaction-turn-digest",
            digest(compaction_turn),
            "--compaction-duration-ms",
            "1250",
            "--observed-turn-count",
            "4",
            "--driver-runtime-kind",
            "native",
        )
        renewed_ledger = json.loads(ledger_path.read_text())
        renewed = renewed_ledger["dispatch_intent"]
        self.assertEqual(renewed["state"], "prepared")
        self.assertEqual(renewed["attempt"], 1)
        self.assertEqual(renewed["generation"], 2)
        self.assertEqual(
            renewed["renewal_reason"], "automatic_reviewer_thread_compaction"
        )
        self.assertEqual(renewed["prompt_digest"], "e" * 64)
        self.assertEqual(renewed["thread_id"], reviewer["thread_id"])
        self.assertEqual(renewed["compaction_recovery"]["state"], "completed")
        self.assertEqual(
            renewed["compaction_recovery"]["compaction_turn_digest"],
            digest(compaction_turn),
        )
        telemetry = self.invoke("status", "--repo", self.repo, "--run", run_id)[
            "telemetry"
        ]
        self.assertEqual(telemetry["lifecycle"]["reviewer_thread_compactions"], 1)
        self.assertEqual(telemetry["lifecycle"]["dispatch_renewals"], 1)
        compaction_stage = next(
            item
            for item in telemetry["stages"]
            if item["name"] == "reviewer_thread_compaction"
        )
        self.assertEqual(compaction_stage["attempts"], 1)
        self.assertEqual(compaction_stage["completed_attempts"], 1)
        self.assertEqual(compaction_stage["total_duration_ms"], 1250)

    def test_dispatch_retry_reconstructs_machine_failure_from_ledger(self) -> None:
        context = {
            "facets": {
                "mission": {},
                "authority": {},
                "assurance": {},
                "execution": {
                    "agents": {
                        "tester": {
                            "agent_id": "tester-agent",
                            "thread_id": "tester-thread",
                        }
                    },
                    "tester_source": None,
                },
            },
            "target_start_head": "0" * 40,
            "candidate_worktree": "/candidate",
            "publication": None,
            "evidence": {},
            "problems": [],
            "machine_failure": {"failure_digest": "d" * 64},
        }
        action = {
            "action": "tester_machine_diagnose",
            "action_id": "a" * 64,
            "machine_failure": copy.deepcopy(context["machine_failure"]),
            "agent": context["facets"]["execution"]["agents"]["tester"],
        }
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="machine-prompt-recovery",
            core=object(),
            transport=object(),
            project_root=ROOT,
        )
        prompt = coordinator._prompt(action, "tester", context)
        intent_only = {
            "action": action["action"],
            "action_id": action["action_id"],
            "role": "tester",
            "thread_id": "tester-thread",
        }
        self.assertNotEqual(
            digest(coordinator._prompt(intent_only, "tester", context)), digest(prompt)
        )
        rebuilt = {
            **intent_only,
            "machine_failure": copy.deepcopy(context["machine_failure"]),
        }
        self.assertEqual(digest(coordinator._prompt(rebuilt, "tester", context)), digest(prompt))

    def test_dispatch_retry_reconstructs_every_action_prompt_payload(self) -> None:
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="all-action-prompt-recovery",
            core=object(),
            transport=object(),
            project_root=ROOT,
        )
        context = {
            "facets": native_contract(ROOT),
            "target_start_head": "0" * 40,
            "candidate_worktree": "/candidate",
            "repo_root": str(ROOT),
            "publication": {"required": False},
            "evidence": {},
            "problems": [],
            "recomposition_intent": {
                "state": "waiting_builder",
                "builder_worktree": "/builder-recompose",
                "tester_worktree": "/tester-recompose",
                "tester_head": "5" * 40,
            },
            "proof_failure": {},
            "machine_failure": {},
        }
        context["facets"]["execution"].update(
            {
                "candidate_head": "1" * 40,
                "agents": {
                    "builder": {
                        "agent_id": "builder-agent",
                        "thread_id": "builder-thread",
                    },
                    "tester": {
                        "agent_id": "tester-agent",
                        "thread_id": "tester-thread",
                    },
                    "reviewer": {
                        "agent_id": "reviewer-agent",
                        "thread_id": "reviewer-thread",
                    },
                },
                "tester_source": {
                    "head": "2" * 40,
                    "base_head": "3" * 40,
                    "branch": "tester/source",
                    "worktree": "/tester-source",
                    "files": [],
                    "agent": {
                        "agent_id": "tester-agent",
                        "thread_id": "tester-thread",
                    },
                },
            }
        )
        failure_ledger = {
            "facets": context["facets"],
            "digests": facet_digests(context["facets"]),
            "target_start_head": context["target_start_head"],
            "publication": context["publication"],
        }
        tester_agent = context["facets"]["execution"]["agents"]["tester"]
        tester_source = context["facets"]["execution"]["tester_source"]
        context["proof_failure"] = {
            "failure_digest": "6" * 64,
            "dependency_digest": evidence_dependency(failure_ledger, "proof"),
            "candidate_head": context["facets"]["execution"]["candidate_head"],
            "tester_source_head": tester_source["head"],
            "producer": {"role": "tester", **tester_agent},
        }
        context["machine_failure"] = {
            "stage": "machine",
            "failure_signature": "7" * 64,
            "dependency_digest": evidence_dependency(failure_ledger, "machine"),
            "candidate_head": context["facets"]["execution"]["candidate_head"],
            "tester_source_head": tester_source["head"],
        }
        actions = {
            "builder_implement": ("builder", {}),
            "builder_fix": ("builder", {}),
            "tester_author": ("tester", {}),
            "tester_fix": ("tester", {}),
            "tester_proof": ("tester", {}),
            "tester_blackbox": ("tester", {}),
            "reviewer_preflight": ("reviewer", {}),
            "reviewer_final": ("reviewer", {}),
            "builder_recompose_fix": (
                "builder",
                {
                    "recomposition": context["recomposition_intent"],
                    "candidate_worktree": "/builder-recompose",
                },
            ),
            "tester_recompose_fix": (
                "tester",
                {
                    "recomposition": context["recomposition_intent"],
                    "tester_source": {
                        "worktree": "/tester-recompose",
                        "head": "5" * 40,
                    },
                },
            ),
            "tester_proof_diagnose": (
                "tester",
                {"proof_failure": context["proof_failure"]},
            ),
            "tester_machine_diagnose": (
                "tester",
                {"machine_failure": context["machine_failure"]},
            ),
        }
        for index, (action_name, (role, payload)) in enumerate(actions.items(), 1):
            with self.subTest(action=action_name):
                action = {
                    "action": action_name,
                    "action_id": f"{index:064x}",
                    "agent": copy.deepcopy(
                        context["facets"]["execution"]["agents"][role]
                    ),
                    **copy.deepcopy(payload),
                }
                if action_name.startswith("tester_"):
                    action.setdefault(
                        "tester_source",
                        copy.deepcopy(
                            context["facets"]["execution"]["tester_source"]
                        ),
                    )
                prompt = coordinator._prompt(action, role, context)
                pending = {
                    "action": action_name,
                    "action_id": action["action_id"],
                    "role": role,
                    "thread_id": action["agent"]["thread_id"],
                    "prompt_digest": digest(prompt),
                    "output_schema_digest": coordinator.output_schema_digest,
                    "state": "prepared",
                    "attempt": 2,
                    "generation": 1,
                }
                ledger = {
                    "candidate_worktree": context["candidate_worktree"],
                    "facets": copy.deepcopy(context["facets"]),
                    "digests": facet_digests(context["facets"]),
                    "target_start_head": context["target_start_head"],
                    "publication": copy.deepcopy(context["publication"]),
                    "recomposition_intent": copy.deepcopy(
                        context["recomposition_intent"]
                    ),
                    "proof_failure": copy.deepcopy(context["proof_failure"]),
                    "machine_failure": copy.deepcopy(context["machine_failure"]),
                }
                rebuilt = assurance_core_driver._dispatch_action(
                    ledger, coordinator.run_id, pending
                )
                self.assertEqual(
                    digest(coordinator._prompt(rebuilt, role, context)),
                    pending["prompt_digest"],
                )

    def test_dispatch_recovery_resumes_recomposition_in_rebuilt_worktree(self) -> None:
        context = {
            "facets": native_contract(ROOT),
            "target_start_head": "0" * 40,
            "candidate_worktree": "/candidate",
            "repo_root": str(ROOT),
            "publication": None,
            "evidence": {},
            "problems": [],
        }
        context["facets"]["execution"]["agents"] = {
            "builder": {
                "agent_id": "builder-agent",
                "thread_id": "builder-thread",
            }
        }
        action = {
            "action": "builder_recompose_fix",
            "action_id": "a" * 64,
            "candidate_worktree": "/rebuilt-builder-worktree",
            "recomposition": {"state": "waiting_builder"},
            "agent": context["facets"]["execution"]["agents"]["builder"],
        }

        class FakeTransport:
            def __init__(self) -> None:
                self.resume_cwd: str | None = None

            def resume_thread(self, **kwargs):
                self.resume_cwd = kwargs["cwd"]

            def read_thread(self, _thread_id):
                return {"turns": []}

            def run_turn(self, **_kwargs):
                return TurnResult(
                    turn_id="recompose-turn",
                    status="failed",
                    text="",
                    error={"codexErrorInfo": "serverOverloaded"},
                )

        class FakeCore:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def call(self, command: str, *_args: str, input_value=None):
                self.calls.append(command)
                return {"status": "ACTIVE"}

        transport = FakeTransport()
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="recomposition-cwd-recovery",
            core=FakeCore(),
            transport=transport,
            project_root=ROOT,
        )
        prompt = coordinator._prompt(action, "builder", context)
        pending = {
            "action": action["action"],
            "action_id": action["action_id"],
            "role": "builder",
            "thread_id": "builder-thread",
            "prompt_digest": digest(prompt),
            "output_schema_digest": coordinator.output_schema_digest,
            "state": "prepared",
            "attempt": 2,
            "generation": 1,
        }
        self.assertIsNone(
            coordinator._recover_dispatch(pending, "builder", context, action)
        )
        self.assertEqual(transport.resume_cwd, "/rebuilt-builder-worktree")

    def test_machine_failure_signature_binds_stdout_and_stderr(self) -> None:
        result = {
            "id": "fixture",
            "argv": ["python3", "verify.py"],
            "returncode": 1,
            "timed_out": False,
            "executable_identity": {"path": "/usr/bin/python3"},
            "stdout": "first observed output",
            "stderr": "same error",
        }
        original = assurance_core._machine_failure_signature("machine", [result])
        changed_stdout = assurance_core._machine_failure_signature(
            "machine", [{**result, "stdout": "different observed output"}]
        )
        changed_stderr = assurance_core._machine_failure_signature(
            "machine", [{**result, "stderr": "different error"}]
        )
        self.assertNotEqual(original, changed_stdout)
        self.assertNotEqual(original, changed_stderr)

    def test_tester_replacement_replays_and_resolves_on_first_new_turn(self) -> None:
        run_id = "native-tester-replacement"
        run_path, old_source, _ = self.prepare_replacement_fixture(run_id)
        ledger = json.loads((run_path / "ledger.json").read_text())
        candidate_head = ledger["facets"]["execution"]["candidate_head"]
        assurance_core.record_evidence(
            self.repo,
            run_id,
            "tester",
            {
                "schema_version": 1,
                "kind": "tester",
                "status": "pass",
                "candidate_head": candidate_head,
                "producer": {"role": "tester", **old_source["agent"]},
                "details": {
                    "result": "tests_ready",
                    "source_head": old_source["head"],
                    "files": old_source["files"],
                },
            },
        )
        assurance_core.record_problems(
            self.repo,
            run_id,
            {
                "schema_version": 1,
                "problems": [
                    {
                        "key": "tester-read-boundary-violated",
                        "summary": "Tester identity lost independence",
                        "details": "The current Tester read unpublished implementation.",
                        "owner": "tester",
                        "producer_continuity": "invalid",
                    }
                ],
            },
            role="tester",
            agent_id="tester-old",
            thread_id="tester-old-thread",
        )
        replace = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.assertEqual(replace["action"], "replace_tester")
        self.invoke(
            "begin-tester-replacement",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            replace["action_id"],
            "--problem-key",
            "tester-read-boundary-violated",
            "--driver-runtime-kind",
            "native",
        )
        prepared = json.loads((run_path / "ledger.json").read_text())[
            "tester_replacement_intent"
        ]
        self.assertEqual(prepared["stage"], "prepared")
        self.assertTrue(Path(prepared["worktree"]).is_dir())
        self.invoke(
            "begin-tester-replacement",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            replace["action_id"],
            "--problem-key",
            "tester-read-boundary-violated",
            "--driver-runtime-kind",
            "native",
        )
        self.invoke(
            "bind-tester-replacement",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            replace["action_id"],
            "--agent-id",
            "tester-new",
            "--thread-id",
            "tester-new-thread",
            "--driver-runtime-kind",
            "native",
        )
        self.invoke(
            "complete-tester-replacement",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            replace["action_id"],
            "--driver-runtime-kind",
            "native",
        )
        self.invoke(
            "complete-tester-replacement",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            replace["action_id"],
            "--driver-runtime-kind",
            "native",
        )
        switched = json.loads((run_path / "ledger.json").read_text())
        self.assertEqual(
            switched["tester_replacement_intent"]["stage"],
            "awaiting_first_turn",
        )
        self.assertFalse(Path(old_source["worktree"]).exists())
        self.assertEqual(
            assurance_core.evidence_state(switched, "tester"), "stale"
        )
        problem = next(
            item
            for item in switched["problems"]
            if item["key"] == "tester-read-boundary-violated"
        )
        self.assertEqual(problem["status"], "open")
        author = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.assertEqual(author["action"], "tester_author")
        self.invoke(
            "begin-dispatch",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            author["action_id"],
            "--action",
            author["action"],
            "--role",
            "tester",
            "--thread-id",
            "tester-new-thread",
            "--prompt-digest",
            "b" * 64,
            "--output-schema-digest",
            "c" * 64,
        )
        self.invoke(
            "bind-dispatch-turn",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            author["action_id"],
            "--turn-id",
            "tester-new-first-turn",
        )
        bound = json.loads((run_path / "ledger.json").read_text())
        self.assertIsNone(bound["tester_replacement_intent"])
        problem = next(
            item
            for item in bound["problems"]
            if item["key"] == "tester-read-boundary-violated"
        )
        self.assertEqual(problem["status"], "resolved")

    def test_tester_replacement_recreates_missing_worktree_from_persisted_branch(
        self,
    ) -> None:
        run_id = "native-tester-replacement-worktree-replay"
        run_path, _old_source, _ = self.prepare_replacement_fixture(run_id)
        assurance_core.record_problems(
            self.repo,
            run_id,
            {
                "schema_version": 1,
                "problems": [
                    {
                        "key": "tester-replacement-worktree-replay",
                        "summary": "Tester identity lost independence",
                        "details": "The replacement source must replay after interrupted creation.",
                        "owner": "tester",
                        "producer_continuity": "invalid",
                    }
                ],
            },
            role="tester",
            agent_id="tester-old",
            thread_id="tester-old-thread",
        )
        replace = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.invoke(
            "begin-tester-replacement",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            replace["action_id"],
            "--problem-key",
            "tester-replacement-worktree-replay",
            "--driver-runtime-kind",
            "native",
        )
        intent = json.loads((run_path / "ledger.json").read_text())[
            "tester_replacement_intent"
        ]
        replacement_worktree = Path(intent["worktree"])
        branch_head = run_process(
            ["git", "-C", str(self.repo), "rev-parse", intent["branch"]]
        ).stdout.strip()
        self.assertEqual(branch_head, intent["source_base_head"])
        removed = run_process(
            ["git", "-C", str(self.repo), "worktree", "remove", str(replacement_worktree)]
        )
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(replacement_worktree.exists())

        self.invoke(
            "begin-tester-replacement",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            replace["action_id"],
            "--problem-key",
            "tester-replacement-worktree-replay",
            "--driver-runtime-kind",
            "native",
        )

        self.assertTrue(replacement_worktree.is_dir())
        replayed_head = run_process(
            ["git", "-C", str(replacement_worktree), "rev-parse", "HEAD"]
        ).stdout.strip()
        self.assertEqual(replayed_head, intent["source_base_head"])

    def test_tester_replacement_recreates_exact_registered_missing_worktree(
        self,
    ) -> None:
        run_id = "native-tester-replacement-registered-worktree-replay"
        run_path, _old_source, _ = self.prepare_replacement_fixture(run_id)
        assurance_core.record_problems(
            self.repo,
            run_id,
            {
                "schema_version": 1,
                "problems": [
                    {
                        "key": "tester-replacement-registered-replay",
                        "summary": "Tester identity lost independence",
                        "details": "The exact registered replacement worktree must be replayable.",
                        "owner": "tester",
                        "producer_continuity": "invalid",
                    }
                ],
            },
            role="tester",
            agent_id="tester-old",
            thread_id="tester-old-thread",
        )
        replace = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.invoke(
            "begin-tester-replacement",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            replace["action_id"],
            "--problem-key",
            "tester-replacement-registered-replay",
            "--driver-runtime-kind",
            "native",
        )
        intent = json.loads((run_path / "ledger.json").read_text())[
            "tester_replacement_intent"
        ]
        replacement_worktree = Path(intent["worktree"])
        moved = replacement_worktree.with_name(replacement_worktree.name + "-missing")
        replacement_worktree.rename(moved)
        self.assertFalse(replacement_worktree.exists())

        self.invoke(
            "begin-tester-replacement",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            replace["action_id"],
            "--problem-key",
            "tester-replacement-registered-replay",
            "--driver-runtime-kind",
            "native",
        )

        self.assertTrue(replacement_worktree.is_dir())
        replayed_head = run_process(
            ["git", "-C", str(replacement_worktree), "rev-parse", "HEAD"]
        ).stdout.strip()
        self.assertEqual(replayed_head, intent["source_base_head"])

    def test_tester_replacement_replays_source_switch_before_retired_cleanup(
        self,
    ) -> None:
        run_id = "native-tester-replacement-source-switch-replay"
        run_path, old_source, _ = self.prepare_replacement_fixture(run_id)
        assurance_core.record_problems(
            self.repo,
            run_id,
            {
                "schema_version": 1,
                "problems": [
                    {
                        "key": "tester-replacement-source-switch-replay",
                        "summary": "Tester identity lost independence",
                        "details": "Source cleanup must replay only after the new source is exact.",
                        "owner": "tester",
                        "producer_continuity": "invalid",
                    }
                ],
            },
            role="tester",
            agent_id="tester-old",
            thread_id="tester-old-thread",
        )
        replace = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.invoke(
            "begin-tester-replacement",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            replace["action_id"],
            "--problem-key",
            "tester-replacement-source-switch-replay",
            "--driver-runtime-kind",
            "native",
        )
        self.invoke(
            "bind-tester-replacement",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            replace["action_id"],
            "--agent-id",
            "tester-source-switch",
            "--thread-id",
            "tester-source-switch-thread",
            "--driver-runtime-kind",
            "native",
        )
        original_remove = assurance_core.git

        def interrupt_old_source_remove(repo, *args, **kwargs):
            if args[:3] == ("worktree", "remove", old_source["worktree"]):
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="simulated interruption",
                )
            return original_remove(repo, *args, **kwargs)

        with patch.object(assurance_core, "git", side_effect=interrupt_old_source_remove):
            with self.assertRaises(assurance_core.AssuranceError) as interrupted:
                assurance_core.complete_tester_replacement(
                    self.repo,
                    run_id,
                    action_id=replace["action_id"],
                    driver_runtime_kind="native",
                )
        self.assertEqual(
            interrupted.exception.code,
            "TESTER_RETIRED_CLEANUP_PENDING",
        )
        preserved_branch = run_process(
            ["git", "-C", str(self.repo), "rev-parse", old_source["branch"]]
        )
        self.assertEqual(preserved_branch.returncode, 0, preserved_branch.stderr)
        self.assertEqual(preserved_branch.stdout.strip(), old_source["head"])
        switched = json.loads((run_path / "ledger.json").read_text())
        switched["problems"].append(
            {
                "key": "independent-builder-problem",
                "summary": "An independent Builder issue remains open",
                "details": "Tester replacement must not own unrelated problem lifecycle.",
                "owner": "builder",
                "status": "open",
                "producer": {
                    "role": "builder",
                    "agent_id": "builder-agent",
                    "thread_id": "builder-thread",
                },
                "candidate_head": switched["facets"]["execution"]["candidate_head"],
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        (run_path / "ledger.json").write_text(
            json.dumps(switched, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        intent = switched["tester_replacement_intent"]
        self.assertEqual(intent["stage"], "source_switched")
        replacement_worktree = Path(intent["worktree"])
        removed = run_process(
            ["git", "-C", str(self.repo), "worktree", "remove", str(replacement_worktree)]
        )
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertTrue(Path(old_source["worktree"]).is_dir())

        self.invoke(
            "complete-tester-replacement",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            replace["action_id"],
            "--driver-runtime-kind",
            "native",
        )

        replayed = json.loads((run_path / "ledger.json").read_text())
        self.assertEqual(
            replayed["tester_replacement_intent"]["stage"],
            "awaiting_first_turn",
        )
        self.assertTrue(replacement_worktree.is_dir())
        self.assertFalse(Path(old_source["worktree"]).exists())

    def test_tester_replacement_bootstrap_stops_after_third_loss(self) -> None:
        run_id = "native-tester-bootstrap-loss"
        run_path, _old_source, _ = self.prepare_replacement_fixture(run_id)
        assurance_core.record_problems(
            self.repo,
            run_id,
            {
                "schema_version": 1,
                "problems": [
                    {
                        "key": "tester-bootstrap-invalid",
                        "summary": "Tester identity lost independence",
                        "details": "The current Tester cannot remain the author.",
                        "owner": "tester",
                        "producer_continuity": "invalid",
                    }
                ],
            },
            role="tester",
            agent_id="tester-old",
            thread_id="tester-old-thread",
        )
        replace = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        for command in ("begin-tester-replacement",):
            self.invoke(
                command,
                "--repo",
                self.repo,
                "--run",
                run_id,
                "--action-id",
                replace["action_id"],
                "--problem-key",
                "tester-bootstrap-invalid",
                "--driver-runtime-kind",
                "native",
            )
        self.invoke(
            "bind-tester-replacement",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            replace["action_id"],
            "--agent-id",
            "tester-bootstrap-1",
            "--thread-id",
            "tester-bootstrap-thread-1",
            "--driver-runtime-kind",
            "native",
        )
        self.invoke(
            "complete-tester-replacement",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            replace["action_id"],
            "--driver-runtime-kind",
            "native",
        )
        for loss, identity in ((1, 2), (2, 3)):
            self.invoke(
                "begin-tester-replacement",
                "--repo",
                self.repo,
                "--run",
                run_id,
                "--action-id",
                replace["action_id"],
                "--problem-key",
                "tester-bootstrap-invalid",
                "--renew-bootstrap",
                "--driver-runtime-kind",
                "native",
            )
            pending_bind = self.invoke(
                "driver-next", "--repo", self.repo, "--run", run_id
            )
            self.assertEqual(pending_bind["action"], "replace_tester")
            self.assertEqual(pending_bind["action_id"], replace["action_id"])
            self.assertIsNone(pending_bind["replacement"]["new_agent"])
            self.invoke(
                "bind-tester-replacement",
                "--repo",
                self.repo,
                "--run",
                run_id,
                "--action-id",
                replace["action_id"],
                "--agent-id",
                f"tester-bootstrap-{identity}",
                "--thread-id",
                f"tester-bootstrap-thread-{identity}",
                "--driver-runtime-kind",
                "native",
            )
            ledger = json.loads((run_path / "ledger.json").read_text())
            self.assertEqual(
                ledger["tester_replacement_intent"]["bootstrap_attempt"],
                loss + 1,
            )
        exhausted = run_process(
            [
                sys.executable,
                CLI,
                "assurance",
                "--experimental-v4",
                "begin-tester-replacement",
                "--repo",
                self.repo,
                "--run",
                run_id,
                "--action-id",
                replace["action_id"],
                "--problem-key",
                "tester-bootstrap-invalid",
                "--renew-bootstrap",
                "--driver-runtime-kind",
                "native",
            ]
        )
        self.assertNotEqual(exhausted.returncode, 0)
        self.assertEqual(
            json.loads(exhausted.stdout)["code"],
            "TESTER_REPLACEMENT_ARCHITECTURE_REVIEW_REQUIRED",
        )
        ledger = json.loads((run_path / "ledger.json").read_text())
        self.assertEqual(
            ledger["tester_replacement_intent"]["new_agent"]["thread_id"],
            "tester-bootstrap-thread-3",
        )
        decision = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.assertEqual(decision["action"], "architecture_review")

    def test_replacement_bootstrap_failure_consumes_pending_dispatch(self) -> None:
        run_id = "native-tester-bootstrap-pending-dispatch"
        run_path, _old_source, _ = self.prepare_replacement_fixture(run_id)
        assurance_core.record_problems(
            self.repo,
            run_id,
            {
                "schema_version": 1,
                "problems": [
                    {
                        "key": "tester-bootstrap-pending-dispatch",
                        "summary": "Tester identity lost independence",
                        "details": "The replacement bootstrap must own its pending dispatch.",
                        "owner": "tester",
                        "producer_continuity": "invalid",
                    }
                ],
            },
            role="tester",
            agent_id="tester-old",
            thread_id="tester-old-thread",
        )
        replace = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.invoke(
            "begin-tester-replacement",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            replace["action_id"],
            "--problem-key",
            "tester-bootstrap-pending-dispatch",
            "--driver-runtime-kind",
            "native",
        )
        self.invoke(
            "bind-tester-replacement",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            replace["action_id"],
            "--agent-id",
            "tester-replacement",
            "--thread-id",
            "tester-replacement-thread",
            "--driver-runtime-kind",
            "native",
        )
        self.invoke(
            "complete-tester-replacement",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            replace["action_id"],
            "--driver-runtime-kind",
            "native",
        )
        author = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.assertEqual(author["action"], "tester_author")
        self.invoke(
            "begin-dispatch",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            author["action_id"],
            "--action",
            author["action"],
            "--role",
            "tester",
            "--thread-id",
            "tester-replacement-thread",
            "--prompt-digest",
            "b" * 64,
            "--output-schema-digest",
            "c" * 64,
            "--driver-runtime-kind",
            "native",
        )

        assurance_core.begin_tester_replacement(
            self.repo,
            run_id,
            action_id=replace["action_id"],
            problem_key="tester-bootstrap-pending-dispatch",
            driver_runtime_kind="native",
            renew_bootstrap=True,
        )

        ledger = json.loads((run_path / "ledger.json").read_text())
        self.assertIsNone(ledger["dispatch_intent"])
        self.assertEqual(
            ledger["tester_replacement_intent"]["bootstrap_attempt"], 2
        )
        self.assertIsNone(ledger["tester_replacement_intent"]["new_agent"])
        self.assertTrue(
            any(
                event["kind"]
                == "tester_replacement_bootstrap_dispatch_consumed"
                for event in ledger["events"]
            )
        )

    def test_tester_replacement_rejects_bootstrap_renewal_after_first_turn(self) -> None:
        run_id = "native-tester-bootstrap-after-turn"
        run_path, _old_source, _ = self.prepare_replacement_fixture(run_id)
        assurance_core.record_problems(
            self.repo,
            run_id,
            {
                "schema_version": 1,
                "problems": [
                    {
                        "key": "tester-bootstrap-after-turn",
                        "summary": "Tester identity lost independence",
                        "details": "Replacement must stop renewing after its first turn.",
                        "owner": "tester",
                        "producer_continuity": "invalid",
                    }
                ],
            },
            role="tester",
            agent_id="tester-old",
            thread_id="tester-old-thread",
        )
        replace = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.invoke(
            "begin-tester-replacement",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            replace["action_id"],
            "--problem-key",
            "tester-bootstrap-after-turn",
            "--driver-runtime-kind",
            "native",
        )
        self.invoke(
            "bind-tester-replacement",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            replace["action_id"],
            "--agent-id",
            "tester-after-turn",
            "--thread-id",
            "tester-after-turn-thread",
            "--driver-runtime-kind",
            "native",
        )
        self.invoke(
            "complete-tester-replacement",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            replace["action_id"],
            "--driver-runtime-kind",
            "native",
        )
        author = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.invoke(
            "begin-dispatch",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            author["action_id"],
            "--action",
            author["action"],
            "--role",
            "tester",
            "--thread-id",
            "tester-after-turn-thread",
            "--prompt-digest",
            "b" * 64,
            "--output-schema-digest",
            "c" * 64,
        )
        self.invoke(
            "bind-dispatch-turn",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            author["action_id"],
            "--turn-id",
            "tester-after-turn-first",
        )
        ledger = json.loads((run_path / "ledger.json").read_text())
        self.assertIsNone(ledger["tester_replacement_intent"])
        renewed = run_process(
            [
                sys.executable,
                CLI,
                "assurance",
                "--experimental-v4",
                "begin-tester-replacement",
                "--repo",
                self.repo,
                "--run",
                run_id,
                "--action-id",
                replace["action_id"],
                "--problem-key",
                "tester-bootstrap-after-turn",
                "--renew-bootstrap",
                "--driver-runtime-kind",
                "native",
            ]
        )
        self.assertNotEqual(renewed.returncode, 0)
        self.assertEqual(
            json.loads(renewed.stdout)["code"],
            "TESTER_REPLACEMENT_ACTION_STALE",
        )

    def test_tester_replacement_first_turn_fails_closed_on_problem_drift(self) -> None:
        run_id = "native-tester-first-turn-drift"
        run_path, _old_source, _ = self.prepare_replacement_fixture(run_id)
        assurance_core.record_problems(
            self.repo,
            run_id,
            {
                "schema_version": 1,
                "problems": [
                    {
                        "key": "tester-first-turn-drift",
                        "summary": "Tester identity lost independence",
                        "details": "First turn must retain the frozen problem snapshot.",
                        "owner": "tester",
                        "producer_continuity": "invalid",
                    }
                ],
            },
            role="tester",
            agent_id="tester-old",
            thread_id="tester-old-thread",
        )
        replace = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.invoke(
            "begin-tester-replacement",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            replace["action_id"],
            "--problem-key",
            "tester-first-turn-drift",
            "--driver-runtime-kind",
            "native",
        )
        self.invoke(
            "bind-tester-replacement",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            replace["action_id"],
            "--agent-id",
            "tester-drift",
            "--thread-id",
            "tester-drift-thread",
            "--driver-runtime-kind",
            "native",
        )
        self.invoke(
            "complete-tester-replacement",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            replace["action_id"],
            "--driver-runtime-kind",
            "native",
        )
        author = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.invoke(
            "begin-dispatch",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            author["action_id"],
            "--action",
            author["action"],
            "--role",
            "tester",
            "--thread-id",
            "tester-drift-thread",
            "--prompt-digest",
            "b" * 64,
            "--output-schema-digest",
            "c" * 64,
        )
        ledger_path = run_path / "ledger.json"
        drifted = json.loads(ledger_path.read_text())
        replacement_problem = next(
            item
            for item in drifted["problems"]
            if item["key"] == "tester-first-turn-drift"
        )
        replacement_problem["details"] = (
            "The continuity-invalid Tester problem changed after replacement began."
        )
        ledger_path.write_text(
            json.dumps(drifted, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        bound = run_process(
            [
                sys.executable,
                CLI,
                "assurance",
                "--experimental-v4",
                "bind-dispatch-turn",
                "--repo",
                self.repo,
                "--run",
                run_id,
                "--action-id",
                author["action_id"],
                "--turn-id",
                "tester-drift-first",
            ]
        )
        self.assertNotEqual(bound.returncode, 0)
        self.assertEqual(
            json.loads(bound.stdout)["code"],
            "TESTER_REPLACEMENT_PROBLEM_DRIFT",
        )
        preserved = json.loads(ledger_path.read_text())
        self.assertEqual(preserved["dispatch_intent"]["state"], "prepared")
        self.assertNotIn("turn_id", preserved["dispatch_intent"])
        self.assertIsNotNone(preserved["tester_replacement_intent"])

    def test_native_cli_persists_unhandled_fatal_after_run_creation(self) -> None:
        class FakeCore:
            def __init__(self) -> None:
                self.calls: list[tuple[str, tuple[str, ...], object | None]] = []

            def start(self, **_kwargs):
                return {"candidate_worktree": str(self.repo / "candidate")}

            def call(self, command: str, *args: str, input_value=None):
                self.calls.append((command, args, input_value))
                if command == "record-driver-failure":
                    return {
                        "status": "ACTIVE",
                        "phase": "active",
                        "driver_failure": {"state": "recorded"},
                    }
                if command == "complete-driver-failure":
                    return {
                        "status": "FATAL",
                        "phase": "failed",
                        "driver_failure": {"state": "terminal"},
                    }
                raise AssertionError(command)

        class FakeTransport:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class FatalCoordinator:
            current_action = {
                "action_id": "a" * 64,
                "action": "checkpoint_builder",
                "reason": "committed_candidate_uncheckpointed",
            }

            def __init__(self, **_kwargs):
                pass

            def run(self):
                raise CorePortError(
                    "published prerequisite drifted",
                    payload={
                        "status": "FATAL",
                        "code": "PUBLISHED_PREREQUISITE_DRIFT",
                        "message": "published prerequisite drifted",
                        "details": {"path": "agents/reviewer.toml"},
                    },
                    returncode=2,
                )

        fake_core = FakeCore()
        fake_core.repo = self.repo
        contract_path = self.artifacts / "native-cli-fatal-contract.json"
        contract_path.write_text(json.dumps(native_contract(self.repo)), encoding="utf-8")
        output = StringIO()
        with (
            patch.object(native_cli, "CorePort", return_value=fake_core),
            patch.object(
                native_cli,
                "probe_app_server",
                return_value=SimpleNamespace(
                    runtime_version="codex-test",
                    protocol_schema_digest="b" * 64,
                ),
            ),
            patch.object(native_cli, "AppServerTransport", return_value=FakeTransport()),
            patch.object(native_cli, "NativeCoordinator", FatalCoordinator),
            redirect_stdout(output),
        ):
            rc = native_cli.main(
                [
                    "start",
                    "--repo",
                    str(self.repo),
                    "--run",
                    "native-cli-fatal",
                    "--session-id",
                    "native-cli-fatal-session",
                    "--contract",
                    str(contract_path),
                ]
            )

        self.assertEqual(rc, 2)
        commands = [command for command, _args, _input in fake_core.calls]
        self.assertEqual(
            commands, ["record-driver-failure", "complete-driver-failure"]
        )
        recorded_failure = fake_core.calls[0][2]
        self.assertEqual(recorded_failure["code"], "PUBLISHED_PREREQUISITE_DRIFT")
        self.assertEqual(
            recorded_failure["action"]["action"], "checkpoint_builder"
        )
        payload = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(payload["status"], "FATAL")
        self.assertEqual(payload["phase"], "failed")

    def test_native_cli_admission_blocker_creates_no_agent_thread_or_turn(self) -> None:
        class BlockedCore:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def start(self, **_kwargs):
                raise CorePortError(
                    "host executable is unavailable",
                    payload={
                        "status": "FAIL",
                        "code": "ASSURANCE_ADMISSION_BLOCKED",
                        "message": "host executable is unavailable",
                        "admission": {
                            "schema_version": 1,
                            "status": "BLOCKED",
                            "trusted_system_path": "/usr/local/bin:/usr/bin:/bin",
                            "commands": [],
                            "public_prerequisites": [],
                        },
                    },
                    returncode=1,
                )

            def call(self, command: str, *_args: str, input_value=None):
                self.calls.append(command)
                raise AssertionError(command)

        class TrackingTransport:
            def __init__(self) -> None:
                self.agent_calls: list[str] = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def start_thread(self, **_kwargs):
                self.agent_calls.append("start_thread")
                raise AssertionError("admission blocker must precede Agent thread creation")

            def run_turn(self, **_kwargs):
                self.agent_calls.append("run_turn")
                raise AssertionError("admission blocker must precede Agent turn dispatch")

        blocked_core = BlockedCore()
        transport = TrackingTransport()
        coordinator_created = []
        contract_path = self.artifacts / "native-cli-admission-blocked.json"
        contract_path.write_text(
            json.dumps(native_contract(self.repo)), encoding="utf-8"
        )
        output = StringIO()
        with (
            patch.object(native_cli, "CorePort", return_value=blocked_core),
            patch.object(
                native_cli,
                "probe_app_server",
                return_value=SimpleNamespace(
                    runtime_version="codex-test",
                    protocol_schema_digest="b" * 64,
                ),
            ),
            patch.object(native_cli, "AppServerTransport", return_value=transport),
            patch.object(
                native_cli,
                "NativeCoordinator",
                side_effect=lambda **_kwargs: coordinator_created.append(True),
            ),
            redirect_stdout(output),
        ):
            rc = native_cli.main(
                [
                    "start",
                    "--repo",
                    str(self.repo),
                    "--run",
                    "native-cli-admission-blocked",
                    "--session-id",
                    "native-cli-admission-blocked-session",
                    "--contract",
                    str(contract_path),
                ]
            )

        self.assertEqual(rc, 1)
        self.assertEqual(blocked_core.calls, [])
        self.assertEqual(transport.agent_calls, [])
        self.assertEqual(coordinator_created, [])
        payload = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(payload["code"], "ASSURANCE_ADMISSION_BLOCKED")

    def test_native_resume_retries_disconnect_during_app_server_startup(self) -> None:
        action = {
            "driver_protocol_version": 1,
            "status": "CONTINUE",
            "action": "reviewer_preflight",
            "action_id": "a" * 64,
            "reason": "reviewer_preflight_missing",
        }

        class FakeCore:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def call(self, command: str, *args: str, input_value=None):
                self.calls.append(command)
                if command == "driver-context":
                    return {
                        "driver_runtime": {"kind": "native", "protocol_version": 1},
                        "facets": {
                            "execution": {
                                "agents": {
                                    "reviewer": {
                                        "agent_id": "reviewer-agent",
                                        "thread_id": "reviewer-thread",
                                    }
                                }
                            }
                        },
                        "dispatch_intent": {
                            "action": "reviewer_preflight",
                            "action_id": "a" * 64,
                            "attempt": 1,
                            "role": "reviewer",
                            "state": "in_flight",
                            "thread_id": "reviewer-thread",
                            "turn_id": "reviewer-turn",
                        },
                    }
                if command == "driver-next":
                    return action
                if command == "retry-dispatch":
                    return {
                        "status": "ACTIVE",
                        "run_id": "native-resume-startup-disconnect",
                        "phase": "active",
                        "dispatch_intent": {"attempt": 2, "state": "prepared"},
                    }
                raise AssertionError(command)

        class StartupDisconnectTransport:
            def __enter__(self):
                raise AppServerError(
                    "Codex App Server closed its output",
                    code="NATIVE_APP_SERVER_DISCONNECTED",
                )

            def __exit__(self, *_args):
                return False

        fake_core = FakeCore()
        output = StringIO()
        with (
            patch.object(native_cli, "CorePort", return_value=fake_core),
            patch.object(
                native_cli,
                "probe_app_server",
                return_value=SimpleNamespace(
                    runtime_version="codex-test",
                    protocol_schema_digest="b" * 64,
                ),
            ),
            patch.object(
                native_cli,
                "AppServerTransport",
                return_value=StartupDisconnectTransport(),
            ),
            redirect_stdout(output),
        ):
            rc = native_cli.main(
                [
                    "resume",
                    "--repo",
                    str(self.repo),
                    "--run",
                    "native-resume-startup-disconnect",
                ]
            )

        self.assertEqual(rc, 1)
        self.assertNotIn("record-driver-failure", fake_core.calls)
        self.assertEqual(
            fake_core.calls,
            ["driver-context", "driver-next", "driver-context", "retry-dispatch"],
        )
        payload = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(payload["status"], "ACTIVE")
        self.assertEqual(payload["dispatch_intent"]["attempt"], 2)

    def test_native_resume_passes_user_reason_only_for_exhausted_dispatch(self) -> None:
        captured: dict[str, object] = {}

        class FakeCore:
            def call(self, command: str, *args: str, input_value=None):
                self.command = command
                return {
                    "driver_runtime": {"kind": "native", "protocol_version": 1},
                    "dispatch_intent": {
                        "action": "builder_implement",
                        "action_id": "a" * 64,
                        "attempt": 3,
                        "generation": 1,
                        "role": "builder",
                        "state": "exhausted",
                        "thread_id": "builder-thread",
                    },
                }

        class FakeTransport:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class FakeCoordinator:
            current_action = None

            def __init__(self, **kwargs):
                captured.update(kwargs)

            def run(self):
                return {"status": "NEEDS_USER", "run_id": "renewed-run"}

        output = StringIO()
        with (
            patch.object(native_cli, "CorePort", return_value=FakeCore()),
            patch.object(
                native_cli,
                "probe_app_server",
                return_value=SimpleNamespace(
                    runtime_version="codex-test",
                    protocol_schema_digest="b" * 64,
                ),
            ),
            patch.object(native_cli, "AppServerTransport", return_value=FakeTransport()),
            patch.object(native_cli, "NativeCoordinator", FakeCoordinator),
            redirect_stdout(output),
        ):
            rc = native_cli.main(
                [
                    "resume",
                    "--repo",
                    str(self.repo),
                    "--run",
                    "renewed-run",
                    "--reason",
                    "user approved a new dispatch generation",
                ]
            )

        self.assertEqual(rc, 1)
        self.assertEqual(
            captured["dispatch_renewal_reason"],
            "user approved a new dispatch generation",
        )

    def test_native_resume_rearms_unknown_activation_without_new_generation(self) -> None:
        captured: dict[str, object] = {}
        calls: list[tuple[str, tuple[str, ...]]] = []
        action_id = "a" * 64

        class FakeCore:
            def call(self, command: str, *args: str, input_value=None):
                calls.append((command, args))
                if command == "driver-context":
                    return {
                        "driver_runtime": {"kind": "native", "protocol_version": 1},
                        "dispatch_intent": {
                            "action": "tester_author",
                            "action_id": action_id,
                            "attempt": 2,
                            "generation": 1,
                            "role": "tester",
                            "state": "prepared",
                            "thread_id": "tester-thread",
                            "activation_state": "unknown",
                            "activation_failure_code": "NATIVE_THREAD_RESUME_FAILED",
                        },
                    }
                if command == "record-dispatch-activation":
                    return {"status": "ACTIVE"}
                raise AssertionError(command)

        class FakeTransport:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class FakeCoordinator:
            current_action = None

            def __init__(self, **kwargs):
                captured.update(kwargs)

            def run(self):
                return {"status": "NEEDS_USER", "run_id": "activation-rearmed"}

        output = StringIO()
        with (
            patch.object(native_cli, "CorePort", return_value=FakeCore()),
            patch.object(
                native_cli,
                "probe_app_server",
                return_value=SimpleNamespace(
                    runtime_version="codex-test",
                    protocol_schema_digest="b" * 64,
                ),
            ),
            patch.object(native_cli, "AppServerTransport", return_value=FakeTransport()),
            patch.object(native_cli, "NativeCoordinator", FakeCoordinator),
            redirect_stdout(output),
        ):
            rc = native_cli.main(
                [
                    "resume",
                    "--repo",
                    str(self.repo),
                    "--run",
                    "activation-rearmed",
                    "--reason",
                    "verified that no turn was started",
                ]
            )

        self.assertEqual(rc, 1)
        self.assertEqual(captured["dispatch_renewal_reason"], None)
        self.assertEqual(
            [command for command, _args in calls],
            ["driver-context", "record-dispatch-activation", "driver-context"],
        )
        activation_args = calls[1][1]
        self.assertIn("--state", activation_args)
        self.assertIn("pending", activation_args)
        self.assertIn("--reason", activation_args)
        self.assertIn("verified that no turn was started", activation_args)

    def test_native_resume_authorizes_tester_architecture_review_without_renewal(
        self,
    ) -> None:
        captured: dict[str, object] = {}
        calls: list[tuple[str, tuple[str, ...]]] = []

        class FakeCore:
            def call(self, command: str, *args: str, input_value=None):
                calls.append((command, args))
                if command == "driver-context":
                    return {
                        "driver_runtime": {"kind": "native", "protocol_version": 1},
                        "dispatch_intent": None,
                    }
                if command == "driver-next":
                    return {
                        "status": "NEEDS_USER",
                        "action": "architecture_review",
                        "reason": "tester_correction_limit_reached",
                        "action_id": "a" * 64,
                        "tester_correction_review_binding": "b" * 64,
                    }
                if command == "authorize-tester-correction":
                    return {"status": "ACTIVE"}
                raise AssertionError(command)

        class FakeTransport:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class FakeCoordinator:
            current_action = None

            def __init__(self, **kwargs):
                captured.update(kwargs)

            def run(self):
                return {"status": "NEEDS_USER", "run_id": "architecture-run"}

        output = StringIO()
        with (
            patch.object(native_cli, "CorePort", return_value=FakeCore()),
            patch.object(
                native_cli,
                "probe_app_server",
                return_value=SimpleNamespace(
                    runtime_version="codex-test",
                    protocol_schema_digest="b" * 64,
                ),
            ),
            patch.object(native_cli, "AppServerTransport", return_value=FakeTransport()),
            patch.object(native_cli, "NativeCoordinator", FakeCoordinator),
            redirect_stdout(output),
        ):
            rc = native_cli.main(
                [
                    "resume",
                    "--repo",
                    str(self.repo),
                    "--run",
                    "architecture-run",
                    "--reason",
                    "user approved one exact correction",
                ]
            )

        self.assertEqual(rc, 1)
        self.assertIsNone(captured["dispatch_renewal_reason"])
        authorize_call = next(item for item in calls if item[0] == "authorize-tester-correction")
        self.assertIn("--allow-runtime-transition", authorize_call[1])
        self.assertIn("a" * 64, authorize_call[1])
        self.assertIn("b" * 64, authorize_call[1])

    def test_native_cli_reports_finalized_when_fatal_finalize_recovery_succeeds(
        self,
    ) -> None:
        class FakeCore:
            def call(self, command: str, *args: str, input_value=None):
                if command == "record-driver-failure":
                    return {"status": "ACTIVE", "phase": "finalizing"}
                if command == "complete-driver-failure":
                    return {
                        "status": "READY",
                        "phase": "finalized",
                        "driver_failure": {
                            "state": "recovered",
                            "recovery": "finalize",
                        },
                    }
                raise AssertionError(command)

        payload, rc = native_cli._persist_fatal(
            core=FakeCore(),
            repo=self.repo,
            run_id="native-finalize-recovered",
            exc=CorePortError(
                "finalize interrupted",
                payload={
                    "status": "FATAL",
                    "code": "FINALIZE_INTERRUPTED",
                    "message": "finalize interrupted",
                },
                returncode=2,
            ),
            coordinator=None,
        )

        self.assertEqual(rc, 0)
        self.assertEqual(payload["status"], "FINALIZED")
        self.assertEqual(payload["driver_failure"]["state"], "recovered")
        self.assertEqual(
            payload["recovered_failure"]["code"], "FINALIZE_INTERRUPTED"
        )


class AppServerTransportContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="fake-app-server-")
        self.root = Path(self.tmp.name)
        self.codex = self.root / "codex"
        self.codex.write_text(
            """#!/usr/bin/env python3
import json, os, sys
if sys.argv[1:] == ['--version']:
    print('codex-cli fake-native')
    raise SystemExit(0)
if sys.argv[1:3] == ['app-server', 'generate-json-schema']:
    out = sys.argv[sys.argv.index('--out') + 1]
    os.makedirs(out, exist_ok=True)
    tokens = ['thread/start','thread/resume','thread/read','thread/compact/start','turn/start','turn/interrupt','developerInstructions','outputSchema','clientUserMessageId']
    open(os.path.join(out, 'codex_app_server_protocol.schemas.json'), 'w').write(json.dumps(tokens))
    raise SystemExit(0)
if sys.argv[1:3] != ['app-server', '--stdio']:
    raise SystemExit(2)
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get('method')
    if method == 'initialize':
        print(json.dumps({'id': msg['id'], 'result': {'userAgent': 'fake'}}), flush=True)
    elif method == 'initialized':
        pass
    elif method == 'thread/start':
        print(json.dumps({'id': msg['id'], 'result': {'thread': {'id': 'thr-native'}}}), flush=True)
    elif method == 'thread/resume':
        print(json.dumps({'id': msg['id'], 'result': {'thread': {'id': msg['params']['threadId']}}}), flush=True)
    elif method == 'thread/read':
        print(json.dumps({'id': msg['id'], 'result': {'thread': {'id': msg['params']['threadId'], 'turns': []}}}), flush=True)
    elif method == 'thread/compact/start':
        print(json.dumps({'method':'item/started','params':{'threadId':'thr-native','turnId':'turn-compact','startedAtMs':1,'item':{'id':'item-compact','type':'contextCompaction'}}}), flush=True)
        print(json.dumps({'id': msg['id'], 'result': {}}), flush=True)
        print(json.dumps({'method':'item/completed','params':{'threadId':'thr-native','turnId':'turn-compact','completedAtMs':2,'item':{'id':'item-compact','type':'contextCompaction'}}}), flush=True)
        print(json.dumps({'method':'turn/completed','params':{'threadId':'thr-native','turn':{'id':'turn-compact','status':'completed','items':[{'id':'item-compact','type':'contextCompaction'}]}}}), flush=True)
    elif method == 'turn/start':
        result = {'result':'implemented','evidence_report':None,'proof_spec':None,'problem_report':None}
        print(json.dumps({'id': msg['id'], 'result': {'turn': {'id': 'turn-native'}}}), flush=True)
        print(json.dumps({'method':'item/completed','params':{'threadId':'thr-native','item':{'id':'item-1','type':'agentMessage','text':json.dumps(result)}}}), flush=True)
        print(json.dumps({'method':'turn/completed','params':{'threadId':'thr-native','turn':{'id':'turn-native','status':'completed','items':[]}}}), flush=True)
""",
            encoding="utf-8",
        )
        self.codex.chmod(0o755)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_native_doctor_reports_legacy_transport_without_mutation(self) -> None:
        class FakeCore:
            def call(self, command: str, *args: str, input_value=None):
                self.command = command
                return {
                    "run_id": "doctor-run",
                    "driver_runtime": {"kind": "native", "protocol_version": 1},
                    "dispatch_intent": None,
                    "transport_cleanup_intent": None,
                    "deferred_wait": None,
                }

        output = StringIO()
        fake_core = FakeCore()
        with (
            patch.object(native_cli, "CorePort", return_value=fake_core),
            redirect_stdout(output),
        ):
            rc = native_cli.main(
                [
                    "doctor",
                    "--repo",
                    str(self.root),
                    "--run",
                    "doctor-run",
                ]
            )
        self.assertEqual(rc, 0)
        payload = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(payload["diagnostic_state"], "legacy-unbound")
        self.assertEqual(fake_core.command, "driver-context")

    def test_probe_and_thread_turn_use_versioned_native_protocol(self) -> None:
        capability = probe_app_server(str(self.codex))
        self.assertEqual(capability.runtime_version, "codex-cli fake-native")
        self.assertTrue(capability.thread_compaction)
        with AppServerTransport(codex_bin=str(self.codex)) as transport:
            thread_id = transport.start_thread(
                cwd=str(self.root), developer_instructions="role", sandbox="workspace-write"
            )
            turn = transport.run_turn(
                thread_id=thread_id,
                prompt="implement",
                output_schema={"type": "object"},
                action_id="d" * 64,
            )
        self.assertEqual(thread_id, "thr-native")
        self.assertEqual(turn.status, "completed")
        self.assertEqual(json.loads(turn.text)["result"], "implemented")

    def test_stderr_is_drained_bounded_and_redacted_in_diagnostic_receipt(self) -> None:
        codex = self.root / "codex-stderr"
        codex.write_text(
            """#!/usr/bin/env python3
import json, os, sys
if sys.argv[1:] == ['--version']:
    print('codex-cli fake-stderr')
    raise SystemExit(0)
if sys.argv[1:3] == ['app-server', 'generate-json-schema']:
    out = sys.argv[sys.argv.index('--out') + 1]
    os.makedirs(out, exist_ok=True)
    open(os.path.join(out, 'codex_app_server_protocol.schemas.json'), 'w').write(
        json.dumps(['thread/start','thread/resume','thread/read','turn/start',
                    'turn/interrupt','developerInstructions','outputSchema',
                    'clientUserMessageId'])
    )
    raise SystemExit(0)
if sys.argv[1:3] != ['app-server', '--stdio']:
    raise SystemExit(2)
sys.stderr.write('Authorization: Bearer super-secret-token\\n')
sys.stderr.write('OPENAI_API_KEY=sk-proj-secret-token\\n')
sys.stderr.write('x' * (256 * 1024))
sys.stderr.flush()
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get('method') == 'initialize':
        print(json.dumps({'id': msg['id'], 'result': {}}), flush=True)
    elif msg.get('method') == 'initialized':
        pass
    elif msg.get('method') == 'thread/start':
        print(json.dumps({'id': msg['id'], 'result': {'thread': {'id': 'thr-stderr'}}}), flush=True)
""",
            encoding="utf-8",
        )
        codex.chmod(0o755)

        transport = AppServerTransport(codex_bin=str(codex))
        with transport:
            self.assertEqual(
                transport.start_thread(
                    cwd=str(self.root),
                    developer_instructions="role",
                    sandbox="danger-full-access",
                ),
                "thr-stderr",
            )
            receipt = transport.diagnostic_receipt(
                failure_code="responseStreamTimeout",
                turn_error={"message": "Bearer another-secret-token"},
            )

        self.assertGreater(receipt["stderr_bytes"], 256 * 1024)
        self.assertTrue(receipt["stderr_truncated"])
        self.assertNotIn("super-secret-token", json.dumps(receipt))
        self.assertNotIn("sk-proj-secret-token", json.dumps(receipt))
        self.assertNotIn("another-secret-token", json.dumps(receipt))
        self.assertIn("[REDACTED]", receipt["stderr_summary"])
        self.assertEqual(
            receipt["receipt_digest"],
            digest({key: value for key, value in receipt.items() if key != "receipt_digest"}),
        )

    def test_transport_uses_explicit_timeout_profile(self) -> None:
        transport = AppServerTransport(codex_bin=str(self.codex))

        self.assertEqual(transport.turn_idle_timeout, TURN_IDLE_TIMEOUT_SECONDS)
        self.assertEqual(transport.turn_idle_timeout, 120.0)
        self.assertEqual(transport.turn_total_timeout, 3600.0)
        self.assertEqual(transport.compaction_total_timeout, 600.0)

    def test_request_uses_longer_initialize_and_control_timeouts(self) -> None:
        transport = AppServerTransport(codex_bin=str(self.codex))

        with (
            patch.object(transport, "_send"),
            patch.object(
                transport,
                "_read",
                return_value={"id": 1, "result": {}},
            ) as read,
        ):
            transport._request("initialize", {})
        read.assert_called_once_with(timeout=INITIALIZE_TIMEOUT_SECONDS)

        with (
            patch.object(transport, "_send"),
            patch.object(
                transport,
                "_read",
                return_value={"id": 2, "result": {}},
            ) as read,
        ):
            transport._next_id = 2
            transport._request("thread/start", {})
        read.assert_called_once_with(timeout=REQUEST_TIMEOUT_SECONDS)

    def test_probe_retries_transient_canary_timeout(self) -> None:
        attempts = 0

        class RetryTransport:
            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def start_thread(self, **_kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise AppServerError(
                        "initialize delayed",
                        code="NATIVE_APP_SERVER_TIMEOUT",
                        details={"timeout_seconds": INITIALIZE_TIMEOUT_SECONDS},
                    )
                return "thread-canary"

        with patch(
            "codex_builder_loop.native_driver.app_server.AppServerTransport",
            RetryTransport,
        ):
            capability = probe_app_server(str(self.codex), strict_protocol=True)

        self.assertEqual(attempts, 2)
        self.assertEqual(capability.protocol_canary_digest is not None, True)

    def test_probe_does_not_label_canary_timeout_as_protocol_incompatible(self) -> None:
        class TimeoutTransport:
            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def start_thread(self, **_kwargs):
                raise AppServerError(
                    "initialize delayed",
                    code="NATIVE_APP_SERVER_TIMEOUT",
                    details={"timeout_seconds": INITIALIZE_TIMEOUT_SECONDS},
                )

        with patch(
            "codex_builder_loop.native_driver.app_server.AppServerTransport",
            TimeoutTransport,
        ):
            with self.assertRaises(AppServerError) as raised:
                probe_app_server(str(self.codex), strict_protocol=True)

        self.assertEqual(raised.exception.code, "NATIVE_DRIVER_PROTOCOL_UNAVAILABLE")
        self.assertEqual(raised.exception.details["canary_code"], "NATIVE_APP_SERVER_TIMEOUT")
        self.assertEqual(raised.exception.details["attempts"], 3)

    def test_strict_protocol_accepts_official_headerless_response_envelope(self) -> None:
        with AppServerTransport(
            codex_bin=str(self.codex), strict_protocol=True
        ) as transport:
            thread_id = transport.start_thread(
                cwd=str(self.root),
                developer_instructions="role",
                sandbox="workspace-write",
            )
        self.assertEqual(thread_id, "thr-native")

    def test_thread_compaction_consumes_deferred_context_item_notifications(self) -> None:
        with AppServerTransport(codex_bin=str(self.codex)) as transport:
            result = transport.compact_thread("thr-native")

        self.assertEqual(result.turn_id, "turn-compact")
        self.assertEqual(result.item_id, "item-compact")

    def test_transport_binds_process_identity_and_reaps_owned_group(self) -> None:
        transport = AppServerTransport(codex_bin=str(self.codex), strict_protocol=True)
        with transport:
            snapshot = transport.runtime_snapshot()
            self.assertEqual(snapshot["contract_version"], 2)
            self.assertNotEqual(snapshot["generation"], "unbound")
            process = snapshot["process_identity"]
            self.assertIsInstance(process, dict)
            self.assertEqual(process["pid"], transport.process.pid)
            self.assertEqual(process["pgid"], transport.process.pid)
            self.assertIsNotNone(read_proc_identity(process["pid"]))

        cleanup = transport.cleanup_observation
        self.assertIsInstance(cleanup, dict)
        self.assertEqual(cleanup["state"], "cleaned")
        self.assertTrue(cleanup["process_group_gone"])
        self.assertTrue(process_group_gone(process["pgid"]))
        self.assertEqual(
            transport.runtime_snapshot()["state"],
            "cleaned",
        )

    def test_process_disconnect_schedules_retry_and_resumes_same_thread(self) -> None:
        state = self.root / "disconnect-state"
        codex = self.root / "codex-disconnect"
        codex.write_text(
            f"""#!/usr/bin/env python3
import json, os, sys
state = {str(state)!r}
if sys.argv[1:] == ['--version']:
    print('codex-cli fake-disconnect')
    raise SystemExit(0)
if sys.argv[1:3] == ['app-server', 'generate-json-schema']:
    out = sys.argv[sys.argv.index('--out') + 1]
    os.makedirs(out, exist_ok=True)
    tokens = ['thread/start','thread/resume','thread/read','turn/start','turn/interrupt','developerInstructions','outputSchema','clientUserMessageId']
    open(os.path.join(out, 'codex_app_server_protocol.schemas.json'), 'w').write(json.dumps(tokens))
    raise SystemExit(0)
if sys.argv[1:3] != ['app-server', '--stdio']:
    raise SystemExit(2)
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get('method')
    if method == 'initialize':
        print(json.dumps({{'id': msg['id'], 'result': {{'userAgent': 'fake'}}}}), flush=True)
    elif method == 'initialized':
        pass
    elif method == 'thread/start':
        print(json.dumps({{'id': msg['id'], 'result': {{'thread': {{'id': 'thr-disconnect'}}}}}}), flush=True)
    elif method == 'thread/resume':
        print(json.dumps({{'id': msg['id'], 'result': {{'thread': {{'id': msg['params']['threadId']}}}}}}), flush=True)
    elif method == 'thread/read':
        print(json.dumps({{'id': msg['id'], 'result': {{'thread': {{'id': msg['params']['threadId'], 'turns': []}}}}}}), flush=True)
    elif method == 'turn/start':
        if not os.path.exists(state):
            open(state, 'w').write('disconnected')
            print(json.dumps({{'id': msg['id'], 'result': {{'turn': {{'id': 'turn-disconnect'}}}}}}), flush=True)
            raise SystemExit(0)
        result = {{'result':'pass','evidence_report':None,'proof_spec':None,'problem_report':None}}
        print(json.dumps({{'id': msg['id'], 'result': {{'turn': {{'id': 'turn-resumed'}}}}}}), flush=True)
        print(json.dumps({{'method':'item/completed','params':{{'threadId':'thr-disconnect','item':{{'id':'item-1','type':'agentMessage','text':json.dumps(result)}}}}}}), flush=True)
        print(json.dumps({{'method':'turn/completed','params':{{'threadId':'thr-disconnect','turn':{{'id':'turn-resumed','status':'completed','items':[]}}}}}}), flush=True)
""",
            encoding="utf-8",
        )
        codex.chmod(0o755)
        action = {
            "action": "reviewer_preflight",
            "action_id": "a" * 64,
            "reason": "reviewer_preflight_missing",
        }

        class FakeCore:
            def __init__(self) -> None:
                self.intent = {
                    "action": "reviewer_preflight",
                    "action_id": "a" * 64,
                    "attempt": 1,
                    "role": "reviewer",
                    "state": "prepared",
                    "thread_id": "thr-disconnect",
                }

            def call(self, command: str, *args: str, input_value=None):
                if command == "bind-dispatch-turn":
                    self.intent["state"] = "in_flight"
                    self.intent["turn_id"] = args[args.index("--turn-id") + 1]
                    return {"status": "ACTIVE"}
                if command == "driver-context":
                    return {
                        "facets": {
                            "execution": {
                                "agents": {
                                    "reviewer": {
                                        "agent_id": "reviewer-agent",
                                        "thread_id": "thr-disconnect",
                                    }
                                }
                            }
                        },
                        "dispatch_intent": dict(self.intent),
                    }
                if command == "retry-dispatch":
                    self.intent["attempt"] = 2
                    self.intent["state"] = "prepared"
                    self.intent.pop("turn_id", None)
                    return {
                        "status": "ACTIVE",
                        "run_id": "process-disconnect",
                        "phase": "active",
                        "dispatch_intent": dict(self.intent),
                    }
                raise AssertionError(command)

        core = FakeCore()
        with self.assertRaises(AppServerError) as raised:
            with AppServerTransport(codex_bin=str(codex)) as transport:
                thread_id = transport.start_thread(
                    cwd=str(self.root),
                    developer_instructions="reviewer",
                    sandbox="danger-full-access",
                )
                coordinator = NativeCoordinator(
                    repo=ROOT,
                    run_id="process-disconnect",
                    core=core,
                    transport=transport,
                    project_root=ROOT,
                )
                coordinator.current_action = action
                transport.run_turn(
                    thread_id=thread_id,
                    prompt="review",
                    output_schema={"type": "object"},
                    action_id=f"{action['action_id']}:1",
                    on_started=lambda turn_id: core.call(
                        "bind-dispatch-turn",
                        "--turn-id",
                        turn_id,
                    ),
                )
        self.assertEqual(raised.exception.code, "NATIVE_APP_SERVER_DISCONNECTED")
        retry = coordinator.retry_transport_failure(raised.exception)
        self.assertEqual(retry["status"], "ACTIVE")
        self.assertEqual(retry["dispatch_intent"]["attempt"], 2)
        self.assertNotIn("turn_id", retry["dispatch_intent"])

        with AppServerTransport(codex_bin=str(codex)) as transport:
            transport.resume_thread(
                thread_id="thr-disconnect",
                cwd=str(self.root),
                developer_instructions="reviewer",
                sandbox="danger-full-access",
            )
            turn = transport.run_turn(
                thread_id="thr-disconnect",
                prompt="review",
                output_schema={"type": "object"},
                action_id=f"{action['action_id']}:2",
            )
        self.assertEqual(turn.status, "completed")
        self.assertEqual(json.loads(turn.text)["result"], "pass")

    def test_process_auth_503_recovers_twice_then_succeeds_same_action_and_thread(
        self,
        ) -> None:
        repo = init_repo()
        self.addCleanup(cleanup_repo, repo)
        state = self.root / "auth-state"
        trace = self.root / "auth-trace.jsonl"
        codex = self.root / "codex-auth"
        codex.write_text(
            f"""#!/usr/bin/env python3
import json, os, sys
state = {str(state)!r}
trace = {str(trace)!r}
if sys.argv[1:] == ['--version']:
    print('codex-cli fake-auth')
    raise SystemExit(0)
if sys.argv[1:3] == ['app-server', 'generate-json-schema']:
    out = sys.argv[sys.argv.index('--out') + 1]
    os.makedirs(out, exist_ok=True)
    tokens = ['thread/start','thread/resume','thread/read','turn/start','turn/interrupt','developerInstructions','outputSchema','clientUserMessageId']
    open(os.path.join(out, 'codex_app_server_protocol.schemas.json'), 'w').write(json.dumps(tokens))
    raise SystemExit(0)
if sys.argv[1:3] != ['app-server', '--stdio']:
    raise SystemExit(2)
attempt = int(open(state).read()) if os.path.exists(state) else 0
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get('method')
    if method == 'initialize':
        print(json.dumps({{'id': msg['id'], 'result': {{'userAgent': 'fake'}}}}), flush=True)
    elif method == 'initialized':
        pass
    elif method == 'thread/start':
        print(json.dumps({{'id': msg['id'], 'result': {{'thread': {{'id': 'thr-auth'}}}}}}), flush=True)
    elif method == 'thread/resume':
        print(json.dumps({{'id': msg['id'], 'result': {{'thread': {{'id': msg['params']['threadId']}}}}}}), flush=True)
    elif method == 'thread/read':
        print(json.dumps({{'id': msg['id'], 'result': {{'thread': {{'id': msg['params']['threadId'], 'turns': []}}}}}}), flush=True)
    elif method == 'turn/start':
        attempt += 1
        open(state, 'w').write(str(attempt))
        with open(trace, 'a') as output:
            output.write(json.dumps({{'thread_id': msg['params']['threadId'], 'client_id': msg['params']['clientUserMessageId']}}) + '\\n')
        turn_id = 'turn-auth-' + str(attempt)
        print(json.dumps({{'id': msg['id'], 'result': {{'turn': {{'id': turn_id}}}}}}), flush=True)
        if attempt <= 2:
            error = {{'codexErrorInfo':'other','message':'unexpected status 503 Service Unavailable: auth_unavailable: no auth available (providers=codex, model=gpt-test)'}}
            print(json.dumps({{'method':'turn/completed','params':{{'threadId':'thr-auth','turn':{{'id':turn_id,'status':'failed','error':error,'items':[]}}}}}}), flush=True)
        else:
            result = {{'result':'implemented','evidence_report':None,'proof_spec':None,'problem_report':None}}
            print(json.dumps({{'method':'item/completed','params':{{'threadId':'thr-auth','item':{{'id':'item-auth','type':'agentMessage','text':json.dumps(result)}}}}}}), flush=True)
            print(json.dumps({{'method':'turn/completed','params':{{'threadId':'thr-auth','turn':{{'id':turn_id,'status':'completed','items':[]}}}}}}), flush=True)
""",
            encoding="utf-8",
        )
        codex.chmod(0o755)
        run_id = "process-auth-retry"
        core = CorePort()
        started = core.start(
            repo=repo,
            run_id=run_id,
            session_id="process-auth-session",
            contract=native_contract(repo),
            runtime_version="codex-cli fake-auth",
            protocol_schema_digest="a" * 64,
        )
        candidate = Path(started["candidate_worktree"])

        with AppServerTransport(codex_bin=str(codex)) as transport:
            thread_id = transport.start_thread(
                cwd=str(candidate),
                developer_instructions="builder",
                sandbox="danger-full-access",
            )
        first = core.call("driver-next", "--repo", str(repo), "--run", run_id)
        core.call(
            "prepare-builder",
            "--repo",
            str(repo),
            "--run",
            run_id,
            "--agent-id",
            f"codex-app-server:{thread_id}",
            "--thread-id",
            thread_id,
            "--action-id",
            first["action_id"],
            "--driver-runtime-kind",
            "native",
        )
        action = core.call("driver-next", "--repo", str(repo), "--run", run_id)
        sleeps: list[float] = []
        clock = [datetime.now(timezone.utc)]

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock[0] += timedelta(seconds=seconds)

        for expected_attempt in (2, 3):
            with AppServerTransport(codex_bin=str(codex)) as transport:
                coordinator = NativeCoordinator(
                    repo=repo,
                    run_id=run_id,
                    core=core,
                    transport=transport,
                    project_root=ROOT,
                    now_fn=lambda: clock[0],
                    sleep_fn=sleep,
                )
                coordinator.current_action = action
                coordinator._run_agent_action(
                    action, AGENT_ACTION_CAPABILITIES[action["action"]]
                )
            pending = core.call(
                "driver-context", "--repo", str(repo), "--run", run_id
            )["dispatch_intent"]
            self.assertEqual(pending["attempt"], expected_attempt)
            self.assertEqual(pending["failure_code"], "authUnavailable")
            clock[0] = datetime.fromisoformat(pending["retry_scheduled_at"])

        with AppServerTransport(codex_bin=str(codex)) as transport:
            coordinator = NativeCoordinator(
                repo=repo,
                run_id=run_id,
                core=core,
                transport=transport,
                project_root=ROOT,
                now_fn=lambda: clock[0],
                sleep_fn=sleep,
            )
            coordinator.current_action = action
            coordinator._run_agent_action(
                action, AGENT_ACTION_CAPABILITIES[action["action"]]
            )

        status = core.call(
            "status", "--repo", str(repo), "--run", run_id
        )
        context = core.call(
            "driver-context", "--repo", str(repo), "--run", run_id
        )
        self.assertTrue(status["builder_checkpointed"])
        self.assertIsNone(context["dispatch_intent"])
        observations = [
            json.loads(line) for line in trace.read_text().splitlines() if line.strip()
        ]
        self.assertEqual(
            [item["thread_id"] for item in observations], [thread_id] * 3
        )
        self.assertEqual(
            [item["client_id"] for item in observations],
            [
                f"{action['action_id']}:1",
                f"{action['action_id']}:2",
                f"{action['action_id']}:3",
            ],
        )
        self.assertEqual(sum(sleeps), 90.0)


class NativeCoordinatorContractTest(unittest.TestCase):
    def test_agent_dispatch_is_persisted_before_role_activation(self) -> None:
        action = {
            "action": "builder_implement",
            "action_id": "a" * 64,
            "reason": "builder_missing",
        }
        context = {
            "repo_root": str(ROOT),
            "target_start_head": "1" * 40,
            "candidate_worktree": str(ROOT),
            "publication": None,
            "evidence": {},
            "problems": [],
            "facets": {
                "execution": {
                    "agents": {
                        "builder": {
                            "agent_id": "builder-agent",
                            "thread_id": "builder-thread",
                        }
                    },
                    "tester_source": None,
                }
            },
        }
        events: list[str] = []

        class FakeCore:
            def call(self, command: str, *args: str, input_value=None):
                events.append(f"core:{command}")
                if command == "driver-context":
                    return context
                if command == "begin-dispatch":
                    return {
                        "dispatch_intent": {
                            "action_id": action["action_id"],
                            "activation_state": "pending",
                        }
                    }
                return {"status": "ACTIVE"}

        class FakeTransport:
            def resume_thread(self, **_kwargs):
                events.append("transport:resume_thread")

            def run_turn(self, **kwargs):
                events.append("transport:run_turn")
                kwargs["on_started"]("turn-1")
                return TurnResult(
                    turn_id="turn-1",
                    status="completed",
                    text=json.dumps(
                        {
                            "result": "implemented",
                            "evidence_report": None,
                            "proof_spec": None,
                            "problem_report": None,
                        }
                    ),
                )

        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="native-dispatch-order",
            core=FakeCore(),
            transport=FakeTransport(),
            project_root=ROOT,
        )
        coordinator._prompt = lambda *_args: "prompt"
        coordinator._apply_agent_result = lambda *_args: None

        coordinator._run_agent_action(
            action,
            AGENT_ACTION_CAPABILITIES["builder_implement"],
        )

        self.assertLess(
            events.index("core:begin-dispatch"),
            events.index("transport:resume_thread"),
        )
        self.assertLess(
            events.index("transport:resume_thread"),
            events.index("transport:run_turn"),
        )
        self.assertEqual(events.count("transport:run_turn"), 1)

    def test_pre_dispatch_disconnect_retries_without_starting_a_turn(self) -> None:
        action = {
            "action": "builder_implement",
            "action_id": "a" * 64,
            "reason": "builder_missing",
        }
        context = {
            "repo_root": str(ROOT),
            "target_start_head": "1" * 40,
            "candidate_worktree": str(ROOT),
            "publication": None,
            "evidence": {},
            "problems": [],
            "facets": {
                "execution": {
                    "agents": {
                        "builder": {
                            "agent_id": "builder-agent",
                            "thread_id": "builder-thread",
                        }
                    },
                    "tester_source": None,
                }
            },
        }
        calls: list[str] = []

        class FakeCore:
            def call(self, command: str, *args: str, input_value=None):
                calls.append(command)
                if command == "driver-context":
                    return context
                if command == "begin-dispatch":
                    context["dispatch_intent"] = {
                        "action": action["action"],
                        "action_id": action["action_id"],
                        "role": "builder",
                        "thread_id": "builder-thread",
                        "state": "prepared",
                        "activation_state": "pending",
                    }
                    return {"dispatch_intent": dict(context["dispatch_intent"])}
                if command == "retry-dispatch":
                    return {
                        "status": "ACTIVE",
                        "dispatch_intent": {"attempt": 2, "state": "prepared"},
                    }
                return {"status": "ACTIVE"}

        class DisconnectTransport:
            def __init__(self) -> None:
                self.resume_calls = 0
                self.turn_calls = 0
                self.start_calls = 0

            def resume_thread(self, **_kwargs):
                self.resume_calls += 1
                raise AppServerError(
                    "Codex App Server closed its output",
                    code="NATIVE_APP_SERVER_DISCONNECTED",
                )

            def run_turn(self, **_kwargs):
                self.turn_calls += 1
                raise AssertionError("pre-dispatch failure must precede turn/start")

            def start_thread(self, **_kwargs):
                self.start_calls += 1
                raise AssertionError("pre-dispatch failure must not create a thread")

        transport = DisconnectTransport()
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="native-pre-dispatch-disconnect",
            core=FakeCore(),
            transport=transport,
            project_root=ROOT,
        )
        coordinator._prompt = lambda *_args: "prompt"
        coordinator.current_action = action

        with self.assertRaises(AppServerError) as raised:
            coordinator._run_agent_action(
                action,
                AGENT_ACTION_CAPABILITIES["builder_implement"],
            )
        retry = coordinator.retry_transport_failure(raised.exception)

        self.assertEqual(retry["status"], "ACTIVE")
        self.assertEqual(transport.resume_calls, 1)
        self.assertEqual(transport.turn_calls, 0)
        self.assertEqual(transport.start_calls, 0)
        self.assertEqual(
            calls,
            [
                "driver-context",
                "begin-dispatch",
                "driver-context",
                "driver-context",
                "retry-dispatch",
            ],
        )

    def test_recovered_activated_dispatch_does_not_resume_thread_again(self) -> None:
        action = {
            "action": "builder_implement",
            "action_id": "b" * 64,
            "reason": "builder_missing",
        }
        context = {
            "repo_root": str(ROOT),
            "target_start_head": "1" * 40,
            "candidate_worktree": str(ROOT),
            "publication": None,
            "evidence": {},
            "problems": [],
            "facets": {
                "execution": {
                    "agents": {
                        "builder": {
                            "agent_id": "builder-agent",
                            "thread_id": "builder-thread",
                        }
                    },
                    "tester_source": None,
                }
            },
        }
        pending = {
            "action": action["action"],
            "action_id": action["action_id"],
            "role": "builder",
            "thread_id": "builder-thread",
            "prompt_digest": digest("prompt"),
            "output_schema_digest": digest(
                json.loads(
                    (
                        ROOT
                        / "schema"
                        / "assurance-v4-native-agent-wire-result.schema.json"
                    ).read_text()
                )
            ),
            "state": "prepared",
            "attempt": 1,
            "generation": 1,
            "activation_state": "activated",
        }
        calls: list[str] = []

        class FakeCore:
            def call(self, command: str, *args: str, input_value=None):
                calls.append(command)
                return {"status": "ACTIVE"}

        class NoResumeTransport:
            def resume_thread(self, **_kwargs):
                raise AssertionError("activated dispatch must not resume the thread")

            def read_thread(self, _thread_id: str):
                return {"turns": []}

            def run_turn(self, **kwargs):
                kwargs["on_started"]("turn-activated")
                return TurnResult(
                    turn_id="turn-activated",
                    status="completed",
                    text=json.dumps(
                        {
                            "result": "implemented",
                            "evidence_report": None,
                            "proof_spec": None,
                            "problem_report": None,
                        }
                    ),
                )

        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="native-dispatch-recovery-activated",
            core=FakeCore(),
            transport=NoResumeTransport(),
            project_root=ROOT,
        )
        coordinator._prompt = lambda *_args: "prompt"

        result = coordinator._recover_dispatch(pending, "builder", context, action)

        self.assertEqual(result["result"], "implemented")
        self.assertEqual(calls, ["bind-dispatch-turn", "complete-dispatch"])

    def test_unknown_pre_dispatch_transport_failure_is_recorded_and_stops(self) -> None:
        action_id = "c" * 64
        action = {
            "action": "builder_implement",
            "action_id": action_id,
            "reason": "builder_missing",
        }
        context = {
            "facets": {
                "execution": {
                    "agents": {
                        "builder": {
                            "agent_id": "builder-agent",
                            "thread_id": "builder-thread",
                        }
                    }
                }
            },
            "dispatch_intent": {
                "action": action["action"],
                "action_id": action_id,
                "role": "builder",
                "thread_id": "builder-thread",
                "state": "prepared",
                "activation_state": "pending",
            },
        }
        calls: list[tuple[str, tuple[str, ...], object]] = []

        class FakeCore:
            def call(self, command: str, *args: str, input_value=None):
                calls.append((command, args, input_value))
                if command == "driver-context":
                    return context
                return {"status": "ACTIVE"}

        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="native-dispatch-activation-unknown",
            core=FakeCore(),
            transport=object(),
            project_root=ROOT,
        )
        coordinator.current_action = action
        error = AppServerError(
            "thread resume returned an unclassified error",
            code="NATIVE_APP_SERVER_REQUEST_FAILED",
            details={"method": "thread/resume", "error": {"code": -32000}},
        )

        with self.assertRaises(NativeDriverError) as raised:
            coordinator.retry_transport_failure(error)

        self.assertEqual(raised.exception.code, "NATIVE_DISPATCH_ACTIVATION_UNKNOWN")
        self.assertEqual(
            [command for command, _args, _input in calls],
            ["driver-context", "record-dispatch-activation"],
        )
        activation = calls[-1]
        self.assertIn("--state", activation[1])
        self.assertIn("unknown", activation[1])
        self.assertEqual(activation[2], error.details)

    def test_projection_prefers_core_payload_and_keeps_legacy_fallback(self) -> None:
        projection = {"schema_version": 1, "action": "builder_implement"}
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="native-projection-preference",
            core=object(),
            transport=object(),
            project_root=ROOT,
        )
        action = {
            "action": "builder_implement",
            "action_id": "d" * 64,
            "context_projection": projection,
            "context_projection_digest": digest(projection),
        }
        coordinator._canonical_projection = lambda *_args: {
            "legacy": "must-not-win"
        }

        selected, selected_digest = coordinator._projection_for_action(
            action, "builder", {}
        )

        self.assertEqual(selected, projection)
        self.assertEqual(selected_digest, digest(projection))
        legacy_action = {
            "action": "builder_implement",
            "action_id": "e" * 64,
        }
        legacy, legacy_digest = coordinator._projection_for_action(
            legacy_action, "builder", {}
        )
        self.assertEqual(legacy, {"legacy": "must-not-win"})
        self.assertEqual(legacy_digest, digest(legacy))

        action["context_projection_digest"] = "f" * 64
        with self.assertRaises(NativeDriverError) as raised:
            coordinator._projection_for_action(action, "builder", {})
        self.assertEqual(
            raised.exception.code, "NATIVE_CONTEXT_PROJECTION_DIGEST_MISMATCH"
        )

    def test_blackbox_only_missing_tester_uses_identity_only_preparation(self) -> None:
        action = {
            "action": "tester_blackbox",
            "action_id": "a" * 64,
            "reason": "blackbox_missing",
        }

        class FakeCore:
            def __init__(self) -> None:
                self.calls: list[tuple[str, tuple[str, ...]]] = []

            def call(self, command: str, *args: str, input_value=None):
                self.calls.append((command, args))
                if command == "driver-context":
                    return {
                        "repo_root": str(ROOT),
                        "candidate_worktree": str(ROOT),
                        "facets": {
                            "execution": {
                                "agents": {},
                                "tester_source": None,
                            }
                        },
                        "dispatch_intent": None,
                    }
                if command == "prepare-tester":
                    return {"status": "ACTIVE"}
                raise AssertionError(command)

        class FakeTransport:
            def start_thread(self, **_kwargs):
                return "thread-blackbox"

        core = FakeCore()
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="native-blackbox-identity",
            core=core,
            transport=FakeTransport(),
            project_root=ROOT,
        )
        coordinator._run_agent_action(
            action,
            AGENT_ACTION_CAPABILITIES["tester_blackbox"],
        )
        command, args = core.calls[-1]
        self.assertEqual(command, "prepare-tester")
        self.assertIn("--identity-only", args)
        self.assertIn("thread-blackbox", args)

    def test_driver_next_failure_does_not_reuse_a_stale_action_identity(self) -> None:
        class FailingCore:
            def call(self, command: str, *args: str, input_value=None):
                if command != "driver-next":
                    raise AssertionError(command)
                raise CorePortError(
                    "driver-next failed",
                    payload={"status": "FATAL", "code": "DRIVER_NEXT_FAILED"},
                    returncode=2,
                )

        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="native-stale-action",
            core=FailingCore(),
            transport=object(),
            project_root=ROOT,
        )
        coordinator.current_action = {
            "action_id": "a" * 64,
            "action": "builder_fix",
            "reason": "open_builder_problem",
        }

        with self.assertRaises(CorePortError):
            coordinator.run()

        self.assertIsNone(coordinator.current_action)

    def test_native_wire_normalizes_nested_json_strings(self) -> None:
        evidence = {"schema_version": 1, "kind": "reviewer"}
        result = NativeCoordinator._parse_turn(
            TurnResult(
                turn_id="turn-wire",
                status="completed",
                text=json.dumps(
                    {
                        "result": "pass",
                        "evidence_report": json.dumps(evidence),
                        "proof_spec": None,
                        "problem_report": None,
                    }
                ),
            )
        )
        self.assertEqual(result["evidence_report"], evidence)

    def test_invalid_role_json_uses_bounded_retry_before_dispatch_completion(self) -> None:
        action = {
            "action": "builder_implement",
            "action_id": "a" * 64,
            "reason": "candidate_missing",
        }
        context = {
            "run_id": "native-invalid-json",
            "target_start_head": "1" * 40,
            "candidate_worktree": str(ROOT),
            "publication": None,
            "evidence": {},
            "problems": [],
            "facets": {
                "mission": {},
                "authority": {},
                "assurance": {},
                "execution": {
                    "agents": {
                        "builder": {
                            "agent_id": "builder-agent",
                            "thread_id": "builder-thread",
                        }
                    },
                    "tester_source": None,
                },
            },
        }

        class FakeCore:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def call(self, command: str, *args: str, input_value=None):
                self.calls.append(command)
                if command == "driver-context":
                    return context
                if command == "retry-dispatch":
                    return {"status": "ACTIVE"}
                return {"status": "ACTIVE"}

        class FakeTransport:
            def resume_thread(self, **_kwargs):
                return None

            def run_turn(self, **kwargs):
                kwargs["on_started"]("turn-invalid")
                return TurnResult(
                    turn_id="turn-invalid",
                    status="completed",
                    text="{not-json",
                )

        core = FakeCore()
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="native-invalid-json",
            core=core,
            transport=FakeTransport(),
            project_root=ROOT,
        )
        coordinator._run_agent_action(
            action,
            AGENT_ACTION_CAPABILITIES["builder_implement"],
        )
        self.assertIn("retry-dispatch", core.calls)
        self.assertNotIn("complete-dispatch", core.calls)

    def test_recovered_invalid_role_json_uses_same_retry_path(self) -> None:
        action = {
            "action": "builder_implement",
            "action_id": "b" * 64,
            "reason": "candidate_missing",
        }
        context = {
            "target_start_head": "1" * 40,
            "candidate_worktree": str(ROOT),
            "publication": None,
            "evidence": {},
            "problems": [],
            "facets": {
                "mission": {},
                "authority": {},
                "assurance": {},
                "execution": {
                    "agents": {
                        "builder": {
                            "agent_id": "builder-agent",
                            "thread_id": "builder-thread",
                        }
                    },
                    "tester_source": None,
                },
            },
        }

        class FakeCore:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def call(self, command: str, *args: str, input_value=None):
                self.calls.append(command)
                return {"status": "ACTIVE"}

        class FakeTransport:
            def resume_thread(self, **_kwargs):
                return None

            def read_thread(self, _thread_id):
                return {
                    "turns": [
                        {
                            "id": "turn-invalid-recovered",
                            "status": "completed",
                            "items": [
                                {"type": "agentMessage", "text": "{not-json"}
                            ],
                        }
                    ]
                }

        core = FakeCore()
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="native-invalid-json-recovered",
            core=core,
            transport=FakeTransport(),
            project_root=ROOT,
        )
        prompt = coordinator._prompt(action, "builder", context)
        pending = {
            "action_id": action["action_id"],
            "action": action["action"],
            "role": "builder",
            "thread_id": "builder-thread",
            "prompt_digest": digest(prompt),
            "output_schema_digest": coordinator.output_schema_digest,
            "state": "in_flight",
            "attempt": 1,
            "generation": 1,
            "turn_id": "turn-invalid-recovered",
        }
        recovered = coordinator._recover_dispatch(
            pending,
            "builder",
            context,
            action,
        )
        self.assertIsNone(recovered)
        self.assertIn("retry-dispatch", core.calls)
        self.assertNotIn("complete-dispatch", core.calls)

    def test_invalid_role_json_exhaustion_remains_needs_user(self) -> None:
        class ExhaustingCore:
            def call(self, command: str, *args: str, input_value=None):
                if command == "retry-dispatch":
                    raise CorePortError(
                        "retry exhausted",
                        payload={
                            "status": "NEEDS_USER",
                            "code": "NATIVE_DISPATCH_RETRY_EXHAUSTED",
                        },
                        returncode=1,
                    )
                raise AssertionError(command)

        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="native-invalid-json-exhausted",
            core=ExhaustingCore(),
            transport=object(),
            project_root=ROOT,
        )
        with self.assertRaises(CorePortError) as raised:
            coordinator._parse_action_result_or_retry(
                "tester_proof_diagnose",
                TurnResult(
                    turn_id="turn-invalid-exhausted",
                    status="completed",
                    text="{not-json",
                ),
                "c" * 64,
            )
        self.assertEqual(raised.exception.payload["status"], "NEEDS_USER")
        self.assertEqual(
            raised.exception.payload["code"], "NATIVE_DISPATCH_RETRY_EXHAUSTED"
        )

    def test_retryable_live_turn_failure_schedules_same_dispatch(self) -> None:
        class FakeCore:
            def __init__(self) -> None:
                self.calls = []

            def call(self, command: str, *args: str, input_value=None):
                self.calls.append((command, args))
                return {"status": "ACTIVE"}

        core = FakeCore()
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="native-retry",
            core=core,
            transport=object(),
            project_root=ROOT,
        )
        retried = coordinator._retry_turn_failure(
            TurnResult(
                turn_id="turn-overloaded",
                status="failed",
                text="",
                error={"codexErrorInfo": "serverOverloaded"},
            ),
            "a" * 64,
        )
        self.assertTrue(retried)
        self.assertEqual(core.calls[0][0], "retry-dispatch")
        self.assertIn("serverOverloaded", core.calls[0][1])

    def test_dirty_builder_retry_is_blocked_before_retry_dispatch(self) -> None:
        action_id = "a" * 64

        class FakeCore:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def call(self, command: str, *args: str, input_value=None):
                self.calls.append(command)
                if command == "driver-context":
                    return {
                        "dispatch_intent": {
                            "action": "builder_implement",
                            "action_id": action_id,
                            "candidate_manifest_digest": "b" * 64,
                        },
                        "candidate_observation": {
                            "manifest_digest": "c" * 64,
                            "candidate_manifest": {
                                "head": "d" * 40,
                                "dirty_paths": ["src/changed.py"],
                                "entries": [],
                                "manifest_digest": "c" * 64,
                            },
                        },
                    }
                raise AssertionError(command)

        core = FakeCore()
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="native-dirty-builder-retry",
            core=core,
            transport=object(),
            project_root=ROOT,
        )

        with self.assertRaises(NativeDriverError) as raised:
            coordinator._schedule_dispatch_retry(
                action_id,
                "responseStreamDisconnected",
                action_name="builder_implement",
            )

        self.assertEqual(
            raised.exception.code,
            "NATIVE_BUILDER_SIDE_EFFECT_RETRY_BLOCKED",
        )
        self.assertEqual(raised.exception.status, "FATAL")
        self.assertEqual(core.calls, ["driver-context"])

    def test_clean_builder_retry_keeps_existing_retry_dispatch(self) -> None:
        action_id = "a" * 64

        class FakeCore:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def call(self, command: str, *args: str, input_value=None):
                self.calls.append(command)
                if command == "driver-context":
                    return {
                        "dispatch_intent": {
                            "action": "builder_fix",
                            "action_id": action_id,
                            "candidate_manifest_digest": "b" * 64,
                        },
                        "candidate_observation": {
                            "manifest_digest": "b" * 64,
                        },
                    }
                if command == "retry-dispatch":
                    return {"status": "ACTIVE", "dispatch_intent": {"attempt": 2}}
                raise AssertionError(command)

        core = FakeCore()
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="native-clean-builder-retry",
            core=core,
            transport=object(),
            project_root=ROOT,
        )
        retried = coordinator._schedule_dispatch_retry(
            action_id,
            "responseStreamDisconnected",
            action_name="builder_fix",
        )

        self.assertEqual(retried["dispatch_intent"]["attempt"], 2)
        self.assertEqual(core.calls, ["driver-context", "retry-dispatch"])

    def test_known_other_stream_disconnect_schedules_same_dispatch(self) -> None:
        class FakeCore:
            def __init__(self) -> None:
                self.calls = []

            def call(self, command: str, *args: str, input_value=None):
                self.calls.append((command, args))
                return {"status": "ACTIVE"}

        core = FakeCore()
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="native-other-disconnect",
            core=core,
            transport=object(),
            project_root=ROOT,
        )
        retried = coordinator._retry_turn_failure(
            TurnResult(
                turn_id="turn-disconnected",
                status="failed",
                text="",
                error={
                    "codexErrorInfo": "other",
                    "message": (
                        "stream disconnected before completion: stream closed before "
                        "response.completed"
                    ),
                },
            ),
            "a" * 64,
        )
        self.assertTrue(retried)
        self.assertEqual(core.calls[0][0], "retry-dispatch")
        self.assertIn("responseStreamDisconnected", core.calls[0][1])

    def test_auth_unavailable_requires_503_and_exact_auth_markers(self) -> None:
        for message in (
            "HTTP 503 auth_unavailable: no auth available "
            "(providers=codex, model=gpt-test)",
            "HTTP status 503 auth_unavailable: no auth available "
            "(providers=codex, model=gpt-test)",
            "unexpected status 503 Service Unavailable: auth_unavailable: "
            "no auth available (providers=codex, model=gpt-test)",
            "503 Service Unavailable: auth_unavailable: no auth available "
            "(providers=codex, model=gpt-test)",
            "HTTP/1.1 503 Service Unavailable: auth_unavailable: "
            "no auth available (providers=codex, model=gpt-test)",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    classify_turn_failure(
                        {"codexErrorInfo": "other", "message": message}
                    ),
                    "authUnavailable",
                )
        for message in (
            "HTTP 503 auth_unavailable",
            "auth_unavailable: no auth available",
            "HTTP 503 no auth available",
            "HTTP 401 auth_unavailable: no auth available",
            "unexpected status 503 Service Unavailable: no auth available",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    classify_turn_failure(
                        {"codexErrorInfo": "other", "message": message}
                    ),
                    "other",
                )
        exact_message = (
            "unexpected status 503 Service Unavailable: auth_unavailable: "
            "no auth available (providers=codex, model=gpt-test)"
        )
        self.assertEqual(
            classify_turn_failure(
                {
                    "codexErrorInfo": "cyberPolicy",
                    "message": exact_message,
                }
            ),
            "cyberPolicy",
        )

    def test_auth_retry_wait_is_visible_and_resumes_from_remaining_time(self) -> None:
        deadline = datetime(2026, 8, 13, 12, 0, 30, tzinfo=timezone.utc)
        clock = [deadline - timedelta(seconds=17)]
        sleeps: list[float] = []
        events: list[dict] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock[0] += timedelta(seconds=seconds)

        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="auth-retry-wait",
            core=object(),
            transport=object(),
            project_root=ROOT,
            event_sink=events.append,
            now_fn=lambda: clock[0],
            sleep_fn=sleep,
        )
        coordinator._wait_for_retry(
            {
                "action_id": "a" * 64,
                "action": "builder_implement",
                "attempt": 2,
                "failure_code": "authUnavailable",
                "retry_not_before": deadline.isoformat(),
            }
        )
        self.assertEqual(sleeps, [10.0, 7.0])
        self.assertEqual(
            [event["remaining_seconds"] for event in events], [17, 7, 0]
        )
        self.assertTrue(
            all(event["event"] == "native_driver_retry_waiting" for event in events)
        )

    def test_unknown_turn_failure_remains_non_retryable(self) -> None:
        turn = TurnResult(
            turn_id="turn-unknown",
            status="failed",
            text="",
            error={"codexErrorInfo": "other", "message": "unrelated model failure"},
        )
        self.assertEqual(NativeCoordinator._turn_result_failure_code(turn), "other")
        with self.assertRaises(NativeDriverError) as raised:
            NativeCoordinator._parse_turn(turn)
        self.assertEqual(raised.exception.code, "NATIVE_ROLE_TURN_FAILED")

    def test_raw_disconnect_retries_only_matching_active_dispatch(self) -> None:
        action = {
            "action": "reviewer_preflight",
            "action_id": "a" * 64,
            "reason": "reviewer_preflight_missing",
        }

        class FakeCore:
            def __init__(self, *, matching: bool = True, exhausted: bool = False) -> None:
                self.matching = matching
                self.exhausted = exhausted
                self.calls: list[str] = []

            def call(self, command: str, *args: str, input_value=None):
                self.calls.append(command)
                if command == "driver-context":
                    return {
                        "facets": {
                            "execution": {
                                "agents": {
                                    "reviewer": {
                                        "agent_id": "reviewer-agent",
                                        "thread_id": "reviewer-thread",
                                    }
                                }
                            }
                        },
                        "dispatch_intent": {
                            "action": "reviewer_preflight",
                            "action_id": ("a" if self.matching else "b") * 64,
                            "attempt": 1,
                            "role": "reviewer",
                            "state": "in_flight",
                            "thread_id": "reviewer-thread",
                            "turn_id": "reviewer-turn",
                        },
                    }
                if command == "retry-dispatch":
                    if self.exhausted:
                        raise CorePortError(
                            "Native role transport failed three times",
                            payload={
                                "status": "NEEDS_USER",
                                "code": "NATIVE_DISPATCH_RETRY_EXHAUSTED",
                                "details": {"attempt": 3},
                            },
                            returncode=1,
                        )
                    return {
                        "status": "ACTIVE",
                        "run_id": "raw-disconnect",
                        "dispatch_intent": {"attempt": 2, "state": "prepared"},
                    }
                raise AssertionError(command)

        error = AppServerError(
            "Codex App Server closed its output",
            code="NATIVE_APP_SERVER_DISCONNECTED",
        )
        core = FakeCore()
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="raw-disconnect",
            core=core,
            transport=object(),
            project_root=ROOT,
        )
        coordinator.current_action = action
        retried = coordinator.retry_transport_failure(error)
        self.assertEqual(retried["status"], "ACTIVE")
        self.assertEqual(retried["dispatch_intent"]["attempt"], 2)
        self.assertEqual(
            retried["transport_retry"]["failure_code"],
            "responseStreamDisconnected",
        )
        self.assertEqual(core.calls, ["driver-context", "retry-dispatch"])

        mismatched = FakeCore(matching=False)
        coordinator.core = mismatched
        self.assertIsNone(coordinator.retry_transport_failure(error))
        self.assertEqual(mismatched.calls, ["driver-context"])

        exhausted = FakeCore(exhausted=True)
        coordinator.core = exhausted
        stopped = coordinator.retry_transport_failure(error)
        self.assertEqual(stopped["status"], "NEEDS_USER")
        self.assertEqual(stopped["code"], "NATIVE_DISPATCH_RETRY_EXHAUSTED")
        self.assertEqual(stopped["run_id"], "raw-disconnect")

    def test_user_reason_creates_new_dispatch_generation_on_same_thread(self) -> None:
        action_id = "a" * 64

        class FakeCore:
            def __init__(self) -> None:
                self.calls: list[tuple[str, tuple[str, ...]]] = []
                self.intent = {
                    "action": "builder_implement",
                    "action_id": action_id,
                    "attempt": 3,
                    "generation": 1,
                    "role": "builder",
                    "state": "exhausted",
                    "thread_id": "builder-thread",
                    "turn_id": "builder-turn-3",
                    "failure_code": "serverOverloaded",
                }

            def call(self, command: str, *args: str, input_value=None):
                self.calls.append((command, args))
                if command == "retry-dispatch":
                    raise CorePortError(
                        "Native role transport failed three times",
                        payload={
                            "status": "NEEDS_USER",
                            "code": "NATIVE_DISPATCH_RETRY_EXHAUSTED",
                            "details": {"attempt": 3, "generation": 1},
                        },
                        returncode=1,
                    )
                if command == "driver-context":
                    return {
                        "facets": {
                            "execution": {
                                "agents": {
                                    "builder": {
                                        "agent_id": "builder-agent",
                                        "thread_id": "builder-thread",
                                    }
                                }
                            }
                        },
                        "dispatch_intent": dict(self.intent),
                    }
                if command == "renew-dispatch":
                    self.intent = {
                        **self.intent,
                        "attempt": 1,
                        "generation": 2,
                        "state": "prepared",
                    }
                    self.intent.pop("turn_id", None)
                    self.intent.pop("failure_code", None)
                    return {
                        "status": "ACTIVE",
                        "dispatch_intent": dict(self.intent),
                    }
                raise AssertionError(command)

        core = FakeCore()
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="renewed-dispatch",
            core=core,
            transport=object(),
            project_root=ROOT,
            dispatch_renewal_reason="user approved a new dispatch generation",
        )
        renewed = coordinator._schedule_dispatch_retry(
            action_id, "serverOverloaded"
        )
        self.assertEqual(renewed["dispatch_intent"]["generation"], 2)
        self.assertEqual(renewed["dispatch_intent"]["attempt"], 1)
        self.assertEqual(renewed["dispatch_intent"]["thread_id"], "builder-thread")
        self.assertEqual(
            [command for command, _args in core.calls],
            ["retry-dispatch", "driver-context", "renew-dispatch"],
        )
        renew_args = core.calls[-1][1]
        self.assertIn("user approved a new dispatch generation", renew_args)
        self.assertEqual(
            NativeCoordinator._dispatch_client_id(renewed["dispatch_intent"]),
            f"{action_id}:g2:1",
        )

    def test_exhausted_reviewer_dispatch_compacts_empty_tail_once(self) -> None:
        action_id = "a" * 64
        prior_turns = [
            {
                "id": f"reviewer-turn-{index}",
                "status": "failed",
                "items": [],
                "error": {"codexErrorInfo": {"responseStreamDisconnected": {}}},
            }
            for index in (1, 2, 3)
        ]
        compaction_turn = {
            "id": "reviewer-compaction-turn",
            "status": "completed",
            "durationMs": 900,
            "items": [{"id": "context-item", "type": "contextCompaction"}],
        }

        class FakeCore:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.intent = {
                    "action": "reviewer_preflight",
                    "action_id": action_id,
                    "attempt": 3,
                    "generation": 1,
                    "role": "reviewer",
                    "state": "exhausted",
                    "thread_id": "reviewer-thread",
                    "turn_id": "reviewer-turn-3",
                    "failure_code": "responseStreamDisconnected",
                    "prompt_digest": "b" * 64,
                    "output_schema_digest": "c" * 64,
                }

            def call(self, command: str, *args: str, input_value=None):
                self.calls.append(command)
                if command == "retry-dispatch":
                    raise CorePortError(
                        "Native role transport failed three times",
                        payload={
                            "status": "NEEDS_USER",
                            "code": "NATIVE_DISPATCH_RETRY_EXHAUSTED",
                        },
                        returncode=1,
                    )
                if command == "driver-context":
                    return {
                        "facets": {
                            "execution": {
                                "agents": {
                                    "reviewer": {
                                        "agent_id": "reviewer-agent",
                                        "thread_id": "reviewer-thread",
                                    }
                                }
                            }
                        },
                        "dispatch_intent": copy.deepcopy(self.intent),
                    }
                if command == "prepare-dispatch-compaction":
                    self.intent["compaction_recovery"] = {
                        "state": "prepared",
                        "source_generation": 1,
                        "source_attempt": 3,
                        "failure_code": "responseStreamDisconnected",
                        "thread_id": "reviewer-thread",
                        "prompt_digest": "b" * 64,
                        "prior_turn_count": 3,
                        "prior_tail_turn_id": "reviewer-turn-3",
                        "prior_turns_digest": digest(prior_turns),
                        "prepared_at": "2026-08-01T00:00:03+00:00",
                    }
                    return {"status": "ACTIVE"}
                if command == "complete-dispatch-compaction":
                    self.intent = {
                        **self.intent,
                        "state": "prepared",
                        "attempt": 1,
                        "generation": 2,
                        "renewal_reason": "automatic_reviewer_thread_compaction",
                        "compaction_recovery": {
                            **self.intent["compaction_recovery"],
                            "state": "completed",
                            "compaction_turn_id": compaction_turn["id"],
                            "context_item_id": "context-item",
                            "compaction_turn_digest": digest(compaction_turn),
                            "compaction_duration_ms": 900,
                            "observed_turn_count": 4,
                            "completed_at": "2026-08-01T00:00:04+00:00",
                        },
                    }
                    self.intent.pop("turn_id", None)
                    self.intent.pop("failure_code", None)
                    return {
                        "status": "ACTIVE",
                        "dispatch_intent": copy.deepcopy(self.intent),
                    }
                raise AssertionError(command)

        class FakeTransport:
            def __init__(self) -> None:
                self.compacted = False

            def read_thread(self, thread_id: str):
                self.assert_thread(thread_id)
                turns = prior_turns + ([compaction_turn] if self.compacted else [])
                return {"id": thread_id, "turns": copy.deepcopy(turns)}

            def compact_thread(self, thread_id: str):
                self.assert_thread(thread_id)
                self.compacted = True
                return SimpleNamespace(
                    turn_id=compaction_turn["id"], item_id="context-item"
                )

            @staticmethod
            def assert_thread(thread_id: str) -> None:
                if thread_id != "reviewer-thread":
                    raise AssertionError(thread_id)

        core = FakeCore()
        transport = FakeTransport()
        events: list[dict] = []
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="reviewer-compaction",
            core=core,
            transport=transport,
            project_root=ROOT,
            event_sink=events.append,
            thread_compaction_available=True,
            dispatch_renewal_reason="user approved recovery",
        )
        coordinator.current_action = {
            "action": "reviewer_preflight",
            "action_id": action_id,
            "reason": "reviewer_preflight_missing",
        }

        recovered = coordinator._schedule_dispatch_retry(
            action_id, "responseStreamDisconnected"
        )

        self.assertTrue(transport.compacted)
        self.assertEqual(recovered["dispatch_intent"]["generation"], 2)
        self.assertEqual(recovered["dispatch_intent"]["attempt"], 1)
        self.assertEqual(recovered["dispatch_intent"]["thread_id"], "reviewer-thread")
        self.assertEqual(recovered["dispatch_intent"]["prompt_digest"], "b" * 64)
        self.assertIsNone(coordinator._dispatch_renewal_reason)
        self.assertEqual(
            core.calls,
            [
                "retry-dispatch",
                "driver-context",
                "prepare-dispatch-compaction",
                "driver-context",
                "complete-dispatch-compaction",
            ],
        )
        self.assertEqual(
            [event["event"] for event in events],
            [
                "native_driver_thread_compaction_started",
                "native_driver_thread_compaction_completed",
            ],
        )

    def test_reviewer_compaction_refuses_nonempty_or_non_tail_failure(self) -> None:
        pending = {
            "action": "reviewer_final",
            "action_id": "a" * 64,
            "attempt": 3,
            "generation": 1,
            "role": "reviewer",
            "state": "exhausted",
            "thread_id": "reviewer-thread",
            "turn_id": "failed-turn",
            "failure_code": "responseStreamDisconnected",
        }

        class NoMutationCore:
            def call(self, command: str, *args: str, input_value=None):
                raise AssertionError(command)

        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="reviewer-compaction-refused",
            core=NoMutationCore(),
            transport=object(),
            project_root=ROOT,
            thread_compaction_available=True,
        )
        thread = {
            "turns": [
                {
                    "id": "failed-turn",
                    "status": "failed",
                    "items": [{"id": "agent", "type": "agentMessage", "text": "partial"}],
                    "error": {
                        "codexErrorInfo": {"responseStreamDisconnected": {}}
                    },
                },
                {"id": "later-turn", "status": "completed", "items": []},
            ]
        }

        self.assertIsNone(
            coordinator._recover_reviewer_thread_compaction(pending, thread)
        )

    def test_prepared_reviewer_compaction_recovers_without_repeating_side_effect(self) -> None:
        prior_turns = [
            {
                "id": "failed-turn",
                "status": "failed",
                "items": [],
                "error": {"codexErrorInfo": {"responseStreamDisconnected": {}}},
            }
        ]
        compaction_turn = {
            "id": "compaction-turn",
            "status": "completed",
            "durationMs": 700,
            "items": [{"id": "context-item", "type": "contextCompaction"}],
        }
        pending = {
            "action": "reviewer_final",
            "action_id": "a" * 64,
            "attempt": 3,
            "generation": 1,
            "role": "reviewer",
            "state": "exhausted",
            "thread_id": "reviewer-thread",
            "turn_id": "failed-turn",
            "failure_code": "responseStreamDisconnected",
            "prompt_digest": "b" * 64,
            "compaction_recovery": {
                "state": "prepared",
                "source_generation": 1,
                "source_attempt": 3,
                "failure_code": "responseStreamDisconnected",
                "thread_id": "reviewer-thread",
                "prompt_digest": "b" * 64,
                "prior_turn_count": 1,
                "prior_tail_turn_id": "failed-turn",
                "prior_turns_digest": digest(prior_turns),
                "prepared_at": "2026-08-01T00:00:03+00:00",
            },
        }

        class CompleteOnlyCore:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def call(self, command: str, *args: str, input_value=None):
                self.calls.append(command)
                if command == "complete-dispatch-compaction":
                    return {
                        "status": "ACTIVE",
                        "dispatch_intent": {
                            "action_id": "a" * 64,
                            "state": "prepared",
                            "generation": 2,
                        },
                    }
                raise AssertionError(command)

        class NoRepeatTransport:
            def compact_thread(self, thread_id: str):
                raise AssertionError("persisted compaction must not repeat")

        core = CompleteOnlyCore()
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="reviewer-compaction-resume",
            core=core,
            transport=NoRepeatTransport(),
            project_root=ROOT,
            thread_compaction_available=True,
        )

        recovered = coordinator._recover_reviewer_thread_compaction(
            pending,
            {"turns": [*prior_turns, compaction_turn]},
        )

        self.assertEqual(recovered["dispatch_intent"]["generation"], 2)
        self.assertEqual(core.calls, ["complete-dispatch-compaction"])

    def test_missing_replacement_rollout_renews_and_binds_new_bootstrap(self) -> None:
        replacement_action_id = "a" * 64
        action = {
            "action": "tester_author",
            "action_id": "b" * 64,
            "reason": "tester_evidence_missing",
        }
        source = {
            "head": "1" * 40,
            "base_head": "1" * 40,
            "branch": "replacement-tester",
            "worktree": "/replacement-tester",
            "files": [],
            "replaces_files": [],
            "agent": {
                "agent_id": "codex-app-server:missing-thread",
                "thread_id": "missing-thread",
            },
        }
        replacement = {
            "action_id": replacement_action_id,
            "problem_key": "tester-bootstrap-missing",
            "stage": "awaiting_first_turn",
            "new_agent": copy.deepcopy(source["agent"]),
            "worktree": source["worktree"],
        }

        class FakeCore:
            def __init__(self) -> None:
                self.calls: list[tuple[str, tuple[str, ...]]] = []
                self.replacement = copy.deepcopy(replacement)

            def call(self, command: str, *args: str, input_value=None):
                self.calls.append((command, args))
                if command == "begin-tester-replacement":
                    self.replacement["new_agent"] = None
                    return {"status": "ACTIVE"}
                if command == "driver-context":
                    return {"tester_replacement_intent": copy.deepcopy(self.replacement)}
                if command == "bind-tester-replacement":
                    thread_id = args[args.index("--thread-id") + 1]
                    self.replacement["new_agent"] = {
                        "agent_id": f"codex-app-server:{thread_id}",
                        "thread_id": thread_id,
                    }
                    return {"status": "ACTIVE"}
                raise AssertionError(command)

        class FakeTransport:
            def start_thread(self, **kwargs):
                self.cwd = kwargs["cwd"]
                return "replacement-thread-2"

        context = {
            "facets": {"execution": {"tester_source": source}},
            "tester_replacement_intent": replacement,
            "dispatch_intent": None,
        }
        error = AppServerError(
            "no rollout found for thread id missing-thread",
            code="NATIVE_APP_SERVER_REQUEST_FAILED",
            details={
                "method": "thread/resume",
                "error": {
                    "code": -32600,
                    "message": "no rollout found for thread id missing-thread",
                },
            },
        )
        core = FakeCore()
        transport = FakeTransport()
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="replacement-missing-rollout",
            core=core,
            transport=transport,
            project_root=ROOT,
        )

        renewed = coordinator._renew_tester_bootstrap_after_missing_rollout(
            action, context, error
        )

        self.assertTrue(renewed)
        self.assertEqual(transport.cwd, source["worktree"])
        self.assertEqual(
            [command for command, _args in core.calls],
            [
                "begin-tester-replacement",
                "driver-context",
                "bind-tester-replacement",
            ],
        )
        self.assertEqual(
            core.replacement["new_agent"]["thread_id"],
            "replacement-thread-2",
        )
        self.assertIn("replacement-thread-2", coordinator._active_threads)

    def test_external_problem_needs_user_stops_without_agent_dispatch(self) -> None:
        class FakeCore:
            def __init__(self) -> None:
                self.calls: list[tuple[str, tuple[str, ...]]] = []

            def call(self, command: str, *args: str, input_value=None):
                self.calls.append((command, args))
                if command == "driver-next":
                    return {
                        "driver_protocol_version": 1,
                        "status": "NEEDS_USER",
                        "action": "external_problem_decision",
                        "reason": "open_external_platform_problem",
                        "action_id": "a" * 64,
                        "problem": {
                            "key": "external-probe-unavailable",
                            "owner": "external_platform",
                            "status": "open",
                        },
                    }
                if command == "driver-context":
                    return {
                        "repo_root": str(ROOT),
                        "candidate_worktree": str(ROOT),
                        "facets": native_contract(ROOT),
                        "evidence": {},
                        "publication": None,
                        "problems": [],
                    }
                raise AssertionError(f"unexpected command: {command}")

        class NoDispatchTransport:
            def start_thread(self, **_):
                raise AssertionError("external problem must not start an agent")

            def run_turn(self, **_):
                raise AssertionError("external problem must not dispatch an agent")

        core = FakeCore()
        result = NativeCoordinator(
            repo=ROOT,
            run_id="native-external-needs-user",
            core=core,
            transport=NoDispatchTransport(),
            project_root=ROOT,
        ).run()

        self.assertEqual(result.get("status"), "NEEDS_USER", result)
        self.assertEqual(
            result.get("decision", {}).get("problem", {}).get("key"),
            "external-probe-unavailable",
        )
        commands = [command for command, _args in core.calls]
        self.assertEqual(commands[0], "driver-next")
        for forbidden in (
            "prepare-builder",
            "begin-dispatch",
            "record-problems",
            "checkpoint-builder",
        ):
            self.assertNotIn(forbidden, commands)

    def test_reviewer_prompt_maps_v4_contract_to_frozen_review_inputs(self) -> None:
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="native-review-inputs",
            core=object(),
            transport=object(),
            project_root=ROOT,
        )
        context = {
            "target_start_head": "1" * 40,
            "candidate_worktree": str(ROOT),
            "facets": native_contract(ROOT),
            "evidence": {},
            "doc_reference_scan": {
                "status": "pass",
                "candidate_head": "2" * 40,
                "semantic_checks": [
                    {"file": "README.md", "question": "Verify the symbol reference."}
                ],
            },
            "doc_reference_scan_state": "pass",
            "publication": None,
            "problems": [],
        }
        context["facets"]["mission"]["delivery_kind"] = "documentation"
        context["facets"]["authority"]["builder_write"] = ["README.md"]
        context["facets"]["assurance"] = {
            "required": ["reviewer", "doc_review"],
            "machine_commands": [],
        }
        context["facets"]["execution"]["candidate_head"] = "2" * 40
        prompt = coordinator._prompt(
            {"action": "reviewer_final", "action_id": "a" * 64},
            "reviewer",
            context,
        )
        payload = json.loads(prompt.split("\n", 1)[1])
        self.assertEqual(payload["prompt_contract_version"], 2)
        review = payload["review_input_contract"]
        self.assertEqual(review["accepted_plan"]["json_pointer"], "/contract")
        self.assertEqual(
            review["accepted_plan"]["digest"], digest(payload["contract"])
        )
        self.assertEqual(review["verification_mode"], "L1-documentation-only")
        self.assertEqual(review["spec_head"], "1" * 40)
        self.assertEqual(review["candidate_head"], "2" * 40)
        self.assertEqual(review["plan_checklist"]["behaviors"], context["facets"]["mission"]["behaviors"])
        self.assertEqual(review["documentation_spec"]["authorized_paths"], ["README.md"])
        self.assertEqual(review["complete_diff"]["argv"][-1], f"{'1' * 40}..{'2' * 40}")
        self.assertTrue(Path(review["documentation_policy_path"]).is_file())
        self.assertEqual(review["doc_reference_scan_state"], "pass")
        self.assertEqual(
            review["doc_reference_scan"]["json_pointer"], "/doc_reference_scan"
        )
        self.assertEqual(
            payload["doc_reference_scan"]["semantic_checks"][0]["file"], "README.md"
        )

    def test_reviewer_preflight_prompt_is_early_evidence_not_the_final_gate(self) -> None:
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="native-review-preflight",
            core=object(),
            transport=object(),
            project_root=ROOT,
        )
        context = {
            "target_start_head": "1" * 40,
            "candidate_worktree": str(ROOT),
            "facets": native_contract(ROOT),
            "evidence": {
                "tester": {"status": "pass"},
                "preflight": {"status": "pass"},
            },
            "publication": None,
            "problems": [],
        }
        context["facets"]["execution"]["candidate_head"] = "2" * 40
        context["facets"]["assurance"]["preflight_before_proof"] = True
        context["facets"]["assurance"]["reviewer_preflight"] = True
        context["facets"]["assurance"]["machine_commands"][0][
            "run_before_full_suite"
        ] = True
        prompt = coordinator._prompt(
            {"action": "reviewer_preflight", "action_id": "a" * 64},
            "reviewer",
            context,
        )
        payload = json.loads(prompt.split("\n", 1)[1])
        self.assertEqual(payload["phase"], "preflight")
        review = payload["review_input_contract"]
        self.assertEqual(review["review_phase"], "preflight")
        self.assertEqual(review["pre_turn_gates"]["required"], ["tester", "preflight"])
        self.assertNotIn("machine", review["pre_turn_gates"]["required"])
        self.assertNotIn("blackbox", review["pre_turn_gates"]["required"])
        self.assertEqual(
            review["pre_turn_gates"]["evidence"]["json_pointer"], "/evidence"
        )
        self.assertEqual(
            review["pre_turn_gates"]["evidence"]["digest"],
            digest(payload["evidence"]),
        )

        final_prompt = coordinator._prompt(
            {"action": "reviewer_final", "action_id": "b" * 64},
            "reviewer",
            context,
        )
        final_review = json.loads(final_prompt.split("\n", 1)[1])[
            "review_input_contract"
        ]
        self.assertEqual(final_review["review_phase"], "final")
        self.assertIn("machine", final_review["pre_turn_gates"]["required"])
        self.assertIn("blackbox", final_review["pre_turn_gates"]["required"])

        blackbox_only = json.loads(json.dumps(context))
        blackbox_only["facets"]["assurance"]["required"] = [
            "machine",
            "blackbox",
            "reviewer",
        ]
        blackbox_only_review = coordinator._review_input_contract(
            blackbox_only,
            phase="final",
        )
        self.assertNotIn(
            "tester",
            blackbox_only_review["pre_turn_gates"]["required"],
        )
        self.assertIn(
            "Tester author, source, integration, and tester evidence are not gates",
            blackbox_only_review["pre_turn_gates"]["requirement_rule"],
        )

    def test_reviewer_prompt_does_not_duplicate_large_authoritative_facts(self) -> None:
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="native-review-prompt-projection",
            core=object(),
            transport=object(),
            project_root=ROOT,
        )
        marker = "unique-machine-output-" + ("x" * 200_000)
        context = {
            "target_start_head": "1" * 40,
            "candidate_worktree": str(ROOT),
            "facets": native_contract(ROOT),
            "evidence": {
                "machine": {
                    "status": "pass",
                    "details": {"stdout": marker},
                }
            },
            "doc_reference_scan": None,
            "doc_reference_scan_state": "missing",
            "publication": None,
            "problems": [],
        }
        context["facets"]["execution"]["candidate_head"] = "2" * 40

        prompt = coordinator._prompt(
            {"action": "reviewer_final", "action_id": "a" * 64},
            "reviewer",
            context,
        )
        payload = json.loads(prompt.split("\n", 1)[1])

        self.assertEqual(prompt.count(marker), 1)
        self.assertLess(len(prompt), len(marker) + 25_000)
        self.assertEqual(
            payload["review_input_contract"]["pre_turn_gates"]["evidence"],
            NativeCoordinator._prompt_source_ref(payload["evidence"], "/evidence"),
        )

    def test_role_contracts_follow_frozen_optional_tester_gate(self) -> None:
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="native-optional-tester-gate",
            core=object(),
            transport=object(),
            project_root=ROOT,
        )
        reviewer, _ = coordinator._role_config("reviewer")
        tester, _ = coordinator._role_config("tester")
        self.assertIn("pre_turn_gates.required", reviewer)
        self.assertIn("不含 `tester`", reviewer)
        self.assertIn("blackbox-only contract", tester)
        self.assertIn("`tester_source` 保持 null", tester)

    def test_tester_prompt_freezes_canonical_test_identity_rules(self) -> None:
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="native-test-identities",
            core=object(),
            transport=object(),
            project_root=ROOT,
        )
        context = {
            "target_start_head": "1" * 40,
            "candidate_worktree": str(ROOT),
            "facets": native_contract(ROOT),
            "evidence": {},
            "publication": None,
            "problems": [],
        }
        context["facets"]["execution"]["tester_source"] = {
            "head": "2" * 40,
            "files": [{"path": "tests/test_calc.py", "blob": "3" * 40}],
        }
        prompt = coordinator._prompt(
            {"action": "tester_proof", "action_id": "a" * 64},
            "tester",
            context,
        )
        payload = json.loads(prompt.split("\n", 1)[1])
        self.assertIn("boundCaseResult", payload["blackbox_case_schema"]["$defs"])
        self.assertIn("tests.test_calc", payload["test_identity_contract"]["unittest"])
        self.assertEqual(
            payload["proof_test_id_hints"][0]["unittest_module"], "tests.test_calc"
        )
        self.assertIn("exactly", payload["proof_execution_rule"])

    def test_proof_failure_prompt_reuses_tester_thread_for_read_only_diagnosis(self) -> None:
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="native-proof-diagnosis",
            core=object(),
            transport=object(),
            project_root=ROOT,
        )
        context = {
            "target_start_head": "1" * 40,
            "candidate_worktree": str(ROOT),
            "facets": native_contract(ROOT),
            "evidence": {},
            "publication": None,
            "problems": [],
        }
        failure = {
            "action_id": "a" * 64,
            "failure_digest": "b" * 64,
            "failure": {
                "status": "FAIL",
                "code": "TEST_PROOF_CANDIDATE_FAILED",
                "message": "candidate failed",
                "details": {"group": 0},
            },
        }
        prompt = coordinator._prompt(
            {
                "action": "tester_proof_diagnose",
                "action_id": "c" * 64,
                "proof_failure": failure,
            },
            "tester",
            context,
        )
        payload = json.loads(prompt.split("\n", 1)[1])
        self.assertEqual(payload["phase"], "proof_diagnose")
        self.assertEqual(payload["proof_failure"], failure)
        self.assertIn("proof_spec_schema", payload)
        self.assertIn("Do not edit files", payload["proof_diagnosis_rule"])
        self.assertIn(
            "replacement",
            payload["result_field_contract"]["proof_spec"],
        )

    def test_machine_failure_prompt_reuses_tester_thread_for_read_only_diagnosis(self) -> None:
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="native-machine-diagnosis",
            core=object(),
            transport=object(),
            project_root=ROOT,
        )
        context = {
            "target_start_head": "1" * 40,
            "candidate_worktree": str(ROOT),
            "facets": native_contract(ROOT),
            "evidence": {},
            "publication": None,
            "problems": [],
        }
        failure = {
            "stage": "machine",
            "action_id": "a" * 64,
            "failure_signature": "b" * 64,
            "recovery": "tester_diagnosis",
            "results": [{"id": "fixture-tests", "returncode": 1}],
        }
        prompt = coordinator._prompt(
            {
                "action": "tester_machine_diagnose",
                "action_id": "c" * 64,
                "machine_failure": failure,
            },
            "tester",
            context,
        )
        payload = json.loads(prompt.split("\n", 1)[1])
        self.assertEqual(payload["phase"], "machine_diagnose")
        self.assertEqual(payload["machine_failure"], failure)
        self.assertIn("Do not edit files", payload["machine_diagnosis_rule"])
        self.assertIn(
            "required_non_empty",
            payload["result_field_contract"]["problem_report"],
        )

    def test_recorded_proof_core_failure_is_consumed_but_unrecorded_error_is_not(self) -> None:
        action_id = "a" * 64
        error = CorePortError(
            "candidate failed",
            payload={"status": "FAIL", "code": "TEST_PROOF_CANDIDATE_FAILED"},
            returncode=1,
        )

        class FakeCore:
            def __init__(self, *, recorded: bool) -> None:
                self.recorded = recorded
                self.calls: list[str] = []

            def call(self, command: str, *args: str, input_value=None):
                self.calls.append(command)
                if command == "prove-tests":
                    raise error
                if command == "driver-context":
                    return {
                        "proof_failure_state": "current" if self.recorded else "missing",
                        "proof_failure": (
                            {
                                "action_id": action_id,
                                "failure_digest": "b" * 64,
                                "failure": {
                                    "status": "FAIL",
                                    "code": "TEST_PROOF_CANDIDATE_FAILED",
                                },
                            }
                            if self.recorded
                            else None
                        ),
                    }
                if command == "consume-dispatch":
                    return {"status": "ACTIVE"}
                raise AssertionError(command)

        context = {
            "facets": {
                "authority": {"tester_write": []},
                "execution": {
                    "agents": {
                        "tester": {
                            "agent_id": "tester-agent",
                            "thread_id": "tester-thread",
                        }
                    },
                    "tester_source": None,
                },
            }
        }
        result = {
            "result": "pass",
            "evidence_report": None,
            "proof_spec": {"schema_version": 1, "groups": []},
            "problem_report": None,
        }
        action = {"action": "tester_proof", "action_id": action_id}

        recorded_core = FakeCore(recorded=True)
        NativeCoordinator(
            repo=ROOT,
            run_id="native-proof-recorded",
            core=recorded_core,
            transport=object(),
            project_root=ROOT,
        )._apply_agent_result(action, "tester", result, context)
        self.assertEqual(
            recorded_core.calls,
            ["prove-tests", "driver-context", "consume-dispatch"],
        )

        unrecorded_core = FakeCore(recorded=False)
        with self.assertRaises(CorePortError):
            NativeCoordinator(
                repo=ROOT,
                run_id="native-proof-unrecorded",
                core=unrecorded_core,
                transport=object(),
                project_root=ROOT,
            )._apply_agent_result(action, "tester", result, context)
        self.assertNotIn("consume-dispatch", unrecorded_core.calls)

    def test_proof_diagnosis_records_problem_before_dispatch_consumption(self) -> None:
        class FakeCore:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def call(self, command: str, *args: str, input_value=None):
                self.calls.append(command)
                return {"status": "ACTIVE"}

        core = FakeCore()
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="native-proof-problem",
            core=core,
            transport=object(),
            project_root=ROOT,
        )
        context = {
            "facets": {
                "execution": {
                    "agents": {
                        "tester": {
                            "agent_id": "tester-agent",
                            "thread_id": "tester-thread",
                        }
                    }
                }
            }
        }
        coordinator._apply_agent_result(
            {"action": "tester_proof_diagnose", "action_id": "a" * 64},
            "tester",
            {
                "result": "fail",
                "evidence_report": None,
                "proof_spec": None,
                "problem_report": {
                    "schema_version": 1,
                    "problems": [
                        {
                            "key": "candidate-contract-failure",
                            "summary": "Candidate violates the frozen behavior.",
                            "details": "The persisted candidate test assertion failed.",
                            "owner": "builder",
                        }
                    ],
                },
            },
            context,
        )
        self.assertEqual(core.calls, ["record-problems", "consume-dispatch"])

        with self.assertRaisesRegex(NativeDriverError, "unsupported owner"):
            coordinator._validate_proof_diagnosis(
                {
                    "result": "fail",
                    "problem_report": {
                        "schema_version": 1,
                        "problems": [
                            {
                                "key": "runtime-defect",
                                "summary": "Runtime defect.",
                                "details": "Not a target correction.",
                                "owner": "builder_loop",
                            }
                        ],
                    },
                }
            )

    def test_proof_diagnosis_can_retry_a_replacement_spec_without_source_integration(self) -> None:
        original_spec = {"schema_version": 1, "groups": [{"original": True}]}
        replacement_spec = {"schema_version": 1, "groups": []}

        class FakeCore:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def call(self, command: str, *args: str, input_value=None):
                self.calls.append(command)
                return {"status": "ACTIVE"}

        core = FakeCore()
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="native-proof-spec-correction",
            core=core,
            transport=object(),
            project_root=ROOT,
        )
        action = {
            "action": "tester_proof_diagnose",
            "action_id": "c" * 64,
            "proof_failure": {
                "action_id": "a" * 64,
                "candidate_head": "1" * 40,
                "tester_source_head": "2" * 40,
                "dependency_digest": "d" * 64,
                "producer": {
                    "role": "tester",
                    "agent_id": "tester-agent",
                    "thread_id": "tester-thread",
                },
                "spec": original_spec,
                "spec_digest": digest(original_spec),
                "failure_digest": "f" * 64,
                "failure": {
                    "status": "FAIL",
                    "code": "TEST_PROOF_CANDIDATE_FAILED",
                },
            },
        }
        context = {
            "facets": {
                "authority": {"tester_write": []},
                "execution": {
                    "candidate_head": "1" * 40,
                    "tester_source": {
                        "head": "2" * 40,
                        "files": [],
                    },
                    "agents": {
                        "tester": {
                            "agent_id": "tester-agent",
                            "thread_id": "tester-thread",
                        }
                    },
                },
            }
        }
        coordinator._apply_agent_result(
            action,
            "tester",
            {
                "result": "tests_ready",
                "evidence_report": None,
                "proof_spec": replacement_spec,
                "problem_report": None,
            },
            context,
        )
        self.assertEqual(core.calls, ["prove-tests", "consume-dispatch"])
        self.assertNotIn("integrate-tester", core.calls)
        self.assertNotIn("record-problems", core.calls)

        with self.assertRaisesRegex(NativeDriverError, "did not change"):
            coordinator._validate_proof_diagnosis(
                {
                    "result": "tests_ready",
                    "evidence_report": None,
                    "proof_spec": original_spec,
                    "problem_report": None,
                },
                proof_failure=action["proof_failure"],
            )

        class CanonicalizingCoordinator(NativeCoordinator):
            def _bind_proof_test_ids(self, spec, context):
                return original_spec

        canonicalizing = CanonicalizingCoordinator(
            repo=ROOT,
            run_id="native-proof-spec-canonical-no-progress",
            core=core,
            transport=object(),
            project_root=ROOT,
        )
        calls_before = list(core.calls)
        with self.assertRaisesRegex(NativeDriverError, "did not change"):
            canonicalizing._apply_agent_result(
                action,
                "tester",
                {
                    "result": "tests_ready",
                    "evidence_report": None,
                    "proof_spec": replacement_spec,
                    "problem_report": None,
                },
                context,
            )
        self.assertEqual(core.calls, calls_before)

    def test_builder_wire_drops_unowned_evidence_fields(self) -> None:
        result = NativeCoordinator._normalize_action_result(
            "builder_implement",
            {
                "result": "implemented",
                "evidence_report": {"checks": ["git diff"]},
                "proof_spec": {"unexpected": True},
                "problem_report": None,
            },
        )
        self.assertIsNone(result["evidence_report"])
        self.assertIsNone(result["proof_spec"])
        self.assertIsNone(result["problem_report"])

    def test_tester_evidence_is_bound_to_post_integration_candidate(self) -> None:
        source = {
            "schema_version": 1,
            "kind": "tester",
            "status": "pass",
            "candidate_head": "1" * 40,
            "producer": {
                "role": "tester",
                "agent_id": "tester-agent",
                "thread_id": "tester-thread",
            },
            "details": {"source_head": "1" * 40, "result": "tests_ready"},
        }
        bound = NativeCoordinator._bind_evidence_candidate(source, "2" * 40)
        self.assertEqual(bound["candidate_head"], "2" * 40)
        self.assertEqual(bound["details"]["source_head"], "1" * 40)
        self.assertEqual(source["candidate_head"], "1" * 40)

    def test_partial_tester_manifest_is_bound_before_evidence_recording(self) -> None:
        action_id = "a" * 64
        candidate_head = "2" * 40
        source_head = "3" * 40
        canonical_files = [
            {"path": "tests/test_native_driver_v1.py", "blob": "4" * 40},
            {"path": "tests/test_assurance_v4_contract.py", "blob": "5" * 40},
        ]
        integrated_context = {
            "facets": {
                "execution": {
                    "candidate_head": candidate_head,
                    "tester_source": {
                        "head": source_head,
                        "files": canonical_files,
                    },
                }
            },
            "driver_failure": None,
        }

        class FakeCore:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.recorded_report: dict | None = None

            def call(self, command: str, *args: str, input_value=None):
                self.calls.append(command)
                if command == "driver-context":
                    return integrated_context
                if command == "record-evidence":
                    self.recorded_report = input_value
                return {"status": "ACTIVE"}

        raw_evidence = {
            "schema_version": 1,
            "kind": "tester",
            "status": "pass",
            "candidate_head": "6" * 40,
            "producer": {
                "role": "tester",
                "agent_id": "tester-agent",
                "thread_id": "tester-thread",
            },
            "details": {
                "result": "tests_ready",
                "source_head": "7" * 40,
                "files": [
                    *canonical_files,
                    {"path": "tests/test_driver_failure_contract.py", "blob": "8" * 40},
                ],
            },
        }
        raw_before = json.loads(json.dumps(raw_evidence))
        core = FakeCore()
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="native-partial-tester-manifest",
            core=core,
            transport=object(),
            project_root=ROOT,
        )
        context = {
            "facets": {
                "execution": {
                    "agents": {
                        "tester": {
                            "agent_id": "tester-agent",
                            "thread_id": "tester-thread",
                        }
                    }
                }
            }
        }

        coordinator._apply_agent_result(
            {"action": "tester_fix", "action_id": action_id},
            "tester",
            {
                "result": "tests_ready",
                "evidence_report": raw_evidence,
                "proof_spec": None,
                "problem_report": None,
            },
            context,
        )

        self.assertEqual(
            core.calls,
            [
                "integrate-tester",
                "driver-context",
                "record-evidence",
                "consume-dispatch",
            ],
        )
        self.assertNotIn("record-driver-failure", core.calls)
        self.assertEqual(
            core.recorded_report,
            {
                "schema_version": 1,
                "kind": "tester",
                "status": "pass",
                "candidate_head": candidate_head,
                "producer": {
                    "role": "tester",
                    "agent_id": "tester-agent",
                    "thread_id": "tester-thread",
                },
                "details": {
                    "result": "tests_ready",
                    "source_head": source_head,
                    "files": canonical_files,
                },
            },
        )
        self.assertEqual(raw_evidence, raw_before)

    def test_existing_role_thread_is_resumed_before_a_new_action(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.resumed = []

            def resume_thread(self, **kwargs):
                self.resumed.append(kwargs)

        transport = FakeTransport()
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="native-thread-activation",
            core=object(),
            transport=transport,
            project_root=ROOT,
        )
        context = {
            "repo_root": str(ROOT),
            "candidate_worktree": str(ROOT),
            "facets": native_contract(ROOT),
        }
        coordinator._activate_role_thread(
            {"action": "tester_proof"}, "tester", context, "tester-thread"
        )
        coordinator._activate_role_thread(
            {"action": "tester_blackbox"}, "tester", context, "tester-thread"
        )
        self.assertEqual(len(transport.resumed), 1)
        self.assertEqual(transport.resumed[0]["thread_id"], "tester-thread")

    def test_proof_ids_are_bound_to_the_unique_tester_module(self) -> None:
        repo = init_repo()
        try:
            test_path = repo / "tests" / "test_calculator.py"
            test_path.write_text(
                "import unittest\n\n"
                "class AddTests(unittest.TestCase):\n"
                "    def test_add(self):\n"
                "        self.assertEqual(2 + 3, 5)\n",
                encoding="utf-8",
            )
            commit_all(repo, "add tester source")
            source_head = run_process(
                ["git", "rev-parse", "HEAD"], cwd=repo
            ).stdout.strip()
            context = {"facets": native_contract(repo)}
            context["facets"]["authority"]["tester_write"] = ["tests/**"]
            context["facets"]["execution"]["tester_source"] = {
                "head": source_head,
                "files": [{"path": "tests/test_calculator.py", "blob": "0" * 40}],
            }
            coordinator = NativeCoordinator(
                repo=repo,
                run_id="native-proof-ids",
                core=object(),
                transport=object(),
                project_root=ROOT,
            )
            bound = coordinator._bind_proof_test_ids(
                {
                    "schema_version": 1,
                    "groups": [
                        {
                            "method": "baseline-red",
                            "argv": ["python3", "-m", "unittest", "discover"],
                            "test_ids": ["test_calculator.AddTests.test_add"],
                        }
                    ],
                },
                context,
            )
            self.assertEqual(
                bound["groups"][0]["test_ids"],
                ["tests.test_calculator.AddTests.test_add"],
            )
        finally:
            cleanup_repo(repo)

    def test_coordinator_binds_builder_dispatches_once_and_stops(self) -> None:
        with tempfile.TemporaryDirectory(prefix="native-coordinator-") as raw:
            root = Path(raw)
            candidate = root / "candidate"
            candidate.mkdir()

            class FakeCore:
                def __init__(self) -> None:
                    self.agent = None
                    self.pending = None
                    self.done = False
                    self.calls: list[str] = []
                    self.consume_args: list[tuple[str, ...]] = []

                def call(self, command: str, *args: str, input_value=None):
                    self.calls.append(command)
                    if command == "driver-next":
                        if self.done:
                            return {
                                "driver_protocol_version": 1,
                                "status": "STOP",
                                "action": "none",
                                "reason": "finalized",
                                "action_id": "f" * 64,
                            }
                        return {
                            "driver_protocol_version": 1,
                            "status": "CONTINUE",
                            "action": "builder_implement",
                            "reason": "candidate_missing",
                            "action_id": "a" * 64,
                        }
                    if command == "driver-context":
                        return {
                            "repo_root": str(root),
                            "target_start_head": "0" * 40,
                            "candidate_worktree": str(candidate),
                            "facets": {
                                "mission": {},
                                "authority": {},
                                "assurance": {"required": []},
                                "execution": {"agents": {"builder": self.agent}},
                            },
                            "evidence": {},
                            "publication": None,
                            "problems": [],
                            "dispatch_intent": self.pending,
                        }
                    if command == "prepare-builder":
                        self.agent = {
                            "agent_id": "codex-app-server:thr-builder",
                            "thread_id": "thr-builder",
                        }
                    elif command == "begin-dispatch":
                        self.pending = {"state": "prepared"}
                    elif command == "bind-dispatch-turn":
                        self.pending = {"state": "in_flight"}
                    elif command == "complete-dispatch":
                        self.pending = {"state": "completed"}
                    elif command == "consume-dispatch":
                        self.consume_args.append(args)
                        self.pending = None
                        self.done = True
                    return {"status": "ACTIVE"}

            class FakeTransport:
                def start_thread(self, **_):
                    return "thr-builder"

                def run_turn(self, *, on_started, **_):
                    on_started("turn-builder")
                    return TurnResult(
                        turn_id="turn-builder",
                        status="completed",
                        text=json.dumps(
                            {
                                "result": "implemented",
                                "evidence_report": None,
                                "proof_spec": None,
                                "problem_report": None,
                            }
                        ),
                    )

            core = FakeCore()
            result = NativeCoordinator(
                repo=root,
                run_id="native-coordinator",
                core=core,
                transport=FakeTransport(),
                project_root=ROOT,
            ).run()
            self.assertEqual(result["status"], "FINALIZED")
            self.assertEqual(core.calls.count("prepare-builder"), 1)
            self.assertEqual(core.calls.count("begin-dispatch"), 1)
            self.assertEqual(core.calls.count("consume-dispatch"), 1)
            self.assertEqual(len(core.consume_args), 1)
            self.assertIn("--consumer-source", core.consume_args[0])
            source_index = core.consume_args[0].index("--consumer-source") + 1
            self.assertEqual(core.consume_args[0][source_index], "native_driver")

    def test_builder_fix_result_checkpoints_before_consumption_and_replays_after_crash(self) -> None:
        class FakeCore:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.fail_consume_once = True

            def call(self, command: str, *args: str, input_value=None):
                self.calls.append(command)
                if command == "consume-dispatch" and self.fail_consume_once:
                    self.fail_consume_once = False
                    raise RuntimeError("simulated crash after checkpoint")
                return {"status": "ACTIVE"}

        core = FakeCore()
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="native-builder-fix-replay",
            core=core,
            transport=object(),
            project_root=ROOT,
        )
        action = {"action": "builder_fix", "action_id": "a" * 64}
        context = {
            "facets": {
                "execution": {
                    "agents": {
                        "builder": {
                            "agent_id": "builder-agent",
                            "thread_id": "builder-thread",
                        }
                    }
                }
            }
        }
        result = {
            "result": "implemented",
            "evidence_report": None,
            "proof_spec": None,
            "problem_report": None,
        }

        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            coordinator._apply_agent_result(
                action, "builder", result, context
            )
        self.assertEqual(core.calls, ["checkpoint-builder", "consume-dispatch"])

        coordinator._apply_agent_result(action, "builder", result, context)

        self.assertEqual(
            core.calls,
            [
                "checkpoint-builder",
                "consume-dispatch",
                "checkpoint-builder",
                "consume-dispatch",
            ],
        )

    def test_recomposition_fix_advances_persisted_transaction_before_consumption(self) -> None:
        class FakeCore:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def call(self, command: str, *args: str, input_value=None):
                self.calls.append(command)
                return {"status": "ACTIVE"}

        for action_name, role in (
            ("builder_recompose_fix", "builder"),
            ("tester_recompose_fix", "tester"),
        ):
            with self.subTest(action=action_name):
                core = FakeCore()
                coordinator = NativeCoordinator(
                    repo=ROOT,
                    run_id=f"native-{action_name}",
                    core=core,
                    transport=object(),
                    project_root=ROOT,
                )
                context = {
                    "facets": {
                        "execution": {
                            "agents": {
                                role: {
                                    "agent_id": f"{role}-agent",
                                    "thread_id": f"{role}-thread",
                                }
                            }
                        }
                    }
                }
                coordinator._apply_agent_result(
                    {"action": action_name, "action_id": "a" * 64},
                    role,
                    {
                        "result": "implemented",
                        "evidence_report": None,
                        "proof_spec": None,
                        "problem_report": None,
                    },
                    context,
                )
                self.assertEqual(
                    core.calls, ["recompose-candidate", "consume-dispatch"]
                )

    def test_coordinator_executes_deployment_restore_before_blackbox_completion(self) -> None:
        with tempfile.TemporaryDirectory(prefix="native-deployment-actions-") as raw:
            root = Path(raw)

            class FakeCore:
                def __init__(self) -> None:
                    self.actions = iter(
                        [
                            "prepare_deployment",
                            "restore_deployment",
                            "complete_blackbox",
                        ]
                    )
                    self.calls: list[str] = []

                def call(self, command: str, *args: str, input_value=None):
                    self.calls.append(command)
                    if command == "driver-next":
                        try:
                            action = next(self.actions)
                        except StopIteration:
                            return {
                                "driver_protocol_version": 1,
                                "status": "STOP",
                                "action": "none",
                                "reason": "finalized",
                                "action_id": "f" * 64,
                            }
                        return {
                            "driver_protocol_version": 1,
                            "status": "CONTINUE",
                            "action": action,
                            "reason": action,
                            "action_id": (action[0] * 64)[:64],
                        }
                    return {"status": "ACTIVE"}

            core = FakeCore()
            result = NativeCoordinator(
                repo=root,
                run_id="native-deployment-actions",
                core=core,
                transport=object(),
                project_root=ROOT,
            ).run()
            self.assertEqual(result["status"], "FINALIZED")
            self.assertEqual(
                [
                    item
                    for item in core.calls
                    if item
                    in {"prepare-deployment", "restore-deployment", "complete-blackbox"}
                ],
                ["prepare-deployment", "restore-deployment", "complete-blackbox"],
            )

    def test_coordinator_executes_doc_reference_scan_action(self) -> None:
        class FakeCore:
            def __init__(self) -> None:
                self.actions = iter(["scan_doc_references"])
                self.calls: list[str] = []

            def call(self, command: str, *args: str, input_value=None):
                self.calls.append(command)
                if command == "driver-next":
                    try:
                        action = next(self.actions)
                    except StopIteration:
                        return {
                            "driver_protocol_version": 1,
                            "status": "STOP",
                            "action": "none",
                            "reason": "finalized",
                            "action_id": "f" * 64,
                        }
                    return {
                        "driver_protocol_version": 1,
                        "status": "CONTINUE",
                        "action": action,
                        "reason": action,
                        "action_id": "a" * 64,
                    }
                return {"status": "ACTIVE"}

        core = FakeCore()
        result = NativeCoordinator(
            repo=ROOT,
            run_id="native-doc-reference-scan",
            core=core,
            transport=object(),
            project_root=ROOT,
        ).run()
        self.assertEqual(result["status"], "FINALIZED")
        self.assertIn("scan-doc-references", core.calls)

    def test_invalid_deployment_blackbox_requires_restore_before_consumption(self) -> None:
        class FakeCore:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def call(self, command: str, *args: str, input_value=None):
                self.calls.append(command)
                if command == "stage-blackbox":
                    raise CorePortError(
                        "observation binding mismatch",
                        payload={
                            "status": "FATAL",
                            "code": "BLACKBOX_OBSERVATION_BINDING_MISMATCH",
                        },
                        returncode=2,
                    )
                return {"status": "ACTIVE"}

        core = FakeCore()
        coordinator = NativeCoordinator(
            repo=ROOT,
            run_id="native-invalid-deployment-blackbox",
            core=core,
            transport=object(),
            project_root=ROOT,
        )
        coordinator._apply_agent_result(
            {"action": "tester_blackbox", "action_id": "a" * 64},
            "tester",
            {
                "result": "pass",
                "evidence_report": {"kind": "blackbox"},
                "proof_spec": None,
                "problem_report": None,
            },
            {
                "facets": {
                    "execution": {
                        "deployment": {"target_id": "fixture"},
                        "agents": {
                            "tester": {
                                "agent_id": "tester-agent",
                                "thread_id": "tester-thread",
                            }
                        },
                    }
                }
            },
        )
        self.assertEqual(
            core.calls,
            ["stage-blackbox", "require-deployment-restore"],
        )

    def test_coordinator_executes_supersede_recovery_actions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="native-supersede-actions-") as raw:
            root = Path(raw)

            class FakeCore:
                def __init__(self) -> None:
                    self.actions = iter(
                        ["complete_supersede_transfer", "restore_superseded_environment"]
                    )
                    self.calls: list[str] = []

                def call(self, command: str, *args: str, input_value=None):
                    self.calls.append(command)
                    if command == "driver-next":
                        try:
                            action = next(self.actions)
                        except StopIteration:
                            return {
                                "driver_protocol_version": 1,
                                "status": "STOP",
                                "action": "none",
                                "reason": "finalized",
                                "action_id": "f" * 64,
                            }
                        return {
                            "driver_protocol_version": 1,
                            "status": "CONTINUE",
                            "action": action,
                            "reason": action,
                            "action_id": (action[0] * 64)[:64],
                        }
                    return {"status": "ACTIVE"}

            core = FakeCore()
            result = NativeCoordinator(
                repo=root,
                run_id="native-supersede-actions",
                core=core,
                transport=object(),
                project_root=ROOT,
            ).run()
            self.assertEqual(result["status"], "FINALIZED")
            self.assertIn("complete-supersede-transfer", core.calls)
            self.assertIn("restore-superseded-environment", core.calls)


class NativeProofRecoveryBlackboxContractTest(unittest.TestCase):
    def test_real_recovery_canary_uses_public_transactions_and_installed_app_server(
        self,
    ) -> None:
        path = ROOT / "tests" / "helpers" / "native_proof_recovery_blackbox.py"
        source = path.read_text(encoding="utf-8")
        compact = "".join(source.split()).lower()

        self.assertIn('"native-driver"', source)
        self.assertIn('"--codex-bin"', source)
        self.assertIn('"resume"', source)
        self.assertIn("probe_app_server", source)
        self.assertIn("signal.SIGKILL", source)
        for command in (
            "begin-dispatch",
            "complete-dispatch",
            "prove-tests",
            "consume-dispatch",
            "driver-next",
            "status",
        ):
            with self.subTest(command=command):
                self.assertIn(command, source)
        self.assertIn("architecture_review", source)
        self.assertIn("proof_attempts(final_status)==3", compact)
        self.assertNotIn("subprocess", compact)
        self.assertNotIn("pytest.main", compact)
        self.assertNotRegex(
            compact,
            r"ledger(?:\.json)?[^\n]{0,80}(?:write_text|write_bytes|json\.dump)",
        )


if __name__ == "__main__":
    unittest.main()
