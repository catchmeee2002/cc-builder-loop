"""generic / pytest proof 现场的端到端积木（link_paths 与 generic 对照运行两组行为共用）。

runner 不是 mock：场景在临时 git 仓里提交一个真实的 bash runner，退出码由它读现场决定
（`git symbolic-ref` 区分候选 worktree 与 detached 临时现场，src/foo.py 的内容区分是否打了 patch，
依赖目录 / 残留文件的状态区分 runtime 有没有按约定布置现场）。runner 把每次执行写成一行
`SITE key=value ...` 记进 run 之外的临时日志，测试据此判断 runtime 在哪个现场、按什么顺序执行了什么。
"""

from __future__ import annotations

import copy
import os
from pathlib import Path

from conftest import CONTRACT, implement_mul, make_tester_result, mutation_patch, role_turn, write_plan

# machine 不参与这些行为：用一条恒过的 stage，别让场景 runner 进 machine 门禁
_TRIVIAL_STAGE = (
    "pass_cmd:\n"
    "  - stage: unit\n"
    '    cmd: "python3 -c \'import sys; sys.exit(0)\'"\n'
    "    timeout: 60\n"
    "max_iterations: 5\n"
)

PYTEST_RUNNER = 'framework: pytest\ncmd: "bash scripts/pytest_wrap.sh"'
# {tests} 占位符让 runner 收到声明的那一个证据 test_id（generic 组的 argv 由 test_ids 拼出）
GENERIC_RUNNER = 'framework: generic\ncmd: "bash scripts/check_runner.sh {tests}"'

MARKER = "candidate-marker-9f3a"

# runner / wrapper 共用的 shell 片段：路径是符号链接 / 别的形态 / 不存在；依赖标记可读；
# 路径在 git status 里是否以未跟踪出现（符号链接不会被目录型 ignore 规则忽略，必须以 ?? 出现）
SHELL_FNS = (
    "link_state() {\n"
    '  if [ -L "$1" ]; then echo symlink; elif [ -e "$1" ]; then echo other; else echo absent; fi\n'
    "}\n"
    "marker_ok() {\n"
    f"  [ -f \"$1/marker\" ] && grep -q '{MARKER}' \"$1/marker\" 2>/dev/null\n"
    "}\n"
    "status_kind() {\n"
    '  if git status --porcelain --untracked-files=all 2>/dev/null | grep -qx "?? $1"; then echo untracked\n'
    "  else echo tracked-or-none; fi\n"
    "}\n"
)


def write_loop(repo, *, runner: str, link_paths: list[str] | None = None, ignore: tuple[str, ...] = ()) -> None:
    """写 loop.yml 与 .gitignore（不提交；调用方写完场景脚本后一次 commit_all）。

    ignore 里的目录写成**目录型**规则（`name/`）：git 因此不会把建立在这些路径上的符号链接当成
    被忽略的文件，链接会以 `?? name` 出现在 status 里——这正是 runtime 必须用 _under_link 排除的
    情形，plain `name` 会把链接一起忽略，那条排除逻辑就永远走不到。"""
    lines = [_TRIVIAL_STAGE.rstrip("\n"), "proof_runner:"]
    lines.extend("  " + ln for ln in runner.splitlines())
    if link_paths is not None:
        lines.append("  link_paths:")
        lines.extend(f'    - "{p}"' for p in link_paths)
    (repo.root / ".claude" / "loop.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    ignores = [".claude/builder-loop/", "__pycache__/", "*.pyc", *(f"{p}/" for p in ignore)]
    (repo.root / ".gitignore").write_text("".join(i + "\n" for i in ignores), encoding="utf-8")


def _runner_head(log: Path) -> str:
    return (
        "#!/usr/bin/env bash\n"
        'LOG="' + str(log) + '"\n'
        + SHELL_FNS
        # 候选现场在分支上；临时 worktree 是 detached HEAD —— 场景只观察反例现场
        + "if git symbolic-ref -q HEAD >/dev/null 2>&1; then exit 0; fi\n"
        "if grep -q 'return a \\* b' src/foo.py 2>/dev/null; then IMPL=original; else IMPL=mutated; fi\n"
        "RESIDUE=$( [ -e residue.txt ] && echo present || echo absent )\n"
    )


def install_runner(repo, log: Path, site_body: str) -> None:
    """场景 runner：候选现场直接 exit 0；反例现场执行 site_body（可用 $IMPL / $LOG / $1 / 上面的函数）。"""
    p = repo.root / "scripts" / "check_runner.sh"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_runner_head(log) + site_body.rstrip("\n") + "\n", encoding="utf-8")


