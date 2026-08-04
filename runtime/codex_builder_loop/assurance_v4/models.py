from __future__ import annotations

import copy
import hashlib
import fnmatch
import json
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

import jsonschema
from referencing import Registry, Resource


FACETS = ("mission", "authority", "assurance", "execution")
EVIDENCE_KINDS = ("machine", "tester", "proof", "blackbox", "reviewer", "doc_review")


class ContractError(ValueError):
    def __init__(self, message: str, *, code: str, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def schema_root() -> Path:
    return Path(__file__).resolve().parents[3] / "schema"


def load_json_source(path: str | Path | None, *, stdin_text: str | None = None) -> Any:
    if path in (None, "-"):
        raw = stdin_text
        if raw is None:
            raise ContractError("JSON input is required", code="JSON_INPUT_REQUIRED")
    else:
        try:
            raw = Path(path).expanduser().read_text()
        except OSError as exc:
            raise ContractError(str(exc), code="JSON_READ_ERROR") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ContractError(str(exc), code="JSON_INVALID") from exc


@lru_cache(maxsize=None)
def _schema(name: str) -> dict[str, Any]:
    return json.loads((schema_root() / name).read_text())


def validate_telemetry(value: Any) -> dict[str, Any]:
    try:
        jsonschema.Draft202012Validator(
            _schema("assurance-v4-telemetry.schema.json")
        ).validate(value)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(part) for part in exc.absolute_path)
        raise ContractError(
            exc.message,
            code="ASSURANCE_TELEMETRY_INVALID",
            details={"path": path},
        ) from exc
    assert isinstance(value, dict)
    return copy.deepcopy(value)


def _schema_registry(*names: str) -> Registry:
    registry = Registry()
    for name in names:
        value = _schema(name)
        registry = registry.with_resource(value["$id"], Resource.from_contents(value))
    return registry


def validate_lineage(value: Any) -> dict[str, Any]:
    try:
        jsonschema.Draft202012Validator(
            _schema("assurance-v4-lineage.schema.json"),
            registry=_schema_registry("assurance-v4-telemetry.schema.json"),
        ).validate(value)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(part) for part in exc.absolute_path)
        raise ContractError(
            exc.message,
            code="ASSURANCE_LINEAGE_INVALID",
            details={"path": path},
        ) from exc
    assert isinstance(value, dict)
    return copy.deepcopy(value)


def validate_environment_probe(value: Any) -> dict[str, Any]:
    try:
        jsonschema.Draft202012Validator(
            _schema("assurance-v4-environment-probe.schema.json")
        ).validate(value)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(part) for part in exc.absolute_path)
        raise ContractError(
            exc.message,
            code="ENVIRONMENT_PROBE_INVALID",
            details={"path": path},
        ) from exc
    assert isinstance(value, dict)
    return copy.deepcopy(value)


