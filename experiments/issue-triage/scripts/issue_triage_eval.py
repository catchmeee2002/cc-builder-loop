#!/usr/bin/env python3
"""Evaluate selective Issue triage with a diagnostician, attacker, and hard routing rules."""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import itertools
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
EXPERIMENT_DIR = SCRIPT_DIR.parent

import issue_triage_responses as meta  # noqa: E402


SCHEMA_VERSION = 3
DIAGNOSIS_STATES = ("established", "needs_evidence")
HUMAN_ATTENTION = ("none", "batch_approval", "first_principles")
ATTENTION_RANK = {attention: index for index, attention in enumerate(HUMAN_ATTENTION)}
WORK_QUEUES = ("agent_execute", "agent_investigate", "batch_approval", "first_principles")
ROOT_CAUSE_STATUSES = ("established", "candidate", "unknown")
ATTACK_VERDICTS = ("stands", "fails", "underdetermined")
ATTACK_ATTENTION_ESCALATIONS = HUMAN_ATTENTION
CLUSTER_ID = re.compile(r"[a-z0-9][a-z0-9-]{2,80}\Z")
MAX_PROJECTS = 8
MAX_CASES_PER_PROJECT = 24
MAX_FACTS_PER_CASE = 20
MAX_PRINCIPLES_PER_PROJECT = 20
MAX_OUTPUT_TOKENS = 6_000
DEFAULT_REASONING_EFFORT = "high"
ALLOWED_REASONING_EFFORTS = ("low", "medium", "high", "xhigh")
DIAGNOSIS_BATCH_SIZE = 3
ATTACK_BATCH_SIZE = 3

DIAGNOSTICIAN_PROMPT = EXPERIMENT_DIR / "roles" / "diagnostician.md"
ATTACKER_PROMPT = EXPERIMENT_DIR / "roles" / "attacker.md"
CLUSTERER_PROMPT = EXPERIMENT_DIR / "roles" / "clusterer.md"


@dataclasses.dataclass(frozen=True)
class Gold:
    diagnosis_state: str
    human_attention: str
    scope_inventory_required: bool
    cluster_id: str
    principle_ids: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Case:
    id: str
    source_url: str
    title: str
    facts: tuple[str, ...]
    gold: Gold


@dataclasses.dataclass(frozen=True)
class Principle:
    id: str
    text: str


@dataclasses.dataclass(frozen=True)
class Project:
    project_id: str
    goal: str
    principles: tuple[Principle, ...]
    cases: tuple[Case, ...]


@dataclasses.dataclass(frozen=True)
class Suite:
    suite_id: str
    projects: tuple[Project, ...]


