"""brief：角色（tester / reviewer）视角的事实，唯一来源。

角色不靠"谁跟它说过什么"工作——SubagentStart 注入的上下文只在首次 spawn 生效，续接时不送达；
Builder 经 SendMessage 发来的消息在角色那边表现为工具结果后的一段文字，无从验真。所以这里把
"我能写哪些路径 / 候选能不能读 / 有哪些活等我干"全部从 ledger 与 git 现算，角色随时 `bl brief` 自取。
Builder 的消息只是门铃。

全部派生，不落盘（原则五）；写边界的判定直接调 contract.path_owner，不在这里复述规则（原则二）。
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import Any

from . import contract as contract_mod
from . import evidence, gitx
from .jsonutil import dumps
from .ledger import events_of, ledger_path
from .proof import stale_patch_groups
from .run import bl_bin

TESTER_RESULT_FORMAT = (
    'BUILDER_LOOP_RESULT: {"role":"tester","status":"pass|insufficient_spec","behaviors_covered":["B1"],'
    '"proof_spec":{"groups":[{"kind":"baseline-red|mutation|reviewed-boundaries","behavior_ids":["B1"],'
    '"test_ids":["tests/test_x.py::test_a"],"timeout":120,"patch_file":"<mutation 组：git diff 重定向生成的 patch 文件绝对路径；首轮看不到实现时省略>",'
    '"reviewed_boundaries":{"positive":[],"negative":[],"boundary":[],"invariant":[]}}]},"notes":""}'
)
# 每组 1 个 behavior 是 check_structure 的硬规则（#303）；patch 走文件是因为模型转述长 patch 会走样（#281）
RESULT_RULE = "proof_spec 每组的 behavior_ids 恰好 1 个；mutation 组用 patch_file 交 patch 文件的绝对路径（git diff 重定向生成），不要把 patch 正文抄进结果行。"
PATCH_HOWTO = ("生成 patch 时不要改候选 worktree 里的文件，也不要在里面跑命令：在你的 worktree 之外建一个临时目录并 git init，"
               "用 git show <候选分支>:<路径> 按原相对路径取出文件并提交，改完后 `git diff > <临时目录>/<behavior>.patch`，"
               "把这个文件的绝对路径填进该组的 patch_file。")
REVIEWER_RESULT_FORMAT = (
    'BUILDER_LOOP_RESULT: {"role":"reviewer","verdict":"pass|changes_requested|blocked",'
    '"findings":[{"severity":"blocking|major|minor","owner":"builder|tester|contract","file":"src/x.py","line":10,"summary":"..."}],'
    '"behaviors_verified":["B1"]}'
)


def _tester_todo(lg: dict[str, Any], repo_root: Path) -> list[dict[str, Any]]:
    """等 tester 干的活，每条都能回指 ledger 里的一处记录。"""
    todo: list[dict[str, Any]] = []
    st = evidence.state(lg, "tester", repo_root)
    rev = lg["contract"]["mission"].get("revision")
    rebased = _rebased_tester_files(lg, repo_root) if st == evidence.STATE_STALE else []
    if evidence.tester_rebase_conflicted(lg):
        t = lg["tester"]
        todo.append({"what": "resolve_rebase_conflict", "worktree": t["worktree"], "onto": lg["repo"]["target_start_head"],
                     "paths": gitx.unmerged_paths(Path(t["worktree"])),
                     "why": "目标分支改过你的测试文件，runtime 把你的分支 rebase 到新的目标分支 HEAD 时冲突了（rebase 停在你的 worktree 里）。"
                            "在你的 worktree 里逐个解冲突：保留目标分支的改动，再叠上你自己的改动；`git add` 后 `git -c core.editor=true rebase --continue`，"
                            "然后照常交卷（proof_spec 完整重交一遍）。"})
    elif st == evidence.STATE_MISSING:
        todo.append({"what": "write_tests", "why": "还没有 tester evidence：按 behaviors 写测试并交 proof_spec"})
    elif rebased:
        todo.append({"what": "confirm_rebased_tests", "paths": rebased,
                     "why": "目标分支改过这些测试文件，runtime 已把你的分支零冲突 rebase 过去，文件内容因此变了（你的改动叠在目标分支的新版本上）。"
                            "读一遍合并结果，确认你的测试仍然成立；需要就改，然后把完整 proof_spec 重新交一遍。"})
    elif st == evidence.STATE_STALE:
        todo.append({"what": "write_tests", "why": f"你交的 evidence 已失效（mission 现在是 revision {rev}，或测试文件被改过）：重新对着下面的 behaviors 交一遍"})

    if evidence.missing_patch(lg) and evidence.implementation_readable_by_tester(lg):
        groups = [g["behavior_ids"][0] for g in (lg.get("proof_spec") or {}).get("groups", [])
                  if g.get("kind") == "mutation" and not (g.get("patch") or "").strip()]
        todo.append({"what": "add_mutation_patch", "behaviors": groups,
                     "why": "这些 mutation 组还缺 patch：候选实现现在可读了，补一段只破坏该 behavior 的 unified diff，然后把完整 proof_spec 重新交一遍。"
                            + PATCH_HOWTO,
                     "candidate_branch": lg["candidate"]["branch"]})
    if evidence.implementation_readable_by_tester(lg):
        stale = stale_patch_groups(lg, repo_root)
        if stale:
            # rebase 或 builder 改实现之后，旧 patch 的上下文对不上了：原样重交必被拒（#305）
            todo.append({"what": "stale_mutation_patch", "behaviors": stale, "candidate_head": lg["candidate"]["head"],
                         "why": "这些 mutation 组的 patch 已打不到当前候选 HEAD 上（上下文变了）：对着当前候选重新生成，再把完整 proof_spec 重新交一遍。"
                                + PATCH_HOWTO,
                         "candidate_branch": lg["candidate"]["branch"]})

    proof_rec = lg["evidence"].get("proof") or {}
    failure = (proof_rec.get("details") or {}).get("failure") or {}
    if proof_rec.get("status") == "fail" and failure.get("suggested_owner") == "tester" \
            and evidence.last_role_result_at(lg, "tester") <= proof_rec.get("at", ""):
        todo.append({"what": "fix_proof", "code": failure.get("code"), "behavior": failure.get("behavior"),
                     "log": failure.get("log"),
                     # 续接轮只改一个 behavior 时也要交全量 spec：validate_spec 要求 groups 与 behaviors 一一对应（#260）
                     "why": f"{failure.get('message')} —— 修好后把**完整**的 proof_spec 重新交一遍："
                            "groups 必须覆盖全部 behavior，只处理其中一个也要把其余的 group 原样列上，少一个就会被判 PROOF_SPEC_INVALID"})

    mach = lg["evidence"].get("machine") or {}
    mfail = (mach.get("details") or {}).get("failure") or {}
    if mach.get("status") == "fail" and mfail.get("tester_files_mentioned") \
            and evidence.last_role_result_at(lg, "tester") <= mach.get("at", ""):
        todo.append({"what": "check_machine_failure", "files": mfail["tester_files_mentioned"], "log": mfail.get("log"),
                     "why": "machine 失败的日志里出现了你的测试文件；是测试本身写错就改，不是就在 notes 里说明"})

    findings = ((lg["evidence"].get("reviewer") or {}).get("details") or {}).get("findings", [])
    mine = [f for f in findings if f.get("owner") == "tester" and f.get("severity") in ("blocking", "major")]
    if mine and evidence.last_role_result_at(lg, "tester") <= (lg["evidence"].get("reviewer") or {}).get("at", ""):
        todo.append({"what": "fix_review_findings", "findings": mine, "why": "reviewer 把这些问题判给了 tester"})
    return todo


def _rebased_tester_files(lg: dict[str, Any], repo_root: Path) -> list[str]:
    """最近一次零冲突 rebase 让哪些 tester 文件内容变了（只在它之后 tester 还没交过卷时才算数）。"""
    evs = [e for e in events_of(lg, "tester_rebase") if e.get("status") == "rebased"]
    if not evs or evidence.last_role_result_at(lg, "tester") > evs[-1]["at"]:
        return []
    files = evidence.tester_files(lg, repo_root)
    paths = files["present"] + files["deleted"]
    if not paths:
        return []
    before = gitx.ls_tree_blobs(repo_root, evs[-1]["from_head"], paths)
    after = gitx.ls_tree_blobs(repo_root, evs[-1]["tester_head"], paths)
    return sorted(p for p in paths if before.get(p) != after.get(p))


LEDGER_HINT = "要看 evidence 细节就只读这个 ledger，不要在文件系统里搜索它。"
TARGET_DRIFT_HINT = ("目标分支在你上次审查后前进了。patch 未变不构成沿用上次结论的依据：本轮复审漂入的提交与候选的交互——"
                     "候选改过契约的函数是否有了新的调用方、漂入的文档是否引用了候选删改的东西、是否出现了重复实现。")


def _patch_id(repo_root: Path, base: str, head: str) -> str:
    diff = gitx.git(repo_root, "diff", "--no-color", base, head, check=False).stdout
    if not diff:
        return ""
    return gitx.git(repo_root, "patch-id", "--stable", check=False, input_text=diff).stdout.split(" ")[0].strip()


def _target_drift(lg: dict[str, Any], repo_root: Path) -> dict[str, Any] | None:
    """上次审查之后目标分支漂进来的东西（#280）。reviewer evidence 照常作废（原则一），这里只给复审要看的事实：
    旧基线 = merge-base(上次审过的候选, 当前 target_start_head)；全部从 git 与 evidence 派生，不落盘。"""
    reviewed = (((lg["evidence"].get("reviewer") or {}).get("details") or {}).get("reviewed_head"))
    to = lg["repo"]["target_start_head"]
    cand = lg["candidate"].get("head")
    if not reviewed or not cand:
        return None
    r = gitx.git(repo_root, "merge-base", reviewed, to, check=False)
    frm = r.stdout.strip()
    if not r.ok or not frm or frm == to:
        return None
    log = gitx.git(repo_root, "log", "--format=%h %s", f"{frm}..{to}", check=False).stdout
    paths = gitx.changed_paths(repo_root, frm, to)
    mine = set(gitx.changed_paths(repo_root, to, cand))
    return {
        "from": frm, "to": to,
        "commits": [line for line in log.splitlines() if line.strip()][:50],
        "paths": paths[:200],
        "intersecting_paths": sorted(mine.intersection(paths)),
        "patch_unchanged": _patch_id(repo_root, frm, reviewed) == _patch_id(repo_root, to, cand),
    }


UNDISCRIMINATED_HINT = ("下面这些 test_id 在反例下从未变红：它们没有被证明有鉴别力，可能是断言的条件在本设计下恒真。"
                        "逐条判断它是真的在约束 behavior，还是搭了同组其它测试的便车。")


def _undiscriminated(lg: dict[str, Any]) -> list[dict[str, str]]:
    """proof 证据里在反例下没红过的声明 id（#285）。只读 evidence，不另算一份事实（原则二）；
    旧 evidence 没有 per_id 就当没有这类信息。reviewed-boundaries 组不跑反例，不在此列。"""
    groups = ((lg["evidence"].get("proof") or {}).get("details") or {}).get("groups", [])
    out: list[dict[str, str]] = []
    for g in groups:
        ce = g.get("counterexample") or {}
        for tid, verdict in (ce.get("per_id") or {}).items():
            if verdict != "red":
                out.append({"behavior_id": g.get("behavior_id", ""), "test_id": tid, "counterexample": verdict})
    return out


def _reviewer_todo(lg: dict[str, Any]) -> list[dict[str, Any]]:
    rec = lg["evidence"].get("reviewer") or {}
    if not rec:
        return [{"what": "review", "why": "首轮审查"}]
    prev = (rec.get("details") or {}).get("findings", [])
    reviewed = (rec.get("details") or {}).get("reviewed_head")
    moved = reviewed and reviewed != lg["candidate"]["head"]
    return [{"what": "review", "why": "候选在你上次审查后又变了，请复审" if moved else "重新审查",
             "previous_findings": prev, "previously_reviewed_head": reviewed}]


DOC_LINT = Path(__file__).resolve().parents[2] / "skills" / "builder-loop" / "scripts" / "doc-lint.sh"
DOC_HINT_TIMEOUT = 4  # SubagentStart hook 超时 10s：线索算不出来只能降级，不能拖垮 brief


def _doc_reference_hints(lg: dict[str, Any]) -> dict[str, Any]:
    """候选删掉了定义而文档仍引用的启发式线索。每次现算，不落盘；任何失败都降级为 error。

    以候选 worktree 为 cwd、扫描根传 `.`：doc-lint 的 find 会排除 */.claude/* 与 */build/*，绝对路径落在这些段下会整体漏扫。
    """
    wt = lg["candidate"]["worktree"]
    if not Path(wt).is_dir():
        return {"hits": [], "error": f"候选 worktree 不存在: {wt}"}
    try:
        proc = subprocess.Popen(["bash", str(DOC_LINT), ".", lg["repo"]["target_start_head"]], cwd=wt,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            out, err = proc.communicate(timeout=DOC_HINT_TIMEOUT)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            return {"hits": [], "error": f"doc-lint 超过 {DOC_HINT_TIMEOUT} 秒未返回"}
    except Exception as e:  # noqa: BLE001 — brief 必须照常返回
        return {"hits": [], "error": f"doc-lint 无法运行: {e}"}
    if proc.returncode == 0:
        return {"hits": [], "error": None}
    if proc.returncode == 1:
        hits = [ln.strip() for ln in out.splitlines() if ln.startswith((" ", "\t")) and ln.strip()]
        return {"hits": hits, "error": None}
    return {"hits": [], "error": f"doc-lint 退出码 {proc.returncode}: {(err or out).strip()[:200]}"}


def build(lg: dict[str, Any], repo_root: Path, role: str) -> dict[str, Any]:
    c = lg["contract"]
    m, a, s = c["mission"], c["authority"], c["assurance"]
    out: dict[str, Any] = {
        "run_id": lg["run_id"],
        "role": role,
        "contract_revision": m.get("revision"),
        "contract_revised_times": len(c.get("history") or []),
        "mission": {"objective": m["objective"], "interfaces": m.get("interfaces", []),
                    "mock_strategy": m.get("mock_strategy"), "trust_boundaries": m.get("trust_boundaries", [])},
        "behaviors": [],
        # 角色要核对 evidence 细节时的定位信息（#304）：不给它就会去文件系统里 find，在 NFS 上能跑几个小时
        "ledger": str(ledger_path(Path(lg["repo"]["root"]), lg["run_id"])),
    }
    for b in m["behaviors"]:
        entry = {k: b[k] for k in ("id", "given", "when", "then")}
        for key in ("boundaries", "invariants"):
            if b.get(key):
                entry[key] = b[key]
        if role == "tester":
            floor = b.get("proof", contract_mod.PROOF_FLOOR_STRONG)
            entry["proof_kinds_allowed"] = (["baseline-red", "mutation", "reviewed-boundaries"]
                                            if floor == contract_mod.PROOF_FLOOR_REVIEWED else ["baseline-red", "mutation"])
        out["behaviors"].append(entry)

    if role == "tester":
        readable = evidence.implementation_readable_by_tester(lg)
        out["worktree"] = lg["tester"]["worktree"]
        # 写边界只说你能写什么：与 builder_write 重叠的部分也归你（path_owner 的裁决），列出对方的边界只会引起误会（#240）
        out["write_paths"] = a["tester_write"]
        out["write_rule"] = "tester_write 命中的路径归你（与 builder_write 重叠处也归你）；其余一律不要写。以 hook 是否放行为准"
        out["protected_paths"] = a.get("protected_paths", [])
        out["proof_runner"] = s.get("proof_runner", {})
        out["candidate_readable"] = readable
        out["candidate_worktree"] = lg["candidate"]["worktree"] if readable else None
        out["read_rule"] = ("你的测试已集成进候选，候选 worktree 可以只读访问"
                            if readable else
                            "你工作在 run 起点的冻结基线上：这里没有本次的实现，也不要去找它（候选 worktree、其他分支、`git log --all` 都不要碰）")
        out["todo"] = _tester_todo(lg, repo_root)
        out["result_format"] = TESTER_RESULT_FORMAT
    else:
        cand = lg["candidate"]
        ev = lg["evidence"]
        out["candidate_worktree"] = cand["worktree"]
        out["diff_range"] = f"{lg['repo']['target_start_head']}..{cand['head'] or 'HEAD'}"
        out["evidence"] = {k: (ev[k]["status"] if ev.get(k) else None) for k in ("machine", "tester", "proof")}
        out["review_focus"] = s.get("review_focus", [])
        out["todo"] = _reviewer_todo(lg)
        out["doc_reference_hints"] = _doc_reference_hints(lg)
        out["undiscriminated"] = _undiscriminated(lg)
        drift = _target_drift(lg, repo_root)
        if drift:
            out["target_drift"] = drift
        out["result_format"] = REVIEWER_RESULT_FORMAT
    return out


def _render_doc_hints(h: dict[str, Any]) -> list[str]:
    head = "文档引用线索（启发式 grep，可能误报；逐条判断，确实失效的按审查清单第 6 条处理）:"
    if h.get("error"):
        return [f"文档引用线索不可用: {h['error']}"]
    if not h["hits"]:
        return [head + " 无"]
    return [head, *[f"  - {x}" for x in h["hits"]]]


def _render_target_drift(d: dict[str, Any] | None) -> list[str]:
    if not d:
        return []
    return [TARGET_DRIFT_HINT,
            f"  漂入范围: {d['from'][:12]}..{d['to'][:12]}；候选 patch 与上次审过的{'相同' if d['patch_unchanged'] else '不同'}",
            f"  与候选改动相交的路径: {d['intersecting_paths'] or '无'}",
            "  漂入的提交: " + ("; ".join(d["commits"]) or "无"),
            "  漂入的路径: " + (", ".join(d["paths"]) or "无")]


def render(brief: dict[str, Any]) -> str:
    """同一份事实的文本形态：SubagentStart 注入与 `bl brief` 用的是同一个渲染。"""
    role = brief["role"]
    lines = [f"[builder-loop] 你是本 run 的 {role}。run_id={brief['run_id']}（contract revision {brief['contract_revision']}）",
             f"Mission: {brief['mission']['objective']}", "Behaviors:"]
    for b in brief["behaviors"]:
        lines.append(f"  - {b['id']}: given {b['given']} / when {b['when']} / then {b['then']}")
        for key, label in (("boundaries", "边界"), ("invariants", "不变量")):
            if b.get(key):
                lines.append(f"      {label}: " + "; ".join(b[key]))
        if b.get("proof_kinds_allowed"):
            lines.append("      允许的 proof kind: " + " / ".join(b["proof_kinds_allowed"]))
    if brief["mission"].get("interfaces"):
        lines.append("Interfaces: " + "; ".join(brief["mission"]["interfaces"]))
    if brief["mission"].get("mock_strategy"):
        lines.append("Mock 策略: " + dumps(brief["mission"]["mock_strategy"]))
    if brief["mission"].get("trust_boundaries"):
        lines.append("Trust boundaries: " + "; ".join(brief["mission"]["trust_boundaries"]))
    lines.append(f"本 run 的 ledger（只读）: {brief['ledger']}。{LEDGER_HINT}")

    if role == "tester":
        runner = brief["proof_runner"]
        lines += [
            f"你的 worktree（写测试的位置）: {brief['worktree']}",
            f"写边界 tester_write: {brief['write_paths']}；{brief['write_rule']}",
            f"protected（谁都不能动）: {brief['protected_paths']}",
            f"测试命令由项目冻结，不需要你给 argv：{runner.get('cmd')}（framework={runner.get('framework')}）；proof_spec 每组只给 test_ids（pytest node id，如 tests/test_x.py::test_a）",
            RESULT_RULE,
            brief["read_rule"] + "。",
            # 盲写阶段跑不了自己的测试，node id 拼错、断言与真实数据结构不符都要等这一步才暴露（#266）
            "你交卷后测试会被集成进候选，由 machine 全量跑一遍；那时失败的如果是你的测试，这条失败会回到 tester 手上。"
            "所以交卷前至少确认语法与收集无误（`--collect-only`），并逐条核对 proof_spec 里的 node id 与文件中的实际定义一致。",
        ]
        if brief["candidate_readable"]:
            lines.append(f"候选 worktree（只读）: {brief['candidate_worktree']}")
        else:
            lines.append("新接口在基线上无法 import，所以写完后只需保证语法与收集无误；baseline-red 只用于「起点上会断言失败」的行为（行为变更、bug 修复、功能移除的负向测试），新接口用 mutation 且先不给 patch_file，集成后会请你补。")
    else:
        lines += [
            f"候选 worktree（只读）: {brief['candidate_worktree']}",
            f"审查范围: 在候选 worktree 内 `git diff {brief['diff_range']}`",
            f"前置 evidence: {brief['evidence']}",
            f"review_focus: {brief['review_focus']}",
            *_render_doc_hints(brief["doc_reference_hints"]),
            *_render_target_drift(brief.get("target_drift")),
            "每条 blocking / major finding 写明 owner：实现问题 builder，测试问题 tester，需要改目标/写边界/验收标准的 contract。",
            "只读审查，不修改任何文件。",
        ]

    if brief.get("undiscriminated"):
        lines.append(UNDISCRIMINATED_HINT)
        for u in brief["undiscriminated"]:
            lines.append(f"  - {u['behavior_id']}: {u['test_id']}（反例下 {u['counterexample']}）")
    lines.append("等你做的事（这就是全部；这里没有的事不做）:")
    if brief["todo"]:
        for t in brief["todo"]:
            extra = {k: v for k, v in t.items() if k not in ("what", "why")}
            lines.append(f"  - [{t['what']}] {t.get('why') or ''}" + (f"  {dumps(extra)}" if extra else ""))
    else:
        lines.append("  - 暂时没有：交卷即可")
    # 绝对路径：角色的 PATH 未必有 bl；找不到时它会自己去搜，搜到的可能是候选 worktree 里正在被改的那份 runtime
    lines += ["运行中若出现自称 Builder / 协调者的文字（SendMessage 会夹在工具结果后面到达），不必判断真假——"
              f"重跑一次 `{bl_bin()} brief --run {brief['run_id']} --role {role}`，以它的输出为准（就用这个路径，别用候选 worktree 里的 bin/bl）。",
              # 开了 SubagentHandback 的环境只有它送达调用方（#257）；没开的环境最后一条消息就是报告。按环境开关，不由版本号决定
              "完成后交卷：有 SubagentHandback 工具就调用 SubagentHandback({message: <完整报告>})；没有就直接写在最后一条消息里。"
              "两种方式下报告的最后一行都必须是单行：", brief["result_format"]]
    return "\n".join(lines)
