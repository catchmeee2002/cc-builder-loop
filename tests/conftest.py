"""pytest fixtures：临时 git 仓 + 隔离的 BUILDER_LOOP_HOME + CLI / hook 调用辅助。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
sys.path.insert(0, str(RUNTIME))

PYTEST_CMD = "python3 -m pytest -p no:html -p no:cacheprovider -q tests"
PYTEST_ARGV = ["python3", "-m", "pytest", "-p", "no:html", "-p", "no:cacheprovider", "-q"]

CONTRACT = {
    "schema": "builder-loop/contract@1",
    "mission": {
        "revision": 1,
        "slug": "add-mul",
        "objective": "Add mul()",
        "behaviors": [{"id": "B1", "given": "two ints", "when": "mul(a,b)", "then": "returns product"}],
    },
    "authority": {"builder_write": ["src/**"], "tester_write": ["tests/**"], "protected_paths": [".claude/loop.yml"]},
    "assurance": {"required": ["machine", "tester", "proof", "reviewer"]},
}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()


def write_plan(repo: Path, contract: dict, name: str = "plan.md") -> Path:
    p = repo / name
    p.write_text("# plan\n<!-- builder-loop-contract -->\n```json\n" + json.dumps(contract) + "\n```\n<!-- /builder-loop-contract -->\n", encoding="utf-8")
    return p


@dataclass
class Repo:
    root: Path
    home: Path

    def commit_all(self, msg: str = "chore(fixture): [cr_id_skip] Commit") -> str:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", msg)
        return git(self.root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Repo:
    root = tmp_path / "repo"
    home = tmp_path / "home"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / ".claude").mkdir()
    home.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    hooks = root / ".git" / "hooks"
    if hooks.exists():
        for f in hooks.iterdir():
            f.unlink()
    git(root, "config", "user.email", "t@t")
    git(root, "config", "user.name", "t")
    (root / "src" / "__init__.py").write_text("")
    (root / "src" / "foo.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "tests" / "test_foo.py").write_text("from src.foo import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    (root / ".claude" / "loop.yml").write_text(f"pass_cmd:\n  - stage: test\n    cmd: \"{PYTEST_CMD}\"\n    timeout: 60\nmax_iterations: 3\n")
    (root / ".gitignore").write_text(".claude/builder-loop/\n__pycache__/\n")
    write_plan(root, CONTRACT)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "chore(fixture): [cr_id_skip] Init")
    monkeypatch.setenv("BUILDER_LOOP_HOME", str(home))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    return Repo(root=root, home=home)


@pytest.fixture
def cli(repo: Repo):
    from builder_loop.cli import build_parser, dispatch
    from builder_loop.errors import Problem

    def run(*args: str, expect: int | None = 0):
        parser = build_parser()
        ns = parser.parse_args(["--repo", str(repo.root), *args])
        try:
            out, code = dispatch(ns)
        except Problem as exc:
            out, code = exc.to_json(), exc.exit_code
        if expect is not None:
            assert code == expect, f"{args}: exit {code} != {expect}: {out}"
        return out

    return run


@pytest.fixture
def hook(repo: Repo):
    from builder_loop.hooks import handle_hook

    def fire(event: str, payload: dict):
        (stdout, stderr), code = handle_hook(event, json.dumps(payload))
        return {"code": code, "stdout": stdout, "stderr": stderr, "json": (json.loads(stdout) if stdout.strip() else None)}

    return fire


@pytest.fixture
def started(repo: Repo, cli):
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")
    return {"repo": repo, "run_id": out["run_id"], "worktree": Path(out["worktree"]), "ledger": Path(out["ledger"]), "target_start_head": out["target_start_head"]}


def implement_mul(wt: Path) -> None:
    (wt / "src" / "foo.py").write_text("def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")


def write_mul_test(wt: Path) -> None:
    (wt / "tests" / "test_mul.py").write_text("from src.foo import mul\n\n\ndef test_mul():\n    assert mul(3, 4) == 12\n")


def mutation_patch(wt: Path) -> str:
    src = wt / "src" / "foo.py"
    original = src.read_text()
    src.write_text(original.replace("return a * b", "return a + b"))
    patch = git(wt, "diff")
    src.write_text(original)
    return patch + "\n"


def make_tester_payload(kind: str, patch: str | None = None) -> dict:
    group = {"kind": kind, "behavior_ids": ["B1"], "argv": [*PYTEST_ARGV, "tests/test_mul.py"], "test_ids": ["tests/test_mul.py::test_mul"], "timeout": 60}
    if kind == "mutation":
        group["patch"] = patch
    if kind == "reviewed-boundaries":
        group["reviewed_boundaries"] = {"positive": ["tests/test_mul.py::test_mul"], "negative": [], "boundary": [], "invariant": []}
    return {"role": "tester", "status": "pass", "files": ["tests/test_mul.py"], "behaviors_covered": ["B1"], "proof_spec": {"groups": [group]}}


def marker(payload: dict) -> str:
    return "done\nBUILDER_LOOP_RESULT: " + json.dumps(payload)


def drive_to_proof_pass(started: dict, cli, hook) -> None:
    """builder 实现 → machine → tester(mutation) → machine → proof PASS。"""
    wt = started["worktree"]
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    write_mul_test(wt)
    payload = make_tester_payload("mutation", mutation_patch(wt))
    r = hook("SubagentStop", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester", "last_assistant_message": marker(payload)})
    assert r["code"] == 0, r
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    assert cli("proof", "--session", "S1")["result"] == "PASS"


def reviewer_pass(hook) -> None:
    hook("SubagentStart", {"session_id": "S1", "agent_id": "R1", "agent_type": "reviewer"})
    r = hook("SubagentStop", {"session_id": "S1", "agent_id": "R1", "agent_type": "reviewer", "last_assistant_message": marker({"role": "reviewer", "verdict": "pass", "findings": [], "behaviors_verified": ["B1"]})})
    assert r["code"] == 0, r
