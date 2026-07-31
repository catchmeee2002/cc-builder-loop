from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import core
from . import driver
from .models import ContractError, load_json_source
from .store import StoreError


EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_FATAL = 2


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise core.AssuranceError(message, code="CLI_USAGE_ERROR")


def emit(value: dict[str, Any], code: int) -> int:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return code


def parser() -> argparse.ArgumentParser:
    value = Parser(prog="codex-builder-loop assurance")
    value.add_argument(
        "--experimental-v4",
        action="store_true",
        help="required while Assurance Core v4 is not a public business entry",
    )
    commands = value.add_subparsers(dest="command", required=True, parser_class=Parser)

    def dispatch_guard(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--action-id",
            help="optional driver-next action identity; stale identities are rejected",
        )

    validate = commands.add_parser("validate")
    validate.add_argument("--contract", required=True)

    start = commands.add_parser("start")
    start.add_argument("--repo", default=".")
    start.add_argument("--run", required=True)
    start.add_argument("--session-id", required=True)
    start.add_argument("--contract", required=True)

    status = commands.add_parser("status")
    status.add_argument("--repo", default=".")
    status.add_argument("--run", required=True)

    checkpoint = commands.add_parser("checkpoint-builder")
    checkpoint.add_argument("--repo", default=".")
    checkpoint.add_argument("--run", required=True)
    dispatch_guard(checkpoint)

    publication = commands.add_parser("publish-prerequisites")
    publication.add_argument("--repo", default=".")
    publication.add_argument("--run", required=True)
    dispatch_guard(publication)

    update = commands.add_parser("update-facet")
    update.add_argument("--repo", default=".")
    update.add_argument("--run", required=True)
    update.add_argument("--facet", choices=["mission", "authority", "assurance", "execution"], required=True)
    update.add_argument("--value", required=True)
    update.add_argument("--semantic-revision", action="store_true")
    update.add_argument("--authorize-expansion", action="store_true")
    update.add_argument("--authorize-downgrade", action="store_true")

    evidence = commands.add_parser("record-evidence")
    evidence.add_argument("--repo", default=".")
    evidence.add_argument("--run", required=True)
    evidence.add_argument("--kind", choices=list(core.EVIDENCE_KINDS), required=True)
    evidence.add_argument("--report", required=True)
    dispatch_guard(evidence)

    prepare_tester = commands.add_parser("prepare-tester")
    prepare_tester.add_argument("--repo", default=".")
    prepare_tester.add_argument("--run", required=True)
    prepare_tester.add_argument("--agent-id", required=True)
    prepare_tester.add_argument("--thread-id", required=True)
    prepare_tester.add_argument("--replace", action="store_true")
    dispatch_guard(prepare_tester)

    prepare_reviewer = commands.add_parser("prepare-reviewer")
    prepare_reviewer.add_argument("--repo", default=".")
    prepare_reviewer.add_argument("--run", required=True)
    prepare_reviewer.add_argument("--agent-id", required=True)
    prepare_reviewer.add_argument("--thread-id", required=True)
    prepare_reviewer.add_argument("--replace", action="store_true")
    dispatch_guard(prepare_reviewer)

    problems = commands.add_parser("record-problems")
    problems.add_argument("--repo", default=".")
    problems.add_argument("--run", required=True)
    problems.add_argument("--report", required=True)
    problems.add_argument("--role", choices=["builder", "tester", "reviewer"], required=True)
    problems.add_argument("--agent-id", required=True)
    problems.add_argument("--thread-id", required=True)
    dispatch_guard(problems)

    prove = commands.add_parser("prove-tests")
    prove.add_argument("--repo", default=".")
    prove.add_argument("--run", required=True)
    prove.add_argument("--spec", required=True)
    prove.add_argument("--agent-id", required=True)
    prove.add_argument("--thread-id", required=True)
    dispatch_guard(prove)

    integrate_tester = commands.add_parser("integrate-tester")
    integrate_tester.add_argument("--repo", default=".")
    integrate_tester.add_argument("--run", required=True)
    dispatch_guard(integrate_tester)

    verify = commands.add_parser("verify-machine")
    verify.add_argument("--repo", default=".")
    verify.add_argument("--run", required=True)
    dispatch_guard(verify)

    finalize = commands.add_parser("finalize")
    finalize.add_argument("--repo", default=".")
    finalize.add_argument("--run", required=True)
    finalize.add_argument("--message", required=True)
    dispatch_guard(finalize)

    rematerialize = commands.add_parser("rematerialize-target")
    rematerialize.add_argument("--repo", default=".")
    rematerialize.add_argument("--run", required=True)
    dispatch_guard(rematerialize)

    recover_finalize = commands.add_parser("recover-finalize")
    recover_finalize.add_argument("--repo", default=".")
    recover_finalize.add_argument("--run", required=True)
    dispatch_guard(recover_finalize)

    next_action = commands.add_parser("driver-next")
    next_action.add_argument("--repo", default=".")
    next_action.add_argument("--run", required=True)

    abandon = commands.add_parser("abandon")
    abandon.add_argument("--repo", default=".")
    abandon.add_argument("--run", required=True)
    abandon.add_argument("--reason", required=True)

    cleanup = commands.add_parser("cleanup")
    cleanup.add_argument("--repo", default=".")
    cleanup.add_argument("--run", required=True)
    return value


