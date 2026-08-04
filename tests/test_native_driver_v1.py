from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from harness import CLI, ROOT, cleanup_repo, commit_all, init_repo, run_process

sys.path.insert(0, str(ROOT / "runtime"))

from codex_builder_loop.native_driver.app_server import (
    AppServerTransport,
    TurnResult,
    probe_app_server,
)
from codex_builder_loop.native_driver.coordinator import NativeCoordinator
from codex_builder_loop.native_driver.core_port import CorePort


def native_contract(repo: Path) -> dict:
    return {
        "schema_version": 4,
        "mission": {
            "revision": 1,
            "objective": "Deliver a native driver fixture.",
            "behaviors": [{"id": "native-loop", "description": "Native Driver advances one action."}],
            "interfaces": [],
            "acceptance_cases": [{"id": "final", "description": "The run can finalize."}],
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

    def start(self) -> tuple[str, Path]:
        run_id = "native-driver-core"
        result = self.invoke(
            "start",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--session-id",
            "native-session",
            "--contract",
            "-",
            "--driver-kind",
            "native",
            "--driver-transport",
            "codex_app_server",
            "--driver-runtime-version",
            "codex-test",
            "--driver-protocol-schema-digest",
            "a" * 64,
            stdin=native_contract(self.repo),
        )
        return run_id, Path(result["candidate_worktree"]).parent

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
    tokens = ['thread/start','thread/resume','thread/read','turn/start','turn/interrupt','developerInstructions','outputSchema','clientUserMessageId']
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

    def test_probe_and_thread_turn_use_versioned_native_protocol(self) -> None:
        capability = probe_app_server(str(self.codex))
        self.assertEqual(capability.runtime_version, "codex-cli fake-native")
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


class NativeCoordinatorContractTest(unittest.TestCase):
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
        review = payload["review_input_contract"]
        self.assertEqual(review["verification_mode"], "L1-documentation-only")
        self.assertEqual(review["spec_head"], "1" * 40)
        self.assertEqual(review["candidate_head"], "2" * 40)
        self.assertEqual(review["plan_checklist"]["behaviors"], context["facets"]["mission"]["behaviors"])
        self.assertEqual(review["documentation_spec"]["authorized_paths"], ["README.md"])
        self.assertEqual(review["complete_diff"]["argv"][-1], f"{'1' * 40}..{'2' * 40}")
        self.assertTrue(Path(review["documentation_policy_path"]).is_file())

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
        self.assertIn("tests.test_calc", payload["test_identity_contract"]["unittest"])
        self.assertEqual(
            payload["proof_test_id_hints"][0]["unittest_module"], "tests.test_calc"
        )
        self.assertIn("exactly", payload["proof_execution_rule"])

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


if __name__ == "__main__":
    unittest.main()
