"""proof：证明 tester 的测试有鉴别力。

测试命令不由 tester 写：项目在 loop.yml 声明 `proof_runner`，start 时冻结进 assurance 面（计入 digest），
tester 只给 test_ids，runtime 拼 argv（#229）。pytest 框架下结果取自 junit xml 的逐用例状态，不扫 stdout。

两段式：tester 首轮在冻结基线上盲写测试，那时看不到实现，mutation 组的 patch 可以缺省；
集成之后读隔离解除，由 readiness 引导续接同一 tester 补 patch。

执行顺序：① 所有 group 在候选（clean）上逐 id 全部 passed（skipped / xfail / 缺失都不算）→
② baseline-red：run 起点 + tester 文件上必须 assertion 失败 → ③ mutation：候选上打 patch（只能破坏
builder 拥有的已有普通文件）后必须 assertion 失败，执行前后比对 diff 防篡改 →
④ reviewed-boundaries：四类 id 并集恰好等于 test_ids，且该 behavior 在 contract 里显式放行了最弱 kind。
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from . import contract as contract_mod
from . import evidence, gitx, ledger as ledger_mod, worktree
from .errors import Problem, fatal, needs_user, negative
from .jsonutil import digest, sha256_bytes
from .machine import failure_signature

KIND_BASELINE = "baseline-red"
KIND_MUTATION = "mutation"
KIND_REVIEWED = "reviewed-boundaries"
DEFAULT_TIMEOUT = 300
MAX_TIMEOUT = 1800
ASSERTION_PREFIXES = ("AssertionError", "assert ", "Failed:")
TESTER_OWNED_FAILURES = ("TEST_BASELINE_RED_NOT_PROVEN", "TEST_MUTATION_SURVIVED", "TEST_MUTATION_INVALID", "TEST_MUTATION_PATCH_MISSING", "TEST_PROOF_NOT_EXECUTED")
# 失败归谁修。缺省 builder（实现没让测试过）；runner 起不来两个角色都改不了，归 contract → 改 loop.yml
OWNER_BY_FAILURE = {**{c: "tester" for c in TESTER_OWNED_FAILURES}, "TEST_PROOF_RUNNER_FAILED": "contract"}


def _spec_error(msg: str, **details: Any) -> Problem:
    return Problem("PROOF_SPEC_INVALID", msg, details=details, exit_code=1)


# ---------------------------------------------------------------- spec 校验（tester 交卷时即可做，不依赖候选）


def patch_paths(patch: str) -> list[str]:
    paths: list[str] = []
    for line in patch.splitlines():
        m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
        if m:
            if m.group(1) != m.group(2):
                raise _spec_error("mutation patch 不允许重命名", line=line)
            paths.append(m.group(2))
    if not paths:
        paths = [m.group(1) for line in patch.splitlines() if (m := re.match(r"^\+\+\+ b/(\S+)", line))]
    return sorted(set(paths))


def missing_patch_groups(spec: dict[str, Any] | None) -> list[str]:
    if not spec:
        return []
    return [g["behavior_ids"][0] for g in spec.get("groups", []) if g.get("kind") == KIND_MUTATION and not (g.get("patch") or "").strip()]


def validate_spec(spec: Any, lg: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    if not isinstance(spec, dict) or not isinstance(spec.get("groups"), list) or not spec["groups"]:
        raise _spec_error("proof_spec 必须含非空 groups 数组")
    c = lg["contract"]
    allowed = set(c["assurance"].get("proof_kinds") or [])
    framework = (c["assurance"].get("proof_runner") or {}).get("framework", "pytest")
    floors = {b["id"]: b.get("proof", contract_mod.PROOF_FLOOR_STRONG) for b in c["mission"]["behaviors"]}
    auth = c["authority"]
    tester_head = (lg.get("tester") or {}).get("head")
    seen: list[str] = []

    for i, g in enumerate(spec["groups"]):
        where = f"groups[{i}]"
        if not isinstance(g, dict):
            raise _spec_error(f"{where} 必须是对象")
        if "argv" in g:
            raise _spec_error(f"{where}.argv 不再接受：测试命令由 loop.yml 的 proof_runner 决定，你只需要给 test_ids")
        kind = g.get("kind")
        if kind not in allowed:
            raise _spec_error(f"{where}.kind={kind} 不在允许范围", allowed=sorted(allowed))
        bids = g.get("behavior_ids")
        if not isinstance(bids, list) or len(bids) != 1 or bids[0] not in floors:
            raise _spec_error(f"{where}.behavior_ids 必须恰好含 1 个 mission behavior id", behaviors=sorted(floors))
        bid = bids[0]
        seen.append(bid)
        if kind == KIND_REVIEWED and floors[bid] != contract_mod.PROOF_FLOOR_REVIEWED:
            raise _spec_error(f"behavior {bid} 在 contract 里没有放行 reviewed-boundaries，只能用 baseline-red 或 mutation")
        if framework == "generic" and kind == KIND_BASELINE:
            raise _spec_error("generic proof_runner 只看退出码，无法区分断言失败与启动错误，不能用 baseline-red")
        tids = g.get("test_ids")
        if not isinstance(tids, list) or not tids or not all(isinstance(x, str) and x.strip() for x in tids):
            raise _spec_error(f"{where}.test_ids 必须是非空字符串数组")
        for t in tids:
            if t.startswith("-"):
                raise _spec_error(f"{where}.test_ids 不能以 '-' 开头（会被当成命令行选项）", test_id=t)
        timeout = g.get("timeout", DEFAULT_TIMEOUT)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not (1 <= timeout <= MAX_TIMEOUT):
            raise _spec_error(f"{where}.timeout 必须在 1..{MAX_TIMEOUT}")
        g["timeout"] = timeout
        if framework == "pytest":
            files = sorted({t.split("::", 1)[0] for t in tids})
            existing = gitx.ls_tree_blobs(repo_root, tester_head, files) if tester_head else {}
            for f in files:
                if contract_mod.path_owner(auth, f) != contract_mod.OWNER_TESTER:
                    raise _spec_error(f"{where}.test_ids 的文件部分必须是 tester_write 内的路径: {f}")
                if f not in existing:
                    raise _spec_error(f"{where}.test_ids 引用的文件在你的 worktree 提交里不存在: {f}")
        if kind == KIND_MUTATION and (g.get("patch") or "").strip():
            if not isinstance(g["patch"], str) or not patch_paths(g["patch"]):
                raise _spec_error(f"{where}.patch 无法识别改动路径（需要 `diff --git a/x b/x` 头）")
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

    if sorted(seen) != sorted(floors) or len(seen) != len(set(seen)):
        raise _spec_error("groups 与 mission.behaviors 必须一一对应", covered=sorted(seen), expected=sorted(floors))
    return spec


# ---------------------------------------------------------------- 命令与结果


def build_argv(runner: dict[str, str], test_ids: list[str], junit_path: Path, main_repo: Path) -> list[str]:
    base = shlex.split(runner["cmd"].replace("{main_repo}", str(main_repo)))
    if runner.get("framework", "pytest") == "pytest":
        return base + ["-q", "-p", "no:cacheprovider", f"--junitxml={junit_path}", *test_ids]
    if "{tests}" in base:
        i = base.index("{tests}")
        return base[:i] + list(test_ids) + base[i + 1:]
    return base + list(test_ids)


def smoke_runner(runner: dict[str, str], repo_root: Path) -> None:
    """proof_runner 能不能起来。规划期 / start 时就跑，别等写完实现才在 proof 门禁暴露（#241）。
    此时 run 还不存在，改 loop.yml 不需要 contract revise，也就不必为「修好它」去要一次授权。"""
    argv = shlex.split(runner["cmd"].replace("{main_repo}", str(repo_root)))
    if not argv:
        raise negative("PROOF_RUNNER_UNAVAILABLE", "proof_runner.cmd 为空", cmd=runner.get("cmd"))
    if not shutil.which(argv[0], path=os.environ.get("PATH")) and not Path(argv[0]).exists():
        raise negative("PROOF_RUNNER_UNAVAILABLE", f"proof_runner 的可执行文件找不到: {argv[0]}",
                       cmd=runner["cmd"], hint="在 .claude/loop.yml 的 proof_runner.cmd 里写项目实际的测试命令；主仓内的解释器用 {main_repo} 引")
    if runner.get("framework", "pytest") != "pytest":
        return
    try:
        proc = subprocess.run([*argv, "--version"], cwd=str(repo_root), capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        raise negative("PROOF_RUNNER_UNAVAILABLE", f"proof_runner 起不来: {exc}", cmd=runner["cmd"]) from None
    if proc.returncode != 0:
        tail = ((proc.stdout or "") + (proc.stderr or ""))[-500:]
        raise negative("PROOF_RUNNER_UNAVAILABLE", f"`{runner['cmd']} --version` 退出码 {proc.returncode}",
                       cmd=runner["cmd"], tail=tail,
                       hint="pass_cmd 用 uv / poetry / venv 时，proof_runner.cmd 也要用同一套（如 `uv run python -m pytest`）")


def parse_junit(path: Path) -> list[dict[str, str]]:
    cases: list[dict[str, str]] = []
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return cases
    for tc in root.iter("testcase"):
        outcome, message = "passed", ""
        failure, error, skipped = tc.find("failure"), tc.find("error"), tc.find("skipped")
        if error is not None:
            outcome, message = "error", error.get("message") or ""
        elif failure is not None:
            outcome, message = "failure", failure.get("message") or (failure.text or "")
        elif skipped is not None:
            outcome, message = "skipped", skipped.get("message") or ""
        cases.append({"classname": tc.get("classname") or "", "name": tc.get("name") or "", "outcome": outcome, "message": message.strip()})
    return cases


def match_cases(test_id: str, cases: list[dict[str, str]]) -> list[dict[str, str]]:
    """pytest node id → junit 用例。classname 用后缀匹配（monorepo 子目录自带 ini 时 rootdir 会变）；
    声明的 id 不带 `[` 时匹配该用例的全部参数实例。只给文件路径则匹配该文件的全部用例。"""
    file_part, _, rest = test_id.partition("::")
    module = file_part[:-3] if file_part.endswith(".py") else file_part
    module = module.replace("/", ".")
    parts = rest.split("::") if rest else []
    name = parts[-1] if parts else None
    expected = ".".join([module, *parts[:-1]])

    def class_ok(actual: str) -> bool:
        if name is None:  # 只给了文件路径：该文件的全部用例
            return actual == module or actual.startswith(module + ".") or module.endswith("." + actual)
        return actual == expected or expected.endswith("." + actual) or actual.endswith("." + expected)

    def name_ok(actual: str) -> bool:
        if name is None:
            return True
        return actual == name if "[" in name else (actual == name or actual.startswith(name + "["))

    return [c for c in cases if class_ok(c["classname"]) and name_ok(c["name"])]


def judge_candidate(framework: str, rc: int, cases: list[dict[str, str]], test_ids: list[str]) -> dict[str, Any]:
    if framework != "pytest":
        return {"ok": rc == 0, "per_id": {}, "reason": None if rc == 0 else f"退出码 {rc}"}
    per_id: dict[str, str] = {}
    for t in test_ids:
        matched = match_cases(t, cases)
        if not matched:
            per_id[t] = "missing"
        elif all(c["outcome"] == "passed" for c in matched):
            per_id[t] = "passed"
        else:
            per_id[t] = next(c["outcome"] for c in matched if c["outcome"] != "passed")
    ok = rc == 0 and all(v == "passed" for v in per_id.values())
    return {"ok": ok, "per_id": per_id, "reason": None if ok else "声明的用例没有全部 passed（skipped / xfail / 未执行都不算）"}


def classify_counterexample(framework: str, rc: int, cases: list[dict[str, str]], test_ids: list[str]) -> str:
    """pass / assertion-failure / error。junit 的 <failure> 涵盖 call 阶段的任意异常，
    所以还要看 message 前缀；ImportError / AttributeError 不算断言失败。"""
    if rc == 0:
        return "pass"
    if framework != "pytest":
        return "assertion-failure"  # generic 只看退出码（仅允许 mutation）
    if rc != 1 or any(c["outcome"] == "error" for c in cases):
        return "error"
    declared = [c for t in test_ids for c in match_cases(t, cases)]
    failures = [c for c in declared if c["outcome"] == "failure"]
    if not failures:
        return "error"
    if all(c["message"].startswith(ASSERTION_PREFIXES) for c in failures):
        return "assertion-failure"
    return "error"


def _run(cwd: Path, argv: list[str], timeout: int, log_path: Path, env: dict[str, str]) -> tuple[int, str]:
    with open(log_path, "wb") as log:
        log.write(("$ " + " ".join(shlex.quote(a) for a in argv) + "\n").encode())
        log.flush()
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


def _ignorable(path: str) -> bool:
    return path.startswith((".pytest_cache", "__pycache__")) or "/__pycache__/" in path or path.endswith(".pyc")


# ---------------------------------------------------------------- 主流程


def run_proof(ledger_path: Path, repo_root: Path, spec_override: dict[str, Any] | None = None) -> dict[str, Any]:
    lg = ledger_mod.load(ledger_path)
    if ledger_mod.is_terminal(lg):
        raise fatal("RUN_TERMINAL", "run 已到终态", terminal=lg["terminal"])
    blocked = evidence.proof_blocked(lg, repo_root)
    if blocked:
        raise needs_user("PROOF_BLOCKED", "同一 proof 失败已重复出现；用 AskUserQuestion 让用户决定，继续则 `bl resume --reason`", blockers=blocked)
    if evidence.state(lg, "tester", repo_root) != evidence.STATE_PASS:
        raise negative("PROOF_PREREQ_TESTER", "tester evidence 不是 fresh pass，先完成 tester", state=evidence.state(lg, "tester", repo_root))
    if evidence.needs_integrate(lg, repo_root):
        raise negative("PROOF_PREREQ_INTEGRATE", "候选上的测试与 tester 分支不一致，先 `bl integrate`")
    spec = spec_override or lg.get("proof_spec")
    if not spec:
        raise negative("PROOF_SPEC_MISSING", "ledger 没有 proof_spec（tester 结果里应携带）")
    spec = validate_spec(spec, lg, repo_root)

    cand = lg["candidate"]
    wt = Path(cand["worktree"])
    worktree.assert_candidate_identity(wt, cand["branch"], cand["head"])
    worktree.assert_candidate_clean(wt)
    runner = lg["contract"]["assurance"].get("proof_runner") or {"framework": "pytest", "cmd": "python3 -m pytest"}
    framework = runner.get("framework", "pytest")
    auth = lg["contract"]["authority"]

    run_dir = ledger_path.parent
    log_dir, tmp_dir = run_dir / "logs", run_dir / "tmp"
    log_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    attempt = len(lg["failures"]["proof"]) + 1
    env = dict(os.environ)
    env.update({"BUILDER_LOOP_RUN_ID": lg["run_id"], "BUILDER_LOOP_PROOF": "1", "BUILDER_LOOP_MAIN_REPO": str(repo_root)})
    files = evidence.tester_files(lg, repo_root)
    groups_out: list[dict[str, Any]] = []
    failure: _Failure | None = None

    def execute(where: Path, g: dict[str, Any], tag: str, i: int) -> tuple[int, str, list[dict[str, str]], Path]:
        junit = tmp_dir / f"junit-{attempt}-g{i}-{tag}.xml"
        junit.unlink(missing_ok=True)
        log = log_dir / f"proof-{attempt}-g{i}-{tag}.log"
        rc, out = _run(where, build_argv(runner, g["test_ids"], junit, repo_root), g["timeout"], log, env)
        return rc, out, (parse_junit(junit) if framework == "pytest" else []), log

    gate = evidence.gate_lock(lg, evidence.GATE_PROOF)  # 全套测试要跑好几轮，执行期间对外可见（#242）
    gate.__enter__()
    try:
        for i, g in enumerate(spec["groups"]):
            if g["kind"] == KIND_MUTATION and not (g.get("patch") or "").strip():
                raise _Failure("TEST_MUTATION_PATCH_MISSING", "mutation 组缺 patch：集成后已可读候选，请续接 tester 补一段只破坏该 behavior 的 unified diff", i, g["behavior_ids"][0])

        # ① 候选：逐 id 全部 passed
        for i, g in enumerate(spec["groups"]):
            bid = g["behavior_ids"][0]
            rc, out, cases, log = execute(wt, g, "candidate", i)
            tracked, untracked = _restore_worktree(wt)
            if framework == "pytest" and rc != 0 and not cases:
                # 一条 junit 记录都没有 = runner 根本没跑起来（收集错误也会写进 junit）。不是实现的锅（#241）
                raise _Failure("TEST_PROOF_RUNNER_FAILED", f"proof_runner 没能产出测试结果：`{runner['cmd']}` 退出码 {rc}", i, bid,
                               returncode=rc, log=str(log), tail=out[-3000:], cmd=runner["cmd"])
            verdict = judge_candidate(framework, rc, cases, g["test_ids"])
            groups_out.append({"behavior_id": bid, "kind": g["kind"], "candidate": {"returncode": rc, "per_id": verdict["per_id"], "log": str(log), "residue_cleaned": untracked}})
            if tracked:
                raise _Failure("PROOF_WORKTREE_MUTATED", "测试命令修改了候选中的 tracked 文件", i, bid, paths=tracked[:50], tail=out[-3000:])
            if not verdict["ok"]:
                not_run = [t for t, v in verdict["per_id"].items() if v in ("missing", "skipped")]
                code = "TEST_PROOF_NOT_EXECUTED" if (rc == 0 and not_run) else "TEST_PROOF_CANDIDATE_FAILED"
                raise _Failure(code, verdict["reason"] or "测试在候选实现上未通过", i, bid, returncode=rc, per_id=verdict["per_id"], log=str(log), tail=out[-3000:])

        # ②③④ 反例
        for i, g in enumerate(spec["groups"]):
            bid = g["behavior_ids"][0]
            if g["kind"] == KIND_REVIEWED:
                groups_out[i]["counterexample"] = {"kind": KIND_REVIEWED, "outcome": "reviewed"}
                continue
            if g["kind"] == KIND_BASELINE:
                overlay = (cand["head"], files["present"], files["deleted"])
                with worktree.temp_worktree(repo_root, lg["repo"]["target_start_head"], tmp_dir, f"baseline-g{i}", overlay=overlay) as tw:
                    rc, out, cases, log = execute(tw, g, "baseline", i)
                cls = classify_counterexample(framework, rc, cases, g["test_ids"])
                groups_out[i]["counterexample"] = {"kind": KIND_BASELINE, "returncode": rc, "classification": cls, "log": str(log)}
                if cls != "assertion-failure":
                    raise _Failure("TEST_BASELINE_RED_NOT_PROVEN", "起点 + 你的测试没有产生断言失败（pass=测试没约束行为；error=依赖了起点上不存在的接口，请改用 mutation）", i, bid, classification=cls, returncode=rc, log=str(log), tail=out[-3000:])
                continue
            ppaths = patch_paths(g["patch"])
            for p in ppaths:
                reason = contract_mod.write_rejection(auth, contract_mod.OWNER_BUILDER, p)
                if reason:
                    raise _Failure("TEST_MUTATION_INVALID", f"patch 只能改 builder 拥有的文件：{p} → {reason}", i, bid, path=p, reason=reason)
                if gitx.blob_mode(repo_root, cand["head"], p) not in ("100644", "100755"):
                    raise _Failure("TEST_MUTATION_INVALID", f"patch 触及候选上不存在的普通文件 {p}", i, bid, path=p)
            with worktree.temp_worktree(repo_root, cand["head"], tmp_dir, f"mutation-g{i}") as tw:
                text = g["patch"] if g["patch"].endswith("\n") else g["patch"] + "\n"
                ap = gitx.git(tw, "apply", "--whitespace=nowarn", "-", check=False, input_text=text)
                if not ap.ok:
                    raise _Failure("TEST_MUTATION_INVALID", "mutation patch 无法应用到候选", i, bid, stderr=ap.stderr[-2000:])
                changed = sorted(p for _, p in gitx.status_porcelain(tw))
                if changed != ppaths:
                    raise _Failure("TEST_MUTATION_INVALID", "patch 实际改动路径与声明不符", i, bid, declared=ppaths, actual=changed)
                before = gitx.git(tw, "diff", "--binary", "--full-index").stdout
                rc, out, cases, log = execute(tw, g, "mutation", i)
                after = gitx.git(tw, "diff", "--binary", "--full-index").stdout
                extra = [p for _, p in gitx.status_porcelain(tw) if p not in changed and not _ignorable(p)]
                if after != before or gitx.head(tw) != cand["head"] or extra:
                    raise _Failure("TEST_MUTATION_INVALID", "执行期间 mutation 现场被改动", i, bid, extra_paths=extra)
            cls = classify_counterexample(framework, rc, cases, g["test_ids"])
            groups_out[i]["counterexample"] = {"kind": KIND_MUTATION, "returncode": rc, "classification": cls, "log": str(log), "patch_sha256": sha256_bytes(g["patch"].encode()), "patch_paths": ppaths}
            if cls != "assertion-failure":
                raise _Failure("TEST_MUTATION_SURVIVED", "破坏实现后测试没有产生断言失败（pass=测试没约束该行为；error=命令或导入出错）", i, bid, classification=cls, returncode=rc, log=str(log), tail=out[-3000:])
    except _Failure as exc:
        failure = exc
    finally:
        gate.__exit__(None, None, None)

    spec_digest = digest(spec)
    if failure:
        tail = str(failure.details.get("tail", ""))
        sig = failure_signature(f"{failure.code}\n{failure.behavior}\n{failure.details.get('classification', '')}\n{tail}", f"proof-{failure.code}", int(failure.details.get("returncode", -1) or -1))
        fdetails = {k: v for k, v in failure.details.items() if k != "tail"}
        fdetails["suggested_owner"] = OWNER_BY_FAILURE.get(failure.code, "builder")
        with ledger_mod.mutate(ledger_path) as lg2:
            lg2["failures"]["proof"].append({"code": failure.code, "behavior": failure.behavior, "signature": sig, "at": ledger_mod.now_iso(), "attempt": attempt})
            evidence.record(lg2, "proof", "fail", {"attempt": attempt, "spec_digest": spec_digest, "groups": groups_out, "failure": {"code": failure.code, "message": failure.message, "group": failure.group, "behavior": failure.behavior, **fdetails, "signature": sig}}, repo_root)
            readiness = evidence.readiness(lg2, repo_root)
        repeats = sum(1 for f in lg2["failures"]["proof"] if f["signature"] == sig)
        return {"result": "FAIL", "attempt": attempt, "failure": {"code": failure.code, "message": failure.message, "group": failure.group, "behavior": failure.behavior, "signature": sig, "repeat_count": repeats, **fdetails, "tail": tail[-1500:]}, "groups": groups_out, "readiness": readiness}

    with ledger_mod.mutate(ledger_path) as lg2:
        evidence.record(lg2, "proof", "pass", {"attempt": attempt, "spec_digest": spec_digest, "runner": runner, "groups": groups_out}, repo_root)
        readiness = evidence.readiness(lg2, repo_root)
    return {"result": "PASS", "attempt": attempt, "groups": groups_out, "readiness": readiness}