def _exact_object(value: Any, *, name: str, required: Iterable[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise meta.RunnerError("input", f"{name} 必须是 JSON object", meta.EXIT_INPUT)
    required_set = set(required)
    missing = required_set - value.keys()
    extra = value.keys() - required_set
    if missing:
        raise meta.RunnerError("input", f"{name} 缺少字段: {', '.join(sorted(missing))}", meta.EXIT_INPUT)
    if extra:
        raise meta.RunnerError("input", f"{name} 含未允许字段: {', '.join(sorted(extra))}", meta.EXIT_INPUT)
    return value


def _string(value: Any, *, name: str, allow_empty: bool = False, limit: int = 20_000) -> str:
    if not isinstance(value, str):
        raise meta.RunnerError("input", f"{name} 必须是字符串", meta.EXIT_INPUT)
    if not allow_empty and not value.strip():
        raise meta.RunnerError("input", f"{name} 不能为空", meta.EXIT_INPUT)
    if len(value) > limit:
        raise meta.RunnerError("input", f"{name} 超过 {limit} 字符", meta.EXIT_INPUT)
    return value


def _string_list(
    value: Any,
    *,
    name: str,
    allow_empty: bool = True,
    max_items: int = 30,
) -> list[str]:
    if not isinstance(value, list):
        raise meta.RunnerError("input", f"{name} 必须是数组", meta.EXIT_INPUT)
    if not allow_empty and not value:
        raise meta.RunnerError("input", f"{name} 不能为空", meta.EXIT_INPUT)
    if len(value) > max_items:
        raise meta.RunnerError("input", f"{name} 最多 {max_items} 项", meta.EXIT_INPUT)
    return [_string(item, name=f"{name}[{index}]") for index, item in enumerate(value)]


def load_suite(path: Path) -> Suite:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise meta.RunnerError("input", "无法读取有效的实验样本 JSON", meta.EXIT_INPUT) from None
    root = _exact_object(raw, name="suite", required=("schema_version", "suite_id", "projects"))
    if root["schema_version"] != SCHEMA_VERSION:
        raise meta.RunnerError("input", f"只支持 schema_version={SCHEMA_VERSION}", meta.EXIT_INPUT)
    suite_id = _string(root["suite_id"], name="suite.suite_id", limit=120)
    if not isinstance(root["projects"], list) or not root["projects"]:
        raise meta.RunnerError("input", "suite.projects 必须是非空数组", meta.EXIT_INPUT)
    if len(root["projects"]) > MAX_PROJECTS:
        raise meta.RunnerError("input", f"suite.projects 最多 {MAX_PROJECTS} 项", meta.EXIT_INPUT)

    projects: list[Project] = []
    seen_project_ids: set[str] = set()
    seen_case_ids: set[str] = set()
    for project_index, project_raw in enumerate(root["projects"]):
        project_obj = _exact_object(
            project_raw,
            name=f"suite.projects[{project_index}]",
            required=("project_id", "goal", "principles", "cases"),
        )
        project_id = _string(project_obj["project_id"], name=f"projects[{project_index}].project_id", limit=80)
        if project_id in seen_project_ids:
            raise meta.RunnerError("input", f"project_id 重复: {project_id}", meta.EXIT_INPUT)
        seen_project_ids.add(project_id)
        goal = _string(project_obj["goal"], name=f"projects[{project_index}].goal")

        principles_raw = project_obj["principles"]
        if not isinstance(principles_raw, list) or not principles_raw:
            raise meta.RunnerError("input", f"{project_id}.principles 必须是非空数组", meta.EXIT_INPUT)
        if len(principles_raw) > MAX_PRINCIPLES_PER_PROJECT:
            raise meta.RunnerError(
                "input",
                f"{project_id}.principles 最多 {MAX_PRINCIPLES_PER_PROJECT} 项",
                meta.EXIT_INPUT,
            )
        principles: list[Principle] = []
        principle_ids: set[str] = set()
        for principle_index, principle_raw in enumerate(principles_raw):
            principle_obj = _exact_object(
                principle_raw,
                name=f"{project_id}.principles[{principle_index}]",
                required=("id", "text"),
            )
            principle_id = _string(principle_obj["id"], name="principle.id", limit=24)
            if principle_id in principle_ids:
                raise meta.RunnerError("input", f"{project_id} principle id 重复: {principle_id}", meta.EXIT_INPUT)
            principle_ids.add(principle_id)
            principles.append(Principle(principle_id, _string(principle_obj["text"], name="principle.text")))

        cases_raw = project_obj["cases"]
        if not isinstance(cases_raw, list) or not cases_raw:
            raise meta.RunnerError("input", f"{project_id}.cases 必须是非空数组", meta.EXIT_INPUT)
        if len(cases_raw) > MAX_CASES_PER_PROJECT:
            raise meta.RunnerError("input", f"{project_id}.cases 最多 {MAX_CASES_PER_PROJECT} 项", meta.EXIT_INPUT)
        cases: list[Case] = []
        for case_index, case_raw in enumerate(cases_raw):
            case_obj = _exact_object(
                case_raw,
                name=f"{project_id}.cases[{case_index}]",
                required=("id", "source_url", "title", "facts", "gold"),
            )
            case_id = _string(case_obj["id"], name="case.id", limit=80)
            if case_id in seen_case_ids:
                raise meta.RunnerError("input", f"case id 重复: {case_id}", meta.EXIT_INPUT)
            seen_case_ids.add(case_id)
            facts = _string_list(
                case_obj["facts"],
                name=f"{case_id}.facts",
                allow_empty=False,
                max_items=MAX_FACTS_PER_CASE,
            )
            gold_obj = _exact_object(
                case_obj["gold"],
                name=f"{case_id}.gold",
                required=(
                    "diagnosis_state",
                    "human_attention",
                    "scope_inventory_required",
                    "cluster_id",
                    "principle_ids",
                ),
            )
            diagnosis_state = _string(
                gold_obj["diagnosis_state"],
                name=f"{case_id}.gold.diagnosis_state",
                limit=40,
            )
            if diagnosis_state not in DIAGNOSIS_STATES:
                raise meta.RunnerError("input", f"{case_id}.gold.diagnosis_state 非法", meta.EXIT_INPUT)
            human_attention = _string(
                gold_obj["human_attention"],
                name=f"{case_id}.gold.human_attention",
                limit=40,
            )
            if human_attention not in HUMAN_ATTENTION:
                raise meta.RunnerError("input", f"{case_id}.gold.human_attention 非法", meta.EXIT_INPUT)
            scope_inventory_required = gold_obj["scope_inventory_required"]
            if not isinstance(scope_inventory_required, bool):
                raise meta.RunnerError(
                    "input",
                    f"{case_id}.gold.scope_inventory_required 必须是 boolean",
                    meta.EXIT_INPUT,
                )
            cluster_id = _string(gold_obj["cluster_id"], name=f"{case_id}.gold.cluster_id", limit=80)
            if not CLUSTER_ID.fullmatch(cluster_id):
                raise meta.RunnerError("input", f"{case_id}.gold.cluster_id 格式非法", meta.EXIT_INPUT)
            gold_principles = _string_list(
                gold_obj["principle_ids"],
                name=f"{case_id}.gold.principle_ids",
                allow_empty=False,
                max_items=MAX_PRINCIPLES_PER_PROJECT,
            )
            unknown_gold = set(gold_principles) - principle_ids
            if unknown_gold:
                raise meta.RunnerError(
                    "input",
                    f"{case_id}.gold 引用未知原则: {', '.join(sorted(unknown_gold))}",
                    meta.EXIT_INPUT,
                )
            cases.append(
                Case(
                    id=case_id,
                    source_url=_string(case_obj["source_url"], name=f"{case_id}.source_url"),
                    title=_string(case_obj["title"], name=f"{case_id}.title"),
                    facts=tuple(facts),
                    gold=Gold(
                        diagnosis_state=diagnosis_state,
                        human_attention=human_attention,
                        scope_inventory_required=scope_inventory_required,
                        cluster_id=cluster_id,
                        principle_ids=tuple(gold_principles),
                    ),
                )
            )
        projects.append(Project(project_id, goal, tuple(principles), tuple(cases)))
    return Suite(suite_id=suite_id, projects=tuple(projects))


def _project_prompt_data(project: Project) -> dict[str, Any]:
    return {
        "project_id": project.project_id,
        "goal": project.goal,
        "principles": [dataclasses.asdict(principle) for principle in project.principles],
        "issues": [
            {"issue_id": case.id, "title": case.title, "facts": list(case.facts)}
            for case in project.cases
        ],
    }


def _subset_project(project: Project, cases: Iterable[Case]) -> Project:
    return Project(
        project_id=project.project_id,
        goal=project.goal,
        principles=project.principles,
        cases=tuple(cases),
    )


def _chunks(values: tuple[Case, ...], size: int) -> list[tuple[Case, ...]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _cluster_schema(project: Project) -> dict[str, Any]:
    issue_ids = [case.id for case in project.cases]
    cluster = {
        "type": "object",
        "properties": {
            "cluster_id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{2,80}$"},
            "issue_ids": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "enum": issue_ids},
            },
            "shared_invariant": {"type": "string"},
            "why_same": {"type": "string"},
        },
        "required": ["cluster_id", "issue_ids", "shared_invariant", "why_same"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "clusters": {"type": "array", "minItems": 1, "items": cluster},
        },
        "required": ["clusters"],
        "additionalProperties": False,
    }


