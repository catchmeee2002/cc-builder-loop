"""bl machine 执行期间 facets（contract 投影）变化也要作废观察，不只是 candidate.head 前进。"""

from __future__ import annotations

import copy
from pathlib import Path

from builder_loop import evidence
from builder_loop import ledger as L
from conftest import ROOT, contract_with, git, implement_mul, write_plan

BL = f"bash {ROOT / 'bin' / 'bl'}"
PASS_STAGE = 'python3 -c "import sys; sys.exit(0)"'


def _start_lite(repo, cli, revising_stage: bool = False):
    if revising_stage:
        # revise 会按 loop.yml 重新冻结 machine_commands：要让它只差 review_focus（neutral），
        # 执行 revise 的那条命令本身就得是 loop.yml 里 pass_cmd 的 stage
        cmd = f"{BL} --repo {repo.root} contract revise --session S1 --plan {repo.root / 'lite2.md'}"
        (repo.root / ".claude" / "loop.yml").write_text(
            f'pass_cmd:\n  - stage: test\n    cmd: "{cmd}"\n    timeout: 60\nmax_iterations: 3\n'
            'proof_runner:\n  framework: pytest\n  cmd: "python3 -m pytest -p no:html"\n')
    write_plan(repo.root, contract_with(**{"assurance.required": ["machine", "reviewer"]}), "lite.md")
    write_plan(repo.root, contract_with(**{"assurance.required": ["machine", "reviewer"], "assurance.review_focus": ["look here"]}), "lite2.md")
    out = cli("start", "--plan", str(repo.root / "lite.md"), "--session", "S1")
    wt = Path(out["worktree"])
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    return wt, Path(out["ledger"])


def _set_stages(lp: Path, *cmds: str) -> None:
    with L.mutate(lp) as x:
        x["contract"]["assurance"]["machine_commands"] = [{"stage": f"s{i}", "cmd": c, "timeout": 60} for i, c in enumerate(cmds)]


def _revise_cmd(repo) -> str:
    return f"{BL} --repo {repo.root} contract revise --session S1 --plan {repo.root / 'lite2.md'}"


def test_b2_neutral_contract_revise_during_machine_invalidates(repo, cli):
    wt, lp = _start_lite(repo, cli, revising_stage=True)
    before = L.load(lp)
    head0 = before["candidate"]["head"]
    res = cli("machine", "--session", "S1", expect=1)
    assert res.get("code") == "MACHINE_INPUT_CHANGED", res
    d = res["details"]
    assert "facets" in d["changed_inputs"] and "candidate_head" not in d["changed_inputs"]
    assert d["changed_inputs"] == sorted(d["changed_inputs"])
    assert d["head_at_start"] == d["head_now"] == head0
    after = L.load(lp)
    assert after["contract"]["assurance"]["review_focus"] == ["look here"]  # revise 确实生效
    assert after["counters"]["machine_iter"] == before["counters"]["machine_iter"]
    assert after["failures"]["machine"] == before["failures"]["machine"]
    assert after["evidence"].get("machine") == before["evidence"].get("machine")
    ev = after["events"][-1]
    assert ev["kind"] == "machine_input_changed"
    assert ev["changed_inputs"] == d["changed_inputs"]


def test_b2_checkpoint_reports_candidate_head_in_changed_inputs(repo, cli):
    wt, lp = _start_lite(repo, cli)
    _set_stages(lp, f"echo '# new' > src/extra.py && {BL} --repo {repo.root} checkpoint --session S1 --role builder")
    head0 = L.load(lp)["candidate"]["head"]
    res = cli("machine", "--session", "S1", expect=1)
    assert res.get("code") == "MACHINE_INPUT_CHANGED", res
    d = res["details"]
    assert "candidate_head" in d["changed_inputs"] and d["changed_inputs"] == sorted(d["changed_inputs"])
    assert d["head_at_start"] == head0 and d["head_now"] != head0
    ev = L.load(lp)["events"][-1]
    assert ev["kind"] == "machine_input_changed"
    assert ev["head_at_start"] == head0 and ev["head_now"] == d["head_now"] and ev["changed_inputs"] == d["changed_inputs"]


def test_b2_rerun_after_facets_invalidation_passes(repo, cli):
    wt, lp = _start_lite(repo, cli, revising_stage=True)
    assert cli("machine", "--session", "S1", expect=1).get("code") == "MACHINE_INPUT_CHANGED"
    _set_stages(lp, PASS_STAGE)
    assert cli("machine", "--session", "S1")["result"] == "PASS"


def test_b2_stage_git_commit_still_fail_not_invalidated(repo, cli):
    wt, lp = _start_lite(repo, cli)
    cmd = ("echo '# c' > src/direct.py && git add -A && "
           "git -c core.hooksPath=/dev/null -c user.email=t@t -c user.name=t commit -q -m 'chore(x): [cr_id_skip] Direct'")
    _set_stages(lp, cmd)
    res = cli("machine", "--session", "S1", expect=1)
    assert res.get("code") != "MACHINE_INPUT_CHANGED"
    assert res["result"] == "FAIL"
    assert res["failure"]["worktree_mutated"]["head_after"] == git(wt, "rev-parse", "HEAD")


def test_b2_input_changes_machine_kind(repo, cli):
    wt, lp = _start_lite(repo, cli)
    a = L.load(lp)
    assert evidence.input_changes(a, copy.deepcopy(a), "machine", repo.root) == []
    b = copy.deepcopy(a)
    b["contract"]["digests"]["assurance"] = "changed"
    ch = evidence.input_changes(a, b, "machine", repo.root)
    assert ch == ["facets"] or ("facets" in ch and ch == sorted(ch))
    assert "candidate_head" not in ch
