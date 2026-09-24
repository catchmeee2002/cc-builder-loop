"""B1: mutation patch 经 "patch_file"（绝对路径）交付，登记后以文件字节原样写进
ledger.proof_spec 的 "patch" 字符串（.encode('utf-8') 与文件字节逐字节相等），该组不再含
"patch_file" 键；随后 `bl proof` 用这份 patch，其 counterexample.patch_sha256 等于文件字节的
sha256。patch 文件在 run 外的临时目录生成，hunk 尾部天然含只有一个空格的空白上下文行
（add/mul 之间的空行经 git diff 渲染即是如此），并以换行结尾。

覆盖对象：hooks.py 的 PreToolUse(SubagentHandback) 与 record_role_result 对 "patch_file" 字段
的解析——冻结基线上尚不存在，这里的核心断言在起点代码上会在 call 阶段直接失败。
"""

from __future__ import annotations

import hashlib

from builder_loop import ledger as L
from conftest import (implement_mul, make_tester_result, marker, mutation_patch,
                       pre_handback, role_turn, write_mul_test, write_patch_file)


def _ready_for_patch(started, cli, hook):
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    return wt, twt


def test_b1_patch_file_bytes_land_in_ledger_and_proof_uses_them(started, cli, hook, tmp_path):
    wt, twt = _ready_for_patch(started, cli, hook)
    patch_text = mutation_patch(wt) + "\n"  # 以换行结尾
    pf = write_patch_file(patch_text, tmp_path / "outside-run")
    expected_bytes = pf.read_bytes()
    assert expected_bytes == patch_text.encode("utf-8")
    assert expected_bytes.endswith(b"\n")
    # hunk 尾部含只有一个空格的空白上下文行（add/mul 之间的空行）
    assert any(line == b" " for line in expected_bytes.split(b"\n")[:-1]), expected_bytes

    payload = make_tester_result("mutation", patch_file=str(pf))

    pre = pre_handback(hook, "tester", "T1", marker(payload))
    pre_json = pre["json"] or {}
    assert (pre_json.get("hookSpecificOutput") or {}).get("permissionDecision") != "deny", pre

    r = role_turn(hook, "tester", "T1", payload, start=False)
    assert r["code"] == 0, r

    lg = L.load(started["ledger"])
    group = lg["proof_spec"]["groups"][0]
    assert "patch_file" not in group, group
    assert group["patch"].encode("utf-8") == expected_bytes, (group.get("patch"), expected_bytes)

    out = cli("proof", "--session", "S1")
    assert out["result"] == "PASS", out
    ce = out["groups"][0]["counterexample"]
    assert ce["patch_sha256"] == hashlib.sha256(expected_bytes).hexdigest(), ce


def test_b1_boundary_chinese_comment_patch_file_bytes_exact(started, cli, hook, tmp_path):
    """边界：patch 文件含中文注释行时同样逐字节相等。"""
    wt, twt = _ready_for_patch(started, cli, hook)
    src = wt / "src" / "foo.py"
    original = src.read_text()
    src.write_text(original.replace("return a * b", "return a + b  # 中文注释：破坏乘法"))
    from conftest import git

    patch_text = git(wt, "diff") + "\n"
    src.write_text(original)
    pf = write_patch_file(patch_text, tmp_path / "outside-run-zh")
    expected_bytes = pf.read_bytes()

    payload = make_tester_result("mutation", patch_file=str(pf))
    r = role_turn(hook, "tester", "T1", payload, start=False)
    assert r["code"] == 0, r
    lg = L.load(started["ledger"])
    group = lg["proof_spec"]["groups"][0]
    assert group["patch"].encode("utf-8") == expected_bytes


def test_b1_boundary_first_round_blind_still_accepts_missing_patch(started, cli, hook):
    """边界：首轮盲写时 mutation 组既无 patch 也无 patch_file：照旧接受，groups 缺 patch。"""
    twt = started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    write_mul_test(twt)
    payload = make_tester_result("mutation")  # 既无 patch 也无 patch_file
    r = role_turn(hook, "tester", "T1", payload, start=False)
    assert r["code"] == 0, r
    lg = L.load(started["ledger"])
    group = lg["proof_spec"]["groups"][0]
    assert not (group.get("patch") or "").strip()
    assert "patch_file" not in group
