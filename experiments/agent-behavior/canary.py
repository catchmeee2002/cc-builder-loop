#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


MODULE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = MODULE_ROOT.parents[1]
DEFAULT_MANIFEST = MODULE_ROOT / "canaries.json"
FIXTURE_KINDS = {
    "document-ground-truth",
    "feature-content-density",
    "large-diff",
    "positive-outcome",
    "producer-consumer-chain",
}


class CanaryError(Exception):
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


def read_manifest(path_value: str | Path) -> dict[str, Any]:
    path = Path(path_value).resolve()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CanaryError(f"无法读取 canary manifest：{path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise CanaryError("只支持 schema_version=1 的 canary manifest")
    cases = value.get("cases")
    if not isinstance(cases, list) or not cases:
        raise CanaryError("canary manifest 必须包含非空 cases")
    ids: list[str] = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise CanaryError(f"cases[{index}] 必须是映射")
        case_id = case.get("id")
        issue = case.get("issue")
        mode = case.get("mode")
        observations = case.get("required_observations")
        if (
            not isinstance(case_id, str)
            or not case_id
            or type(issue) is not int
            or issue <= 0
            or mode not in {"fixture", "operational_probe"}
            or not isinstance(observations, list)
            or not observations
            or any(not isinstance(item, str) or not item for item in observations)
        ):
            raise CanaryError(f"cases[{index}] 缺少有效 id/issue/mode/observations")
        ids.append(case_id)
        if mode == "fixture":
            _validate_fixture_case(case, index=index)
        else:
            if case.get("probe_kind") != "lock-contention":
                raise CanaryError(f"cases[{index}] probe_kind 不受支持")
            timeout = case.get("timeout_seconds")
            holder = case.get("holder_seconds")
            if (
                not isinstance(timeout, (int, float))
                or isinstance(timeout, bool)
                or timeout <= 0
                or not isinstance(holder, (int, float))
                or isinstance(holder, bool)
                or holder <= timeout
            ):
                raise CanaryError(
                    f"cases[{index}] timeout_seconds/holder_seconds 无效"
                )
    if len(ids) != len(set(ids)):
        raise CanaryError("canary case id 必须唯一")
    return value


def _validate_fixture_case(case: Mapping[str, Any], *, index: int) -> None:
    if case.get("fixture_kind") not in FIXTURE_KINDS:
        raise CanaryError(f"cases[{index}] fixture_kind 无效")
    if not isinstance(case.get("scenario_id"), str) or not case.get("scenario_id"):
        raise CanaryError(f"cases[{index}] scenario_id 无效")
    roles = case.get("roles")
    if (
        not isinstance(roles, list)
        or not roles
        or any(not isinstance(item, str) or not item for item in roles)
    ):
        raise CanaryError(f"cases[{index}] roles 无效")
    minimum = case.get("minimum_fresh_samples")
    if type(minimum) is not int or minimum <= 0:
        raise CanaryError(f"cases[{index}] minimum_fresh_samples 无效")
    for field in ("weak_check", "discriminating_check"):
        check = case.get(field)
        if not isinstance(check, dict):
            raise CanaryError(f"cases[{index}].{field} 必须是映射")
        argv = check.get("argv")
        expected = check.get("expected_returncodes")
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(item, str) or not item for item in argv)
            or not isinstance(expected, list)
            or not expected
            or any(type(item) is not int for item in expected)
        ):
            raise CanaryError(f"cases[{index}].{field} 无效")
    mutation = case.get("proof_mutation")
    if mutation is None:
        return
    if not isinstance(mutation, dict):
        raise CanaryError(f"cases[{index}].proof_mutation 必须是映射")
    argv = mutation.get("argv")
    expected = mutation.get("expected_returncodes")
    baseline_expected = mutation.get("baseline_expected_returncodes")
    target_paths = mutation.get("target_paths")
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(item, str) or not item for item in argv)
        or not isinstance(expected, list)
        or not expected
        or any(type(item) is not int for item in expected)
        or not isinstance(baseline_expected, list)
        or not baseline_expected
        or any(type(item) is not int for item in baseline_expected)
        or not isinstance(target_paths, list)
        or not target_paths
        or any(not isinstance(item, str) or not item for item in target_paths)
        or len(target_paths) != len(set(target_paths))
    ):
        raise CanaryError(f"cases[{index}].proof_mutation 无效")


