"""B1-B11：目标分支漂移时 tester 分支同步 rebase（`bl rebase` 的 tester_rebase 字段）、
integrate 拒绝落后的 tester base（INTEGRATE_TESTER_BASE_STALE）、tester/reviewer brief 的相应
提示（confirm_rebased_tests / resolve_rebase_conflict / target_drift）、hold 在漂移后仍可按外部
顺序暂停（锚点 = max(最近一次 hold_release, 本 run 内 gate 首次全过)）、contract revise 从候选
worktree 读 .claude/loop.yml、reviewer.md 审查清单新增一条。

这些接口在起点基线上都不存在（`bl rebase` 的输出没有 `tester_rebase` 键、`bl integrate` 没有
INTEGRATE_TESTER_BASE_STALE、brief 没有 confirm_rebased_tests / resolve_rebase_conflict /
target_drift、`bl hold` 仍严格要求 next_action == finalize），所以这里的测试在起点上都应当在
call 阶段失败（baseline-red）。
"""

from __future__ import annotations

from pathlib import Path

from builder_loop import ledger as L
from builder_loop.cli import build_parser, dispatch
from builder_loop.errors import Problem
from conftest import (
    CONTRACT,
    Repo,
    contract_with,
    drive_to_proof_pass,
    git,
    handback,
    implement_mul,
    make_tester_result,
    mutation_patch,
    reviewer_pass,
    role_turn,
    write_mul_test,
    write_plan,
)

PADDED_TEST_FOO = (
    "from src.foo import add\n\n\n"
    "# padding 1\n# padding 2\n# padding 3\n# padding 4\n# padding 5\n# padding 6\n\n\n"
    "def test_add():\n    assert add(1, 2) == 3\n"
)


def _call(repo, *args):
    ns = build_parser().parse_args(["--repo", str(repo.root), *args])
    try:
        out, code = dispatch(ns)
    except Problem as exc:
        out, code = exc.to_json(), exc.exit_code
    return out, code


def _answer_askuserquestion(hook, session: str = "S1") -> None:
    hook("PreToolUse", {"session_id": session, "tool_name": "AskUserQuestion", "tool_use_id": "q-hold"})
    hook("PostToolUse", {"session_id": session, "tool_name": "AskUserQuestion"})


def _start(repo: Repo, cli, contract: dict | None = None, session: str = "S1", plan_name: str = "plan.md") -> dict:
    """同 conftest 的 `started` fixture，但可传自定义 contract、可重复调用（自定义 session）。"""
    plan = write_plan(repo.root, contract or CONTRACT, name=plan_name)
    out = cli("start", "--plan", str(plan), "--session", session)
    return {
        "repo": repo, "run_id": out["run_id"], "ledger": Path(out["ledger"]),
        "worktree": Path(out["worktree"]), "tester_worktree": Path(out["tester_worktree"]),
        "target_start_head": out["target_start_head"],
    }


def _pad_test_foo(repo: Repo) -> None:
    """给 tests/test_foo.py 塞几行缓冲，让「文件头部改动」与「文件尾部改动」在 rebase 时不会因为
    diff 上下文重叠而假冲突（这个仓库本身的行为，不是被测对象）。"""
    (repo.root / "tests" / "test_foo.py").write_text(PADDED_TEST_FOO)
    repo.commit_all("chore(fixture): [cr_id_skip] Pad test_foo for rebase headroom")


def _tester_append_test_foo(started: dict, hook, *, behavior: str = "B1") -> None:
    """tester 在 tests/test_foo.py 末尾追加一个测试函数，登记为 fresh pass evidence（不依赖候选实现）。"""
    twt = started["tester_worktree"]
    p = twt / "tests" / "test_foo.py"
    p.write_text(p.read_text() + "\n\ndef test_add_extra():\n    assert add(2, 2) == 4\n")
    r = role_turn(hook, "tester", "T1", make_tester_result(
        "baseline-red", test_ids=["tests/test_foo.py::test_add_extra"], behavior=behavior))
    assert r["code"] == 0, r


