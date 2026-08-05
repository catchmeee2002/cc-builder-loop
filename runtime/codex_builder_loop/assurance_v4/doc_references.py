from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


DOC_REFERENCE_CONTRACT_VERSION = 1

EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".claude",
        ".builder-loop",
        ".venv",
        ".tox",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "target",
        "vendor",
        "venv",
    }
)
HISTORICAL_DOCS = frozenset({"changelog.md", "improvements.md"})
SOURCE_SUFFIXES = frozenset(
    {
        ".bash",
        ".cjs",
        ".go",
        ".js",
        ".jsx",
        ".mjs",
        ".py",
        ".rs",
        ".sh",
        ".ts",
        ".tsx",
    }
)


class DocReferenceScanError(RuntimeError):
    def __init__(self, message: str, *, code: str = "DOC_REFERENCE_SCAN_FAILED"):
        super().__init__(message)
        self.code = code


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise DocReferenceScanError(str(exc)) from exc
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace")
        stdout = completed.stdout.decode("utf-8", errors="replace")
        raise DocReferenceScanError(
            stderr.strip() or stdout.strip() or f"git {' '.join(args)} failed"
        )
    return completed


def _validate_commit(repo: Path, value: str, label: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise DocReferenceScanError(
            f"{label} must be a full commit id",
            code="DOC_REFERENCE_COMMIT_INVALID",
        )
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{value}^{{commit}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise DocReferenceScanError(str(exc)) from exc
    if completed.returncode != 0:
        raise DocReferenceScanError(
            f"{label} commit is unavailable: {value}",
            code="DOC_REFERENCE_COMMIT_MISSING",
        )


def _tree(repo: Path, commit: str) -> dict[str, tuple[str, str]]:
    raw = _run_git(repo, "ls-tree", "-r", "-z", commit).stdout
    result: dict[str, tuple[str, str]] = {}
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        metadata, separator, raw_path = entry.partition(b"\t")
        parts = metadata.decode("ascii", errors="strict").split()
        if not separator or len(parts) != 3 or parts[1] != "blob":
            continue
        path = raw_path.decode("utf-8", errors="surrogateescape")
        result[path] = (parts[0], parts[2])
    return result


def _blob_text(repo: Path, blob: str, *, path: str) -> str:
    raw = _run_git(repo, "cat-file", "blob", blob).stdout
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DocReferenceScanError(
            f"tracked text is not UTF-8: {path}",
            code="DOC_REFERENCE_TEXT_INVALID",
        ) from exc


def _excluded(path: str) -> bool:
    return any(part in EXCLUDED_DIRS for part in PurePosixPath(path).parts[:-1])


def _is_document(path: str, mode: str) -> bool:
    pure = PurePosixPath(path)
    return (
        mode in {"100644", "100755"}
        and pure.suffix.lower() == ".md"
        and pure.name.lower() not in HISTORICAL_DOCS
        and not _excluded(path)
    )


def _is_source(path: str, mode: str, text: str) -> bool:
    if mode not in {"100644", "100755"} or _excluded(path):
        return False
    suffix = PurePosixPath(path).suffix.lower()
    return suffix in SOURCE_SUFFIXES or (not suffix and text.startswith("#!") and "sh" in text[:128])


PYTHON_DEFINITIONS = (
    ("function", re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")),
    ("type", re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b")),
)
JAVASCRIPT_DEFINITIONS = (
    (
        "function",
        re.compile(
            r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+"
            r"([A-Za-z_$][A-Za-z0-9_$]*)\s*\("
        ),
    ),
    (
        "function",
        re.compile(
            r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*="
            r"\s*(?:(?:async\s*)?\([^)]*\)|(?:async\s+)?[A-Za-z_$][A-Za-z0-9_$]*)\s*=>"
        ),
    ),
    (
        "function",
        re.compile(
            r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*="
            r"\s*(?:async\s+)?function\b"
        ),
    ),
    ("type", re.compile(r"^\s*(?:export\s+(?:default\s+)?)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)\b")),
)
GO_DEFINITIONS = (
    (
        "function",
        re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*\("),
    ),
    ("type", re.compile(r"^\s*type\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:struct|interface)\b")),
)
RUST_DEFINITIONS = (
    (
        "function",
        re.compile(
            r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+"
            r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:<[^>]*>)?\s*\("
        ),
    ),
    (
        "type",
        re.compile(
            r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|type)\s+"
            r"([A-Za-z_][A-Za-z0-9_]*)\b"
        ),
    ),
)
SHELL_DEFINITIONS = (
    ("function", re.compile(r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*\)\s*\{")),
    ("function", re.compile(r"^\s*function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{")),
)


def _definition_patterns(path: str) -> Iterable[tuple[str, re.Pattern[str]]]:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == ".py":
        return PYTHON_DEFINITIONS
    if suffix in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}:
        return JAVASCRIPT_DEFINITIONS
    if suffix == ".go":
        return GO_DEFINITIONS
    if suffix == ".rs":
        return RUST_DEFINITIONS
    return SHELL_DEFINITIONS


def _definitions(path: str, text: str) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    patterns = _definition_patterns(path)
    for line in text.splitlines():
        for kind, pattern in patterns:
            matched = pattern.match(line)
            if matched:
                found.add((kind, matched.group(1)))
                break
    return found


def _changed_paths(repo: Path, base_head: str, candidate_head: str) -> list[str]:
    raw = _run_git(
        repo,
        "diff",
        "--name-only",
        "--no-renames",
        "-z",
        base_head,
        candidate_head,
        "--",
    ).stdout
    return sorted(
        {
            item.decode("utf-8", errors="surrogateescape")
            for item in raw.split(b"\0")
            if item
        }
    )


def _changed_definitions(
    repo: Path,
    base_tree: dict[str, tuple[str, str]],
    candidate_tree: dict[str, tuple[str, str]],
    changed_paths: list[str],
) -> list[dict[str, Any]]:
    before: dict[str, set[tuple[str, str]]] = {}
    after: dict[str, set[tuple[str, str]]] = {}
    for path in changed_paths:
        if path in base_tree:
            mode, blob = base_tree[path]
            text = _blob_text(repo, blob, path=path)
            if _is_source(path, mode, text):
                before[path] = _definitions(path, text)
        if path in candidate_tree:
            mode, blob = candidate_tree[path]
            text = _blob_text(repo, blob, path=path)
            if _is_source(path, mode, text):
                after[path] = _definitions(path, text)

    destinations: dict[tuple[str, str], set[str]] = {}
    for path, values in after.items():
        for value in values:
            destinations.setdefault(value, set()).add(path)

    changes: list[dict[str, Any]] = []
    for path in sorted(before):
        for kind, symbol in sorted(before[path]):
            if (kind, symbol) in after.get(path, set()):
                continue
            new_paths = sorted(destinations.get((kind, symbol), set()) - {path})
            changes.append(
                {
                    "definition_kind": kind,
                    "symbol": symbol,
                    "old_path": path,
                    "change": "moved" if new_paths else "removed_or_renamed",
                    "new_paths": new_paths,
                }
            )
    return changes


def scan_repository(repo_value: str | Path, base_head: str, candidate_head: str) -> dict[str, Any]:
    repo = Path(repo_value).expanduser().resolve()
    _validate_commit(repo, base_head, "base_head")
    _validate_commit(repo, candidate_head, "candidate_head")
    base_tree = _tree(repo, base_head)
    candidate_tree = _tree(repo, candidate_head)
    changed_paths = _changed_paths(repo, base_head, candidate_head)
    changes = _changed_definitions(repo, base_tree, candidate_tree, changed_paths)

    documents: list[tuple[str, list[str]]] = []
    for path in sorted(candidate_tree):
        mode, blob = candidate_tree[path]
        if not _is_document(path, mode):
            continue
        documents.append((path, _blob_text(repo, blob, path=path).splitlines()))

    broken: list[dict[str, Any]] = []
    semantic: list[dict[str, Any]] = []
    for change in changes:
        symbol = str(change["symbol"])
        old_path = str(change["old_path"])
        symbol_pattern = re.compile(
            r"(?<![A-Za-z0-9_$])" + re.escape(symbol) + r"(?![A-Za-z0-9_$])"
        )
        old_pointer = re.compile(re.escape(f"{old_path}::{symbol}"))
        new_pointers = [
            re.compile(re.escape(f"{path}::{symbol}")) for path in change["new_paths"]
        ]
        for document, lines in documents:
            for line_number, line in enumerate(lines, 1):
                if not symbol_pattern.search(line):
                    continue
                if old_pointer.search(line):
                    broken.append(
                        {
                            "file": document,
                            "line": line_number,
                            "symbol": symbol,
                            "old_path": old_path,
                            "new_paths": list(change["new_paths"]),
                            "change": change["change"],
                        }
                    )
                    continue
                if any(pattern.search(line) for pattern in new_pointers):
                    continue
                candidate = {
                    "file": document,
                    "line": line_number,
                    "symbol": symbol,
                    "old_path": old_path,
                    "new_paths": list(change["new_paths"]),
                    "question": (
                        f"{symbol} is no longer defined at {old_path}; verify whether this "
                        "symbol-only documentation reference remains accurate."
                    ),
                }
                if candidate not in semantic:
                    semantic.append(candidate)

    return {
        "contract_version": DOC_REFERENCE_CONTRACT_VERSION,
        "base_head": base_head,
        "candidate_head": candidate_head,
        "changed_paths": changed_paths,
        "changed_definitions": changes,
        "documents": [path for path, _lines in documents],
        "broken_references": broken,
        "semantic_checks": semantic,
    }
