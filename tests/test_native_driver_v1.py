from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from harness import CLI, ROOT, cleanup_repo, init_repo, run_process

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
        )
        self.assertIsNone(json.loads((run_path / "ledger.json").read_text())["dispatch_intent"])


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


if __name__ == "__main__":
    unittest.main()
