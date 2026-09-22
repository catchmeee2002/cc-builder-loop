"""B2: CLAUDE.md 的 `## Commands` 一节同步 pytest-xdist -n 16 并行测试命令。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLAUDE_MD = ROOT / "CLAUDE.md"

NEW_TEST_CMD = "python3 -m pytest -p no:html -p no:cacheprovider -q -n 16 tests"
OLD_TEST_CMD = "python3 -m pytest -p no:html -p no:cacheprovider -q tests"


def _commands_section() -> str:
    text = CLAUDE_MD.read_text(encoding="utf-8")
    start = text.index("## Commands")
    rest = text[start + len("## Commands"):]
    end_marker = "\n## "
    end = rest.find(end_marker)
    section = rest if end == -1 else rest[:end]
    return section


def test_b2_commands_section_has_xdist_test_command():
    section = _commands_section()
    assert NEW_TEST_CMD in section


def test_b2_commands_section_mentions_pytest_xdist():
    section = _commands_section()
    assert "pytest-xdist" in section


def test_b2_boundary_old_non_parallel_full_command_removed():
    section = _commands_section()
    assert OLD_TEST_CMD not in section


def test_b2_invariant_install_and_doctor_commands_kept():
    section = _commands_section()
    assert "./install.sh" in section
    assert "bl doctor" in section
