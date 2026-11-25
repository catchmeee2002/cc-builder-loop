"""contract 开闭标签配对：extract_tag 在入口校验配对，孤儿/错序标签报 CONTRACT_TAG_UNBALANCED。

标签一律由 contract.CONTRACT_TAG 常量拼出，测试源码里不写整串字面量。
"""

from __future__ import annotations

import copy
import json
import re

import pytest

from builder_loop import contract as C
from builder_loop.errors import Problem
from conftest import CONTRACT


# ---------------------------------------------------------------- helpers（不要用 test 前缀命名）


def _open_tag() -> str:
    return f"<!-- {C.CONTRACT_TAG} -->"


def _close_tag() -> str:
    return f"<!-- /{C.CONTRACT_TAG} -->"


_OPEN_RE = re.compile(r"<!--\s*" + re.escape(C.CONTRACT_TAG) + r"\s*-->")
_CLOSE_RE = re.compile(r"<!--\s*/\s*" + re.escape(C.CONTRACT_TAG) + r"\s*-->")


def _tag_lines(text: str) -> tuple[list[int], list[int]]:
    """测试自己数出标签的 1 起始行号（不从被测函数回读），同一行多个标签按出现次数重复。"""
    opens: list[int] = []
    closes: list[int] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        opens.extend([lineno] * len(_OPEN_RE.findall(line)))
        closes.extend([lineno] * len(_CLOSE_RE.findall(line)))
    return opens, closes


def _expected_contract() -> dict:
    """合法单对标签时 parse_contract_text 的逐字段期望（含 validate_contract 补的缺省值）。"""
    c = copy.deepcopy(CONTRACT)
    c["mission"].setdefault("interfaces", [])
    c["mission"].setdefault("acceptance_cases", [])
    c["mission"].setdefault("trust_boundaries", [])
    c["authority"].setdefault("tester_write", [])
    c["authority"].setdefault("protected_paths", [])
    c["authority"].setdefault("target_branch", None)
    c["assurance"].setdefault("proof_kinds", list(C.PROOF_KINDS))
    c["assurance"].setdefault("review_focus", [])
    return c


def _wrap(body: str, open_tag: str | None = None, close_tag: str | None = None) -> str:
    return "plan intro\n" + (open_tag or _open_tag()) + "\n" + body + "\n" + (close_tag or _close_tag()) + "\n"


# ---------------------------------------------------------------- B1 合法 / 缺失 / 重复


def test_valid_single_pair_extracts_raw_body_and_parses():
    body = json.dumps(CONTRACT)
    text = _wrap(body)

    assert C.extract_tag(text, C.CONTRACT_TAG) == "\n" + body + "\n"
    assert C.parse_contract_text(text) == _expected_contract()


def test_two_sequential_pairs_reports_duplicate_count():
    one_pair = _wrap("{}")
    text = one_pair + one_pair

    with pytest.raises(Problem) as ei:
        C.extract_tag(text, C.CONTRACT_TAG)
    assert ei.value.code == "CONTRACT_DUPLICATE"
    assert ei.value.details["count"] == 2

    # 既有用例 test_duplicate_tag_rejected 走的也是这条路径：parse 入口同样报 DUPLICATE。
    with pytest.raises(Problem) as ei2:
        C.parse_contract_text(text)
    assert ei2.value.code == "CONTRACT_DUPLICATE"
    assert ei2.value.details["count"] == 2


def test_zero_tags_returns_none_and_parse_reports_missing():
    text = "plain plan without any contract tag\n"

    assert C.extract_tag(text, C.CONTRACT_TAG) is None
    with pytest.raises(Problem) as ei:
        C.parse_contract_text(text)
    assert ei.value.code == "CONTRACT_MISSING"


def test_valid_single_pair_tolerates_inner_whitespace():
    """注释符内侧的空白可有可无：开标签与闭标签都适用。"""
    body = json.dumps(CONTRACT)
    # 沿用现有容忍：name 与注释符内侧边缘之间可以没有空白，也可以有多处空白；
    # 闭标签里 `/` 与 name 之间的空白不属于现有容忍（基线不接受），这里不构造。
    tight_open = f"<!--{C.CONTRACT_TAG}-->"
    tight_close = f"<!--/{C.CONTRACT_TAG}-->"
    padded_open = f"<!--   {C.CONTRACT_TAG}   -->"
    padded_close = f"<!--   /{C.CONTRACT_TAG}   -->"

    for open_tag, close_tag in (
        (tight_open, tight_close),
        (padded_open, padded_close),
        (tight_open, padded_close),
        (padded_open, tight_close),
    ):
        text = _wrap(body, open_tag, close_tag)
        assert C.extract_tag(text, C.CONTRACT_TAG) == "\n" + body + "\n"
        assert C.parse_contract_text(text) == _expected_contract()


# ---------------------------------------------------------------- B2 孤儿开标签 + 合法对（行号）


def _orphan_then_pair_text(inner_body: str) -> str:
    """孤儿开标签在第 3 行，合法对的开/闭标签在第 10 / 15 行。"""
    lines = [
        "# plan",                       # 1
        "",                             # 2
        _open_tag(),                    # 3  孤儿开标签
        "",                             # 4
        "some prose",                   # 5
        "",                             # 6
        "",                             # 7
        "",                             # 8
        "",                             # 9
        _open_tag(),                    # 10 合法对开标签
        "```json",                      # 11
        inner_body,                     # 12
        "```",                          # 13
        "",                             # 14
        _close_tag(),                   # 15 合法对闭标签
    ]
    return "\n".join(lines) + "\n"


