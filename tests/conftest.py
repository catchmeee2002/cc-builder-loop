"""pytest fixtures：临时 git 仓 + 隔离的 BUILDER_LOOP_HOME + CLI / hook 调用辅助。"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
sys.path.insert(0, str(RUNTIME))

# 本机全局 pytest-html 插件是坏的，所有 pytest 调用都要 -p no:html
PYTEST_CMD = "python3 -m pytest -p no:html -p no:cacheprovider -q tests"
PROOF_RUNNER_CMD = "python3 -m pytest -p no:html"

CONTRACT = {
    "schema": "builder-loop/contract@1",
    "mission": {
        "revision": 1,
        "slug": "add-mul",
        "objective": "Add mul()",
        "behaviors": [{"id": "B1", "given": "two ints", "when": "mul(a,b)", "then": "returns product", "boundaries": ["a or b is 0"], "invariants": ["add() unchanged"]}],
        "interfaces": ["src/foo.py::mul(a:int, b:int) -> int"],
    },
    "authority": {"builder_write": ["src/**"], "tester_write": ["tests/**"], "protected_paths": [".claude/loop.yml"]},
    "assurance": {"required": ["machine", "tester", "proof", "reviewer"]},
}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()


def write_plan(repo: Path, contract: dict, name: str = "plan.md") -> Path:
    p = repo / name
    p.write_text("# plan\n<!-- builder-loop-contract -->\n```json\n" + json.dumps(contract) + "\n```\n<!-- /builder-loop-contract -->\n", encoding="utf-8")
    return p


def contract_with(**patches) -> dict:
    """CONTRACT 的深拷贝 + 点路径补丁，如 contract_with(**{"assurance.required": ["machine"]})。"""
    c = copy.deepcopy(CONTRACT)
    for dotted, value in patches.items():
        node = c
        keys = dotted.split(".")
        for k in keys[:-1]:
            node = node[k]
        node[keys[-1]] = value
    return c


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
    git(root, "config", "user.email", "t@t")
    git(root, "config", "user.name", "t")
    (root / "src" / "__init__.py").write_text("")
    (root / "src" / "foo.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "tests" / "test_foo.py").write_text("from src.foo import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    (root / ".claude" / "loop.yml").write_text(
        f"pass_cmd:\n  - stage: test\n    cmd: \"{PYTEST_CMD}\"\n    timeout: 60\nmax_iterations: 3\n"
        f"proof_runner:\n  framework: pytest\n  cmd: \"{PROOF_RUNNER_CMD}\"\n"
    )
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
        ns = build_parser().parse_args(["--repo", str(repo.root), *args])
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
    return {
        "repo": repo, "run_id": out["run_id"], "ledger": Path(out["ledger"]),
        "worktree": Path(out["worktree"]), "tester_worktree": Path(out["tester_worktree"]),
        "target_start_head": out["target_start_head"],
    }


# ---------------------------------------------------------------- 场景积木


def implement_mul(wt: Path) -> None:
    (wt / "src" / "foo.py").write_text("def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")


def write_mul_test(tester_wt: Path, body: str = "assert mul(3, 4) == 12") -> None:
    (tester_wt / "tests" / "test_mul.py").write_text(f"from src.foo import mul\n\n\ndef test_mul():\n    {body}\n")


def mutation_patch(candidate_wt: Path) -> str:
    src = candidate_wt / "src" / "foo.py"
    original = src.read_text()
    src.write_text(original.replace("return a * b", "return a + b"))
    patch = git(candidate_wt, "diff")
    src.write_text(original)
    return patch  # 故意不带末尾换行：runtime 要能容忍


def write_patch_file(patch: str, directory: Path | None = None, name: str = "mutation.patch") -> Path:
    """把 patch 文本原样（不追加/改动任何字节）写到 run 外的临时目录，返回绝对路径。
    result-channel run：mutation 组用 patch_file 交付大段 diff，不把正文塞进 handback 消息。"""
    d = directory or Path(tempfile.mkdtemp(prefix="bl-patch-"))
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(patch.encode("utf-8"))
    return p


def make_tester_result(kind: str = "mutation", patch: str | None = None, test_ids: list[str] | None = None,
                       behavior: str = "B1", patch_file: str | None = None) -> dict:
    """result-channel run（B1/B2）：候选可读之后 mutation 的 patch 只能走 patch_file 交付，inline
    "patch" 字段会被 PreToolUse 拒。非空 patch 一律落成 run 外的临时文件、以 patch_file 交付，
    与老调用方（直接传一段 patch 文本）保持源码兼容；显式传 patch_file 时优先于 patch。"""
    ids = test_ids or ["tests/test_mul.py::test_mul"]
    group: dict = {"kind": kind, "behavior_ids": [behavior], "test_ids": ids, "timeout": 60}
    if patch_file is not None:
        group["patch_file"] = patch_file
    elif patch:
        group["patch_file"] = str(write_patch_file(patch))
    elif patch is not None:
        group["patch"] = patch  # 空字符串：保留原样，等价于缺省 patch
    if kind == "reviewed-boundaries":
        group["reviewed_boundaries"] = {"positive": ids, "negative": [], "boundary": [], "invariant": []}
    return {"role": "tester", "status": "pass", "behaviors_covered": [behavior], "proof_spec": {"groups": [group]}}


def marker(payload: dict) -> str:
    return "done\nBUILDER_LOOP_RESULT: " + json.dumps(payload)


def send_message(hook, to_agent_id: str, *, session: str = "S1"):
    """主会话（无 agent_id）发出的 PreToolUse(SendMessage)，用于续接已登记角色 to_agent_id。
    B2：SubagentStart 之前先喂这一条，续接才会记 resume_request + role_start（而不是 role_wake）。"""
    return hook("PreToolUse", {"session_id": session, "tool_name": "SendMessage", "tool_input": {"to": to_agent_id}})


def pre_handback(hook, role: str, agent_id: str, message: str, *, session: str = "S1"):
    """result-channel run：SubagentHandback 现在也走 PreToolUse（投递前的 gate），与 PostToolUse 的
    handback() 成对使用——先 Pre 再 Post，Pre deny 时不应再有 Post 登记。"""
    return hook("PreToolUse", {"session_id": session, "agent_id": agent_id, "agent_type": role,
                               "tool_name": "SubagentHandback", "tool_input": {"message": message}})


def handback(hook, role: str, agent_id: str, message: str, *, session: str = "S1"):
    """CC 2.1.273：角色用 SubagentHandback 工具把报告交回调用方，结果以它的 message 为准。"""
    return hook("PostToolUse", {
        "session_id": session, "agent_id": agent_id, "agent_type": role, "tool_name": "SubagentHandback",
        "tool_input": {"message": message},
        "tool_response": {"success": True, "message": "Report delivered to your caller."},
    })


def role_turn(hook, role: str, agent_id: str, payload: dict | None, *, start: bool = True, message: str | None = None):
    """模拟一个角色 turn：SubagentStart（首轮或续接都会触发）+ SubagentHandback 交结果。

    真实续接是主会话先 SendMessage 再触发 SubagentStart（B2）；这里在 start=True 时无条件先发一条
    send_message：agent_id 还没登记过（真正的首次 spawn）时它是没有效果的 no-op（B2 边界：to 不是已登记
    agent_id 不记 resume_request），已登记时它才会让随后的 SubagentStart 记成新一轮而不是 role_wake。"""
    if start:
        send_message(hook, agent_id)
        hook("SubagentStart", {"session_id": "S1", "agent_id": agent_id, "agent_type": role})
    text = message if message is not None else marker(payload)
    return handback(hook, role, agent_id, text)


def drive_to_proof_pass(started: dict, cli, hook) -> None:
    """两段式全流程：tester 先后台盲写（patch 留空）→ builder 实现 → integrate → machine → 续接补 patch → proof。"""
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    assert r["code"] == 0, r
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "integrate"
    cli("integrate", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "resume_tester"  # 缺 patch
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation", mutation_patch(wt)))
    assert r["code"] == 0, r
    out = cli("proof", "--session", "S1")
    assert out["result"] == "PASS", out


def reviewer_pass(hook, agent_id: str = "R1") -> None:
    r = role_turn(hook, "reviewer", agent_id, {"role": "reviewer", "verdict": "pass", "findings": [], "behaviors_verified": ["B1"]})
    assert r["code"] == 0, r