def validate_contract(value: Any) -> dict[str, Any]:
    try:
        jsonschema.Draft202012Validator(
            _schema("assurance-v4-contract.schema.json"),
            registry=_schema_registry(
                "assurance-v4-lineage.schema.json",
                "assurance-v4-telemetry.schema.json",
            ),
        ).validate(value)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(part) for part in exc.absolute_path)
        raise ContractError(
            exc.message,
            code="ASSURANCE_CONTRACT_INVALID",
            details={"path": path},
        ) from exc
    assert isinstance(value, dict)
    contract = copy.deepcopy(value)
    contract["mission"].setdefault("delivery_kind", "code")
    contract["authority"].setdefault("public_prerequisites", [])
    contract["authority"].setdefault("protected_support_paths", [])
    contract["authority"].setdefault("external_targets", [])
    contract["execution"].setdefault("continuation", None)
    contract["execution"].setdefault("deployment", None)
    contract["execution"].setdefault("driver_enforced", False)
    contract["execution"].setdefault("revision_transition", None)
    contract["execution"].setdefault("prior_problem_dispositions", None)
    all_commands = list(contract["assurance"]["machine_commands"]) + list(
        contract["execution"]["commands"]
    )
    deployment = contract["execution"].get("deployment")
    if isinstance(deployment, dict):
        all_commands.extend(
            deployment[name]
            for name in ("build_command", "deploy_command", "probe_command", "restore_command")
        )
    for command in all_commands:
        command.setdefault("expected_returncodes", [0])
        command.setdefault("run_before_full_suite", False)
    for facet, fields in (
        ("mission", ("behaviors", "interfaces", "acceptance_cases", "trust_boundaries")),
    ):
        for field in fields:
            ids = [item["id"] for item in contract[facet][field]]
            if len(ids) != len(set(ids)):
                raise ContractError(
                    f"duplicate {facet}.{field} ids",
                    code="ASSURANCE_CONTRACT_DUPLICATE_ID",
                    details={"facet": facet, "field": field},
                )
    command_ids = [item["id"] for item in contract["assurance"]["machine_commands"]]
    if len(command_ids) != len(set(command_ids)):
        raise ContractError(
            "duplicate assurance.machine_commands ids",
            code="ASSURANCE_CONTRACT_DUPLICATE_ID",
            details={"facet": "assurance", "field": "machine_commands"},
        )
    for command in contract["assurance"]["machine_commands"]:
        executable = command["argv"][0]
        path = PurePosixPath(executable)
        if not path.is_absolute() and "/" in executable and any(
            part in {"", ".."} for part in path.parts
        ):
            raise ContractError(
                "repository machine executable path must be canonical",
                code="MACHINE_EXECUTABLE_PATH_INVALID",
                details={"command_id": command["id"], "executable": executable},
            )
    execution_ids = [item["id"] for item in contract["execution"]["commands"]]
    if len(execution_ids) != len(set(execution_ids)):
        raise ContractError(
            "duplicate execution.commands ids",
            code="ASSURANCE_CONTRACT_DUPLICATE_ID",
            details={"facet": "execution", "field": "commands"},
        )
    invalid_early = [
        item["id"]
        for item in contract["execution"]["commands"]
        if item.get("run_before_full_suite")
    ]
    if invalid_early:
        raise ContractError(
            "only machine commands may run before the full suite",
            code="EXECUTION_COMMAND_ORDER_INVALID",
            details={"command_ids": invalid_early},
        )
    for facet in ("authority", "execution"):
        for field in ("builder_write", "tester_write") if facet == "authority" else ("builder_files", "tester_files"):
            for item in contract[facet][field]:
                validate_repo_path(item)
                if facet == "execution" and any(token in item for token in "*?["):
                    raise ContractError(
                        "execution file manifests require exact paths",
                        code="EXECUTION_PATH_NOT_EXACT",
                        details={"path": item},
                    )
    for field in ("public_prerequisites", "protected_support_paths"):
        for item in contract["authority"][field]:
            validate_repo_path(item)
            if any(token in item for token in "*?["):
                raise ContractError(
                    f"authority.{field} requires exact paths",
                    code="AUTHORITY_PATH_NOT_EXACT",
                    details={"field": field, "path": item},
                )
    public = set(contract["authority"]["public_prerequisites"])
    if not public.issubset(set(contract["execution"]["builder_files"])) and contract["execution"]["builder_files"]:
        raise ContractError(
            "public prerequisites must be classified as Builder files",
            code="PUBLIC_PREREQUISITE_CLASSIFICATION_INVALID",
            details={"paths": sorted(public - set(contract["execution"]["builder_files"]))},
        )
    continuation = contract["execution"].get("continuation")
    if isinstance(continuation, dict):
        for item in continuation["support_paths"]:
            validate_repo_path(item)
    deployment = contract["execution"].get("deployment")
    if isinstance(deployment, dict):
        validate_repo_path(deployment["artifact_path"])
        if any(token in deployment["artifact_path"] for token in "*?["):
            raise ContractError(
                "deployment artifact path must be exact",
                code="DEPLOYMENT_ARTIFACT_PATH_INVALID",
            )
        target_ids = {item["id"] for item in contract["authority"]["external_targets"]}
        if deployment["target_id"] not in target_ids:
            raise ContractError(
                "deployment target is not authorized",
                code="DEPLOYMENT_TARGET_UNAUTHORIZED",
                details={"target_id": deployment["target_id"]},
            )
        if "blackbox" not in set(contract["assurance"]["required"]):
            raise ContractError(
                "deployment requires blackbox assurance",
                code="DEPLOYMENT_BLACKBOX_REQUIRED",
            )
    supersedes = contract["mission"].get("supersedes")
    if contract["mission"]["revision"] == 1 and supersedes is not None:
        raise ContractError(
            "mission revision 1 cannot supersede another run",
            code="MISSION_SUPERSEDES_UNEXPECTED",
        )
    carryover = contract["execution"].get("carryover")
    if carryover is not None and supersedes is None:
        raise ContractError(
            "execution carryover requires mission supersedes",
            code="CARRYOVER_SUPERSEDES_REQUIRED",
        )
    if isinstance(carryover, dict):
        invalid_carryover = sorted(
            item["path"]
            for item in carryover["files"]
            if not any(
                fnmatch.fnmatchcase(item["path"], pattern)
                for pattern in [*contract["authority"]["builder_write"], *contract["authority"]["tester_write"]]
            )
        )
        if invalid_carryover:
            raise ContractError(
                "carryover files must remain inside frozen ownership authority",
                code="CARRYOVER_AUTHORITY_INVALID",
                details={"paths": invalid_carryover},
            )
    transition = contract["execution"].get("revision_transition")
    prior_problems = contract["execution"].get("prior_problem_dispositions")
    if contract["mission"]["revision"] == 1 and (transition is not None or prior_problems is not None):
        raise ContractError(
            "mission revision 1 cannot declare revision continuity",
            code="REVISION_CONTINUITY_UNEXPECTED",
        )
    if isinstance(prior_problems, dict):
        keys = [item["key"] for item in prior_problems["items"]]
        if len(keys) != len(set(keys)):
            raise ContractError(
                "prior-problem dispositions contain duplicate keys",
                code="PRIOR_PROBLEM_DUPLICATE",
            )
    execution = contract["execution"]
    builder_files = set(execution["builder_files"])
    tester_files = set(execution["tester_files"])
    if builder_files & tester_files:
        raise ContractError(
            "execution builder_files and tester_files must be disjoint",
            code="EXECUTION_OWNERSHIP_OVERLAP",
            details={"paths": sorted(builder_files & tester_files)},
        )
    invalid_builder = sorted(
        path
        for path in builder_files
        if not any(fnmatch.fnmatchcase(path, pattern) for pattern in contract["authority"]["builder_write"])
    )
    invalid_tester = sorted(
        path
        for path in tester_files
        if not any(fnmatch.fnmatchcase(path, pattern) for pattern in contract["authority"]["tester_write"])
    )
    if invalid_builder or invalid_tester:
        raise ContractError(
            "execution files must match their owning role authority",
            code="EXECUTION_OWNERSHIP_VIOLATION",
            details={"builder_files": invalid_builder, "tester_files": invalid_tester},
        )
    for item in contract["authority"]["dirty_intake"]:
        validate_repo_path(item["path"])
        if any(token in item["path"] for token in "*?["):
            raise ContractError(
                "dirty intake requires exact paths",
                code="DIRTY_INTAKE_PATH_NOT_EXACT",
                details={"path": item["path"]},
            )
    builder = contract["authority"]["builder_write"]
    tester = contract["authority"]["tester_write"]
    overlaps = sorted(
        {f"{left} <> {right}" for left in builder for right in tester if patterns_overlap(left, right)}
    )
    if overlaps:
        raise ContractError(
            "builder and tester authority must not overlap",
            code="AUTHORITY_OWNERSHIP_OVERLAP",
            details={"overlaps": overlaps},
        )
    if "machine" in contract["assurance"]["required"] and not contract["assurance"]["machine_commands"]:
        raise ContractError(
            "required machine assurance needs at least one command",
            code="MACHINE_COMMAND_REQUIRED",
        )
    delivery_kind = contract["mission"]["delivery_kind"]
    required = set(contract["assurance"]["required"])
    if delivery_kind == "documentation":
        invalid = required & {"tester", "proof", "machine", "blackbox"}
        if invalid or "reviewer" not in required or "doc_review" not in required:
            raise ContractError(
                "documentation delivery requires only Reviewer and doc-review assurance",
                code="DOCUMENTATION_ASSURANCE_INVALID",
                details={"invalid": sorted(invalid)},
            )
    if contract["authority"]["public_prerequisites"] and "tester" not in required:
        raise ContractError(
            "public prerequisites require Tester assurance",
            code="PUBLICATION_TESTER_REQUIRED",
        )
    invalid_support = sorted(
        path
        for path in contract["authority"]["protected_support_paths"]
        if not any(fnmatch.fnmatchcase(path, pattern) for pattern in builder)
    )
    if invalid_support:
        raise ContractError(
            "protected support paths must be Builder-owned",
            code="PROTECTED_SUPPORT_AUTHORITY_INVALID",
            details={"paths": invalid_support},
        )
    return contract