def _diagnosis_schema(project: Project) -> dict[str, Any]:
    issue_ids = [case.id for case in project.cases]
    principle_ids = [principle.id for principle in project.principles]
    flags = {
        "type": "object",
        "properties": {
            "goal_or_taste": {"type": "boolean"},
            "new_or_changed_principle": {"type": "boolean"},
            "principle_conflict": {"type": "boolean"},
            "public_contract_or_role_boundary": {"type": "boolean"},
            "wide_scope": {"type": "boolean"},
            "hard_to_reverse": {"type": "boolean"},
            "deterministic_acceptance": {"type": "boolean"},
        },
        "required": [
            "goal_or_taste",
            "new_or_changed_principle",
            "principle_conflict",
            "public_contract_or_role_boundary",
            "wide_scope",
            "hard_to_reverse",
            "deterministic_acceptance",
        ],
        "additionalProperties": False,
    }
    assessment = {
        "type": "object",
        "properties": {
            "issue_id": {"type": "string", "enum": issue_ids},
            "principle_ids": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "enum": principle_ids},
            },
            "invariant": {"type": "string"},
            "root_cause": {"type": "string"},
            "root_cause_status": {"type": "string", "enum": list(ROOT_CAUSE_STATUSES)},
            "surviving_alternatives": {"type": "array", "items": {"type": "string"}},
            "diagnostic_missing_evidence": {"type": "array", "items": {"type": "string"}},
            "scope_notes": {"type": "array", "items": {"type": "string"}},
            "flags": flags,
            "proposed_cluster_id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{2,80}$"},
        },
        "required": [
            "issue_id",
            "principle_ids",
            "invariant",
            "root_cause",
            "root_cause_status",
            "surviving_alternatives",
            "diagnostic_missing_evidence",
            "scope_notes",
            "flags",
            "proposed_cluster_id",
        ],
        "additionalProperties": False,
    }
    cluster = {
        "type": "object",
        "properties": {
            "cluster_id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{2,80}$"},
            "issue_ids": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "enum": issue_ids},
            },
            "shared_invariant": {"type": "string"},
            "why_same": {"type": "string"},
        },
        "required": ["cluster_id", "issue_ids", "shared_invariant", "why_same"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "issue_assessments": {
                "type": "array",
                "minItems": len(issue_ids),
                "maxItems": len(issue_ids),
                "items": assessment,
            },
            "clusters": {"type": "array", "minItems": 1, "items": cluster},
        },
        "required": ["issue_assessments", "clusters"],
        "additionalProperties": False,
    }


