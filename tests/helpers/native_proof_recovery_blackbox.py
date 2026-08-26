from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests"
HELPERS = TESTS / "helpers"
for value in (str(TESTS), str(HELPERS)):
    if value not in sys.path:
        sys.path.insert(0, value)

from harness import (  # noqa: E402
    CLI,
    add_v4_progress_contract,
    cleanup_repo,
    commit_all,
    fixture_runtime_env,
    init_repo,
    run_process,
)
from proof_readiness_blackbox import (  # noqa: E402
    assurance,
    parse_json_output,
    require,
    require_ready,
)

sys.path.insert(0, str(ROOT / "runtime"))
from codex_builder_loop.native_driver.app_server import probe_app_server  # noqa: E402


def contract() -> dict[str, Any]:
    value = {
        "schema_version": 4,
        "mission": {
            "revision": 1,
            "objective": "Recover one persisted proof failure transaction.",
            "behaviors": [
                {
                    "id": "proof-recovery",
                    "description": "The frozen candidate must return 99 for add(1, 2).",
                }
            ],
            "interfaces": [
                {
                    "id": "native-driver-resume",
                    "description": "native-driver resume replays a completed proof dispatch.",
                }
            ],
            "acceptance_cases": [
                {
                    "id": "proof-recovery",
                    "description": "A persisted proof failure is consumed exactly once.",
                    "observation": {
                        "surface_id": "native-driver-resume",
                        "surface_description": "The real Native Driver resume CLI.",
                        "execution_ids": ["native-recovery-blackbox"],
                        "required_dimensions": ["mechanical", "verify"],
                    },
                }
            ],
            "trust_boundaries": [
                {
                    "id": "public-transactions-only",
                    "description": "Setup and assertions use public runtime transactions.",
                }
            ],
        },
        "authority": {
            "target_branch": "main",
            "builder_write": ["src/calc.py"],
            "tester_write": ["tests/**"],
            "dirty_intake": [],
            "external_targets": [],
            "protected_support_paths": [],
            "public_prerequisites": [],
        },
        "assurance": {
            "required": ["tester", "proof", "machine", "blackbox", "reviewer"],
            "machine_commands": [
                {
                    "id": "fixture-machine",
                    "argv": ["/usr/bin/python3", "-m", "unittest"],
                    "expected_returncodes": [0],
                    "timeout_seconds": 30,
                    "run_before_full_suite": False,
                }
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
                {
                    "id": "native-recovery-blackbox",
                    "argv": ["/usr/bin/python3", "-m", "unittest"],
                    "expected_returncodes": [0],
                    "timeout_seconds": 30,
                    "run_before_full_suite": False,
                }
            ],
            "agents": {},
        },
    }
    return add_v4_progress_contract(value)


def wire_result(result: str, proof_spec: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "result": result,
        "evidence_report": None,
        "proof_spec": proof_spec,
        "problem_report": None,
    }


def native_args(codex_bin: Path, repo: Path, run_id: str) -> list[str]:
    return [
        sys.executable,
        str(CLI),
        "native-driver",
        "--codex-bin",
        str(codex_bin),
        "resume",
        "--repo",
        str(repo),
        "--run",
        run_id,
    ]


def begin_completed_dispatch(
    artifacts: Path,
    *,
    repo: Path,
    run_id: str,
    action: Mapping[str, Any],
    role: str,
    thread_id: str,
    result: dict[str, Any],
) -> None:
    require_ready(
        "begin-dispatch",
        "--repo",
        repo,
        "--run",
        run_id,
        "--action-id",
        str(action["action_id"]),
        "--action",
        str(action["action"]),
        "--role",
        role,
        "--thread-id",
        thread_id,
        "--prompt-digest",
        "a" * 64,
        "--output-schema-digest",
        "b" * 64,
    )
    result_path = artifacts / f"{action['action_id']}-result.json"
    result_path.write_text(json.dumps(result), encoding="utf-8")
    require_ready(
        "complete-dispatch",
        "--repo",
        repo,
        "--run",
        run_id,
        "--action-id",
        str(action["action_id"]),
        "--result",
        result_path,
    )