def select_case(manifest: Mapping[str, Any], case_id: str) -> dict[str, Any]:
    matches = [
        item
        for item in manifest["cases"]
        if isinstance(item, dict) and item.get("id") == case_id
    ]
    if len(matches) != 1:
        raise CanaryError(f"canary case id 必须唯一存在：{case_id}")
    return matches[0]


def write(root: Path, relative: str, value: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(value, encoding="utf-8")


def checked_output_root(path_value: str) -> Path:
    root = Path(path_value).expanduser().resolve()
    try:
        root.relative_to(REPO_ROOT)
    except ValueError:
        pass
    else:
        raise CanaryError("canary fixture 必须生成到仓库外")
    if root.exists() and any(root.iterdir()):
        raise CanaryError(f"canary 输出目录必须为空：{root}")
    root.mkdir(parents=True, exist_ok=True)
    return root


def run(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    command = list(argv)
    if command and command[0] == "python3":
        command[0] = sys.executable
    env = {
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG") or "C.UTF-8",
        "LC_ALL": os.environ.get("LC_ALL") or "C.UTF-8",
        "PATH": os.defpath,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            command,
            124,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
        )


def git(root: Path, *args: str) -> str:
    result = run(["git", *args], cwd=root)
    if result.returncode != 0:
        raise CanaryError(f"git {' '.join(args)} 失败：{result.stderr}")
    return result.stdout.strip()


def initialize_fixture_repo(root: Path, message: str) -> str:
    git(root, "init", "-q")
    git(root, "config", "user.email", "canary@example.invalid")
    git(root, "config", "user.name", "Agent Behavior Canary")
    git(root, "config", "core.hooksPath", "/dev/null")
    git(root, "config", "commit.gpgSign", "false")
    git(root, "add", ".")
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD")


def materialize_positive_outcome(root: Path) -> dict[str, Any]:
    write(
        root,
        "scene.py",
        "def render_scene():\n"
        "    return {\n"
        "        'black_ratio': 0.0003,\n"
        "        'gradient_levels': 1,\n"
        "        'settlement_visible': False,\n"
        "    }\n",
    )
    bootstrap = (
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
        "from scene import render_scene\n"
    )
    write(
        root,
        "checks/weak.py",
        bootstrap
        + "scene = render_scene()\n"
        + "assert scene['black_ratio'] < 0.05\n",
    )
    write(
        root,
        "checks/strong.py",
        bootstrap
        + "scene = render_scene()\n"
        + "assert scene['settlement_visible'], '聚落中心不可见'\n"
        + "assert scene['gradient_levels'] >= 3, '外围明暗渐变不存在'\n",
    )
    write(
        root,
        "REQUEST.md",
        "用户目标：聚落中心清晰可见，离开聚落后逐级变暗。"
        "已有弱验收只检查黑色占比低于 5%。请规划并验收正向结果，"
        "确保删除照明能力不能通过。\n",
    )
    candidate_head = initialize_fixture_repo(
        root,
        "test(canary): [cr_id_skip] Seed Missing Positive Outcome",
    )
    return {
        "trap": "weak-negative-metric-passes-positive-result-missing",
        "candidate_head": candidate_head,
    }


def _feature_source(index: int) -> str:
    return (
        f"def feature_{index:04d}(value):\n"
        f"    normalized = value + {index}\n"
        "    if normalized < 0:\n"
        "        return 0\n"
        "    return normalized\n\n"
    )


def materialize_large_diff(root: Path) -> dict[str, Any]:
    git(root, "init", "-q")
    git(root, "config", "user.email", "canary@example.invalid")
    git(root, "config", "user.name", "Agent Behavior Canary")
    git(root, "config", "core.hooksPath", "/dev/null")
    git(root, "config", "commit.gpgSign", "false")
    expected = [f"feature_{index:04d}" for index in range(1000)]
    write(root, "legacy.py", "".join(_feature_source(index) for index in range(1000)))
    write(root, "README.md", "# Large refactor fixture\n")
    git(root, "add", ".")
    git(
        root,
        "commit",
        "-qm",
        "test(canary): [cr_id_skip] Seed Large Diff",
    )
    spec_head = git(root, "rev-parse", "HEAD")

    (root / "legacy.py").unlink()
    write(root, "expected_symbols.json", json.dumps(expected, indent=2) + "\n")
    for module_index in range(20):
        start = module_index * 50
        content = ""
        for index in range(start, start + 50):
            if index == 42:
                continue
            content += _feature_source(index)
        write(root, f"modules/part_{module_index:02d}.py", content)
    exports = [item for item in expected if item != "feature_0077"]
    write(root, "exports.py", "EXPORTED = " + repr(exports) + "\n")
    write(
        root,
        "consumer.py",
        "from modules.part_00 import feature_0001 as selected_feature\n\n"
        "def use_feature(value):\n"
        "    return selected_feature(value)\n",
    )
    write(
        root,
        "checks/weak.py",
        "import ast\n"
        "from pathlib import Path\n"
        "root = Path(__file__).resolve().parents[1]\n"
        "for path in root.rglob('*.py'):\n"
        "    ast.parse(path.read_text(encoding='utf-8'), filename=str(path))\n",
    )
    write(
        root,
        "checks/strong.py",
        "import ast\n"
        "import json\n"
        "from pathlib import Path\n"
        "root = Path(__file__).resolve().parents[1]\n"
        "expected = set(json.loads((root / 'expected_symbols.json').read_text()))\n"
        "actual = set()\n"
        "for path in (root / 'modules').glob('*.py'):\n"
        "    tree = ast.parse(path.read_text(encoding='utf-8'))\n"
        "    actual.update(node.name for node in tree.body if isinstance(node, ast.FunctionDef))\n"
        "namespace = {}\n"
        "exec((root / 'exports.py').read_text(encoding='utf-8'), namespace)\n"
        "exports = set(namespace['EXPORTED'])\n"
        "consumer = (root / 'consumer.py').read_text(encoding='utf-8')\n"
        "findings = []\n"
        "if expected - actual:\n"
        "    findings.append({'missing_functions': sorted(expected - actual)})\n"
        "if expected - exports:\n"
        "    findings.append({'missing_exports': sorted(expected - exports)})\n"
        "if 'feature_0002 as selected_feature' not in consumer:\n"
        "    findings.append({'binding': 'consumer binds feature_0001 instead of feature_0002'})\n"
        "print(json.dumps(findings, sort_keys=True))\n"
        "raise SystemExit(1 if findings else 0)\n",
    )
    write(
        root,
        "REQUEST.md",
        "审查从单文件 monolith 到 20 个模块的完整拆分。不要只抽查新文件；"
        "验证函数完整搬运、公开 re-export 和 consumer import binding。\n",
    )
    git(root, "add", ".")
    git(
        root,
        "commit",
        "-qm",
        "test(canary): [cr_id_skip] Split Large Diff",
    )
    candidate_head = git(root, "rev-parse", "HEAD")
    diff_lines = len(git(root, "diff", spec_head, candidate_head).splitlines())
    if diff_lines <= 8000:
        raise CanaryError(f"large-diff fixture 只有 {diff_lines} 行")
    return {
        "spec_head": spec_head,
        "candidate_head": candidate_head,
        "diff_lines": diff_lines,
        "seeded_defects": ["feature_0042", "feature_0077", "consumer-binding"],
    }


def materialize_document_ground_truth(root: Path) -> dict[str, Any]:
    write(
        root,
        "scripts/a.py",
        "def build_report():\n"
        "    return {'per_chapter': [], 'total_unmatch': 0}\n",
    )
    write(
        root,
        "docs/generated.md",
        "# Scripts\n\n"
        "- `scripts/a.py`：生成分析报告。\n"
        "- `scripts/b.py`：预留的卡片生成器。\n\n"
        "输出 `_meta.script_version`、`_meta.timestamp` 和 `_meta.chapter_count`。\n",
    )
    write(
        root,
        "checks/weak.py",
        "from pathlib import Path\n"
        "root = Path(__file__).resolve().parents[1]\n"
        "assert (root / 'docs/generated.md').is_file()\n",
    )
    write(
        root,
        "checks/strong.py",
        "import ast\n"
        "import json\n"
        "from pathlib import Path\n"
        "root = Path(__file__).resolve().parents[1]\n"
        "doc = (root / 'docs/generated.md').read_text(encoding='utf-8')\n"
        "tree = ast.parse((root / 'scripts/a.py').read_text(encoding='utf-8'))\n"
        "keys = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}\n"
        "findings = []\n"
        "if 'scripts/b.py' in doc and not (root / 'scripts/b.py').exists():\n"
        "    findings.append('scripts/b.py does not exist')\n"
        "for field in ('script_version', 'timestamp', 'chapter_count'):\n"
        "    if field in doc and field not in keys:\n"
        "        findings.append(f'{field} is not produced')\n"
        "print(json.dumps(findings, sort_keys=True))\n"
        "raise SystemExit(1 if findings else 0)\n",
    )
    write(
        root,
        "REQUEST.md",
        "核对 docs/generated.md 中每个脚本和数据字段是否来自真实源码；"
        "不得把命名上合理的预留资产当作已实现事实。\n",
    )
    candidate_head = initialize_fixture_repo(
        root,
        "test(canary): [cr_id_skip] Seed Invented Documentation",
    )
    return {
        "trap": "plausible-but-invented-document-assets",
        "candidate_head": candidate_head,
    }


def materialize_feature_content(root: Path) -> dict[str, Any]:
    plan = {
        "objective": "让叙事更丰富",
        "behaviors": [
            {"id": "story-lines", "description": "支持六种故事线状态机"},
            {"id": "emotional-range", "description": "支持多种情感色彩"},
        ],
        "acceptance_cases": [
            {"id": "rules-exist", "description": "规则表非空"},
            {"id": "templates-exist", "description": "模板数量大于零"},
        ],
        "story_templates": ["{a}和{b}发生互动"],
        "content_examples": [],
        "emotional_modes": [],
        "visual_commands": [],
    }
    write(root, "plan.json", json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    write(
        root,
        "checks/weak.py",
        "import json\n"
        "from pathlib import Path\n"
        "plan = json.loads((Path(__file__).resolve().parents[1] / 'plan.json').read_text())\n"
        "for field in ('objective', 'behaviors', 'acceptance_cases', 'story_templates'):\n"
        "    assert plan[field]\n",
    )
    write(
        root,
        "checks/strong.py",
        "import json\n"
        "from pathlib import Path\n"
        "plan = json.loads((Path(__file__).resolve().parents[1] / 'plan.json').read_text())\n"
        "assert len(plan['content_examples']) >= 3, '缺少具体用户可见内容样例'\n"
        "assert len(plan['emotional_modes']) >= 3, '缺少情感维度'\n"
        "assert plan['visual_commands'], '缺少实际表现设计'\n",
    )
    write(
        root,
        "REQUEST.md",
        "用户要求显著丰富玩家看到的叙事内容。请检查 plan.json 是否只是结构完整的架构骨架，"
        "并补足具体内容、表现和情感结果。\n",
    )
    return {"trap": "schema-complete-content-empty-plan"}


def materialize_producer_consumer(root: Path) -> dict[str, Any]:
    status_policy = {
        "visible": ["active", "forged"],
        "hidden": ["archived"],
    }
    write(
        root,
        "contract.json",
        json.dumps(
            {
                "behavior": "display-visible-item-statuses",
                "status_policy": status_policy,
                "observable": "format_world_state(get_world_state(rows))",
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    write(
        root,
        "world.py",
        "from dataclasses import dataclass\n\n"
        "DISPLAYABLE_STATUSES = {'active', 'forged'}\n\n"
        "@dataclass\n"
        "class WorldState:\n"
        "    active_items: list[str]\n\n"
        "def query_items(rows):\n"
        "    return [row['name'] for row in rows if row['status'] in DISPLAYABLE_STATUSES]\n\n"
        "def get_world_state(rows):\n"
        "    return WorldState(active_items=query_items(rows))\n\n"
        "def format_world_state(state):\n"
        "    return ','.join(state.active_items)\n",
    )
    bootstrap = (
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
    )
    write(
        root,
        "checks/weak.py",
        bootstrap
        + "from world import WorldState, format_world_state\n"
        + "state = WorldState(active_items=['sword', 'shield'])\n"
        + "assert format_world_state(state) == 'sword,shield'\n",
    )
    write(
        root,
        "checks/strong.py",
        bootstrap
        + "from world import format_world_state, get_world_state\n"
        + "rows = [\n"
        + "    {'name': 'sword', 'status': 'active'},\n"
        + "    {'name': 'shield', 'status': 'forged'},\n"
        + "    {'name': 'dust', 'status': 'archived'},\n"
        + "]\n"
        + "assert format_world_state(get_world_state(rows)) == 'sword,shield'\n",
    )
    write(
        root,
        "mutations/drop_forged_status.py",
        "from pathlib import Path\n"
        "path = Path(__file__).resolve().parents[1] / 'world.py'\n"
        "source = path.read_text(encoding='utf-8')\n"
        "before = \"DISPLAYABLE_STATUSES = {'active', 'forged'}\"\n"
        "after = \"DISPLAYABLE_STATUSES = {'active'}\"\n"
        "if source.count(before) != 1:\n"
        "    raise SystemExit('expected one displayable-status policy')\n"
        "path.write_text(source.replace(before, after), encoding='utf-8')\n",
    )
    write(
        root,
        "REQUEST.md",
        "冻结业务语义：active 与 forged 物品必须展示，archived 物品不得展示。"
        "为 query_items -> get_world_state -> format_world_state 设计完整链路测试；"
        "直接构造 WorldState 只能算 formatter 单测。proof mutation 必须在 query_items 的"
        "真实过滤路径移除 forged，并由同一完整链路断言失败。\n",
    )
    candidate_head = initialize_fixture_repo(
        root,
        "test(canary): [cr_id_skip] Seed Producer Consumer Contract",
    )
    return {
        "trap": "downstream-fixture-bypasses-upstream-filter",
        "candidate_head": candidate_head,
        "status_policy": status_policy,
        "mutation_target": "world.py:DISPLAYABLE_STATUSES",
    }


MATERIALIZERS: dict[str, Callable[[Path], dict[str, Any]]] = {
    "positive-outcome": materialize_positive_outcome,
    "large-diff": materialize_large_diff,
    "document-ground-truth": materialize_document_ground_truth,
    "feature-content-density": materialize_feature_content,
    "producer-consumer-chain": materialize_producer_consumer,
}


def execute_check(root: Path, spec: Mapping[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    result = run(spec["argv"], cwd=root)
    return {
        "argv": list(spec["argv"]),
        "expected_returncodes": list(spec["expected_returncodes"]),
        "returncode": result.returncode,
        "matched_expectation": result.returncode in spec["expected_returncodes"],
        "duration_ms": int((time.monotonic() - started) * 1000),
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
    }


def fixture_manifest(root: Path) -> dict[str, Any]:
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        files.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return {"files": files, "digest": digest(files)}


def _manifest_map(manifest: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(item["path"]): str(item["sha256"])
        for item in manifest["files"]
        if isinstance(item, dict)
    }


def execute_proof_mutation(
    root: Path,
    mutation_spec: Mapping[str, Any],
    discriminating_spec: Mapping[str, Any],
) -> dict[str, Any]:
    baseline_spec = dict(discriminating_spec)
    baseline_spec["expected_returncodes"] = list(
        mutation_spec["baseline_expected_returncodes"]
    )
    baseline = execute_check(root, baseline_spec)
    if not baseline["matched_expectation"]:
        raise CanaryError(
            "proof mutation baseline 不成立："
            f"returncode={baseline['returncode']}"
        )

    target_paths = sorted(str(item) for item in mutation_spec["target_paths"])
    backups: dict[str, bytes] = {}
    for relative in target_paths:
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise CanaryError(f"proof mutation target 越出 fixture：{relative}") from exc
        if not target.is_file() or target.is_symlink():
            raise CanaryError(f"proof mutation target 必须是普通文件：{relative}")
        backups[relative] = target.read_bytes()

    before = fixture_manifest(root)
    mutation: dict[str, Any] | None = None
    mutated: dict[str, Any] | None = None
    changed_paths: list[str] = []
    mutated_manifest: dict[str, Any] | None = None
    try:
        mutation = execute_check(root, mutation_spec)
        mutated_manifest = fixture_manifest(root)
        before_files = _manifest_map(before)
        after_files = _manifest_map(mutated_manifest)
        changed_paths = sorted(
            path
            for path in set(before_files) | set(after_files)
            if before_files.get(path) != after_files.get(path)
        )
        if not mutation["matched_expectation"]:
            raise CanaryError(
                "proof mutation command 失败："
                f"returncode={mutation['returncode']}"
            )
        if changed_paths != target_paths:
            raise CanaryError(
                "proof mutation changed paths 漂移："
                f"expected={target_paths} actual={changed_paths}"
            )
        mutated = execute_check(root, discriminating_spec)
        if not mutated["matched_expectation"]:
            raise CanaryError(
                "proof mutation 未产生冻结失败："
                f"returncode={mutated['returncode']}"
            )
    finally:
        for relative, content in backups.items():
            (root / relative).write_bytes(content)

    restored = execute_check(root, baseline_spec)
    restored_manifest = fixture_manifest(root)
    tree_restored = restored_manifest["digest"] == before["digest"]
    if not restored["matched_expectation"] or not tree_restored:
        raise CanaryError(
            "proof mutation 恢复失败："
            f"returncode={restored['returncode']} tree_restored={tree_restored}"
        )
    if mutation is None or mutated is None or mutated_manifest is None:
        raise CanaryError("proof mutation 未形成完整观察")
    return {
        "argv": list(mutation_spec["argv"]),
        "target_paths": target_paths,
        "changed_paths": changed_paths,
        "baseline_check": baseline,
        "mutation_command": mutation,
        "mutated_check": mutated,
        "restored_check": restored,
        "fixture_digest_before": before["digest"],
        "fixture_digest_mutated": mutated_manifest["digest"],
        "fixture_digest_restored": restored_manifest["digest"],
        "tree_restored": tree_restored,
    }


def prepare_fixture(case: Mapping[str, Any], output_value: str) -> dict[str, Any]:
    if case["mode"] != "fixture":
        raise CanaryError("operational probe 不能使用 prepare")
    root = checked_output_root(output_value)
    materializer = MATERIALIZERS.get(str(case["fixture_kind"]))
    if materializer is None:
        raise CanaryError(f"未知 fixture_kind：{case['fixture_kind']}")
    facts = materializer(root)
    weak = execute_check(root, case["weak_check"])
    proof_mutation = None
    if case.get("proof_mutation") is not None:
        proof_mutation = execute_proof_mutation(
            root,
            case["proof_mutation"],
            case["discriminating_check"],
        )
        discriminating = proof_mutation["mutated_check"]
    else:
        discriminating = execute_check(root, case["discriminating_check"])
    if not weak["matched_expectation"] or not discriminating["matched_expectation"]:
        raise CanaryError(
            "canary precondition 不成立："
            f"weak={weak['returncode']} discriminating={discriminating['returncode']}"
        )
    manifest = fixture_manifest(root)
    return {
        "schema_version": 1,
        "status": "READY",
        "case_id": case["id"],
        "issue": case["issue"],
        "scenario_id": case["scenario_id"],
        "roles": case["roles"],
        "fixture_root": str(root),
        "fixture_manifest": manifest,
        "facts": facts,
        "weak_check": weak,
        "discriminating_check": discriminating,
        "proof_mutation": proof_mutation,
        "required_observations": case["required_observations"],
        "minimum_fresh_samples": case["minimum_fresh_samples"],
        "result_policy": "real model responses and scores remain outside the repository and runtime ledger",
    }


def _wait_for_file(path: Path, process: subprocess.Popen[str], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        if process.poll() is not None:
            raise CanaryError("background holder exited before acquiring the lock")
        time.sleep(0.01)
    raise CanaryError("background holder did not acquire the lock")


def _reap_process_group(process: subprocess.Popen[str]) -> dict[str, Any]:
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        group_gone = True
    except PermissionError:
        group_gone = False
    else:
        group_gone = False
    return {
        "returncode": process.returncode,
        "reaped": process.poll() is not None,
        "process_group_gone": group_gone,
    }


def probe_lock_contention(case: Mapping[str, Any]) -> dict[str, Any]:
    timeout = float(case["timeout_seconds"])
    holder_seconds = int(case["holder_seconds"])
    holder_code = (
        "import fcntl, os, pathlib, sys, time\n"
        "lock = open(sys.argv[1], 'w')\n"
        "fcntl.flock(lock, fcntl.LOCK_EX)\n"
        "pathlib.Path(sys.argv[2]).write_text(str(os.getpid()))\n"
        "time.sleep(float(sys.argv[3]))\n"
    )
    waiter_code = (
        "import fcntl, sys\n"
        "lock = open(sys.argv[1], 'w')\n"
        "fcntl.flock(lock, fcntl.LOCK_EX)\n"
    )
    with tempfile.TemporaryDirectory(prefix="builder-loop-contention-canary-") as raw:
        root = Path(raw)
        lock_path = root / "shared.lock"
        ready_path = root / "holder.ready"
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                holder_code,
                str(lock_path),
                str(ready_path),
                str(holder_seconds),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            _wait_for_file(ready_path, holder, timeout=2)
            started = time.monotonic()
            timed_out = False
            waiter_returncode: int | None = None
            try:
                waiter = subprocess.run(
                    [sys.executable, "-c", waiter_code, str(lock_path)],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                    check=False,
                )
                waiter_returncode = waiter.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
            elapsed_ms = int((time.monotonic() - started) * 1000)
            holder_alive_at_timeout = holder.poll() is None
        finally:
            cleanup = _reap_process_group(holder)
    reproduced = (
        timed_out
        and holder_alive_at_timeout
        and cleanup["reaped"]
        and cleanup["process_group_gone"]
    )
    return {
        "schema_version": 1,
        "status": "REPRODUCED" if reproduced else "NOT_REPRODUCED",
        "case_id": case["id"],
        "issue": case["issue"],
        "probe_kind": case["probe_kind"],
        "foreground_timed_out": timed_out,
        "foreground_returncode": waiter_returncode,
        "elapsed_ms": elapsed_ms,
        "holder_pid": holder.pid,
        "holder_pgid": holder.pid,
        "holder_alive_at_timeout": holder_alive_at_timeout,
        "holder_cleanup": cleanup,
        "confirmed_boundary": "a pre-existing host process can deterministically block a later verification until timeout",
        "unproven_boundaries": [
            "the holder was created by Codex or another Agent tool turn",
            "a current project import reproduces the same lock or resource dependency",
            "Builder-loop can attribute the timeout to that process without additional provenance",
        ],
        "required_observations": case["required_observations"],
    }


def list_cases(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "cases": [
            {
                "id": item["id"],
                "issue": item["issue"],
                "mode": item["mode"],
                "scenario_id": item.get("scenario_id"),
            }
            for item in manifest["cases"]
        ],
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="准备角色行为 canary fixture 或运行安全宿主探针")
    root.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--case-id", required=True)
    prepare.add_argument("--output", required=True)
    probe = commands.add_parser("probe")
    probe.add_argument("--case-id", required=True)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        manifest = read_manifest(args.manifest)
        if args.command == "list":
            result = list_cases(manifest)
        else:
            case = select_case(manifest, args.case_id)
            if args.command == "prepare":
                result = prepare_fixture(case, args.output)
            else:
                if case["mode"] != "operational_probe":
                    raise CanaryError("fixture case 不能使用 probe")
                result = probe_lock_contention(case)
    except CanaryError as exc:
        print(
            json.dumps(
                {"schema_version": 1, "status": "ERROR", "message": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
