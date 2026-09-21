"""B1: MAX_ITERATIONS 只计失败的 machine 运行（窗口内的 PASS 不消耗预算）。
B2: machine 的基线提示读收尾时最新的 preflight event（执行期间被追加的更新 event 生效）。
B3: 超时的 preflight stage 不算基线红（baseline_timed_out 与 baseline_red 分离，全超时 = INCONCLUSIVE）。
B4: skills/builder/SKILL.md 第 1 节第 4 条提醒门禁期间不要另起全量测试 / 长时间占用 CPU 的后台任务。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from builder_loop import ledger as L, machine as M

from conftest import ROOT, RUNTIME, contract_with, git, write_plan

SKILL = ROOT / "skills" / "builder" / "SKILL.md"


def _start_lite(repo, cli, session="S1"):
    write_plan(repo.root, contract_with(**{"assurance.required": ["machine", "reviewer"]}), "lite.md")
    out = cli("start", "--plan", str(repo.root / "lite.md"), "--session", session)
    return Path(out["worktree"]), Path(out["ledger"])


# ---------------------------------------------------------------- B1


def test_b1_max_iterations_counts_only_failures_not_interspersed_passes(repo, cli):
    """3 次 PASS 穿插在窗口里不该提前把预算耗光；只有连续失败才该计入 used_in_window。"""
    wt, lp = _start_lite(repo, cli)
    foo = wt / "src" / "foo.py"

    # run1/2/3：内容仍正确（只加注释），每次都 checkpoint 后 PASS
    for i in range(1, 4):
        foo.write_text(foo.read_text().rstrip("\n") + f"\n# v{i}\n")
        cli("checkpoint", "--session", "S1", "--role", "builder")
        out = cli("machine", "--session", "S1")
        assert out["result"] == "PASS", out

    # run4：错误实现 → 第 1 次失败。窗口内只有 1 次失败：不该触发 MAX_ITERATIONS，
    # remaining_iterations 应该是 max_iterations - 1（与之前 3 次 PASS 无关）
    foo.write_text("def add(a, b):\n    return a - b\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    r4 = cli("machine", "--session", "S1", expect=1)
    assert r4["remaining_iterations"] == 2, r4
    assert not any(b["code"] == "MAX_ITERATIONS" for b in r4["readiness"]["blockers"]), r4["readiness"]["blockers"]

    # run5：同一处错误再失败一次（第 2 次失败）。仍然不该触发 MAX_ITERATIONS
    r5 = cli("machine", "--session", "S1", expect=1)
    assert r5["failure"]["signature"] == r4["failure"]["signature"]
    assert not any(b["code"] == "MAX_ITERATIONS" for b in r5["readiness"]["blockers"]), r5["readiness"]["blockers"]

    # 到此为止共 5 次运行、2 次失败
    assert L.load(lp)["counters"]["machine_iter"] == 5

    # run6：第 6 次仍然执行（不是 exit 3 / MACHINE_BLOCKED）
    r6 = cli("machine", "--session", "S1", expect=None)
    assert r6.get("code") != "MACHINE_BLOCKED", r6
    # 第 6 次又失败 = 窗口内第 3 次失败 → 这次调用返回 exit 3
    assert r6["result"] == "FAIL"
    blockers = r6["readiness"]["blockers"]
    hit = [b for b in blockers if b["code"] == "MAX_ITERATIONS"]
    assert hit and hit[0]["used_in_window"] == 3 and hit[0]["max_iterations"] == 3, blockers


# ---------------------------------------------------------------- B2


_INJECT_SCRIPT = """
import os, sys
sys.path.insert(0, os.environ["BL_TEST_RUNTIME"])
from pathlib import Path
from builder_loop import ledger as L, machine as M

repo = Path(os.environ["BUILDER_LOOP_MAIN_REPO"])
run_id = os.environ["BUILDER_LOOP_RUN_ID"]
lp = L.run_dir(repo, run_id) / "ledger.json"
lg = L.load(lp)
stages = lg["contract"]["assurance"].get("machine_commands") or []
want = M.commands_digest(stages)
rc = int(os.environ.get("BL_INJECT_RC", "0"))
with L.mutate(lp) as lg2:
    L.log_event(
        lg2, "preflight",
        stages=[{"stage": "test", "returncode": rc, "timed_out": False, "log": "/dev/null"}],
        commands_digest=want,
        baseline_red=([] if rc == 0 else ["test"]),
    )
