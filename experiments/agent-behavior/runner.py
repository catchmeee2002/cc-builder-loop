#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


MODULE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = MODULE_ROOT.parents[1]


class ExperimentError(Exception):
    pass


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExperimentError(f"无法读取 JSON：{path}: {exc}") from exc
    schema_version = value.get("schema_version") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or type(schema_version) is not int
        or schema_version != 1
    ):
        raise ExperimentError(f"只支持 schema_version=1：{path}")
    return value


def select(items: Any, item_id: str, kind: str) -> dict[str, Any]:
    if not isinstance(items, list):
        raise ExperimentError(f"{kind} 列表无效")
    matches = [
        item
        for item in items
        if isinstance(item, dict) and item.get("id") == item_id
    ]
    if len(matches) != 1:
        raise ExperimentError(f"{kind} id 必须唯一存在：{item_id}")
    return matches[0]


def repo_commit() -> str:
    completed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise ExperimentError("无法读取仓库提交")
    return completed.stdout.strip()


def load_inputs(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    scenarios = read_json(Path(args.scenarios).resolve())
    variants = read_json(Path(args.variants).resolve())
    scenario = select(scenarios.get("scenarios"), args.scenario, "场景")
    variant = select(variants.get("variants"), args.variant, "变体")
    role = scenario.get("role")
    roles = variant.get("roles")
    declared_variant = scenario.get("variant_id")
    if (
        not isinstance(role, str)
        or not isinstance(roles, list)
        or any(not isinstance(item, str) or not item for item in roles)
        or role not in roles
    ):
        raise ExperimentError(
            f"场景 {scenario.get('id')} 的角色 {role} 与变体 {variant.get('id')} 不匹配"
        )
    if declared_variant is not None and variant.get("id") != declared_variant:
        raise ExperimentError(
            f"场景 {scenario.get('id')} 必须使用变体 {declared_variant}"
        )
    return scenario, variant


def instruction_input(variant: dict[str, Any]) -> dict[str, Any]:
    kind = variant.get("kind")
    source = variant.get("instruction_source")
    if source is None:
        if kind != "baseline":
            raise ExperimentError("instruction 变体必须声明指令来源")
        return {"path": None, "sha256": None, "revision": None, "content": ""}
    if kind != "instruction" or not isinstance(source, dict):
        raise ExperimentError("instruction_source 必须是映射或 null")
    relative = source.get("path")
    expected = source.get("sha256")
    revision = source.get("revision")
    if (
        not isinstance(relative, str)
        or not relative
        or not isinstance(expected, str)
        or len(expected) != 64
    ):
        raise ExperimentError("指令来源必须包含 path 和 64 位 sha256")
    path = (REPO_ROOT / relative).resolve()
    try:
        path.relative_to(REPO_ROOT)
    except ValueError as exc:
        raise ExperimentError("指令来源越出仓库") from exc
    if not path.is_file() or path.is_symlink():
        raise ExperimentError("指令来源必须是仓库内普通文件")
    actual = file_digest(path)
    content = path.read_text(encoding="utf-8")
    if actual != expected and isinstance(revision, str) and revision != "WORKTREE":
        completed = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "show", f"{revision}:{relative}"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode == 0:
            candidate = completed.stdout
            candidate_digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
            if candidate_digest == expected:
                content = candidate
                actual = candidate_digest
    if actual != expected:
        raise ExperimentError(
            f"指令来源摘要不匹配：{relative} expected={expected} actual={actual}"
        )
    return {
        "path": relative,
        "sha256": actual,
        "revision": revision,
        "content": content,
    }


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    scenario, variant = load_inputs(args)
    instruction = instruction_input(variant)
    commit = repo_commit()
    source_metadata = (
        None
        if instruction["path"] is None
        else {
            "path": instruction["path"],
            "sha256": instruction["sha256"],
            "revision": instruction["revision"],
        }
    )
    request = {
        "role": scenario.get("role"),
        "instructions": instruction["content"],
        "instruction_source": source_metadata,
        "prompt": scenario.get("prompt"),
    }
    input_sha = digest(
        {
            "scenario": scenario,
            "variant": {
                "id": variant.get("id"),
                "kind": variant.get("kind"),
                "roles": variant.get("roles"),
                "instruction_source": source_metadata,
            },
            "repo_commit": commit,
            "request": request,
        }
    )
    return {
        "schema_version": 1,
        "scenario_id": scenario["id"],
        "variant_id": variant["id"],
        "role": scenario["role"],
        "trigger_type": scenario["trigger_type"],
        "repo_commit": commit,
        "input_digest": input_sha,
        "request": request,
    }


def response_text(path_value: str) -> str:
    if path_value == "-":
        return sys.stdin.read()
    try:
        return Path(path_value).resolve().read_text(encoding="utf-8")
    except OSError as exc:
        raise ExperimentError(f"无法读取响应：{exc}") from exc


def score(
    args: argparse.Namespace,
    *,
    response_override: str | None = None,
) -> dict[str, Any]:
    scenario, variant = load_inputs(args)
    instruction = instruction_input(variant)
    response = (
        response_override
        if response_override is not None
        else response_text(args.response)
    )
    checks = scenario.get("mechanical_checks")
    if not isinstance(checks, dict):
        raise ExperimentError("场景缺少 mechanical_checks")
    contains = checks.get("contains", [])
    excludes = checks.get("not_contains", [])
    if (
        not isinstance(contains, list)
        or not isinstance(excludes, list)
        or any(not isinstance(item, str) or not item for item in contains + excludes)
    ):
        raise ExperimentError("机械检查必须是非空字符串列表")
    results = [
        {"kind": "包含", "value": item, "passed": item in response}
        for item in contains
    ]
    results.extend(
        {"kind": "不包含", "value": item, "passed": item not in response}
        for item in excludes
    )
    omissions = [item for item in contains if item not in response]
    false_triggers = [item for item in excludes if item in response]
    criteria = scenario.get("semantic_criteria")
    if (
        not isinstance(criteria, list)
        or not criteria
        or any(not isinstance(item, str) or not item for item in criteria)
    ):
        raise ExperimentError("场景缺少人工语义标准")
    return {
        "schema_version": 1,
        "scenario_id": scenario["id"],
        "variant_id": variant["id"],
        "trigger_type": scenario["trigger_type"],
        "instruction_source_sha256": instruction["sha256"],
        "mechanical_checks": results,
        "mechanical_pass": not omissions and not false_triggers,
        "missed_triggers": omissions,
        "false_triggers": false_triggers,
        "semantic_pending": True,
        "semantic_criteria": criteria,
        "response_digest": hashlib.sha256(response.encode("utf-8")).hexdigest(),
    }


def suite_pairs(args: argparse.Namespace) -> list[tuple[str, str, dict[str, Any]]]:
    scenarios = read_json(Path(args.scenarios).resolve()).get("scenarios")
    variants = read_json(Path(args.variants).resolve()).get("variants")
    if not isinstance(scenarios, list) or not isinstance(variants, list):
        raise ExperimentError("场景或变体列表无效")
    by_id = {
        str(item.get("id")): item
        for item in variants
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    pairs: list[tuple[str, str, dict[str, Any]]] = []
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            raise ExperimentError("场景列表包含无效条目")
        scenario_id = scenario.get("id")
        role = scenario.get("role")
        if not isinstance(scenario_id, str) or not isinstance(role, str):
            raise ExperimentError("场景必须包含 id 和 role")
        declared_variant = scenario.get("variant_id")
        if declared_variant is not None and (
            not isinstance(declared_variant, str) or not declared_variant
        ):
            raise ExperimentError(f"场景 {scenario_id} 的 variant_id 无效")
        if declared_variant is not None:
            if declared_variant not in by_id:
                raise ExperimentError(
                    f"场景 {scenario_id} 声明了不存在的变体 {declared_variant}"
                )
            variant_id = declared_variant
        else:
            preferred = f"{role}-current"
            variant_id = preferred if preferred in by_id else "baseline"
        if variant_id not in by_id:
            raise ExperimentError(f"场景 {scenario_id} 没有可用变体")
        pairs.append((scenario_id, variant_id, scenario))
    return pairs


def suite_namespace(
    args: argparse.Namespace,
    *,
    scenario_id: str,
    variant_id: str,
    response: str = "-",
) -> argparse.Namespace:
    return argparse.Namespace(
        command=args.command,
        scenario=scenario_id,
        variant=variant_id,
        scenarios=args.scenarios,
        variants=args.variants,
        response=response,
    )


def prepare_suite(args: argparse.Namespace) -> dict[str, Any]:
    requests = []
    for scenario_id, variant_id, scenario in suite_pairs(args):
        prepared = prepare(
            suite_namespace(
                args,
                scenario_id=scenario_id,
                variant_id=variant_id,
            )
        )
        prepared["request"].pop("instructions", None)
        prepared["mechanical_checks"] = scenario.get("mechanical_checks")
        prepared["semantic_criteria"] = scenario.get("semantic_criteria")
        requests.append(prepared)
    return {
        "schema_version": 1,
        "mode": "deterministic-suite",
        "command": "prepare",
        "guards": {"stale_source": "拒绝过期指令来源摘要"},
        "requests": requests,
    }


def score_suite(args: argparse.Namespace) -> dict[str, Any]:
    scores = []
    for scenario_id, variant_id, scenario in suite_pairs(args):
        checks = scenario.get("mechanical_checks")
        contains = checks.get("contains", []) if isinstance(checks, dict) else []
        fixture_response = "；".join(
            str(item) for item in contains if isinstance(item, str)
        )
        scores.append(
            score(
                suite_namespace(
                    args,
                    scenario_id=scenario_id,
                    variant_id=variant_id,
                ),
                response_override=fixture_response,
            )
        )
    return {
        "schema_version": 1,
        "mode": "deterministic-suite",
        "command": "score",
        "guards": {"stale_source": "拒绝过期指令来源摘要"},
        "scores": scores,
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="离线角色行为试验准备与评分")
    subparsers = root.add_subparsers(dest="command", required=True)
    for name in ("prepare", "score"):
        command = subparsers.add_parser(name)
        command.add_argument(
            "--scenario-id", "--scenario", dest="scenario"
        )
        command.add_argument(
            "--variant-id", "--variant", dest="variant"
        )
        command.add_argument(
            "--scenarios",
            default=str(MODULE_ROOT / "scenarios.json"),
        )
        command.add_argument(
            "--variants",
            default=str(MODULE_ROOT / "variants.json"),
        )
        if name == "score":
            command.add_argument(
                "--response-file", "--response", dest="response", default="-"
            )
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if bool(args.scenario) != bool(args.variant):
            raise ExperimentError("定向执行必须同时提供 scenario-id 和 variant-id")
        if args.scenario:
            result = prepare(args) if args.command == "prepare" else score(args)
        else:
            result = (
                prepare_suite(args)
                if args.command == "prepare"
                else score_suite(args)
            )
    except ExperimentError as exc:
        print(
            json.dumps(
                {"status": "ERROR", "message": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