def consume_dispatch(repo: Path, run_id: str, action_id: str) -> None:
    require_ready(
        "consume-dispatch",
        "--repo",
        repo,
        "--run",
        run_id,
        "--action-id",
        action_id,
        "--consumer-source",
        "native_driver",
    )


def scan_if_required(repo: Path, run_id: str) -> dict[str, Any]:
    action = require_ready("driver-next", "--repo", repo, "--run", run_id)
    if action.get("action") != "scan_doc_references":
        return action
    require_ready(
        "scan-doc-references",
        "--repo",
        repo,
        "--run",
        run_id,
        "--action-id",
        str(action["action_id"]),
        "--driver-runtime-kind",
        "native",
    )
    return require_ready("driver-next", "--repo", repo, "--run", run_id)


def make_launcher(path: Path, marker: Path) -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import sys\n\n"
        f"open({str(marker)!r}, 'a', encoding='utf-8').write('invoked\\n')\n"
        "if sys.argv[1:5] != ['run', '--frozen', '--offline', '--no-env-file']:\n"
        "    raise SystemExit(92)\n"
        "command = sys.argv[5:]\n"
        "if command[0] == 'python':\n"
        "    command[0] = sys.executable\n"
        "os.execvpe(command[0], command, os.environ)\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path.resolve()


def proof_spec(launcher: Path, *, timeout_seconds: int) -> dict[str, Any]:
    test_id = "tests.test_native_recovery.NativeRecoveryTest.test_expected_value"
    return {
        "schema_version": 1,
        "groups": [
            {
                "behavior_ids": ["proof-recovery"],
                "method": "baseline-red",
                "argv": [
                    str(launcher),
                    "run",
                    "--frozen",
                    "--offline",
                    "--no-env-file",
                    "python",
                    "-m",
                    "unittest",
                    test_id,
                ],
                "test_ids": [test_id],
                "timeout_seconds": timeout_seconds,
                "claimed_failure_kind": "assertion-failure",
            }
        ],
    }


def status(repo: Path, run_id: str) -> dict[str, Any]:
    return require_ready("status", "--repo", repo, "--run", run_id)


def proof_attempts(value: Mapping[str, Any]) -> int:
    return int(value["telemetry"]["evidence_attempts"]["proof"])


def record_proof_failure(
    artifacts: Path,
    *,
    repo: Path,
    run_id: str,
    action: Mapping[str, Any],
    tester: Mapping[str, str],
    spec: dict[str, Any],
    result_name: str,
) -> dict[str, Any]:
    begin_completed_dispatch(
        artifacts,
        repo=repo,
        run_id=run_id,
        action=action,
        role="tester",
        thread_id=tester["thread_id"],
        result=wire_result(result_name, spec),
    )
    spec_path = artifacts / f"{action['action_id']}-proof.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    returncode, failure = assurance(
        "prove-tests",
        "--repo",
        repo,
        "--run",
        run_id,
        "--spec",
        spec_path,
        "--agent-id",
        tester["agent_id"],
        "--thread-id",
        tester["thread_id"],
        "--action-id",
        str(action["action_id"]),
        "--driver-runtime-kind",
        "native",
    )
    require(returncode == 1, "proof failure transaction unexpectedly passed", failure)
    require(
        failure.get("code") == "TEST_PROOF_CANDIDATE_FAILED",
        "proof failure transaction returned wrong code",
        failure,
    )
    observed = status(repo, run_id)
    require(
        observed["proof_failure_state"] == "current",
        "proof failure was not persisted",
        observed,
    )
    return observed


def spawn_native_resume(
    codex_bin: Path,
    repo: Path,
    run_id: str,
    artifacts: Path,
) -> tuple[int, Path, Path]:
    stdout_path = artifacts / "native-resume.stdout"
    stderr_path = artifacts / "native-resume.stderr"
    pid = os.fork()
    if pid == 0:
        os.setsid()
        os.chdir(ROOT)
        stdout_fd = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        stderr_fd = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.dup2(stdout_fd, 1)
        os.dup2(stderr_fd, 2)
        os.close(stdout_fd)
        os.close(stderr_fd)
        environment = dict(os.environ)
        environment.update(fixture_runtime_env(repo))
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        argv = native_args(codex_bin, repo, run_id)
        os.execve(sys.executable, argv, environment)
    return pid, stdout_path, stderr_path