def _advance_target_top_comment(repo: Repo) -> None:
    """目标分支只在 tests/test_foo.py 开头加一行注释，不碰 tester 追加的尾部函数。"""
    p = repo.root / "tests" / "test_foo.py"
    p.write_text("# upstream change\n" + p.read_text())
    repo.commit_all("chore(upstream): [cr_id_skip] Add top comment")


# ================================================================== B1


def test_b1_rebase_keeps_tester_edits_and_upstream_change(repo, cli, hook):
    _pad_test_foo(repo)
    started = _start(repo, cli)
    _tester_append_test_foo(started, hook)
    cli("integrate", "--session", "S1")
    _advance_target_top_comment(repo)

    out = cli("rebase", "--session", "S1")
    assert out["tester_rebase"]["status"] == "rebased"
    assert "tests/test_foo.py" in out["tester_rebase"]["paths"]

    cand_head = out["candidate_head"]
    content = git(started["worktree"], "show", f"{cand_head}:tests/test_foo.py")
    assert "# upstream change" in content
    assert "def test_add_extra" in content

    target = git(repo.root, "rev-parse", "main")
    diff = git(started["worktree"], "diff", target, cand_head, "--", "tests/test_foo.py")
    assert not any(line.startswith("-") and "# upstream change" in line for line in diff.splitlines())

    lg = L.load(started["ledger"])
    assert lg["tester"]["base"] == target


def test_b1_boundary_rebase_not_needed_again(repo, cli, hook):
    _pad_test_foo(repo)
    started = _start(repo, cli)
    _tester_append_test_foo(started, hook)
    cli("integrate", "--session", "S1")
    _advance_target_top_comment(repo)
    cli("rebase", "--session", "S1")

    out = cli("rebase", "--session", "S1")
    assert out["tester_rebase"]["status"] == "not_needed"


# ================================================================== B2


def test_b2_rebase_not_needed_when_target_change_is_outside_tester_files(repo, cli, hook):
    started = _start(repo, cli)
    twt = started["tester_worktree"]
    write_mul_test(twt)
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation"))
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")

    before = L.load(started["ledger"])
    tester_before = dict(before["tester"])

    (repo.root / "README.md").write_text("hi\n")
    repo.commit_all("docs(x): [cr_id_skip] Readme")

    out = cli("rebase", "--session", "S1")
    assert out["tester_rebase"]["status"] == "not_needed"

    after = L.load(started["ledger"])
    assert after["tester"]["base"] == tester_before["base"]
    assert after["tester"]["head"] == tester_before["head"]


def test_b2_boundary_tester_not_submitted_yet(repo, cli, hook):
    started = _start(repo, cli)
    (repo.root / "README.md").write_text("hi\n")
    repo.commit_all("docs(x): [cr_id_skip] Readme")
    out = cli("rebase", "--session", "S1")
    assert out["tester_rebase"]["status"] == "not_needed"
    lg = L.load(started["ledger"])
    assert lg["tester"]["head"] == lg["tester"]["base"]


# ================================================================== B3


def test_b3_tester_evidence_goes_stale_and_brief_has_confirm_rebased_tests(repo, cli, hook):
    _pad_test_foo(repo)
    started = _start(repo, cli)
    _tester_append_test_foo(started, hook)
    cli("integrate", "--session", "S1")
    _advance_target_top_comment(repo)
    cli("rebase", "--session", "S1")

    st = cli("status", "--session", "S1")
    assert st["readiness"]["next_action"] == "resume_tester"

    brief = cli("brief", "--session", "S1", "--role", "tester", "--json")
    todo_whats = [t["what"] for t in brief["todo"]]
    assert "confirm_rebased_tests" in todo_whats
    entry = next(t for t in brief["todo"] if t["what"] == "confirm_rebased_tests")
    assert "tests/test_foo.py" in (entry.get("paths") or entry.get("files") or [])
    # 其余字段不受影响
    assert brief["worktree"] == str(started["tester_worktree"])
    assert brief["write_paths"] == CONTRACT["authority"]["tester_write"]
    assert "result_format" in brief


