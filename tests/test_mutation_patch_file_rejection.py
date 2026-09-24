"""B2: 与 B1 相同的 run（候选已集成、tester 可读）下，mutation 组的 patch 交付方式不合规时，
PreToolUse(SubagentHandback) 必须 deny：reason 含 PROOF_SPEC_INVALID 与出错的 groups[i]；只给
inline patch 时 reason 还含 "patch_file" 字样（提示改用它）。ledger 不产生 role_result、
proof_spec 不变。边界：同样的 payload 绕过 PreToolUse 直接走 PostToolUse(SubagentHandback)，
同样不登记，改记 role_malformed（Post 路径保留校验）。

覆盖对象：hooks.py 新增的 PreToolUse(SubagentHandback) 校验——冻结基线上不存在，起点上
PreToolUse 对 SubagentHandback 一律放行（silent），下面的 deny 断言在起点代码上会在 call
阶段直接失败（baseline-red）。
"""

from __future__ import annotations

from builder_loop import ledger as L
from conftest import (implement_mul, make_tester_result, marker, mutation_patch,
                       pre_handback, role_turn, write_mul_test, write_patch_file)


def _role_result_count(ledger_path) -> int:
    return len([e for e in L.load(ledger_path)["events"] if e["kind"] == "role_result"])


def _ready(started, cli, hook):
    """返回 (候选 worktree, tester worktree, 此刻 ledger.proof_spec 的快照, 此刻 role_result 事件数)。
    首轮盲写（第 27 行）本身就是一次合规交卷，已经登记过一条 proof_spec 与一条 role_result——后续
    坏交卷的『不变』『不产生 role_result』要跟这个时间点的快照/计数比，不是跟 None / 0 比。"""
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    proof_spec_before = L.load(started["ledger"]).get("proof_spec")
    role_result_count_before = _role_result_count(started["ledger"])
    return wt, twt, proof_spec_before, role_result_count_before


def _group(patch: str | None = None, patch_file: str | None = None) -> dict:
    g = {"kind": "mutation", "behavior_ids": ["B1"], "test_ids": ["tests/test_mul.py::test_mul"], "timeout": 60}
    if patch is not None:
        g["patch"] = patch
    if patch_file is not None:
        g["patch_file"] = patch_file
    return g


def _payload(group: dict) -> dict:
    return {"role": "tester", "status": "pass", "behaviors_covered": ["B1"], "proof_spec": {"groups": [group]}}


def _assert_denied(pre, ledger_path, proof_spec_before, role_result_count_before):
    """proof_spec 不变、不产生 role_result：都是跟 `_ready()` 那一刻的快照/计数比，不是跟
    None / 0 比——`_ready()` 本身的首轮盲写已经合规登记过一次。"""
    j = pre["json"] or {}
    hso = j.get("hookSpecificOutput") or {}
    assert hso.get("permissionDecision") == "deny", pre
    reason = hso.get("permissionDecisionReason") or ""
    assert "PROOF_SPEC_INVALID" in reason and "groups[0]" in reason, reason
    lg = L.load(ledger_path)
    assert lg.get("proof_spec") == proof_spec_before
    assert _role_result_count(ledger_path) == role_result_count_before
    return reason


def test_b2_inline_only_patch_denied_with_patch_file_hint(started, cli, hook):
    wt, twt, proof_spec_before, role_result_count_before = _ready(started, cli, hook)
    payload = _payload(_group(patch=mutation_patch(wt)))
    pre = pre_handback(hook, "tester", "T1", marker(payload))
    reason = _assert_denied(pre, started["ledger"], proof_spec_before, role_result_count_before)
    assert "patch_file" in reason, reason


def test_b2_patch_and_patch_file_together_denied(started, cli, hook, tmp_path):
    wt, twt, proof_spec_before, role_result_count_before = _ready(started, cli, hook)
    pf = write_patch_file(mutation_patch(wt) + "\n", tmp_path / "both")
    payload = _payload(_group(patch=mutation_patch(wt), patch_file=str(pf)))
    pre = pre_handback(hook, "tester", "T1", marker(payload))
    _assert_denied(pre, started["ledger"], proof_spec_before, role_result_count_before)


def test_b2_relative_patch_file_denied(started, cli, hook):
    wt, twt, proof_spec_before, role_result_count_before = _ready(started, cli, hook)
    payload = _payload(_group(patch_file="relative/mutation.patch"))
    pre = pre_handback(hook, "tester", "T1", marker(payload))
    _assert_denied(pre, started["ledger"], proof_spec_before, role_result_count_before)


def test_b2_nonexistent_patch_file_denied(started, cli, hook, tmp_path):
    wt, twt, proof_spec_before, role_result_count_before = _ready(started, cli, hook)
    payload = _payload(_group(patch_file=str(tmp_path / "outside" / "does-not-exist.patch")))
    pre = pre_handback(hook, "tester", "T1", marker(payload))
    _assert_denied(pre, started["ledger"], proof_spec_before, role_result_count_before)