def kill_after_third_failure(
    *,
    pid: int,
    repo: Path,
    run_id: str,
    run_path: Path,
    stdout_path: Path,
    stderr_path: Path,
    expected_signature: str,
) -> dict[str, Any]:
    ledger_path = run_path / "ledger.json"
    baseline_mtime = ledger_path.stat().st_mtime_ns
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        waited, wait_status = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            stdout = stdout_path.read_text(encoding="utf-8") if stdout_path.exists() else ""
            stderr = stderr_path.read_text(encoding="utf-8") if stderr_path.exists() else ""
            raise AssertionError(
                "Native Driver exited before the persisted failure crash point: "
                f"status={wait_status} stdout={stdout!r} stderr={stderr!r}"
            )
        current_mtime = ledger_path.stat().st_mtime_ns
        if current_mtime == baseline_mtime:
            time.sleep(0.001)
            continue
        baseline_mtime = current_mtime
        os.kill(pid, signal.SIGSTOP)
        time.sleep(0.02)
        observed = status(repo, run_id)
        failure = observed.get("proof_failure")
        dispatch = observed.get("dispatch_intent")
        if (
            proof_attempts(observed) == 3
            and observed.get("proof_failure_state") == "current"
            and isinstance(failure, Mapping)
            and failure.get("failure_signature") == expected_signature
            and isinstance(dispatch, Mapping)
            and dispatch.get("state") == "completed"
        ):
            os.killpg(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            return observed
        os.kill(pid, signal.SIGCONT)
    os.killpg(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    raise AssertionError("timed out before observing the persisted third proof failure")


def run_native_resume(codex_bin: Path, repo: Path, run_id: str) -> tuple[int, dict[str, Any]]:
    completed = run_process(
        native_args(codex_bin, repo, run_id),
        cwd=ROOT,
        env={
            **fixture_runtime_env(repo),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    return completed.returncode, parse_json_output(completed)


def exercise(codex_bin: Path, artifacts: Path) -> dict[str, Any]:
    capability = probe_app_server(str(codex_bin))
    repo = init_repo(
        {
            "src/calc.py": "def add(a, b):\n    return a + b - 1\n",
            "pyproject.toml": (
                "[project]\n"
                "name = 'native-proof-recovery'\n"
                "version = '0.0.0'\n"
                "requires-python = '>=3.11'\n"
            ),
            "uv.lock": "version = 1\nrevision = 3\nrequires-python = '>=3.11'\n",
        }
    )
    run_id = "native-proof-recovery-blackbox"
    marker = artifacts / "proof-invocations"
    launcher = make_launcher(artifacts / "uv", marker)
    try:
        contract_path = artifacts / "contract.json"
        contract_path.write_text(json.dumps(contract()), encoding="utf-8")
        started = require_ready(
            "start",
            "--repo",
            repo,
            "--run",
            run_id,
            "--session-id",
            "native-proof-recovery-session",
            "--contract",
            contract_path,
            "--driver-kind",
            "native",
            "--driver-transport",
            "codex_app_server",
            "--driver-runtime-version",
            capability.runtime_version,
            "--driver-protocol-schema-digest",
            capability.protocol_schema_digest,
        )
        candidate = Path(started["candidate_worktree"])
        run_path = candidate.parent

        builder_action = require_ready("driver-next", "--repo", repo, "--run", run_id)
        require(builder_action.get("action") == "builder_implement", "builder action missing")
        require_ready(
            "prepare-builder",
            "--repo",
            repo,
            "--run",
            run_id,
            "--agent-id",
            "native-recovery-builder",
            "--thread-id",
            "native-recovery-builder-thread",
            "--action-id",
            builder_action["action_id"],
            "--driver-runtime-kind",
            "native",
        )
        builder_action = require_ready(
            "driver-next", "--repo", repo, "--run", run_id
        )
        require(
            builder_action.get("action") == "builder_implement",
            "builder action did not resume after preparation",
            builder_action,
        )
        begin_completed_dispatch(
            artifacts,
            repo=repo,
            run_id=run_id,
            action=builder_action,
            role="builder",
            thread_id="native-recovery-builder-thread",
            result=wire_result("implemented"),
        )
        (candidate / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8"
        )
        commit_all(candidate, "implement native proof recovery candidate")
        consume_dispatch(repo, run_id, str(builder_action["action_id"]))

        checkpoint = require_ready("driver-next", "--repo", repo, "--run", run_id)
        require(checkpoint.get("action") == "checkpoint_builder", "checkpoint action missing")
        require_ready(
            "checkpoint-builder",
            "--repo",
            repo,
            "--run",
            run_id,
            "--action-id",
            checkpoint["action_id"],
            "--driver-runtime-kind",
            "native",
        )
        next_action = scan_if_required(repo, run_id)
        require(next_action.get("action") == "tester_author", "tester author action missing", next_action)
        require_ready(
            "prepare-tester",
            "--repo",
            repo,
            "--run",
            run_id,
            "--agent-id",
            "native-recovery-tester",
            "--thread-id",
            "native-recovery-tester-thread",
            "--action-id",
            next_action["action_id"],
            "--driver-runtime-kind",
            "native",
        )
        author_action = require_ready("driver-next", "--repo", repo, "--run", run_id)
        require(author_action.get("action") == "tester_author", "tester author did not resume")
        context = require_ready("driver-context", "--repo", repo, "--run", run_id)
        tester_source = context["facets"]["execution"]["tester_source"]
        tester_worktree = Path(tester_source["worktree"])
        (tester_worktree / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (tester_worktree / "tests" / "test_native_recovery.py").write_text(
            "import unittest\n"
            "from src.calc import add\n\n"
            "class NativeRecoveryTest(unittest.TestCase):\n"
            "    def test_expected_value(self):\n"
            "        self.assertEqual(add(1, 2), 99)\n",
            encoding="utf-8",
        )
        commit_all(tester_worktree, "author native proof recovery tests")
        begin_completed_dispatch(
            artifacts,
            repo=repo,
            run_id=run_id,
            action=author_action,
            role="tester",
            thread_id="native-recovery-tester-thread",
            result=wire_result("tests_ready"),
        )
        require_ready(
            "integrate-tester",
            "--repo",
            repo,
            "--run",
            run_id,
            "--action-id",
            author_action["action_id"],
            "--driver-runtime-kind",
            "native",
        )
        context = require_ready("driver-context", "--repo", repo, "--run", run_id)
        execution = context["facets"]["execution"]
        source = execution["tester_source"]
        tester = execution["agents"]["tester"]
        tester_report = artifacts / "tester-report.json"
        tester_report.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "tester",
                    "status": "pass",
                    "candidate_head": execution["candidate_head"],
                    "producer": {"role": "tester", **tester},
                    "details": {
                        "result": "tests_ready",
                        "source_head": source["head"],
                        "files": source["files"],
                    },
                }
            ),
            encoding="utf-8",
        )
        require_ready(
            "record-evidence",
            "--repo",
            repo,
            "--run",
            run_id,
            "--kind",
            "tester",
            "--report",
            tester_report,
            "--action-id",
            author_action["action_id"],
            "--driver-runtime-kind",
            "native",
        )
        consume_dispatch(repo, run_id, str(author_action["action_id"]))
        proof_action = scan_if_required(repo, run_id)
        require(proof_action.get("action") == "tester_proof", "proof action missing", proof_action)

        first_status = record_proof_failure(
            artifacts,
            repo=repo,
            run_id=run_id,
            action=proof_action,
            tester=tester,
            spec=proof_spec(launcher, timeout_seconds=30),
            result_name="pass",
        )
        first_signature = first_status["proof_failure"]["failure_signature"]
        require(proof_attempts(first_status) == 1, "first proof event count drifted")
        consume_dispatch(repo, run_id, str(proof_action["action_id"]))

        second_action = require_ready("driver-next", "--repo", repo, "--run", run_id)
        require(
            second_action.get("action") == "tester_proof_diagnose",
            "first proof failure did not route to diagnosis",
            second_action,
        )
        second_status = record_proof_failure(
            artifacts,
            repo=repo,
            run_id=run_id,
            action=second_action,
            tester=tester,
            spec=proof_spec(launcher, timeout_seconds=31),
            result_name="tests_ready",
        )
        require(
            second_status["proof_failure"]["failure_signature"] == first_signature,
            "second public proof transaction changed stable signature",
            second_status["proof_failure"],
        )
        require(proof_attempts(second_status) == 2, "second proof event count drifted")
        consume_dispatch(repo, run_id, str(second_action["action_id"]))

        third_action = require_ready("driver-next", "--repo", repo, "--run", run_id)
        require(
            third_action.get("action") == "tester_proof_diagnose",
            "second proof failure did not route to diagnosis",
            third_action,
        )
        begin_completed_dispatch(
            artifacts,
            repo=repo,
            run_id=run_id,
            action=third_action,
            role="tester",
            thread_id=tester["thread_id"],
            result=wire_result(
                "tests_ready", proof_spec(launcher, timeout_seconds=32)
            ),
        )
        require(
            marker.read_text(encoding="utf-8").splitlines() == ["invoked", "invoked"],
            "prior proof commands did not execute exactly twice",
        )

        pid, stdout_path, stderr_path = spawn_native_resume(
            codex_bin, repo, run_id, artifacts
        )
        crashed = kill_after_third_failure(
            pid=pid,
            repo=repo,
            run_id=run_id,
            run_path=run_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            expected_signature=first_signature,
        )
        require(
            marker.read_text(encoding="utf-8").splitlines()
            == ["invoked", "invoked", "invoked"],
            "third proof command did not execute exactly once before SIGKILL",
        )
        require(
            crashed["dispatch_intent"]["action_id"] == third_action["action_id"],
            "crash did not preserve the completed third dispatch",
            crashed["dispatch_intent"],
        )

        resume_rc, resumed = run_native_resume(codex_bin, repo, run_id)
        require(resume_rc == 1, "Native Driver recovery returned unexpected code", resumed)
        require(resumed.get("status") == "NEEDS_USER", "recovery did not stop for user", resumed)
        decision = resumed.get("decision")
        require(isinstance(decision, Mapping), "recovery omitted DriverPort decision", resumed)
        require(
            decision.get("action") == "architecture_review",
            "third identical proof failure did not route to architecture review",
            decision,
        )
        failures = decision.get("failures")
        require(isinstance(failures, list) and failures, "architecture review omitted failures")
        proof_failure = next(
            (item for item in failures if item.get("kind") == "proof"), None
        )
        require(
            isinstance(proof_failure, Mapping) and proof_failure.get("count") == 3,
            "architecture review did not report three proof failures",
            failures,
        )
        final_status = status(repo, run_id)
        require(proof_attempts(final_status) == 3, "resume duplicated proof failure event")
        require(
            marker.read_text(encoding="utf-8").splitlines()
            == ["invoked", "invoked", "invoked"],
            "resume reran the proof command",
        )
        require(final_status.get("dispatch_intent") is None, "completed dispatch was not consumed")
        return {
            "runtime_version": capability.runtime_version,
            "failure_signature": first_signature,
            "proof_attempts": proof_attempts(final_status),
            "proof_invocations": len(marker.read_text(encoding="utf-8").splitlines()),
            "decision": decision["action"],
            "decision_count": proof_failure["count"],
            "sigkill_observed": True,
        }
    finally:
        cleanup_repo(repo)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--codex-bin", type=Path, required=True)
    return value


def main() -> int:
    args = parser().parse_args()
    codex_bin = args.codex_bin.resolve()
    require(codex_bin.is_file(), "installed Codex executable is missing", codex_bin)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    with tempfile.TemporaryDirectory(prefix="native-proof-recovery-blackbox-") as raw:
        observation = exercise(codex_bin, Path(raw))
    print(json.dumps({"status": "pass", "observation": observation}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
