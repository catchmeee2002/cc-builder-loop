"""B1/B2/B3：mutation patch 的 apply 预检、路径归属、test_id 语言校验要在 tester 交卷（handback）
当场做掉，不等到 `bl proof`。参见 run proof-input-precheck-and-private-pycache-* 的 mission。

这些测试锚定的是 `validate_spec` 的新行为（在冻结基线上尚未实现）：交卷时 hook 必须非 0 返回，
stderr 含 PROOF_SPEC_INVALID；此前基线只在 `bl proof` 真正跑到 mutation 阶段才发现同样的问题，
所以这里的核心断言在起点代码上会在 call 阶段直接失败（baseline-red）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from builder_loop import ledger as L
from conftest import (
    contract_with, git, implement_mul, mutation_patch, role_turn,
    make_tester_result, write_mul_test, write_plan,
)


# ---------------------------------------------------------------- 构造坏 patch


def corrupt_apply_patch(wt: Path) -> str:
    """一段头部行数声明比 body 多 1 行的合法外观 diff：git apply 必定报 `corrupt patch`
    （对应 B1 边界：hunk 头 @@ -a,n +a,n @@ 声明的行数比 body 实际多）。"""
    src = wt / "src" / "foo.py"
    original = src.read_text()
    src.write_text(original.replace("return a * b", "return a + b"))
    patch = git(wt, "diff")
    src.write_text(original)

    def bump(m: re.Match) -> str:
        return f"@@ -{m[1]},{int(m[2]) + 1} +{m[3]},{int(m[4]) + 1} @@"

    corrupted = re.sub(r"@@ -(\d+),(\d+) \+(\d+),(\d+) @@", bump, patch, count=1)
    assert corrupted != patch  # 保证正则确实命中了
    return corrupted


def nonexistent_target_patch(path: str = "src/nope.py") -> str:
    """diff 头指向候选 head 上根本不存在的文件。"""
    return (
        f"diff --git a/{path} b/{path}\n"
        f"index 0000000..1111111 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -1 +1 @@\n"
        f"-old\n"
        f"+new\n"
    )


def _snapshot(wt: Path, repo_root: Path) -> tuple[str, str, str]:
    return git(wt, "status", "--porcelain"), git(wt, "rev-parse", "HEAD"), git(repo_root, "worktree", "list")


# ---------------------------------------------------------------- B1：mutation patch apply 预检


def test_b1_mutation_patch_apply_precheck_at_handback(started, cli, hook):
    """given 候选已 integrate 过一次、B1 实现存在 / when 交卷的 mutation patch 打不上或碰不存在的文件
    / then 当场打回：hook 非 0、stderr 含 PROOF_SPEC_INVALID 与 git 的原始报错或点名的路径；
    proof_spec / tester evidence 不被这次坏交卷改写。"""
    wt, twt = started["worktree"], started["tester_worktree"]
    repo_root = started["repo"].root
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)

    # 首轮盲写：候选尚不可读，非空 patch 也不做 apply 预检（哪怕内容打不上）——不变量
    bad_before_integrate = corrupt_apply_patch(wt)
    r0 = role_turn(hook, "tester", "T1", make_tester_result("mutation", bad_before_integrate), start=False)
    assert r0["code"] == 0, r0

    cli("integrate", "--session", "S1")
    lg_before = L.load(started["ledger"])
    before_snapshot = _snapshot(wt, repo_root)

    # 集成之后：打不上的 patch（hunk 头行数比 body 多 1）被当场打回
    bad_patch = corrupt_apply_patch(wt)
    r1 = role_turn(hook, "tester", "T1", make_tester_result("mutation", bad_patch))
    assert r1["code"] != 0
    assert "PROOF_SPEC_INVALID" in r1["stderr"]
    assert "corrupt patch" in r1["stderr"], r1["stderr"]
    lg_after = L.load(started["ledger"])
    assert lg_after["proof_spec"] == lg_before["proof_spec"]
    assert lg_after["evidence"]["tester"] == lg_before["evidence"]["tester"]
    assert _snapshot(wt, repo_root) == before_snapshot  # 预检不留残留、不改候选状态

    # 指向候选上不存在的文件同样被打回，并点名该路径
    missing_patch = nonexistent_target_patch("src/nope.py")
    r2 = role_turn(hook, "tester", "T1", make_tester_result("mutation", missing_patch))
    assert r2["code"] != 0
    assert "PROOF_SPEC_INVALID" in r2["stderr"] and "src/nope.py" in r2["stderr"]
    assert _snapshot(wt, repo_root) == before_snapshot

    # 边界：patch 本身正确、只是末尾缺换行 → 放行（mutation_patch() 特意不带末尾换行）
    good_patch = mutation_patch(wt)
    r3 = role_turn(hook, "tester", "T1", make_tester_result("mutation", good_patch))
    assert r3["code"] == 0, r3
    lg_final = L.load(started["ledger"])
    assert lg_final["proof_spec"]["groups"][0]["patch"] == good_patch


def test_b1_multi_group_patch_error_locates_offending_group(repo, cli, hook):
    """边界：contract 有两个 behavior 时，坏 patch 落在第 2 组，打回信息能定位到那一组。"""
    c = contract_with()
    c["mission"]["behaviors"] = [
        {"id": "B1", "given": "two ints", "when": "mul(a,b)", "then": "returns product"},
        {"id": "B2", "given": "two ints, b != 0", "when": "div(a,b)", "then": "returns quotient"},
    ]
    c["mission"]["interfaces"] = ["src/foo.py::mul(a:int,b:int)->int", "src/foo.py::div(a:int,b:int)->int"]
    write_plan(repo.root, c, "two.md")
    out = cli("start", "--plan", str(repo.root / "two.md"), "--session", "S1")
    wt, twt = Path(out["worktree"]), Path(out["tester_worktree"])

    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    (wt / "src" / "foo.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n\n\ndef div(a, b):\n    return a / b\n"
    )
    cli("checkpoint", "--session", "S1", "--role", "builder")
    (twt / "tests" / "test_mul.py").write_text("from src.foo import mul\n\n\ndef test_mul():\n    assert mul(3, 4) == 12\n")
    (twt / "tests" / "test_div.py").write_text("from src.foo import div\n\n\ndef test_div():\n    assert div(6, 3) == 2\n")

    first = {
        "role": "tester", "status": "pass", "behaviors_covered": ["B1", "B2"],
        "proof_spec": {"groups": [
            {"kind": "mutation", "behavior_ids": ["B1"], "test_ids": ["tests/test_mul.py::test_mul"], "timeout": 60},
            {"kind": "mutation", "behavior_ids": ["B2"], "test_ids": ["tests/test_div.py::test_div"], "timeout": 60},
        ]},
    }
    assert role_turn(hook, "tester", "T1", first, start=False)["code"] == 0
    cli("integrate", "--session", "S1")

    bad = nonexistent_target_patch("src/nope.py")
    second = {
        "role": "tester", "status": "pass", "behaviors_covered": ["B1", "B2"],
        "proof_spec": {"groups": [
            {"kind": "mutation", "behavior_ids": ["B1"], "test_ids": ["tests/test_mul.py::test_mul"], "timeout": 60},
            {"kind": "mutation", "behavior_ids": ["B2"], "test_ids": ["tests/test_div.py::test_div"], "timeout": 60, "patch": bad},
        ]},
    }
    r = role_turn(hook, "tester", "T1", second)
    assert r["code"] != 0
    assert "PROOF_SPEC_INVALID" in r["stderr"]
    assert "groups[1]" in r["stderr"] or "B2" in r["stderr"], r["stderr"]


# ---------------------------------------------------------------- B2：mutation patch 归属预检


def test_b2_mutation_patch_ownership_precheck_at_handback(started, cli, hook):
    """given 任意一轮 tester 交卷 / when mutation patch 改到了 tests/test_mul.py（tester 地盘）
    / then 当场打回：不必等到集成 / bl proof。归属判定不依赖候选是否可读，首轮盲写也当场生效。"""
    twt = started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    write_mul_test(twt)
    bad = (
        "diff --git a/tests/test_mul.py b/tests/test_mul.py\n"
        "--- a/tests/test_mul.py\n"
        "+++ b/tests/test_mul.py\n"
        "@@ -4 +4 @@\n"
        "-    assert mul(3, 4) == 12\n"
        "+    assert False\n"
    )
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation", bad), start=False)
    assert r["code"] != 0
    assert "PROOF_SPEC_INVALID" in r["stderr"] and "tests/test_mul.py" in r["stderr"], r["stderr"]
    lg = L.load(started["ledger"])
    assert lg["evidence"]["tester"] is None
    assert lg.get("proof_spec") is None

    # 只改 builder 地盘（src/foo.py）的合法 patch，不因归属被打回（此时候选还不可读，只是静态归属判定）
    good_placeholder = (
        "diff --git a/src/foo.py b/src/foo.py\n"
        "--- a/src/foo.py\n"
        "+++ b/src/foo.py\n"
        "@@ -1 +1 @@\n"
        "-def add(a, b):\n"
        "+def add(a, b):  # noop\n"
    )
    r2 = role_turn(hook, "tester", "T1", make_tester_result("mutation", good_placeholder), start=False)
    # apply 预检对候选不可读期不生效，这里只验证不因归属被拒（可能因 apply/其它原因失败，但不应提 test_mul.py 归属）
    assert "tests/test_mul.py" not in r2["stderr"]


def test_b2_proof_entry_rechecks_ownership_via_spec_file(started, cli, hook, tmp_path):
    """不变量：`bl proof --spec-file` 绕过 handback 直接进入 validate_spec，同一归属检查依旧生效。"""
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    cli("integrate", "--session", "S1")
    cli("machine", "--session", "S1")
    role_turn(hook, "tester", "T1", make_tester_result("mutation", mutation_patch(wt)))

    bad_spec = {"groups": [{
        "kind": "mutation", "behavior_ids": ["B1"], "test_ids": ["tests/test_mul.py::test_mul"], "timeout": 60,
        "patch": (
            "diff --git a/tests/test_mul.py b/tests/test_mul.py\n"
            "--- a/tests/test_mul.py\n"
            "+++ b/tests/test_mul.py\n"
            "@@ -4 +4 @@\n"
            "-    assert mul(3, 4) == 12\n"
            "+    assert False\n"
        ),
    }]}
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(bad_spec))
    out = cli("proof", "--session", "S1", "--spec-file", str(spec_file), expect=1)
    assert out["code"] == "PROOF_SPEC_INVALID"
    assert "tests/test_mul.py" in out["message"]


# ---------------------------------------------------------------- B3：test_id 语言校验


def test_b3_non_python_test_id_rejected_under_pytest_framework(started, cli, hook):
    """given loop.yml 的 proof_runner.framework 是 pytest / when test_ids 的文件部分不以 .py 结尾
    / then 当场打回，提示 suggested_owner=contract、语言不匹配，并建议 tester 改交 insufficient_spec。
    边界：语言检查先于存在性检查——文件不管存不存在，报的都是语言不匹配，不是"不存在"。"""
    twt = started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    write_mul_test(twt)

    # 文件确实存在于 tester worktree 里（tests_write 内），但扩展名不是 .py
    (twt / "tests" / "mul_test.go").write_text("package tests\nfunc TestMul(t *testing.T) {}\n")
    payload_exists = make_tester_result("mutation", test_ids=["tests/mul_test.go::TestMul"])
    r1 = role_turn(hook, "tester", "T1", payload_exists, start=False)
    assert r1["code"] != 0
    assert "PROOF_SPEC_INVALID" in r1["stderr"]
    assert "suggested_owner" in r1["stderr"] and "contract" in r1["stderr"], r1["stderr"]
    assert "insufficient_spec" in r1["stderr"] and "loop.yml" in r1["stderr"], r1["stderr"]
    assert "不存在" not in r1["stderr"]

    # 文件根本不存在：同样报语言不匹配，不是"不存在"
    payload_missing = make_tester_result("mutation", test_ids=["tests/mul_missing.go::TestMul"])
    r2 = role_turn(hook, "tester", "T1", payload_missing, start=False)
    assert r2["code"] != 0
    assert "PROOF_SPEC_INVALID" in r2["stderr"] and "不存在" not in r2["stderr"]

    # .py 文件不受影响
    r3 = role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    assert r3["code"] == 0, r3


def test_b3_bl_proof_entry_rejects_non_python_test_id(started, cli, hook, tmp_path):
    """不变量：`bl proof` 入口遇到同一 spec 也报 PROOF_SPEC_INVALID（语言检查不是只在 handback 才跑）。
    文件本身确实存在于 tester 分支里（避免与既有的"不存在"检查混淆——这里要暴露的是语言检查）。"""
    twt = started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    write_mul_test(twt)
    (twt / "tests" / "mul_test.go").write_text("package tests\nfunc TestMul(t *testing.T) {}\n")
    role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    cli("integrate", "--session", "S1")
    spec = {"groups": [{"kind": "mutation", "behavior_ids": ["B1"], "test_ids": ["tests/mul_test.go::TestMul"], "timeout": 60}]}
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec))
    out = cli("proof", "--session", "S1", "--spec-file", str(spec_file), expect=1)
    assert out["code"] == "PROOF_SPEC_INVALID"
    assert out["details"].get("suggested_owner") == "contract"
    assert "不存在" not in out["message"]


def test_b3_language_check_skipped_for_generic_framework():
    """边界：framework=generic 时不做语言检查（纯函数单测：不需要真的配一套 go/generic 项目）。"""
    from builder_loop import proof as P

    lg = {
        "contract": {
            "assurance": {
                "proof_kinds": ["baseline-red", "mutation", "reviewed-boundaries"],
                "proof_runner": {"framework": "generic", "cmd": "go test {tests}"},
            },
            "mission": {"behaviors": [{"id": "B1", "proof": "strong"}]},
            "authority": {"builder_write": ["src/**"], "tester_write": ["tests/**"], "protected_paths": []},
        },
        "tester": {"head": None},
    }
    spec = {"groups": [{"kind": "mutation", "behavior_ids": ["B1"], "test_ids": ["tests/mul_test.go::TestMul"], "timeout": 60}]}
    out = P.validate_spec(spec, lg, Path("/nonexistent-repo-root"))
    assert out["groups"][0]["test_ids"] == ["tests/mul_test.go::TestMul"]
