"""B5：CLI help 与文档同步 finalize / cleanup 的新边界（worktree 留给 bl cleanup）。

行为变更了 finalize 的对外语义、扩大了 cleanup 的适用范围，所以 subcommand help、两个 SKILL、
architecture 与 CHANGELOG 都要改。字面锚句在起点上都不存在，断言在 call 阶段失败（baseline-red）。
"""

from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_LOOP = ROOT / "skills" / "builder-loop" / "SKILL.md"
SKILL_BUILDER = ROOT / "skills" / "builder" / "SKILL.md"
ARCH = ROOT / "docs" / "architecture.md"
CHANGELOG = ROOT / "CHANGELOG.md"

CLEANUP_HELP = "回收已复盘的终态 run（abandoned / finalized）的 worktree"
OLD_CLEANUP_HELP = "回收已复盘的 abandoned run 的 worktree"

FLOW_LOOP = "→ bl finalize（commit-tree + update-ref CAS 写回目标分支；worktree 与分支留给 bl cleanup）"
ROW_LOOP = "| `bl cleanup [--run R]` | 回收已复盘的终态 run（abandoned / finalized）的 worktree（clean、HEAD 未漂移、调用者不在其中才删） | 0 |"

STEP6_BUILDER = "6. finalize 不回收 worktree：复盘后在候选目录之外执行 `bl cleanup --run <run_id>` 回收。"

FLOW_ARCH = "terminal（worktree 留给 bl cleanup）"
ABANDON_ARCH = "终态 abandoned，worktree 与分支保留；复盘后 `bl cleanup` 回收 clean、HEAD 未漂移且调用者不在其中的"
RETRO_ROW_ARCH = "确定性信号派生（只读 ledger）、复盘记录校验、cleanup"


def _norm(text: str) -> str:
    """去掉 markdown 强调标记与反引号，连续空白归一为一个空格。"""
    text = text.replace("**", "").replace("__", "").replace("`", "")
    return re.sub(r"\s+", " ", text).strip()


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _help_texts() -> str:
    from builder_loop.cli import build_parser

    def _capture(argv: list[str]) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                build_parser().parse_args(argv)
            except SystemExit:
                pass
        return buf.getvalue()

    return _capture(["--help"]) + "\n" + _capture(["cleanup", "--help"])


def _section(text: str, heading: str) -> str:
    idx = text.index(heading)
    m = re.search(r"(?m)^## (\d)\.", text[idx + len(heading):])
    return text[idx:] if not m else text[idx: idx + len(heading) + m.start()]


# ---------------------------------------------------------------- given / when / then


def test_cleanup_help_text_updated():
    helptext = _norm(_help_texts())
    assert CLEANUP_HELP in helptext
    assert OLD_CLEANUP_HELP not in helptext


def test_builder_loop_skill_flow_and_command_table_updated():
    text = _norm(_read(SKILL_LOOP))
    assert text.count(_norm(FLOW_LOOP)) == 1
    assert text.count(_norm(ROW_LOOP)) == 1


def test_builder_skill_retro_step_6_added():
    text = _read(SKILL_BUILDER)
    sect = _section(text, "## 3. 复盘")
    assert _norm(STEP6_BUILDER) in _norm(sect)


def test_architecture_flow_and_abandon_row_updated():
    text = _norm(_read(ARCH))
    assert text.count(_norm(FLOW_ARCH)) == 1
    assert "删两个 worktree" not in text
    assert text.count(_norm(ABANDON_ARCH)) == 1
    # retro 模块那一行不变
    assert _norm(RETRO_ROW_ARCH) in text


def test_changelog_new_section_first():
    text = _read(CHANGELOG)
    headings = re.findall(r"(?m)^## .+$", text)
    assert headings, "CHANGELOG 至少要有一节"
    assert "#369 #390" in headings[0]