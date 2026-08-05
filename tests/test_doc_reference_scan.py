from __future__ import annotations

import unittest
from pathlib import Path

from harness import cleanup_repo, commit_all, git, init_repo
from runtime.codex_builder_loop.assurance_v4.doc_references import (
    DocReferenceScanError,
    scan_repository,
)


class DocReferenceScanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repos: list[Path] = []

    def tearDown(self) -> None:
        for repo in self.repos:
            cleanup_repo(repo)

    def repo(self, files: dict[str, str]) -> tuple[Path, str]:
        repo = init_repo(files)
        self.repos.append(repo)
        return repo, git(repo, "rev-parse", "HEAD")

    @staticmethod
    def write(repo: Path, path: str, value: str) -> None:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value, encoding="utf-8")

    def test_qualified_pointer_move_is_a_mechanical_finding(self) -> None:
        languages = [
            (
                "python",
                "src/old.py",
                "src/new.py",
                "move_value",
                "def move_value(value):\n    return value\n",
            ),
            (
                "javascript",
                "src/old.js",
                "src/new.js",
                "moveValue",
                "export function moveValue(value) { return value; }\n",
            ),
            (
                "go",
                "src/old.go",
                "src/new.go",
                "MoveValue",
                "package source\n\nfunc MoveValue(value int) int { return value }\n",
            ),
            (
                "rust",
                "src/old.rs",
                "src/new.rs",
                "move_value",
                "pub fn move_value(value: i32) -> i32 { value }\n",
            ),
            (
                "shell",
                "scripts/old.sh",
                "scripts/new.sh",
                "move_value",
                "move_value() {\n  printf '%s\\n' \"$1\"\n}\n",
            ),
        ]
        for language, old_path, new_path, symbol, definition in languages:
            with self.subTest(language=language):
                repo, base = self.repo(
                    {
                        old_path: definition,
                        "docs/architecture.md": (
                            f"# Architecture\n\nUse `{old_path}::{symbol}` for conversion.\n"
                        ),
                    }
                )
                (repo / old_path).unlink()
                self.write(repo, new_path, definition)
                candidate = commit_all(repo, "move definition")
                self.write(
                    repo,
                    "docs/architecture.md",
                    f"# Architecture\n\nUse `{new_path}::{symbol}` for conversion.\n",
                )

                result = scan_repository(repo, base, candidate)

                self.assertEqual(result["base_head"], base)
                self.assertEqual(result["candidate_head"], candidate)
                self.assertEqual(
                    result["broken_references"],
                    [
                        {
                            "file": "docs/architecture.md",
                            "line": 3,
                            "symbol": symbol,
                            "old_path": old_path,
                            "new_paths": [new_path],
                            "change": "moved",
                        }
                    ],
                )

    def test_updated_pointer_signature_change_and_history_are_clean(self) -> None:
        with self.subTest(case="updated pointer"):
            repo, base = self.repo(
                {
                    "src/old.py": "def move_value(value):\n    return value\n",
                    "docs/architecture.md": (
                        "# Architecture\n\nUse `src/old.py::move_value`.\n"
                    ),
                }
            )
            (repo / "src/old.py").unlink()
            self.write(repo, "src/new.py", "def move_value(value):\n    return value\n")
            self.write(
                repo,
                "docs/architecture.md",
                "# Architecture\n\nUse `src/new.py::move_value`.\n",
            )
            candidate = commit_all(repo, "move definition and pointer")
            result = scan_repository(repo, base, candidate)
            self.assertEqual(result["broken_references"], [])
            self.assertEqual(result["semantic_checks"], [])

        with self.subTest(case="same-file signature"):
            repo, base = self.repo(
                {
                    "src/value.py": "def move_value(value):\n    return value\n",
                    "docs/architecture.md": (
                        "# Architecture\n\nUse `src/value.py::move_value`.\n"
                    ),
                }
            )
            self.write(
                repo,
                "src/value.py",
                "def move_value(value, default=None):\n    return value or default\n",
            )
            candidate = commit_all(repo, "change signature")
            result = scan_repository(repo, base, candidate)
            self.assertEqual(result["changed_definitions"], [])
            self.assertEqual(result["broken_references"], [])
            self.assertEqual(result["semantic_checks"], [])

        with self.subTest(case="history containers"):
            repo, base = self.repo(
                {
                    "src/old.py": "def move_value(value):\n    return value\n",
                    "CHANGELOG.md": "# Changelog\n\nUsed `src/old.py::move_value`.\n",
                    "docs/improvements.md": (
                        "# Improvements\n\nReplace `src/old.py::move_value`.\n"
                    ),
                }
            )
            (repo / "src/old.py").unlink()
            self.write(repo, "src/new.py", "def move_value(value):\n    return value\n")
            candidate = commit_all(repo, "move historical definition")
            result = scan_repository(repo, base, candidate)
            self.assertEqual(result["documents"], ["README.md"])
            self.assertEqual(result["broken_references"], [])
            self.assertEqual(result["semantic_checks"], [])

    def test_symbol_only_reference_is_semantic_not_mechanical(self) -> None:
        repo, base = self.repo(
            {
                "src/old.py": "def move_value(value):\n    return value\n",
                "docs/architecture.md": "# Architecture\n\nReuse `move_value`.\n",
            }
        )
        (repo / "src/old.py").unlink()
        self.write(repo, "src/new.py", "def move_value(value):\n    return value\n")
        candidate = commit_all(repo, "move definition")

        result = scan_repository(repo, base, candidate)

        self.assertEqual(result["broken_references"], [])
        self.assertEqual(len(result["semantic_checks"]), 1)
        self.assertEqual(result["semantic_checks"][0]["file"], "docs/architecture.md")
        self.assertEqual(result["semantic_checks"][0]["symbol"], "move_value")

    def test_scan_failure_is_fail_closed(self) -> None:
        repo, _base = self.repo({})
        with self.assertRaises(DocReferenceScanError) as caught:
            scan_repository(repo, "missing-reference", git(repo, "rev-parse", "HEAD"))
        self.assertEqual(caught.exception.code, "DOC_REFERENCE_COMMIT_INVALID")

    def test_deleted_or_renamed_qualified_pointer_is_mechanical(self) -> None:
        for case, replacement in (
            ("deleted", "VALUE = 1\n"),
            ("renamed", "def renamed_value(value):\n    return value\n"),
        ):
            with self.subTest(case=case):
                repo, base = self.repo(
                    {
                        "src/value.py": "def old_value(value):\n    return value\n",
                        "docs/architecture.md": (
                            "# Architecture\n\nUse `src/value.py::old_value`.\n"
                        ),
                    }
                )
                self.write(repo, "src/value.py", replacement)
                candidate = commit_all(repo, case)
                result = scan_repository(repo, base, candidate)
                self.assertEqual(len(result["broken_references"]), 1)
                self.assertEqual(
                    result["broken_references"][0]["change"], "removed_or_renamed"
                )
                self.assertEqual(result["broken_references"][0]["new_paths"], [])


if __name__ == "__main__":
    unittest.main()