def test_b2_patch_file_is_directory_denied(started, cli, hook, tmp_path):
    wt, twt, proof_spec_before, role_result_count_before = _ready(started, cli, hook)
    d = tmp_path / "outside" / "adir"
    d.mkdir(parents=True)
    payload = _payload(_group(patch_file=str(d)))
    pre = pre_handback(hook, "tester", "T1", marker(payload))
    _assert_denied(pre, started["ledger"], proof_spec_before, role_result_count_before)


def test_b2_patch_file_too_large_denied(started, cli, hook, tmp_path):
    wt, twt, proof_spec_before, role_result_count_before = _ready(started, cli, hook)
    big = "diff --git a/src/foo.py b/src/foo.py\n" + ("+" + "x" * 200 + "\n") * 1400  # > 256 KiB
    pf = write_patch_file(big, tmp_path / "outside-big")
    assert pf.stat().st_size > 256 * 1024
    payload = _payload(_group(patch_file=str(pf)))
    pre = pre_handback(hook, "tester", "T1", marker(payload))
    _assert_denied(pre, started["ledger"], proof_spec_before, role_result_count_before)


def test_b2_patch_file_invalid_utf8_denied(started, cli, hook, tmp_path):
    wt, twt, proof_spec_before, role_result_count_before = _ready(started, cli, hook)
    d = tmp_path / "outside-bad-utf8"
    d.mkdir(parents=True)
    pf = d / "mutation.patch"
    pf.write_bytes(b"diff --git a/src/foo.py b/src/foo.py\n\xff\xfe not valid utf-8\n")
    payload = _payload(_group(patch_file=str(pf)))
    pre = pre_handback(hook, "tester", "T1", marker(payload))
    _assert_denied(pre, started["ledger"], proof_spec_before, role_result_count_before)


def test_b2_patch_file_inside_candidate_worktree_denied(started, cli, hook):
    wt, twt, proof_spec_before, role_result_count_before = _ready(started, cli, hook)
    pf = write_patch_file(mutation_patch(wt) + "\n", wt / "leaked-patch-dir")
    payload = _payload(_group(patch_file=str(pf)))
    pre = pre_handback(hook, "tester", "T1", marker(payload))
    _assert_denied(pre, started["ledger"], proof_spec_before, role_result_count_before)


def test_b2_patch_file_inside_tester_worktree_denied(started, cli, hook):
    wt, twt, proof_spec_before, role_result_count_before = _ready(started, cli, hook)
    pf = write_patch_file(mutation_patch(wt) + "\n", twt / "leaked-patch-dir")
    payload = _payload(_group(patch_file=str(pf)))
    pre = pre_handback(hook, "tester", "T1", marker(payload))
    _assert_denied(pre, started["ledger"], proof_spec_before, role_result_count_before)


def test_b2_patch_file_inside_main_repo_denied(started, cli, hook):
    wt, twt, proof_spec_before, role_result_count_before = _ready(started, cli, hook)
    repo_root = started["repo"].root
    pf = write_patch_file(mutation_patch(wt) + "\n", repo_root / "leaked-patch-dir")
    payload = _payload(_group(patch_file=str(pf)))
    pre = pre_handback(hook, "tester", "T1", marker(payload))
    _assert_denied(pre, started["ledger"], proof_spec_before, role_result_count_before)


def test_b2_boundary_same_payload_bypassing_pretooluse_still_rejected_at_post(started, cli, hook):
    """边界：同样的不合规 payload 若绕过 PreToolUse 直接走 PostToolUse(SubagentHandback)，
    同样不登记 role_result，改记 role_malformed（Post 路径保留校验）。"""
    wt, twt, proof_spec_before, role_result_count_before = _ready(started, cli, hook)
    payload = _payload(_group(patch=mutation_patch(wt)))  # 只给 inline patch
    r = role_turn(hook, "tester", "T1", payload, start=False)
    assert r["code"] != 0
    lg = L.load(started["ledger"])
    assert _role_result_count(started["ledger"]) == role_result_count_before  # `_ready()` 那一条不算数
    malformed = [e for e in lg["events"] if e["kind"] == "role_malformed"]
    assert malformed and malformed[-1]["role"] == "tester", lg["events"]


def test_b2_invariant_ownership_check_still_protects_builder_owned_files(started, cli, hook, tmp_path):
    """不变量：patch 只能改 builder 拥有文件的既有校验（含 suggested_owner=contract 的提示）不变——
    用 patch_file 交付一份改到 tests/test_mul.py（tester 地盘）的 patch，同样在归属层面被拒。"""
    wt, twt, proof_spec_before, role_result_count_before = _ready(started, cli, hook)
    bad = (
        "diff --git a/tests/test_mul.py b/tests/test_mul.py\n"
        "--- a/tests/test_mul.py\n"
        "+++ b/tests/test_mul.py\n"
        "@@ -4 +4 @@\n"
        "-    assert mul(3, 4) == 12\n"
        "+    assert False\n"
    )
    pf = write_patch_file(bad, tmp_path / "outside-ownership")
    payload = _payload(_group(patch_file=str(pf)))
    r = role_turn(hook, "tester", "T1", payload, start=False)
    assert r["code"] != 0
    assert "tests/test_mul.py" in r["stderr"], r["stderr"]