def _attack_schema(project: Project) -> dict[str, Any]:
    issue_ids = [case.id for case in project.cases]
    attack = {
        "type": "object",
        "properties": {
            "issue_id": {"type": "string", "enum": issue_ids},
            "diagnosis_verdict": {"type": "string", "enum": list(ATTACK_VERDICTS)},
            "cluster_verdict": {"type": "string", "enum": ["stands", "fails"]},
            "cluster_reason": {"type": "string"},
            "human_attention_escalation": {
                "type": "string",
                "enum": list(ATTACK_ATTENTION_ESCALATIONS),
            },
            "reason": {"type": "string"},
            "surviving_alternative": {"type": "string", "enum": ["none", "survives"]},
            "surviving_alternative_reason": {"type": "string"},
            "diagnostic_missing_evidence": {"type": "array", "items": {"type": "string"}},
            "scope_notes": {"type": "array", "items": {"type": "string"}},
            "scope_inventory_required": {"type": "boolean"},
            "principle_conflict": {"type": "boolean"},
        },
        "required": [
            "issue_id",
            "diagnosis_verdict",
            "cluster_verdict",
            "cluster_reason",
            "human_attention_escalation",
            "reason",
            "surviving_alternative",
            "surviving_alternative_reason",
            "diagnostic_missing_evidence",
            "scope_notes",
            "scope_inventory_required",
            "principle_conflict",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "attacks": {
                "type": "array",
                "minItems": len(issue_ids),
                "maxItems": len(issue_ids),
                "items": attack,
            }
        },
        "required": ["attacks"],
        "additionalProperties": False,
    }


def _validate_unique_issue_rows(rows: Any, *, expected_ids: set[str], name: str) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or len(rows) != len(expected_ids):
        raise meta.RunnerError("response", f"{name} 必须恰好覆盖全部 Issue", meta.EXIT_RESPONSE)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get("issue_id"), str):
            raise meta.RunnerError("response", f"{name}[{index}] 结构非法", meta.EXIT_RESPONSE)
        issue_id = row["issue_id"]
        if issue_id not in expected_ids or issue_id in seen:
            raise meta.RunnerError("response", f"{name} Issue 覆盖重复或未知: {issue_id}", meta.EXIT_RESPONSE)
        seen.add(issue_id)
        result.append(row)
    if seen != expected_ids:
        raise meta.RunnerError("response", f"{name} 未覆盖全部 Issue", meta.EXIT_RESPONSE)
    return result


def validate_diagnosis(value: Any, project: Project) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"issue_assessments", "clusters"}:
        raise meta.RunnerError("response", "diagnosis 顶层结构非法", meta.EXIT_RESPONSE)
    expected_ids = {case.id for case in project.cases}
    allowed_principles = {principle.id for principle in project.principles}
    assessments = _validate_unique_issue_rows(
        value["issue_assessments"], expected_ids=expected_ids, name="issue_assessments"
    )
    assessment_clusters: dict[str, str] = {}
    for row in assessments:
        principles = row.get("principle_ids")
        if not isinstance(principles, list) or not principles or not set(principles) <= allowed_principles:
            raise meta.RunnerError("response", f"{row['issue_id']} principle_ids 非法", meta.EXIT_RESPONSE)
        cluster_id = row.get("proposed_cluster_id")
        if not isinstance(cluster_id, str) or not CLUSTER_ID.fullmatch(cluster_id):
            raise meta.RunnerError("response", f"{row['issue_id']} cluster id 非法", meta.EXIT_RESPONSE)
        assessment_clusters[row["issue_id"]] = cluster_id

    clusters = value["clusters"]
    if not isinstance(clusters, list) or not clusters:
        raise meta.RunnerError("response", "clusters 必须是非空数组", meta.EXIT_RESPONSE)
    cluster_ids: set[str] = set()
    covered: set[str] = set()
    for index, cluster in enumerate(clusters):
        if not isinstance(cluster, dict):
            raise meta.RunnerError("response", f"clusters[{index}] 结构非法", meta.EXIT_RESPONSE)
        cluster_id = cluster.get("cluster_id")
        issue_ids = cluster.get("issue_ids")
        if not isinstance(cluster_id, str) or not CLUSTER_ID.fullmatch(cluster_id) or cluster_id in cluster_ids:
            raise meta.RunnerError("response", f"clusters[{index}].cluster_id 非法或重复", meta.EXIT_RESPONSE)
        if not isinstance(issue_ids, list) or not issue_ids or len(issue_ids) != len(set(issue_ids)):
            raise meta.RunnerError("response", f"clusters[{index}].issue_ids 非法", meta.EXIT_RESPONSE)
        issue_set = set(issue_ids)
        if not issue_set <= expected_ids or covered & issue_set:
            raise meta.RunnerError("response", f"clusters[{index}] 覆盖重复或未知 Issue", meta.EXIT_RESPONSE)
        for issue_id in issue_ids:
            if assessment_clusters[issue_id] != cluster_id:
                raise meta.RunnerError("response", f"{issue_id} assessment 与 cluster 表不一致", meta.EXIT_RESPONSE)
        cluster_ids.add(cluster_id)
        covered |= issue_set
    if covered != expected_ids:
        raise meta.RunnerError("response", "clusters 未覆盖全部 Issue", meta.EXIT_RESPONSE)
    return value


