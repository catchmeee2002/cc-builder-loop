"""B1/B2/B3：contract 字段定义收口为 contract.py 的一张字段表。

B1：`validate_contract` 在入口拒绝字段表之外的键（details.path 点路径、details.suggestion 近似名），
   经 `parse_contract_text` 的路径同样拒绝；既有错误码与消息不变。起点上未知键被静默放行 → baseline-red。
B2：新增 `bl contract fields`（无必填参数）输出字段表 JSON。新接口，proof 用 mutation。
B3：`contract.field_table()` 的形状 / readers / source / description，并与 tester / reviewer 的 brief
   渲染对账（brief.py 本批不改，标记值由测试自带、不从被测函数回读）。新接口，proof 用 mutation。

断言文本前先对两侧做同样归一化：去掉反引号与 * 强调标记、连续空白归一为一个空格。
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys

import pytest

from builder_loop import brief
from builder_loop import contract
from builder_loop import ledger as L
from builder_loop.errors import Problem
from conftest import CONTRACT, ROOT, contract_with, write_plan

# ------------------------------------------------------------------ 最小合法 contract

MINIMAL = {
    "schema": "builder-loop/contract@1",
    "mission": {
        "revision": 1,
        "slug": "add-mul",
        "objective": "Add mul()",
        "behaviors": [{"id": "B1", "given": "two ints", "when": "mul(a,b)", "then": "returns product"}],
    },
    "authority": {"builder_write": ["src/**"]},
    "assurance": {"required": ["machine"]},
}


def _minimal() -> dict:
    return copy.deepcopy(MINIMAL)


def _run_bl(repo, *args: str) -> subprocess.CompletedProcess:
    """真实 CLI 进程：`ok` 字段由 cli.dispatch 统一补全，main 只负责写 stdout。"""
    env = os.environ.copy()
    runtime = str(ROOT / "runtime")
    env["PYTHONPATH"] = runtime + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return subprocess.run(
        [sys.executable, "-m", "builder_loop", *args],
        cwd=str(repo.root), capture_output=True, text=True, env=env,
    )


def _fields_out(repo) -> dict:
    proc = _run_bl(repo, "contract", "fields")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _field_table_by_path() -> dict[str, dict]:
    table = contract.field_table()
    assert isinstance(table, list) and table
    out = {}
    for entry in table:
        assert isinstance(entry, dict)
        out[entry["path"]] = entry
    return out


# ==================================================================== B1（proof：baseline-red）


def test_b1_rejects_unknown_top_level_field_without_suggestion():
    c = _minimal()
    c["foo"] = 1
    with pytest.raises(Problem) as ei:
        contract.validate_contract(c)
    exc = ei.value
    assert exc.code == "CONTRACT_INVALID"
    assert exc.details.get("path") == "foo"
    assert "foo" in exc.message
    # 顶层 foo 没有近似合法名：无建议（message 的具体措辞未冻结，只断言不含建议片段与建议路径）
    assert exc.details.get("suggestion") is None
    assert "建议" not in exc.message


def test_b1_rejects_unknown_mission_field_with_suggestion():
    c = _minimal()
    c["mission"]["interface"] = ["src/x.py::fn"]
    with pytest.raises(Problem) as ei:
        contract.validate_contract(c)
    exc = ei.value
    assert exc.code == "CONTRACT_INVALID"
    assert exc.details.get("path") == "mission.interface"
    assert exc.details.get("suggestion") == "mission.interfaces"
    assert "mission.interface" in exc.message
    assert "mission.interfaces" in exc.message


def test_b1_rejects_unknown_assurance_field_with_suggestion():
    c = _minimal()
    c["assurance"]["reviewFocus"] = ["candidate diff"]
    with pytest.raises(Problem) as ei:
        contract.validate_contract(c)
    exc = ei.value
    assert exc.code == "CONTRACT_INVALID"
    assert exc.details.get("path") == "assurance.reviewFocus"
    assert exc.details.get("suggestion") == "assurance.review_focus"
    assert "assurance.reviewFocus" in exc.message
    assert "assurance.review_focus" in exc.message


def test_b1_rejects_unknown_behavior_field_with_suggestion():
    c = _minimal()
    c["mission"]["behaviors"][0]["boundary"] = ["a or b is 0"]
    with pytest.raises(Problem) as ei:
        contract.validate_contract(c)
    exc = ei.value
    assert exc.code == "CONTRACT_INVALID"
    assert exc.details.get("path") == "mission.behaviors[0].boundary"
    assert exc.details.get("suggestion") == "mission.behaviors[0].boundaries"
    assert "mission.behaviors[0].boundary" in exc.message
    assert "mission.behaviors[0].boundaries" in exc.message


def test_b1_unknown_key_and_type_error_is_never_silently_accepted():
    """边界：同一份 contract 同时含未知键与类型错误时，两种错误至少报其一，不得放行。"""
    c = _minimal()
    c["foo"] = 1
    c["mission"]["revision"] = "one"
    with pytest.raises(Problem) as ei:
        contract.validate_contract(c)
    assert ei.value.code == "CONTRACT_INVALID"


def test_b1_parse_contract_text_rejects_unknown_field(repo):
    bad = _minimal()
    bad["assurance"]["reviewFocus"] = ["candidate diff"]
    plan = write_plan(repo.root, bad)
    with pytest.raises(Problem) as ei:
        contract.parse_contract_text(plan.read_text(encoding="utf-8"))
    exc = ei.value
    assert exc.code == "CONTRACT_INVALID"
    assert exc.details.get("path") == "assurance.reviewFocus"


# ---------------------------------------------------------------- B1（守卫，不列入 proof group）


def test_b1_frozen_source_keys_are_accepted():
    """字段表里 source 为 frozen 的键出现在 contract 里仍被接受。"""
    c = _minimal()
    c["authority"]["control_basenames"] = ["conftest.py", "loop.yml"]
    c["assurance"]["max_iterations"] = 3
    c["assurance"]["proof_runner"] = {"framework": "pytest", "cmd": "python3 -m pytest -p no:html"}
    c["assurance"]["evidence_neutral_paths"] = ["CHANGELOG.md"]
    c["assurance"]["machine_commands"] = [{"stage": "test", "cmd": "python3 -m pytest", "timeout": 60}]
    assert contract.validate_contract(c) is None


def test_b1_internal_keys_of_mock_strategy_and_proof_runner_are_accepted():
    """mock_strategy 与 proof_runner 的内部键不被当成未知字段。"""
    c = _minimal()
    c["mission"]["mock_strategy"] = {"db": "sqlite in-memory", "clock": {"now": "fixed"}}
    c["assurance"]["proof_runner"] = {
        "framework": "pytest",
        "cmd": "python3 -m pytest -p no:html",
        "timeout": 120,
        "env": {"PYTHONPATH": "."},
    }
    assert contract.validate_contract(c) is None


def test_b1_valid_contract_with_all_skill_template_fields_returns_none():
    """合法 contract（含 SKILL 模板里出现的全部字段）校验通过且返回 None。"""
    c = {
        "schema": "builder-loop/contract@1",
        "mission": {
            "revision": 1,
            "slug": "add-mul",
            "objective": "Add mul()",
            "behaviors": [{
                "id": "B1",
                "given": "two ints",
                "when": "mul(a,b)",
                "then": "returns product",
                "boundaries": ["a or b is 0"],
                "invariants": ["add() unchanged"],
            }],
            "interfaces": ["src/x.py::mul(a:int, b:int) -> int"],
            "mock_strategy": {"db": "sqlite in-memory"},
            "acceptance_cases": [],
            "trust_boundaries": ["do not touch src/y.py"],
        },
        "authority": {
            "builder_write": ["src/**"],
            "tester_write": ["tests/**"],
            "protected_paths": [".claude/loop.yml"],
        },
        "assurance": {
            "required": ["machine", "tester", "proof", "reviewer"],
            "review_focus": ["src/foo.py"],
        },
    }
    assert contract.validate_contract(c) is None


def test_b1_invariant_revision_error_unchanged():
    with pytest.raises(Problem) as ei:
        contract.validate_contract(contract_with(**{"mission.revision": 0}))
    assert ei.value.code == "CONTRACT_INVALID"
    assert ei.value.message == "mission.revision 必须 ≥ 1"


def test_b1_invariant_slug_error_unchanged():
    with pytest.raises(Problem) as ei:
        contract.validate_contract(contract_with(**{"mission.slug": "Bad Slug"}))
    assert ei.value.code == "CONTRACT_INVALID"
    assert ei.value.message == "mission.slug 必须是小写字母/数字/连字符，2–49 字符"
    assert ei.value.details.get("slug") == "Bad Slug"


def test_b1_invariant_empty_behaviors_error_unchanged():
    with pytest.raises(Problem) as ei:
        contract.validate_contract(contract_with(**{"mission.behaviors": []}))
    assert ei.value.code == "CONTRACT_INVALID"
    assert ei.value.message == "mission.behaviors 不能为空"


def test_b1_invariant_builder_write_swallowing_tester_write_error_unchanged():
    with pytest.raises(Problem) as ei:
        contract.validate_contract(contract_with(**{"authority.builder_write": ["tests/evil.py"]}))
    assert ei.value.code == "CONTRACT_INVALID"
    assert ei.value.message == (
        "authority.builder_write 的这些条目落在 tester_write 之内：这些路径归 tester，builder 改不了。"
        "需要它们跟着实现变，就在对应 behavior 里写明要改成什么，由 tester 来改"
    )


# ==================================================================== B2（proof：mutation）

FIELD_KEYS = {"path", "type", "readers", "source", "description"}
REQUIRED_PATHS = (
    "mission.behaviors[].boundaries",
    "mission.interfaces",
    "assurance.review_focus",
    "authority.builder_write",
    "assurance.proof_runner",
)


def test_b2_fields_cli_shape(repo):
    out = _fields_out(repo)
    assert out["ok"] is True
    fields = out["fields"]
    assert isinstance(fields, list) and fields
    for entry in fields:
        assert set(entry) == FIELD_KEYS, entry
        assert isinstance(entry["path"], str) and entry["path"]
        assert isinstance(entry["description"], str) and entry["description"].strip()
        assert isinstance(entry["readers"], list)
        assert set(entry["readers"]) <= {"tester", "reviewer", "runtime"}, entry
        assert entry["source"] in ("planner", "frozen"), entry
    paths = [entry["path"] for entry in fields]
    assert len(paths) == len(set(paths))


def test_b2_fields_covers_known_paths(repo):
    paths = {entry["path"] for entry in _fields_out(repo)["fields"]}
    for path in REQUIRED_PATHS:
        assert path in paths, f"{path} 不在 bl contract fields 的输出里：{sorted(paths)}"


def test_b2_fields_source_values(repo):
    table = {entry["path"]: entry for entry in _fields_out(repo)["fields"]}
    assert table["assurance.proof_runner"]["source"] == "frozen"
    assert table["authority.control_basenames"]["source"] == "frozen"
    assert table["mission.interfaces"]["source"] == "planner"
    assert table["assurance.review_focus"]["source"] == "planner"


def test_b2_fields_paths_equal_field_table(repo):
    out = _fields_out(repo)
    assert {entry["path"] for entry in out["fields"]} == {entry["path"] for entry in contract.field_table()}


def test_b2_fields_does_not_read_plan(repo):
    """边界：命令不读取 plan 文件。"""
    (repo.root / "plan.md").unlink()
    out = _fields_out(repo)
    assert out["ok"] is True
    assert out["fields"]


def test_b2_fields_needs_no_locators(repo):
    """边界：命令不要求 --plan / --session / --repo 之类参数。"""
    proc = _run_bl(repo, "contract", "fields")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["ok"] is True
    assert out["fields"]


# ---------------------------------------------------------------- B2（守卫，不列入 proof group）


def test_b2_invariant_contract_validate_output_unchanged(repo, cli):
    """不变量：bl contract validate --plan 对合法 plan 的输出与改动前一致。"""
    out = cli("contract", "validate", "--plan", str(repo.root / "plan.md"))
    assert out["valid"] is True
    assert out["slug"] == "add-mul"
    assert out["behaviors"] == ["B1"]
    assert out["required"] == ["machine", "tester", "proof", "reviewer"]
    assert set(out["digests"]) == {"mission", "authority", "assurance"}


# ==================================================================== B3（proof：mutation）

MARK_INTERFACES = "MARK-INTERFACES-5f3a"
MARK_TRUST = "MARK-TRUST-5f3b"
MARK_MOCK_KEY = "MARK-MOCK-KEY-5f3c"
MARK_MOCK_VALUE = "MARK-MOCK-VALUE-5f3d"
MARK_ACCEPTANCE = "MARK-ACCEPTANCE-5f3e"
MARK_BOUNDARIES = "MARK-BOUNDARIES-5f3f"
MARK_INVARIANTS = "MARK-INVARIANTS-5f40"
MARK_REVIEW_FOCUS = "MARK-REVIEW-FOCUS-5f41"

# 字段点路径 → 测试自带的唯一标记值（不从 field_table 回读标记值）
MARKERS = {
    "mission.interfaces": MARK_INTERFACES,
    "mission.trust_boundaries": MARK_TRUST,
    "mission.mock_strategy": MARK_MOCK_KEY,
    "mission.behaviors[].boundaries": MARK_BOUNDARIES,
    "mission.behaviors[].invariants": MARK_INVARIANTS,
    "mission.acceptance_cases": MARK_ACCEPTANCE,
    "assurance.review_focus": MARK_REVIEW_FOCUS,
}


def _marked_contract() -> dict:
    c = copy.deepcopy(CONTRACT)
    c["mission"]["interfaces"] = [MARK_INTERFACES]
    c["mission"]["trust_boundaries"] = [MARK_TRUST]
    c["mission"]["mock_strategy"] = {MARK_MOCK_KEY: MARK_MOCK_VALUE}
    c["mission"]["acceptance_cases"] = [MARK_ACCEPTANCE]
    c["mission"]["behaviors"][0]["boundaries"] = [MARK_BOUNDARIES]
    c["mission"]["behaviors"][0]["invariants"] = [MARK_INVARIANTS]
    c["assurance"]["review_focus"] = [MARK_REVIEW_FOCUS]
    return c


@pytest.fixture
def marked_started(repo, cli):
    """与 conftest.start 同一条路径：写入带标记的 plan 并起 run，供 brief 渲染对账。"""
    write_plan(repo.root, _marked_contract(), "marked-plan.md")
    out = cli("start", "--plan", str(repo.root / "marked-plan.md"), "--session", "S1")
    return {"repo": repo, "ledger": out["ledger"]}


def _render(marked_started, role: str) -> str:
    """与 tests/test_planner_plan_body_narrowing.py::_render 同一做法。"""
    lg = L.load(marked_started["ledger"])
    return brief.render(brief.build(lg, marked_started["repo"].root, role))


def test_b3_field_table_shape():
    fields = contract.field_table()
    assert isinstance(fields, list) and fields
    for entry in fields:
        assert isinstance(entry, dict)
        assert set(entry) == FIELD_KEYS, entry
        assert isinstance(entry["path"], str) and entry["path"]
        assert isinstance(entry["description"], str) and entry["description"].strip()
        assert isinstance(entry["readers"], list)
        assert set(entry["readers"]) <= {"tester", "reviewer", "runtime"}, entry
        assert entry["source"] in ("planner", "frozen"), entry


def test_b3_review_focus_readers_are_reviewer_only():
    assert _field_table_by_path()["assurance.review_focus"]["readers"] == ["reviewer"]


def test_b3_tester_readable_fields_include_tester():
    table = _field_table_by_path()
    for path in (
        "mission.interfaces",
        "mission.trust_boundaries",
        "mission.mock_strategy",
        "mission.behaviors[].boundaries",
        "mission.behaviors[].invariants",
    ):
        assert "tester" in table[path]["readers"], f"{path} 的 readers 不含 tester：{table[path]}"


def test_b3_acceptance_cases_has_no_readers():
    assert _field_table_by_path()["mission.acceptance_cases"]["readers"] == []


def test_b3_review_focus_description_names_reviewer_only():
    desc = _field_table_by_path()["assurance.review_focus"]["description"]
    assert "reviewer" in desc
    assert "tester 的" not in desc


def test_b3_field_table_readers_match_rendered_briefs(marked_started):
    """对账：readers 含 tester 的字段，其标记出现在 tester brief；review_focus 的标记不在 tester
    brief 中（出现次数恰好为 0）且出现在 reviewer brief 中。"""
    table = _field_table_by_path()
    for path in MARKERS:
        assert path in table, f"{path} 不在字段表里"

    tester_text = _render(marked_started, "tester")
    for path, marker in MARKERS.items():
        if "tester" in table[path]["readers"]:
            assert marker in tester_text, f"{path} 的 readers 含 tester，标记却不在 tester brief：{path}"
        else:
            assert tester_text.count(marker) == 0, f"{path} 的 readers 不含 tester，标记却出现在 tester brief：{path}"

    reviewer_text = _render(marked_started, "reviewer")
    assert MARK_REVIEW_FOCUS in reviewer_text