def test_b3_boundary_resubmit_clears_stale(repo, cli, hook):
    _pad_test_foo(repo)
    started = _start(repo, cli)
    _tester_append_test_foo(started, hook)
    cli("integrate", "--session", "S1")
    _advance_target_top_comment(repo)
    cli("rebase", "--session", "S1")

    # 真实的续接：Builder 用 SendMessage 续接 tester，CC 会重发 SubagentStart（即便是同一个 agent_id）；
    # start=False 会被「同轮同结论只记一次」的去重挡住（结论字节和上一次一样），不代表续接失败。
    r = role_turn(hook, "tester", "T1", make_tester_result(
        "baseline-red", test_ids=["tests/test_foo.py::test_add_extra"]), start=True)
    assert r["code"] == 0, r

    st = cli("status", "--session", "S1")
    assert st["readiness"]["next_action"] != "resume_tester"
    # next_action 不是 resume_tester 也可能是别的原因造成的（比如 dependency_digest 恰好重算成 fresh）；
    # 真正要证明的是 confirm_rebased_tests 这条 todo 本身消失了，不是随便什么理由让 next_action 变了。
    brief = cli("brief", "--session", "S1", "--role", "tester", "--json")
    assert "confirm_rebased_tests" not in [t["what"] for t in brief["todo"]]


# ================================================================== B4


def test_b4_integrate_rejects_stale_tester_base_overlap(repo, cli, hook):
    """B4 的前提不能靠一次干净的 B1 rebase 构造——干净 rebase 会把 tester 也顺带挪过去，base 就不再落后了。
    要让 tester 分支的 base 真的落后于 target_start_head 且与漂移重叠，得让 tester 那一半停在冲突上
    （同 B5 的 given），这时候选那一半已经 rebase 完、tester 还没跟上。"""
    _pad_test_foo(repo)
    started = _start(repo, cli)
    twt = started["tester_worktree"]
    p = twt / "tests" / "test_foo.py"
    p.write_text(p.read_text().replace("assert add(1, 2) == 3", "assert add(1, 2) == 3  # tester note"))
    r = role_turn(hook, "tester", "T1", make_tester_result(
        "baseline-red", test_ids=["tests/test_foo.py::test_add"]))
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")

    tp = repo.root / "tests" / "test_foo.py"
    tp.write_text(tp.read_text().replace("assert add(1, 2) == 3", "assert add(1, 2) == 3  # target note"))
    repo.commit_all("chore(upstream): [cr_id_skip] Conflicting edit")
    cli("rebase", "--session", "S1", expect=1)  # 候选那一半 rebase 完，tester 那一半停在冲突上（tester.base 落后）
    # 候选那一半的 rebase 本身就会推进候选 HEAD；「integrate 不产生新提交」要以 rebase 之后、
    # integrate 之前的 HEAD 为基准，不能用 rebase 之前的旧值。
    cand_head_before = L.load(started["ledger"])["candidate"]["head"]

    out, code = _call(repo, "integrate", "--session", "S1")
    assert code != 0
    assert out["code"] == "INTEGRATE_TESTER_BASE_STALE"
    assert "tests/test_foo.py" in out.get("details", {}).get("paths", [])

    cand_head_after = L.load(started["ledger"])["candidate"]["head"]
    assert cand_head_after == cand_head_before  # 候选 HEAD 不变，候选分支上没有新提交