def validate_clusters(value: Any, project: Project) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"clusters"}:
        raise meta.RunnerError("response", "cluster 顶层结构非法", meta.EXIT_RESPONSE)
    expected_ids = {case.id for case in project.cases}
    clusters = value["clusters"]
    if not isinstance(clusters, list) or not clusters:
        raise meta.RunnerError("response", "clusters 必须是非空数组", meta.EXIT_RESPONSE)
    seen_cluster_ids: set[str] = set()
    covered: set[str] = set()
    for index, cluster in enumerate(clusters):
        if not isinstance(cluster, dict):
            raise meta.RunnerError("response", f"clusters[{index}] 结构非法", meta.EXIT_RESPONSE)
        cluster_id = cluster.get("cluster_id")
        issue_ids = cluster.get("issue_ids")
        if not isinstance(cluster_id, str) or not CLUSTER_ID.fullmatch(cluster_id) or cluster_id in seen_cluster_ids:
            raise meta.RunnerError("response", f"clusters[{index}].cluster_id 非法或重复", meta.EXIT_RESPONSE)
        if not isinstance(issue_ids, list) or not issue_ids or len(issue_ids) != len(set(issue_ids)):
            raise meta.RunnerError("response", f"clusters[{index}].issue_ids 非法", meta.EXIT_RESPONSE)
        issue_set = set(issue_ids)
        if not issue_set <= expected_ids or covered & issue_set:
            raise meta.RunnerError("response", f"clusters[{index}] 覆盖重复或未知 Issue", meta.EXIT_RESPONSE)
        seen_cluster_ids.add(cluster_id)
        covered |= issue_set
    if covered != expected_ids:
        raise meta.RunnerError("response", "clusters 未覆盖全部 Issue", meta.EXIT_RESPONSE)
    return value


def apply_clusters(assessments: list[dict[str, Any]], clusters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issue_to_cluster = {
        issue_id: cluster["cluster_id"]
        for cluster in clusters
        for issue_id in cluster["issue_ids"]
    }
    return [
        {**assessment, "proposed_cluster_id": issue_to_cluster[assessment["issue_id"]]}
        for assessment in assessments
    ]


def validate_attacks(value: Any, project: Project) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"attacks"}:
        raise meta.RunnerError("response", "attack 顶层结构非法", meta.EXIT_RESPONSE)
    expected_ids = {case.id for case in project.cases}
    attacks = _validate_unique_issue_rows(value["attacks"], expected_ids=expected_ids, name="attacks")
    for attack in attacks:
        if attack.get("diagnosis_verdict") not in ATTACK_VERDICTS:
            raise meta.RunnerError(
                "response",
                f"{attack['issue_id']} diagnosis_verdict 非法",
                meta.EXIT_RESPONSE,
            )
        if attack.get("cluster_verdict") not in {"stands", "fails"}:
            raise meta.RunnerError(
                "response",
                f"{attack['issue_id']} cluster_verdict 非法",
                meta.EXIT_RESPONSE,
            )
        if attack.get("human_attention_escalation") not in ATTACK_ATTENTION_ESCALATIONS:
            raise meta.RunnerError(
                "response",
                f"{attack['issue_id']} human_attention_escalation 非法",
                meta.EXIT_RESPONSE,
            )
        alternative = attack.get("surviving_alternative")
        if alternative not in {"none", "survives"}:
            raise meta.RunnerError("response", f"{attack['issue_id']} surviving_alternative 非法", meta.EXIT_RESPONSE)
    return value


def _max_attention(first: str, second: str) -> str:
    return first if ATTENTION_RANK[first] >= ATTENTION_RANK[second] else second


def base_axes(assessment: dict[str, Any]) -> dict[str, Any]:
    flags = assessment["flags"]
    if (
        flags["goal_or_taste"]
        or flags["new_or_changed_principle"]
        or flags["principle_conflict"]
        or not flags["deterministic_acceptance"]
    ):
        human_attention = "first_principles"
    elif flags["public_contract_or_role_boundary"] or flags["hard_to_reverse"]:
        human_attention = "batch_approval"
    else:
        human_attention = "none"
    diagnosis_state = "needs_evidence" if (
        assessment["root_cause_status"] != "established"
        or bool(assessment["surviving_alternatives"])
        or bool(assessment["diagnostic_missing_evidence"])
    ) else "established"
    return {
        "diagnosis_state": diagnosis_state,
        "human_attention": human_attention,
        "scope_inventory_required": flags["wide_scope"],
    }


