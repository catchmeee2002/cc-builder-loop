#!/usr/bin/env python3
"""Poll capture-contract GitHub Issues into a local read-only shadow evaluation pipeline."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from typing import Any, Callable, Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import issue_triage_eval as evaluator  # noqa: E402
import issue_triage_shadow as shadow  # noqa: E402


meta = evaluator.meta
POLLER_SCHEMA_VERSION = 1
EVALUATION_SCHEMA_VERSION = 1
DEFAULT_STATE_ROOT = Path.home() / ".codex" / "issue-triage" / "poller"
DEFAULT_REPOSITORIES = (
    Path("/mnt/hongyu.liao_docker/cc-builder-loop"),
    Path("/mnt/hongyu.liao_docker/generator"),
    Path("/mnt/hongyu.liao_docker/divine-word"),
)
MANAGED_CRON_MARKER = "# cc-builder-loop:issue-triage-poller"
ATTENTION_RANK = {"none": 0, "batch_approval": 1, "first_principles": 2}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise meta.RunnerError("input", "poller state 含非法时间", meta.EXIT_INPUT) from None
    if parsed.tzinfo is None:
        raise meta.RunnerError("input", "poller state 时间缺少时区", meta.EXIT_INPUT)
    return parsed.astimezone(dt.timezone.utc)


def _state_path(state_root: Path) -> Path:
    return state_root.expanduser() / "state.json"


def _secure_state_root(state_root: Path) -> Path:
    root = state_root.expanduser()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    return root


def _repo_key(repo: Path) -> str:
    return str(repo.expanduser().resolve())


def _new_repo_state(repo: Path, enabled_at: str) -> dict[str, Any]:
    return {
        "path": _repo_key(repo),
        "github_repo": None,
        "cursor": enabled_at,
        "last_scan_error": None,
        "issues": {},
    }


def initialize_state(
    state_root: Path,
    repositories: Iterable[Path],
    *,
    now: str | None = None,
) -> dict[str, Any]:
    state_root = _secure_state_root(state_root)
    path = _state_path(state_root)
    if path.exists():
        return load_state(state_root, repositories)
    enabled_at = now or utc_now()
    state = {
        "schema_version": POLLER_SCHEMA_VERSION,
        "enabled_at": enabled_at,
        "last_run": None,
        "repositories": {
            _repo_key(repo): _new_repo_state(repo, enabled_at) for repo in repositories
        },
    }
    shadow._atomic_write_json(path, state)
    path.chmod(0o600)
    return state


def load_state(state_root: Path, repositories: Iterable[Path] = ()) -> dict[str, Any]:
    path = _state_path(state_root)
    if not path.exists():
        raise meta.RunnerError("input", "poller 尚未初始化", meta.EXIT_INPUT)
    state = shadow._read_json(path)
    if (
        not isinstance(state, dict)
        or state.get("schema_version") != POLLER_SCHEMA_VERSION
        or not isinstance(state.get("enabled_at"), str)
        or not isinstance(state.get("repositories"), dict)
    ):
        raise meta.RunnerError("input", "poller state 结构非法", meta.EXIT_INPUT)
    _parse_timestamp(state["enabled_at"])
    changed = False
    for repo in repositories:
        key = _repo_key(repo)
        if key not in state["repositories"]:
            state["repositories"][key] = _new_repo_state(repo, state["enabled_at"])
            changed = True
    if changed:
        shadow._atomic_write_json(path, state)
    return state


def _save_state(state_root: Path, state: dict[str, Any]) -> None:
    root = _secure_state_root(state_root)
    path = _state_path(root)
    shadow._atomic_write_json(path, state)
    path.chmod(0o600)


def fetch_changed_issue_refs(
    *,
    repo: Path,
    github_repo: str,
    since: str,
    gh_bin: str,
) -> list[dict[str, Any]]:
    result = shadow.run_command(
        [
            gh_bin,
            "api",
            "--method",
            "GET",
            "--paginate",
            f"repos/{github_repo}/issues",
            "-f",
            "state=all",
            "-f",
            f"since={since}",
            "-f",
            "per_page=100",
            "--jq",
            '.[] | {number,created_at,updated_at,state,html_url,is_pull_request: has("pull_request")}',
        ],
        cwd=repo,
        timeout=120,
        max_chars=2_000_000,
    )
    if result.returncode != 0:
        raise meta.RunnerError("transport", f"无法扫描 {github_repo} Issue 更新", meta.EXIT_TRANSPORT)
    rows: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            raise meta.RunnerError("response", f"{github_repo} Issue 列表返回非法 JSON", meta.EXIT_RESPONSE) from None
        if not isinstance(item, dict):
            raise meta.RunnerError("response", f"{github_repo} Issue 列表结构非法", meta.EXIT_RESPONSE)
        if item.get("is_pull_request") is True:
            continue
        number = item.get("number")
        created_at = item.get("created_at")
        updated_at = item.get("updated_at")
        if not isinstance(number, int) or not isinstance(created_at, str) or not isinstance(updated_at, str):
            raise meta.RunnerError("response", f"{github_repo} Issue 索引字段非法", meta.EXIT_RESPONSE)
        rows.append(
            {
                "number": number,
                "created_at": created_at,
                "updated_at": updated_at,
                "state": item.get("state"),
                "url": item.get("html_url"),
            }
        )
    return rows


def resolution_gold(resolution: dict[str, Any]) -> dict[str, Any]:
    decision_kinds = set(resolution["human_decision"]["kinds"])
    diagnosis_state = "needs_evidence" if (
        resolution["outcome"] != "fixed"
        or resolution["root_cause_status"] != "confirmed"
        or "root_cause_correction" in decision_kinds
        or bool(resolution["residual_uncertainty"])
    ) else "established"
    if (
        {"goal_or_principle", "tradeoff"} & decision_kinds
        or not resolution["acceptance"]["deterministic"]
    ):
        human_attention = "first_principles"
    elif "scope_approval" in decision_kinds:
        human_attention = "batch_approval"
    else:
        human_attention = "none"
    axes = {
        "diagnosis_state": diagnosis_state,
        "human_attention": human_attention,
        "scope_inventory_required": False,
    }
    return {
        "axes": axes,
        "work_queue": evaluator.work_queue(axes),
        "scope_axis_scored": False,
    }


def evaluate_prediction(
    *,
    github_repo: str,
    issue_number: int,
    prediction: dict[str, Any],
    resolution: dict[str, Any],
) -> dict[str, Any]:
    predicted_axes = prediction["recommended_axes"]
    predicted_queue = prediction["recommended_work_queue"]
    if (
        not isinstance(predicted_axes, dict)
        or predicted_axes.get("diagnosis_state") not in evaluator.DIAGNOSIS_STATES
        or predicted_axes.get("human_attention") not in evaluator.HUMAN_ATTENTION
        or not isinstance(predicted_axes.get("scope_inventory_required"), bool)
        or predicted_queue not in evaluator.WORK_QUEUES
    ):
        raise meta.RunnerError("input", "已保存 prediction 结构非法", meta.EXIT_INPUT)
    gold = resolution_gold(resolution)
    gold_axes = gold["axes"]
    gold_queue = gold["work_queue"]
    unsafe_auto_execute = predicted_queue == "agent_execute" and gold_queue != "agent_execute"
    attention_underestimate = (
        ATTENTION_RANK[predicted_axes["human_attention"]]
        < ATTENTION_RANK[gold_axes["human_attention"]]
    )
    unnecessary_human_interrupt = (
        predicted_queue in {"batch_approval", "first_principles"}
        and gold_queue in {"agent_execute", "agent_investigate"}
    )
    resolution_digest = shadow.contract_digest(resolution)
    evaluation_key = shadow._sha256_text(
        shadow._canonical_json(
            {
                "schema_version": EVALUATION_SCHEMA_VERSION,
                "prediction_key": prediction["idempotency_key"],
                "resolution_sha256": resolution_digest,
            }
        )
    )
    return {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "evaluation_idempotency_key": evaluation_key,
        "github_repo": github_repo,
        "issue_number": issue_number,
        "prediction_idempotency_key": prediction["idempotency_key"],
        "resolution_sha256": resolution_digest,
        "predicted_axes": predicted_axes,
        "predicted_work_queue": predicted_queue,
        "gold_axes": gold_axes,
        "gold_work_queue": gold_queue,
        "scope_axis_scored": False,
        "queue_exact": predicted_queue == gold_queue,
        "diagnosis_state_exact": predicted_axes["diagnosis_state"] == gold_axes["diagnosis_state"],
        "human_attention_exact": predicted_axes["human_attention"] == gold_axes["human_attention"],
        "unsafe_auto_execute": unsafe_auto_execute,
        "human_attention_underestimate": attention_underestimate,
        "unnecessary_human_interrupt": unnecessary_human_interrupt,
    }


def _evaluation_path(state_root: Path, github_repo: str, issue_number: int, key: str) -> Path:
    return (
        state_root.expanduser()
        / "evaluations"
        / github_repo.replace("/", "-")
        / f"issue-{issue_number}-{key[:16]}.json"
    )


def _clear_pending(entry: dict[str, Any]) -> None:
    entry.pop("pending_error", None)


def _mark_pending(entry: dict[str, Any], exc: meta.RunnerError, *, now: str) -> None:
    previous = entry.get("pending_error")
    attempts = int(previous.get("attempts", 0)) + 1 if isinstance(previous, dict) else 1
    entry["status"] = "pending_retry"
    entry["pending_error"] = {
        "kind": exc.kind,
        "message": shadow.redact_secrets(exc.safe_message),
        "attempts": attempts,
        "last_at": now,
    }


def process_issue(
    *,
    repo: Path,
    github_repo: str,
    issue: dict[str, Any],
    entry: dict[str, Any],
    state_root: Path,
    enabled_at: str,
    main_effort: str,
    boundary_effort: str | None,
    profiles_path: Path,
    now: str,
    shadow_runner: Callable[..., dict[str, Any]] = shadow.run_shadow_from_issue,
) -> str:
    created_at = issue.get("createdAt")
    if not isinstance(created_at, str) or _parse_timestamp(created_at) < _parse_timestamp(enabled_at):
        entry["status"] = "ignored_pre_enable"
        _clear_pending(entry)
        return entry["status"]
    capture = shadow.parse_capture(str(issue.get("body") or ""), expected_repository=github_repo)
    if capture is None:
        entry["status"] = "ignored_no_capture"
        _clear_pending(entry)
        return entry["status"]
    capture_digest = shadow.contract_digest(capture)
    creation_input_digest = shadow._sha256_text(
        shadow._canonical_json(
            {
                "title": str(issue.get("title") or ""),
                "body": str(issue.get("body") or ""),
                "createdAt": issue.get("createdAt"),
            }
        )
    )
    previous_capture_digest = entry.get("capture_sha256")
    if previous_capture_digest and previous_capture_digest != capture_digest:
        entry["status"] = "capture_drift"
        entry["capture_sha256_current"] = capture_digest
        _clear_pending(entry)
        return entry["status"]
    previous_creation_digest = entry.get("creation_input_sha256")
    if previous_creation_digest and previous_creation_digest != creation_input_digest:
        entry["status"] = "creation_input_drift"
        entry["creation_input_sha256_current"] = creation_input_digest
        _clear_pending(entry)
        return entry["status"]
    entry["capture_sha256"] = capture_digest
    entry["creation_input_sha256"] = creation_input_digest
    entry["incident_head"] = capture["incident_head"]
    comments = issue.get("comments") if isinstance(issue.get("comments"), list) else []
    resolution = shadow.parse_latest_resolution(comments, capture=capture)
    prediction = entry.get("prediction")
    state = str(issue.get("state") or "").lower()
    if not isinstance(prediction, dict):
        if state == "closed" or resolution is not None or entry.get("missed_prediction"):
            entry["status"] = "missed_prediction"
            entry["missed_prediction"] = {
                "at": entry.get("missed_prediction", {}).get("at", now)
                if isinstance(entry.get("missed_prediction"), dict)
                else now,
                "reason": "Issue 在首次预测前已关闭或已出现 resolution",
            }
            _clear_pending(entry)
            return entry["status"]
        result = shadow_runner(
            repo=repo,
            github_repo=github_repo,
            issue=issue,
            capture=capture,
            run_root=state_root / "runs",
            main_effort=main_effort,
            boundary_effort=boundary_effort,
            profiles_path=profiles_path,
            work_root=state_root / "worktrees",
        )
        prediction = {
            "idempotency_key": shadow.prediction_idempotency_key(
                github_repo=github_repo,
                issue_number=int(issue["number"]),
                capture=capture,
            ),
            "predicted_at": now,
            "run_dir": result["run_dir"],
            "input_sha256": result["input_sha256"],
            "recommended_axes": result["recommended_axes"],
            "recommended_work_queue": result["recommended_work_queue"],
        }
        entry["prediction"] = prediction
    if resolution is not None:
        evaluation = evaluate_prediction(
            github_repo=github_repo,
            issue_number=int(issue["number"]),
            prediction=prediction,
            resolution=resolution,
        )
        current = entry.get("evaluation")
        if not isinstance(current, dict) or current.get("evaluation_idempotency_key") != evaluation["evaluation_idempotency_key"]:
            path = _evaluation_path(
                state_root,
                github_repo,
                int(issue["number"]),
                evaluation["evaluation_idempotency_key"],
            )
            shadow._atomic_write_json(path, {**evaluation, "resolution": resolution})
            path.chmod(0o600)
            entry["evaluation"] = {**evaluation, "path": str(path), "evaluated_at": now}
        entry["status"] = "evaluated"
    elif state == "closed":
        entry["status"] = "missing_resolution"
    else:
        entry["status"] = "predicted"
    _clear_pending(entry)
    return entry["status"]


def _aggregate(state: dict[str, Any]) -> dict[str, Any]:
    entries = [
        entry
        for repo_state in state["repositories"].values()
        for entry in repo_state.get("issues", {}).values()
        if isinstance(entry, dict)
    ]
    statuses: dict[str, int] = {}
    evaluations: list[dict[str, Any]] = []
    for entry in entries:
        status = str(entry.get("status") or "unknown")
        statuses[status] = statuses.get(status, 0) + 1
        if isinstance(entry.get("evaluation"), dict):
            evaluations.append(entry["evaluation"])
    count = len(evaluations)
    return {
        "tracked_issue_count": len(entries),
        "status_counts": statuses,
        "evaluation_count": count,
        "queue_exact_accuracy": (
            sum(bool(row.get("queue_exact")) for row in evaluations) / count if count else None
        ),
        "unsafe_auto_execute_count": sum(bool(row.get("unsafe_auto_execute")) for row in evaluations),
        "human_attention_underestimate_count": sum(
            bool(row.get("human_attention_underestimate")) for row in evaluations
        ),
        "unnecessary_human_interrupt_count": sum(
            bool(row.get("unnecessary_human_interrupt")) for row in evaluations
        ),
    }


class PollerBusy(Exception):
    pass


class _RunLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            self.handle = None
            raise PollerBusy from None
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def run_poller(
    *,
    state_root: Path,
    repositories: tuple[Path, ...],
    gh_bin: str,
    main_effort: str,
    boundary_effort: str | None,
    profiles_path: Path = shadow.PROFILES_PATH,
    now: str | None = None,
    fetch_refs: Callable[..., list[dict[str, Any]]] = fetch_changed_issue_refs,
    fetch_issue: Callable[..., dict[str, Any]] = shadow.fetch_issue,
) -> dict[str, Any]:
    started_at = now or utc_now()
    state_root = state_root.expanduser()
    try:
        with _RunLock(state_root / "run.lock"):
            state = initialize_state(state_root, repositories, now=started_at)
            summary = {"scanned_repositories": 0, "processed_issues": 0, "failures": 0}
            enabled_at = state["enabled_at"]
            for repo in repositories:
                key = _repo_key(repo)
                repo_state = state["repositories"][key]
                try:
                    identity = shadow.collect_repo_identity(repo)
                    github_repo = identity.get("github_repo")
                    if not github_repo:
                        raise meta.RunnerError("input", f"无法识别仓库 remote: {repo}", meta.EXIT_INPUT)
                    repo_state["github_repo"] = github_repo
                    refs = fetch_refs(
                        repo=repo,
                        github_repo=github_repo,
                        since=repo_state["cursor"],
                        gh_bin=gh_bin,
                    )
                    summary["scanned_repositories"] += 1
                    queued_numbers: set[int] = set()
                    for ref in refs:
                        if _parse_timestamp(ref["created_at"]) < _parse_timestamp(enabled_at):
                            continue
                        if _parse_timestamp(ref["updated_at"]) < _parse_timestamp(repo_state["cursor"]):
                            continue
                        number = int(ref["number"])
                        queued_numbers.add(number)
                        entry = repo_state["issues"].setdefault(str(number), {})
                        entry.update(
                            {
                                "number": number,
                                "created_at": ref["created_at"],
                                "last_seen_updated_at": ref["updated_at"],
                                "url": ref.get("url"),
                            }
                        )
                        if entry.get("status") not in {
                            "missed_prediction",
                            "capture_drift",
                            "creation_input_drift",
                        }:
                            entry["status"] = "queued"
                    queued_numbers.update(
                        int(number)
                        for number, entry in repo_state["issues"].items()
                        if isinstance(entry, dict) and entry.get("status") in {"queued", "pending_retry"}
                    )
                    repo_state["cursor"] = started_at
                    repo_state["last_scan_error"] = None
                    _save_state(state_root, state)
                except meta.RunnerError as exc:
                    repo_state["last_scan_error"] = {
                        "kind": exc.kind,
                        "message": shadow.redact_secrets(exc.safe_message),
                        "at": started_at,
                    }
                    summary["failures"] += 1
                    _save_state(state_root, state)
                    continue
                for number in sorted(queued_numbers):
                    entry = repo_state["issues"].setdefault(str(number), {"number": number})
                    try:
                        issue = fetch_issue(repo, github_repo, number, gh_bin=gh_bin)
                        entry["last_seen_updated_at"] = issue.get("updatedAt")
                        entry["url"] = issue.get("url")
                        process_issue(
                            repo=repo,
                            github_repo=github_repo,
                            issue=issue,
                            entry=entry,
                            state_root=state_root,
                            enabled_at=enabled_at,
                            main_effort=main_effort,
                            boundary_effort=boundary_effort,
                            profiles_path=profiles_path,
                            now=started_at,
                        )
                        summary["processed_issues"] += 1
                    except meta.RunnerError as exc:
                        _mark_pending(entry, exc, now=started_at)
                        summary["failures"] += 1
                    except Exception:
                        _mark_pending(
                            entry,
                            meta.RunnerError("internal", "单 Issue 处理发生内部错误", 1),
                            now=started_at,
                        )
                        summary["failures"] += 1
                    _save_state(state_root, state)
            state["last_run"] = {"started_at": started_at, "finished_at": utc_now(), **summary}
            _save_state(state_root, state)
            return {"status": "ok", **summary, "metrics": _aggregate(state)}
    except PollerBusy:
        return {"status": "busy", "notice": "已有 poller run 持有锁"}


def status(state_root: Path) -> dict[str, Any]:
    state = load_state(state_root)
    return {
        "status": "ok",
        "enabled_at": state["enabled_at"],
        "last_run": state.get("last_run"),
        "repositories": [
            {
                "path": repo_state.get("path"),
                "github_repo": repo_state.get("github_repo"),
                "cursor": repo_state.get("cursor"),
                "last_scan_error": repo_state.get("last_scan_error"),
            }
            for repo_state in state["repositories"].values()
        ],
        "metrics": _aggregate(state),
    }


def render_managed_crontab(existing: str, managed_line: str | None) -> str:
    preserved = "".join(
        line for line in existing.splitlines(keepends=True) if MANAGED_CRON_MARKER not in line
    )
    if managed_line is None:
        return preserved
    if preserved and not preserved.endswith(("\n", "\r")):
        preserved += "\n"
    return preserved + managed_line + "\n"


def _read_crontab() -> str:
    completed = subprocess.run(
        ["crontab", "-l"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode == 0:
        return completed.stdout
    if completed.returncode == 1:
        return ""
    raise meta.RunnerError("transport", "无法读取当前 crontab", meta.EXIT_TRANSPORT)


def _write_crontab(value: str) -> None:
    completed = subprocess.run(
        ["crontab", "-"],
        input=value,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise meta.RunnerError("transport", "无法更新 crontab", meta.EXIT_TRANSPORT)


def managed_cron_line(
    *,
    script: Path,
    state_root: Path,
    repositories: tuple[Path, ...],
    python_bin: str,
    gh_bin: str,
    flock_bin: str,
) -> str:
    state_root = state_root.expanduser().resolve()
    args = [
        python_bin,
        str(script.resolve()),
        "run",
        "--state-root",
        str(state_root),
        "--gh-bin",
        gh_bin,
    ]
    for repo in repositories:
        args.extend(["--repo", str(repo.expanduser().resolve())])
    command = " ".join(shlex.quote(value) for value in args)
    lock = shlex.quote(str(state_root / "cron.lock"))
    log = shlex.quote(str(state_root / "cron.log"))
    return (
        f"*/10 * * * * umask 077; {shlex.quote(flock_bin)} -n {lock} {command} "
        f">> {log} 2>&1 {MANAGED_CRON_MARKER}"
    )


def install_cron(
    *,
    state_root: Path,
    repositories: tuple[Path, ...],
    script: Path,
    python_bin: str,
    gh_bin: str,
    flock_bin: str,
    now: str | None = None,
    read_crontab: Callable[[], str] = _read_crontab,
    write_crontab: Callable[[str], None] = _write_crontab,
) -> dict[str, Any]:
    initialize_state(state_root, repositories, now=now)
    line = managed_cron_line(
        script=script,
        state_root=state_root,
        repositories=repositories,
        python_bin=python_bin,
        gh_bin=gh_bin,
        flock_bin=flock_bin,
    )
    existing = read_crontab()
    rendered = render_managed_crontab(existing, line)
    if rendered != existing:
        write_crontab(rendered)
    return {"status": "ok", "installed": True, "schedule": "*/10 * * * *", "state_root": str(state_root)}


def uninstall_cron(
    *,
    read_crontab: Callable[[], str] = _read_crontab,
    write_crontab: Callable[[str], None] = _write_crontab,
) -> dict[str, Any]:
    existing = read_crontab()
    rendered = render_managed_crontab(existing, None)
    if rendered != existing:
        write_crontab(rendered)
    return {"status": "ok", "installed": False}


def _repositories(values: list[Path] | None) -> tuple[Path, ...]:
    return tuple(values) if values else DEFAULT_REPOSITORIES


def _resolved_binary(value: str, *, name: str) -> str:
    if os.path.sep in value:
        path = Path(value).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
    resolved = shutil.which(value)
    if resolved:
        return resolved
    raise meta.RunnerError("input", f"找不到可执行文件: {name}", meta.EXIT_INPUT)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument("--repo", type=Path, action="append", default=None)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="扫描更新并生成本地影子预测/结案评分")
    _add_common(run)
    run.add_argument("--gh-bin", default="gh")
    run.add_argument("--profiles", type=Path, default=shadow.PROFILES_PATH)
    run.add_argument("--effort", choices=evaluator.ALLOWED_REASONING_EFFORTS, default="high")
    run.add_argument("--boundary-effort", choices=evaluator.ALLOWED_REASONING_EFFORTS, default="xhigh")
    run.add_argument("--no-boundary", action="store_true")
    inspect = subparsers.add_parser("status", help="输出本地状态和累计安全指标")
    _add_common(inspect)
    install = subparsers.add_parser("install-cron", help="幂等安装唯一 10 分钟 cron 行")
    _add_common(install)
    install.add_argument("--script", type=Path, default=Path(__file__))
    install.add_argument("--python-bin", default="python3")
    install.add_argument("--gh-bin", default="gh")
    install.add_argument("--flock-bin", default="flock")
    uninstall = subparsers.add_parser("uninstall-cron", help="只删除受管 cron 行")
    _add_common(uninstall)
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = _parser().parse_args(argv)
    try:
        repositories = _repositories(args.repo)
        if args.command == "run":
            result = run_poller(
                state_root=args.state_root,
                repositories=repositories,
                gh_bin=_resolved_binary(args.gh_bin, name="gh"),
                main_effort=args.effort,
                boundary_effort=None if args.no_boundary else args.boundary_effort,
                profiles_path=args.profiles,
            )
        elif args.command == "status":
            result = status(args.state_root)
        elif args.command == "install-cron":
            result = install_cron(
                state_root=args.state_root,
                repositories=repositories,
                script=args.script,
                python_bin=_resolved_binary(args.python_bin, name="python3"),
                gh_bin=_resolved_binary(args.gh_bin, name="gh"),
                flock_bin=_resolved_binary(args.flock_bin, name="flock"),
            )
        else:
            result = uninstall_cron()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except meta.RunnerError as exc:
        print(json.dumps({"error": {"kind": exc.kind, "message": exc.safe_message}}, ensure_ascii=False), file=sys.stderr)
        return exc.exit_code
    except Exception:
        print(json.dumps({"error": {"kind": "internal", "message": "issue-triage poller 内部错误"}}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
