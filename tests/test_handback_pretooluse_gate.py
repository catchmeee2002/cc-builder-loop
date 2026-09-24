"""B3: tester / reviewer 已登记且本轮是续接/首次 spawn（非唤醒）时，PreToolUse(SubagentHandback)
要在投递前校验 message：不合规就 deny（reason 以 "[builder-loop]" 开头、含具体错误与重调
SubagentHandback 的提示），并记一条 role_malformed(final=false)，不产生 role_result；合规则
静默放行，PreToolUse 不写 role_result，随后 PostToolUse(SubagentHandback) 才登记
role_result(via=handback)。

覆盖对象：hooks.py 新增的 PreToolUse(SubagentHandback) 分支——冻结基线上不存在（当前
handle_pre_tool_use 对 SubagentHandback 一律放行），下面的 deny 断言在起点代码上会在 call 阶段
直接失败（baseline-red）。
"""

from __future__ import annotations

from builder_loop import ledger as L
from conftest import (handback, implement_mul, make_tester_result, marker,
                       pre_handback, send_message, write_mul_test, write_patch_file)


def _tester_ready(started, cli, hook) -> None:
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(started["worktree"])
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(started["tester_worktree"])


def _reviewer_ready(started, cli, hook, agent_id: str = "R1") -> None:
    implement_mul(started["worktree"])
    cli("checkpoint", "--session", "S1", "--role", "builder")
    hook("SubagentStart", {"session_id": "S1", "agent_id": agent_id, "agent_type": "reviewer"})


def _malformed_events(started):
    return [e for e in L.load(started["ledger"])["events"] if e["kind"] == "role_malformed"]


def _result_events(started):
    return [e for e in L.load(started["ledger"])["events"] if e["kind"] == "role_result"]


def _assert_pre_deny(pre) -> str:
    j = pre["json"] or {}
    hso = j.get("hookSpecificOutput") or {}
    assert hso.get("permissionDecision") == "deny", pre
    reason = hso.get("permissionDecisionReason") or ""
    assert reason.startswith("[builder-loop]"), reason
    assert "SubagentHandback" in reason, reason
    return reason


def test_b3_no_result_marker_denied(started, cli, hook):
    _tester_ready(started, cli, hook)
    pre = pre_handback(hook, "tester", "T1", "我做完了，没有写结果行。")
    reason = _assert_pre_deny(pre)
    assert "没有找到 BUILDER_LOOP_RESULT" in reason, reason
    assert _malformed_events(started) and _malformed_events(started)[-1]["final"] is False
    assert not _result_events(started)


def test_b3_json_parse_failure_denied(started, cli, hook):
    """真正的 JSON 解析失败分支要求那一行本身"看起来像"一个 `{...}`（结果行正则要求单行、
    以 `}` 收尾），内容却不是合法 JSON——用尾随逗号触发，跟"没写全大括号"（走的是"格式不对"
    分支，见 B3 的 no_result_marker/wrong_role 等用例）是两回事。"""
    _tester_ready(started, cli, hook)
    pre = pre_handback(hook, "tester", "T1", 'done\nBUILDER_LOOP_RESULT: {"role": "tester", "status": "pass",}')
    reason = _assert_pre_deny(pre)
    assert "JSON 解析失败" in reason, reason


def test_b3_wrong_role_field_denied(started, cli, hook):
    _tester_ready(started, cli, hook)
    bad = make_tester_result("mutation")
    bad["role"] = "builder"
    pre = pre_handback(hook, "tester", "T1", marker(bad))
    reason = _assert_pre_deny(pre)
    assert "role" in reason, reason


def test_b3_proof_spec_structure_invalid_denied(started, cli, hook):
    _tester_ready(started, cli, hook)
    bad = make_tester_result("mutation")
    bad["proof_spec"]["groups"][0]["behavior_ids"] = ["B1", "B2"]
    pre = pre_handback(hook, "tester", "T1", marker(bad))
    reason = _assert_pre_deny(pre)
    assert "PROOF_SPEC_INVALID" in reason, reason


def test_b3_mutation_patch_that_does_not_apply_denied(started, cli, hook, tmp_path):
    _tester_ready(started, cli, hook)
    r0 = pre_handback(hook, "tester", "T1", marker(make_tester_result("mutation")))
    assert (r0["json"] or {}).get("hookSpecificOutput", {}).get("permissionDecision") != "deny", r0
    assert hook("PostToolUse", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester",
                                "tool_name": "SubagentHandback", "tool_input": {"message": marker(make_tester_result("mutation"))},
                                "tool_response": {"success": True}})["code"] == 0
    cli("integrate", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"

    broken = (
        "diff --git a/src/foo.py b/src/foo.py\n"
        "index 0000000..1111111 100644\n"
        "--- a/src/foo.py\n"
        "+++ b/src/foo.py\n"
        "@@ -99,3 +99,3 @@\n"
        "-nonexistent context\n"
        "+won't apply\n"
    )
    pf = write_patch_file(broken, tmp_path / "outside-broken")
    payload = make_tester_result("mutation", patch_file=str(pf))
    pre = pre_handback(hook, "tester", "T1", marker(payload))
    _assert_pre_deny(pre)


def test_b3_boundary_reviewer_finding_missing_owner_denied(started, cli, hook):
    _reviewer_ready(started, cli, hook)
    bad = {"role": "reviewer", "verdict": "changes_requested",
           "findings": [{"severity": "major", "file": "src/foo.py", "line": 1, "summary": "缺 owner"}],
           "behaviors_verified": []}
    pre = pre_handback(hook, "reviewer", "R1", marker(bad))
    _assert_pre_deny(pre)


def test_b3_compliant_report_pre_silent_then_post_registers(started, cli, hook):
    _tester_ready(started, cli, hook)
    payload = make_tester_result("mutation")
    pre = pre_handback(hook, "tester", "T1", marker(payload))
    j = pre["json"] or {}
    assert (j.get("hookSpecificOutput") or {}).get("permissionDecision") != "deny", pre
    assert not _result_events(started), "PreToolUse 不应写 role_result"

    post = handback(hook, "tester", "T1", marker(payload))
    assert post["code"] == 0, post
    res = _result_events(started)
    assert len(res) == 1 and res[0].get("via") == "handback", res


def test_b3_boundary_deny_then_compliant_same_round_resets_stops(started, cli, hook):
    _tester_ready(started, cli, hook)
    pre1 = pre_handback(hook, "tester", "T1", "没有结果行。")
    _assert_pre_deny(pre1)
    assert L.load(started["ledger"])["agents"]["tester"]["stops"] == 1

    payload = make_tester_result("mutation")
    pre2 = pre_handback(hook, "tester", "T1", marker(payload))
    assert (pre2["json"] or {}).get("hookSpecificOutput", {}).get("permissionDecision") != "deny", pre2
    post2 = handback(hook, "tester", "T1", marker(payload))
    assert post2["code"] == 0, post2
    assert L.load(started["ledger"])["agents"]["tester"]["stops"] == 0


def test_b3_invariant_no_handback_stop_fallback_unaffected(started, cli, hook):
    """不变量：没有 SubagentHandback 的环境（不调用 PreToolUse(SubagentHandback)）：SubagentStop
    兜底解析登记的行为不变。"""
    _tester_ready(started, cli, hook)
    payload = make_tester_result("mutation")
    r = hook("SubagentStop", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester",
                              "last_assistant_message": marker(payload)})
    assert r["code"] == 0, r
    res = _result_events(started)
    assert len(res) == 1 and res[0].get("via") == "stop", res