def validate_evidence_report(value: Any) -> dict[str, Any]:
    try:
        jsonschema.Draft202012Validator(_schema("assurance-v4-evidence.schema.json")).validate(value)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(part) for part in exc.absolute_path)
        raise ContractError(
            exc.message,
            code="EVIDENCE_REPORT_INVALID",
            details={"path": path},
        ) from exc
    assert isinstance(value, dict)
    return copy.deepcopy(value)


def validate_problem_report(value: Any) -> dict[str, Any]:
    try:
        jsonschema.Draft202012Validator(_schema("codex-problem-report.schema.json")).validate(value)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(part) for part in exc.absolute_path)
        raise ContractError(
            exc.message,
            code="PROBLEM_REPORT_INVALID",
            details={"path": path},
        ) from exc
    assert isinstance(value, dict)
    return copy.deepcopy(value)


def validate_agent_result(value: Any) -> dict[str, Any]:
    try:
        jsonschema.Draft202012Validator(
            _schema("assurance-v4-agent-result.schema.json")
        ).validate(value)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(part) for part in exc.absolute_path)
        raise ContractError(
            exc.message,
            code="AGENT_RESULT_INVALID",
            details={"path": path},
        ) from exc
    assert isinstance(value, dict)
    result = copy.deepcopy(value)
    if result.get("evidence_report") is not None:
        result["evidence_report"] = validate_evidence_report(result["evidence_report"])
    if result.get("proof_spec") is not None:
        result["proof_spec"] = validate_test_proof_spec(result["proof_spec"])
    if result.get("problem_report") is not None:
        result["problem_report"] = validate_problem_report(result["problem_report"])
    return result


