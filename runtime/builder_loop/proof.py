"""proof：证明 tester 的测试有鉴别力。

测试命令不由 tester 写：项目在 loop.yml 声明 `proof_runner`，start 时冻结进 assurance 面（计入 digest），
tester 只给 test_ids，runtime 拼 argv（#229）。pytest 框架下结果取自 junit xml 的逐用例状态，不扫 stdout。

两段式：tester 首轮在冻结基线上盲写测试，那时看不到实现，mutation 组的 patch 可以缺省；
集成之后读隔离解除，由 readiness 引导续接同一 tester 补 patch。

依次：① 所有 group 在候选（clean）上逐 id 全部 passed（skipped / xfail / 缺失都不算）→
② baseline-red：run 起点 + tester 文件上必须在 call 阶段失败 → ③ mutation：候选上打 patch（只能破坏
builder 拥有的已有普通文件）后必须在 call 阶段失败，执行前后比对 diff 防篡改 →
④ reviewed-boundaries：四类 id 并集恰好等于 test_ids，且该 behavior 在 contract 里显式放行了最弱 kind。

②③ 的通过条件是「声明的每个 test_id 及其参数实例都在 call 阶段失败」：test_ids 是证据名单，
不是该 behavior 的全部相关测试名单（#370，原则一）。只检查现役行为的守卫测试不列入，仍由 machine 全量运行。
generic 没有逐用例结果，强证明组因此只许一个证据 test_id。
"""

from __future__ import annotations

import ast
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Sequence

from . import contract as contract_mod
from . import evidence, gitx, ledger as ledger_mod, process, worktree
from .config import expand_main_repo
from .errors import Problem, fatal, needs_user, negative
from .jsonutil import digest, sha256_bytes
from .machine import failure_signature, private_pycache

KIND_BASELINE = "baseline-red"
KIND_MUTATION = "mutation"
KIND_REVIEWED = "reviewed-boundaries"
DEFAULT_TIMEOUT = 300
PYTEST_NO_TESTS_COLLECTED = 5
MAX_TIMEOUT = 1800
PATCH_FILE_MAX_BYTES = 256 * 1024
TESTER_OWNED_FAILURES = ("TEST_BASELINE_RED_NOT_PROVEN", "TEST_MUTATION_SURVIVED", "TEST_MUTATION_INVALID", "TEST_MUTATION_PATCH_MISSING", "TEST_PROOF_NOT_EXECUTED", "TEST_PROOF_GROUP_TIMEOUT")
# patch 路径被这几类原因拒绝时，是 contract 没把要破坏的行为所在文件交给 builder：tester 改 patch 解决不了（#286）
CONTRACT_REJECTIONS = ("protected", "outside_authority", "control_file")
ISOLATION_HINT = ("全量 machine 在同一候选上通过、单独跑声明的用例却失败：先查测试隔离（导入顺序、fixture 替换模块）；"
                  "确认是 machine 没跑到它、或确实是实现的问题，就在 notes 里说明")
# 失败归谁修。缺省 builder（实现没让测试过）；runner 起不来两个角色都改不了，归 contract → 改 loop.yml
OWNER_BY_FAILURE = {**{c: "tester" for c in TESTER_OWNED_FAILURES}, "TEST_PROOF_RUNNER_FAILED": "contract"}


def _spec_error(msg: str, **details: Any) -> Problem:
    return Problem("PROOF_SPEC_INVALID", msg, details=details, exit_code=1)


def kinds_allowed(framework: str, floor: str) -> list[str]:
    """该 framework 与 proof 下限下可选的 proof kind。唯一来源：brief 与 check_structure 都引用它，
    不各抄一份（#380 #265，原则二）。generic 只有整条命令的退出码，既分不清断言失败与启动错误，
    也没有逐用例结果，所以只能 mutation。"""
    out = [KIND_BASELINE, KIND_MUTATION] if framework == "pytest" else [KIND_MUTATION]
    if floor == contract_mod.PROOF_FLOOR_REVIEWED:
        out.append(KIND_REVIEWED)
    return out


# ---------------------------------------------------------------- spec 校验（tester 交卷时做；候选可读后另加 patch 预检）


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


def guard_items(g: dict[str, Any]) -> list[dict[str, Any]]:
    """group 声明的守卫条目（#399）。守卫不进 test_ids，但要证明它锁住的东西：patch 打上后它必须变红。"""
    return [gd for gd in (g.get("guards") or []) if isinstance(gd, dict)]


def missing_patch_groups(spec: dict[str, Any] | None) -> list[str]:
    if not spec:
        return []
    out: list[str] = []
    for g in spec.get("groups", []):
        missing = g.get("kind") == KIND_MUTATION and not (g.get("patch") or "").strip()
        if not missing:
            missing = any(not (gd.get("patch") or "").strip() for gd in guard_items(g))
        if missing and g["behavior_ids"][0] not in out:
            out.append(g["behavior_ids"][0])
    return out


def normalized_patch(patch: str) -> str:
    """交卷预检与 proof ③ 段对同一份 patch 做的唯一规范化：保证末尾换行。"""
    return patch if patch.endswith("\n") else patch + "\n"


def validate_spec(spec: Any, lg: dict[str, Any], repo_root: Path, *, worktree_files: bool = False) -> dict[str, Any]:
    """tester 交卷时的校验：patch_file 取字节 + 结构 + 候选可读后对 patch 的可应用性预检。返回要登记的那份 spec。
    预检对象是 runtime 自己从文件读到的字节，不是模型转述的文本（#274 #281，原则一）。
    worktree_files：test_ids 的文件查 tester worktree 的工作树而不是提交——PreToolUse 时还没提交（登记时才提交）。"""
    spec = check_structure(resolve_patch_files(spec, lg, repo_root), lg, repo_root, worktree_files=worktree_files)
    if lg.get("candidate") and evidence.implementation_readable_by_tester(lg):
        _precheck_patches(spec, lg, repo_root)
    return spec