def final_axes(assessment: dict[str, Any], attack: dict[str, Any]) -> dict[str, Any]:
    axes = base_axes(assessment)
    axes["diagnosis_state"] = "needs_evidence" if (
        assessment["root_cause_status"] != "established"
        or attack["diagnosis_verdict"] != "stands"
        or attack["surviving_alternative"] == "survives"
        or bool(attack["diagnostic_missing_evidence"])
    ) else "established"
    escalation = attack["human_attention_escalation"]
    if attack["principle_conflict"]:
        escalation = "first_principles"
    axes["human_attention"] = _max_attention(axes["human_attention"], escalation)
    axes["scope_inventory_required"] = False if axes["diagnosis_state"] == "needs_evidence" else (
        axes["scope_inventory_required"] or attack["scope_inventory_required"]
    )
    return axes


def work_queue(axes: dict[str, Any]) -> str:
    if axes["diagnosis_state"] == "needs_evidence":
        return "agent_investigate"
    if axes["human_attention"] == "first_principles":
        return "first_principles"
    if axes["human_attention"] == "batch_approval":
        return "batch_approval"
    return "agent_execute"


def _run_project(client: meta.ResponsesClient, project: Project, reasoning_effort: str) -> dict[str, Any]:
    diagnostician_prompt = DIAGNOSTICIAN_PROMPT.read_text(encoding="utf-8")
    attacker_prompt = ATTACKER_PROMPT.read_text(encoding="utf-8")
    clusterer_prompt = CLUSTERER_PROMPT.read_text(encoding="utf-8")

    def diagnose_batch(batch: tuple[Case, ...]) -> tuple[list[dict[str, Any]], str]:
        subproject = _subset_project(project, batch)
        result = client.request(
            developer_prompt=diagnostician_prompt,
            task_data=_project_prompt_data(subproject),
            schema_name="issue_triage_diagnosis",
            schema=_diagnosis_schema(subproject),
            validator=lambda value: validate_diagnosis(value, subproject),
            reasoning_effort=reasoning_effort,
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )
        return result.value["issue_assessments"], result.request_hash

    diagnosis_batches = _chunks(project.cases, DIAGNOSIS_BATCH_SIZE)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(3, len(diagnosis_batches))) as executor:
        diagnosis_parts = list(executor.map(diagnose_batch, diagnosis_batches))
    raw_assessments = [assessment for rows, _ in diagnosis_parts for assessment in rows]
    diagnosis_hashes = [request_hash for _, request_hash in diagnosis_parts]

    cluster_input = {
        **_project_prompt_data(project),
        "assessments": [
            {key: value for key, value in assessment.items() if key != "proposed_cluster_id"}
            for assessment in raw_assessments
        ],
    }
    cluster_result = client.request(
        developer_prompt=clusterer_prompt,
        task_data=cluster_input,
        schema_name="issue_triage_clusters",
        schema=_cluster_schema(project),
        validator=lambda value: validate_clusters(value, project),
        reasoning_effort=reasoning_effort,
        max_output_tokens=MAX_OUTPUT_TOKENS,
    )
    final_clusters = cluster_result.value["clusters"]
    final_assessment_rows = apply_clusters(raw_assessments, final_clusters)
    assessments = {row["issue_id"]: row for row in final_assessment_rows}

    def attack_batch(batch: tuple[Case, ...]) -> tuple[list[dict[str, Any]], str]:
        subproject = _subset_project(project, batch)
        batch_ids = {case.id for case in batch}
        relevant_cluster_ids = {assessments[issue_id]["proposed_cluster_id"] for issue_id in batch_ids}
        attack_data = {
            **_project_prompt_data(subproject),
            "diagnosis": {
                "issue_assessments": [assessments[case.id] for case in batch],
                "clusters": [
                    cluster for cluster in final_clusters if cluster["cluster_id"] in relevant_cluster_ids
                ],
            },
        }
        result = client.request(
            developer_prompt=attacker_prompt,
            task_data=attack_data,
            schema_name="issue_triage_attack",
            schema=_attack_schema(subproject),
            validator=lambda value: validate_attacks(value, subproject),
            reasoning_effort=reasoning_effort,
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )
        return result.value["attacks"], result.request_hash

    attack_batches = _chunks(project.cases, ATTACK_BATCH_SIZE)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(3, len(attack_batches))) as executor:
        attack_parts = list(executor.map(attack_batch, attack_batches))
    attack_rows = [attack for rows, _ in attack_parts for attack in rows]
    attack_hashes = [request_hash for _, request_hash in attack_parts]
    attacks = {row["issue_id"]: row for row in attack_rows}
    cases: list[dict[str, Any]] = []
    for case in project.cases:
        assessment = assessments[case.id]
        attack = attacks[case.id]
        predicted_axes = final_axes(assessment, attack)
        gold_axes = {
            "diagnosis_state": case.gold.diagnosis_state,
            "human_attention": case.gold.human_attention,
            "scope_inventory_required": case.gold.scope_inventory_required,
        }
        cases.append(
            {
                "issue_id": case.id,
                "source_url": case.source_url,
                "title": case.title,
                "predicted_axes": predicted_axes,
                "gold_axes": gold_axes,
                "predicted_work_queue": work_queue(predicted_axes),
                "gold_work_queue": work_queue(gold_axes),
                "gold_cluster_id": case.gold.cluster_id,
                "gold_principle_ids": list(case.gold.principle_ids),
                "assessment": assessment,
                "attack": attack,
            }
        )
    return {
        "project_id": project.project_id,
        "diagnosis": {"issue_assessments": final_assessment_rows, "clusters": final_clusters},
        "attacks": {"attacks": attack_rows},
        "cases": cases,
        "request_sha256": diagnosis_hashes + [cluster_result.request_hash] + attack_hashes,
    }


