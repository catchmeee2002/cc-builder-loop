from __future__ import annotations

import copy
import fcntl
import os
import subprocess
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Mapping

from . import core
from .core import AssuranceError, _resolve_proof_group
from .models import ContractError, digest, proof_semantic_digest
from .store import locked, read_ledger, append_event, run_dir


def _proof_input(ledger: Mapping[str, Any]) -> str:
    value = copy.deepcopy(dict(ledger))
    for key in ("events", "updated_at", "transport_cleanup_intent", "deferred_wait"):
        value.pop(key, None)
    if isinstance(value.get("driver_runtime"), dict):
        value["driver_runtime"].pop("native_transport", None)
    if isinstance(value.get("dispatch_intent"), dict):
        for key in ("native_transport_generation", "last_wire_sequence", "last_wire_event_digest"):
            value["dispatch_intent"].pop(key, None)
    value["worktree_observation"] = core._progress_side_effect_digest(ledger, "tester")
    return digest(value)


@contextmanager
def proof_snapshot(repo: Path, run_id: str):
    """Serialize this proof only; release the repository lock during commands."""
    with (run_dir(repo, run_id) / "proof-operation.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with locked(repo):
            ledger = read_ledger(repo, run_id)
            input_digest = _proof_input(ledger)
            event_count = len(ledger["events"])

        def persist():
            with locked(repo):
                current = read_ledger(repo, run_id)
                if _proof_input(current) != input_digest:
                    raise AssuranceError(
                        "proof inputs changed while commands were running",
                        code="TEST_PROOF_INPUT_DRIFT", status="NEEDS_USER",
                    )
                current["proof_failure"] = copy.deepcopy(ledger.get("proof_failure"))
                if "proof" in ledger["evidence"]:
                    current["evidence"]["proof"] = copy.deepcopy(ledger["evidence"]["proof"])
                current["events"].extend(copy.deepcopy(ledger["events"][event_count:]))
                mark_effect(current, "proof")
                core.save_ledger(repo, current)
                ledger.clear()
                ledger.update(current)

        yield ledger, persist


# Process-local authority for one already persisted result. This is neither a
# role scheduler nor a second plan: only mutations made inside this transaction
# may advance its recovery observation.
_active_application: ContextVar[tuple[str, str, str] | None] = ContextVar(
    "active_dispatch_result_application", default=None
)


def binding(ledger: Mapping[str, Any], intent: Mapping[str, Any]) -> str:
    return digest({
        "runtime": ledger.get("runtime_identity"),
        "driver": {
            name: (ledger.get("driver_runtime") or {}).get(name) for name in (
                "kind", "transport", "runtime_version", "protocol_schema_digest",
                "root_session_identity",
            )
        },
        "dispatch": {
            name: intent.get(name) for name in (
                "action_id", "action", "role", "thread_id", "owner_mode",
                "owner_session_id", "generation", "prompt_digest",
                "output_schema_digest", "context_projection_digest",
                "result_digest",
            )
        },
        "producer": ledger["facets"]["execution"]["agents"].get(intent["role"]),
        "semantic_facets": {
            name: ledger["digests"][name] for name in ("mission", "authority", "assurance")
        },
    })


def projection_digest(ledger: Mapping[str, Any], intent: Mapping[str, Any]) -> str:
    return core.canonical_context_projection_digest(
        ledger, action_id=intent["action_id"], action=intent["action"],
        role=intent["role"], work_unit_id=intent.get("work_unit_id"),
    )


def checkpoint_observation(ledger: dict[str, Any]) -> None:
    """Called in the same atomic save as a Core mutation, never after it."""
    active = _active_application.get()
    intent = ledger.get("dispatch_intent")
    if active is None or not isinstance(intent, dict):
        return
    if active[:2] != (ledger["run_id"], intent["action_id"]):
        return
    application = intent.get("application")
    if not isinstance(application, dict) or binding(ledger, intent) != active[2]:
        raise AssuranceError(
            "result application binding changed", code="DISPATCH_APPLICATION_BINDING_DRIFT",
            status="NEEDS_USER",
        )
    application["projection_digest"] = projection_digest(ledger, intent)
    application["observation_digest"] = core._progress_side_effect_digest(ledger, intent["role"])


def mark_effect(ledger: dict[str, Any], effect: str) -> bool:
    active = _active_application.get()
    intent = ledger.get("dispatch_intent")
    if active is None or not isinstance(intent, dict) or active[:2] != (ledger["run_id"], intent["action_id"]):
        return False
    application = intent.get("application")
    if not isinstance(application, dict) or application["binding_digest"] != active[2]:
        raise AssuranceError("application identity changed", code="DISPATCH_APPLICATION_BINDING_DRIFT", status="NEEDS_USER")
    if effect in application["effects"]:
        return False
    application["effects"].append(effect)
    return True


def validate_application(ledger: Mapping[str, Any], intent: Mapping[str, Any]) -> None:
    application = intent.get("application")
    if not isinstance(application, Mapping):
        return
    if application["binding_digest"] != binding(ledger, intent):
        raise AssuranceError(
            "result application binding changed", code="DISPATCH_APPLICATION_BINDING_DRIFT",
            status="NEEDS_USER",
        )
    if application["projection_digest"] != projection_digest(ledger, intent):
        raise AssuranceError(
            "result application inputs changed outside its transaction",
            code="DISPATCH_APPLICATION_INPUT_DRIFT", status="NEEDS_USER",
        )
    allowed_observations = {application["observation_digest"]}
    integration = application.get("integration")
    if isinstance(integration, Mapping):
        allowed_observations.add(integration["after_observation_digest"])
    if core._progress_side_effect_digest(ledger, intent["role"]) not in allowed_observations:
        raise AssuranceError(
            "result application worktree changed outside its transaction",
            code="DISPATCH_APPLICATION_WORKTREE_DRIFT", status="NEEDS_USER",
        )
    effects = set(application["effects"])
    if effects - {"problems"}:
        execution = ledger["facets"]["execution"]
        manifest = core._candidate_manifest_for_ledger(ledger)
        if not isinstance(manifest, Mapping) or manifest["head"] != execution.get("candidate_head"):
            raise AssuranceError("candidate differs from its applied Git fact",
                                 code="DISPATCH_APPLICATION_WORKTREE_DRIFT", status="NEEDS_USER")
        source = execution.get("tester_source")
        if "integration" in effects and isinstance(source, Mapping):
            observed = core.git(Path(source["worktree"]), "rev-parse", "HEAD").stdout.strip()
            if observed != source["head"]:
                raise AssuranceError("Tester source differs from its integration fact",
                                     code="DISPATCH_APPLICATION_WORKTREE_DRIFT", status="NEEDS_USER")


def read_result(intent: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return core._read_dispatch_result_artifact(intent)
    except ContractError as exc:
        raise AssuranceError(
            "accepted result no longer satisfies its schema",
            code=exc.code, status="NEEDS_USER", details=exc.details,
        ) from exc


def assert_applied(ledger: Mapping[str, Any], intent: Mapping[str, Any]) -> None:
    if not isinstance(intent.get("application"), Mapping):
        return
    validate_application(ledger, intent)
    result = read_result(intent)
    if isinstance(result.get("problem_report"), dict):
        required = {"problems"}
    elif intent["action"] == "reviewer_final":
        required = {"evidence:" + kind for kind in ("reviewer", "doc_review")
                    if kind in ledger["facets"]["assurance"]["required"]}
    else:
        required = {
            "builder_implement": {"checkpoint"}, "builder_fix": {"checkpoint"},
            "builder_recompose_fix": {"recompose"}, "tester_recompose_fix": {"recompose"},
            "tester_author": {"integration", "evidence:tester"},
            "tester_fix": {"integration", "evidence:tester"},
            "tester_proof": {"proof"}, "tester_proof_diagnose": {"proof"},
            "reviewer_preflight": {"evidence:reviewer_preflight"},
            "tester_blackbox": {"blackbox"} if ledger["facets"]["execution"].get("deployment") else {"evidence:blackbox"},
        }.get(intent["action"])
        if required is None:
            raise AssuranceError("unsupported result application", code="DISPATCH_APPLICATION_ACTION_UNSUPPORTED", status="NEEDS_USER")
    if not required.issubset(intent["application"]["effects"]):
        raise AssuranceError(
            "accepted result has unapplied effects", code="DISPATCH_RESULT_NOT_APPLIED", status="FAIL",
            details={"missing_effects": sorted(required - set(intent["application"]["effects"]))},
        )


def recovery_view(ledger: Mapping[str, Any]) -> dict[str, Any] | None:
    if ledger.get("phase") != "active":
        return None
    intent = ledger.get("dispatch_intent")
    if (not isinstance(intent, Mapping) or intent.get("state") != "completed"
        or not intent.get("result_digest")):
        return None
    view = {
        "state": "apply_accepted_result", "action_id": intent["action_id"],
        "generation": intent.get("generation", 1),
        "completed_effects": list(intent.get("application", {}).get("effects", [])),
        "preconditions": ["same runtime and producer", "unchanged accepted result", "bound input and worktree observations"],
    }
    try:
        validate_application(ledger, intent)
        read_result(intent)
        if "application" not in intent:
            from .driver import _dispatch_action
            _dispatch_action(ledger, str(ledger["run_id"]), intent)
    except (AssuranceError, OSError, ValueError) as exc:
        return {**view, "state": "needs_user", "code": getattr(exc, "code", "DISPATCH_RESULT_ARTIFACT_INVALID")}
    args = [
        "assurance", "--experimental-v4", "apply-dispatch-result",
        "--repo", str(ledger["repo_root"]), "--run", str(ledger["run_id"]),
        "--action-id", intent["action_id"], "--generation", str(intent.get("generation", 1)),
        "--driver-runtime-kind", str((ledger.get("driver_runtime") or {}).get("kind")),
    ]
    if intent.get("owner_mode") == "root_session":
        args.extend(["--owner-session-id", intent["owner_session_id"]])
    return {**view, "command": args}


def integrate_git(
    repo: Path, ledger: dict[str, Any], source_head: str,
    paths: list[str], retired_paths: list[str],
) -> str:
    """Persist the exact Git result before materializing an integration."""
    application = ledger["dispatch_intent"]["application"]
    candidate = Path(ledger["candidate_worktree"])
    before = core.git(candidate, "rev-parse", "HEAD").stdout.strip()
    intent = application.get("integration")
    if intent is None:
        with tempfile.TemporaryDirectory(prefix="assurance-integration-index-") as raw:
            env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
            env["GIT_INDEX_FILE"] = str(Path(raw) / "index")
            def git(*args, input_text=None):
                result = subprocess.run(
                    ["git", "-C", str(repo), *args], input=input_text, env=env,
                    text=True, capture_output=True, check=False,
                )
                if result.returncode:
                    raise AssuranceError(
                        "Tester integration could not prepare Git objects",
                        code="TESTER_INTEGRATION_PREPARE_FAILED",
                        details={"stderr": result.stderr[-8000:]},
                    )
                return result.stdout.strip()
            git("read-tree", before)
            for path in sorted(set(paths + retired_paths)):
                entry = git("ls-tree", source_head, "--", path)
                if entry:
                    mode, _kind, blob = entry.split("\t", 1)[0].split()
                    git("update-index", "--add", "--cacheinfo", mode, blob, path)
                else:
                    git("update-index", "--force-remove", "--", path)
            tree = git("write-tree")
            after = git(
                "-c", "commit.gpgSign=false", "commit-tree", tree, "-p", before,
                "-m", f"test(assurance): [cr_id_skip] Integrate Tester Source {source_head[:12]}",
            )
        observation = core._progress_side_effect_observation(ledger, "tester")
        manifest = {"head": after, "dirty_paths": [], "entries": []}
        manifest["manifest_digest"] = digest(manifest)
        observation["candidate_manifest"] = manifest
        intent = {
            "before": before, "after": after, "source_head": source_head,
            "after_observation_digest": digest(observation),
        }
        application["integration"] = intent
        core.save_ledger(repo, ledger)
    if source_head != intent["source_head"] or before not in {intent["before"], intent["after"]}:
        raise AssuranceError(
            "Tester integration identity changed", code="TESTER_INTEGRATION_INPUT_DRIFT",
            status="NEEDS_USER",
        )
    if before != intent["after"]:
        core.git(candidate, "reset", "--hard", intent["after"])
    return intent["after"]


def apply(
    repo_value: str | Path, run_value: str, *, action_id: str,
    driver_runtime_kind: str, generation: int | None = None,
    owner_session_id: str | None = None, result_value: Any = None,
) -> dict[str, Any]:
    repo, run_id = core.resolve_repo(repo_value), core.ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        core._require_driver_runtime_owner(ledger, driver_runtime_kind)
        intent = ledger.get("dispatch_intent")
        if not isinstance(intent, dict) or intent.get("action_id") != action_id:
            receipts = [
                event["details"] for event in ledger.get("events", [])
                if event.get("kind") == "dispatch_consumed"
                and event.get("details", {}).get("action_id") == action_id
                and event.get("details", {}).get("application_binding")
            ]
            if not receipts:
                raise AssuranceError("dispatch action is stale", code="DRIVER_ACTION_STALE", status="FAIL")
            receipt = receipts[-1]
            if (receipt.get("owner_session_id") != owner_session_id
                or (generation is not None and receipt.get("generation") != generation)
                or (result_value is not None and digest(result_value) not in {
                    receipt.get("result_digest"), receipt.get("submitted_result_digest")
                })):
                raise AssuranceError("consumed result identity changed", code="DISPATCH_APPLICATION_REPLAY_MISMATCH", status="FAIL")
            return core.status(repo, run_id)
        if ledger["phase"] != "active":
            raise AssuranceError("run is not active", code="ASSURANCE_RUN_NOT_ACTIVE", status="NEEDS_USER")
        compatibility = core.runtime_compatibility(ledger)
        if compatibility["state"] != "current":
            raise AssuranceError(
                "active run must use its original runtime",
                code="RUNTIME_IDENTITY_MUTATION_INCOMPATIBLE", status="NEEDS_USER",
                details={"runtime_compatibility": compatibility},
            )
        if intent.get("owner_mode") == "root_session":
            core._assert_builder_owner(ledger, owner_session_id=owner_session_id)
        elif owner_session_id is not None:
            raise AssuranceError("thread dispatch does not accept a root owner", code="BUILDER_OWNER_SESSION_MISMATCH", status="FAIL")
        if generation is not None and generation != intent.get("generation", 1):
            raise AssuranceError("dispatch generation changed", code="DISPATCH_APPLICATION_GENERATION_MISMATCH", status="FAIL")
        selected_generation = intent.get("generation", 1)
        from .driver_contract import AGENT_ACTION_CAPABILITIES
        capability = AGENT_ACTION_CAPABILITIES.get(intent["action"])
        if capability is None or capability.role != intent["role"]:
            raise AssuranceError("dispatch action does not match its role", code="DISPATCH_APPLICATION_ROLE_MISMATCH", status="FAIL")
    if result_value is not None:
        core.complete_dispatch(repo, run_id, action_id=action_id, result_value=result_value,
                               owner_session_id=owner_session_id, expected_generation=selected_generation)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        intent = ledger.get("dispatch_intent")
        if not isinstance(intent, dict) or intent.get("action_id") != action_id:
            raise AssuranceError("dispatch action is stale", code="DRIVER_ACTION_STALE", status="FAIL")
        if intent["state"] != "completed":
            raise AssuranceError("result is not complete", code="DISPATCH_NOT_COMPLETE", status="FAIL")
        result = read_result(intent)
        if digest(result) != intent.get("result_digest"):
            raise AssuranceError("result artifact changed", code="DISPATCH_RESULT_ARTIFACT_DRIFT", status="NEEDS_USER")
        if "application" not in intent:
            # Validate the original projection before claiming any application.
            from .driver import _dispatch_action
            _dispatch_action(ledger, run_id, intent)
            intent["application"] = {
                "binding_digest": binding(ledger, intent),
                "projection_digest": projection_digest(ledger, intent),
                "observation_digest": core._progress_side_effect_digest(ledger, intent["role"]),
                "effects": [],
            }
            append_event(ledger, "dispatch_application_started", {
                "action_id": action_id, "binding_digest": intent["application"]["binding_digest"],
            })
            core.save_ledger(repo, ledger)
        else:
            validate_application(ledger, intent)
        identity = intent["application"]["binding_digest"]
    token = _active_application.set((run_id, action_id, identity))
    try:
        _Application(repo, run_id, action_id, owner_session_id).execute(result)
        if read_ledger(repo, run_id).get("dispatch_intent") is not None:
            core.consume_dispatch(
                repo, run_id, action_id=action_id, owner_session_id=owner_session_id,
                consumer_source="root_session" if owner_session_id else
                    "native_driver" if driver_runtime_kind == "native" else "full_driver_skill",
            )
    except ResultApplicationError as exc:
        raise AssuranceError(str(exc), code=exc.code, status="NEEDS_USER", details=exc.details) from exc
    finally:
        _active_application.reset(token)
    return core.status(repo, run_id)


class ResultApplicationError(RuntimeError):
    def __init__(self, message: str, *, code: str, status: str = "FATAL", details: Any = None):
        super().__init__(message)
        self.code, self.status, self.details = code, status, details


class ResultSemantics:
    @staticmethod
    def _evidence_kind(evidence: dict[str, Any], kind: str) -> dict[str, Any]:
        value = copy.deepcopy(evidence)
        value["kind"] = kind
        return value


    @staticmethod
    def _bind_evidence_candidate(evidence: dict[str, Any], candidate_head: str) -> dict[str, Any]:
        value = copy.deepcopy(evidence)
        value["candidate_head"] = candidate_head
        return value


    @staticmethod
    def _bind_tester_evidence_identity(
        evidence: dict[str, Any],
        *,
        candidate_head: str,
        tester_source: dict[str, Any],
    ) -> dict[str, Any]:
        value = copy.deepcopy(evidence)
        value["candidate_head"] = candidate_head
        details = value.get("details")
        if isinstance(details, dict):
            details["source_head"] = tester_source.get("head")
            details["files"] = copy.deepcopy(tester_source.get("files"))
        return value


    @staticmethod
    def _proof_test_id_hints(context: dict[str, Any]) -> list[dict[str, str]]:
        source = context["facets"]["execution"].get("tester_source")
        if not isinstance(source, dict):
            return []
        hints: list[dict[str, str]] = []
        for item in source.get("files", []):
            path = item.get("path")
            if not isinstance(path, str):
                continue
            hint = {"path": path}
            if path.endswith(".py"):
                hint["unittest_module"] = path[:-3].replace("/", ".")
                hint["pytest_prefix"] = path + "::"
            hints.append(hint)
        return hints


    def _bind_proof_test_ids(
        self, spec: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        from ..core import proof_test_source_path

        value = copy.deepcopy(spec)
        facets = context["facets"]
        source = facets["execution"].get("tester_source")
        if not isinstance(source, dict):
            return value
        tester_head = str(source["head"])
        tester_patterns = facets["authority"]["tester_write"]
        modules = [
            item["path"][:-3].replace("/", ".")
            for item in source.get("files", [])
            if isinstance(item.get("path"), str) and item["path"].endswith(".py")
        ]
        for group in value.get("groups", []):
            try:
                resolved, _runner = _resolve_proof_group(group, facets.get("assurance", {}).get("proof_runners", []))
            except AssuranceError:
                # Core records binding failures and routes them back to Tester diagnosis.
                continue
            argv = resolved.get("argv", [])
            framework = "unittest" if "unittest" in argv else "pytest" if "pytest" in argv else ""
            if not framework:
                continue
            normalized_ids = []
            for test_id in group.get("test_ids", []):
                if proof_test_source_path(
                    self.repo, tester_head, framework, test_id, tester_patterns
                ) is not None:
                    normalized_ids.append(test_id)
                    continue
                candidates: set[str] = set()
                for module in modules:
                    basename = module.rsplit(".", 1)[-1]
                    if framework == "unittest":
                        if test_id == basename or test_id.startswith(basename + "."):
                            candidates.add(module + test_id[len(basename) :])
                        else:
                            candidates.add(module + "." + test_id)
                    elif "::" in test_id:
                        file_part, suffix = test_id.split("::", 1)
                        path = module.replace(".", "/") + ".py"
                        if file_part == Path(path).name:
                            candidates.add(path + "::" + suffix)
                bound = sorted(
                    candidate
                    for candidate in candidates
                    if proof_test_source_path(
                        self.repo, tester_head, framework, candidate, tester_patterns
                    ) is not None
                )
                normalized_ids.append(bound[0] if len(bound) == 1 else test_id)
            group["test_ids"] = normalized_ids
        return value


    @staticmethod
    def _validate_proof_diagnosis(
        result: dict[str, Any],
        *,
        proof_failure: Any = None,
        proof_runners: list[dict[str, Any]] | tuple = (),
    ) -> None:
        def semantic_digest(value):
            resolved = copy.deepcopy(value)
            try:
                resolved["groups"] = [_resolve_proof_group(group, proof_runners)[0] for group in value.get("groups", [])]
            except AssuranceError:
                return proof_semantic_digest(value)
            return proof_semantic_digest(resolved)

        spec = result.get("proof_spec")
        report = result.get("problem_report")
        problems = report.get("problems") if isinstance(report, dict) else None
        if isinstance(spec, dict):
            if isinstance(report, dict):
                raise ResultApplicationError(
                    "proof diagnosis returned both a replacement spec and a problem",
                    code="NATIVE_PROOF_DIAGNOSIS_AMBIGUOUS",
                )
            if result.get("result") != "tests_ready":
                raise ResultApplicationError(
                    "proof diagnosis returned an invalid replacement result",
                    code="NATIVE_PROOF_DIAGNOSIS_INVALID",
                )
            prior_digest = (
                proof_failure.get("spec_digest")
                if isinstance(proof_failure, dict)
                else None
            )
            if not isinstance(prior_digest, str):
                raise ResultApplicationError(
                    "proof diagnosis has no persisted source spec",
                    code="NATIVE_PROOF_DIAGNOSIS_FAILURE_MISSING",
                )
            prior_spec = proof_failure.get("spec") if isinstance(proof_failure, dict) else None
            unchanged = (semantic_digest(spec) == semantic_digest(prior_spec)
                         if isinstance(prior_spec, dict) else digest(spec) == prior_digest)
            if unchanged:
                raise ResultApplicationError(
                    "proof diagnosis replacement spec did not change",
                    code="NATIVE_PROOF_DIAGNOSIS_NO_PROGRESS",
                )
            return
        if result.get("result") not in {"fail", "blocked", "target_change_required"}:
            raise ResultApplicationError(
                "proof diagnosis returned an invalid terminal result",
                code="NATIVE_PROOF_DIAGNOSIS_INVALID",
            )
        if not isinstance(problems, list) or not problems:
            raise ResultApplicationError(
                "proof diagnosis returned no problems",
                code="NATIVE_PROOF_DIAGNOSIS_MISSING",
            )
        invalid = [
            item.get("owner")
            for item in problems
            if not isinstance(item, dict)
            or item.get("owner") not in {"builder", "tester", "plan"}
        ]
        if invalid:
            raise ResultApplicationError(
                "proof diagnosis returned an unsupported owner",
                code="NATIVE_PROOF_DIAGNOSIS_OWNER_INVALID",
                details={"owners": invalid},
            )


    @staticmethod
    def _validate_machine_diagnosis(result: dict[str, Any]) -> None:
        report = result.get("problem_report")
        problems = report.get("problems") if isinstance(report, dict) else None
        if result.get("result") not in {"fail", "blocked", "target_change_required"}:
            raise ResultApplicationError(
                "machine diagnosis returned an invalid terminal result",
                code="NATIVE_MACHINE_DIAGNOSIS_INVALID",
            )
        if not isinstance(problems, list) or not problems:
            raise ResultApplicationError(
                "machine diagnosis returned no problems",
                code="NATIVE_MACHINE_DIAGNOSIS_MISSING",
            )
        allowed = {
            "builder",
            "tester",
            "plan",
            "current_project",
            "builder_loop",
            "external_platform",
        }
        invalid = [
            item.get("owner")
            for item in problems
            if not isinstance(item, dict) or item.get("owner") not in allowed
        ]
        if invalid:
            raise ResultApplicationError(
                "machine diagnosis returned an unsupported owner",
                code="NATIVE_MACHINE_DIAGNOSIS_OWNER_INVALID",
                details={"owners": invalid},
            )


    @staticmethod
    def _proof_failure_matches(
        value: Any,
        state: Any,
        action_id: str,
        error: Any,
    ) -> bool:
        failure = value.get("failure") if isinstance(value, dict) else None
        return bool(
            state == "current"
            and value.get("action_id") == action_id
            and isinstance(value.get("failure_digest"), str)
            and isinstance(failure, dict)
            and failure.get("code") == error.code
            and failure.get("status") == error.status
        )


    @staticmethod
    def _persisted_proof_failure_matches(
        value: Any,
        state: Any,
        action_id: str,
        spec: dict[str, Any],
        agent: dict[str, Any],
    ) -> bool:
        producer = value.get("producer") if isinstance(value, dict) else None
        return bool(
            isinstance(value, dict)
            and state == "current"
            and value.get("action_id") == action_id
            and value.get("spec") == spec
            and value.get("spec_digest") == digest(spec)
            and isinstance(value.get("failure_digest"), str)
            and producer
            == {
                "role": "tester",
                "agent_id": agent.get("agent_id"),
                "thread_id": agent.get("thread_id"),
            }
        )


class _Application(ResultSemantics):
    def __init__(self, repo: Path, run_id: str, action_id: str, owner_session_id: str | None):
        self.repo, self.run_id, self.action_id = repo, run_id, action_id
        self.owner_session_id = owner_session_id

    def effect(self, name: str, operation) -> None:
        with locked(self.repo):
            ledger = read_ledger(self.repo, self.run_id)
            intent = ledger["dispatch_intent"]
            if intent is None:
                # Deployment restoration owns the persisted continuation.
                return
            validate_application(ledger, intent)
            if name in intent["application"]["effects"]:
                return
        # Core owns each atomic mutation and any Git/proof/deployment intent.
        # No application lock is held over commands or external observations.
        operation()
        with locked(self.repo):
            ledger = read_ledger(self.repo, self.run_id)
            intent = ledger["dispatch_intent"]
            if intent is None:
                return
            validate_application(ledger, intent)
            if name not in intent["application"]["effects"]:
                intent["application"]["effects"].append(name)
                core.save_ledger(self.repo, ledger)

    def evidence(self, kind: str, report: dict[str, Any]) -> None:
        self.effect("evidence:" + kind, lambda: core.record_evidence(
            self.repo, self.run_id, kind, self._evidence_kind(report, kind)
        ))

    def execute(self, result: dict[str, Any]) -> None:
        context = core.driver_context(self.repo, self.run_id)
        pending = context["dispatch_intent"]
        action, role = pending["action"], pending["role"]
        agent = context["facets"]["execution"]["agents"][role]
        problem = result.get("problem_report")
        spec = result.get("proof_spec")
        if isinstance(spec, dict):
            spec = self._bind_proof_test_ids(spec, context)
            result = {**result, "proof_spec": spec}
        if action == "tester_machine_diagnose":
            self._validate_machine_diagnosis(result)
        if action == "tester_proof_diagnose":
            if not self._persisted_proof_failure_matches(
                context.get("proof_failure"), context.get("proof_failure_state"),
                self.action_id, spec, agent,
            ):
                # After successful proof, this effect's receipt supersedes the
                # consumed diagnostic input; do not diagnose it a second time.
                if "proof" not in pending["application"]["effects"]:
                    self._validate_proof_diagnosis(
                        result, proof_failure=context.get("proof_failure"),
                        proof_runners=context["facets"]["assurance"].get("proof_runners", []),
                    )
        if isinstance(problem, dict):
            self.effect("problems", lambda: core.record_problems(
                self.repo, self.run_id, problem, role=role, agent_id=agent["agent_id"],
                thread_id=agent.get("thread_id"), action_id=self.action_id,
                owner_session_id=self.owner_session_id,
            ))
        elif action in {"builder_recompose_fix", "tester_recompose_fix"}:
            self.effect("recompose", lambda: core.recompose_candidate(
                self.repo, self.run_id, action_id=self.action_id,
                owner_session_id=self.owner_session_id,
            ))
        elif action in {"builder_implement", "builder_fix"}:
            self.effect("checkpoint", lambda: core.checkpoint_builder(
                self.repo, self.run_id, action_id=self.action_id,
                owner_session_id=self.owner_session_id,
            ))
        elif action in {"tester_author", "tester_fix"}:
            self.effect("integration", lambda: core.integrate_tester(self.repo, self.run_id))
            integrated = read_ledger(self.repo, self.run_id)["facets"]["execution"]
            self.evidence("tester", self._bind_tester_evidence_identity(
                result["evidence_report"], candidate_head=integrated["candidate_head"],
                tester_source=integrated["tester_source"],
            ))
        elif action in {"tester_proof", "tester_proof_diagnose"}:
            if not isinstance(spec, dict):
                raise ResultApplicationError("Tester returned no proof spec", code="NATIVE_PROOF_SPEC_MISSING")
            def prove():
                try:
                    core.prove_tests(
                        self.repo, self.run_id, spec, agent_id=agent["agent_id"],
                        thread_id=agent["thread_id"], action_id=self.action_id,
                    )
                except AssuranceError as exc:
                    failed = core.driver_context(self.repo, self.run_id)
                    if not self._proof_failure_matches(
                        failed.get("proof_failure"), failed.get("proof_failure_state"),
                        self.action_id, exc,
                    ):
                        raise
            self.effect("proof", prove)
        elif action == "tester_blackbox":
            if isinstance(context["facets"]["execution"].get("deployment"), dict):
                def stage():
                    try:
                        core.stage_blackbox(self.repo, self.run_id, result["evidence_report"])
                    except AssuranceError as exc:
                        core.require_deployment_restore(self.repo, self.run_id, failure_code=exc.code)
                self.effect("blackbox", stage)
            else:
                self.evidence("blackbox", result["evidence_report"])
        elif action == "reviewer_preflight":
            self.evidence("reviewer_preflight", result["evidence_report"])
        elif action == "reviewer_final":
            for kind in ("reviewer", "doc_review"):
                if kind in context["facets"]["assurance"]["required"]:
                    self.evidence(kind, result["evidence_report"])
        else:
            raise AssuranceError(
                "result action has no application contract",
                code="DISPATCH_APPLICATION_ACTION_UNSUPPORTED", status="NEEDS_USER",
            )