def _json(path: str) -> Any:
    text = sys.stdin.read() if path == "-" else None
    return load_json_source(path, stdin_text=text)


def _guard_dispatch(args: argparse.Namespace, accepted: set[str]) -> None:
    action_id = getattr(args, "action_id", None)
    current = driver.next_action(args.repo, args.run)
    if action_id is None and not current.get("driver_enforced"):
        return
    if current.get("action") not in accepted or (
        action_id is not None and current.get("action_id") != action_id
    ):
        raise core.AssuranceError(
            "driver action is stale or does not authorize this mutation",
            code="DRIVER_ACTION_STALE",
            status="FAIL",
            details={
                "expected_action_id": current.get("action_id"),
                "expected_action": current.get("action"),
            },
        )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        if not args.experimental_v4:
            raise core.AssuranceError(
                "Assurance Core v4 requires the explicit experimental flag during maintenance",
                code="ASSURANCE_V4_EXPERIMENTAL_REQUIRED",
            )
        if args.command == "validate":
            payload = core.validate(_json(args.contract))
        elif args.command == "start":
            payload = core.start(args.repo, args.run, args.session_id, _json(args.contract))
        elif args.command == "status":
            payload = core.status(args.repo, args.run)
        elif args.command == "checkpoint-builder":
            _guard_dispatch(args, {"builder_implement", "builder_fix", "checkpoint_builder"})
            payload = core.checkpoint_builder(args.repo, args.run)
        elif args.command == "publish-prerequisites":
            _guard_dispatch(args, {"publish_prerequisites"})
            payload = core.publish_prerequisites(args.repo, args.run)
        elif args.command == "update-facet":
            payload = core.update_facet(
                args.repo,
                args.run,
                args.facet,
                _json(args.value),
                semantic_revision=args.semantic_revision,
                authorize_expansion=args.authorize_expansion,
                authorize_downgrade=args.authorize_downgrade,
            )
        elif args.command == "record-evidence":
            accepted = {
                "tester": {"tester_author", "tester_fix"},
                "proof": {"tester_proof"},
                "blackbox": {"tester_blackbox"},
                "reviewer": {"reviewer_final"},
                "doc_review": {"reviewer_final"},
            }.get(args.kind, set())
            _guard_dispatch(args, accepted)
            payload = core.record_evidence(args.repo, args.run, args.kind, _json(args.report))
        elif args.command == "prepare-tester":
            _guard_dispatch(args, {"tester_author", "tester_fix"})
            payload = core.prepare_tester(
                args.repo,
                args.run,
                args.agent_id,
                args.thread_id,
                replace=args.replace,
            )
        elif args.command == "prepare-reviewer":
            _guard_dispatch(args, {"reviewer_final"})
            payload = core.prepare_reviewer(
                args.repo,
                args.run,
                args.agent_id,
                args.thread_id,
                replace=args.replace,
            )
        elif args.command == "record-problems":
            role_actions = {
                "builder": {"builder_implement", "builder_fix", "checkpoint_builder"},
                "tester": {"tester_author", "tester_proof", "tester_blackbox", "tester_fix"},
                "reviewer": {"reviewer_final"},
            }
            _guard_dispatch(args, role_actions[args.role])
            payload = core.record_problems(
                args.repo,
                args.run,
                _json(args.report),
                role=args.role,
                agent_id=args.agent_id,
                thread_id=args.thread_id,
            )
        elif args.command == "prove-tests":
            _guard_dispatch(args, {"tester_proof"})
            payload = core.prove_tests(
                args.repo,
                args.run,
                _json(args.spec),
                agent_id=args.agent_id,
                thread_id=args.thread_id,
            )
        elif args.command == "integrate-tester":
            _guard_dispatch(args, {"tester_author", "tester_fix"})
            payload = core.integrate_tester(args.repo, args.run)
        elif args.command == "verify-machine":
            _guard_dispatch(args, {"verify_machine"})
            payload = core.verify_machine(args.repo, args.run)
        elif args.command == "finalize":
            _guard_dispatch(args, {"finalize"})
            payload = core.finalize(args.repo, args.run, args.message)
        elif args.command == "rematerialize-target":
            _guard_dispatch(args, {"rematerialize_target"})
            payload = core.rematerialize_target(args.repo, args.run)
        elif args.command == "recover-finalize":
            _guard_dispatch(args, {"recover_finalize"})
            payload = core.recover_finalize(args.repo, args.run)
        elif args.command == "driver-next":
            payload = driver.next_action(args.repo, args.run)
        elif args.command == "abandon":
            payload = core.abandon(args.repo, args.run, args.reason)
        elif args.command == "cleanup":
            payload = core.cleanup(args.repo, args.run)
        else:
            raise core.AssuranceError("unknown assurance command", code="CLI_USAGE_ERROR")
        return emit(payload, EXIT_PASS)
    except (core.AssuranceError, ContractError, StoreError) as exc:
        status = getattr(exc, "status", "FATAL")
        payload = {"status": status, "code": exc.code, "message": str(exc)}
        payload.update(getattr(exc, "details", {}))
        return emit(payload, EXIT_FAIL if status in {"NEEDS_USER", "FAIL"} else EXIT_FATAL)
    except KeyboardInterrupt:
        return emit({"status": "FATAL", "code": "INTERRUPTED", "message": "interrupted"}, EXIT_FATAL)


if __name__ == "__main__":
    raise SystemExit(main())
