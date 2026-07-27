#!/usr/bin/env python3
"""Run a read-only shadow triage for an existing GitHub Issue."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import dataclasses
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit


SCRIPT_DIR = Path(__file__).resolve().parent
EXPERIMENT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import issue_triage_eval as evaluator  # noqa: E402


meta = evaluator.meta
SHADOW_SCHEMA_VERSION = 3
CAPTURE_SCHEMA_VERSION = 1
RESOLUTION_SCHEMA_VERSION = 1
PROFILES_PATH = EXPERIMENT_DIR / "profiles" / "projects.json"
DEFAULT_RUN_ROOT = Path.home() / ".codex" / "issue-triage" / "runs"
MAX_ISSUE_BODY_CHARS = 100_000
MAX_COMMENT_CHARS = 4_000
MAX_COMMENTS = 8
MAX_FETCHED_COMMENTS = 100
MAX_EVIDENCE_FACTS = 20
MAX_FACT_CHARS = 18_000
MAX_FILE_BYTES = 2_000_000
MAX_FILE_REFS = 8
MAX_IDENTIFIER_SEARCHES = 8
SECRET_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{16,}"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s]+"),
)
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
BACKTICK = re.compile(r"`([^`\n]{1,240})`")
PATH_TOKEN = re.compile(r"(?<![A-Za-z0-9_.-])((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+(?::[0-9]+)?)")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{2,100}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
CAPTURE_MARKER = f"issue-capture:v{CAPTURE_SCHEMA_VERSION}"
RESOLUTION_MARKER = f"issue-resolution:v{RESOLUTION_SCHEMA_VERSION}"
ROOT_CAUSE_STATUSES = {"unknown", "candidate", "confirmed"}
RESOLUTION_OUTCOMES = {"fixed", "duplicate", "not-a-bug", "cannot-reproduce", "wontfix"}
HUMAN_DECISION_KINDS = {
    "scope_approval",
    "goal_or_principle",
    "root_cause_correction",
    "tradeoff",
}


@dataclasses.dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def prompt_hashes() -> dict[str, str]:
    return {
        "diagnostician": hashlib.sha256(evaluator.DIAGNOSTICIAN_PROMPT.read_bytes()).hexdigest(),
        "attacker": hashlib.sha256(evaluator.ATTACKER_PROMPT.read_bytes()).hexdigest(),
    }


def _utc_timestamp(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise meta.RunnerError("input", f"{name} 必须是 UTC ISO-8601 字符串", meta.EXIT_INPUT)
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        raise meta.RunnerError("input", f"{name} 不是有效 ISO-8601 时间", meta.EXIT_INPUT) from None
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise meta.RunnerError("input", f"{name} 必须使用 UTC 时区", meta.EXIT_INPUT)
    return value


def _exact_object(value: Any, *, name: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise meta.RunnerError("input", f"{name} 字段集合非法", meta.EXIT_INPUT)
    return value


def _nonempty_string(value: Any, *, name: str, limit: int = 20_000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise meta.RunnerError("input", f"{name} 必须是非空字符串", meta.EXIT_INPUT)
    return value


def _string_list(value: Any, *, name: str, max_items: int = 100) -> list[str]:
    if not isinstance(value, list) or len(value) > max_items:
        raise meta.RunnerError("input", f"{name} 必须是字符串数组", meta.EXIT_INPUT)
    rows: list[str] = []
    for index, item in enumerate(value):
        rows.append(_nonempty_string(item, name=f"{name}[{index}]"))
    return rows


def parse_marker_json(text: str, marker: str) -> dict[str, Any] | None:
    """Parse one exact fenced JSON marker; reject duplicates and malformed markers."""

    opening = f"<!-- {marker} -->"
    closing = f"<!-- /{marker} -->"
    opening_count = text.count(opening)
    closing_count = text.count(closing)
    if opening_count == 0 and closing_count == 0:
        return None
    if opening_count != 1 or closing_count != 1:
        raise meta.RunnerError("input", f"{marker} marker 数量非法", meta.EXIT_INPUT)
    start = text.index(opening) + len(opening)
    end = text.find(closing, start)
    if end < 0:
        raise meta.RunnerError("input", f"{marker} marker 顺序非法", meta.EXIT_INPUT)
    if text.find(opening, start) != -1 or text.find(closing, end + len(closing)) != -1:
        raise meta.RunnerError("input", f"{marker} marker 重复", meta.EXIT_INPUT)
    payload = text[start:end].strip()
    match = re.fullmatch(r"```json\s*\n(.*?)\n```", payload, flags=re.DOTALL)
    if match is None:
        raise meta.RunnerError("input", f"{marker} 必须包含唯一 JSON fenced block", meta.EXIT_INPUT)
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        raise meta.RunnerError("input", f"{marker} 包含非法 JSON", meta.EXIT_INPUT) from None
    if not isinstance(value, dict):
        raise meta.RunnerError("input", f"{marker} JSON 必须是 object", meta.EXIT_INPUT)
    return value


def validate_capture(value: Any, *, expected_repository: str | None = None) -> dict[str, Any]:
    capture = _exact_object(
        value,
        name=CAPTURE_MARKER,
        keys={"captured_at", "repository", "incident_head", "branch", "dirty", "root_cause_status"},
    )
    _utc_timestamp(capture["captured_at"], name="capture.captured_at")
    repository = _nonempty_string(capture["repository"], name="capture.repository", limit=200)
    if expected_repository is not None and repository != expected_repository:
        raise meta.RunnerError("input", "capture.repository 与目标仓库不一致", meta.EXIT_INPUT)
    incident_head = capture["incident_head"]
    if incident_head != "unavailable" and (not isinstance(incident_head, str) or not COMMIT.fullmatch(incident_head)):
        raise meta.RunnerError("input", "capture.incident_head 必须是 40 位 commit 或 unavailable", meta.EXIT_INPUT)
    _nonempty_string(capture["branch"], name="capture.branch", limit=300)
    if not isinstance(capture["dirty"], bool):
        raise meta.RunnerError("input", "capture.dirty 必须是 boolean", meta.EXIT_INPUT)
    if capture["root_cause_status"] not in ROOT_CAUSE_STATUSES:
        raise meta.RunnerError("input", "capture.root_cause_status 非法", meta.EXIT_INPUT)
    return capture


def parse_capture(text: str, *, expected_repository: str | None = None) -> dict[str, Any] | None:
    value = parse_marker_json(text, CAPTURE_MARKER)
    return None if value is None else validate_capture(value, expected_repository=expected_repository)


def validate_resolution(value: Any, *, capture: dict[str, Any] | None = None) -> dict[str, Any]:
    resolution = _exact_object(
        value,
        name=RESOLUTION_MARKER,
        keys={
            "resolved_at",
            "outcome",
            "incident_head",
            "resolved_head",
            "fix_commits",
            "root_cause_status",
            "root_cause",
            "violated_invariant",
            "human_decision",
            "acceptance",
            "residual_uncertainty",
        },
    )
    _utc_timestamp(resolution["resolved_at"], name="resolution.resolved_at")
    if resolution["outcome"] not in RESOLUTION_OUTCOMES:
        raise meta.RunnerError("input", "resolution.outcome 非法", meta.EXIT_INPUT)
    for field in ("incident_head", "resolved_head"):
        head = resolution[field]
        if head != "unavailable" and (not isinstance(head, str) or not COMMIT.fullmatch(head)):
            raise meta.RunnerError("input", f"resolution.{field} 非法", meta.EXIT_INPUT)
    if capture is not None and resolution["incident_head"] != capture["incident_head"]:
        raise meta.RunnerError("input", "resolution.incident_head 与 capture 不一致", meta.EXIT_INPUT)
    commits = _string_list(resolution["fix_commits"], name="resolution.fix_commits")
    if any(not COMMIT.fullmatch(commit) for commit in commits):
        raise meta.RunnerError("input", "resolution.fix_commits 必须是 40 位 commit", meta.EXIT_INPUT)
    if resolution["root_cause_status"] not in ROOT_CAUSE_STATUSES:
        raise meta.RunnerError("input", "resolution.root_cause_status 非法", meta.EXIT_INPUT)
    _nonempty_string(resolution["root_cause"], name="resolution.root_cause")
    _nonempty_string(resolution["violated_invariant"], name="resolution.violated_invariant")
    decision = _exact_object(
        resolution["human_decision"],
        name="resolution.human_decision",
        keys={"required", "kinds", "evidence"},
    )
    if not isinstance(decision["required"], bool):
        raise meta.RunnerError("input", "resolution.human_decision.required 必须是 boolean", meta.EXIT_INPUT)
    kinds = _string_list(decision["kinds"], name="resolution.human_decision.kinds")
    if len(kinds) != len(set(kinds)) or set(kinds) - HUMAN_DECISION_KINDS:
        raise meta.RunnerError("input", "resolution.human_decision.kinds 非法", meta.EXIT_INPUT)
    if decision["required"] != bool(kinds):
        raise meta.RunnerError("input", "resolution.human_decision.required 与 kinds 不一致", meta.EXIT_INPUT)
    _string_list(decision["evidence"], name="resolution.human_decision.evidence")
    acceptance = _exact_object(
        resolution["acceptance"],
        name="resolution.acceptance",
        keys={"deterministic", "evidence"},
    )
    if not isinstance(acceptance["deterministic"], bool):
        raise meta.RunnerError("input", "resolution.acceptance.deterministic 必须是 boolean", meta.EXIT_INPUT)
    _string_list(acceptance["evidence"], name="resolution.acceptance.evidence")
    _string_list(resolution["residual_uncertainty"], name="resolution.residual_uncertainty")
    return resolution


def parse_latest_resolution(
    comments: list[dict[str, Any]],
    *,
    capture: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    resolutions: list[dict[str, Any]] = []
    for comment in comments:
        body = str(comment.get("body") or "") if isinstance(comment, dict) else ""
        value = parse_marker_json(body, RESOLUTION_MARKER)
        if value is not None:
            resolutions.append(validate_resolution(value, capture=capture))
    return resolutions[-1] if resolutions else None


def contract_digest(value: dict[str, Any]) -> str:
    return _sha256_text(_canonical_json(value))


def redact_secrets(value: str) -> str:
    redacted = value
    for pattern in SECRET_PATTERNS:
        if pattern.groups:
            redacted = pattern.sub(r"\1[REDACTED_SECRET]", redacted)
        else:
            redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    redacted = re.sub(
        r"(https?://)([^/@\s]+)@",
        r"\1[REDACTED_USERINFO]@",
        redacted,
    )
    return redacted


def run_command(
    args: list[str],
    *,
    cwd: Path,
    timeout: int = 30,
    max_chars: int = 100_000,
) -> CommandResult:
    try:
        completed = subprocess.run(
            args,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return CommandResult(124, "", "command unavailable or timed out")
    return CommandResult(
        completed.returncode,
        redact_secrets(completed.stdout[:max_chars]),
        redact_secrets(completed.stderr[:max_chars]),
    )


def sanitize_remote(remote: str) -> str:
    value = remote.strip()
    if value.startswith(("http://", "https://")):
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if parsed.port is not None:
            host += f":{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))
    return redact_secrets(value)


def github_repo_from_remote(remote: str) -> str | None:
    sanitized = sanitize_remote(remote)
    match = re.search(r"github\.com(?::|/)([^/\s:]+)/([^/\s]+?)(?:\.git)?\Z", sanitized)
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def collect_repo_identity(repo: Path) -> dict[str, Any]:
    repo = repo.resolve()
    head = run_command(["git", "rev-parse", "HEAD"], cwd=repo)
    branch = run_command(["git", "branch", "--show-current"], cwd=repo)
    remote = run_command(["git", "remote", "get-url", "origin"], cwd=repo)
    status = run_command(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=repo)
    if head.returncode != 0:
        raise meta.RunnerError("input", f"不是有效 Git 仓库: {repo}", meta.EXIT_INPUT)
    status_lines = [line for line in status.stdout.splitlines() if line]
    sanitized_remote = sanitize_remote(remote.stdout) if remote.returncode == 0 else ""
    return {
        "repo_path": str(repo),
        "head": head.stdout.strip(),
        "branch": branch.stdout.strip() if branch.returncode == 0 else "",
        "remote": sanitized_remote,
        "github_repo": github_repo_from_remote(sanitized_remote),
        "dirty": bool(status_lines),
        "dirty_entry_count": len(status_lines),
        "dirty_entries_preview": status_lines[:80],
        "dirty_status_sha256": _sha256_text(status.stdout),
    }


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise meta.RunnerError("input", f"无法读取有效 JSON: {path}", meta.EXIT_INPUT) from None


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _load_profiles(path: Path = PROFILES_PATH) -> dict[str, Any]:
    raw = _read_json(path)
    if not isinstance(raw, dict) or raw.get("schema_version") != 1 or not isinstance(raw.get("profiles"), dict):
        raise meta.RunnerError("input", "project profiles 结构非法", meta.EXIT_INPUT)
    return raw["profiles"]


def _markdown_sections(text: str) -> list[dict[str, Any]]:
    lines = text.splitlines()
    headings: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        match = HEADING.match(line)
        if match:
            headings.append((index, len(match.group(1)), match.group(2).strip()))
    sections: list[dict[str, Any]] = []
    for heading_index, (line_index, level, title) in enumerate(headings):
        end = len(lines)
        for next_line, next_level, _ in headings[heading_index + 1 :]:
            if next_level <= level:
                end = next_line
                break
        sections.append(
            {
                "title": title,
                "line": line_index + 1,
                "text": "\n".join(lines[line_index:end]).strip(),
            }
        )
    return sections


def load_profile(repo: Path, github_repo: str, profiles_path: Path = PROFILES_PATH) -> tuple[str, list[evaluator.Principle]]:
    profiles = _load_profiles(profiles_path)
    profile = profiles.get(github_repo)
    if not isinstance(profile, dict):
        raise meta.RunnerError("input", f"缺少项目 profile: {github_repo}", meta.EXIT_INPUT)
    goal = profile.get("goal")
    sources = profile.get("principle_sources")
    if not isinstance(goal, str) or not goal.strip() or not isinstance(sources, list):
        raise meta.RunnerError("input", f"项目 profile 非法: {github_repo}", meta.EXIT_INPUT)
    principles: list[evaluator.Principle] = []
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"path", "heading_patterns"}:
            raise meta.RunnerError("input", f"profile principle source 非法: {github_repo}", meta.EXIT_INPUT)
        relative = Path(str(source["path"]))
        path = (repo / relative).resolve()
        if repo.resolve() not in path.parents or not path.is_file():
            raise meta.RunnerError("input", f"原则源不存在或越界: {relative}", meta.EXIT_INPUT)
        patterns = [re.compile(str(pattern)) for pattern in source["heading_patterns"]]
        for section in _markdown_sections(path.read_text(encoding="utf-8")):
            if not any(pattern.search(section["title"]) for pattern in patterns):
                continue
            principle_id = f"P{len(principles) + 1}"
            text = f"[{relative}:{section['line']}]\n{section['text']}"
            principles.append(evaluator.Principle(principle_id, text[:12_000]))
    if not principles:
        raise meta.RunnerError("input", f"项目 profile 未提取到原则: {github_repo}", meta.EXIT_INPUT)
    if len(principles) > evaluator.MAX_PRINCIPLES_PER_PROJECT:
        raise meta.RunnerError("input", f"项目原则超过 {evaluator.MAX_PRINCIPLES_PER_PROJECT} 条", meta.EXIT_INPUT)
    return goal, principles


def fetch_issue(repo: Path, github_repo: str, issue_number: int, *, gh_bin: str = "gh") -> dict[str, Any]:
    fields = "number,title,body,state,labels,comments,createdAt,updatedAt,url,author"
    result = run_command(
        [gh_bin, "issue", "view", str(issue_number), "--repo", github_repo, "--json", fields],
        cwd=repo,
        timeout=45,
        max_chars=300_000,
    )
    if result.returncode != 0:
        raise meta.RunnerError("transport", f"无法读取 GitHub Issue #{issue_number}", meta.EXIT_TRANSPORT)
    try:
        issue = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise meta.RunnerError("response", f"GitHub Issue #{issue_number} 返回非法 JSON", meta.EXIT_RESPONSE) from None
    if not isinstance(issue, dict):
        raise meta.RunnerError("response", f"GitHub Issue #{issue_number} 结构非法", meta.EXIT_RESPONSE)
    issue["body"] = str(issue.get("body") or "")[:MAX_ISSUE_BODY_CHARS]
    comments = issue.get("comments")
    if isinstance(comments, list):
        issue["comments"] = [
            {
                "author": ((comment.get("author") or {}).get("login") if isinstance(comment, dict) else None),
                "createdAt": comment.get("createdAt") if isinstance(comment, dict) else None,
                "body": str(comment.get("body") or "")[:MAX_COMMENT_CHARS] if isinstance(comment, dict) else "",
            }
            for comment in comments[-MAX_FETCHED_COMMENTS:]
        ]
    else:
        issue["comments"] = []
    return issue


def _safe_file_excerpt(repo: Path, reference: str) -> dict[str, Any] | None:
    match = re.fullmatch(r"(.+?)(?::([0-9]+))?", reference.strip())
    if not match:
        return None
    relative = Path(match.group(1))
    if relative.is_absolute() or ".." in relative.parts:
        return None
    path = (repo / relative).resolve()
    root = repo.resolve()
    if root not in path.parents or not path.is_file() or path.is_symlink():
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) > MAX_FILE_BYTES:
        return {
            "path": str(relative),
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "excerpt": "[file too large for shadow excerpt]",
        }
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if match.group(2):
        center = max(1, int(match.group(2)))
        start = max(1, center - 50)
        end = min(len(lines), center + 50)
    else:
        start = 1
        end = min(len(lines), 220)
    numbered = "\n".join(f"{index}: {lines[index - 1]}" for index in range(start, end + 1))
    return {
        "path": str(relative),
        "line_start": start,
        "line_end": end,
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "excerpt": redact_secrets(numbered[:MAX_FACT_CHARS]),
    }


def collect_evidence(repo: Path, issue: dict[str, Any], repo_identity: dict[str, Any]) -> dict[str, Any]:
    body = str(issue.get("body") or "")
    candidates: list[str] = []
    for token in BACKTICK.findall(body):
        cleaned = token.strip().split()[0] if token.strip() else ""
        if "/" in cleaned or (repo / cleaned.split(":", 1)[0]).is_file():
            candidates.append(cleaned)
    candidates.extend(PATH_TOKEN.findall(body))
    file_refs: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for candidate in candidates:
        normalized = candidate.strip(".,;()[]{}")
        if normalized in seen_paths:
            continue
        excerpt = _safe_file_excerpt(repo, normalized)
        if excerpt is None:
            continue
        seen_paths.add(normalized)
        file_refs.append(excerpt)
        if len(file_refs) >= MAX_FILE_REFS:
            break

    identifiers: list[str] = []
    for token in BACKTICK.findall(body):
        cleaned = token.strip()
        if "/" not in cleaned and IDENTIFIER.fullmatch(cleaned) and cleaned not in identifiers:
            identifiers.append(cleaned)
        if len(identifiers) >= MAX_IDENTIFIER_SEARCHES:
            break
    searches: list[dict[str, Any]] = []
    for identifier in identifiers:
        result = run_command(
            ["git", "grep", "-n", "-F", "--", identifier],
            cwd=repo,
            timeout=15,
            max_chars=20_000,
        )
        hits = result.stdout.splitlines()[:40] if result.returncode in {0, 1} else []
        searches.append({"identifier": identifier, "hits": hits})
    return {
        "repo_identity": repo_identity,
        "file_refs": file_refs,
        "identifier_searches": searches,
    }


def evidence_facts(issue: dict[str, Any], evidence: dict[str, Any]) -> list[str]:
    facts = [
        f"GitHub Issue 标题：{issue.get('title', '')}",
        "GitHub Issue 正文：\n" + str(issue.get("body") or "")[:MAX_FACT_CHARS],
        "仓库运行身份：\n" + _canonical_json(evidence["repo_identity"])[:MAX_FACT_CHARS],
    ]
    comments = issue.get("comments") or []
    if comments:
        facts.append("Issue 最近评论：\n" + _canonical_json(comments[-MAX_COMMENTS:])[:MAX_FACT_CHARS])
    for reference in evidence["file_refs"]:
        facts.append(
            f"显式引用文件 {reference['path']}，sha256={reference['sha256']}，"
            f"lines={reference.get('line_start')}-{reference.get('line_end')}：\n{reference['excerpt']}"
        )
    for search in evidence["identifier_searches"]:
        if search["hits"]:
            facts.append(
                f"标识符 {search['identifier']} 的 tracked 源码命中：\n"
                + "\n".join(search["hits"])[:MAX_FACT_CHARS]
            )
    return [redact_secrets(fact[:MAX_FACT_CHARS]) for fact in facts[:MAX_EVIDENCE_FACTS]]


def _request_stage(
    path: Path,
    *,
    request: Callable[[], meta.ApiResult],
    validator: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    if path.exists():
        cached = _read_json(path)
        if not isinstance(cached, dict) or "value" not in cached:
            raise meta.RunnerError("response", f"阶段缓存非法: {path}", meta.EXIT_RESPONSE)
        validator(cached["value"])
        return cached
    result = request()
    payload = {"value": result.value, "request_sha256": result.request_hash}
    _atomic_write_json(path, payload)
    return payload


def _single_pass(
    *,
    client: meta.ResponsesClient,
    project: evaluator.Project,
    effort: str,
    run_dir: Path,
    prefix: str,
) -> dict[str, Any]:
    project_data = evaluator._project_prompt_data(project)
    diagnosis_path = run_dir / f"{prefix}-diagnosis.json"
    diagnosis = _request_stage(
        diagnosis_path,
        request=lambda: client.request(
            developer_prompt=evaluator.DIAGNOSTICIAN_PROMPT.read_text(encoding="utf-8"),
            task_data=project_data,
            schema_name="issue_triage_shadow_diagnosis",
            schema=evaluator._diagnosis_schema(project),
            validator=lambda value: evaluator.validate_diagnosis(value, project),
            reasoning_effort=effort,
            max_output_tokens=evaluator.MAX_OUTPUT_TOKENS,
        ),
        validator=lambda value: evaluator.validate_diagnosis(value, project),
    )
    attack_path = run_dir / f"{prefix}-attack.json"
    attack_data = {**project_data, "diagnosis": diagnosis["value"]}
    attack = _request_stage(
        attack_path,
        request=lambda: client.request(
            developer_prompt=evaluator.ATTACKER_PROMPT.read_text(encoding="utf-8"),
            task_data=attack_data,
            schema_name="issue_triage_shadow_attack",
            schema=evaluator._attack_schema(project),
            validator=lambda value: evaluator.validate_attacks(value, project),
            reasoning_effort=effort,
            max_output_tokens=evaluator.MAX_OUTPUT_TOKENS,
        ),
        validator=lambda value: evaluator.validate_attacks(value, project),
    )
    assessment = diagnosis["value"]["issue_assessments"][0]
    attack_row = attack["value"]["attacks"][0]
    axes = evaluator.final_axes(assessment, attack_row)
    return {
        "effort": effort,
        "assessment": assessment,
        "attack": attack_row,
        "axes": axes,
        "work_queue": evaluator.work_queue(axes),
        "request_sha256": [diagnosis["request_sha256"], attack["request_sha256"]],
    }


def _is_boundary(pass_result: dict[str, Any]) -> bool:
    if pass_result["axes"]["human_attention"] == "none":
        return False
    assessment = pass_result["assessment"]
    return evaluator.base_axes(assessment)["human_attention"] == "none"


def prediction_idempotency_key(
    *,
    github_repo: str,
    issue_number: int,
    capture: dict[str, Any],
) -> str:
    material = {
        "github_repo": github_repo,
        "issue_number": issue_number,
        "incident_head": capture["incident_head"],
        "capture_sha256": contract_digest(capture),
        "capture_schema_version": CAPTURE_SCHEMA_VERSION,
        "shadow_schema_version": SHADOW_SCHEMA_VERSION,
        "prompt_sha256": prompt_hashes(),
    }
    return _sha256_text(_canonical_json(material))


def _commit_exists(repo: Path, head: str) -> bool:
    result = run_command(["git", "cat-file", "-e", f"{head}^{{commit}}"], cwd=repo, timeout=15)
    return result.returncode == 0


def ensure_incident_commit(repo: Path, head: str) -> None:
    if not COMMIT.fullmatch(head):
        raise meta.RunnerError("input", "事故 commit 不可用，禁止回退当前 HEAD", meta.EXIT_INPUT)
    if _commit_exists(repo, head):
        return
    fetched = run_command(
        ["git", "fetch", "--no-tags", "origin", head],
        cwd=repo,
        timeout=120,
        max_chars=20_000,
    )
    if fetched.returncode != 0 or not _commit_exists(repo, head):
        raise meta.RunnerError("transport", "无法获取精确事故 commit，禁止回退当前 HEAD", meta.EXIT_TRANSPORT)


@contextmanager
def incident_worktree(repo: Path, head: str, *, work_root: Path | None = None):
    repo = repo.resolve()
    ensure_incident_commit(repo, head)
    if work_root is not None:
        work_root.expanduser().mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            prefix="issue-triage-incident-",
            dir=str(work_root.expanduser()) if work_root is not None else None,
        )
    )
    checkout = temporary / "checkout"
    added = run_command(
        ["git", "worktree", "add", "--detach", str(checkout), head],
        cwd=repo,
        timeout=120,
        max_chars=20_000,
    )
    if added.returncode != 0:
        shutil.rmtree(temporary, ignore_errors=True)
        raise meta.RunnerError("input", "无法创建事故 commit 隔离 checkout", meta.EXIT_INPUT)
    try:
        actual = run_command(["git", "rev-parse", "HEAD"], cwd=checkout, timeout=15)
        if actual.returncode != 0 or actual.stdout.strip() != head:
            raise meta.RunnerError("input", "事故 checkout 未绑定精确 commit", meta.EXIT_INPUT)
        yield checkout
    finally:
        run_command(["git", "worktree", "remove", "--force", str(checkout)], cwd=repo, timeout=60)
        shutil.rmtree(temporary, ignore_errors=True)


def _shadow_project(
    *,
    github_repo: str,
    issue: dict[str, Any],
    facts: list[str],
    goal: str,
    principles: list[evaluator.Principle],
) -> evaluator.Project:
    issue_number = int(issue["number"])
    return evaluator.Project(
        project_id=github_repo,
        goal=goal,
        principles=tuple(principles),
        cases=(
            evaluator.Case(
                id=f"issue-{issue_number}",
                source_url=str(issue.get("url") or ""),
                title=str(issue.get("title") or ""),
                facts=tuple(facts),
                gold=evaluator.Gold(
                    diagnosis_state="needs_evidence",
                    human_attention="first_principles",
                    scope_inventory_required=False,
                    cluster_id="shadow-single",
                    principle_ids=(principles[0].id,),
                ),
            ),
        ),
    )


def _run_prepared_shadow(
    *,
    github_repo: str,
    issue: dict[str, Any],
    evidence: dict[str, Any],
    goal: str,
    principles: list[evaluator.Principle],
    run_root: Path,
    main_effort: str,
    boundary_effort: str | None,
    client: meta.ResponsesClient | None = None,
    extra_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    issue_number = int(issue["number"])
    facts = evidence_facts(issue, evidence)
    project = _shadow_project(
        github_repo=github_repo,
        issue=issue,
        facts=facts,
        goal=goal,
        principles=principles,
    )
    input_payload = {
        "schema_version": SHADOW_SCHEMA_VERSION,
        "github_repo": github_repo,
        "issue": issue,
        "evidence": evidence,
        "goal": goal,
        "principles": [dataclasses.asdict(principle) for principle in principles],
        "main_effort": main_effort,
        "boundary_effort": boundary_effort,
        "prompt_sha256": prompt_hashes(),
    }
    if extra_input:
        input_payload.update(extra_input)
    digest = _sha256_text(_canonical_json(input_payload))
    run_dir = run_root.expanduser() / github_repo.replace("/", "-") / f"issue-{issue_number}-{digest[:16]}"
    _atomic_write_json(run_dir / "input.json", input_payload)
    responses = client or meta.ResponsesClient(meta.load_runtime_config())
    high = _single_pass(client=responses, project=project, effort=main_effort, run_dir=run_dir, prefix="main")
    boundary: dict[str, Any] | None = None
    if boundary_effort is not None and _is_boundary(high):
        boundary = _single_pass(
            client=responses,
            project=project,
            effort=boundary_effort,
            run_dir=run_dir,
            prefix="boundary",
        )
    recommended = boundary if boundary is not None else high
    result = {
        "schema_version": SHADOW_SCHEMA_VERSION,
        "shadow_only": True,
        "mutations": [],
        "github_repo": github_repo,
        "issue_number": issue_number,
        "issue_url": issue.get("url"),
        "run_dir": str(run_dir),
        "input_sha256": digest,
        "main": high,
        "boundary": boundary,
        "recommended_axes": recommended["axes"],
        "recommended_work_queue": recommended["work_queue"],
        "notice": "只读影子结果；未修改 Issue、标签、Planner 或代码仓库",
    }
    _atomic_write_json(run_dir / "result.json", result)
    return result


def run_shadow(
    *,
    repo: Path,
    issue_number: int,
    run_root: Path,
    main_effort: str,
    boundary_effort: str | None,
    profiles_path: Path = PROFILES_PATH,
    gh_bin: str = "gh",
) -> dict[str, Any]:
    repo_identity = collect_repo_identity(repo)
    github_repo = repo_identity.get("github_repo")
    if not github_repo:
        raise meta.RunnerError("input", "origin 不是可识别的 GitHub repository", meta.EXIT_INPUT)
    goal, principles = load_profile(repo.resolve(), github_repo, profiles_path)
    issue = fetch_issue(repo.resolve(), github_repo, issue_number, gh_bin=gh_bin)
    evidence = collect_evidence(repo.resolve(), issue, repo_identity)
    return _run_prepared_shadow(
        github_repo=github_repo,
        issue=issue,
        evidence=evidence,
        goal=goal,
        principles=principles,
        run_root=run_root,
        main_effort=main_effort,
        boundary_effort=boundary_effort,
    )


def run_shadow_from_issue(
    *,
    repo: Path,
    github_repo: str,
    issue: dict[str, Any],
    capture: dict[str, Any],
    run_root: Path,
    main_effort: str,
    boundary_effort: str | None,
    profiles_path: Path = PROFILES_PATH,
    work_root: Path | None = None,
    client: meta.ResponsesClient | None = None,
) -> dict[str, Any]:
    """Predict from immutable creation input and the exact incident commit."""

    capture = validate_capture(capture, expected_repository=github_repo)
    issue_number = issue.get("number")
    if not isinstance(issue_number, int) or issue_number <= 0:
        raise meta.RunnerError("input", "Issue number 非法", meta.EXIT_INPUT)
    stable_issue = {
        "number": issue_number,
        "title": str(issue.get("title") or ""),
        "body": str(issue.get("body") or "")[:MAX_ISSUE_BODY_CHARS],
        "createdAt": issue.get("createdAt"),
        "url": issue.get("url"),
        "author": issue.get("author"),
        "comments": [],
    }
    run_key = prediction_idempotency_key(
        github_repo=github_repo,
        issue_number=issue_number,
        capture=capture,
    )
    incident_head = capture["incident_head"]
    if incident_head == "unavailable":
        raise meta.RunnerError("input", "capture 未提供事故 commit，禁止回退当前 HEAD", meta.EXIT_INPUT)
    with incident_worktree(repo, incident_head, work_root=work_root) as checkout:
        goal, principles = load_profile(checkout, github_repo, profiles_path)
        checkout_identity = collect_repo_identity(checkout)
        incident_identity = {
            **checkout_identity,
            "repo_path": f"{github_repo}@{incident_head}",
            "branch": capture["branch"],
            "dirty": capture["dirty"],
            "captured_at": capture["captured_at"],
            "evidence_checkout": "incident_head",
        }
        if capture["dirty"]:
            incident_identity.update(
                {
                    "dirty_entry_count": None,
                    "dirty_entries_preview": [],
                    "dirty_status_sha256": None,
                    "evidence_mode": "issue-body-and-incident-principles-only",
                    "evidence_limitation": "未提交 dirty 现场不可重建；禁止读取文件摘录和标识符命中",
                }
            )
            evidence = {
                "repo_identity": incident_identity,
                "file_refs": [],
                "identifier_searches": [],
            }
        else:
            evidence = collect_evidence(checkout, stable_issue, incident_identity)
            evidence["repo_identity"]["evidence_mode"] = "clean-incident-checkout"
        return _run_prepared_shadow(
            github_repo=github_repo,
            issue=stable_issue,
            evidence=evidence,
            goal=goal,
            principles=principles,
            run_root=run_root,
            main_effort=main_effort,
            boundary_effort=boundary_effort,
            client=client,
            extra_input={
                "capture": capture,
                "capture_sha256": contract_digest(capture),
                "prediction_idempotency_key": run_key,
                "historical_input_boundary": "creation body plus exact incident commit; comments and resolution excluded",
            },
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    shadow = subparsers.add_parser("shadow", help="只读抓取 Issue 并生成影子分流结果")
    shadow.add_argument("--repo", type=Path, required=True)
    shadow.add_argument("--issue", type=int, required=True)
    shadow.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    shadow.add_argument("--profiles", type=Path, default=PROFILES_PATH)
    shadow.add_argument("--gh-bin", default="gh")
    shadow.add_argument("--effort", choices=evaluator.ALLOWED_REASONING_EFFORTS, default="high")
    shadow.add_argument("--boundary-effort", choices=evaluator.ALLOWED_REASONING_EFFORTS, default="xhigh")
    shadow.add_argument("--no-boundary", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run_shadow(
            repo=args.repo,
            issue_number=args.issue,
            run_root=args.run_root,
            main_effort=args.effort,
            boundary_effort=None if args.no_boundary else args.boundary_effort,
            profiles_path=args.profiles,
            gh_bin=args.gh_bin,
        )
        print(
            json.dumps(
                {
                    "status": "ok",
                    "recommended_axes": result["recommended_axes"],
                    "recommended_work_queue": result["recommended_work_queue"],
                    "run_dir": result["run_dir"],
                    "notice": result["notice"],
                },
                ensure_ascii=False,
            )
        )
        return 0
    except meta.RunnerError as exc:
        print(json.dumps({"error": {"kind": exc.kind, "message": exc.safe_message}}, ensure_ascii=False), file=sys.stderr)
        return exc.exit_code
    except Exception:
        print(json.dumps({"error": {"kind": "internal", "message": "issue-triage shadow 内部错误"}}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
