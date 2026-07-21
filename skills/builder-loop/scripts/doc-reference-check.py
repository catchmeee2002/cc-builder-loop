#!/usr/bin/env python3
"""Find documentation references invalidated by removed or moved definitions."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


EXCLUDED_DIRS = {
    ".git",
    ".claude",
    ".builder-loop",
    ".venv",
    "venv",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "target",
    ".tox",
    "__pycache__",
}
HISTORICAL_DOCS = {"CHANGELOG.md", "improvements.md"}
DEFINITION_PATTERNS = [
    ("def", re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")),
    ("class", re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b")),
    (
        "function",
        re.compile(r"^\s*function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("),
    ),
    (
        "func",
        re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*\("),
    ),
    (
        "fn",
        re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("),
    ),
    ("shell", re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{")),
]


def maintained_docs(project_root: Path) -> list[str]:
    result: list[str] = []
    for root, dirs, files in os.walk(project_root):
        dirs[:] = [directory for directory in dirs if directory not in EXCLUDED_DIRS]
        root_path = Path(root)
        for name in files:
            if not name.endswith(".md") or name in HISTORICAL_DOCS:
                continue
            full = root_path / name
            if full.is_symlink():
                continue
            result.append(full.relative_to(project_root).as_posix())
    return sorted(result)


def definition(text: str) -> tuple[str, str] | None:
    for kind, pattern in DEFINITION_PATTERNS:
        match = pattern.match(text)
        if match:
            return kind, match.group(1)
    return None


def changed_definitions(project_root: Path, diff_base: str) -> list[dict[str, object]]:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(project_root),
            "diff",
            "--no-ext-diff",
            "--unified=0",
            diff_base,
            "--",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "git diff failed")

    removed: list[dict[str, str]] = []
    added: list[dict[str, str]] = []
    old_file: str | None = None
    new_file: str | None = None
    for line in completed.stdout.splitlines():
        if line.startswith("--- "):
            value = line[4:].split("\t", 1)[0]
            old_file = None if value == "/dev/null" else re.sub(r"^a/", "", value)
            continue
        if line.startswith("+++ "):
            value = line[4:].split("\t", 1)[0]
            new_file = None if value == "/dev/null" else re.sub(r"^b/", "", value)
            continue
        if line.startswith("-") and not line.startswith("---") and old_file:
            found = definition(line[1:])
            if found:
                removed.append({"kind": found[0], "symbol": found[1], "path": old_file})
        elif line.startswith("+") and not line.startswith("+++") and new_file:
            found = definition(line[1:])
            if found:
                added.append({"kind": found[0], "symbol": found[1], "path": new_file})

    added_by_symbol: dict[tuple[str, str], set[str]] = {}
    for item in added:
        added_by_symbol.setdefault((item["kind"], item["symbol"]), set()).add(
            item["path"]
        )

    result: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in removed:
        key = (item["kind"], item["symbol"], item["path"])
        if key in seen:
            continue
        seen.add(key)
        destinations = sorted(added_by_symbol.get((item["kind"], item["symbol"]), set()))
        if item["path"] in destinations:
            continue
        result.append(
            {
                **item,
                "change": "moved" if destinations else "removed",
                "new_paths": destinations,
            }
        )
    return result


def scan(project_root: Path, diff_base: str) -> dict[str, object]:
    documents = maintained_docs(project_root)
    doc_lines: dict[str, list[str]] = {}
    for document in documents:
        try:
            doc_lines[document] = (project_root / document).read_text(
                encoding="utf-8"
            ).splitlines()
        except (OSError, UnicodeError):
            continue

    broken: list[dict[str, object]] = []
    semantic: list[dict[str, str]] = []
    definitions = changed_definitions(project_root, diff_base)
    for item in definitions:
        symbol = str(item["symbol"])
        symbol_pattern = re.compile(
            r"(?<![A-Za-z0-9_])" + re.escape(symbol) + r"(?![A-Za-z0-9_])"
        )
        for document, lines in doc_lines.items():
            for line_number, line in enumerate(lines, 1):
                if not symbol_pattern.search(line):
                    continue
                if str(item["path"]) in line:
                    broken.append(
                        {
                            "file": document,
                            "line": line_number,
                            "symbol": symbol,
                            "old_path": item["path"],
                            "new_paths": item["new_paths"],
                            "change": item["change"],
                        }
                    )
                else:
                    destinations = ", ".join(item["new_paths"]) or "已删除或重命名"
                    candidate = {
                        "file": document,
                        "question": (
                            f"{symbol} 已不再定义于 {item['path']}（候选位置：{destinations}），"
                            "检查该符号引用是否仍准确"
                        ),
                    }
                    if candidate not in semantic:
                        semantic.append(candidate)
    return {
        "documents": documents,
        "broken_symbol_references": broken,
        "semantic_checks": semantic,
    }


def main() -> int:
    if len(sys.argv) not in {2, 3}:
        print("usage: doc-reference-check.py <project_root> [diff_base]", file=sys.stderr)
        return 2
    project_root = Path(sys.argv[1]).expanduser().resolve()
    diff_base = sys.argv[2] if len(sys.argv) == 3 else "HEAD"
    try:
        result = scan(project_root, diff_base)
    except (OSError, RuntimeError) as exc:
        print(f"doc reference scan failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
