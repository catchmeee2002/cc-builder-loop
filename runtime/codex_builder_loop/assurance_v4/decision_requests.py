"""Decision proposal semantics shared by intake, correction and application.

Proposal validation does not grant authority or change a frozen facet.
"""
from __future__ import annotations
import copy
import re
from typing import Any, Mapping
from pathlib import PurePosixPath
from .models import digest

def _json_pointer_parts(pointer: str) -> list[str]:
    from .core import AssuranceError
    if not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
        raise AssuranceError(
            "decision change pointer must be an absolute JSON pointer",
            code="DECISION_POINTER_INVALID",
            status="FAIL",
            details={"pointer": pointer},
        )
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def apply_changes(value: Any, changes: Any) -> Any:
    from .core import AssuranceError
    if not isinstance(changes, list) or not changes:
        raise AssuranceError(
            "structured decision request must declare at least one exact change",
            code="DECISION_CHANGES_REQUIRED",
            status="FAIL",
        )
    result = copy.deepcopy(value)
    for change in changes:
        if not isinstance(change, Mapping):
            raise AssuranceError(
                "decision change must be an object",
                code="DECISION_CHANGE_INVALID",
                status="FAIL",
            )
        operation = change.get("operation")
        pointer = change.get("pointer")
        if operation not in {"add", "replace", "remove"} or not isinstance(pointer, str):
            raise AssuranceError(
                "decision change operation or pointer is invalid",
                code="DECISION_CHANGE_INVALID",
                status="FAIL",
                details={"change": copy.deepcopy(change)},
            )
        if operation in {"add", "replace"} and "value" not in change:
            raise AssuranceError(
                "decision add or replace change requires a value",
                code="DECISION_CHANGE_VALUE_REQUIRED",
                status="FAIL",
                details={"pointer": pointer, "operation": operation},
            )
        parts = _json_pointer_parts(pointer)
        parent = result
        for token in parts[:-1]:
            if isinstance(parent, list):
                try:
                    if not re.fullmatch(r"0|[1-9][0-9]*", token):
                        raise ValueError(token)
                    index = int(token)
                except ValueError as exc:
                    raise AssuranceError(
                        "decision array pointer is invalid",
                        code="DECISION_POINTER_INVALID",
                        status="FAIL",
                        details={"pointer": pointer},
                    ) from exc
                if index < 0 or index >= len(parent):
                    raise AssuranceError(
                        "decision pointer does not exist",
                        code="DECISION_POINTER_MISSING",
                        status="FAIL",
                        details={"pointer": pointer},
                    )
                parent = parent[index]
            elif isinstance(parent, dict) and token in parent:
                parent = parent[token]
            else:
                raise AssuranceError(
                    "decision pointer does not exist",
                    code="DECISION_POINTER_MISSING",
                    status="FAIL",
                    details={"pointer": pointer},
                )
        token = parts[-1]
        if isinstance(parent, list):
            if operation == "add" and token == "-":
                parent.append(copy.deepcopy(change["value"]))
                continue
            try:
                if not re.fullmatch(r"0|[1-9][0-9]*", token):
                    raise ValueError(token)
                index = int(token)
            except ValueError as exc:
                raise AssuranceError(
                    "decision array pointer is invalid",
                    code="DECISION_POINTER_INVALID",
                    status="FAIL",
                    details={"pointer": pointer},
                ) from exc
            upper = len(parent) if operation == "add" else len(parent) - 1
            if index < 0 or index > upper:
                raise AssuranceError(
                    "decision array pointer is out of range",
                    code="DECISION_POINTER_MISSING",
                    status="FAIL",
                    details={"pointer": pointer},
                )
            if operation == "add":
                parent.insert(index, copy.deepcopy(change["value"]))
            elif operation == "replace":
                parent[index] = copy.deepcopy(change["value"])
            else:
                parent.pop(index)
        elif isinstance(parent, dict):
            if operation in {"replace", "remove"} and token not in parent:
                raise AssuranceError(
                    "decision pointer does not exist",
                    code="DECISION_POINTER_MISSING",
                    status="FAIL",
                    details={"pointer": pointer},
                )
            if operation == "remove":
                parent.pop(token)
            else:
                parent[token] = copy.deepcopy(change["value"])
        else:
            raise AssuranceError(
                "decision pointer parent is not a container",
                code="DECISION_POINTER_INVALID",
                status="FAIL",
                details={"pointer": pointer},
            )
    return result



def validate_request(facets: Mapping[str, Any], request: Mapping[str, Any]) -> None:
    from .core import AssuranceError
    # Engineering corrections have their own whole-contract pointer convention.
    if request.get("kind") != "facet_change":
        return
    facet = request.get("facet")
    if facet not in {"mission", "authority", "assurance"}:
        raise AssuranceError("unknown decision facet", code="DECISION_FACET_UNSUPPORTED", status="FAIL")
    apply_changes(facets[facet], request.get("changes"))


