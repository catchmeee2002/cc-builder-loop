"""bl machine 观察期间候选被 checkpoint：作废本次观察（MACHINE_INPUT_CHANGED），不记成失败。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from builder_loop import ledger as L
from conftest import ROOT, contract_with, git, implement_mul, write_plan

BL = f"bash {ROOT / 'bin' / 'bl'}"
PASS_STAGE = 'python3 -c "import sys; sys.exit(0)"'


def _start_lite(repo, cli):
    write_plan(repo.root, contract_with(**{"assurance.required": ["machine", "reviewer"]}), "lite.md")
    out = cli("start", "--plan", str(repo.root / "lite.md"), "--session", "S1")
    wt = Path(out["worktree"])
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    return wt, Path(out["ledger"])


def _set_stages(lp: Path, *cmds: str) -> None:
    with L.mutate(lp) as x:
        x["contract"]["assurance"]["machine_commands"] = [{"stage": f"s{i}", "cmd": c, "timeout": 60} for i, c in enumerate(cmds)]


def _checkpointing_cmd(repo) -> str:
    return f"echo '# new' > src/extra.py && {BL} --repo {repo.root} checkpoint --session S1 --role builder"


def test_b2_checkpoint_during_machine_invalidates(repo, cli):
    wt, lp = _start_lite(repo, cli)
    _set_stages(lp, PASS_STAGE, _checkpointing_cmd(repo), PASS_STAGE)
    before = L.load(lp)
    head0 = before["candidate"]["head"]
    res = cli("machine", "--session", "S1", expect=1)
    assert res.get("code") == "MACHINE_INPUT_CHANGED", res
    d = res["details"]
    assert d["head_at_start"] == head0
    after = L.load(lp)
    assert d["head_now"] == after["candidate"]["head"] and d["head_now"] != d["head_at_start"]
    assert after["counters"]["machine_iter"] == before["counters"]["machine_iter"]
    assert after["failures"]["machine"] == before["failures"]["machine"]
    ev = after["events"][-1]
    assert ev["kind"] == "machine_input_changed"
    assert ev["head_at_start"] == head0 and ev["head_now"] == after["candidate"]["head"]
    assert after["evidence"].get("machine") == before["evidence"].get("machine")


def test_b2_rerun_passes_signals_clean_next_action_machine(repo, cli):
    wt, lp = _start_lite(repo, cli)
    _set_stages(lp, _checkpointing_cmd(repo))
    before_iter = L.load(lp)["counters"]["machine_iter"]
    assert cli("machine", "--session", "S1", expect=1).get("code") == "MACHINE_INPUT_CHANGED"
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "machine"
    from builder_loop import retro
    assert "S-machine-failures" not in [s["id"] for s in retro.derive_signals(L.load(lp))]
    _set_stages(lp, PASS_STAGE)
    out = cli("machine", "--session", "S1")
    assert out["result"] == "PASS" and out["iter"] == before_iter + 1


def test_b2_real_failure_still_fail(repo, cli):
    wt, lp = _start_lite(repo, cli)
    _set_stages(lp, 'python3 -c "import sys; sys.exit(3)"')
    before = L.load(lp)
    res = cli("machine", "--session", "S1", expect=1)
    assert res.get("result") == "FAIL" and res.get("code") != "MACHINE_INPUT_CHANGED"
    after = L.load(lp)
    assert len(after["failures"]["machine"]) == len(before["failures"]["machine"]) + 1
    assert after["counters"]["machine_iter"] == before["counters"]["machine_iter"] + 1


def test_b3_direct_git_commit_is_fail_not_invalidated(repo, cli):
    wt, lp = _start_lite(repo, cli)
    cmd = ("echo '# c' > src/direct.py && git add -A && "
           "git -c core.hooksPath=/dev/null -c user.email=t@t -c user.name=t commit -q -m 'chore(x): [cr_id_skip] Direct'")
    _set_stages(lp, cmd)
    before = L.load(lp)
    res = cli("machine", "--session", "S1", expect=1)
    assert res.get("code") != "MACHINE_INPUT_CHANGED"
    assert res["result"] == "FAIL"
    assert res["failure"]["worktree_mutated"]["head_after"] == git(wt, "rev-parse", "HEAD")
    assert L.load(lp)["candidate"]["head"] == before["candidate"]["head"]
    assert len(L.load(lp)["failures"]["machine"]) == len(before["failures"]["machine"]) + 1


def test_b3_tracked_file_edit_is_fail(repo, cli):
    wt, lp = _start_lite(repo, cli)
    _set_stages(lp, "echo x >> src/foo.py")
    res = cli("machine", "--session", "S1", expect=1)
    assert res.get("code") != "MACHINE_INPUT_CHANGED"
    assert res["result"] == "FAIL" and "src/foo.py" in res["failure"]["worktree_mutated"]["paths"]