def _same_cluster_pairs(case_rows: list[dict[str, Any]], *, predicted: bool) -> set[tuple[str, str]]:
    cluster_field = "proposed_cluster_id" if predicted else "gold_cluster_id"
    clusters: dict[str, list[str]] = {}
    for row in case_rows:
        if predicted:
            cluster_id = row["assessment"][cluster_field]
        else:
            cluster_id = row[cluster_field]
        clusters.setdefault(cluster_id, []).append(row["issue_id"])
    pairs: set[tuple[str, str]] = set()
    for issue_ids in clusters.values():
        for first, second in itertools.combinations(sorted(issue_ids), 2):
            pairs.add((first, second))
    return pairs


def score(project_results: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [row for project in project_results for row in project["cases"]]
    total = len(rows)
    axis_exact = sum(row["predicted_axes"] == row["gold_axes"] for row in rows)
    queue_exact = sum(row["predicted_work_queue"] == row["gold_work_queue"] for row in rows)
    diagnosis_exact = sum(
        row["predicted_axes"]["diagnosis_state"] == row["gold_axes"]["diagnosis_state"]
        for row in rows
    )
    attention_exact = sum(
        row["predicted_axes"]["human_attention"] == row["gold_axes"]["human_attention"]
        for row in rows
    )
    scope_exact = sum(
        row["predicted_axes"]["scope_inventory_required"]
        == row["gold_axes"]["scope_inventory_required"]
        for row in rows
    )
    unsafe_auto_execute = [
        row["issue_id"]
        for row in rows
        if row["predicted_work_queue"] == "agent_execute"
        and row["gold_work_queue"] != "agent_execute"
    ]
    attention_under = [
        row["issue_id"]
        for row in rows
        if ATTENTION_RANK[row["predicted_axes"]["human_attention"]]
        < ATTENTION_RANK[row["gold_axes"]["human_attention"]]
    ]
    unnecessary_human_interrupt = [
        row["issue_id"]
        for row in rows
        if row["predicted_work_queue"] in {"batch_approval", "first_principles"}
        and row["gold_work_queue"] in {"agent_execute", "agent_investigate"}
    ]
    predicted_execute = [row for row in rows if row["predicted_work_queue"] == "agent_execute"]
    correctly_execute = [row for row in predicted_execute if row["gold_work_queue"] == "agent_execute"]
    agent_owned = [
        row for row in rows if row["predicted_work_queue"] in {"agent_execute", "agent_investigate"}
    ]
    human_interrupt = [
        row for row in rows if row["predicted_work_queue"] in {"batch_approval", "first_principles"}
    ]
    first_principles = [row for row in rows if row["predicted_work_queue"] == "first_principles"]
    principle_hits = sum(
        bool(set(row["assessment"]["principle_ids"]) & set(row["gold_principle_ids"]))
        for row in rows
    )
    predicted_pairs: set[tuple[str, str]] = set()
    gold_pairs: set[tuple[str, str]] = set()
    for project in project_results:
        predicted_pairs |= _same_cluster_pairs(project["cases"], predicted=True)
        gold_pairs |= _same_cluster_pairs(project["cases"], predicted=False)
    true_pairs = predicted_pairs & gold_pairs
    pair_precision = len(true_pairs) / len(predicted_pairs) if predicted_pairs else 1.0
    pair_recall = len(true_pairs) / len(gold_pairs) if gold_pairs else 1.0
    pair_f1 = (
        2 * pair_precision * pair_recall / (pair_precision + pair_recall)
        if pair_precision + pair_recall
        else 0.0
    )
    confusion = {gold: {predicted: 0 for predicted in WORK_QUEUES} for gold in WORK_QUEUES}
    for row in rows:
        confusion[row["gold_work_queue"]][row["predicted_work_queue"]] += 1
    return {
        "case_count": total,
        "axis_exact_accuracy": axis_exact / total if total else 0.0,
        "work_queue_exact_accuracy": queue_exact / total if total else 0.0,
        "diagnosis_state_accuracy": diagnosis_exact / total if total else 0.0,
        "human_attention_accuracy": attention_exact / total if total else 0.0,
        "scope_inventory_accuracy": scope_exact / total if total else 0.0,
        "unsafe_auto_execute_count": len(unsafe_auto_execute),
        "unsafe_auto_execute_issue_ids": unsafe_auto_execute,
        "human_attention_underestimate_count": len(attention_under),
        "human_attention_underestimate_issue_ids": attention_under,
        "unnecessary_human_interrupt_count": len(unnecessary_human_interrupt),
        "unnecessary_human_interrupt_issue_ids": unnecessary_human_interrupt,
        "agent_execute_rate": len(predicted_execute) / total if total else 0.0,
        "agent_execute_precision": len(correctly_execute) / len(predicted_execute) if predicted_execute else 1.0,
        "agent_owned_rate": len(agent_owned) / total if total else 0.0,
        "human_interrupt_rate": len(human_interrupt) / total if total else 0.0,
        "first_principles_rate": len(first_principles) / total if total else 0.0,
        "principle_any_overlap_rate": principle_hits / total if total else 0.0,
        "cluster_pair_precision": pair_precision,
        "cluster_pair_recall": pair_recall,
        "cluster_pair_f1": pair_f1,
        "predicted_cluster_count": sum(len(project["diagnosis"]["clusters"]) for project in project_results),
        "gold_cluster_count": len({(project["project_id"], row["gold_cluster_id"]) for project in project_results for row in project["cases"]}),
        "work_queue_confusion": confusion,
    }


def run_suite(
    client: meta.ResponsesClient,
    suite: Suite,
    *,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
) -> dict[str, Any]:
    if reasoning_effort not in ALLOWED_REASONING_EFFORTS:
        raise meta.RunnerError("input", f"不支持 reasoning effort: {reasoning_effort}", meta.EXIT_INPUT)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(3, len(suite.projects))) as executor:
        project_results = list(
            executor.map(lambda project: _run_project(client, project, reasoning_effort), suite.projects)
        )
    request_hashes = [
        request_hash
        for project in project_results
        for request_hash in project["request_sha256"]
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "suite_id": suite.suite_id,
        "run_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "model": client.config.model,
        "provider": client.config.provider_name,
        "reasoning_effort": reasoning_effort,
        "boundary": "上下文独立；同模型/provider，不是分布独立；gold 未发送给模型",
        "request_sha256": request_hashes,
        "projects": project_results,
        "metrics": score(project_results),
    }