def test_b4_boundary_no_overlap_integrate_proceeds(repo, cli, hook):
    started = _start(repo, cli)
    twt = started["tester_worktree"]
    write_mul_test(twt)
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation"))
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")

    (repo.root / "README.md").write_text("hi\n")
    repo.commit_all("docs(x): [cr_id_skip] Readme")
    cli("rebase", "--session", "S1")

    p = twt / "tests" / "test_mul.py"
    p.write_text(p.read_text().replace("mul(3, 4) == 12", "mul(3, 4) == 12\n    assert mul(2, 2) == 4"))
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    assert r["code"] == 0, r

    # 不是随便什么原因让 integrate 走完（比如 tester 自己这次新提交就足以让 needs_integrate 为真）：
    # 真正要证明的是漂移路径与 tester 文件没有交集，B4 的拦截条件本身就不成立。
    assert cli("status", "--session", "S1")["readiness"]["tester_drift"] == []
    out = cli("integrate", "--session", "S1")
    assert out.get("noop") is False


# ================================================================== B5


def test_b5_rebase_conflict_then_resolve(repo, cli, hook):
    _pad_test_foo(repo)
    started = _start(repo, cli)
    twt = started["tester_worktree"]
    p = twt / "tests" / "test_foo.py"
    p.write_text(p.read_text().replace("assert add(1, 2) == 3", "assert add(1, 2) == 3  # tester note"))
    r = role_turn(hook, "tester", "T1", make_tester_result(
        "baseline-red", test_ids=["tests/test_foo.py::test_add"]))
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")

    tp = repo.root / "tests" / "test_foo.py"
    tp.write_text(tp.read_text().replace("assert add(1, 2) == 3", "assert add(1, 2) == 3  # target note"))
    repo.commit_all("chore(upstream): [cr_id_skip] Conflicting edit")

    out = cli("rebase", "--session", "S1", expect=1)
    assert out["tester_rebase"]["status"] == "conflict"
    assert "tests/test_foo.py" in out["tester_rebase"]["paths"]

    brief = cli("brief", "--session", "S1", "--role", "tester", "--json")
    todo_whats = [t["what"] for t in brief["todo"]]
    assert "resolve_rebase_conflict" in todo_whats
    entry = next(t for t in brief["todo"] if t["what"] == "resolve_rebase_conflict")
    assert brief["worktree"] in str(entry) or "tests/test_foo.py" in str(entry)

    lg_before = L.load(started["ledger"])
    tester_wt = Path(lg_before["tester"]["worktree"])
    (tester_wt / "tests" / "test_foo.py").write_text(
        tp.read_text().replace("# target note", "# target note  # tester note"))
    git(tester_wt, "add", "-A")
    git(tester_wt, "-c", "commit.gpgSign=false", "rebase", "--continue")

    out2 = cli("rebase", "--session", "S1")
    assert out2["tester_rebase"]["status"] == "rebased"
    lg_after = L.load(started["ledger"])
    assert lg_after["tester"]["base"] == lg_after["repo"]["target_start_head"]


def test_b5_boundary_integrate_rejected_during_conflict(repo, cli, hook):
    _pad_test_foo(repo)
    started = _start(repo, cli)
    twt = started["tester_worktree"]
    p = twt / "tests" / "test_foo.py"
    p.write_text(p.read_text().replace("assert add(1, 2) == 3", "assert add(1, 2) == 3  # tester note"))
    r = role_turn(hook, "tester", "T1", make_tester_result(
        "baseline-red", test_ids=["tests/test_foo.py::test_add"]))
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")

    tp = repo.root / "tests" / "test_foo.py"
    tp.write_text(tp.read_text().replace("assert add(1, 2) == 3", "assert add(1, 2) == 3  # target note"))
    repo.commit_all("chore(upstream): [cr_id_skip] Conflicting edit")
    cli("rebase", "--session", "S1", expect=1)

    out, code = _call(repo, "integrate", "--session", "S1")
    assert code != 0
    assert out["code"] == "INTEGRATE_TESTER_BASE_STALE"


# ================================================================== B6