def install_pytest_wrapper(repo, log: Path, node_modules: bool = False) -> None:
    """pytest 框架的 runner：每次被调用都记一行（含现场、实现状态与依赖链接可见性），再原样转交 pytest。"""
    p = repo.root / "scripts" / "pytest_wrap.sh"
    p.parent.mkdir(parents=True, exist_ok=True)
    extra = (' link=$(link_state node_modules)'
             ' marker="$(marker_ok node_modules && echo ok || echo missing)"'
             ' link_status=$(status_kind node_modules)') if node_modules else ""
    p.write_text(
        "#!/usr/bin/env bash\n"
        'LOG="' + str(log) + '"\n'
        + SHELL_FNS
        + "if grep -q 'return a \\* b' src/foo.py 2>/dev/null; then IMPL=original; else IMPL=mutated; fi\n"
        + 'echo "SITE cwd=$(pwd -P) impl=$IMPL' + extra + '" >> "$LOG"\n'
        + 'exec python3 -m pytest -p no:html "$@"\n',
        encoding="utf-8",
    )


def commit_scenario(repo, msg: str = "chore(fixture): [cr_id_skip] Proof scenario") -> None:
    repo.commit_all(msg)


def start_run(repo, cli, *, plan_name: str = "plan-scenario.md", contract: dict | None = None) -> dict:
    write_plan(repo.root, copy.deepcopy(contract if contract is not None else CONTRACT), plan_name)
    out = cli("start", "--plan", str(repo.root / plan_name), "--session", "S1")
    return {
        "worktree": Path(out["worktree"]),
        "candidate": os.path.realpath(out["worktree"]),
        "tester_worktree": Path(out["tester_worktree"]),
        "ledger": Path(out["ledger"]),
    }


def add_ignored_dep(ctx: dict, name: str) -> None:
    """候选 worktree 里被 .gitignore 忽略的目录（真实依赖的替身），放一个可辨认的标记文件。"""
    d = ctx["worktree"] / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "marker").write_text(MARKER + "\n", encoding="utf-8")


def prepare_site(ctx: dict, cli, hook, files: dict[str, str], *, test_id: str, kind: str = "mutation") -> None:
    """tester 盲写（先不带 patch）→ builder 实现 → checkpoint → integrate → machine PASS。"""
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(ctx["worktree"])
    cli("checkpoint", "--session", "S1", "--role", "builder")
    for rel, body in files.items():
        target = ctx["tester_worktree"] / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    r = role_turn(hook, "tester", "T1", make_tester_result(kind, test_ids=[test_id]), start=False)
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"


def deliver_patch(ctx: dict, hook, test_id: str, *, timeout: int | None = None) -> None:
    """续接 tester 交 patch（只把 mul 从乘积改成加法），让 mutation 组可执行。"""
    res = make_tester_result("mutation", mutation_patch(ctx["worktree"]), test_ids=[test_id])
    if timeout is not None:
        res["proof_spec"]["groups"][0]["timeout"] = timeout
    r = role_turn(hook, "tester", "T1", res)
    assert r["code"] == 0, r


def records(log: Path) -> list[dict[str, str]]:
    """runner 写下的 `SITE key=value ...` 行 → 记录列表（保持执行顺序）。"""
    out: list[dict[str, str]] = []
    if not log.exists():
        return out
    for line in log.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if not parts or parts[0] != "SITE":
            continue
        rec: dict[str, str] = {}
        for kv in parts[1:]:
            k, _, v = kv.partition("=")
            rec[k] = v
        out.append(rec)
    return out


def site_records(ctx: dict, log: Path) -> list[dict[str, str]]:
    """反例现场写下的记录，按执行顺序。

    generic runner 在候选阶段（分支上）直接 exit 0，从不写日志，所以没有 cwd 字段的记录一定来自
    反例现场；pytest wrapper 候选阶段也写日志，带 cwd，据此剔除。"""
    run_dir = os.path.realpath(ctx["ledger"].parent)
    out = []
    for r in records(log):
        if "cwd" not in r:
            out.append(r)
            continue
        cwd = os.path.realpath(r["cwd"])
        if cwd != ctx["candidate"] and cwd.startswith(run_dir + os.sep):
            out.append(r)
    return out


def candidate_records(ctx: dict, log: Path) -> list[dict[str, str]]:
    return [r for r in records(log) if os.path.realpath(r.get("cwd", "")) == ctx["candidate"]]


def snapshot(root: Path) -> dict[str, tuple]:
    """worktree 内容快照（跳过 .git 指针文件 / 目录）：证明候选现场在 proof 前后一致。"""
    out: dict[str, tuple] = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if rel == ".git" or rel.startswith(".git/"):
            continue
        if p.is_symlink():
            out[rel] = ("symlink", os.path.realpath(p))
        elif p.is_dir():
            out[rel] = ("dir",)
        else:
            out[rel] = ("file", p.read_bytes())
    return out


def proof_record(ledger: Path) -> dict:
    from builder_loop import ledger as L

    return L.load(ledger)["evidence"].get("proof") or {}


def recorded_groups(ledger: Path) -> list[dict]:
    return (proof_record(ledger).get("details") or {}).get("groups") or []