def validate_report(facets: Mapping[str, Any], report: Mapping[str, Any]) -> None:
    from .core import AssuranceError
    for problem in report.get("problems", []):
        request = problem.get("decision_request")
        if not isinstance(request, Mapping):
            continue
        try:
            validate_request(facets, request)
        except AssuranceError as exc:
            raise AssuranceError("decision proposal is not applicable to its frozen facet",
                code="DECISION_REQUEST_INVALID", status="FAIL", details={
                    "problem_key": problem["key"], "cause": exc.code,
                    "details": exc.details, "recovery": "correct the result and resubmit with the same role identity",
                }) from exc


def effective_problem(ledger: Mapping[str, Any], problem: Mapping[str, Any]) -> dict[str, Any]:
    """Derive the current request; never overwrite the original problem or artifact."""
    result = copy.deepcopy(dict(problem))
    identity = problem_identity(problem)
    for event in ledger.get("events", []):
        if event.get("kind") != "decision_request_corrected":
            continue
        details = event["details"]
        if details.get("problem_identity") == identity:
            result["decision_request"] = copy.deepcopy(details["request"])
    return result


def authority_scope_preview(ledger: Mapping[str, Any], replacement: Mapping[str, Any]) -> dict[str, Any]:
    """Validate exact path additions and their work-unit dependency closure together."""
    from . import core
    old = ledger['facets']
    before, after = old['authority'], replacement['authority']
    old_exec, new_exec = old['execution'], replacement['execution']
    old_units, new_units = old_exec.get('work_units', []), new_exec.get('work_units', [])
    problems = []
    linked = old_exec != new_exec
    def reject(code, **facts):
        problems.append({'code': code, **facts})
    if linked:
        rest = copy.deepcopy(new_exec)
        rest['work_units'] = copy.deepcopy(old_units)
        if rest != old_exec or len(old_units) != len(new_units):
            reject('EXECUTION_FACTS_CHANGED')
        unchanged = copy.deepcopy(after)
        unchanged['builder_write'] = copy.deepcopy(before['builder_write'])
        if unchanged != before or not set(before['builder_write']).issubset(after['builder_write']):
            reject('AUTHORITY_NOT_ADDITIVE_BUILDER_PATHS')
    added = sorted(set(after['builder_write']) - set(before['builder_write']))
    completed = core._completed_work_unit_ids(ledger)
    assignments = []
    for first, second in zip(old_units, new_units):
        if first == second:
            continue
        restored = copy.deepcopy(second)
        restored['scope']['paths'] = copy.deepcopy(first['scope']['paths'])
        if (restored != first or first['role'] != 'builder' or first['id'] in completed
                or not set(first['scope']['paths']).issubset(second['scope']['paths'])):
            reject('WORK_UNIT_NOT_PENDING_PATH_EXTENSION', work_unit_id=first['id'])
        for path in sorted(set(second['scope']['paths']) - set(first['scope']['paths'])):
            assignments.append({'work_unit_id': first['id'], 'path': path})
            if not core._matches(path, after['builder_write']):
                reject('WORK_UNIT_PATH_NOT_AUTHORIZED', path=path)
    paths = sorted(set(added + [a['path'] for a in assignments]))
    # Legacy runs without work units retain their existing facet-only behavior.
    if linked or any(u.get('scope', {}).get('paths') for u in old_units):
        for path in paths:
            if (any(c in path for c in '*?[') or str(PurePosixPath(path)) != path
                    or '\\' in path or path.startswith(('.git/', '.codex/', '.agents/'))
                    or path in {'.git', '.codex', '.agents'}):
                reject('EXACT_PATH_REQUIRED', path=path)
            if core._matches(path, before['tester_write']):
                reject('TESTER_OWNERSHIP', path=path)
            protected = list(before.get('protected_support_paths', [])) + list(before.get('public_prerequisites', []))
            if core._matches(path, protected):
                reject('PROTECTED_OR_PUBLISHED_PATH', path=path)
            if not any(u['role'] == 'builder' and u['id'] not in completed
                       and core._matches(path, u['scope']['paths']) for u in new_units):
                reject('MISSING_PENDING_BUILDER_SCOPE', path=path)
    if linked:
        blockers = core._candidate_residue_transaction_blockers(ledger)
        for field in ('candidate_residue_intent', 'context_rotation_intent', 'dispatch_rehydration_intent', 'tester_replacement_intent'):
            if ledger.get(field) is not None:
                blockers.append(field)
        if blockers or ledger.get('hold'):
            reject('TRANSACTION_PENDING', transactions=sorted(set(blockers)))
    if problems:
        raise core.AssuranceError('authority change is not jointly executable',
            code='DECISION_SCOPE_INVALID', status='FAIL', details={'blockers': problems})
    return {'added_paths': added, 'scope_additions': assignments, 'linked': linked}


def problem_identity(problem: Mapping[str, Any]) -> str:
    return digest({k: v for k, v in problem.items() if k not in {"status", "resolution", "resolved_at"}})


def effective_problems(ledger: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [effective_problem(ledger, p) for p in ledger.get("problems", [])]
