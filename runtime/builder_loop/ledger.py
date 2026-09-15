"""ledger：run 的唯一事实源。单写者（本模块 mutate），flock 串行化，seq 单调递增。

stale 不落盘（由 evidence 模块每次重算）；ledger 不保存"下一步让谁做"。
session 索引只是指针：读到后必须回核 ledger.session.owner_session_id。
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .errors import fatal
from .jsonutil import atomic_write_json, read_json

LEDGER_SCHEMA = "builder-loop/ledger@1"
RUNS_SUBDIR = Path(".claude") / "builder-loop" / "runs"
TERMINAL_STATUSES = ("finalized", "abandoned", "finalize_failed")

EVIDENCE_KINDS = ("machine", "tester", "proof", "reviewer")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def home_dir() -> Path:
    override = os.environ.get("BUILDER_LOOP_HOME")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "builder-loop"


def sessions_dir() -> Path:
    return home_dir() / "sessions"


def run_dir(repo_root: Path, run_id: str) -> Path:
    return repo_root / RUNS_SUBDIR / run_id


def ledger_path(repo_root: Path, run_id: str) -> Path:
    return run_dir(repo_root, run_id) / "ledger.json"


def new_ledger(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema": LEDGER_SCHEMA,
        "run_id": None,
        "seq": 0,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "session": {"owner_session_id": None},
        "repo": {"root": None, "target_branch": None, "target_start_head": None},
        "candidate": {"branch": None, "worktree": None, "head": None, "checkpoints": []},
        "contract": None,
        "loop_config": None,
        "agents": {"tester": None, "reviewer": None},
        "evidence": {k: None for k in EVIDENCE_KINDS},
        "proof_spec": None,
        "failures": {"machine": [], "proof": []},
        "counters": {"machine_iter": 0, "stall": {"seq_seen": 0, "count": 0}},
        "waiting_for_user": None,
        "finalize_intent": None,
        "terminal": None,
    }
    base.update(fields)
    return base


def validate(ledger: dict[str, Any]) -> None:
    if ledger.get("schema") != LEDGER_SCHEMA:
        raise fatal("LEDGER_SCHEMA", f"ledger schema 不匹配: {ledger.get('schema')}")
    for key in ("run_id", "seq", "session", "repo", "candidate", "contract", "loop_config", "agents", "evidence", "counters"):
        if key not in ledger:
            raise fatal("LEDGER_INVALID", f"ledger 缺少字段 {key}")
    if not isinstance(ledger["seq"], int):
        raise fatal("LEDGER_INVALID", "seq 必须是整数")
    ev = ledger["evidence"]
    for k in EVIDENCE_KINDS:
        if k not in ev:
            raise fatal("LEDGER_INVALID", f"evidence 缺少 {k}")
    term = ledger.get("terminal")
    if term is not None and term.get("status") not in TERMINAL_STATUSES:
        raise fatal("LEDGER_INVALID", f"未知终态 {term.get('status')}")


def load(path: Path) -> dict[str, Any]:
    if not Path(path).is_file():
        raise fatal("LEDGER_NOT_FOUND", f"ledger 不存在: {path}", path=str(path))
    ledger = read_json(Path(path))
    validate(ledger)
    return ledger


def is_terminal(ledger: dict[str, Any]) -> bool:
    return ledger.get("terminal") is not None


def create(path: Path, ledger: dict[str, Any]) -> None:
    validate(ledger)
    if Path(path).exists():
        raise fatal("LEDGER_EXISTS", f"ledger 已存在: {path}", path=str(path))
    atomic_write_json(Path(path), ledger)


@contextmanager
def mutate(path: Path) -> Iterator[dict[str, Any]]:
    """独占锁内读-改-写。调用方在 with 体内直接改 dict；退出时校验、seq+1、原子写。"""
    path = Path(path)
    lock_path = path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            ledger = load(path)
            yield ledger
            ledger["seq"] = int(ledger["seq"]) + 1
            ledger["updated_at"] = now_iso()
            validate(ledger)
            atomic_write_json(path, ledger)
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------- session 索引


def _session_file(session_id: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in session_id)
    return sessions_dir() / f"{safe}.json"


def bind_session(session_id: str, repo_root: Path, run_id: str) -> None:
    atomic_write_json(_session_file(session_id), {
        "session_id": session_id,
        "repo_root": str(repo_root),
        "run_id": run_id,
        "ledger_path": str(ledger_path(repo_root, run_id)),
        "bound_at": now_iso(),
    })


def unbind_session(session_id: str) -> None:
    try:
        _session_file(session_id).unlink()
    except FileNotFoundError:
        pass


def lookup_session(session_id: str | None) -> dict[str, Any] | None:
    """返回已核对的 {ledger_path, ledger}；指针失效或 owner 不匹配 → None。"""
    if not session_id:
        return None
    f = _session_file(session_id)
    if not f.is_file():
        return None
    try:
        ptr = read_json(f)
        ledger = load(Path(ptr["ledger_path"]))
    except Exception:  # noqa: BLE001 — 指针腐坏视同无绑定
        return None
    if ledger["session"].get("owner_session_id") != session_id:
        return None
    return {"ledger_path": Path(ptr["ledger_path"]), "ledger": ledger, "repo_root": Path(ptr["repo_root"])}


def find_ledger_by_run(repo_root: Path, run_id: str) -> Path:
    p = ledger_path(repo_root, run_id)
    if not p.is_file():
        raise fatal("RUN_NOT_FOUND", f"run {run_id} 不存在", run_id=run_id)
    return p


def list_runs(repo_root: Path) -> list[dict[str, Any]]:
    base = repo_root / RUNS_SUBDIR
    out: list[dict[str, Any]] = []
    if not base.is_dir():
        return out
    for d in sorted(base.iterdir()):
        lp = d / "ledger.json"
        if lp.is_file():
            try:
                lg = load(lp)
            except Exception:  # noqa: BLE001
                continue
            out.append({
                "run_id": lg["run_id"],
                "terminal": (lg.get("terminal") or {}).get("status"),
                "owner_session_id": lg["session"].get("owner_session_id"),
                "candidate_head": lg["candidate"].get("head"),
                "ledger_path": str(lp),
            })
    return out