def validate_test_proof_spec(value: Any) -> dict[str, Any]:
    try:
        jsonschema.Draft202012Validator(_schema("codex-test-proof.schema.json")).validate(value)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(part) for part in exc.absolute_path)
        raise ContractError(
            exc.message,
            code="TEST_PROOF_SPEC_INVALID",
            details={"path": path},
        ) from exc
    assert isinstance(value, dict)
    return copy.deepcopy(value)


def _validate_retrospective(value: Any, definition: str) -> dict[str, Any]:
    schema = _schema("assurance-v4-retrospective.schema.json")
    try:
        jsonschema.Draft202012Validator(
            {"$ref": f"{schema['$id']}#/$defs/{definition}"},
            registry=Registry().with_resource(schema["$id"], Resource.from_contents(schema)),
        ).validate(value)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(part) for part in exc.absolute_path)
        raise ContractError(
            exc.message,
            code="RETROSPECTIVE_REPORT_INVALID",
            details={"path": path},
        ) from exc
    assert isinstance(value, dict)
    return copy.deepcopy(value)


def validate_retrospective_snapshot(value: Any) -> dict[str, Any]:
    return _validate_retrospective(value, "snapshot")


def validate_retrospective_report(value: Any) -> dict[str, Any]:
    return _validate_retrospective(value, "reportInput")