def select_project(suite: Suite, project_id: str | None) -> Suite:
    if project_id is None:
        return suite
    projects = tuple(project for project in suite.projects if project.project_id == project_id)
    if not projects:
        raise meta.RunnerError("input", f"suite 不含 project: {project_id}", meta.EXIT_INPUT)
    return Suite(suite_id=suite.suite_id, projects=projects)


def select_cases(suite: Suite, case_ids: list[str] | None) -> Suite:
    if not case_ids:
        return suite
    requested = set(case_ids)
    selected_projects: list[Project] = []
    found: set[str] = set()
    for project in suite.projects:
        cases = tuple(case for case in project.cases if case.id in requested)
        if cases:
            selected_projects.append(_subset_project(project, cases))
            found |= {case.id for case in cases}
    missing = requested - found
    if missing:
        raise meta.RunnerError("input", f"suite 不含 case: {', '.join(sorted(missing))}", meta.EXIT_INPUT)
    return Suite(suite_id=suite.suite_id, projects=tuple(selected_projects))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate", help="只校验实验样本")
    validate.add_argument("--suite", type=Path, required=True)
    validate.add_argument("--project", default=None)
    validate.add_argument("--case", action="append", default=None)
    run = subparsers.add_parser("run", help="运行诊断、攻击与评分")
    run.add_argument("--suite", type=Path, required=True)
    run.add_argument("--project", default=None)
    run.add_argument("--case", action="append", default=None)
    run.add_argument("--output", type=Path, default=None)
    run.add_argument("--codex-home", type=Path, default=None)
    run.add_argument("--effort", choices=ALLOWED_REASONING_EFFORTS, default=DEFAULT_REASONING_EFFORT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        suite = select_cases(select_project(load_suite(args.suite), args.project), args.case)
        if args.command == "validate":
            print(json.dumps({"status": "ok", "suite_id": suite.suite_id, "case_count": sum(len(p.cases) for p in suite.projects)}, ensure_ascii=False))
            return 0
        config = meta.load_runtime_config(args.codex_home)
        result = run_suite(meta.ResponsesClient(config), suite, reasoning_effort=args.effort)
        serialized = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output is None:
            print(serialized, end="")
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(serialized, encoding="utf-8")
            print(json.dumps({"status": "ok", "output": str(args.output), "metrics": result["metrics"]}, ensure_ascii=False))
        return 0
    except meta.RunnerError as exc:
        print(json.dumps({"error": {"kind": exc.kind, "message": exc.safe_message}}, ensure_ascii=False), file=sys.stderr)
        return exc.exit_code
    except Exception:
        print(json.dumps({"error": {"kind": "internal", "message": "issue-triage evaluator 内部错误"}}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