def resolve_patch_files(spec: Any, lg: dict[str, Any], repo_root: Path) -> Any:
    """mutation 组的 patch 只从 patch_file 读原始字节（#281）：模型转述长 patch 会丢空白上下文行、字符走样，
    这个输入条件不改，交卷预检只能拦下、拦不住。返回副本：patch_file 换成读到的 patch，ledger 只存字节。"""
    if not isinstance(spec, dict) or not isinstance(spec.get("groups"), list):
        return spec  # 结构错误留给 check_structure 报
    roots = [Path(repo_root)] + [Path(x["worktree"]) for x in (lg.get("candidate"), lg.get("tester")) if x and x.get("worktree")]
    out = {**spec, "groups": []}
    for i, g in enumerate(spec["groups"]):
        if not isinstance(g, dict):
            out["groups"].append(g)
            continue
        where = f"groups[{i}]"
        g2 = dict(g)
        if isinstance(g.get("guards"), list):
            g2["guards"] = [_resolve_guard(gd, roots, f"{where}.guards[{k}]", group=i) for k, gd in enumerate(g["guards"])]
        if "patch_file" in g or (g.get("patch") or "").strip():
            if "patch_file" not in g:
                raise _spec_error(f"{where}.patch 不再接受 patch 正文：用 git diff 把 patch 重定向到你 worktree 之外的文件，"
                                  "把它的绝对路径填进 patch_file（runtime 读原始字节，避免转述走样）", group=i)
            if "patch" in g:
                raise _spec_error(f"{where} 不能同时给 patch 和 patch_file：只给 patch_file", group=i)
            if g.get("kind") != KIND_MUTATION:
                raise _spec_error(f"{where}.patch_file 只用于 mutation 组", group=i)
            g2.pop("patch_file", None)
            g2["patch"] = _read_patch_file(g["patch_file"], roots, where)
        out["groups"].append(g2)
    return out


def _resolve_guard(gd: Any, roots: list[Path], where: str, *, group: int) -> Any:
    """守卫的 patch 与 mutation 组同一入口读字节（#399）：两处各读一份就有两套走样规则。"""
    if not isinstance(gd, dict):
        return gd  # 结构错误留给 check_structure 报
    if "patch_file" not in gd:
        if (gd.get("patch") or "").strip():
            raise _spec_error(f"{where}.patch 不再接受 patch 正文：用 git diff 把 patch 重定向到你 worktree 之外的文件，"
                              "把它的绝对路径填进 patch_file（runtime 读原始字节，避免转述走样）", group=group)
        return gd  # 首轮盲写允许省略
    if "patch" in gd:
        raise _spec_error(f"{where} 不能同时给 patch 和 patch_file：只给 patch_file", group=group)
    out = {k: v for k, v in gd.items() if k != "patch_file"}
    out["patch"] = _read_patch_file(gd["patch_file"], roots, where)
    return out


def _read_patch_file(pf: Any, roots: list[Path], where: str) -> str:
    if not isinstance(pf, str) or not os.path.isabs(pf):
        raise _spec_error(f"{where}.patch_file 必须是绝对路径", patch_file=pf)
    real = os.path.realpath(pf)
    if not os.path.isfile(real):
        raise _spec_error(f"{where}.patch_file 不存在或不是普通文件: {pf}", patch_file=pf)
    for r in roots:
        base = os.path.realpath(r)
        if real == base or real.startswith(base.rstrip("/") + "/"):
            # tester worktree 里的会被当成测试提交进候选；候选与主仓不归 tester 写
            raise _spec_error(f"{where}.patch_file 不能放在候选 / tester worktree / 主仓之内: {pf}（放到你自己建的临时目录）", patch_file=pf, root=str(r))
    size = os.path.getsize(real)
    if size > PATCH_FILE_MAX_BYTES:
        raise _spec_error(f"{where}.patch_file 超过 {PATCH_FILE_MAX_BYTES} 字节（{size}）：mutation 只需破坏一处", patch_file=pf, size=size)
    try:
        return Path(real).read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _spec_error(f"{where}.patch_file 不是 UTF-8 文本: {exc}", patch_file=pf) from exc


def patch_applies(repo_root: Path, head: str, patch: str) -> tuple[bool, str]:
    """patch 能否打到 head 上：临时 index，不碰任何 worktree。"""
    for p in patch_paths(patch):
        if gitx.blob_mode(repo_root, head, p) not in ("100644", "100755"):
            return False, f"触及 {head[:12]} 上不存在的普通文件 {p}"
    with tempfile.TemporaryDirectory(prefix="bl-patch-check-") as td:
        env = {"GIT_INDEX_FILE": str(Path(td) / "index")}
        gitx.git(repo_root, "read-tree", head, env=env)
        ap = gitx.git(repo_root, "apply", "--cached", "--check", "--whitespace=nowarn", "-", check=False,
                      input_text=normalized_patch(patch), env=env)
    return ap.ok, ap.stderr


def stale_patch_groups(lg: dict[str, Any], repo_root: Path) -> list[str]:
    """ledger 里已有 patch、但打不到当前候选 HEAD 上的组（#305：rebase 或 builder 改实现之后）。
    守卫的 patch 同理：它们的上下文同样绑在候选 HEAD 上（#399）。"""
    head = (lg.get("candidate") or {}).get("head")
    if not head:
        return []
    out: list[str] = []
    for g in (lg.get("proof_spec") or {}).get("groups", []):
        patches = [g["patch"]] if g.get("kind") == KIND_MUTATION and (g.get("patch") or "").strip() else []
        patches += [gd["patch"] for gd in guard_items(g) if (gd.get("patch") or "").strip()]
        for patch in patches:
            try:
                ok = patch_applies(repo_root, head, patch)[0]
            except Problem:
                ok = False  # patch 头本身不合法（如重命名）也是要重新生成
            if not ok:
                if g["behavior_ids"][0] not in out:
                    out.append(g["behavior_ids"][0])
                break
    return out


def _precheck_patches(spec: dict[str, Any], lg: dict[str, Any], repo_root: Path) -> None:
    """在交卷那一刻的候选 head 上判 patch 能否打上。
    之后 builder 再改实现导致对不上，由 proof 反例段报 TEST_MUTATION_INVALID（owner=tester），所以 proof 入口不做这一步。"""
    head = lg["candidate"]["head"]
    for i, g in enumerate(spec["groups"]):
        where, bid = f"groups[{i}]", g["behavior_ids"][0]
        targets = []
        if g.get("kind") == KIND_MUTATION and (g.get("patch") or "").strip():
            targets.append((where, g["patch"]))
        targets += [(f"{where}.guards[{k}]", gd["patch"]) for k, gd in enumerate(guard_items(g)) if (gd.get("patch") or "").strip()]
        for gw, patch in targets:
            for p in patch_paths(patch):
                if gitx.blob_mode(repo_root, head, p) not in ("100644", "100755"):
                    raise _spec_error(f"{gw}.patch 触及候选上不存在的普通文件 {p}", group=i, behavior=bid, path=p, candidate_head=head)
            ok, stderr = patch_applies(repo_root, head, patch)
            if not ok:
                raise _spec_error(f"{gw}.patch 无法应用到候选 {head[:12]}：{stderr.strip()[-1500:]}",
                                  group=i, behavior=bid, stderr=stderr[-2000:], candidate_head=head)