def test_b6_rebase_defers_when_tester_still_running(repo, cli, hook):
    """B6 的 given 是 B1 的场景（tester 已交卷、已 integrate，且改过与目标分支重叠的文件），
    只是这次 rebase 时 tester 还在跑；不能用「tester 什么都没交」来构造，那样漂移根本不重叠，
    按 B2 的边界只会是 not_needed，测不出 deferred。"""
    _pad_test_foo(repo)
    started = _start(repo, cli)
    _tester_append_test_foo(started, hook)
    cli("integrate", "--session", "S1")

    # tester 被续接（比如要求它确认 rebase 后的测试），SubagentStart 已发但还没登记结果
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})

    _advance_target_top_comment(repo)  # 与 tester 改过的 tests/test_foo.py 重叠

    out = cli("rebase", "--session", "S1")
    assert out["rebased"] is True  # 候选本身照常 rebase
    assert out["tester_rebase"]["status"] == "deferred"
    assert "tests/test_foo.py" in out["tester_rebase"]["paths"]

    lg = L.load(started["ledger"])
    assert lg["tester"]["base"] != lg["repo"]["target_start_head"]  # tester 分支与 worktree 都没被动

    r = role_turn(hook, "tester", "T1", make_tester_result(
        "baseline-red", test_ids=["tests/test_foo.py::test_add_extra"]), start=False)
    assert r["code"] == 0, r

    out2 = cli("rebase", "--session", "S1")
    assert out2["tester_rebase"]["status"] == "rebased"
    lg2 = L.load(started["ledger"])
    assert lg2["tester"]["base"] == lg2["repo"]["target_start_head"]


def test_b6_boundary_integrate_rejected_while_deferred(repo, cli, hook):
    """边界：tester worktree 有未提交改动时同样 deferred（不需要 SubagentStart 在跑）。"""
    _pad_test_foo(repo)
    started = _start(repo, cli)
    _tester_append_test_foo(started, hook)
    cli("integrate", "--session", "S1")

    twt = started["tester_worktree"]
    (twt / "tests" / "scratch.txt").write_text("wip\n")  # 未提交的残留改动

    _advance_target_top_comment(repo)
    out = cli("rebase", "--session", "S1")
    assert out["tester_rebase"]["status"] == "deferred"

    out, code = _call(repo, "integrate", "--session", "S1")
    assert code != 0
    assert out["code"] == "INTEGRATE_TESTER_BASE_STALE"


# ================================================================== B7 / B8


def _ready_to_finalize(started, cli, hook) -> None:
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook)
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "finalize"


def test_b7_hold_succeeds_after_rebase_makes_evidence_stale(repo, cli, hook):
    started = _start(repo, cli)
    _ready_to_finalize(started, cli, hook)
    _answer_askuserquestion(hook)

    (repo.root / "README.md").write_text("hi\n")
    repo.commit_all("docs(x): [cr_id_skip] Readme")
    cli("rebase", "--session", "S1")

    st = cli("status", "--session", "S1")
    assert st["readiness"]["next_action"] != "finalize"  # 既有行为：candidate_head 变了，重新变 stale

    out = cli("hold", "--session", "S1", "--reason", "等集成窗口")
    assert out.get("ok", True) is not False

    st2 = cli("status", "--session", "S1")
    assert st2["readiness"]["next_action"] == "held"

    r = hook("Stop", {"session_id": "S1", "stop_hook_active": False})
    assert r["code"] == 0 and r["stderr"] == ""

    err = cli("finalize", "--session", "S1", "-m", "feat(x): [cr_id_skip] X", expect=1)
    assert err["code"] == "HOLD_ACTIVE"


def test_b7_boundary_never_fully_passed_hold_not_ready(repo, cli, hook):
    started = _start(repo, cli)
    out = cli("hold", "--session", "S1", "--reason", "太早了", expect=1)
    assert out["code"] == "HOLD_NOT_READY"


def test_b7_boundary_release_returns_to_real_next_action(repo, cli, hook):
    started = _start(repo, cli)
    _ready_to_finalize(started, cli, hook)
    _answer_askuserquestion(hook)
    (repo.root / "README.md").write_text("hi\n")
    repo.commit_all("docs(x): [cr_id_skip] Readme")
    cli("rebase", "--session", "S1")
    cli("hold", "--session", "S1", "--reason", "等集成窗口")

    out = cli("hold", "--session", "S1", "--release")
    assert out.get("ok", True) is not False
    st = cli("status", "--session", "S1")
    assert st["readiness"]["next_action"] == "machine"


