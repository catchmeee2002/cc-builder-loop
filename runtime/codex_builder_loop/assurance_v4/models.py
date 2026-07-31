from __future__ import annotations

import copy
import hashlib
import fnmatch
import json
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


def _schema(name: str) -> dict[str, Any]:
    return json.loads((schema_root() / name).read_text())


def validate_contract(value: Any) -> dict[str, Any]:
    try:
        jsonschema.Draft202012Validator(_schema("assurance-v4-contract.schema.json")).validate(value)
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
    contract["execution"].setdefault("continuation", None)
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


def validate_ledger(value: Any) -> dict[str, Any]:
    contract_schema = _schema("assurance-v4-contract.schema.json")
    ledger_schema = _schema("assurance-v4-ledger.schema.json")
    registry = Registry().with_resource(
        contract_schema["$id"], Resource.from_contents(contract_schema)
    )
    try:
        jsonschema.Draft202012Validator(ledger_schema, registry=registry).validate(value)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(part) for part in exc.absolute_path)
        raise ContractError(
            exc.message,
            code="ASSURANCE_LEDGER_INVALID",
            details={"path": path},
        ) from exc
    assert isinstance(value, dict)
    validate_contract(value["facets"])
    if facet_digests(value["facets"]) != value["digests"]:
        raise ContractError(
            "ledger facet digests do not match their canonical values",
            code="ASSURANCE_LEDGER_DIGEST_MISMATCH",
        )
    return copy.deepcopy(value)


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
        base.update(candidate_head=candidate)
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
        base.update(
            candidate_head=candidate,
            commands=execution["commands"],
            tester_source=execution.get("tester_source"),
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
    return not new_intake.issubset(old_intake)


def assurance_downgrades(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
    if not set(old["required"]).issubset(set(new["required"])):
        return True
    old_commands = {item["id"]: item for item in old["machine_commands"]}
    new_commands = {item["id"]: item for item in new["machine_commands"]}
    return any(
        command_id not in new_commands or new_commands[command_id] != command
        for command_id, command in old_commands.items()
    )