def _module_symbols(body: list[ast.stmt]) -> dict[str, Any] | None:
    """源文件的符号树：名字 → {children, has_bases, opaque}。只认顶层函数 / 类 / 赋值 / import。
    `opaque` 表示这个符号的子符号未知（导入来的、赋值别名）：里面找不到不代表不存在。
    `from x import *` 让整个模块的符号表不可知，返回 None。"""
    out: dict[str, Any] = {}
    for ch in body:
        if isinstance(ch, ast.ClassDef):
            children = _module_symbols(ch.body)
            if children is None:
                return None
            out[ch.name] = {"children": children,
                            "has_bases": any(ast.unparse(b) != "object" for b in ch.bases),
                            "opaque": False}
        elif isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[ch.name] = {"children": {}, "has_bases": False, "opaque": False}
        elif isinstance(ch, ast.Assign):
            for t in ch.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = {"children": {}, "has_bases": False, "opaque": True}
        elif isinstance(ch, ast.AnnAssign) and isinstance(ch.target, ast.Name):
            out[ch.target.id] = {"children": {}, "has_bases": False, "opaque": True}
        elif isinstance(ch, (ast.Import, ast.ImportFrom)):
            if isinstance(ch, ast.ImportFrom) and any(al.name == "*" for al in ch.names):
                return None
            for al in ch.names:
                out[al.asname or al.name.split(".")[0]] = {"children": {}, "has_bases": False, "opaque": True}
    return out


def _pytest_id_missing(symbols: dict[str, Any], parts: list[str]) -> bool:
    """`Class::method` 这类路径在符号树里是否缺。父节点未知（opaque）或类声明了基类时，缺的那一段可能来自别处，不判缺。"""
    cur, unknown, inheriting = symbols, False, False
    for i, seg in enumerate(parts):
        node = cur.get(seg)
        if node is None:
            return not (unknown or (inheriting and i == len(parts) - 1))
        cur, unknown, inheriting = node["children"], bool(node.get("opaque")), bool(node.get("has_bases"))
    return False


def _node_id_missing(source: str | None, parts: list[str]) -> bool:
    """源文件读不到、解析不了、或有 `from x import *` 就不判缺：那是别处的失败，不能在这里冒充（#397）。"""
    if source is None:
        return False
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return False
    symbols = _module_symbols(tree.body)
    if symbols is None:
        return False
    return _pytest_id_missing(symbols, parts)