def test_b8_answer_before_full_pass_and_before_rebase_still_counts(repo, cli, hook):
    """#299：回答发生在 gate 首次全过之后、rebase 之前，rebase 后重新全绿仍可 hold，不需要再问。"""
    started = _start(repo, cli)
    _ready_to_finalize(started, cli, hook)
    _answer_askuserquestion(hook)

    (repo.root / "README.md").write_text("hi\n")
    repo.commit_all("docs(x): [cr_id_skip] Readme")
    cli("rebase", "--session", "S1")
    # 重新验证到全绿（不需要新的用户回答）
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    assert cli("proof", "--session", "S1")["result"] == "PASS"
    reviewer_pass(hook, agent_id="R2")
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "finalize"

    out = cli("hold", "--session", "S1", "--reason", "等集成窗口")
    assert out.get("ok", True) is not False


def test_b8_boundary_release_then_hold_needs_new_answer(repo, cli, hook):
    started = _start(repo, cli)
    _ready_to_finalize(started, cli, hook)
    _answer_askuserquestion(hook)
    cli("hold", "--session", "S1", "--reason", "第一次")
    cli("hold", "--session", "S1", "--release")

    out = cli("hold", "--session", "S1", "--reason", "第二次", expect=3)
    assert out["code"] == "USER_DECISION_REQUIRED"


