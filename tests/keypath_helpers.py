"""本批行为（关键路径加速）测试共用的积木：loop.yml 文本、子进程持锁、tester 结果拼装。

不是 test_ 开头，不会被 pytest 收集。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from builder_loop import ledger as L

PYTEST_CMD = "python3 -m pytest -p no:html -p no:cacheprovider -q tests"
PROOF_CMD = "python3 -m pytest -p no:html"
TRIVIAL_CMD = "python3 -c 'import sys; sys.exit(0)'"


def loop_yml(*, gates: int | None = None, proof_groups: int | None = None,
             preflight: str | None = None, stage_cmd: str | None = None) -> str:
    """拼一份合法的 loop.yml；只有显式给出的新键才写进去（复核「不写新键时形状不变」）。"""
    lines = [
        "pass_cmd:",
        "  - stage: test",
        f'    cmd: "{stage_cmd or PYTEST_CMD}"',
        "    timeout: 120",
        "max_iterations: 5",
    ]
    if preflight is not None:
        lines.append(f"preflight: {preflight}")
    if gates is not None or proof_groups is not None:
        lines.append("concurrency:")
        if gates is not None:
            lines.append(f"  gates: {gates}")
        if proof_groups is not None:
            lines.append(f"  proof_groups: {proof_groups}")
    lines += ["proof_runner:", "  framework: pytest", f'  cmd: "{PROOF_CMD}"']
    return "\n".join(lines) + "\n"


def start_with_loop(repo, cli, loop_text: str, *, plan_path: Path | None = None,
                    plan_name: str = "plan-keypath.md", session: str = "S1") -> dict:
    """写 loop.yml 并提交（start 从目标提交冻结它），再起 run。"""
    (repo.root / ".claude" / "loop.yml").write_text(loop_text, encoding="utf-8")
    repo.commit_all("chore(fixture): [cr_id_skip] Keypath loop config")
    if plan_path is None:
        write_plan_if_missing(repo)
        plan_path = repo.root / "plan.md"
    out = cli("start", "--plan", str(plan_path), "--session", session)
    return {
        "repo": repo,
        "run_id": out["run_id"],
        "ledger": Path(out["ledger"]),
        "worktree": Path(out["worktree"]),
        "tester_worktree": Path(out["tester_worktree"]),
    }


def write_plan_if_missing(repo) -> None:
    from conftest import CONTRACT, write_plan

    path = repo.root / "plan.md"
    if not path.exists():
        write_plan(repo.root, CONTRACT)


_CHILD_HOLD = r'''
import fcntl, os, sys, time
from pathlib import Path
lock, ready, seconds = sys.argv[1], sys.argv[2], float(sys.argv[3])
fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
Path(ready).write_text("1", encoding="utf-8")
try:
    time.sleep(seconds)
finally:
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
'''


def hold_gate(ledger: dict, holder: str, seconds: float, ready_dir: Path) -> subprocess.Popen:
    """在子进程里以 flock 持有某个门禁锁；返回已确认拿到锁的 Popen。"""
    lock = L.run_dir(Path(ledger["repo"]["root"]), ledger["run_id"]) / f"gate-{holder}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    ready = ready_dir / f"ready-{holder}.flag"
    proc = subprocess.Popen([sys.executable, "-c", _CHILD_HOLD, str(lock), str(ready), str(seconds)])
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if ready.exists():
            return proc
        if proc.poll() is not None:
            raise RuntimeError(f"持锁子进程提前退出（code {proc.returncode}）")
        time.sleep(0.01)
    proc.kill()
    raise RuntimeError("未能确认持锁子进程已经拿到锁")


def release_hold(proc: subprocess.Popen, timeout: float = 15) -> None:
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:  # 不给测试留悬挂子进程
        proc.kill()
        proc.wait(timeout=5)


def build_tester_payload(groups: list[dict], behaviors: list[str]) -> dict:
    return {
        "role": "tester",
        "status": "pass",
        "behaviors_covered": behaviors,
        "proof_spec": {"groups": groups},
    }