def _source_of(lg: dict[str, Any], repo_root: Path, path: str, worktree_files: bool) -> str | None:
    if worktree_files:
        wt = (lg.get("tester") or {}).get("worktree")
        if not wt:
            return None
        try:
            return (Path(wt) / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    head = (lg.get("tester") or {}).get("head")
    if not head:
        return None
    res = gitx.git(repo_root, "show", f"{head}:{path}", check=False)
    return res.stdout if res.ok else None


def _node_id_parts(test_id: str) -> tuple[str, list[str]]:
    """pytest node id → (文件, 符号路径)。参数化 id 里可能含 `::`（#310），所以只取第一个 `[` 之前。"""
    path, _, rest = test_id.partition("::")
    rest = rest.split("[", 1)[0]
    return path, [p for p in rest.split("::") if p]


def _check_patch_owner(patch: Any, where: str, *, group: int, behavior: str, auth: dict[str, Any]) -> None:
    """patch 只能改 builder 拥有的文件。mutation 组与守卫共用同一个判定（#399，原则二）。"""
    if not isinstance(patch, str) or not patch_paths(patch):
        raise _spec_error(f"{where}.patch 无法识别改动路径（需要 `diff --git a/x b/x` 头）")
    for p in patch_paths(patch):
        reason = contract_mod.write_rejection(auth, contract_mod.OWNER_BUILDER, p)
        if reason:
            if reason in CONTRACT_REJECTIONS:
                raise _spec_error(f"{where}.patch 只能改 builder 拥有的文件：{p} → {reason}。这不是你能改的：contract 没把这个行为所在的文件交给 builder——"
                                  "改交 status=insufficient_spec，在 notes 里写明需要把该文件列进 builder_write（并由 reviewer 看住零改动）",
                                  behavior=behavior, path=p, reason=reason, suggested_owner="contract")
            raise _spec_error(f"{where}.patch 只能改 builder 拥有的文件：{p} → {reason}", behavior=behavior, path=p, reason=reason)


def _check_guards(guards: Any, *, where: str, group: int, behavior: str, evidence_ids: list[str],
                  framework: str, auth: dict[str, Any], lg: dict[str, Any], repo_root: Path,
                  sources: dict[str, str | None], worktree_files: bool, tester_head: str | None) -> None:
    """守卫的形状校验（#399）：test_ids 与证据名单同规则，patch 与 mutation 组同规则。
    守卫不进 test_ids：所以也不允许与证据 id 重叠，否则「证据名单」与「守卫」的语义就混了（原则一）。"""
    if not isinstance(guards, list):
        raise _spec_error(f"{where}.guards 必须是数组", group=group, behavior=behavior)
    for k, gd in enumerate(guards):
        gw = f"{where}.guards[{k}]"
        if not isinstance(gd, dict):
            raise _spec_error(f"{gw} 必须是对象", group=group, behavior=behavior)
        tids = gd.get("test_ids")
        if framework == "generic":
            n = len(tids) if isinstance(tids, list) else 0
            if n != 1 or not all(isinstance(x, str) and x.strip() for x in tids):
                raise _spec_error(f"{gw}: generic 下守卫也必须恰好声明 1 个 test_id（当前 {n} 个）", group=group, behavior=behavior)
        if not isinstance(tids, list) or not tids or not all(isinstance(x, str) and x.strip() for x in tids):
            raise _spec_error(f"{gw}.test_ids 必须是非空字符串数组", group=group, behavior=behavior)
        for t in tids:
            if t.startswith("-"):
                raise _spec_error(f"{gw}.test_ids 不能以 '-' 开头（会被当成命令行选项）", test_id=t)
        overlap = sorted(set(tids) & set(evidence_ids))
        if overlap:
            raise _spec_error(f"{gw}.test_ids 不能与所在组的 test_ids 重复（守卫不是证据）：{', '.join(overlap)}",
                              group=group, behavior=behavior)
        if framework == "pytest":
            files = sorted({t.split("::", 1)[0] for t in tids})
            foreign = [f for f in files if not f.endswith(".py")]
            if foreign:
                raise _spec_error(f"{gw}.test_ids 不是 Python 测试文件，但 proof_runner.framework=pytest：{', '.join(foreign)}",
                                  files=foreign, framework=framework, suggested_owner="contract")
            if worktree_files:
                twt = Path((lg.get("tester") or {}).get("worktree") or "")
                existing = {f: True for f in files if (twt / f).is_file()}
            else:
                existing = gitx.ls_tree_blobs(repo_root, tester_head, files) if tester_head else {}
            for f in files:
                if contract_mod.path_owner(auth, f) != contract_mod.OWNER_TESTER:
                    raise _spec_error(f"{gw}.test_ids 的文件部分必须是 tester_write 内的路径: {f}")
                if f not in existing:
                    raise _spec_error(f"{gw}.test_ids 引用的文件在你的 worktree 里不存在: {f}")
            missing_ids: list[str] = []
            for t in tids:
                f, parts = _node_id_parts(t)
                if not parts or f not in existing:
                    continue
                if f not in sources:
                    sources[f] = _source_of(lg, repo_root, f, worktree_files)
                if _node_id_missing(sources[f], parts):
                    missing_ids.append(t)
            if missing_ids:
                raise _spec_error(f"{gw}.test_ids 指向的测试函数 / 类在文件里不存在：{', '.join(missing_ids)}",
                                  test_ids_missing=missing_ids, group=group, behavior=behavior)
        if (gd.get("patch") or "").strip():
            _check_patch_owner(gd["patch"], gw, group=group, behavior=behavior, auth=auth)


def check_structure(spec: Any, lg: dict[str, Any], repo_root: Path, *, worktree_files: bool = False) -> dict[str, Any]:
    """不依赖候选内容的校验：交卷与 proof 入口都做。"""
    if not isinstance(spec, dict) or not isinstance(spec.get("groups"), list) or not spec["groups"]:
        raise _spec_error("proof_spec 必须含非空 groups 数组")
    c = lg["contract"]
    allowed = set(c["assurance"].get("proof_kinds") or [])
    framework = (c["assurance"].get("proof_runner") or {}).get("framework", "pytest")
    floors = {b["id"]: b.get("proof", contract_mod.PROOF_FLOOR_STRONG) for b in c["mission"]["behaviors"]}
    auth = c["authority"]
    tester_head = (lg.get("tester") or {}).get("head")
    seen: list[str] = []
    sources: dict[str, str | None] = {}  # 同一文件只读一次

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
        allowed_here = kinds_allowed(framework, floors[bid])
        if kind not in allowed_here:
            if kind == KIND_BASELINE:
                raise _spec_error("generic proof_runner 只看退出码，无法区分断言失败与启动错误，不能用 baseline-red")
            raise _spec_error(f"{where}.kind={kind} 在 framework={framework}、proof 下限 {floors[bid]} 下不可用："
                              f"只允许 {allowed_here}", allowed=allowed_here)
        tids = g.get("test_ids")
        if framework == "generic" and kind != KIND_REVIEWED:
            # generic 只有整条命令的退出码，没有逐用例结果：多条 id（含 0 个）无法逐条证明各自有鉴别力（#370）。
            # 先于通用非空检查，空数组也给出这条可执行的修复路径，而不是笼统的「非空数组」
            n = len(tids) if isinstance(tids, list) else 0
            if n != 1 or not all(isinstance(x, str) and x.strip() for x in tids):
                raise _spec_error(f"{where}: generic 强证明组必须恰好声明 1 个证据 test_id（当前 {n} 个）。"
                                  "generic 只有整条命令的退出码，无法逐条证明；拆成多个组，或让这一组只留一个能抓住该实现的用例", group=i, behavior=bid)
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
            foreign = [f for f in files if not f.endswith(".py")]
            if foreign:
                # tester 修不了：runner 是 loop.yml 冻结的（#265）
                raise _spec_error(f"{where}.test_ids 不是 Python 测试文件，但 proof_runner.framework=pytest：{', '.join(foreign)}。"
                                  "这不是你能改的——改交 status=insufficient_spec，在 notes 里写明 loop.yml 的 proof_runner 与测试语言不匹配、需要改 loop.yml",
                                  files=foreign, framework=framework, suggested_owner="contract")
            if worktree_files:
                twt = Path((lg.get("tester") or {}).get("worktree") or "")
                existing = {f: True for f in files if (twt / f).is_file()}
            else:
                existing = gitx.ls_tree_blobs(repo_root, tester_head, files) if tester_head else {}
            for f in files:
                if contract_mod.path_owner(auth, f) != contract_mod.OWNER_TESTER:
                    raise _spec_error(f"{where}.test_ids 的文件部分必须是 tester_write 内的路径: {f}")
                if f not in existing:
                    raise _spec_error(f"{where}.test_ids 引用的文件在你的 worktree 里不存在: {f}")
            missing_ids: list[str] = []
            for t in tids:
                f, parts = _node_id_parts(t)
                if not parts or f not in existing:
                    continue
                if f not in sources:
                    sources[f] = _source_of(lg, repo_root, f, worktree_files)
                if _node_id_missing(sources[f], parts):
                    missing_ids.append(t)
            if missing_ids:
                # 拼错的 node id 不必等到 proof 阶段才以 missing 的形式暴露（#397）
                raise _spec_error(f"{where}.test_ids 指向的测试函数 / 类在文件里不存在：{', '.join(missing_ids)}",
                                  test_ids_missing=missing_ids, group=i, behavior=bid)
        if kind == KIND_MUTATION and (g.get("patch") or "").strip():
            _check_patch_owner(g["patch"], where, group=i, behavior=bid, auth=auth)
        if g.get("guards") is not None:
            _check_guards(g["guards"], where=where, group=i, behavior=bid, evidence_ids=tids, framework=framework,
                          auth=auth, lg=lg, repo_root=repo_root, sources=sources,
                          worktree_files=worktree_files, tester_head=tester_head)
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
    base = shlex.split(expand_main_repo(runner["cmd"], main_repo))
    if runner.get("framework", "pytest") == "pytest":
        return base + ["-q", "-p", "no:cacheprovider", f"--junitxml={junit_path}", *test_ids]
    if "{tests}" in base:
        i = base.index("{tests}")
        return base[:i] + list(test_ids) + base[i + 1:]
    return base + list(test_ids)


def smoke_runner(runner: dict[str, str], repo_root: Path) -> None:
    """proof_runner 能不能起来。规划期 / start 时就跑，别等写完实现才在 proof 门禁暴露（#241）。
    此时 run 还不存在，改 loop.yml 不需要 contract revise，也就不必为「修好它」去要一次授权。"""
    argv = shlex.split(expand_main_repo(runner["cmd"], repo_root))
    if not argv:
        raise negative("PROOF_RUNNER_UNAVAILABLE", "proof_runner.cmd 为空", cmd=runner.get("cmd"))
    if not shutil.which(argv[0], path=os.environ.get("PATH")) and not Path(argv[0]).exists():
        raise negative("PROOF_RUNNER_UNAVAILABLE", f"proof_runner 的可执行文件找不到: {argv[0]}",
                       cmd=runner["cmd"], hint="在 .claude/loop.yml 的 proof_runner.cmd 里写项目实际的测试命令；主仓内的解释器用 {main_repo} 引")
    if runner.get("framework", "pytest") != "pytest":
        return
    # 对空目录收集：pytest 完整启动（含 entry-point 插件）且没收集到用例 = 退出码 5。
    # 不能用 --version：它不加载插件，装坏了的插件照样返回 0，要到真跑 proof 才炸。
    with tempfile.TemporaryDirectory(prefix="bl-smoke-") as empty:
        try:
            proc = subprocess.run([*argv, "--collect-only", "-q", "-p", "no:cacheprovider", empty], cwd=str(repo_root),
                                  capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            # 120s 不够不说明 runner 坏了：`uv run --with-requirements` 首次要下载整棵依赖（#394）
            raise negative("PROOF_RUNNER_UNAVAILABLE",
                           f"冒烟超时：`{runner['cmd']}` 对空目录收集超过 120s（可能仍在下载依赖）",
                           cmd=runner["cmd"], timeout=120,
                           hint="依赖首次下载可能超过 120s（如 `uv run --with-requirements` 要装几十个包）：网络就绪后再跑一次通常即可；"
                                "仍超时就缩短 cmd 的依赖面或预热环境") from None
        except (OSError, subprocess.SubprocessError) as exc:
            raise negative("PROOF_RUNNER_UNAVAILABLE", f"proof_runner 起不来: {exc}", cmd=runner["cmd"]) from None
    if proc.returncode != PYTEST_NO_TESTS_COLLECTED:
        tail = ((proc.stdout or "") + (proc.stderr or ""))[-500:]
        raise negative("PROOF_RUNNER_UNAVAILABLE", f"`{runner['cmd']}` 没能完整启动（对空目录收集，退出码 {proc.returncode}，健康时应为 5）",
                       cmd=runner["cmd"], tail=tail,
                       hint="pass_cmd 用 uv / poetry / venv 时，proof_runner.cmd 也要用同一套（如 `uv run python -m pytest`）；"
                            "本机插件装坏了就在 cmd 里加 `-p no:<插件名>`")


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
    声明的 id 不带 `[` 时匹配该用例的全部参数实例。只给文件路径则匹配该文件的全部用例。
    切分与 pytest 写 junit 时的 `mangle_test_address` 一致：先在第一个 `[` 处切开，只把前半段按 `::` 切分，
    参数部分原样接回 name——参数 id 里可以有 `::`（#310）。"""
    path, bracket, params = test_id.partition("[")
    file_part, _, rest = path.partition("::")
    module = file_part[:-3] if file_part.endswith(".py") else file_part
    module = module.replace("/", ".")
    parts = rest.split("::") if rest else []
    if parts:
        parts[-1] += bracket + params
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


def discrimination(cases: list[dict[str, str]], test_ids: list[str]) -> dict[str, str]:
    """反例下每条声明 id 的鉴别力：red = 它匹配到的全部用例都在 call 阶段失败；mixed = 只有部分参数实例失败；
    green = 全跑了但没红；missing = 没跑到。
    声明的每条都必须 red 才算这一组有鉴别力（#370）：green / mixed / missing 都没有被证明有鉴别力，
    不能搭同组其它测试的便车。只看 junit 的结构位，与 classify_counterexample 同源；
    generic 框架没有 junit，调用方不记这一项。"""
    out: dict[str, str] = {}
    for t in test_ids:
        matched = match_cases(t, cases)
        if not matched:
            out[t] = "missing"
            continue
        failed = sum(1 for c in matched if c["outcome"] == "failure")
        if failed == len(matched):
            out[t] = "red"
        elif failed:
            out[t] = "mixed"
        else:
            out[t] = "green"
    return out


def undiscriminated(per_id: dict[str, str]) -> list[str]:
    """反例下没有全部变红的声明 id（green / mixed / missing）。proof 的 PASS 不认它们（#370）。"""
    return sorted(t for t, v in per_id.items() if v != "red")


UNDISCRIMINATED_MESSAGE = "proof_spec 只列用于证明的测试，以下 test_id 在反例下没有全部变红"


def classify_counterexample(framework: str, rc: int, cases: list[dict[str, str]], test_ids: list[str]) -> str:
    """pass / assertion-failure / error。判据取 junit 的结构位，不看失败信息的文本（原则四；#114 #255
    是同一判据两个方向的误判）：<error> 是 setup / teardown / collection 出错，测试根本没跑起来，没有
    鉴别力；<failure> 是 call 阶段失败，测试跑到了且被破坏的实现让它红了——KeyError、解包错同样算数。
    error 的检查范围是全场而非声明 id：collection 错误可能挂在别的 node 上。"""
    if rc == 0:
        return "pass"
    if framework != "pytest":
        return "assertion-failure"  # generic 只看退出码（仅允许 mutation）
    if rc != 1 or any(c["outcome"] == "error" for c in cases):
        return "error"
    declared = [c for t in test_ids for c in match_cases(t, cases)]
    if any(c["outcome"] == "failure" for c in declared):
        return "assertion-failure"
    return "error"


def _run(cwd: Path, argv: list[str], timeout: int, log_path: Path, env: dict[str, str]) -> tuple[int, str, bool]:
    """返回 (returncode, 完整日志, 是否被 runtime 按 timeout 终止)。
    timeout 是 runtime 观察到的执行事实，不能与 runner 自己返回 124 混为一谈（#320，原则一）。"""
    timed_out = False
    with open(log_path, "wb") as log:
        log.write(("$ " + " ".join(shlex.quote(a) for a in argv) + "\n").encode())
        log.flush()
        try:
            # 进程创建 / 超时 / 整组回收共用门禁执行边界（#363）：preflight、machine 与此处同一实现
            rc, timed_out = process.run(argv, cwd=cwd, env=env, timeout=timeout, stdout=log)
        except FileNotFoundError as exc:
            rc = 127
            log.write(f"\n[NOT FOUND] {exc}\n".encode())
        if timed_out:
            rc = 124
            log.write(f"\n[TIMEOUT] 超过 {timeout}s\n".encode())
    return rc, log_path.read_bytes().decode("utf-8", "replace"), timed_out


def _log_excerpt(log_path: Any, limit: int = 24000) -> str:
    """重复失败签名读完整日志的尾部窗口：这只是 runtime 内部的归一化输入，不是对外输出的第二事实视图（#365）。
    日志读不到时退回空串——签名退化为按 code / behavior / returncode 区分，不伪造内容。"""
    if not log_path:
        return ""
    try:
        return Path(str(log_path)).read_bytes().decode("utf-8", "replace")[-limit:]
    except OSError:
        return ""


class _Failure(Exception):
    def __init__(self, code: str, message: str, group: int, behavior: str, **details: Any):
        super().__init__(message)
        self.code, self.message, self.group, self.behavior, self.details = code, message, group, behavior, details


def _group_timeout(phase: str, i: int, bid: str, g: dict[str, Any], rc: int, log: Path) -> _Failure:
    """runtime 按 proof_spec 组 timeout 终止执行：参数由 tester 冻结，归 tester 处理（#320）。
    超时是没观察到结果，不是观察到失败（原则一）：因此不落到 runner 故障，也不进入鉴别力判定。"""
    return _Failure("TEST_PROOF_GROUP_TIMEOUT",
                    f"{phase} 阶段在组 timeout {g['timeout']}s 内没有跑完：这是 proof_spec 里的超时参数，不是 runner 故障",
                    i, bid, phase=phase, timeout=g["timeout"], returncode=rc, log=str(log))


def _restore_worktree(wt: Path, links: Sequence[str] = ()) -> tuple[list[str], list[str]]:
    """返回 (被修改的 tracked 路径, 被清理的 untracked 路径)。

    `links` 是 run 起点声明的依赖链接。只有**当前真的是符号链接**的那几条会被保留（`git clean` 会把
    它们当未跟踪文件删掉）：否则候选在后续组里失去依赖（#383）。同名但被 git 跟踪的真实目录照常清理——
    它内部新写的文件仍算现场改动，那是写入者的行为，不是 runtime 的链接。
    """
    entries = gitx.status_porcelain(wt)
    raw_untracked = [p for xy, p in entries if xy == "??"]
    declared = set(links)
    keep = [p for p in raw_untracked if p in declared and (wt / p).is_symlink()]
    tracked = [p for xy, p in entries if xy != "??"]
    untracked = [p for p in raw_untracked if p not in keep]
    if raw_untracked:
        argv = ["clean", "-fdq"]
        for rel in keep:
            argv.extend(["-e", rel])
        gitx.git(wt, *argv, check=False)
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
    if evidence.role_running(lg, "tester"):
        # tester 在交卷途中：ledger 里的 proof_spec 可能还是上一轮的，按它判出的失败是白跑（#250，原则一）
        raise negative("PROOF_TESTER_RUNNING", "tester 还在运行，proof_spec 可能尚未登记；等它交卷后再跑 proof")
    blocked = evidence.proof_blocked(lg, repo_root)
    if blocked:
        raise needs_user("PROOF_BLOCKED", "同一 proof 失败已重复出现；用 AskUserQuestion 让用户决定，继续则 `bl resume --reason`", blockers=blocked)
    machine_passed = evidence.state(lg, "machine", repo_root) == evidence.STATE_PASS
    if evidence.state(lg, "tester", repo_root) != evidence.STATE_PASS:
        raise negative("PROOF_PREREQ_TESTER", "tester evidence 不是 fresh pass，先完成 tester", state=evidence.state(lg, "tester", repo_root))
    if evidence.needs_integrate(lg, repo_root):
        raise negative("PROOF_PREREQ_INTEGRATE", "候选上的测试与 tester 分支不一致，先 `bl integrate`")
    spec = spec_override or lg.get("proof_spec")
    if not spec:
        raise negative("PROOF_SPEC_MISSING", "ledger 没有 proof_spec（tester 结果里应携带）")
    spec = check_structure(spec, lg, repo_root)  # patch 可应用性留给 ③ 段：那里失败归 tester（TEST_MUTATION_INVALID）

    cand = lg["candidate"]
    wt = Path(cand["worktree"])
    worktree.assert_candidate_identity(wt, cand["branch"], cand["head"])
    worktree.assert_candidate_clean(wt, worktree.link_ignores(lg))
    runner = lg["contract"]["assurance"].get("proof_runner") or {"framework": "pytest", "cmd": "python3 -m pytest"}
    framework = runner.get("framework", "pytest")
    link_paths = list(runner.get("link_paths") or [])
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
    guard_candidate: dict[tuple[int, int], dict[str, Any]] = {}
    failure: _Failure | None = None

    def execute(where: Path, g: dict[str, Any], tag: str, i: int) -> tuple[int, str, list[dict[str, str]], Path, bool]:
        junit = tmp_dir / f"junit-{attempt}-g{i}-{tag}.xml"
        junit.unlink(missing_ok=True)
        log = log_dir / f"proof-{attempt}-g{i}-{tag}.log"
        rc, out, timed_out = _run(where, build_argv(runner, g["test_ids"], junit, repo_root), g["timeout"], log, env)
        return rc, out, (parse_junit(junit) if framework == "pytest" else []), log, timed_out

    def counterexample_mutation(g: dict[str, Any], i: int, bid: str, patch: str, test_ids: list[str],
                                tag: str, guard: int | None, store: dict[str, Any], phase: str) -> None:
        """打上 patch 后，声明的 id 必须在 call 阶段全部失败。mutation 组与守卫共用这一段（#399，原则二）：
        两者要证明的是同一件事——这条测试确实锁住了被破坏的那行。
        store：mutation 组写 `store["counterexample"]`，守卫写进它自己的条目；失败也先写现场再抛（既有行为）。"""
        detail: dict[str, Any] = {} if guard is None else {"guard": guard}
        sub = {"test_ids": test_ids, "timeout": g["timeout"]}
        control_phase = "control" if guard is None else f"guard{guard}-control"
        ppaths = patch_paths(patch)
        for p in ppaths:
            reason = contract_mod.write_rejection(auth, contract_mod.OWNER_BUILDER, p)
            if reason:
                raise _Failure("TEST_MUTATION_INVALID", f"patch 只能改 builder 拥有的文件：{p} → {reason}", i, bid, path=p, reason=reason, **detail)
            if gitx.blob_mode(repo_root, cand["head"], p) not in ("100644", "100755"):
                raise _Failure("TEST_MUTATION_INVALID", f"patch 触及候选上不存在的普通文件 {p}", i, bid, path=p, **detail)
        with worktree.temp_worktree(repo_root, cand["head"], tmp_dir, f"{tag}-g{i}") as tw:
            worktree.link_paths(wt, tw, link_paths)
            if framework != "pytest":
                # 对照实验（#375）：同一现场先不打 patch 跑一次。命令起不来时非 0，那是零观察，
                # 不能落到 assertion-failure——只比较成败翻转，不解读输出（原则一、四）
                c_rc, c_out, c_cases, c_log, c_timed_out = execute(tw, sub, f"{tag}-control", i)
                if c_timed_out:
                    raise _group_timeout(control_phase, i, bid, sub, c_rc, c_log)
                _restore_worktree(tw, link_paths)
                worktree.link_paths(wt, tw, link_paths)  # clean -fd 会把链接当未跟踪文件删掉，补回来
                if c_rc != 0:
                    raise _Failure("TEST_PROOF_RUNNER_FAILED",
                                   f"mutation 现场在打 patch 前就跑不过（退出码 {c_rc}）：runner 起不来或依赖缺失，"
                                   "这次的「破坏后失败」证明不了任何事",
                                   i, bid, phase=control_phase, returncode=c_rc, log=str(c_log), cmd=runner["cmd"], **detail)
            text = normalized_patch(patch)
            ap = gitx.git(tw, "apply", "--whitespace=nowarn", "-", check=False, input_text=text)
            if not ap.ok:
                raise _Failure("TEST_MUTATION_INVALID", "mutation patch 无法应用到候选", i, bid, stderr=ap.stderr[-2000:], **detail)
            changed = sorted(p for _, p in gitx.status_porcelain(tw) if not worktree.under_link(p, link_paths))
            if changed != ppaths:
                raise _Failure("TEST_MUTATION_INVALID", "patch 实际改动路径与声明不符", i, bid, declared=ppaths, actual=changed, **detail)
            before = gitx.git(tw, "diff", "--binary", "--full-index").stdout
            rc, out, cases, log, timed_out = execute(tw, sub, tag, i)
            after = gitx.git(tw, "diff", "--binary", "--full-index").stdout
            extra = [p for _, p in gitx.status_porcelain(tw) if p not in changed and not _ignorable(p) and not worktree.under_link(p, link_paths)]
            if after != before or gitx.head(tw) != cand["head"] or extra:
                raise _Failure("TEST_MUTATION_INVALID", "执行期间 mutation 现场被改动", i, bid, extra_paths=extra, **detail)
        if timed_out:
            raise _group_timeout(phase, i, bid, sub, rc, log)
        cls = classify_counterexample(framework, rc, cases, test_ids)
        per_id = discrimination(cases, test_ids) if framework == "pytest" else {}
        record = {"kind": KIND_MUTATION, "returncode": rc, "classification": cls, "log": str(log),
                  "patch_sha256": sha256_bytes(patch.encode()), "patch_paths": ppaths, "per_id": per_id}
        if guard is None:
            store["counterexample"] = record
        else:
            store.update(record)
        not_red = undiscriminated(per_id)
        if cls != "assertion-failure":
            # error / pass：先报「测试没跑起来 / 没约束行为」。带上 per_id 与未全红 id
            raise _Failure("TEST_MUTATION_SURVIVED", "破坏实现后测试没有在 call 阶段失败（pass=测试没约束该行为；error=测试根本没跑起来，收集或 setup 阶段就出错）", i, bid, classification=cls, returncode=rc, per_id=per_id, undiscriminated=not_red, log=str(log), **detail)
        if not_red:
            raise _Failure("TEST_MUTATION_SURVIVED", f"{UNDISCRIMINATED_MESSAGE}：{', '.join(not_red)}", i, bid, classification=cls, returncode=rc, per_id=per_id, undiscriminated=not_red, log=str(log), **detail)

    gate = evidence.gate_lock(lg, evidence.GATE_PROOF)  # 全套测试要跑好几轮，执行期间对外可见（#242）
    gate.__enter__()
    pycache = private_pycache(env, run_dir)
    pycache.__enter__()
    try:
        for i, g in enumerate(spec["groups"]):
            if g["kind"] == KIND_MUTATION and not (g.get("patch") or "").strip():
                raise _Failure("TEST_MUTATION_PATCH_MISSING", "mutation 组缺 patch：集成后已可读候选，请续接 tester 补一段只破坏该 behavior 的 unified diff", i, g["behavior_ids"][0])
            for k, gd in enumerate(guard_items(g)):
                if not (gd.get("patch") or "").strip():
                    raise _Failure("TEST_MUTATION_PATCH_MISSING",
                                   "守卫缺 patch：守卫的鉴别力也要证明——补一段只破坏它声称锁住的那处的 unified diff，把它填进该守卫的 patch_file",
                                   i, g["behavior_ids"][0], guard=k)

        if link_paths and any(g["kind"] in (KIND_BASELINE, KIND_MUTATION) or guard_items(g) for g in spec["groups"]):
            # 声明的路径必须真的在候选里：借不到就说明声明写错了，归 contract（#376）
            missing = worktree.missing_link_paths(wt, link_paths)
            if missing:
                gi, g = next((i, g) for i, g in enumerate(spec["groups"]) if g["kind"] in (KIND_BASELINE, KIND_MUTATION))
                raise _Failure("TEST_PROOF_RUNNER_FAILED",
                               "proof_runner.link_paths 声明的路径在候选 worktree 里不存在，反例现场无法与候选同源",
                               gi, g["behavior_ids"][0], phase="link", missing=missing, link_paths=link_paths)

        # ① 候选：逐 id 全部 passed
        for i, g in enumerate(spec["groups"]):
            bid = g["behavior_ids"][0]
            rc, out, cases, log, timed_out = execute(wt, g, "candidate", i)
            tracked, untracked = _restore_worktree(wt, worktree.link_ignores(lg))
            if timed_out:
                # 组超时是 tester 冻结的参数到期，不是「runner 起不来」，也不是测试没鉴别力（#320）
                raise _group_timeout("candidate", i, bid, g, rc, log)
            if framework == "pytest" and rc != 0 and not cases:
                # 一条 junit 记录都没有 = runner 根本没跑起来（收集错误也会写进 junit）。不是实现的锅（#241）
                raise _Failure("TEST_PROOF_RUNNER_FAILED", f"proof_runner 没能产出测试结果：`{runner['cmd']}` 退出码 {rc}", i, bid,
                               returncode=rc, log=str(log), cmd=runner["cmd"])
            verdict = judge_candidate(framework, rc, cases, g["test_ids"])
            groups_out.append({"behavior_id": bid, "kind": g["kind"], "candidate": {"returncode": rc, "per_id": verdict["per_id"], "log": str(log), "residue_cleaned": untracked}})
            if tracked:
                raise _Failure("PROOF_WORKTREE_MUTATED", "测试命令修改了候选中的 tracked 文件", i, bid, paths=tracked[:50], log=str(log))
            if not verdict["ok"]:
                not_run = [t for t, v in verdict["per_id"].items() if v in ("missing", "skipped")]
                code = "TEST_PROOF_NOT_EXECUTED" if (rc == 0 and not_run) else "TEST_PROOF_CANDIDATE_FAILED"
                raise _Failure(code, verdict["reason"] or "测试在候选实现上未通过", i, bid, returncode=rc, per_id=verdict["per_id"], log=str(log))
            # 守卫也要先在候选上全绿：打上 patch 后变红，才说明它锁住的正是这处代码（#399）
            for k, gd in enumerate(guard_items(g)):
                gsub = {"test_ids": gd["test_ids"], "timeout": g["timeout"]}
                grc, gout, gcases, glog, gtimed_out = execute(wt, gsub, f"guard{k}-candidate", i)
                gtracked, gun = _restore_worktree(wt, worktree.link_ignores(lg))
                if gtimed_out:
                    raise _group_timeout(f"guard{k}-candidate", i, bid, gsub, grc, glog)
                if framework == "pytest" and grc != 0 and not gcases:
                    raise _Failure("TEST_PROOF_RUNNER_FAILED", f"proof_runner 没能产出测试结果：`{runner['cmd']}` 退出码 {grc}", i, bid,
                                   returncode=grc, log=str(glog), cmd=runner["cmd"], guard=k)
                gverdict = judge_candidate(framework, grc, gcases, gd["test_ids"])
                guard_candidate[(i, k)] = {"returncode": grc, "per_id": gverdict["per_id"], "residue_cleaned": gun}
                if gtracked:
                    raise _Failure("PROOF_WORKTREE_MUTATED", "测试命令修改了候选中的 tracked 文件", i, bid, paths=gtracked[:50], log=str(glog), guard=k)
                if not gverdict["ok"]:
                    gnot_run = [t for t, v in gverdict["per_id"].items() if v in ("missing", "skipped")]
                    gcode = "TEST_PROOF_NOT_EXECUTED" if (grc == 0 and gnot_run) else "TEST_PROOF_CANDIDATE_FAILED"
                    raise _Failure(gcode, gverdict["reason"] or "守卫测试在候选实现上未通过", i, bid, returncode=grc, per_id=gverdict["per_id"], log=str(glog), guard=k)

        # ②③④ 反例
        for i, g in enumerate(spec["groups"]):
            bid = g["behavior_ids"][0]
            guards_out = []
            for k, gd in enumerate(guard_items(g)):
                entry: dict[str, Any] = {"candidate": guard_candidate.get((i, k), {})}
                counterexample_mutation(g, i, bid, gd["patch"], list(gd["test_ids"]), f"guard{k}", k, entry, f"guard{k}")
                guards_out.append(entry)
            if guards_out:
                groups_out[i]["guards"] = guards_out
            if g["kind"] == KIND_REVIEWED:
                groups_out[i]["counterexample"] = {"kind": KIND_REVIEWED, "outcome": "reviewed"}
                continue
            if g["kind"] == KIND_BASELINE:
                overlay = (cand["head"], files["present"], files["deleted"])
                with worktree.temp_worktree(repo_root, lg["repo"]["target_start_head"], tmp_dir, f"baseline-g{i}", overlay=overlay) as tw:
                    worktree.link_paths(wt, tw, link_paths)
                    rc, out, cases, log, timed_out = execute(tw, g, "baseline", i)
                if timed_out:
                    raise _group_timeout("baseline", i, bid, g, rc, log)
                cls = classify_counterexample(framework, rc, cases, g["test_ids"])
                per_id = discrimination(cases, g["test_ids"]) if framework == "pytest" else {}
                groups_out[i]["counterexample"] = {"kind": KIND_BASELINE, "returncode": rc, "classification": cls, "log": str(log), "per_id": per_id}
                not_red = undiscriminated(per_id)
                if cls != "assertion-failure":
                    # error / pass：先报「测试没跑起来 / 没约束行为」。带上 per_id 与未全红 id，全 green 时也能直接看到是哪几条
                    raise _Failure("TEST_BASELINE_RED_NOT_PROVEN", "起点 + 你的测试没有在 call 阶段失败（pass=测试没约束行为；error=测试根本没跑起来，多半是依赖了起点上不存在的接口导致收集失败，请改用 mutation）", i, bid, classification=cls, returncode=rc, per_id=per_id, undiscriminated=not_red, log=str(log))
                if not_red:
                    raise _Failure("TEST_BASELINE_RED_NOT_PROVEN", f"{UNDISCRIMINATED_MESSAGE}：{', '.join(not_red)}", i, bid, classification=cls, returncode=rc, per_id=per_id, undiscriminated=not_red, log=str(log))
                continue
            counterexample_mutation(g, i, bid, g["patch"], list(g["test_ids"]), "mutation", None, groups_out[i], "mutation")
    except _Failure as exc:
        failure = exc
    finally:
        pycache.__exit__(None, None, None)
        gate.__exit__(None, None, None)

    spec_digest = digest(spec)

    def void_if_input_changed(lg2: dict[str, Any]) -> Problem | None:
        """观察期间 proof 投影里的输入变了：结论对应不到确定输入，不能写成 evidence / failures（原则一，与 machine 同一判据）。"""
        changed = evidence.input_changes(lg, lg2, "proof", repo_root)
        if not changed:
            return None
        head_now = lg2["candidate"]["head"]
        ledger_mod.log_event(lg2, "proof_input_changed", changed_inputs=changed, attempt=attempt, head_at_start=cand["head"], head_now=head_now)
        return negative("PROOF_INPUT_CHANGED", "proof 执行期间输入变化（checkpoint / integrate / tester 交卷 / contract revise），本次观察作废：不记 evidence、不计失败；在新输入上直接重跑 `bl proof`",
                        changed_inputs=changed, head_at_start=cand["head"], head_now=head_now, attempt=attempt)

    if failure:
        sig_text = _log_excerpt(failure.details.get("log"))
        sig = failure_signature(f"{failure.code}\n{failure.behavior}\n{failure.details.get('classification', '')}\n{sig_text}", f"proof-{failure.code}", int(failure.details.get("returncode", -1) or -1))
        fdetails = dict(failure.details)
        fdetails["suggested_owner"] = OWNER_BY_FAILURE.get(failure.code, "builder")
        if failure.code == "TEST_PROOF_CANDIDATE_FAILED" and machine_passed:
            # 全量 machine 在同一候选上通过、单跑声明的用例却失败：最可能是测试依赖了全量顺序（#313）。
            # 判据是 machine evidence 的状态，不解析日志（原则四）
            fdetails["suggested_owner"] = "tester"
            fdetails["isolation_hint"] = ISOLATION_HINT
        voided: Problem | None = None
        with ledger_mod.mutate(ledger_path) as lg2:
            voided = void_if_input_changed(lg2)
            if voided is None:
                lg2["failures"]["proof"].append({"code": failure.code, "behavior": failure.behavior, "signature": sig, "at": ledger_mod.now_iso(), "attempt": attempt})
                evidence.record(lg2, "proof", "fail", {"attempt": attempt, "spec_digest": spec_digest, "groups": groups_out, "failure": {"code": failure.code, "message": failure.message, "group": failure.group, "behavior": failure.behavior, **fdetails, "signature": sig}}, repo_root)
                readiness = evidence.readiness(lg2, repo_root)
        if voided is not None:
            raise voided
        repeats = sum(1 for f in lg2["failures"]["proof"] if f["signature"] == sig)
        return {"result": "FAIL", "attempt": attempt, "failure": {"code": failure.code, "message": failure.message, "group": failure.group, "behavior": failure.behavior, "signature": sig, "repeat_count": repeats, **fdetails}, "groups": groups_out, "readiness": readiness}

    voided = None
    with ledger_mod.mutate(ledger_path) as lg2:
        voided = void_if_input_changed(lg2)
        if voided is None:
            evidence.record(lg2, "proof", "pass", {"attempt": attempt, "spec_digest": spec_digest, "runner": runner, "groups": groups_out}, repo_root)
            readiness = evidence.readiness(lg2, repo_root)
    if voided is not None:
        raise voided
    return {"result": "PASS", "attempt": attempt, "groups": groups_out, "readiness": readiness}