def validate_stored_retrospective_report(value: Any) -> dict[str, Any]:
    return _validate_retrospective(value, "storedReport")


def validate_ledger(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("ledger must be an object", code="ASSURANCE_LEDGER_INVALID")
    normalized = copy.deepcopy(value)
    if "runtime_identity" not in normalized:
        normalized["runtime_identity"] = {
            "adapter": "unknown",
            "adapter_commit": None,
            "adapter_dirty": None,
            "capture_status": "legacy-unavailable",
        }
    elif normalized["runtime_identity"] is None:
        normalized["runtime_identity"] = {
            "adapter": "unknown",
            "adapter_commit": None,
            "adapter_dirty": None,
            "capture_status": "unavailable",
        }
    ledger_schema = _schema("assurance-v4-ledger.schema.json")
    registry = _schema_registry(
        "assurance-v4-contract.schema.json",
        "assurance-v4-lineage.schema.json",
        "assurance-v4-telemetry.schema.json",
        "codex-test-proof.schema.json",
    )
    try:
        jsonschema.Draft202012Validator(ledger_schema, registry=registry).validate(normalized)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(part) for part in exc.absolute_path)
        raise ContractError(
            exc.message,
            code="ASSURANCE_LEDGER_INVALID",
            details={"path": path},
        ) from exc
    validate_contract(normalized["facets"])
    if facet_digests(normalized["facets"]) != normalized["digests"]:
        raise ContractError(
            "ledger facet digests do not match their canonical values",
            code="ASSURANCE_LEDGER_DIGEST_MISMATCH",
        )
    return normalized


def validate_repo_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not value or any(part in {"", ".", ".."} for part in path.parts):
        raise ContractError(
            f"unsafe repository path: {value!r}",
            code="ASSURANCE_PATH_UNSAFE",
            details={"path": value},
        )
    return path.as_posix()


def facet_digests(contract: Mapping[str, Any]) -> dict[str, str]:
    return {facet: digest(contract[facet]) for facet in FACETS}


def requirement(contract: Mapping[str, Any], kind: str) -> Any:
    if kind == "machine":
        return {
            "required": kind in contract["assurance"]["required"],
            "commands": contract["assurance"]["machine_commands"],
        }
    return {"required": kind in contract["assurance"]["required"]}


def tester_source_dependency(execution: Mapping[str, Any]) -> Any:
    source = execution.get("tester_source")
    if not isinstance(source, Mapping):
        return None
    files = sorted(
        (copy.deepcopy(item) for item in source.get("files", [])),
        key=lambda item: (item.get("path", ""), item.get("blob", "")),
    )
    replaced = sorted(
        (copy.deepcopy(item) for item in source.get("replaces_files", [])),
        key=lambda item: (item.get("path", ""), item.get("blob", "")),
    )
    return {
        "head": source.get("head"),
        "base_head": source.get("base_head"),
        "files": files,
        "replaces_files": replaced,
        "agent": copy.deepcopy(source.get("agent")),
    }


