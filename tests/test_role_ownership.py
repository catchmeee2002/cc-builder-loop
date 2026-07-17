from __future__ import annotations

import unittest
from pathlib import Path

from harness import (
    assert_status,
    cleanup_repo,
    commit_all,
    finish_agent_turn,
    head,
    init_repo,
    plan_markdown,
    register_agent,
    run_cli,
    start_run,
    start_agent_turn,
    worktrees_from,
    write_plan,
)


class RoleOwnershipTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repos: list[Path] = []

    def tearDown(self) -> None:
        for repo in self.repos:
            cleanup_repo(repo)

    def start(self) -> tuple[Path, Path, Path, Path]:
        repo = init_repo()
        self.repos.append(repo)
        plan = write_plan(repo, plan_markdown(head(repo)))
        started, run_path = start_run(repo, plan)
        builder, tester = worktrees_from(started, run_path)
        return repo, builder, tester, run_path

    def violations(self, result) -> list[dict]:
        value = result.data.get("violations")
        self.assertIsInstance(value, list, result.data)
        return value

    def test_owned_writes_are_ready(self) -> None:
        _repo, builder, tester, run_path = self.start()
        (builder / "src" / "feature.py").write_text("VALUE = 1\n")
        commit_all(builder, "builder owned source")
        (tester / "tests" / "test_feature.py").write_text(
            "from src.calc import add\n\ndef test_feature():\n    assert add(2, 3) == 5\n"
        )
        commit_all(tester, "tester owned test")

        assert_status(
            run_cli("role-check", "--run", run_path, "--role", "builder"),
            "READY",
            rc=0,
        )
        assert_status(
            run_cli("role-check", "--run", run_path, "--role", "tester"),
            "READY",
            rc=0,
        )

    def test_builder_cannot_modify_tester_owned_tests(self) -> None:
        _repo, builder, _tester, run_path = self.start()
        (builder / "tests" / "test_calc.py").write_text(
            "from src.calc import add\n\ndef test_add():\n    assert add(1, 2) in (2, 3)\n"
        )
        commit_all(builder, "builder weakens tester assertion")

        result = run_cli("role-check", "--run", run_path, "--role", "builder")
        assert_status(result, "NEEDS_USER")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(
            any(v.get("path") == "tests/test_calc.py" for v in self.violations(result)),
            result.data,
        )

    def test_constant_assertion_and_assertion_deletion_need_user(self) -> None:
        for replacement in (
            "def test_add():\n    assert True\n",
            "def test_add():\n    pass\n",
        ):
            with self.subTest(replacement=replacement):
                _repo, _builder, tester, run_path = self.start()
                (tester / "tests" / "test_calc.py").write_text(replacement)
                commit_all(tester, "weaken existing test")
                result = run_cli("role-check", "--run", run_path, "--role", "tester")
                assert_status(result, "NEEDS_USER", rc=1)
                self.assertTrue(
                    any(item.get("kind") == "reward_hacking" for item in self.violations(result)),
                    result.data,
                )

        _repo, _builder, tester, run_path = self.start()
        (tester / "tests" / "test_calc.py").unlink()
        commit_all(tester, "delete existing test")
        deleted = run_cli("role-check", "--run", run_path, "--role", "tester")
        assert_status(deleted, "NEEDS_USER", rc=1)
        self.assertTrue(
            any("deleted" in str(item.get("reason", "")) for item in self.violations(deleted)),
            deleted.data,
        )

    def test_tester_cannot_modify_builder_owned_source(self) -> None:
        _repo, _builder, tester, run_path = self.start()
        (tester / "src" / "calc.py").write_text(
            "def add(a, b):\n    return 3  # tester must not repair source\n"
        )
        commit_all(tester, "tester crosses source boundary")

        result = run_cli("role-check", "--run", run_path, "--role", "tester")
        assert_status(result, "NEEDS_USER")
        self.assertTrue(
            any(v.get("path") == "src/calc.py" for v in self.violations(result)),
            result.data,
        )

    def test_reward_hacking_inside_tester_owned_path_still_needs_user(self) -> None:
        _repo, _builder, tester, run_path = self.start()
        (tester / "tests" / "test_calc.py").write_text(
            "import pytest\n"
            "from src.calc import add\n\n"
            "@pytest.mark.skip(reason='make the gate green')\n"
            "def test_add():\n"
            "    assert add(1, 2) == 3\n"
        )
        commit_all(tester, "tester adds skip marker")

        result = run_cli("role-check", "--run", run_path, "--role", "tester")
        assert_status(result, "NEEDS_USER")
        violations = self.violations(result)
        self.assertTrue(
            any(
                v.get("kind") == "reward_hacking"
                or "reward" in str(v.get("reason", "")).lower()
                or "skip" in str(v.get("reason", "")).lower()
                for v in violations
            ),
            result.data,
        )

    def test_ignored_builder_source_cannot_influence_uncommitted_evidence(self) -> None:
        repo = init_repo({".gitignore": "src/generated.py\n"})
        self.repos.append(repo)
        plan = write_plan(repo, plan_markdown(head(repo)))
        started, run_path = start_run(repo, plan)
        builder, _tester = worktrees_from(started, run_path)
        (builder / "src" / "generated.py").write_text("VALUE = 7\n")

        result = run_cli("role-check", "--run", run_path, "--role", "builder")
        assert_status(result, "NEEDS_USER", rc=1)
        self.assertTrue(
            any(
                item.get("kind") == "ignored_untracked"
                and item.get("path") == "src/generated.py"
                for item in self.violations(result)
            ),
            result.data,
        )

    def test_ignored_tester_test_cannot_be_silently_dropped(self) -> None:
        repo = init_repo({".gitignore": "tests/test_ignored.py\n"})
        self.repos.append(repo)
        plan = write_plan(repo, plan_markdown(head(repo)))
        started, run_path = start_run(repo, plan)
        _builder, tester = worktrees_from(started, run_path)
        (tester / "tests" / "test_ignored.py").write_text(
            "def test_ignored():\n    assert False\n"
        )

        result = run_cli("role-check", "--run", run_path, "--role", "tester")
        assert_status(result, "NEEDS_USER", rc=1)
        self.assertTrue(
            any(
                item.get("kind") == "ignored_untracked"
                and item.get("path") == "tests/test_ignored.py"
                for item in self.violations(result)
            ),
            result.data,
        )

    def test_tester_cannot_reduce_assertions_after_previous_integration(self) -> None:
        _repo, _builder, tester, run_path = self.start()
        tester_agent_id, tester_turn_id = start_agent_turn(run_path, "tester")
        test_path = tester / "tests" / "test_incremental.py"
        test_path.write_text(
            "def test_incremental():\n"
            "    assert 1 + 1 == 2\n"
            "    assert 2 + 2 == 4\n"
            "    assert 3 + 3 == 6\n"
        )
        commit_all(tester, "add independent assertions")
        finish_agent_turn(
            run_path,
            "tester",
            agent_id=tester_agent_id,
            turn_id=tester_turn_id,
            result="tests_ready",
        )
        assert_status(run_cli("integrate-tests", "--run", run_path), "READY", rc=0)

        test_path.write_text(
            "def test_incremental():\n"
            "    assert 1 + 1 == 2\n"
        )
        commit_all(tester, "weaken previously integrated assertions")
        result = run_cli("role-check", "--run", run_path, "--role", "tester")
        assert_status(result, "NEEDS_USER", rc=1)
        self.assertTrue(
            any("assertions were removed" in str(item.get("reason", "")) for item in self.violations(result)),
            result.data,
        )


if __name__ == "__main__":
    unittest.main()