def test_b8_boundary_task_notification_is_not_an_answer(repo, cli, hook):
    started = _start(repo, cli)
    _ready_to_finalize(started, cli, hook)
    (repo.root / "README.md").write_text("hi\n")
    repo.commit_all("docs(x): [cr_id_skip] Readme")
    cli("rebase", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    assert cli("proof", "--session", "S1")["result"] == "PASS"
    reviewer_pass(hook, agent_id="R2")

    n_events_before = len(L.load(started["ledger"])["events"])
    hook("UserPromptSubmit", {"session_id": "S1", "prompt": "<task-notification>…</task-notification>"})
    # 走真实的写入端（hooks.py::_user_input）：UserPromptSubmit 这个 hook 事件本身不该落一条 user_input
    # 事件（只有 AskUserQuestion 的回答会），断言事件数没变、也没有任何 kind=user_input 的新事件，
    # 才是覆盖真正的 guard；手工往 ledger 里塞一条事件测不出这段 guard 是否还在。
    events_after = L.load(started["ledger"])["events"]
    assert len(events_after) == n_events_before
    assert not [e for e in events_after if e["kind"] == "user_input"]

    out = cli("hold", "--session", "S1", "--reason", "x", expect=3)
    assert out["code"] == "USER_DECISION_REQUIRED"


# ================================================================== B9


def test_b9_contract_revise_reads_loop_yml_from_candidate(repo, cli, hook):
    contract = contract_with(**{
        "authority.builder_write": ["src/**", ".claude/loop.yml"],
        "authority.protected_paths": [],
    })
    started = _start(repo, cli, contract=contract)
    wt = started["worktree"]
    loop_yml = wt / ".claude" / "loop.yml"
    original = loop_yml.read_text()
    assert "timeout: 60" in original
    loop_yml.write_text(original.replace("timeout: 60", "timeout: 90"))
    cli("checkpoint", "--session", "S1", "--role", "builder")

    plan = repo.root / "plan.md"
    out = cli("contract", "revise", "--session", "S1", "--plan", str(plan), "--authorize")
    assert out["applied"] is True

    lg = L.load(started["ledger"])
    stages = {s["stage"]: s for s in lg["contract"]["assurance"]["machine_commands"]}
    assert stages["test"]["timeout"] == 90

    # 主仓工作区的 loop.yml 没改：revise 不该读它
    assert "timeout: 60" in (repo.root / ".claude" / "loop.yml").read_text()


def test_b9_boundary_uncommitted_candidate_change_not_picked_up(repo, cli, hook):
    """三个候选值都要不同（60 主仓原值 / 75 候选已提交 / 90 候选未提交）才有鉴别力：
    只断言"停在 60"分不清"读候选 HEAD（正确，落在已提交的 75）"和"整体退回读主仓工作区"两种实现。"""
    contract = contract_with(**{
        "authority.builder_write": ["src/**", ".claude/loop.yml"],
        "authority.protected_paths": [],
    })
    started = _start(repo, cli, contract=contract)
    wt = started["worktree"]
    loop_yml = wt / ".claude" / "loop.yml"
    original = loop_yml.read_text()
    assert "timeout: 60" in original

    # 候选先提交一次真实改动（75），revise 应该以它为准
    loop_yml.write_text(original.replace("timeout: 60", "timeout: 75"))
    cli("checkpoint", "--session", "S1", "--role", "builder")

    # 再叠一层未提交的改动（90）：revise 不该读到这一层
    loop_yml.write_text(loop_yml.read_text().replace("timeout: 75", "timeout: 90"))

    plan = repo.root / "plan.md"
    cli("contract", "revise", "--session", "S1", "--plan", str(plan), "--authorize")
    lg = L.load(started["ledger"])
    stages = {s["stage"]: s for s in lg["contract"]["assurance"]["machine_commands"]}
    assert stages["test"]["timeout"] == 75

    # 未提交的改动仍原样留在候选 worktree 里（revise 没有动它，也没有把它提交掉）
    assert "timeout: 90" in loop_yml.read_text()


# ================================================================== B10


def test_b10_reviewer_brief_target_drift(repo, cli, hook):
    started = _start(repo, cli)
    _ready_to_finalize(started, cli, hook)

    (repo.root / "docs").mkdir(exist_ok=True)
    (repo.root / "docs" / "other.md").write_text("other\n")
    repo.commit_all("docs(x): [cr_id_skip] Other doc")
    t0 = started["target_start_head"]
    t1 = git(repo.root, "rev-parse", "main")
    assert t0 != t1

    cli("rebase", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    assert cli("proof", "--session", "S1")["result"] == "PASS"

    brief = cli("brief", "--session", "S1", "--role", "reviewer", "--json")
    drift = brief["target_drift"]
    assert drift["from"] == t0
    assert drift["to"] == t1
    assert "docs/other.md" in drift["paths"]
    assert drift["intersecting_paths"] == []
    assert drift["patch_unchanged"] is True

    text = cli("brief", "--session", "S1", "--role", "reviewer")
    assert "目标分支在你上次审查后前进了。patch 未变不构成沿用上次结论的依据" in text


def test_b10_boundary_no_prior_review_no_target_drift(repo, cli, hook):
    started = _start(repo, cli)
    drive_to_proof_pass(started, cli, hook)
    brief = cli("brief", "--session", "S1", "--role", "reviewer", "--json")
    assert "target_drift" not in brief
    text = cli("brief", "--session", "S1", "--role", "reviewer")
    assert "目标分支在你上次审查后前进了" not in text


# ================================================================== B11


def test_b11_reviewer_md_checklist_has_target_drift_guidance():
    path = Path(__file__).resolve().parents[1] / "agents" / "reviewer.md"
    text = path.read_text(encoding="utf-8")
    start = text.index("## 审查清单")
    rest = text[start + len("## 审查清单"):]
    end = rest.find("\n## ")
    section = rest if end == -1 else rest[:end]

    def normalize(s: str) -> str:
        s = s.replace("**", "").replace("`", "")
        return " ".join(s.split())

    normalized = normalize(section)
    assert "brief 若给出目标分支漂移，复审漂入的提交与候选的交互；patch 未变不能作为沿用上次结论的依据。" in normalized
    # 原有条目仍在
    assert "mutation patch 是否真的破坏了对应 behavior" in normalized
    assert "从未变红的 test_id" in normalized