def evidence_dependency(
    ledger: Mapping[str, Any], kind: str, *, evidence: Mapping[str, Any] | None = None
) -> str:
    contract = ledger["facets"]
    execution = contract["execution"]
    candidate = execution.get("candidate_head")
    base: dict[str, Any] = {
        "kind": kind,
        "mission": ledger["digests"]["mission"],
        "authority": ledger["digests"]["authority"],
        "requirement": requirement(contract, kind),
    }
    if kind == "machine":
        base.update(
            candidate_head=candidate,
            tester_source=tester_source_dependency(execution),
        )
    elif kind == "tester":
        base.update(
            target_start_head=ledger["target_start_head"],
            tester_files=execution["tester_files"],
            tester_source=execution.get("tester_source"),
        )
    elif kind == "proof":
        base.update(
            candidate_head=candidate,
            tester_source=execution.get("tester_source"),
            behaviors=[item["id"] for item in contract["mission"]["behaviors"]],
        )
    elif kind == "blackbox":
        transaction = ledger.get("deployment_transaction")
        deployment_observation = None
        if isinstance(transaction, Mapping):
            deployment_observation = {
                "target_id": transaction.get("target_id"),
                "artifact_sha256": transaction.get("artifact_sha256"),
                "baseline_probe": transaction.get("baseline_probe"),
                "deployed_probe": transaction.get("deployed_probe"),
                "deploy_action": transaction.get("deploy_action", "executed"),
            }
        base.update(
            candidate_head=candidate,
            commands=execution["commands"],
            tester_source=execution.get("tester_source"),
            deployment=execution.get("deployment"),
            deployment_observation=deployment_observation,
        )
    elif kind in {"reviewer", "doc_review"}:
        base.update(
            candidate_head=candidate,
            assurance=ledger["digests"]["assurance"],
            execution=ledger["digests"]["execution"],
        )
        if kind == "reviewer":
            prereq = {}
            for name in ("machine", "tester", "proof", "blackbox"):
                record = ledger.get("evidence", {}).get(name)
                prereq[name] = (
                    {
                        "status": record.get("status"),
                        "dependency_digest": record.get("dependency_digest"),
                    }
                    if isinstance(record, dict)
                    else None
                )
            base["prerequisites"] = prereq
            base["reviewer_agent"] = execution["agents"].get("reviewer")
    else:
        raise ContractError(f"unknown evidence kind: {kind}", code="EVIDENCE_KIND_INVALID")
    if evidence is not None:
        base["details"] = evidence.get("details", {})
    return digest(base)


def pattern_covers(old: str, new: str) -> bool:
    if old == new or old in {"*", "**"}:
        return True
    if old.endswith("/**"):
        prefix = old[:-3].rstrip("/")
        return new == prefix or new.startswith(prefix + "/")
    if not any(token in new for token in "*?["):
        return fnmatch.fnmatchcase(new, old)
    return False


def patterns_overlap(left: str, right: str) -> bool:
    if pattern_covers(left, right) or pattern_covers(right, left):
        return True
    left_prefix = left.split("*", 1)[0].split("?", 1)[0].split("[", 1)[0].rstrip("/")
    right_prefix = right.split("*", 1)[0].split("?", 1)[0].split("[", 1)[0].rstrip("/")
    return bool(left_prefix and right_prefix) and (
        left_prefix.startswith(right_prefix + "/") or right_prefix.startswith(left_prefix + "/")
    )


def authority_expands(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
    for field in ("builder_write", "tester_write"):
        if any(not any(pattern_covers(previous, current) for previous in old[field]) for current in new[field]):
            return True
    old_intake = {(item["path"], item["sha256"]) for item in old["dirty_intake"]}
    new_intake = {(item["path"], item["sha256"]) for item in new["dirty_intake"]}
    if not new_intake.issubset(old_intake):
        return True
    old_targets = {item["id"] for item in old.get("external_targets", [])}
    new_targets = {item["id"] for item in new.get("external_targets", [])}
    return not new_targets.issubset(old_targets)


def assurance_downgrades(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
    if not set(old["required"]).issubset(set(new["required"])):
        return True
    old_commands = {item["id"]: item for item in old["machine_commands"]}
    new_commands = {item["id"]: item for item in new["machine_commands"]}
    return any(
        command_id not in new_commands or new_commands[command_id] != command
        for command_id, command in old_commands.items()
    )
