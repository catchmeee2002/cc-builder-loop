from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "codex-builder-loop.py"


@dataclass(frozen=True)
class ProcessResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    data: dict[str, Any]


def run_process(
    argv: Iterable[str | os.PathLike[str]],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update({str(k): str(v) for k, v in env.items()})
    return subprocess.run(
        [str(v) for v in argv],
        cwd=str(cwd) if cwd is not None else None,
        env=merged_env,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def run_cli(
    *args: str | os.PathLike[str],
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> ProcessResult:
    if not CLI.is_file():
        raise AssertionError(f"runtime missing: {CLI}")
    cp = run_process(
        [sys.executable, CLI, *args], cwd=cwd, env=env, input_text=input_text
    )
    lines = [line.strip() for line in cp.stdout.splitlines() if line.strip()]
    if not lines:
        raise AssertionError(
            f"CLI produced no JSON line\nargv={args!r}\nrc={cp.returncode}\nstderr={cp.stderr}"
        )
    try:
        data = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise AssertionError(
            "CLI stdout last line is not JSON\n"
            f"argv={args!r}\nrc={cp.returncode}\nstdout={cp.stdout}\nstderr={cp.stderr}"
        ) from exc
    if not isinstance(data, dict):
        raise AssertionError(f"CLI JSON must be an object, got: {data!r}")
    return ProcessResult(
        tuple(str(v) for v in args), cp.returncode, cp.stdout, cp.stderr, data
    )


def assert_status(result: ProcessResult, expected: str, *, rc: int | None = None) -> None:
    actual = result.data.get("status")
    if actual != expected:
        raise AssertionError(
            f"status={actual!r}, expected={expected!r}\n"
            f"argv={result.argv!r}\nrc={result.returncode}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    if rc is not None and result.returncode != rc:
        raise AssertionError(
            f"rc={result.returncode}, expected={rc}\n"
            f"status={actual!r}\nstdout={result.stdout}\nstderr={result.stderr}"
        )


def assert_status_one_of(
    result: ProcessResult,
    expected: Iterable[str],
    *,
    rc: int,
) -> None:
    allowed = tuple(expected)
    actual = result.data.get("status")
    if actual not in allowed:
        raise AssertionError(
            f"status={actual!r}, expected one of={allowed!r}\n"
            f"argv={result.argv!r}\nrc={result.returncode}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    if result.returncode != rc:
        raise AssertionError(
            f"rc={result.returncode}, expected={rc}\n"
            f"status={actual!r}\nstdout={result.stdout}\nstderr={result.stderr}"
        )


def git(repo: str | os.PathLike[str], *args: str, check: bool = True) -> str:
    cp = run_process(["git", "-C", repo, *args])
    if check and cp.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed rc={cp.returncode}\n"
            f"stdout={cp.stdout}\nstderr={cp.stderr}"
        )
    return cp.stdout.strip()


def init_repo(files: Mapping[str, str] | None = None) -> Path:
    repo = Path(tempfile.mkdtemp(prefix="codex-builder-loop-test-"))
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "builder-loop@test.local")
    git(repo, "config", "user.name", "builder-loop fixture")
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for hook in hooks_dir.iterdir():
        if not hook.name.endswith(".sample") and (hook.is_file() or hook.is_symlink()):
            hook.unlink()
    git(repo, "config", "core.hooksPath", str(hooks_dir))
    seed = {
        "README.md": "fixture\n",
        "src/calc.py": "def add(a, b):\n    return a + b\n",
        "src/proof_fixture.py": "VALUE = 1\n",
        "tests/test_calc.py": (
            "from src.calc import add\n\n"
            "def test_add():\n"
            "    assert add(1, 2) == 3\n"
        ),
        "verify.sh": (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "python3 - <<'PY'\n"
            "import pathlib\n"
            "import runpy\n"
            "import types\n"
            "import unittest\n"
            "\n"
            "for path in sorted(pathlib.Path('tests').glob('test_*.py')):\n"
            "    namespace = runpy.run_path(str(path))\n"
            "    for name, value in namespace.items():\n"
            "        if name.startswith('test_') and callable(value):\n"
            "            value()\n"
            "    module = types.ModuleType(path.stem)\n"
            "    module.__dict__.update(namespace)\n"
            "    suite = unittest.defaultTestLoader.loadTestsFromModule(module)\n"
            "    result = unittest.TextTestRunner(verbosity=0).run(suite)\n"
            "    if not result.wasSuccessful():\n"
            "        raise SystemExit(1)\n"
            "PY\n"
        ),
    }
    if files:
        seed.update(files)
    for rel, content in seed.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (repo / "verify.sh").chmod(0o755)
    commit_all(repo, "fixture seed")
    return repo


def cleanup_repo(repo: Path) -> None:
    if repo.exists():
        git(repo, "worktree", "prune", check=False)
        shutil.rmtree(repo, ignore_errors=True)


def commit_all(repo: str | os.PathLike[str], message: str) -> str:
    git(repo, "add", "-A")
    cp = run_process(
        [
            "git",
            "-C",
            repo,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            message,
        ]
    )
    if cp.returncode != 0:
        raise AssertionError(f"commit failed\nstdout={cp.stdout}\nstderr={cp.stderr}")
    return git(repo, "rev-parse", "HEAD")


def head(repo: str | os.PathLike[str]) -> str:
    return git(repo, "rev-parse", "HEAD")


def tree(repo: str | os.PathLike[str], revision: str = "HEAD") -> str:
    return git(repo, "rev-parse", f"{revision}^{{tree}}")


def plan_markdown(
    spec_head: str,
    *,
    parallel_ready: bool = True,
    builder_write: list[str] | None = None,
    tester_write: list[str] | None = None,
    target_test_dirs: list[str] | None = None,
    runner: str | None = "bash verify.sh",
    include_e2e: bool = False,
    include_unit_spec: bool = True,
    evidence_scopes: dict[str, dict[str, list[str]]] | None = None,
    workspace_intake: list[dict[str, str]] | None = None,
) -> str:
    builder_write = builder_write or ["src/**", "docs/**"]
    tester_write = tester_write or ["tests/**"]
    target_test_dirs = target_test_dirs or ["tests"]
    lines = [
        "# Fixture delivery plan",
        "",
        "## Background",
        "Implement and independently verify addition behavior.",
        "",
    ]
    if workspace_intake:
        lines.extend(
            [
                "<!-- workspace-intake -->",
                "schema_version: 1",
                "files:",
            ]
        )
        for item in workspace_intake:
            lines.extend(
                [
                    f'  - path: {json.dumps(item["path"])}',
                    f'    state_sha256: {json.dumps(item["state_sha256"])}',
                ]
            )
        lines.extend(["<!-- /workspace-intake -->", ""])
    if include_unit_spec:
        lines.extend(
            [
                "<!-- unit-test-spec -->",
                "schema_version: 3",
                f'spec_head: "{spec_head}"',
                "plan_revision: 1",
                f"parallel_ready: {'true' if parallel_ready else 'false'}",
                "interfaces:",
                '  - "src/calc.py:add(a, b) -> int"',
                "test_context:",
                f"  target_test_dirs: {json.dumps(target_test_dirs)}",
                '  support_paths: ["verify.sh"]',
                "  public_prerequisites: "
                + json.dumps([] if parallel_ready else ["src/public_api.py"]),
            ]
        )
        if runner is not None:
            lines.append(f"  runner: {json.dumps(runner)}")
        lines.extend(
            [
                "ownership:",
                f"  builder_write: {json.dumps(builder_write)}",
                f"  tester_write: {json.dumps(tester_write)}",
            ]
        )
        if evidence_scopes is not None:
            lines.append("evidence_scopes:")
            for key in ("machine", "blackbox"):
                lines.extend(
                    [
                        f"  {key}:",
                        f"    affects: {json.dumps(evidence_scopes[key]['affects'])}",
                        f"    exempt: {json.dumps(evidence_scopes[key]['exempt'])}",
                    ]
                )
        lines.extend(
            [
                "behaviors:",
                "  - id: add-positive",
                '    what: "add returns the arithmetic sum"',
                '    boundaries: ["zero", "negative"]',
                '    invariants: ["inputs are not mutated"]',
                "test_effectiveness:",
                "  requirements:",
                "    - behavior_id: add-positive",
                "      minimum: strong",
                "mock_strategy: {}",
                "<!-- /unit-test-spec -->",
                "",
            ]
        )
    lines.extend(
        [
            "<!-- plan-checklist -->",
            "- [ ] Builder changes implementation only in owned paths.",
            "- [ ] Tester changes tests only in owned paths.",
            "- [ ] Verify, review, and doc review target the integrated head.",
            "<!-- /plan-checklist -->",
            "",
        ]
    )
    if include_e2e:
        lines.extend(
            [
                "<!-- e2e-cases -->",
                "schema_version: 1",
                "cases:",
                "  - id: add-cli",
                "    covers: [add-positive]",
                '    input: "add 1 2"',
                "    level: full",
                "    hard_rules:",
                '      response_contains: ["3"]',
                "    verify:",
                '      must: ["The result is 3."]',
                '      must_not: ["An error is emitted."]',
                "    quality:",
                '      criteria: ["The response is concise."]',
                "<!-- /e2e-cases -->",
                "",
            ]
        )
    return "\n".join(lines)


def l1_plan_markdown(
    spec_head: str,
    *,
    builder_write: list[str] | None = None,
    plan_revision: int = 1,
    supersedes_run_id: str | None = None,
    supersedes_plan_sha256: str | None = None,
) -> str:
    builder_write = builder_write or ["README.md"]
    lines = [
        "# Documentation-only plan",
        "",
        "预估改动级别：L1",
        "",
        "<!-- documentation-spec -->",
        "schema_version: 3",
        f'spec_head: "{spec_head}"',
        f"plan_revision: {plan_revision}",
    ]
    if supersedes_run_id is not None or supersedes_plan_sha256 is not None:
        run_value = supersedes_run_id or ""
        sha_value = supersedes_plan_sha256 or ""
        lines.extend(
            [
                "supersedes:",
                f'  run_id: "{run_value}"',
                f'  plan_sha256: "{sha_value}"',
            ]
        )
    lines.extend(
        [
            "ownership:",
            f"  builder_write: {json.dumps(builder_write)}",
            "<!-- /documentation-spec -->",
            "",
            "<!-- plan-checklist -->",
            "- [ ] Update the requested Markdown documents.",
            "- [ ] Review content and document policy on the final HEAD.",
            "<!-- /plan-checklist -->",
            "",
        ]
    )
    return "\n".join(lines)


def write_plan(repo: Path, text: str, name: str = "plan.md") -> Path:
    plan_dir = repo / ".git" / "builder-loop-fixtures"
    plan_dir.mkdir(parents=True, exist_ok=True)
    path = plan_dir / name
    path.write_text(text)
    return path


def start_run(repo: Path, plan: Path, *, task: str = "fixture task") -> tuple[ProcessResult, Path]:
    result = run_cli(
        "start",
        "--repo",
        repo,
        "--plan",
        plan,
        "--task",
        task,
        "--session-id",
        "fixture-session",
    )
    assert_status(result, "READY", rc=0)
    run_id = result.data.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise AssertionError(f"start must return run_id: {result.data!r}")
    run_path_raw = result.data.get("run_path")
    run_path = (
        Path(run_path_raw)
        if isinstance(run_path_raw, str) and run_path_raw
        else repo / ".builder-loop" / "codex" / "runs" / run_id
    )
    if not run_path.is_dir():
        raise AssertionError(f"run directory missing: {run_path}")
    return result, run_path


def ledger_path(run_path: Path) -> Path:
    return run_path / "ledger.json"


def load_ledger(run_path: Path) -> dict[str, Any]:
    path = ledger_path(run_path)
    if not path.is_file():
        raise AssertionError(f"ledger missing: {path}")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise AssertionError(f"ledger must be object: {data!r}")
    return data


def assert_ledger_schema(run_path: Path) -> None:
    from jsonschema import Draft202012Validator, FormatChecker

    schema = json.loads((ROOT / "schema" / "codex-loop-ledger.schema.json").read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(
        load_ledger(run_path)
    )


def start_agent_turn(
    run_path: Path,
    role: str,
    *,
    agent_id: str | None = None,
) -> tuple[str, str]:
    ledger = load_ledger(run_path)
    repo_root = ledger.get("repo_root")
    session_id = ledger.get("owner_session_id")
    if not isinstance(repo_root, str) or not repo_root:
        raise AssertionError(f"ledger missing repo_root: {ledger!r}")
    if not isinstance(session_id, str) or not session_id:
        raise AssertionError(f"ledger missing owner_session_id: {ledger!r}")
    resolved_agent_id = agent_id or f"fixture-{role}-agent"
    turn_id = f"{resolved_agent_id}-turn-{len(ledger.get('events', [])) + 1}"
    event_result = run_cli(
        "agent-event",
        "--repo",
        repo_root,
        "--session-id",
        session_id,
        "--role",
        role,
        "--agent-id",
        resolved_agent_id,
        "--turn-id",
        turn_id,
        "--event",
        "start",
        env={"BUILDER_LOOP_HOOK_EVENT": "1"},
    )
    if event_result.data.get("status") not in {"READY", "NOOP"} or event_result.returncode != 0:
        raise AssertionError(
            f"agent-event failed role={role} event=start: "
            f"rc={event_result.returncode} data={event_result.data!r} stderr={event_result.stderr}"
        )
    if not event_result.data.get("recorded"):
        raise AssertionError(f"agent-event was not recorded: {event_result.data!r}")
    return resolved_agent_id, turn_id


def finish_agent_turn(
    run_path: Path,
    role: str,
    *,
    agent_id: str,
    turn_id: str,
    result: str,
) -> None:
    ledger = load_ledger(run_path)
    if role == "tester" and result == "tests_ready":
        tester = Path(str(ledger["worktrees"]["tester"]["path"]))
        source_head = str(ledger["tester_integration"]["source_head"])
        changed = git(tester, "diff", "--name-only", source_head, "HEAD")
        if changed:
            _ensure_standard_proof_source(run_path)
            ledger = load_ledger(run_path)
    event_result = run_cli(
        "agent-event",
        "--repo",
        str(ledger["repo_root"]),
        "--session-id",
        str(ledger["owner_session_id"]),
        "--role",
        role,
        "--agent-id",
        agent_id,
        "--turn-id",
        turn_id,
        "--event",
        "idle",
        "--result",
        result,
        env={"BUILDER_LOOP_HOOK_EVENT": "1"},
    )
    if event_result.data.get("status") not in {"READY", "NOOP"} or event_result.returncode != 0:
        raise AssertionError(
            f"agent-event failed role={role} event=idle: "
            f"rc={event_result.returncode} data={event_result.data!r} stderr={event_result.stderr}"
        )
    if not event_result.data.get("recorded"):
        raise AssertionError(f"agent-event was not recorded: {event_result.data!r}")


def register_agent(
    run_path: Path,
    role: str,
    *,
    agent_id: str | None = None,
    result: str | None = None,
) -> str:
    resolved_result = result or ("tests_ready" if role == "tester" else "pass")
    resolved_agent_id, turn_id = start_agent_turn(
        run_path, role, agent_id=agent_id
    )
    if role == "tester" and resolved_result == "tests_ready":
        _ensure_standard_proof_source(run_path)
    finish_agent_turn(
        run_path,
        role,
        agent_id=resolved_agent_id,
        turn_id=turn_id,
        result=resolved_result,
    )
    return resolved_agent_id


def _ensure_standard_proof_source(run_path: Path) -> None:
    ledger = load_ledger(run_path)
    if ledger["tester_integration"]["completed"]:
        return
    tester = Path(str(ledger["worktrees"]["tester"]["path"]))
    target = tester / "tests" / "test_effectiveness_contract.py"
    package = tester / "tests" / "__init__.py"
    changed = False
    if not package.is_file():
        package.write_text("")
        changed = True
    if not target.is_file():
        target.write_text(
            "import unittest\n"
            "from src.proof_fixture import VALUE\n\n"
            "class TestEffectivenessContract(unittest.TestCase):\n"
            "    def test_frozen_invariant(self):\n"
            "        self.assertEqual(VALUE, 1)\n"
        )
        changed = True
    if changed:
        commit_all(tester, "add standard test-effectiveness source")


def _frozen_plan_text(ledger: dict[str, Any]) -> str:
    path = Path(str(ledger.get("plan", {}).get("path", "")))
    return path.read_text() if path.is_file() else ""


def _planned_e2e_cases(ledger: dict[str, Any]) -> list[dict[str, str]]:
    text = _frozen_plan_text(ledger)
    if "<!-- e2e-cases -->" not in text or "<!-- /e2e-cases -->" not in text:
        return []
    body = text.split("<!-- e2e-cases -->", 1)[1].split(
        "<!-- /e2e-cases -->", 1
    )[0]
    cases: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in body.splitlines():
        case_match = re.match(r"^\s*- id:\s*([a-z0-9][a-z0-9-]*)\s*$", line)
        if case_match:
            if current is not None:
                cases.append(current)
            current = {"id": case_match.group(1)}
            continue
        level_match = re.match(r"^\s+level:\s*(full|fast)\s*$", line)
        if current is not None and level_match:
            current["level"] = level_match.group(1)
            continue
        if current is not None and re.match(r"^\s+hard_rules:\s*$", line):
            current["has_hard_rules"] = "true"
    if current is not None:
        cases.append(current)
    return cases


def _canonical_case_results(
    ledger: dict[str, Any], *, passed: bool
) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for case in _planned_e2e_cases(ledger):
        level = case["level"]
        results.append(
            {
                "case_id": case["id"],
                "level": level,
                "mechanical": "pass" if passed else "fail",
                "verify": ("pass" if passed else "fail")
                if level == "full"
                else "not_applicable",
                "quality": ("pass" if passed else "fail")
                if level == "full"
                else "not_applicable",
                "outcome": "pass" if passed else "fail",
            }
        )
    return results


def _v2_case_results(ledger: dict[str, Any], *, passed: bool) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for case in _planned_e2e_cases(ledger):
        level = case["level"]
        mechanical_applicable = level == "fast" or case.get("has_hard_rules") == "true"

        def dimension(name: str, applicable: bool) -> dict[str, str]:
            if not applicable:
                return {
                    "status": "not_applicable",
                    "observation": f"{name} is not applicable to the frozen {level} case.",
                }
            return {
                "status": "pass" if passed else "fail",
                "observation": (
                    f"{name} passed for the frozen {level} case."
                    if passed
                    else f"{name} failed for the frozen {level} case."
                ),
            }

        results.append(
            {
                "case_id": case["id"],
                "mechanical": dimension("mechanical", mechanical_applicable),
                "verify": dimension("verify", level == "full"),
                "quality": dimension("quality", level == "full"),
                "outcome": "pass" if passed else "fail",
            }
        )
    return results


def blackbox_report_details(
    ledger: dict[str, Any],
    *,
    candidate_worktree: str | os.PathLike[str],
    head_before: str,
    head_after: str,
    command: str,
    returncode: int,
    candidate_dirty: bool,
    timed_out: bool = False,
) -> dict[str, Any]:
    report_version = ledger.get("plan", {}).get("blackbox_report_schema_version", 1)
    if report_version == 2:
        details: dict[str, Any] = {
            "schema_version": 2,
            "candidate_worktree": str(candidate_worktree),
            "head_before": head_before,
            "head_after": head_after,
            "candidate_dirty": candidate_dirty,
            "executions": [
                {
                    "method": "command",
                    "command": command,
                    "returncode": returncode,
                    "timed_out": timed_out,
                }
            ],
        }
        cases = _v2_case_results(ledger, passed=returncode == 0 and not timed_out)
    else:
        details = {
            "candidate_worktree": str(candidate_worktree),
            "head_before": head_before,
            "head_after": head_after,
            "command": command,
            "returncode": returncode,
            "candidate_dirty": candidate_dirty,
        }
        cases = _canonical_case_results(ledger, passed=returncode == 0)
    if cases:
        details["cases"] = cases
    return details


def ensure_test_effectiveness(run_path: Path) -> None:
    ledger = load_ledger(run_path)
    if "test_effectiveness:" not in _frozen_plan_text(ledger):
        return
    repo = Path(str(ledger["repo_root"]))
    builder = Path(str(ledger["worktrees"]["builder"]["path"]))
    candidate = head(builder)
    proof_evidence = ledger.get("evidence", {}).get("test_effectiveness")
    if isinstance(proof_evidence, dict) and candidate in {
        proof_evidence.get("head"),
        proof_evidence.get("observed_head"),
        proof_evidence.get("accepted_head"),
    }:
        return
    if ledger.get("evidence", {}).get("test_effectiveness_head") == candidate:
        return
    if ledger.get("verification", {}).get("test_effectiveness_head") == candidate:
        return
    patch = (
        "diff --git a/src/proof_fixture.py b/src/proof_fixture.py\n"
        "--- a/src/proof_fixture.py\n"
        "+++ b/src/proof_fixture.py\n"
        "@@ -1 +1 @@\n"
        "-VALUE = 1\n"
        "+VALUE = 0\n"
    )
    spec = {
        "schema_version": 1,
        "groups": [
            {
                "behavior_ids": ["add-positive"],
                "method": "mutation",
                "argv": [
                    "python3",
                    "-m",
                    "unittest",
                    "tests.test_effectiveness_contract."
                    "TestEffectivenessContract.test_frozen_invariant",
                ],
                "test_ids": [
                    "tests.test_effectiveness_contract."
                    "TestEffectivenessContract.test_frozen_invariant"
                ],
                "timeout_seconds": 30,
                "patch": patch,
            }
        ],
    }
    result = run_cli(
        "prove-tests",
        "--repo",
        repo,
        "--run",
        run_path,
        "--spec",
        "-",
        input_text=json.dumps(spec),
    )
    if result.returncode != 0 or result.data.get("status") not in {"READY", "NOOP"}:
        raise AssertionError(
            f"test-effectiveness proof failed: rc={result.returncode} "
            f"data={result.data!r} stderr={result.stderr}"
        )


def record_evidence(
    run_path: Path,
    kind: str,
    evidence_head: str,
    *,
    agent_id: str | None = None,
    command_argv: list[str] | None = None,
) -> ProcessResult:
    if not agent_id:
        raise AssertionError(f"agent_id is required for {kind} evidence")
    if kind == "e2e_verified":
        ensure_test_effectiveness(run_path)
    argv: list[str | os.PathLike[str]] = [
        "record-evidence",
        "--run",
        run_path,
        "--kind",
        kind,
        "--head",
        evidence_head,
    ]
    argv.extend(["--agent-id", agent_id])
    if kind == "e2e_verified":
        ledger = load_ledger(run_path)
        builder = Path(str(ledger["worktrees"]["builder"]["path"]))
        command_argv = command_argv or [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-p",
            "test_*.py",
        ]
        head_before = head(builder)
        blackbox = run_process(
            command_argv,
            cwd=builder,
            env={"PYTHONDONTWRITEBYTECODE": "1"},
        )
        head_after = head(builder)
        dirty = bool(
            git(builder, "status", "--porcelain", "--untracked-files=all")
            or git(
                builder,
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
            )
        )
        details = blackbox_report_details(
            ledger,
            candidate_worktree=builder,
            head_before=head_before,
            head_after=head_after,
            command=shlex.join(command_argv),
            returncode=blackbox.returncode,
            candidate_dirty=dirty,
        )
        argv.extend(
            [
                "--details",
                json.dumps(details),
            ]
        )
    result = run_cli(*argv)
    if result.data.get("status") not in {"READY", "NOOP"} or result.returncode != 0:
        raise AssertionError(
            f"record-evidence failed kind={kind}: "
            f"rc={result.returncode} data={result.data!r} stderr={result.stderr}"
        )
    return result


def worktrees_from(result: ProcessResult, run_path: Path) -> tuple[Path, Path]:
    value = result.data.get("worktrees")
    if not isinstance(value, dict):
        value = load_ledger(run_path).get("worktrees")
    if not isinstance(value, dict):
        raise AssertionError("start result/ledger must contain worktrees object")
    builder = value.get("builder")
    tester = value.get("tester")
    if not isinstance(builder, str) or not isinstance(tester, str):
        raise AssertionError(f"worktrees must contain string builder/tester paths: {value!r}")
    return Path(builder), Path(tester)
