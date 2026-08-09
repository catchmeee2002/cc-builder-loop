from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import core
from . import driver
from .driver_contract import actions_for_preparation
from .models import ContractError, digest, load_json_source
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
        command.add_argument(
            "--driver-runtime-kind",
            choices=["native", "full_driver_skill"],
            help="required to match the frozen owner for runs that bind a driver runtime",
        )

    validate = commands.add_parser("validate")
    validate.add_argument("--repo", default=".")
    validate.add_argument("--contract", required=True)

    validate_decision = commands.add_parser("validate-decision")
    validate_decision.add_argument("--repo", default=".")
    validate_decision.add_argument("--run", required=True)
    validate_decision.add_argument("--session-id", required=True)
    validate_decision.add_argument("--problem-key", required=True)
    validate_decision.add_argument("--action-id", required=True)
    validate_decision.add_argument(
        "--facet", choices=["mission", "authority", "assurance"], required=True
    )
    validate_decision.add_argument("--facet-digest", required=True)
    validate_decision.add_argument("--contract", required=True)

    start = commands.add_parser("start")
    start.add_argument("--repo", default=".")
    start.add_argument("--run", required=True)
    start.add_argument("--session-id", required=True)
    start.add_argument("--contract", required=True)
    start.add_argument("--driver-kind", choices=["native", "full_driver_skill"])
    start.add_argument("--driver-transport", choices=["codex_app_server", "native_tools"])
    start.add_argument("--driver-runtime-version")
    start.add_argument("--driver-protocol-schema-digest")

    status = commands.add_parser("status")
    status.add_argument("--repo", default=".")
    status.add_argument("--run", required=True)

    record_driver_failure = commands.add_parser("record-driver-failure")
    record_driver_failure.add_argument("--repo", default=".")
    record_driver_failure.add_argument("--run", required=True)
    record_driver_failure.add_argument("--failure", required=True)
    record_driver_failure.add_argument(
        "--driver-runtime-kind",
        choices=["native", "full_driver_skill"],
        required=True,
    )

    complete_driver_failure = commands.add_parser("complete-driver-failure")
    complete_driver_failure.add_argument("--repo", default=".")
    complete_driver_failure.add_argument("--run", required=True)
    complete_driver_failure.add_argument(
        "--driver-runtime-kind",
        choices=["native", "full_driver_skill"],
        required=True,
    )

    retrospective_status = commands.add_parser("retrospective-status")
    retrospective_status.add_argument("--repo", default=".")
    retrospective_status.add_argument("--session-id", required=True)

    record_retrospective = commands.add_parser("record-retrospective")
    record_retrospective.add_argument("--repo", default=".")
    record_retrospective.add_argument("--session-id", required=True)
    record_retrospective.add_argument("--report", required=True)
    record_retrospective.add_argument("--replace", action="store_true")

    resolve_external_problem = commands.add_parser("resolve-external-problem")
    resolve_external_problem.add_argument("--repo", default=".")
    resolve_external_problem.add_argument("--run", required=True)
    resolve_external_problem.add_argument("--problem-key", required=True)
    resolve_external_problem.add_argument("--reason", required=True)

    context = commands.add_parser("driver-context")
    context.add_argument("--repo", default=".")
    context.add_argument("--run", required=True)

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
    update.add_argument("--resolve-plan-problem-key")
    update.add_argument("--decision-action-id")
    update.add_argument("--expected-facet-digest")
    update.add_argument("--session-id")

    revise = commands.add_parser("revise-mission")
    revise.add_argument("--repo", default=".")
    revise.add_argument("--run", required=True)
    revise.add_argument("--mission", required=True)
    revise.add_argument("--transition")
    revise.add_argument("--resolve-plan-problem-key")
    revise.add_argument("--decision-action-id")
    revise.add_argument("--expected-facet-digest")
    revise.add_argument("--session-id")

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
    prepare_tester.add_argument("--identity-only", action="store_true")
    dispatch_guard(prepare_tester)

    prepare_builder = commands.add_parser("prepare-builder")
    prepare_builder.add_argument("--repo", default=".")
    prepare_builder.add_argument("--run", required=True)
    prepare_builder.add_argument("--agent-id", required=True)
    prepare_builder.add_argument("--thread-id", required=True)
    dispatch_guard(prepare_builder)

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

    preflight = commands.add_parser("verify-preflight")
    preflight.add_argument("--repo", default=".")
    preflight.add_argument("--run", required=True)
    dispatch_guard(preflight)

    scan_doc_references = commands.add_parser("scan-doc-references")
    scan_doc_references.add_argument("--repo", default=".")
    scan_doc_references.add_argument("--run", required=True)
    dispatch_guard(scan_doc_references)

    prepare_deployment = commands.add_parser("prepare-deployment")
    prepare_deployment.add_argument("--repo", default=".")
    prepare_deployment.add_argument("--run", required=True)
    dispatch_guard(prepare_deployment)

    stage_blackbox = commands.add_parser("stage-blackbox")
    stage_blackbox.add_argument("--repo", default=".")
    stage_blackbox.add_argument("--run", required=True)
    stage_blackbox.add_argument("--report", required=True)
    dispatch_guard(stage_blackbox)

    restore_deployment = commands.add_parser("restore-deployment")
    restore_deployment.add_argument("--repo", default=".")
    restore_deployment.add_argument("--run", required=True)
    dispatch_guard(restore_deployment)

    restore_superseded = commands.add_parser("restore-superseded-environment")
    restore_superseded.add_argument("--repo", default=".")
    restore_superseded.add_argument("--run", required=True)
    dispatch_guard(restore_superseded)

    complete_supersede = commands.add_parser("complete-supersede-transfer")
    complete_supersede.add_argument("--repo", default=".")
    complete_supersede.add_argument("--run", required=True)
    dispatch_guard(complete_supersede)

    require_restore = commands.add_parser("require-deployment-restore")
    require_restore.add_argument("--repo", default=".")
    require_restore.add_argument("--run", required=True)
    require_restore.add_argument("--failure-code", required=True)
    dispatch_guard(require_restore)

    complete_blackbox = commands.add_parser("complete-blackbox")
    complete_blackbox.add_argument("--repo", default=".")
    complete_blackbox.add_argument("--run", required=True)
    dispatch_guard(complete_blackbox)

    finalize = commands.add_parser("finalize")
    finalize.add_argument("--repo", default=".")
    finalize.add_argument("--run", required=True)
    finalize.add_argument("--message", required=True)
    dispatch_guard(finalize)

    rematerialize = commands.add_parser("rematerialize-target")
    rematerialize.add_argument("--repo", default=".")
    rematerialize.add_argument("--run", required=True)
    dispatch_guard(rematerialize)

    recompose = commands.add_parser("recompose-candidate")
    recompose.add_argument("--repo", default=".")
    recompose.add_argument("--run", required=True)
    dispatch_guard(recompose)

    recover_finalize = commands.add_parser("recover-finalize")
    recover_finalize.add_argument("--repo", default=".")
    recover_finalize.add_argument("--run", required=True)
    dispatch_guard(recover_finalize)

    next_action = commands.add_parser("driver-next")
    next_action.add_argument("--repo", default=".")
    next_action.add_argument("--run", required=True)

    begin_dispatch = commands.add_parser("begin-dispatch")
    begin_dispatch.add_argument("--repo", default=".")
    begin_dispatch.add_argument("--run", required=True)
    begin_dispatch.add_argument("--action-id", required=True)
    begin_dispatch.add_argument("--action", required=True)
    begin_dispatch.add_argument("--role", choices=["builder", "tester", "reviewer"], required=True)
    begin_dispatch.add_argument("--thread-id", required=True)
    begin_dispatch.add_argument("--prompt-digest", required=True)
    begin_dispatch.add_argument("--output-schema-digest", required=True)

    bind_turn = commands.add_parser("bind-dispatch-turn")
    bind_turn.add_argument("--repo", default=".")
    bind_turn.add_argument("--run", required=True)
    bind_turn.add_argument("--action-id", required=True)
    bind_turn.add_argument("--turn-id", required=True)

    complete_dispatch = commands.add_parser("complete-dispatch")
    complete_dispatch.add_argument("--repo", default=".")
    complete_dispatch.add_argument("--run", required=True)
    complete_dispatch.add_argument("--action-id", required=True)
    complete_dispatch.add_argument("--result", required=True)

    consume_dispatch = commands.add_parser("consume-dispatch")
    consume_dispatch.add_argument("--repo", default=".")
    consume_dispatch.add_argument("--run", required=True)
    consume_dispatch.add_argument("--action-id", required=True)
    consume_dispatch.add_argument(
        "--consumer-source",
        choices=["native_driver", "full_driver_skill", "operator_recovery"],
    )

    retry_dispatch = commands.add_parser("retry-dispatch")
    retry_dispatch.add_argument("--repo", default=".")
    retry_dispatch.add_argument("--run", required=True)
    retry_dispatch.add_argument("--action-id", required=True)
    retry_dispatch.add_argument("--failure-code", required=True)

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
    expected_kind = current.get("driver_runtime_kind")
    provided_kind = getattr(args, "driver_runtime_kind", None)
    if expected_kind is not None and provided_kind != expected_kind:
        raise core.AssuranceError(
            "driver runtime owner does not match this mutation",
            code="DRIVER_RUNTIME_OWNER_MISMATCH",
            status="FAIL",
            details={"expected_driver_runtime_kind": expected_kind},
        )
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
            payload = core.validate(_json(args.contract), args.repo)
        elif args.command == "validate-decision":
            payload = driver.validate_decision(
                args.repo,
                args.run,
                session_id=args.session_id,
                problem_key=args.problem_key,
                action_id=args.action_id,
                facet=args.facet,
                facet_digest=args.facet_digest,
                replacement_contract=_json(args.contract),
            )
        elif args.command == "start":
            runtime = None
            driver_values = (
                args.driver_kind,
                args.driver_transport,
                args.driver_runtime_version,
                args.driver_protocol_schema_digest,
            )
            if args.driver_kind == "full_driver_skill" and not any(driver_values[1:]):
                wire_schema = json.loads(
                    (
                        Path(__file__).resolve().parents[3]
                        / "schema"
                        / "assurance-v4-native-agent-wire-result.schema.json"
                    ).read_text()
                )
                runtime = {
                    "kind": "full_driver_skill",
                    "protocol_version": 1,
                    "transport": "native_tools",
                    "runtime_version": "full-driver-v4-experiment",
                    "protocol_schema_digest": digest(wire_schema),
                }
            elif any(driver_values):
                if not all(driver_values):
                    raise core.AssuranceError(
                        "all driver runtime fields are required together",
                        code="DRIVER_RUNTIME_INCOMPLETE",
                    )
                runtime = {
                    "kind": args.driver_kind,
                    "protocol_version": 1,
                    "transport": args.driver_transport,
                    "runtime_version": args.driver_runtime_version,
                    "protocol_schema_digest": args.driver_protocol_schema_digest,
                }
            payload = core.start(
                args.repo,
                args.run,
                args.session_id,
                _json(args.contract),
                driver_runtime=runtime,
            )
        elif args.command == "status":
            payload = core.status(args.repo, args.run)
        elif args.command == "record-driver-failure":
            payload = core.record_driver_failure(
                args.repo,
                args.run,
                _json(args.failure),
                driver_runtime_kind=args.driver_runtime_kind,
            )
        elif args.command == "complete-driver-failure":
            payload = core.complete_driver_failure(
                args.repo,
                args.run,
                driver_runtime_kind=args.driver_runtime_kind,
            )
        elif args.command == "retrospective-status":
            payload = core.retrospective_status(args.repo, args.session_id)
        elif args.command == "record-retrospective":
            payload = core.record_retrospective(
                args.repo,
                args.session_id,
                _json(args.report),
                replace=args.replace,
            )
        elif args.command == "resolve-external-problem":
            payload = core.resolve_external_problem(
                args.repo,
                args.run,
                problem_key=args.problem_key,
                reason=args.reason,
            )
        elif args.command == "driver-context":
            payload = core.driver_context(args.repo, args.run)
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
                resolve_plan_problem_key=args.resolve_plan_problem_key,
                decision_action_id=args.decision_action_id,
                expected_facet_digest=args.expected_facet_digest,
                owner_session_id=args.session_id,
            )
        elif args.command == "revise-mission":
            payload = core.revise_mission(
                args.repo,
                args.run,
                _json(args.mission),
                _json(args.transition) if args.transition else None,
                resolve_plan_problem_key=args.resolve_plan_problem_key,
                decision_action_id=args.decision_action_id,
                expected_facet_digest=args.expected_facet_digest,
                owner_session_id=args.session_id,
            )
        elif args.command == "record-evidence":
            accepted = {
                "tester": {"tester_author", "tester_fix"},
                "proof": {"tester_proof"},
                "blackbox": {"tester_blackbox"},
                "reviewer_preflight": {"reviewer_preflight"},
                "reviewer": {"reviewer_preflight", "reviewer_final"},
                "doc_review": {"reviewer_final"},
            }.get(args.kind, set())
            _guard_dispatch(args, accepted)
            payload = core.record_evidence(args.repo, args.run, args.kind, _json(args.report))
        elif args.command == "prepare-tester":
            preparation = "tester_identity" if args.identity_only else "tester_source"
            _guard_dispatch(args, actions_for_preparation("tester", preparation))
            payload = core.prepare_tester(
                args.repo,
                args.run,
                args.agent_id,
                args.thread_id,
                replace=args.replace,
                identity_only=args.identity_only,
            )
        elif args.command == "prepare-builder":
            _guard_dispatch(args, actions_for_preparation("builder", "role_identity"))
            payload = core.prepare_builder(args.repo, args.run, args.agent_id, args.thread_id)
        elif args.command == "prepare-reviewer":
            _guard_dispatch(args, actions_for_preparation("reviewer", "role_identity"))
            payload = core.prepare_reviewer(
                args.repo,
                args.run,
                args.agent_id,
                args.thread_id,
                replace=args.replace,
            )
        elif args.command == "record-problems":
            role_actions = {
                "builder": {
                    "builder_implement",
                    "builder_fix",
                    "builder_recompose_fix",
                    "checkpoint_builder",
                },
                "tester": {
                    "tester_author",
                    "tester_proof",
                    "tester_proof_diagnose",
                    "tester_machine_diagnose",
                    "tester_blackbox",
                    "tester_fix",
                    "tester_recompose_fix",
                },
                "reviewer": {"reviewer_preflight", "reviewer_final"},
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
            _guard_dispatch(args, {"tester_proof", "tester_proof_diagnose"})
            payload = core.prove_tests(
                args.repo,
                args.run,
                _json(args.spec),
                agent_id=args.agent_id,
                thread_id=args.thread_id,
                action_id=args.action_id,
            )
        elif args.command == "integrate-tester":
            _guard_dispatch(args, {"tester_author", "tester_fix"})
            payload = core.integrate_tester(args.repo, args.run)
        elif args.command == "verify-machine":
            _guard_dispatch(args, {"verify_machine"})
            payload = core.verify_machine(args.repo, args.run, action_id=args.action_id)
        elif args.command == "verify-preflight":
            _guard_dispatch(args, {"verify_preflight"})
            payload = core.verify_preflight(args.repo, args.run, action_id=args.action_id)
        elif args.command == "scan-doc-references":
            _guard_dispatch(args, {"scan_doc_references"})
            payload = core.scan_doc_references(args.repo, args.run)
        elif args.command == "prepare-deployment":
            _guard_dispatch(args, {"prepare_deployment"})
            payload = core.prepare_deployment(args.repo, args.run)
        elif args.command == "stage-blackbox":
            _guard_dispatch(args, {"tester_blackbox"})
            payload = core.stage_blackbox(args.repo, args.run, _json(args.report))
        elif args.command == "restore-deployment":
            _guard_dispatch(args, {"restore_deployment"})
            payload = core.restore_deployment(args.repo, args.run)
        elif args.command == "restore-superseded-environment":
            _guard_dispatch(args, {"restore_superseded_environment"})
            payload = core.restore_superseded_environment(args.repo, args.run)
        elif args.command == "complete-supersede-transfer":
            _guard_dispatch(args, {"complete_supersede_transfer"})
            payload = core.complete_supersede_transfer(args.repo, args.run)
        elif args.command == "require-deployment-restore":
            _guard_dispatch(args, {"tester_blackbox"})
            payload = core.require_deployment_restore(
                args.repo, args.run, failure_code=args.failure_code
            )
        elif args.command == "complete-blackbox":
            _guard_dispatch(args, {"complete_blackbox"})
            payload = core.complete_staged_blackbox(args.repo, args.run)
        elif args.command == "finalize":
            _guard_dispatch(args, {"finalize"})
            payload = core.finalize(args.repo, args.run, args.message)
        elif args.command == "rematerialize-target":
            _guard_dispatch(args, {"rematerialize_target", "recompose_candidate"})
            payload = core.rematerialize_target(args.repo, args.run)
        elif args.command == "recompose-candidate":
            _guard_dispatch(
                args,
                {"recompose_candidate", "builder_recompose_fix", "tester_recompose_fix"},
            )
            payload = core.recompose_candidate(args.repo, args.run)
        elif args.command == "recover-finalize":
            _guard_dispatch(args, {"recover_finalize"})
            payload = core.recover_finalize(args.repo, args.run)
        elif args.command == "driver-next":
            payload = driver.next_action(args.repo, args.run)
        elif args.command == "begin-dispatch":
            payload = core.begin_dispatch(
                args.repo,
                args.run,
                action_id=args.action_id,
                action=args.action,
                role=args.role,
                thread_id=args.thread_id,
                prompt_digest=args.prompt_digest,
                output_schema_digest=args.output_schema_digest,
            )
        elif args.command == "bind-dispatch-turn":
            payload = core.bind_dispatch_turn(
                args.repo, args.run, action_id=args.action_id, turn_id=args.turn_id
            )
        elif args.command == "complete-dispatch":
            payload = core.complete_dispatch(
                args.repo,
                args.run,
                action_id=args.action_id,
                result_value=_json(args.result),
            )
        elif args.command == "consume-dispatch":
            payload = core.consume_dispatch(
                args.repo,
                args.run,
                action_id=args.action_id,
                consumer_source=args.consumer_source,
            )
        elif args.command == "retry-dispatch":
            payload = core.retry_dispatch(
                args.repo,
                args.run,
                action_id=args.action_id,
                failure_code=args.failure_code,
            )
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
