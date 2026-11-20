"""B4：`bl machine --retry --reason <非空原因>` 在当前输入有 fresh FAIL 且无 blocker 时，
执行前追加 machine_retry event（原因、上一失败签名、candidate_head、machine dependency_digest），
随后照常运行；retro 派生 S-machine-retry。

起点 CLI 还没有 --retry / --reason（argparse 直接 exit 2），所以正向断言在 call 阶段失败
（baseline-red）。非法路径（无 fresh FAIL / 空原因 / 只有 --reason / 有 blocker）在起点上也表现为
拒绝，是守卫，不列入 proof_spec。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from builder_loop import evidence
from builder_loop import ledger as L
from builder_loop import retro

from conftest import PROOF_RUNNER_CMD, ROOT, contract_with, implement_mul, write_plan

BL = str(ROOT / "bin" / "bl")
STAGE_CMD = "echo 'boom'; echo 'see tests/test_foo.py'; exit 1"
REASON = "同一失败再次确认：候选没变，重跑以排除资源争抢"


def _start(repo, cli, *, max_iterations: int = 5):
    (repo.root / ".claude" / "loop.yml").write_text(
        f'pass_cmd:\n  - stage: unit\n    cmd: "{STAGE_CMD}"\n    timeout: 30\n'
        f"max_iterations: {max_iterations}\n"
        f'proof_runner:\n  framework: pytest\n  cmd: "{PROOF_RUNNER_CMD}"\n',
        encoding="utf-8",
    )
    repo.commit_all("chore(fixture): [cr_id_skip] Failing machine stage")
    write_plan(repo.root, contract_with(**{"assurance.required": ["machine", "reviewer"]}), "lite.md")
    out = cli("start", "--plan", str(repo.root / "lite.md"), "--session", "S1")
    return Path(out["worktree"]), Path(out["ledger"])


def _bl(repo, *args: str) -> tuple[int, dict | None]:
    """子进程跑 CLI：argparse 的用法错误只体现为退出码，不会打断测试。"""
    proc = subprocess.run(["bash", BL, "--repo", str(repo.root), *args],
                          cwd=str(repo.root), capture_output=True, text=True, timeout=300)
    payload = None
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    if lines:
        try:
            payload = json.loads(lines[-1])
        except json.JSONDecodeError:
            payload = None
    return proc.returncode, payload


def _values(obj):
    """递归收集 JSON 值，用于「某个事实出现在 event / signal 里」的断言（不冻结键名）。"""
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _values(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _values(v)
    else:
        yield obj


def _contains(obj, needle) -> bool:
    return any(v == needle for v in _values(obj))


def _retry_events(lg):
    return [e for e in L.events_of(lg, "machine_retry")]


def _fresh_fail(repo, cli, *, max_iterations: int = 5):
    wt, lp = _start(repo, cli, max_iterations=max_iterations)
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    res = cli("machine", "--session", "S1", expect=1)
    assert res["result"] == "FAIL" and res["failure"]["tester_files_mentioned"] == ["tests/test_foo.py"]
    lg = L.load(lp)
    assert evidence.state(lg, "machine", repo.root) == "fail"
    return wt, lp, lg


def test_b4_retry_records_event_and_reruns_on_fresh_fail(repo, cli):
    _wt, lp, before = _fresh_fail(repo, cli)
    prev_sig = before["failures"]["machine"][-1]["signature"]
    prev_head = before["candidate"]["head"]
    prev_digest = evidence.dependency_digest(before, "machine", repo.root)
    prev_failures = list(before["failures"]["machine"])

    rc, out = _bl(repo, "machine", "--session", "S1", "--retry", "--reason", REASON)
    assert out is not None and out.get("result") == "FAIL", (rc, out)  # stage 仍然红：正常运行了一遍
    assert rc == 1, (rc, out)

    after = L.load(lp)
    events = _retry_events(after)
    assert len(events) == 1, events
    ev = events[0]
    assert _contains(ev, REASON), ev
    assert _contains(ev, prev_sig), ev
    assert _contains(ev, prev_head), ev
    assert _contains(ev, prev_digest), ev
    # 执行前追加：event 早于这一次运行写下的 evidence
    assert ev["at"] <= after["evidence"]["machine"]["at"], (ev, after["evidence"]["machine"]["at"])
    # 不重置计数、不改旧 evidence：旧失败仍在，machine_iter 加一
    assert after["counters"]["machine_iter"] == before["counters"]["machine_iter"] + 1
    assert after["failures"]["machine"][: len(prev_failures)] == prev_failures
    assert len(after["failures"]["machine"]) == len(prev_failures) + 1
    assert after["candidate"]["head"] == prev_head


def test_b4_retro_signal_reports_retry_count_and_reason(repo, cli):
    _wt, lp, _before = _fresh_fail(repo, cli)
    rc, _out = _bl(repo, "machine", "--session", "S1", "--retry", "--reason", REASON)
    assert rc == 1, rc

    lg = L.load(lp)
    sigs = retro.derive_signals(lg)
    sig = next((s for s in sigs if s.get("id") == "S-machine-retry"), None)
    assert sig is not None, sigs
    assert REASON in json.dumps(sig, ensure_ascii=False), sig
    ints = [v for v in _values(sig) if isinstance(v, int) and not isinstance(v, bool)]
    assert 1 in ints or "1" in str(sig.get("summary", "")), sig


# ---------------------------------------------------------------- 守卫：非法路径（不列入 proof_spec）


def test_guard_retry_without_fresh_fail_is_rejected_and_not_executed(repo, cli):
    wt, lp = _start(repo, cli)
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    before = L.load(lp)
    rc, _out = _bl(repo, "machine", "--session", "S1", "--retry", "--reason", REASON)
    assert rc != 0, rc
    after = L.load(lp)
    assert _retry_events(after) == []
    assert after["counters"]["machine_iter"] == before["counters"]["machine_iter"]
    assert after["evidence"].get("machine") == before["evidence"].get("machine")


def test_guard_retry_with_empty_reason_is_rejected(repo, cli):
    _wt, lp, before = _fresh_fail(repo, cli)
    rc, _out = _bl(repo, "machine", "--session", "S1", "--retry", "--reason", "   ")
    assert rc != 0, rc
    after = L.load(lp)
    assert _retry_events(after) == []
    assert after["counters"]["machine_iter"] == before["counters"]["machine_iter"]
    assert after["evidence"].get("machine") == before["evidence"].get("machine")


def test_guard_reason_without_retry_is_rejected(repo, cli):
    _wt, lp, before = _fresh_fail(repo, cli)
    rc, _out = _bl(repo, "machine", "--session", "S1", "--reason", REASON)
    assert rc != 0, rc
    after = L.load(lp)
    assert _retry_events(after) == []
    assert after["counters"]["machine_iter"] == before["counters"]["machine_iter"]


def test_guard_blocked_retry_is_not_registered_and_needs_user(repo, cli):
    _wt, lp, _before = _fresh_fail(repo, cli, max_iterations=3)
    cli("machine", "--session", "S1", expect=1)
    third = cli("machine", "--session", "S1", expect=3)
    assert {"MAX_ITERATIONS", "NO_PROGRESS"} <= {b["code"] for b in third["readiness"]["blockers"]}
    blocked = L.load(lp)
    rc, _out = _bl(repo, "machine", "--session", "S1", "--retry", "--reason", REASON)
    assert rc != 0, rc  # 仍需用户授权，不是一个可绕过的重试
    after = L.load(lp)
    assert _retry_events(after) == []
    assert after["counters"]["machine_iter"] == blocked["counters"]["machine_iter"]
    assert after["evidence"].get("machine") == blocked["evidence"].get("machine")