"""


def _b2_setup(repo, cli, monkeypatch):
    monkeypatch.setenv("BL_TEST_RUNTIME", str(RUNTIME))
    script = repo.root / "bl_test_inject.py"
    script.write_text(_INJECT_SCRIPT, encoding="utf-8")
    (repo.root / ".claude" / "loop.yml").write_text(
        "pass_cmd:\n"
        "  - stage: inject\n    cmd: \"python3 bl_test_inject.py\"\n    timeout: 10\n"
        "  - stage: test\n    cmd: \"python3 -c \\\"import sys; sys.exit(1)\\\"\"\n    timeout: 10\n",
        encoding="utf-8",
    )
    repo.commit_all()
    wt, lp = _start_lite(repo, cli)
    # given：已有一条与当前 machine_commands 匹配的 preflight event，stage `test` 是基线红
    lg = L.load(lp)
    stages = lg["contract"]["assurance"].get("machine_commands") or []
    want = M.commands_digest(stages)
    with L.mutate(lp) as lg2:
        L.log_event(
            lg2, "preflight",
            stages=[{"stage": "inject", "returncode": 0, "timed_out": False, "log": "/dev/null"},
                    {"stage": "test", "returncode": 1, "timed_out": False, "log": "/dev/null"}],
            commands_digest=want,
            baseline_red=["test"],
        )
    (wt / "src" / "foo.py").write_text((wt / "src" / "foo.py").read_text() + "\n# candidate\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    return wt, lp


def test_b2_machine_uses_freshest_preflight_seen_at_finish(repo, cli, monkeypatch):
    """执行期间（inject stage）追加的更新 preflight event（test → GREEN）让收尾的基线提示不再标 baseline_red。"""
    _wt, lp = _b2_setup(repo, cli, monkeypatch)
    monkeypatch.setenv("BL_INJECT_RC", "0")
    out = cli("machine", "--session", "S1", expect=1)
    failure = out["failure"]
    assert failure.get("baseline_red") is not True, failure
    assert "与候选无关" not in (failure.get("baseline") or ""), failure
    ev_failure = L.load(lp)["evidence"]["machine"]["details"]["failure"]
    assert ev_failure.get("baseline_red") is not True


def test_b2_reverse_new_event_marks_truly_red(repo, cli, monkeypatch):
    """反向：执行期间追加的新 event 把 test 标为真正失败 → baseline_red True 且提示含「与候选无关」。"""
    _b2_setup(repo, cli, monkeypatch)
    monkeypatch.setenv("BL_INJECT_RC", "1")
    out = cli("machine", "--session", "S1", expect=1)
    failure = out["failure"]
    assert failure.get("baseline_red") is True, failure
    assert "与候选无关" in (failure.get("baseline") or ""), failure


def test_b2_no_new_event_keeps_existing_match(repo, cli):
    """执行期间没有新的 preflight event → 行为与现在相同，取已有最新的匹配 event。"""
    (repo.root / ".claude" / "loop.yml").write_text(
        "pass_cmd:\n  - stage: test\n    cmd: \"python3 -c \\\"import sys; sys.exit(1)\\\"\"\n    timeout: 10\n",
        encoding="utf-8",
    )
    repo.commit_all()
    wt, lp = _start_lite(repo, cli)
    lg = L.load(lp)
    stages = lg["contract"]["assurance"].get("machine_commands") or []
    want = M.commands_digest(stages)
    with L.mutate(lp) as lg2:
        L.log_event(
            lg2, "preflight",
            stages=[{"stage": "test", "returncode": 1, "timed_out": False, "log": "/dev/null"}],
            commands_digest=want, baseline_red=["test"],
        )
    (wt / "src" / "foo.py").write_text((wt / "src" / "foo.py").read_text() + "\n# c\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    out = cli("machine", "--session", "S1", expect=1)
    assert out["failure"].get("baseline_red") is True
    assert "与候选无关" in (out["failure"].get("baseline") or "")


def test_b2_commands_digest_mismatch_still_none(repo, cli):
    """不变量：machine_commands 与 preflight event 的 commands_digest 不匹配 → baseline_red 仍是 None。"""
    wt, lp = _start_lite(repo, cli)
    with L.mutate(lp) as lg2:
        L.log_event(lg2, "preflight", stages=[{"stage": "unit", "returncode": 1, "timed_out": False, "log": "/x"}],
                     commands_digest="deadbeef00000000", baseline_red=["unit"])
    assert M.baseline_red(L.load(lp)) is None


# ---------------------------------------------------------------- B3


def _slow_loop_yml(fast_cmd: str = "python3 -c \\\"import sys; sys.exit(0)\\\"") -> str:
    return (
        "pass_cmd:\n"
        f"  - stage: fast\n    cmd: \"{fast_cmd}\"\n    timeout: 5\n"
        "  - stage: slow\n    cmd: \"python3 -c \\\"import time; time.sleep(30)\\\"\"\n    timeout: 2\n"
    )


def test_b3_preflight_reports_timeout_separately_from_red(repo, cli):
    (repo.root / ".claude" / "loop.yml").write_text(_slow_loop_yml(), encoding="utf-8")
    repo.commit_all()
    wt, lp = _start_lite(repo, cli)

    out = cli("preflight", "--session", "S1")
    assert out["result"] == "INCONCLUSIVE", out
    assert out["baseline_red"] == [], out
    assert out["baseline_timed_out"] == ["slow"], out

    st = cli("status", "--session", "S1")["preflight"]
    assert st["baseline_timed_out"] == ["slow"] and st["baseline_red"] == []

    (wt / "src" / "foo.py").write_text((wt / "src" / "foo.py").read_text() + "\n# c\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    failure = cli("machine", "--session", "S1", expect=1)["failure"]
    assert failure["stage"] == "slow"
    assert failure.get("baseline_red") is not True, failure
    assert "超时" in (failure.get("baseline") or ""), failure
    assert "与候选无关" not in (failure.get("baseline") or ""), failure


def test_b3_preflight_mixed_red_and_timeout(repo, cli):
    (repo.root / ".claude" / "loop.yml").write_text(
        _slow_loop_yml(fast_cmd="python3 -c \\\"import sys; sys.exit(1)\\\""), encoding="utf-8")
    repo.commit_all()
    _start_lite(repo, cli)
    out = cli("preflight", "--session", "S1")
    assert out["result"] == "RED", out
    assert out["baseline_red"] == ["fast"], out
    assert out["baseline_timed_out"] == ["slow"], out


def test_b3_preflight_all_green(repo, cli):
    (repo.root / ".claude" / "loop.yml").write_text(
        "pass_cmd:\n  - stage: unit\n    cmd: python3 -c \"import sys; sys.exit(0)\"\n    timeout: 5\n",
        encoding="utf-8")
    repo.commit_all()
    _start_lite(repo, cli)
    out = cli("preflight", "--session", "S1")
    assert out["result"] == "GREEN" and out["baseline_red"] == [] and out["baseline_timed_out"] == []


def test_b3_old_format_event_derives_from_stages_not_field(repo, cli):
    """旧格式 preflight event（stages[] 里 slow 是 timed_out，且顶层 baseline_red 字段写着 ["slow"]）。
    baseline_red(ledger) 不该含 slow；baseline_timed_out(ledger) 该含 slow（以 stages 为准）。"""
    wt, lp = _start_lite(repo, cli)
    lg = L.load(lp)
    stages = lg["contract"]["assurance"].get("machine_commands") or []
    want = M.commands_digest(stages)
    with L.mutate(lp) as lg2:
        L.log_event(
            lg2, "preflight",
            stages=[{"stage": "slow", "returncode": 124, "timed_out": True, "log": "/x"}],
            commands_digest=want,
            baseline_red=["slow"],  # 旧字段：错误地把超时也算进了 red
        )
    lg = L.load(lp)
    assert "slow" not in (M.baseline_red(lg) or [])
    assert "slow" in (M.baseline_timed_out(lg) or [])


def test_b3_preflight_never_writes_evidence_or_counters(repo, cli):
    (repo.root / ".claude" / "loop.yml").write_text(_slow_loop_yml(), encoding="utf-8")
    repo.commit_all()
    wt, lp = _start_lite(repo, cli)
    out = cli("preflight", "--session", "S1")
    assert out  # exit 0 已由 cli fixture 的默认 expect=0 校验
    lg = L.load(lp)
    assert not any(lg["evidence"].values())
    assert lg["counters"]["machine_iter"] == 0


def test_b3_real_failure_still_marks_baseline_red(repo, cli):
    """不变量：真正失败（非 0、未超时）的基线 stage 仍产生 failure.baseline_red == True。"""
    (repo.root / ".claude" / "loop.yml").write_text(
        "pass_cmd:\n  - stage: unit\n    cmd: python3 -c \"import sys; sys.exit(0)\"\n    timeout: 5\n"
        "  - stage: legacy\n    cmd: python3 -c \"import missing_module\"\n    timeout: 5\n",
        encoding="utf-8")
    repo.commit_all()
    wt, lp = _start_lite(repo, cli)
    out = cli("preflight", "--session", "S1")
    assert out["result"] == "RED" and out["baseline_red"] == ["legacy"]
    (wt / "src" / "foo.py").write_text((wt / "src" / "foo.py").read_text() + "\n# c\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    failure = cli("machine", "--session", "S1", expect=1)["failure"]
    assert failure["stage"] == "legacy" and failure["baseline_red"] is True and "与候选无关" in failure["baseline"]


# ---------------------------------------------------------------- B4


def _skill_section_1_item(n: int) -> str:
    text = SKILL.read_text(encoding="utf-8")
    m = re.search(r"(?m)^## 1\. .*?\n(.*?)(?=\n## 2\.)", text, flags=re.S)
    assert m, "找不到 SKILL.md 的 ## 1. 启动 一节"
    body = m.group(1)
    steps = re.findall(r"(?ms)^\d+\..*?(?=^\d+\.|\Z)", body)
    assert len(steps) >= n, steps
    return steps[n - 1]


def test_b4_step4_warns_against_heavy_background_jobs_during_gates():
    item4 = _skill_section_1_item(4)
    assert item4.lstrip().startswith("4. **顺手后台跑一次基线预检**"), item4
    assert ("preflight、machine、proof 运行期间，不要在本机另起全量测试或长时间占用 CPU / IO 的后台任务："
            "它们会把门禁挤成假超时。") in item4, item4
    # 不出现在文件的其他位置
    text = SKILL.read_text(encoding="utf-8")
    assert text.count("它们会把门禁挤成假超时。") == 1


def test_b4_step4_still_guides_background_preflight_call():
    item4 = _skill_section_1_item(4)
    assert "bl preflight --session ${CLAUDE_SESSION_ID}" in item4
    assert "Bash `run_in_background`" in item4
