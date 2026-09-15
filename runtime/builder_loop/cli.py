"""CLI：stdout 恒为单个 JSON；退出码 0 成功 / 1 判据为负 / 2 配置或用法错误 / 3 需用户决策。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import ledger as ledger_mod
from .errors import EXIT_FATAL, EXIT_OK, Problem
from .jsonutil import dumps


def _emit(obj: Any) -> None:
    sys.stdout.write(dumps(obj) + "\n")
    sys.stdout.flush()


def _locate(args: argparse.Namespace) -> tuple[Path, Path]:
    """返回 (ledger_path, repo_root)。优先 --run，其次 --session。"""
    from .run import resolve_repo_root

    if getattr(args, "run", None):
        repo_root = resolve_repo_root(args.repo)
        return ledger_mod.find_ledger_by_run(repo_root, args.run), repo_root
    if getattr(args, "session", None):
        bound = ledger_mod.lookup_session(args.session)
        if not bound:
            raise Problem("SESSION_UNBOUND", "该 session 没有绑定 run", exit_code=EXIT_FATAL)
        return bound["ledger_path"], bound["repo_root"]
    repo_root = resolve_repo_root(args.repo)
    runs = [r for r in ledger_mod.list_runs(repo_root) if not r["terminal"]]
    if len(runs) == 1:
        return Path(runs[0]["ledger_path"]), repo_root
    raise Problem("RUN_AMBIGUOUS", "请用 --run 或 --session 指定 run", details={"active_runs": runs}, exit_code=EXIT_FATAL)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bl", description="builder-loop V8 runtime")
    p.add_argument("--repo", help="仓库路径（默认当前目录所在主仓）")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_locators(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--run", help="run id")
        sp.add_argument("--session", help="CC session id")

    sp = sub.add_parser("start", help="从 plan 冻结 contract、创建候选 worktree")
    sp.add_argument("--plan", required=True)
    sp.add_argument("--session", required=True)
    sp.add_argument("--target", help="目标分支（默认 contract.authority.target_branch 或当前分支）")

    sp = sub.add_parser("status"); add_locators(sp)
    sp = sub.add_parser("checkpoint"); add_locators(sp)
    sp.add_argument("--role", required=True, choices=["builder", "tester"])
    sp.add_argument("-m", "--message")
    sp = sub.add_parser("machine"); add_locators(sp)
    sp = sub.add_parser("proof"); add_locators(sp)
    sp.add_argument("--spec-file", help="覆盖 ledger.proof_spec（调试用）")
    sp = sub.add_parser("finalize"); add_locators(sp)
    sp.add_argument("-m", "--message")
    sp.add_argument("--run-commit-hook", action="store_true")
    sp = sub.add_parser("rebase"); add_locators(sp)
    sp = sub.add_parser("abandon"); add_locators(sp)
    sp.add_argument("--reason", required=True)

    ev = sub.add_parser("evidence"); add_locators(ev)
    evs = ev.add_subparsers(dest="evidence_cmd", required=True)
    e1 = evs.add_parser("show")
    e2 = evs.add_parser("record", help="hook 内部用")
    e2.add_argument("--kind", required=True, choices=["tester", "reviewer"])
    e2.add_argument("--agent-id", required=True)
    e2.add_argument("--payload-file", required=True)

    ct = sub.add_parser("contract"); add_locators(ct)
    cts = ct.add_subparsers(dest="contract_cmd", required=True)
    c1 = cts.add_parser("validate"); c1.add_argument("--plan", required=True)
    c2 = cts.add_parser("revise"); c2.add_argument("--plan", required=True); c2.add_argument("--authorize", action="store_true")

    hk = sub.add_parser("hook", help="stdin 读 CC hook JSON")
    hk.add_argument("event")

    sub.add_parser("doctor")
    sub.add_parser("runs", help="列出本仓 run")
    return p


def dispatch(args: argparse.Namespace) -> tuple[Any, int]:
    from . import run as run_mod

    cmd = args.cmd
    if cmd == "start":
        repo_root = run_mod.resolve_repo_root(args.repo)
        return run_mod.start(repo_root, Path(args.plan).resolve(), args.session, args.target), EXIT_OK
    if cmd == "status":
        lp, root = _locate(args)
        return run_mod.status(lp, root), EXIT_OK
    if cmd == "checkpoint":
        lp, root = _locate(args)
        return run_mod.checkpoint(lp, root, args.role, args.message), EXIT_OK
    if cmd == "machine":
        from .machine import run_machine

        lp, root = _locate(args)
        out = run_machine(lp, root)
        code = EXIT_OK if out["result"] == "PASS" else 1
        if out["readiness"]["blockers"]:
            code = 3
        return out, code
    if cmd == "proof":
        from .proof import run_proof

        lp, root = _locate(args)
        spec = None
        if args.spec_file:
            spec = json.loads(Path(args.spec_file).read_text(encoding="utf-8"))
        out = run_proof(lp, root, spec_override=spec)
        code = EXIT_OK if out["result"] == "PASS" else 1
        if out.get("readiness", {}).get("blockers"):
            code = 3
        return out, code
    if cmd == "finalize":
        from .finalize import finalize

        lp, root = _locate(args)
        return finalize(lp, root, args.message, run_commit_hook=args.run_commit_hook), EXIT_OK
    if cmd == "rebase":
        from .finalize import rebase_candidate

        lp, root = _locate(args)
        out = rebase_candidate(lp, root)
        return out, (EXIT_OK if not out.get("conflicts") else 1)
    if cmd == "abandon":
        lp, _ = _locate(args)
        return run_mod.abandon(lp, args.reason), EXIT_OK
    if cmd == "evidence":
        lp, root = _locate(args)
        if args.evidence_cmd == "show":
            from . import evidence

            lg = ledger_mod.load(lp)
            return {"evidence": lg["evidence"], "readiness": evidence.readiness(lg, root)}, EXIT_OK
        from .hooks import record_role_result

        payload = json.loads(Path(args.payload_file).read_text(encoding="utf-8"))
        return record_role_result(lp, root, args.kind, args.agent_id, payload), EXIT_OK
    if cmd == "contract":
        if args.contract_cmd == "validate":
            return run_mod.contract_validate(Path(args.plan).resolve()), EXIT_OK
        lp, root = _locate(args)
        return run_mod.contract_revise(lp, root, Path(args.plan).resolve(), args.authorize), EXIT_OK
    if cmd == "hook":
        from .hooks import handle_hook

        return handle_hook(args.event, sys.stdin.read())
    if cmd == "doctor":
        from .doctor import doctor

        return doctor(args.repo), EXIT_OK
    if cmd == "runs":
        root = run_mod.resolve_repo_root(args.repo)
        return {"runs": ledger_mod.list_runs(root)}, EXIT_OK
    raise Problem("USAGE", f"未知命令 {cmd}", exit_code=EXIT_FATAL)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        out, code = dispatch(args)
    except Problem as exc:
        if args.cmd == "hook":
            # hook 路径：错误只进 stderr，绝不阻塞会话
            sys.stderr.write(f"[builder-loop hook] {exc.code}: {exc.message}\n")
            return EXIT_OK
        _emit(exc.to_json())
        return exc.exit_code
    if args.cmd == "hook":
        # hooks.handle_hook 自己决定 stdout/stderr 与退出码
        stdout_text, stderr_text = out
        if stdout_text:
            sys.stdout.write(stdout_text)
        if stderr_text:
            sys.stderr.write(stderr_text)
        return code
    if isinstance(out, dict) and "ok" not in out:
        out = {"ok": code in (EXIT_OK, 1), **out}
    _emit(out)
    return code
