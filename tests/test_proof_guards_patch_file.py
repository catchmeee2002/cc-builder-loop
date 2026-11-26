"""B6：`proof.resolve_patch_files` 处理 guard 的 `patch_file`——把它替换成从文件读到的原始字节（key 为 `patch`）。

- guard 带 patch_file（worktree 之外的绝对路径）→ 替换为 `patch`，字节原样解码；
- guard 同时给 `patch` 与 `patch_file`、或只给 inline `patch` → PROOF_SPEC_INVALID；
- guard 既没有 patch_file 也没有 patch → 原样保留（首轮盲写允许省略）。

起点代码的 resolve_patch_files 只处理 groups，对 guards 视而不见，所以「替换 / 拒绝」的断言在起点上
call 阶段失败（baseline-red）。「都没有时原样保留」「mutation 组 patch_file 行为不变」是守卫（不列入 group）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from builder_loop import ledger as L
from builder_loop import proof as P
from builder_loop.errors import Problem

from conftest import write_patch_file

GUARD_PATCH = (
    "diff --git a/src/foo.py b/src/foo.py\n"
    "--- a/src/foo.py\n"
    "+++ b/src/foo.py\n"
    "@@ -1,2 +1,2 @@\n"
    "-def add(a, b):\n"
    "-    return a + b\n"
    "+def add(a, b):\n"
    "+    return a - b\n"
)
GUARD_ID = "tests/test_guard.py::test_guard"
GROUP_ID = "tests/test_mul.py::test_mul"


def _spec(guard: dict) -> dict:
    return {"groups": [{"kind": "mutation", "behavior_ids": ["B1"], "test_ids": [GROUP_ID], "timeout": 60,
                        "guards": [guard]}]}


def _resolve(started, guard: dict):
    lg = L.load(started["ledger"])
    return P.resolve_patch_files(_spec(guard), lg, started["repo"].root), lg


# ---------------------------------------------------------------- 替换 patch_file → patch


def test_b6_guard_patch_file_replaced_with_raw_bytes(started, tmp_path):
    pf = write_patch_file(GUARD_PATCH, directory=tmp_path, name="guard.patch")
    out, _ = _resolve(started, {"test_ids": [GUARD_ID], "patch_file": str(pf)})
    guard = out["groups"][0]["guards"][0]
    assert guard["patch"] == GUARD_PATCH, guard
    assert "patch_file" not in guard, guard


# ---------------------------------------------------------------- 非法交付形态被拒


def test_b6_guard_with_both_patch_and_patch_file_rejected(started, tmp_path):
    pf = write_patch_file(GUARD_PATCH, directory=tmp_path, name="guard2.patch")
    with pytest.raises(Problem) as ei:
        _resolve(started, {"test_ids": [GUARD_ID], "patch": GUARD_PATCH, "patch_file": str(pf)})
    assert ei.value.code == "PROOF_SPEC_INVALID", ei.value.to_json()


def test_b6_guard_inline_patch_rejected(started):
    with pytest.raises(Problem) as ei:
        _resolve(started, {"test_ids": [GUARD_ID], "patch": GUARD_PATCH})
    assert ei.value.code == "PROOF_SPEC_INVALID", ei.value.to_json()


# ---------------------------------------------------------------- 边界：与 mutation 组一致的拒绝形态


def test_b6_guard_patch_file_relative_path_rejected(started):
    with pytest.raises(Problem) as ei:
        _resolve(started, {"test_ids": [GUARD_ID], "patch_file": "relative/guard.patch"})
    assert ei.value.code == "PROOF_SPEC_INVALID", ei.value.to_json()


def test_b6_guard_patch_file_missing_rejected(started, tmp_path):
    with pytest.raises(Problem) as ei:
        _resolve(started, {"test_ids": [GUARD_ID], "patch_file": str(tmp_path / "absent.patch")})
    assert ei.value.code == "PROOF_SPEC_INVALID", ei.value.to_json()


def test_b6_guard_patch_file_oversize_rejected(started, tmp_path):
    big = tmp_path / "big.patch"
    big.write_bytes(b"x" * (P.PATCH_FILE_MAX_BYTES + 1))
    with pytest.raises(Problem) as ei:
        _resolve(started, {"test_ids": [GUARD_ID], "patch_file": str(big)})
    assert ei.value.code == "PROOF_SPEC_INVALID", ei.value.to_json()


# ---------------------------------------------------------------- 守卫（不列入 proof group）


def test_guard_b6_guard_without_patch_kept_as_is(started):
    """guard 既没有 patch_file 也没有 patch：原样保留（首轮盲写允许省略）。"""
    out, _ = _resolve(started, {"test_ids": [GUARD_ID]})
    assert out["groups"][0]["guards"][0] == {"test_ids": [GUARD_ID]}


def test_guard_b6_mutation_group_patch_file_unchanged(started, tmp_path):
    """不变量：mutation 组的 patch_file 处理行为不变（换成了 patch）。"""
    pf = write_patch_file(GUARD_PATCH, directory=tmp_path, name="mutation.patch")
    spec = {"groups": [{"kind": "mutation", "behavior_ids": ["B1"], "test_ids": [GROUP_ID],
                        "timeout": 60, "patch_file": str(pf)}]}
    lg = L.load(started["ledger"])
    out = P.resolve_patch_files(spec, lg, started["repo"].root)
    assert out["groups"][0]["patch"] == GUARD_PATCH
    assert "patch_file" not in out["groups"][0]