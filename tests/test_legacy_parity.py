from __future__ import annotations

import ast
import json
import re
import subprocess
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PARITY_PATH = ROOT / "tests" / "fixtures" / "legacy_feature_parity.json"
SECTION = re.compile(r'\s*section\s+"([^"]+)"')
ASSERTION = re.compile(r"\s*(?:assert|assert_[A-Za-z0-9_]+)\s")


def load_corpus() -> dict[str, Any]:
    value = json.loads(PARITY_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError("legacy parity corpus must be an object")
    return value


def test_method_exists(test_id: str) -> bool:
    parts = test_id.split(".")
    if len(parts) < 4 or parts[0] != "tests" or not parts[-1].startswith("test"):
        return False
    path = ROOT.joinpath(*parts[:2]).with_suffix(".py")
    if not path.is_file():
        return False
    tree = ast.parse(path.read_text(encoding="utf-8"))
    class_name, method_name = parts[-2:]
    return any(
        isinstance(node, ast.ClassDef)
        and node.name == class_name
        and any(
            isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            and child.name == method_name
            for child in node.body
        )
        for node in tree.body
    )


class LegacyParityContractTest(unittest.TestCase):
    def test_every_legacy_case_has_one_explicit_disposition(self) -> None:
        corpus = load_corpus()
        self.assertEqual(corpus.get("schema_version"), 2)
        source = corpus["source"]
        self.assertEqual(source["fixture_count"], 51)
        self.assertEqual(source["case_count"], 252)
        self.assertEqual(source["assertion_count"], 824)
        self.assertRegex(source["commit"], r"^[0-9a-f]{40}$")
        self.assertRegex(source["tree"], r"^[0-9a-f]{40}$")
        self.assertRegex(source["disposition_commit"], r"^[0-9a-f]{40}$")

        fixtures = corpus["fixtures"]
        self.assertEqual(len(fixtures), 51)
        self.assertEqual(
            len({item["fixture"] for item in fixtures}),
            len(fixtures),
        )
        case_keys: set[tuple[str, str]] = set()
        status_counts = {status: 0 for status in ("covered", "rescue", "retired")}
        assertion_count = 0
        for fixture in fixtures:
            self.assertEqual(
                set(fixture),
                {"fixture", "source_blob", "precondition_assertion_count", "cases"},
                "fixture-level disposition fallback is forbidden",
            )
            self.assertTrue(
                fixture["fixture"].startswith("test-")
                and fixture["fixture"].endswith(".sh")
            )
            self.assertRegex(fixture["source_blob"], r"^[0-9a-f]{40}$")
            self.assertGreaterEqual(fixture["precondition_assertion_count"], 0)
            assertion_count += fixture["precondition_assertion_count"]
            for case in fixture["cases"]:
                self.assertEqual(
                    set(case),
                    {
                        "id",
                        "title",
                        "source_line",
                        "assertion_count",
                        "status",
                        "replacement",
                        "rationale",
                        "test_ids",
                    },
                )
                key = (fixture["fixture"], case["id"])
                self.assertNotIn(key, case_keys)
                case_keys.add(key)
                self.assertTrue(case["title"])
                self.assertGreater(case["source_line"], 0)
                self.assertGreaterEqual(case["assertion_count"], 0)
                assertion_count += case["assertion_count"]
                status_counts[case["status"]] += 1
                self.assertTrue(case["replacement"])
                self.assertTrue(case["rationale"])
                self.assertTrue(case["test_ids"])
                for test_id in case["test_ids"]:
                    self.assertTrue(
                        test_method_exists(test_id),
                        f"legacy case references missing test method: {test_id}",
                    )
        self.assertEqual(len(case_keys), 252)
        self.assertEqual(assertion_count, 824)
        self.assertEqual(status_counts, {"covered": 138, "rescue": 42, "retired": 72})

    def test_issue_82_cases_are_rescued_by_the_v4_reference_scan(self) -> None:
        corpus = load_corpus()
        expected = {
            "test-diff-level-check.sh": {22, 23, 24, 25, 26, 27},
            "test-doc-lint.sh": {10, 11, 12, 13},
        }
        for fixture in corpus["fixtures"]:
            numbers = expected.get(fixture["fixture"])
            if numbers is None:
                continue
            matched = []
            for case in fixture["cases"]:
                match = re.search(r"Case\s+(\d+)", case["title"])
                if match and int(match.group(1)) in numbers:
                    matched.append(int(match.group(1)))
                    self.assertEqual(case["status"], "rescue")
                    self.assertIn("documentation reference scan", case["replacement"])
                    self.assertTrue(
                        all("test_doc_reference_scan" in item for item in case["test_ids"])
                    )
            self.assertEqual(set(matched), numbers)

    def test_frozen_source_inventory_matches_when_legacy_git_objects_are_available(self) -> None:
        corpus = load_corpus()
        source = corpus["source"]
        available = subprocess.run(
            ["git", "-C", str(ROOT), "cat-file", "-e", source["commit"]],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if available.returncode != 0:
            self.skipTest("legacy source commit is not present in this clone")
        tree = subprocess.check_output(
            [
                "git",
                "-C",
                str(ROOT),
                "rev-parse",
                f"{source['commit']}:{source['path_prefix']}",
            ],
            text=True,
        ).strip()
        self.assertEqual(tree, source["tree"])
        assertion_total = 0
        case_total = 0
        for fixture in corpus["fixtures"]:
            path = f"{source['path_prefix']}/{fixture['fixture']}"
            listed = subprocess.check_output(
                ["git", "-C", str(ROOT), "ls-tree", source["commit"], "--", path],
                text=True,
            ).split()
            self.assertEqual(listed[2], fixture["source_blob"])
            text = subprocess.check_output(
                ["git", "-C", str(ROOT), "show", f"{source['commit']}:{path}"],
                text=True,
            )
            section_count = sum(bool(SECTION.match(line)) for line in text.splitlines())
            assertion_count = sum(bool(ASSERTION.match(line)) for line in text.splitlines())
            self.assertEqual(section_count, len(fixture["cases"]))
            self.assertEqual(
                assertion_count,
                fixture["precondition_assertion_count"]
                + sum(item["assertion_count"] for item in fixture["cases"]),
            )
            case_total += section_count
            assertion_total += assertion_count
        self.assertEqual(case_total, 252)
        self.assertEqual(assertion_total, 824)


if __name__ == "__main__":
    unittest.main()
