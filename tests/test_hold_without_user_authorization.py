"""hold-without-user-authorization run 的 B1-B6。

B1：`bl hold` 不再要求任何用户问答（gate 全过后直接暂缓合入）。
B2：授权锚点仍是「当前 mission revision 下 gate 首次全过」；新 revision 下重新全绿后不需要新回答。
B3：无用户回答的 hold 期间 Stop 放行 / finalize 报 HOLD_ACTIVE / checkpoint 回到 machine / release 恢复。
B4：SKILL.md、hooks.py、architecture.md、README.md、cli help 的契约文本。
B5：retro 新增 S-hold 信号（count / reasons）与未覆盖时的 RETRO_COVERAGE。
B6：版本号 8.2.2 与 CHANGELOG 的 hold/#368 新节。

断言文档文本前按 contract 的规则归一化：去掉 markdown 强调标记与反引号，连续空白归一为一个空格。
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from builder_loop import evidence, hooks, ledger as L
from builder_loop.cli import build_parser, dispatch
from builder_loop.errors import Problem
from conftest import ROOT, contract_with, drive_to_proof_pass, git, reviewer_pass, write_plan


# ---------------------------------------------------------------- helpers


def _call(repo, *args):
    """同 conftest 的 cli fixture，但把 (out, exit_code) 都还给调用方。"""
    ns = build_parser().parse_args(["--repo", str(repo.root), *args])
    try:
        out, code = dispatch(ns)
    except Problem as exc:
        out, code = exc.to_json(), exc.exit_code
    return out, code


def _normalize(text: str) -> str:
    text = re.sub(r"[*_`]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _ready_to_finalize(started, cli, hook) -> None:
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook)
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "finalize"


def _events(ledger_path: Path) -> list[dict]:
    return L.load(ledger_path)["events"]


def _user_inputs(ledger_path: Path) -> list[dict]:
    return [e for e in _events(ledger_path) if e["kind"] == "user_input"]


def _answer_askuserquestion(hook, session: str = "S1") -> dict:
    hook("PreToolUse", {"session_id": session, "tool_name": "AskUserQuestion", "tool_use_id": "q-hold"})
    return hook("PostToolUse", {"session_id": session, "tool_name": "AskUserQuestion"})


def _lite_ready_to_finalize(repo, cli, hook, plan_name: str = "lite-hold.md") -> dict:
    """不需要 tester 的轻量 contract（machine + reviewer），便于反复修订 revision。"""
    write_plan(repo.root, contract_with(**{"assurance.required": ["machine", "reviewer"]}), plan_name)
    out = cli("start", "--plan", str(repo.root / plan_name), "--session", "S1")
    wt = Path(out["worktree"])
    (wt / "src" / "extra.py").write_text("# v1\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    reviewer_pass(hook)
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "finalize"
    return {"worktree": wt, "ledger": Path(out["ledger"])}


def _skill_text() -> str:
    return (ROOT / "skills" / "builder" / "SKILL.md").read_text(encoding="utf-8")


def _skill_row(text: str, cmd: str) -> str:
    m = re.search(rf"(?m)^\| `{cmd}` \|.*$", text)
    assert m, f"SKILL.md 缺少 `{cmd}` 行"
    return m.group(0)


# ---------------------------------------------------------------- B1


def test_b1_hold_succeeds_without_any_user_input(started, cli, hook):
    _ready_to_finalize(started, cli, hook)
    assert _user_inputs(started["ledger"]) == []

    out = cli("hold", "--session", "S1", "--reason", "等集成放行")
    assert out.get("ok", True) is not False

    lg = L.load(started["ledger"])
    assert "hold" not in lg  # 不新增 ledger 顶层字段：从事件派生
    assert lg["events"][-1]["kind"] == "hold"
    assert lg["events"][-1]["reason"] == "等集成放行"

    st = cli("status", "--session", "S1")
    assert st["readiness"]["next_action"] == "held"
    assert st["hold"]["reason"] == "等集成放行"


def test_b1_boundary_only_task_notification_still_succeeds(started, cli, hook):
    _ready_to_finalize(started, cli, hook)
    hook("UserPromptSubmit", {"session_id": "S1", "prompt": "<task-notification>…</task-notification>"})
    assert _user_inputs(started["ledger"]) == []

    cli("hold", "--session", "S1", "--reason", "等集成放行")
    assert _events(started["ledger"])[-1]["kind"] == "hold"
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "held"


def test_b1_boundary_answer_predates_last_evidence_still_succeeds(started, cli, hook):
    # 回答发生在最后一项 evidence 之前：不再影响 hold 的成立
    drive_to_proof_pass(started, cli, hook)
    _answer_askuserquestion(hook)
    reviewer_pass(hook)
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "finalize"

    lg = L.load(started["ledger"])
    answer_at = max(e["at"] for e in _user_inputs(started["ledger"]))
    last_evidence_at = max(rec["at"] for rec in lg["evidence"].values())
    assert answer_at < last_evidence_at

    cli("hold", "--session", "S1", "--reason", "等集成放行")
    assert _events(started["ledger"])[-1]["kind"] == "hold"


def test_b1_boundary_already_held_rejected_and_events_unchanged(started, cli, hook):
    _ready_to_finalize(started, cli, hook)
    cli("hold", "--session", "S1", "--reason", "第一次")
    n_events = len(_events(started["ledger"]))

    out, code = _call(started["repo"], "hold", "--session", "S1", "--reason", "第二次")
    assert code == 1
    assert out["code"] == "ALREADY_HELD"
    assert len(_events(started["ledger"])) == n_events


def test_b1_invariant_blank_reason_rejected_and_events_unchanged(started, cli, hook):
    _ready_to_finalize(started, cli, hook)
    n_events = len(_events(started["ledger"]))

    out, code = _call(started["repo"], "hold", "--session", "S1", "--reason", "   ")
    assert code != 0
    assert out["code"] == "REASON_REQUIRED"
    assert len(_events(started["ledger"])) == n_events


# ---------------------------------------------------------------- B2


def test_b2_hold_not_ready_before_any_gate_pass(started, cli):
    n_events = len(_events(started["ledger"]))
    out = cli("hold", "--session", "S1", "--reason", "x", expect=1)
    assert out["code"] == "HOLD_NOT_READY"
    assert len(_events(started["ledger"])) == n_events


def test_b2_boundary_new_revision_needs_fresh_gate_pass_but_no_new_answer(repo, cli, hook):
    ctx = _lite_ready_to_finalize(repo, cli, hook)

    c2 = contract_with(**{"assurance.required": ["machine", "reviewer"], "mission.revision": 2})
    write_plan(repo.root, c2, "lite-rev2.md")
    revised = cli("contract", "revise", "--session", "S1", "--plan", str(repo.root / "lite-rev2.md"), "--authorize")
    assert "MISSION_REVISION" in revised["changes"]

    # 新 revision 下 gate 还没全过：HOLD_NOT_READY（事件数不变）
    n_events = len(_events(ctx["ledger"]))
    out = cli("hold", "--session", "S1", "--reason", "x", expect=1)
    assert out["code"] == "HOLD_NOT_READY"
    assert len(_events(ctx["ledger"])) == n_events

    # 新 revision 下重新全绿：不需要任何新回答即可 hold
    (ctx["worktree"] / "src" / "extra.py").write_text("# v2\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    reviewer_pass(hook, agent_id="R2")
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "finalize"
    assert _user_inputs(ctx["ledger"]) == []

    cli("hold", "--session", "S1", "--reason", "等第二批")
    assert _events(ctx["ledger"])[-1]["kind"] == "hold"
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "held"


def test_b2_invariant_legacy_gates_passed_event_treated_as_revision_1(repo, cli, hook):
    ctx = _lite_ready_to_finalize(repo, cli, hook)
    with L.mutate(ctx["ledger"]) as lg2:
        for e in lg2["events"]:
            if e["kind"] == "gates_passed":
                e.pop("revision", None)
    assert L.load(ctx["ledger"])["contract"]["mission"]["revision"] == 1

    cli("hold", "--session", "S1", "--reason", "等集成放行")
    assert _events(ctx["ledger"])[-1]["kind"] == "hold"
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "held"


# ---------------------------------------------------------------- B3


def test_b3_stop_permissive_and_finalize_rejected_after_hold_without_answer(started, cli, hook):
    repo = started["repo"]
    _ready_to_finalize(started, cli, hook)
    cli("hold", "--session", "S1", "--reason", "等集成放行")
    assert _user_inputs(started["ledger"]) == []

    lg_before = L.load(started["ledger"])
    stall_before = lg_before["counters"]["stall"]["count"]
    target_head_before = git(repo.root, "rev-parse", lg_before["repo"]["target_branch"])

    for active in (True, False):
        r = hook("Stop", {"session_id": "S1", "stop_hook_active": active})
        assert r["code"] == 0 and r["stderr"] == ""

    lg_after = L.load(started["ledger"])
    assert lg_after["counters"]["stall"]["count"] == stall_before
    assert [e for e in lg_after["events"] if e["kind"] == "stall_escape"] == []

    out = cli("finalize", "--session", "S1", "-m", "feat(x): [cr_id_skip] X", expect=1)
    assert out["code"] == "HOLD_ACTIVE"
    assert git(repo.root, "rev-parse", lg_after["repo"]["target_branch"]) == target_head_before
    assert not L.load(started["ledger"]).get("terminal")


def test_b3_checkpoint_after_hold_returns_to_machine_and_stop_blocks(started, cli, hook):
    _ready_to_finalize(started, cli, hook)
    cli("hold", "--session", "S1", "--reason", "等集成放行")

    wt = started["worktree"]
    (wt / "src" / "foo.py").write_text((wt / "src" / "foo.py").read_text() + "\n# touch after hold\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")

    st = cli("status", "--session", "S1")["readiness"]
    assert st["next_action"] == "machine"

    r = hook("Stop", {"session_id": "S1", "stop_hook_active": False})
    assert r["code"] == 2 and "next_action=machine" in r["stderr"]


def test_b3_release_needs_no_answer_and_returns_to_finalize(started, cli, hook):
    _ready_to_finalize(started, cli, hook)
    cli("hold", "--session", "S1", "--reason", "等集成放行")
    assert _user_inputs(started["ledger"]) == []

    out = cli("hold", "--session", "S1", "--release")
    assert out.get("ok", True) is not False
    assert _events(started["ledger"])[-1]["kind"] == "hold_release"

    st = cli("status", "--session", "S1")
    assert st["readiness"]["next_action"] == "finalize"
    assert st["hold"] is None


def test_b3_invariant_release_keeps_evidence_and_machine_iter(started, cli, hook):
    _ready_to_finalize(started, cli, hook)
    cli("hold", "--session", "S1", "--reason", "等集成放行")
    before = L.load(started["ledger"])

    cli("hold", "--session", "S1", "--release")

    after = L.load(started["ledger"])
    assert after["evidence"] == before["evidence"]
    assert after["counters"]["machine_iter"] == before["counters"]["machine_iter"]


def test_b3_invariant_finalize_after_release_still_requires_fresh_four(started, cli, hook):
    _ready_to_finalize(started, cli, hook)
    cli("hold", "--session", "S1", "--reason", "等集成放行")
    cli("hold", "--session", "S1", "--release")

    wt = started["worktree"]
    (wt / "src" / "foo.py").write_text((wt / "src" / "foo.py").read_text() + "\n# stale after release\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")

    out, code = _call(started["repo"], "finalize", "--session", "S1", "-m", "feat(x): [cr_id_skip] X")
    assert code != 0
    assert not L.load(started["ledger"]).get("terminal")


# ---------------------------------------------------------------- B4


def test_b4_skill_finalize_row_says_normal_wait_needs_no_question():
    row = _skill_row(_skill_text(), "finalize")
    assert _normalize("正常等待集成放行不必问用户") in _normalize(row)


def test_b4_skill_held_row_has_new_phrases():
    row = _skill_row(_skill_text(), "held")
    norm = _normalize(row)
    assert _normalize("已暂缓合入，Stop 放行，直接结束这一轮") in norm
    assert _normalize("结束前必须在给用户的回复里说明：已暂缓、在等什么") in norm


def test_b4_boundary_skill_drops_old_hold_phrases():
    text = _skill_text()
    assert "已按用户决定暂缓合入" not in _normalize(text)
    assert "release 不会作废用户已经给过的回答" not in _normalize(text)


def test_b4_hooks_finalize_hint_needs_no_user_question():
    hint = hooks.ACTION_HINTS[evidence.ACTION_FINALIZE]
    assert "不必问用户" in hint
    assert "用户决定等外部条件" not in hint


def test_b4_architecture_and_readme_say_hold_needs_no_answer():
    arch = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    assert _normalize("hold 不再要求用户回答") in _normalize(arch)

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert _normalize("不必先问用户") in _normalize(readme)


def test_b4_boundary_hold_help_drops_old_phrase():
    # add_parser(help=...) 的摘要只出现在顶层 format_help 里；子 parser 的 format_help 只有 usage / options，
    # 盯不住 cli 措辞（基线上一版就是在这个位置空转）。
    text = build_parser().format_help()
    assert "按用户决定暂缓" not in text


def test_b4_invariant_held_row_keeps_release_and_finalize_row_keeps_rebase():
    text = _skill_text()
    assert "bl hold --release" in _skill_row(text, "held")
    assert "bl rebase" in _skill_row(text, "finalize")


# ---------------------------------------------------------------- B5


def _finalize_with_two_holds(started, cli, hook) -> None:
    _ready_to_finalize(started, cli, hook)
    assert _user_inputs(started["ledger"]) == []
    cli("hold", "--session", "S1", "--reason", "等集成放行")
    cli("hold", "--session", "S1", "--release")
    cli("hold", "--session", "S1", "--reason", "等第二批")
    cli("hold", "--session", "S1", "--release")
    out = cli("finalize", "--session", "S1", "-m", "feat(x): [cr_id_skip] X")
    assert out.get("terminal") == "finalized", out


def test_b5_retro_signals_hold_count_and_reasons(started, cli, hook):
    _finalize_with_two_holds(started, cli, hook)

    sig = cli("retro", "signals", "--session", "S1")
    hold = next((s for s in sig["signals"] if s["id"] == "S-hold"), None)
    assert hold is not None, sig["signals"]
    assert set(hold.keys()) == {"id", "summary", "facts"}
    assert hold["facts"].get("count") == 2
    assert list(hold["facts"].get("reasons") or []) == ["等集成放行", "等第二批"]


def test_b5_boundary_never_held_has_no_signal(started, cli, hook):
    _ready_to_finalize(started, cli, hook)
    out = cli("finalize", "--session", "S1", "-m", "feat(x): [cr_id_skip] X")
    assert out.get("terminal") == "finalized", out

    sig = cli("retro", "signals", "--session", "S1")
    assert "S-hold" not in [s["id"] for s in sig["signals"]]


def test_b5_boundary_uncovered_hold_signal_blocks_retro(started, cli, hook):
    _finalize_with_two_holds(started, cli, hook)

    f = Path(tempfile.mkdtemp(prefix="bl-retro-")) / "r.json"
    f.write_text(json.dumps({"dispositions": [], "observations": []}), encoding="utf-8")

    out, code = _call(started["repo"], "retro", "record", "--session", "S1", "--file", str(f))
    assert code != 0
    assert out["code"] == "RETRO_COVERAGE"
    # negative() 把 missing / unknown 放进 details（Problem.to_json 的形状）
    assert "S-hold" in out["details"]["missing"]


# ---------------------------------------------------------------- B6


def test_b6_version_is_8_2_2():
    import builder_loop

    assert builder_loop.__version__ == "8.2.2"


def test_b6_changelog_top_section_covers_hold_and_368():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    headings = re.findall(r"(?m)^## (.+)$", text)
    assert headings
    # 本次交付新增了 hold/#368 一节；之后的交付会继续在顶部加节，所以只要求这一节
    # 存在且含 #368，不把「它永远是第一条」钉死（否则每次新版本都会假红）。
    section = next((h for h in headings if "hold" in h.lower()), None)
    assert section is not None, headings

    idx = headings.index(section)
    start = text.index("## " + section)
    end = text.index("## " + headings[idx + 1]) if idx + 1 < len(headings) else len(text)
    assert "#368" in text[start:end]


def test_b6_boundary_new_section_first_and_history_product_line_intact():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    first_section = text.index("## ")
    assert _normalize("本文件记 CC 版产品线（分支 cc/main）。Codex 版见 codex/main 分支。") in _normalize(text[:first_section])

    old = "## 候选完整性、角色事实补全与 hold 授权锚点（#312 #290 #252 #313 #299 #308 #288 #296，2026-09-27）"
    assert old in text
    headings = re.findall(r"(?m)^## (.+)$", text)
    assert text.index("## " + headings[0]) < text.index(old)