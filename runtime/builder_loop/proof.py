"""proof：证明 tester 的测试有鉴别力。

顺序：① 所有 group 在候选（clean）上必须全绿 → ② baseline-red：起点 + tester 文件上必须 assertion
失败 → ③ mutation：候选上打 tester 提供的 patch（只能破坏 builder_write 内已有普通文件）后必须
assertion 失败，执行前后比对 diff 防篡改 → ④ reviewed-boundaries：四类 id 并集必须恰好等于 test_ids。
group ↔ mission.behaviors 双射。失败记 failure_signature，同签名三次由 readiness 报 PROOF_STALL。
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from . import contract as contract_mod
from . import evidence, gitx, ledger as ledger_mod, worktree
from .errors import Problem, fatal, negative
from .jsonutil import digest, sha256_bytes
from .machine import failure_signature

KIND_BASELINE = "baseline-red"
KIND_MUTATION = "mutation"
KIND_REVIEWED = "reviewed-boundaries"
DEFAULT_TIMEOUT = 300
MAX_TIMEOUT = 1800

PYTEST_FAMILY = ("pytest", "py.test")
PYTHON_BIN = ("python", "python3")
WEAK_COMMANDS = {"go": "test", "cargo": "test", "bazel": "test"}

_ERROR_MARKERS = re.compile(r"ImportError|ModuleNotFoundError|SyntaxError|ERROR (?:collecting|at setup)|no tests ran|collected 0 items|error: unrecognized|INTERNALERROR")
_ASSERT_MARKERS = re.compile(r"AssertionError|\bassert\b|FAILED .*::|Failure|--- FAIL|test result: FAILED|panicked at|FAILED in")


def _spec_error(msg: str, **details: Any) -> Problem:
    return Problem("PROOF_SPEC_INVALID", msg, details=details, exit_code=1)


# ---------------------------------------------------------------- spec 校验


def _command_family(argv: list[str]) -> str:
    if not argv:
        raise _spec_error("argv 不能为空")
    head = os.path.basename(argv[0])
    if head in PYTEST_FAMILY:
        return "pytest"
    if head in PYTHON_BIN and len(argv) >= 3 and argv[1] == "-m" and argv[2] in ("pytest", "unittest"):
        return "pytest" if argv[2] == "pytest" else "unittest"
    if head == "uv" and len(argv) >= 3 and argv[1] == "run" and os.path.basename(argv[2]) in PYTEST_FAMILY:
        return "pytest"
    if head in WEAK_COMMANDS and len(argv) >= 2 and argv[1] == WEAK_COMMANDS[head]:
        return head
    raise _spec_error("不支持的 proof 命令；允许 pytest / python -m pytest|unittest / uv run pytest / go test / cargo test / bazel test", argv=argv)


def patch_paths(patch: str) -> list[str]:
    paths: list[str] = []
    for line in patch.splitlines():
        m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
        if m:
            if m.group(1) != m.group(2):
                raise _spec_error("mutation patch 不允许重命名", line=line)
            paths.append(m.group(2))
    if not paths:
        for line in patch.splitlines():
            m = re.match(r"^\+\+\+ b/(\S+)", line)
            if m:
                paths.append(m.group(1))
    return sorted(set(paths))


def validate_spec(spec: Any, lg: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(spec, dict) or not isinstance(spec.get("groups"), list) or not spec["groups"]:
        raise _spec_error("proof_spec 必须含非空 groups 数组")
    c = lg["contract"]
    allowed = set(c["assurance"].get("proof_kinds") or [])
    auth = c["authority"]
    behaviors = {b["id"] for b in c["mission"]["behaviors"]}
    cand_head = lg["candidate"]["head"]
    repo_root = Path(lg["repo"]["root"])
    seen: list[str] = []

    for i, g in enumerate(spec["groups"]):
        where = f"groups[{i}]"
        if not isinstance(g, dict):
            raise _spec_error(f"{where} 必须是对象")
        kind = g.get("kind")
        if kind not in allowed:
            raise _spec_error(f"{where}.kind={kind} 不在允许范围", allowed=sorted(allowed))
        bids = g.get("behavior_ids")
        if not isinstance(bids, list) or len(bids) != 1 or bids[0] not in behaviors:
            raise _spec_error(f"{where}.behavior_ids 必须恰好含 1 个 mission behavior id", behaviors=sorted(behaviors))
        seen.append(bids[0])
        argv = g.get("argv")
        if not isinstance(argv, list) or not all(isinstance(x, str) and x for x in argv):
            raise _spec_error(f"{where}.argv 必须是非空字符串数组")
        family = _command_family(argv)
        tids = g.get("test_ids")
        if not isinstance(tids, list) or not tids or not all(isinstance(x, str) and x for x in tids):
            raise _spec_error(f"{where}.test_ids 必须是非空字符串数组")
        timeout = g.get("timeout", DEFAULT_TIMEOUT)
        if not isinstance(timeout, int) or not (1 <= timeout <= MAX_TIMEOUT):
            raise _spec_error(f"{where}.timeout 必须在 1..{MAX_TIMEOUT}")
        g["timeout"] = timeout
        if family in ("pytest", "unittest") and cand_head:
            files = sorted({t.split("::", 1)[0] for t in tids})
            existing = gitx.ls_tree_blobs(repo_root, cand_head, files) if files else {}
            for f in files:
                if not contract_mod.path_in(auth["tester_write"], f):
                    raise _spec_error(f"{where}.test_ids 引用了 tester_write 之外的文件 {f}")
                if f not in existing:
                    raise _spec_error(f"{where}.test_ids 引用的文件在候选上不存在: {f}")
        if kind == KIND_MUTATION:
            patch = g.get("patch")
            if not isinstance(patch, str) or not patch.strip():
                raise _spec_error(f"{where}.patch 缺失（mutation 必须附 unified diff）")
            ppaths = patch_paths(patch)
            if not ppaths:
                raise _spec_error(f"{where}.patch 无法识别改动路径（需要 `diff --git a/x b/x` 头）")
            modes = {p: gitx.blob_mode(repo_root, cand_head, p) for p in ppaths} if cand_head else {}
            for p in ppaths:
                if contract_mod.path_in(auth.get("protected_paths", []), p) or contract_mod.path_in(auth["tester_write"], p) or not contract_mod.path_in(auth["builder_write"], p):
                    raise _spec_error(f"{where}.patch 触及非 builder_write 路径 {p}", path=p)
                if modes.get(p) not in ("100644", "100755"):
                    raise _spec_error(f"{where}.patch 触及候选上不存在的普通文件 {p}", path=p, mode=modes.get(p))
            g["patch_paths"] = ppaths
        if kind == KIND_REVIEWED:
            rb = g.get("reviewed_boundaries")
            if not isinstance(rb, dict):
                raise _spec_error(f"{where}.reviewed_boundaries 缺失")
            union: set[str] = set()
            for k in ("positive", "negative", "boundary", "invariant"):
                v = rb.get(k, [])
                if not isinstance(v, list):
                    raise _spec_error(f"{where}.reviewed_boundaries.{k} 必须是数组")
                union.update(v)
            if union != set(tids):
                raise _spec_error(f"{where}.reviewed_boundaries 四类并集必须恰好等于 test_ids", missing=sorted(set(tids) - union), extra=sorted(union - set(tids)))
        g["family"] = family

    if sorted(seen) != sorted(behaviors) or len(seen) != len(set(seen)):
        raise _spec_error("groups 与 mission.behaviors 必须一一对应", covered=sorted(seen), expected=sorted(behaviors))
    return spec


# ---------------------------------------------------------------- 执行


def classify(family: str, rc: int, output: str) -> str:
    if rc == 0:
        return "pass"
    if family in ("pytest", "unittest"):
        if _ERROR_MARKERS.search(output):
            return "error"
        return "assertion-failure" if _ASSERT_MARKERS.search(output) else "error"
    return "assertion-failure" if _ASSERT_MARKERS.search(output) else "error"


def _run(cwd: Path, argv: list[str], timeout: int, log_path: Path, env: dict[str, str]) -> tuple[int, str]:
    with open(log_path, "wb") as log:
        try:
            proc = subprocess.run(argv, cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=timeout, env=env)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            rc = 124
            log.write(f"\n[TIMEOUT] 超过 {timeout}s\n".encode())
        except FileNotFoundError as exc:
            rc = 127
            log.write(f"\n[NOT FOUND] {exc}\n".encode())
    return rc, log_path.read_bytes().decode("utf-8", "replace")


class _Failure(Exception):
    def __init__(self, code: str, message: str, group: int, behavior: str, **details: Any):
        super().__init__(message)
        self.code, self.message, self.group, self.behavior, self.details = code, message, group, behavior, details


def _restore_worktree(wt: Path) -> tuple[list[str], list[str]]:
    """返回 (被修改的 tracked 路径, 被清理的 untracked 路径)。"""
    entries = gitx.status_porcelain(wt)
    tracked = [p for xy, p in entries if xy != "??"]
    untracked = [p for xy, p in entries if xy == "??"]
    if untracked:
        gitx.git(wt, "clean", "-fdq", check=False)
    if tracked:
        gitx.git(wt, "checkout", "--", ".", check=False)
    return tracked, untracked


def run_proof(ledger_path: Path, repo_root: Path, spec_override: dict[str, Any] | None = None) -> dict[str, Any]:
    lg = ledger_mod.load(ledger_path)
    if ledger_mod.is_terminal(lg):
        raise fatal("RUN_TERMINAL", "run 已到终态", terminal=lg["terminal"])
    if evidence.state(lg, "tester", repo_root) != evidence.STATE_PASS:
        raise negative("PROOF_PREREQ_TESTER", "tester evidence 不是 fresh pass，先完成 tester", state=evidence.state(lg, "tester", repo_root))
    spec = spec_override or lg.get("proof_spec")
    if not spec:
        raise negative("PROOF_SPEC_MISSING", "ledger 没有 proof_spec（tester 结果里应携带）")
    spec = validate_spec(spec, lg)
    cand = lg["candidate"]
    wt = Path(cand["worktree"])
    worktree.assert_candidate_identity(wt, cand["branch"], cand["head"])
    worktree.assert_candidate_clean(wt)

    run_dir = ledger_path.parent
    log_dir = run_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    attempt = len(lg["failures"]["proof"]) + 1
    env = dict(os.environ)
    env.update({"BUILDER_LOOP_RUN_ID": lg["run_id"], "BUILDER_LOOP_PROOF": "1"})
    tester_files = lg["evidence"]["tester"]["details"]["files"]
    groups_out: list[dict[str, Any]] = []
    failure: _Failure | None = None

    try:
        # ① 候选全绿
        for i, g in enumerate(spec["groups"]):
            log = log_dir / f"proof-{attempt}-g{i}-candidate.log"
            rc, out = _run(wt, g["argv"], g["timeout"], log, env)
            tracked, untracked = _restore_worktree(wt)
            entry = {"behavior_id": g["behavior_ids"][0], "kind": g["kind"], "candidate": {"returncode": rc, "classification": classify(g["family"], rc, out), "log": str(log), "residue_cleaned": untracked}}
            groups_out.append(entry)
            if tracked:
                raise _Failure("PROOF_WORKTREE_MUTATED", "测试命令修改了候选中的 tracked 文件", i, g["behavior_ids"][0], paths=tracked[:50], tail=out[-3000:])
            if rc != 0:
                raise _Failure("TEST_PROOF_CANDIDATE_FAILED", "测试在候选实现上未通过", i, g["behavior_ids"][0], returncode=rc, log=str(log), tail=out[-3000:])

        # ②③④ 反例
        for i, g in enumerate(spec["groups"]):
            bid = g["behavior_ids"][0]
            if g["kind"] == KIND_REVIEWED:
                groups_out[i]["counterexample"] = {"kind": KIND_REVIEWED, "outcome": "reviewed"}
                continue
            if g["kind"] == KIND_BASELINE:
                log = log_dir / f"proof-{attempt}-g{i}-baseline.log"
                with worktree.temp_worktree(repo_root, lg["repo"]["target_start_head"], run_dir / "tmp", f"baseline-g{i}", overlay=(cand["head"], tester_files)) as tw:
                    rc, out = _run(tw, g["argv"], g["timeout"], log, env)
                cls = classify(g["family"], rc, out)
                groups_out[i]["counterexample"] = {"kind": KIND_BASELINE, "returncode": rc, "classification": cls, "log": str(log)}
                if cls != "assertion-failure":
                    raise _Failure("TEST_BASELINE_RED_NOT_PROVEN", "起点 + tester 文件上没有产生 assertion 失败（pass 说明测试没约束行为；error 说明依赖了新接口，请改用 mutation）", i, bid, classification=cls, returncode=rc, log=str(log), tail=out[-3000:])
                continue
            # mutation
            log = log_dir / f"proof-{attempt}-g{i}-mutation.log"
            with worktree.temp_worktree(repo_root, cand["head"], run_dir / "tmp", f"mutation-g{i}") as tw:
                patch_text = g["patch"] if g["patch"].endswith("\n") else g["patch"] + "\n"
                ap = gitx.git(tw, "apply", "--whitespace=nowarn", "-", check=False, input_text=patch_text)
                if not ap.ok:
                    raise _Failure("TEST_MUTATION_INVALID", "mutation patch 无法应用到候选", i, bid, stderr=ap.stderr[-2000:])
                changed = sorted(p for _, p in gitx.status_porcelain(tw))
                if changed != g["patch_paths"]:
                    raise _Failure("TEST_MUTATION_INVALID", "patch 实际改动路径与声明不符", i, bid, declared=g["patch_paths"], actual=changed)
                diff_before = gitx.git(tw, "diff", "--binary", "--full-index").stdout
                rc, out = _run(tw, g["argv"], g["timeout"], log, env)
                diff_after = gitx.git(tw, "diff", "--binary", "--full-index").stdout
                after_paths = sorted(p for _, p in gitx.status_porcelain(tw) if not p.startswith(".pytest_cache"))
                if diff_after != diff_before or gitx.head(tw) != cand["head"] or [p for p in after_paths if p not in changed and not _ignorable(p)]:
                    raise _Failure("TEST_MUTATION_INVALID", "执行期间 mutation 现场被改动", i, bid, before=sha256_bytes(diff_before.encode()), after=sha256_bytes(diff_after.encode()), paths=after_paths)
            cls = classify(g["family"], rc, out)
            groups_out[i]["counterexample"] = {"kind": KIND_MUTATION, "returncode": rc, "classification": cls, "log": str(log), "patch_sha256": sha256_bytes(g["patch"].encode()), "patch_paths": g["patch_paths"]}
            if cls != "assertion-failure":
                raise _Failure("TEST_MUTATION_SURVIVED", "破坏实现后测试仍未产生 assertion 失败（pass=测试没约束该行为；error=命令本身出错）", i, bid, classification=cls, returncode=rc, log=str(log), tail=out[-3000:])
    except _Failure as exc:
        failure = exc

    spec_digest = digest(spec)
    if failure:
        tail = str(failure.details.get("tail", ""))
        sig = failure_signature(f"{failure.code}\n{failure.behavior}\n{failure.details.get('classification', '')}\n{tail}", f"proof-{failure.code}", int(failure.details.get("returncode", -1) or -1))
        fdetails = {k: v for k, v in failure.details.items() if k != "tail"}
        owner = "tester" if failure.code in ("TEST_BASELINE_RED_NOT_PROVEN", "TEST_MUTATION_SURVIVED", "TEST_MUTATION_INVALID") else "builder"
        fdetails["suggested_owner"] = owner
        with ledger_mod.mutate(ledger_path) as lg2:
            lg2["failures"]["proof"].append({"code": failure.code, "behavior": failure.behavior, "signature": sig, "at": ledger_mod.now_iso(), "attempt": attempt})
            evidence.record(lg2, "proof", "fail", {"attempt": attempt, "spec_digest": spec_digest, "groups": groups_out, "failure": {"code": failure.code, "message": failure.message, "group": failure.group, "behavior": failure.behavior, **fdetails, "signature": sig}}, repo_root)
            readiness = evidence.readiness(lg2, repo_root)
        repeats = sum(1 for f in lg2["failures"]["proof"] if f["signature"] == sig)
        return {"result": "FAIL", "attempt": attempt, "failure": {"code": failure.code, "message": failure.message, "group": failure.group, "behavior": failure.behavior, "signature": sig, "repeat_count": repeats, "suggested_owner": owner, **fdetails}, "groups": groups_out, "readiness": readiness}

    with ledger_mod.mutate(ledger_path) as lg2:
        evidence.record(lg2, "proof", "pass", {"attempt": attempt, "spec_digest": spec_digest, "groups": groups_out}, repo_root)
        readiness = evidence.readiness(lg2, repo_root)
    return {"result": "PASS", "attempt": attempt, "groups": groups_out, "readiness": readiness}


def _ignorable(path: str) -> bool:
    return path.startswith((".pytest_cache", "__pycache__")) or "/__pycache__/" in path or path.endswith(".pyc")
