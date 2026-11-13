"""Single-operation interface used by the native Codex coordinating Agent.

There is deliberately no run-until-done loop. Every invocation names an
operation; status reports facts and alternatives, never a mandatory next step.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .assurance_v4 import core, operations
from .assurance_v4.driver_contract import AGENT_ACTION_CAPABILITIES
from .assurance_v4.models import ContractError, builder_runtime_mode, digest, load_json_source
from .assurance_v4.store import StoreError, read_ledger
from .native_driver.app_server import AppServerError, AppServerTransport, probe_app_server
from .native_driver.core_port import CorePort, CorePortError
from .native_driver.role_transport import RoleTransport, NativeDriverError
from .native_driver.host_runtime import (_root_runtime, _persist_transport_cleanup,
                                _reconcile_previous_transport, _verify_resume_executable_identity)
from .session_identity import SessionIdentityError, current_session_id


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="codex-builder-loop native-agent")
    result.add_argument("--codex-bin", default="codex")
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("start", "status", "inspect", "invoke"):
        item = commands.add_parser(name)
        item.add_argument("--repo", default=".")
        item.add_argument("--run", required=True)
        if name == "start":
            item.add_argument("--contract", required=True)
        if name in {"inspect", "invoke"}:
            item.add_argument("--action", required=True, choices=sorted(operations.ACTIONS))
        if name == "invoke":
            item.add_argument("--expected-operation-id", required=True)
            item.add_argument("--message", help="finalize commit message")
            item.add_argument("--renew-reason", help="explicitly authorized renewal of an exhausted role dispatch")
    return result


def _owner(ledger: dict[str, Any]) -> str:
    if not operations.is_native(ledger):
        raise core.AssuranceError("use this task's frozen runtime; automatic migration is not supported",
                                  code="OPERATION_RUNTIME_MISMATCH", status="FAIL")
    return current_session_id(ledger["owner_session_id"])


def _root_turn(port: CorePort, repo: Path, run: str, owner: str,
               action: dict[str, Any]) -> dict[str, Any]:
    context = core.driver_context(repo, run)
    if (context.get("dispatch_intent") or {}).get("state") == "completed":
        return core.apply_root_builder_result(repo, run, action_id=action["action_id"], owner_session_id=owner)
    adapter = RoleTransport(repo=repo, run_id=run, core=port, transport=None,
                            builder_mode="root_session", root_session_id=owner)
    if not context["facets"]["execution"]["agents"].get("builder"):
        port.call("prepare-builder", "--repo", str(repo), "--run", run,
                  "--agent-id", f"codex-root-session:{owner}", "--owner-mode", "root_session",
                  "--owner-session-id", owner, "--action-id", action["action_id"],
                  "--driver-runtime-kind", "native")
        action = operations.operation(repo, run, action["action"])
        context = core.driver_context(repo, run)
    if not context.get("dispatch_intent"):
        prompt = adapter._prompt(action, "builder", context)
        extra = []
        if action.get("work_unit_id"):
            extra = ["--work-unit-id", action["work_unit_id"], "--context-projection-digest",
                     action["context_projection_digest"]]
        port.call("begin-dispatch", "--repo", str(repo), "--run", run,
                  "--action", action["action"], "--action-id", action["action_id"],
                  "--role", "builder", "--owner-session-id", owner,
                  "--prompt-digest", digest(prompt), "--output-schema-digest", adapter.output_schema_digest,
                  "--driver-runtime-kind", "native", *extra)
    return {"status": "BUILDER_HANDOFF", "run_id": run, "action": action["action"],
            "action_id": action["action_id"], "candidate_worktree": context["candidate_worktree"],
            "owner_session_id": owner, "input_command": "assurance --experimental-v4 role-context",
            "submit_command": "assurance --experimental-v4 apply-root-builder-result"}


def _role_turn(port: CorePort, repo: Path, run: str, owner: str,
               action: dict[str, Any], codex_bin: str, renew_reason: str | None = None) -> dict[str, Any]:
    context = core.driver_context(repo, run)
    capability = AGENT_ACTION_CAPABILITIES.get(action["action"])
    if renew_reason is not None:
        pending = context.get("dispatch_intent") or {}
        if not renew_reason.strip() or pending.get("state") != "exhausted" or pending.get("role") == "builder":
            raise core.AssuranceError("renewal requires an exhausted role dispatch and explicit reason",
                                      code="NATIVE_DISPATCH_RENEWAL_NOT_AVAILABLE", status="FAIL")
    if (context.get("dispatch_intent") or {}).get("state") == "completed":
        return core.apply_dispatch_result(repo, run, action_id=action["action_id"], driver_runtime_kind="native")
    host = probe_app_server(codex_bin, strict_protocol=True, isolate_memories=True)
    _verify_resume_executable_identity(context, host)
    _reconcile_previous_transport(core=port, repo=repo, run_id=run, context=context)
    transport = AppServerTransport(codex_bin=codex_bin, strict_protocol=True,
                                   isolate_memories=True, executable_identity=host.executable_identity)
    try:
        with transport:
            port.call("bind-native-transport", "--repo", str(repo), "--run", run,
                      "--transport", "-", "--driver-runtime-kind", "native",
                      input_value=transport.runtime_snapshot())
            adapter = RoleTransport(repo=repo, run_id=run, core=port, transport=transport,
                                    builder_mode=builder_runtime_mode(context["facets"]), root_session_id=owner,
                                    thread_compaction_available=host.thread_compaction,
                                    dispatch_renewal_reason=renew_reason)
            adapter.current_action = action
            if action["action"] in operations.TRANSPORT_ACTIONS:
                getattr(adapter, "_" + action["action"])(action)
                return operations.status(repo, run)
            agent = context["facets"]["execution"]["agents"].get(capability.role)
            if not agent or (capability.preparation == "tester_source" and
                             not context["facets"]["execution"].get("tester_source")):
                adapter._prepare_role(action, capability, context)
                action = operations.operation(repo, run, action["action"])
            adapter._run_agent_action(action, capability)
    finally:
        _persist_transport_cleanup(core=port, repo=repo, run_id=run, transport=transport)
    return operations.status(repo, run)


def invoke(args: argparse.Namespace, owner: str) -> dict[str, Any]:
    repo = Path(args.repo).resolve()
    action = operations.operation(repo, args.run, args.action)
    if action["action_id"] != args.expected_operation_id:
        raise core.AssuranceError("selected operation inputs changed", code="OPERATION_INPUT_STALE", status="FAIL")
    port = CorePort()
    capability = AGENT_ACTION_CAPABILITIES.get(args.action)
    if getattr(args, "renew_reason", None) is not None and (capability is None or
            (capability.role == "builder" and builder_runtime_mode(read_ledger(repo, args.run)["facets"]) == "root_session")):
        raise core.AssuranceError("renewal applies only to an exhausted child role dispatch",
                                  code="NATIVE_DISPATCH_RENEWAL_NOT_AVAILABLE", status="FAIL")
    if args.action in operations.TRANSPORT_ACTIONS:
        return _role_turn(port, repo, args.run, owner, action, args.codex_bin, getattr(args, "renew_reason", None))
    if capability:
        if capability.role == "builder" and builder_runtime_mode(read_ledger(repo, args.run)["facets"]) == "root_session":
            return _root_turn(port, repo, args.run, owner, action)
        return _role_turn(port, repo, args.run, owner, action, args.codex_bin, getattr(args, "renew_reason", None))
    command = args.action.replace("_", "-")
    extra = []
    if args.action in {"checkpoint_builder", "recompose_candidate", "complete_driver_failure"}:
        extra = ["--owner-session-id", owner]
    if args.action == "finalize":
        if not args.message:
            raise core.AssuranceError("finalize requires an explicit commit message", code="FINALIZE_MESSAGE_REQUIRED")
        extra = ["--message", args.message]
    binding = [] if args.action == "complete_driver_failure" else ["--action-id", action["action_id"]]
    return port.call(command, "--repo", str(repo), "--run", args.run,
                     *binding, "--driver-runtime-kind", "native", *extra)


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        repo = Path(args.repo).resolve()
        if args.command == "start":
            owner = current_session_id()
            contract = load_json_source(args.contract, stdin_text=sys.stdin.read() if args.contract == "-" else None)
            if builder_runtime_mode(contract) == "root_session":
                runtime = _root_runtime(owner)
            else:
                host = probe_app_server(args.codex_bin, strict_protocol=True, isolate_memories=True)
                runtime = {"kind": "native", "protocol_version": 1, "transport": "codex_app_server",
                           "runtime_version": host.runtime_version,
                           "protocol_schema_digest": host.protocol_schema_digest,
                           "protocol_canary_digest": host.protocol_canary_digest}
            runtime["orchestration"] = operations.ORCHESTRATION
            result = core.start(repo, args.run, owner, contract, driver_runtime=runtime)
        else:
            ledger = read_ledger(repo, args.run)
            owner = _owner(ledger)
            if args.command == "status":
                result = operations.status(repo, args.run)
            elif args.command == "inspect":
                result = operations.operation(repo, args.run, args.action)
                result.pop("context_projection", None)
            else:
                result = invoke(args, owner)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (core.AssuranceError, CorePortError, NativeDriverError, AppServerError,
            ContractError, StoreError, SessionIdentityError) as exc:
        print(json.dumps({"status": getattr(exc, "status", "FAIL"), "code": getattr(exc, "code", "NATIVE_OPERATION_FAILED"),
                          "message": str(exc), "details": getattr(exc, "details", None)}, ensure_ascii=False))
        return 1
