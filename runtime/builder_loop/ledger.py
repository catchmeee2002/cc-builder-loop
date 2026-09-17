"""ledger：run 的唯一事实源。单写者（本模块 mutate），flock 串行化，seq 单调递增。

stale 不落盘（由 evidence 模块每次重算）；ledger 不保存"下一步让谁做"，也不保存"角色是否在跑"
（由 events + 心跳租约派生）。session 索引只是指针：读到后必须回核 ledger.session.owner_session_id。

events[] 只记没有别处归属的事实（角色生命周期、被拒的 checkpoint、integrate、用户输入、授权续跑、
stall 逃生、复盘）；machine / proof 失败、checkpoint、contract 修订各自已有归属，不在这里重复。
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

LEDGER_SCHEMA = "builder-loop/ledger@2"
LEGACY_SCHEMAS = ("builder-loop/ledger@1",)
RUNS_SUBDIR = Path(".claude") / "builder-loop" / "runs"
TERMINAL_STATUSES = ("finalized", "abandoned", "finalize_failed")

EVIDENCE_KINDS = ("machine", "tester", "proof", "reviewer")


def now_iso() -> str:
    # 微秒精度：readiness 靠时间戳字符串比较事件先后（固定格式 + UTC，字典序即时间序）
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


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
        "runtime_identity": None,
        "session": {"owner_session_id": None},
        "repo": {"root": None, "target_branch": None, "target_start_head": None},
        "candidate": {"branch": None, "worktree": None, "head": None, "checkpoints": []},
        "tester": None,
        "contract": None,
        "loop_config": None,
        "agents": {"tester": None, "reviewer": None},
        "evidence": {k: None for k in EVIDENCE_KINDS},
        "proof_spec": None,
        "failures": {"machine": [], "proof": []},
        "authorizations": [],
        "events": [],
        "counters": {"machine_iter": 0, "stall": {"seq_seen": 0, "count": 0}},
        "waiting_for_user": None,
        "finalize_intent": None,
        "terminal": None,
        "retrospective": None,
    }
    base.update(fields)
    return base


def validate(ledger: dict[str, Any], *, legacy_ok: bool = False) -> None:
    schema = ledger.get("schema")
    if schema in LEGACY_SCHEMAS:
        if legacy_ok:
            return
        raise fatal("LEDGER_LEGACY", f"ledger 是旧版 {schema}，本 runtime 只能列出或 abandon 它", schema=schema)
    if schema != LEDGER_SCHEMA:
        raise fatal("LEDGER_SCHEMA", f"ledger schema 不匹配: {schema}")
    for key in ("run_id", "seq", "session", "repo", "candidate", "contract", "loop_config", "agents", "evidence", "counters", "events", "authorizations"):
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


def peek(path: Path) -> dict[str, Any] | None:
    """不校验 schema 的只读读取，给 doctor / runs 列旧版与损坏 ledger 用。"""
    try:
        data = read_json(Path(path))
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def load(path: Path, *, legacy_ok: bool = False) -> dict[str, Any]:
    if not Path(path).is_file():
        raise fatal("LEDGER_NOT_FOUND", f"ledger 不存在: {path}", path=str(path))
    ledger = read_json(Path(path))
    validate(ledger, legacy_ok=legacy_ok)
    return ledger


def is_terminal(ledger: dict[str, Any]) -> bool:
    return ledger.get("terminal") is not None


def needs_retro(ledger: dict[str, Any]) -> bool:
    return is_terminal(ledger) and not ledger.get("retrospective")


def create(path: Path, ledger: dict[str, Any]) -> None:
    validate(ledger)
    if Path(path).exists():
        raise fatal("LEDGER_EXISTS", f"ledger 已存在: {path}", path=str(path))
    atomic_write_json(Path(path), ledger)


@contextmanager
def mutate(path: Path, *, legacy_ok: bool = False) -> Iterator[dict[str, Any]]:
    """独占锁内读-改-写。调用方在 with 体内直接改 dict；退出时校验、seq+1、原子写。"""
    path = Path(path)
    lock_path = path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            ledger = load(path, legacy_ok=legacy_ok)
            yield ledger
            ledger["seq"] = int(ledger.get("seq", 0)) + 1
            ledger["updated_at"] = now_iso()
            validate(ledger, legacy_ok=legacy_ok)
            atomic_write_json(path, ledger)
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def log_event(ledger: dict[str, Any], kind: str, **data: Any) -> dict[str, Any]:
    """在 mutate 上下文里调用。"""
    event = {"at": now_iso(), "kind": kind, **data}
    ledger.setdefault("events", []).append(event)
    return event


def events_of(ledger: dict[str, Any], *kinds: str) -> list[dict[str, Any]]:
    return [e for e in ledger.get("events", []) if e.get("kind") in kinds]


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
    """返回已核对的 {ledger_path, ledger, repo_root}；指针失效、旧版 ledger 或 owner 不匹配 → None。"""
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
        if not lp.is_file():
            continue
        lg = peek(lp)
        if lg is None:
            out.append({"run_id": d.name, "schema": None, "unreadable": True, "terminal": None, "ledger_path": str(lp)})
            continue
        out.append({
            "run_id": lg.get("run_id") or d.name,
            "schema": lg.get("schema"),
            "legacy": lg.get("schema") in LEGACY_SCHEMAS,
            "terminal": (lg.get("terminal") or {}).get("status"),
            "retro_pending": bool(lg.get("terminal")) and not lg.get("retrospective") and lg.get("schema") == LEDGER_SCHEMA,
            "owner_session_id": (lg.get("session") or {}).get("owner_session_id"),
            "candidate_head": (lg.get("candidate") or {}).get("head"),
            "ledger_path": str(lp),
        })
    return out
