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

    prepare_tester = commands.add_parser("prepare-tester")
    prepare_tester.add_argument("--repo", default=".")
    prepare_tester.add_argument("--run", required=True)
    prepare_tester.add_argument("--agent-id", required=True)
    prepare_tester.add_argument("--thread-id", required=True)
    prepare_tester.add_argument("--replace", action="store_true")

    integrate_tester = commands.add_parser("integrate-tester")
    integrate_tester.add_argument("--repo", default=".")
    integrate_tester.add_argument("--run", required=True)

    verify = commands.add_parser("verify-machine")
    verify.add_argument("--repo", default=".")
    verify.add_argument("--run", required=True)

    finalize = commands.add_parser("finalize")
    finalize.add_argument("--repo", default=".")
    finalize.add_argument("--run", required=True)
    finalize.add_argument("--message", required=True)

    rematerialize = commands.add_parser("rematerialize-target")
    rematerialize.add_argument("--repo", default=".")
    rematerialize.add_argument("--run", required=True)

    recover_finalize = commands.add_parser("recover-finalize")
    recover_finalize.add_argument("--repo", default=".")
    recover_finalize.add_argument("--run", required=True)

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
            payload = core.record_evidence(args.repo, args.run, args.kind, _json(args.report))
        elif args.command == "prepare-tester":
            payload = core.prepare_tester(
                args.repo,
                args.run,
                args.agent_id,
                args.thread_id,
                replace=args.replace,
            )
        elif args.command == "integrate-tester":
            payload = core.integrate_tester(args.repo, args.run)
        elif args.command == "verify-machine":
            payload = core.verify_machine(args.repo, args.run)
        elif args.command == "finalize":
            payload = core.finalize(args.repo, args.run, args.message)
        elif args.command == "rematerialize-target":
            payload = core.rematerialize_target(args.repo, args.run)
        elif args.command == "recover-finalize":
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