def test_orphan_open_tag_reports_unbalanced_line_numbers():
    text = _orphan_then_pair_text("{this is not valid json")
    # 行号由测试自己数出，并逐值断言与构造一致。
    assert _tag_lines(text) == ([3, 10], [15])

    with pytest.raises(Problem) as ei:
        C.parse_contract_text(text)
    assert ei.value.code == "CONTRACT_TAG_UNBALANCED"  # 不是 CONTRACT_PARSE / CONTRACT_DUPLICATE
    assert ei.value.details["open_lines"] == [3, 10]
    assert ei.value.details["close_lines"] == [15]
    assert "3" in ei.value.message and "10" in ei.value.message and "15" in ei.value.message


def test_orphan_open_tag_wins_over_wellformed_json():
    """配对校验发生在 JSON 解析之前：合法对内部的 JSON 完全合法，也仍报 UNBALANCED。"""
    text = _orphan_then_pair_text(json.dumps(CONTRACT))
    assert _tag_lines(text) == ([3, 10], [15])

    with pytest.raises(Problem) as ei:
        C.parse_contract_text(text)
    assert ei.value.code == "CONTRACT_TAG_UNBALANCED"
    assert ei.value.details["open_lines"] == [3, 10]
    assert ei.value.details["close_lines"] == [15]


def test_single_pair_with_bad_json_still_reports_parse():
    """不变量：合法单对标签 + JSON 语法错误仍报 CONTRACT_PARSE。"""
    with pytest.raises(Problem) as ei:
        C.parse_contract_text(_wrap("{not json"))
    assert ei.value.code == "CONTRACT_PARSE"


# ---------------------------------------------------------------- B3 extract_tag 的形态判定


def _text_with(*tags: str) -> str:
    return "\n".join(["# plan", *tags, ""])


def test_only_close_tag_reports_unbalanced():
    text = _text_with(_close_tag())
    assert _tag_lines(text) == ([], [2])
    with pytest.raises(Problem) as ei:
        C.extract_tag(text, C.CONTRACT_TAG)
    assert ei.value.code == "CONTRACT_TAG_UNBALANCED"
    assert ei.value.details["open_lines"] == []
    assert ei.value.details["close_lines"] == [2]


def test_only_open_tag_reports_unbalanced_not_none_not_missing():
    text = _text_with(_open_tag())
    assert _tag_lines(text) == ([2], [])
    with pytest.raises(Problem) as ei:
        C.extract_tag(text, C.CONTRACT_TAG)
    assert ei.value.code == "CONTRACT_TAG_UNBALANCED"
    assert ei.value.details["open_lines"] == [2]
    assert ei.value.details["close_lines"] == []


def test_close_before_open_reports_unbalanced():
    text = _text_with("intro", _close_tag(), "", _open_tag())
    assert _tag_lines(text) == ([5], [3])
    with pytest.raises(Problem) as ei:
        C.extract_tag(text, C.CONTRACT_TAG)
    assert ei.value.code == "CONTRACT_TAG_UNBALANCED"
    assert ei.value.details["open_lines"] == [5]
    assert ei.value.details["close_lines"] == [3]


def test_three_open_two_close_reports_unbalanced():
    text = _text_with(_open_tag(), _open_tag(), _close_tag(), _open_tag(), _close_tag())
    assert _tag_lines(text) == ([2, 3, 5], [4, 6])
    with pytest.raises(Problem) as ei:
        C.extract_tag(text, C.CONTRACT_TAG)
    assert ei.value.code == "CONTRACT_TAG_UNBALANCED"
    assert ei.value.details["open_lines"] == [2, 3, 5]
    assert ei.value.details["close_lines"] == [4, 6]


def test_nested_order_equal_counts_reports_unbalanced():
    """开闭数相等但顺序不成对（开 开 闭 闭）同样按不成对处理。"""
    text = _text_with(_open_tag(), _open_tag(), _close_tag(), _close_tag())
    assert _tag_lines(text) == ([2, 3], [4, 5])
    with pytest.raises(Problem) as ei:
        C.extract_tag(text, C.CONTRACT_TAG)
    assert ei.value.code == "CONTRACT_TAG_UNBALANCED"
    assert ei.value.details["open_lines"] == [2, 3]
    assert ei.value.details["close_lines"] == [4, 5]


def test_multiple_tags_on_one_line_repeat_line_numbers():
    """同一行多个标签：行号按出现次数重复列出，不去重。"""
    text = _text_with(_open_tag() + _open_tag() + _close_tag())
    assert _tag_lines(text) == ([2, 2], [2])
    with pytest.raises(Problem) as ei:
        C.extract_tag(text, C.CONTRACT_TAG)
    assert ei.value.code == "CONTRACT_TAG_UNBALANCED"
    assert ei.value.details["open_lines"] == [2, 2]
    assert ei.value.details["close_lines"] == [2]


# ---------------------------------------------------------------- B4 CLI contract validate


def _plan_file_text(contract: dict, *, orphan: bool) -> str:
    lines = ["# plan", ""]
    if orphan:
        lines += [_open_tag(), ""]
    lines += [
        "body",
        _open_tag(),
        "```json",
        json.dumps(contract),
        "```",
        _close_tag(),
        "",
    ]
    return "\n".join(lines) + "\n"


def test_cli_validate_reports_unbalanced_tag(repo, cli):
    plan = repo.root / "unbalanced-plan.md"
    plan.write_text(_plan_file_text(CONTRACT, orphan=True), encoding="utf-8")

    err = cli("contract", "validate", "--plan", str(plan), expect=2)
    assert err["ok"] is False
    assert err["code"] == "CONTRACT_TAG_UNBALANCED"

    # 删掉孤立开标签后，同一命令判定合法。
    plan.write_text(_plan_file_text(CONTRACT, orphan=False), encoding="utf-8")
    out = cli("contract", "validate", "--plan", str(plan))
    assert out["valid"] is True