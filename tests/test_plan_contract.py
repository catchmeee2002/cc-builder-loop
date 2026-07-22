from __future__ import annotations

import json
import sys
import unittest

from harness import (
    CLI,
    assert_status,
    cleanup_repo,
    commit_all,
    git,
    head,
    init_repo,
    l1_plan_markdown,
    plan_markdown,
    run_cli,
    run_process,
    write_plan,
)


class PlanContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo()
        self.spec_head = head(self.repo)

    def tearDown(self) -> None:
        cleanup_repo(self.repo)

    def test_valid_parallel_plan_from_path_and_stdin(self) -> None:
        text = plan_markdown(self.spec_head, parallel_ready=True, include_e2e=True)
        plan = write_plan(self.repo, text)

        by_path = run_cli("plan-validate", "--repo", self.repo, "--plan", plan)
        assert_status(by_path, "READY", rc=0)
        self.assertEqual(by_path.data.get("spec_head"), self.spec_head)
        self.assertIs(by_path.data.get("parallel_ready"), True)
        self.assertEqual(
            by_path.data.get("effective_verification_source"),
            "plan:test_context.runner",
        )

        by_stdin = run_cli("plan-validate", "--repo", self.repo, input_text=text)
        assert_status(by_stdin, "READY", rc=0)
        self.assertEqual(by_stdin.data.get("spec_head"), self.spec_head)
        self.assertIs(by_stdin.data.get("parallel_ready"), True)

    def test_plan_validate_rejects_stale_spec_head(self) -> None:
        text = plan_markdown(self.spec_head)
        (self.repo / "README.md").write_text("target moved\n")
        commit_all(self.repo, "move target after planning")

        result = run_cli(
            "plan-validate",
            "--repo",
            self.repo,
            input_text=text,
        )

        assert_status(result, "NEEDS_USER", rc=1)
        self.assertEqual(result.data.get("code"), "TARGET_SPEC_MISMATCH")

    def test_v1_unit_and_documentation_contracts_are_rejected_explicitly(self) -> None:
        plans = (
            plan_markdown(self.spec_head).replace("schema_version: 2", "schema_version: 1"),
            l1_plan_markdown(self.spec_head).replace(
                "schema_version: 2", "schema_version: 1"
            ),
        )
        for text in plans:
            with self.subTest(marker="documentation" if "documentation-spec" in text else "unit"):
                result = run_cli(
                    "plan-validate",
                    "--repo",
                    self.repo,
                    input_text=text,
                )
                assert_status(result, "NEEDS_USER", rc=1)
                self.assertEqual(result.data.get("code"), "PLAN_SCHEMA_UNSUPPORTED")
                self.assertEqual(result.data.get("supported_schema_versions"), [2])

    def test_effective_runner_source_is_exclusive(self) -> None:
        missing = run_cli(
            "plan-validate",
            "--repo",
            self.repo,
            input_text=plan_markdown(self.spec_head, runner=None),
        )
        assert_status(missing, "NEEDS_USER", rc=1)
        self.assertEqual(missing.data.get("code"), "VERIFICATION_RUNNER_MISSING")

        loop_config = self.repo / ".claude" / "loop.yml"
        loop_config.parent.mkdir(parents=True, exist_ok=True)
        loop_config.write_text(
            "pass_cmd:\n"
            "  - stage: test\n"
            "    cmd: bash verify.sh\n"
            "    timeout: 30\n"
        )
        commit_all(self.repo, "add repository verification source")
        self.spec_head = head(self.repo)

        duplicate = run_cli(
            "plan-validate",
            "--repo",
            self.repo,
            input_text=plan_markdown(self.spec_head),
        )
        assert_status(duplicate, "NEEDS_USER", rc=1)
        self.assertEqual(duplicate.data.get("code"), "PLAN_RUNNER_DUPLICATE_SOURCE")

        repository_source = run_cli(
            "plan-validate",
            "--repo",
            self.repo,
            input_text=plan_markdown(self.spec_head, runner=None),
        )
        assert_status(repository_source, "READY", rc=0)
        self.assertEqual(
            repository_source.data.get("effective_verification_source"),
            ".claude/loop.yml",
        )

    def test_invalid_repository_runner_is_rejected_without_validation_side_effects(self) -> None:
        loop_config = self.repo / ".claude" / "loop.yml"
        loop_config.parent.mkdir(parents=True, exist_ok=True)
        loop_config.write_text(
            "pass_cmd:\n"
            "  - stage: import\n"
            "    cmd: python3 -c 'import app'\n"
        )
        commit_all(self.repo, "add unsupported inline repository runner")
        self.spec_head = head(self.repo)
        plan = plan_markdown(self.spec_head, runner=None)
        before_status = git(self.repo, "status", "--short")
        before_worktrees = git(self.repo, "worktree", "list", "--porcelain")
        exclude = self.repo / ".git" / "info" / "exclude"
        before_exclude = exclude.read_text() if exclude.exists() else None

        result = run_cli(
            "plan-validate",
            "--repo",
            self.repo,
            input_text=plan,
        )

        assert_status(result, "FATAL", rc=2)
        self.assertEqual(result.data.get("code"), "RUNNER_INLINE_CODE_UNSUPPORTED")
        self.assertEqual(
            result.data.get("effective_verification_source"), ".claude/loop.yml"
        )
        self.assertEqual(git(self.repo, "status", "--short"), before_status)
        self.assertEqual(
            git(self.repo, "worktree", "list", "--porcelain"), before_worktrees
        )
        self.assertFalse((self.repo / ".builder-loop").exists())
        self.assertEqual(exclude.read_text() if exclude.exists() else None, before_exclude)

    def test_parallel_plan_rejects_overlapping_ownership(self) -> None:
        plan = write_plan(
            self.repo,
            plan_markdown(
                self.spec_head,
                parallel_ready=True,
                builder_write=["src/**", "tests/**"],
                tester_write=["tests/**"],
            ),
        )
        result = run_cli("plan-validate", "--repo", self.repo, "--plan", plan)
        assert_status(result, "NEEDS_USER")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.data.get("errors"), result.data)

    def test_serial_plan_still_requires_exclusive_ownership(self) -> None:
        plan = write_plan(
            self.repo,
            plan_markdown(
                self.spec_head,
                parallel_ready=False,
                builder_write=["src/**", "tests/**"],
                tester_write=["tests/**"],
            ),
        )
        result = run_cli("plan-validate", "--repo", self.repo, "--plan", plan)
        assert_status(result, "NEEDS_USER", rc=1)
        self.assertTrue(
            any("ownership overlaps" in str(error) for error in result.data.get("errors", [])),
            result.data,
        )

    def test_builder_cannot_own_verification_support_path(self) -> None:
        plan = write_plan(
            self.repo,
            plan_markdown(
                self.spec_head,
                builder_write=["src/**", "docs/**", "verify.sh"],
            ),
        )
        result = run_cli("plan-validate", "--repo", self.repo, "--plan", plan)
        assert_status(result, "NEEDS_USER", rc=1)
        self.assertTrue(
            any("support paths" in str(error) for error in result.data.get("errors", [])),
            result.data,
        )

    def test_runner_script_must_be_declared_as_support(self) -> None:
        text = plan_markdown(
            self.spec_head,
            builder_write=["src/**", "docs/**", "verify.sh"],
        ).replace('  support_paths: ["verify.sh"]', "  support_paths: []")
        result = run_cli("plan-validate", "--repo", self.repo, input_text=text)
        assert_status(result, "NEEDS_USER", rc=1)
        self.assertEqual(result.data.get("code"), "RUNNER_SUPPORT_PATH_MISSING")

    def test_wrapped_constant_success_runner_is_rejected(self) -> None:
        result = run_cli(
            "plan-validate",
            "--repo",
            self.repo,
            input_text=plan_markdown(self.spec_head, runner="bash -c 'exit 0'"),
        )
        assert_status(result, "FATAL", rc=2)
        self.assertEqual(result.data.get("code"), "TAUTOLOGICAL_PASS_COMMAND")

    def test_more_constant_success_runners_are_rejected(self) -> None:
        for runner in ("test 1 = 1", "bash -c 'true && true'", "bash -c 'pytest -q || true'"):
            with self.subTest(runner=runner):
                result = run_cli(
                    "plan-validate",
                    "--repo",
                    self.repo,
                    input_text=plan_markdown(self.spec_head, runner=runner),
                )
                assert_status(result, "FATAL", rc=2)
                self.assertEqual(result.data.get("code"), "TAUTOLOGICAL_PASS_COMMAND")

    def test_inverted_and_scripted_success_runners_are_rejected(self) -> None:
        runners = (
            "! pytest",
            "bash -c '! pytest'",
            "if pytest; then false; else true; fi",
            "python3 -c 'import sys; sys.exit(0)'",
            "python3 -c 'raise SystemExit(0)'",
            "bash -c 'exit $((0))'",
        )
        for runner in runners:
            with self.subTest(runner=runner):
                result = run_cli(
                    "plan-validate",
                    "--repo",
                    self.repo,
                    input_text=plan_markdown(self.spec_head, runner=runner),
                )
                assert_status(result, "FATAL", rc=2)

    def test_behavior_mock_and_checklist_contracts_are_required(self) -> None:
        mutations = (
            lambda text: text.replace('    boundaries: ["zero", "negative"]\n', ""),
            lambda text: text.replace('    invariants: ["inputs are not mutated"]\n', ""),
            lambda text: text.replace("mock_strategy: {}\n", ""),
            lambda text: text.replace("id: add-positive", "id: NOT_KEBAB"),
            lambda text: text.replace("- [ ]", "-", 3),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                result = run_cli(
                    "plan-validate",
                    "--repo",
                    self.repo,
                    input_text=mutate(plan_markdown(self.spec_head)),
                )
                assert_status(result, "NEEDS_USER", rc=1)

    def test_serial_plan_requires_declared_public_prerequisites(self) -> None:
        text = plan_markdown(self.spec_head, parallel_ready=False).replace(
            '  public_prerequisites: ["src/public_api.py"]',
            "  public_prerequisites: []",
        )
        result = run_cli("plan-validate", "--repo", self.repo, input_text=text)
        assert_status(result, "NEEDS_USER", rc=1)
        self.assertTrue(
            any("public_prerequisites" in str(error) for error in result.data.get("errors", [])),
            result.data,
        )

    def test_serial_public_prerequisites_are_exact_builder_owned_files(self) -> None:
        mutations = (
            lambda text: text.replace(
                '  public_prerequisites: ["src/public_api.py"]',
                '  public_prerequisites: ["src/**"]',
            ),
            lambda text: text.replace(
                '  public_prerequisites: ["src/public_api.py"]',
                '  public_prerequisites: ["outside/public_api.py"]',
            ),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                result = run_cli(
                    "plan-validate",
                    "--repo",
                    self.repo,
                    input_text=mutate(plan_markdown(self.spec_head, parallel_ready=False)),
                )
                assert_status(result, "NEEDS_USER", rc=1)

    def test_l1_write_boundary_requires_exact_markdown_files(self) -> None:
        for path in ("docs/**", "**", "src/generated.py"):
            with self.subTest(path=path):
                result = run_cli(
                    "plan-validate",
                    "--repo",
                    self.repo,
                    input_text=l1_plan_markdown(self.spec_head, builder_write=[path]),
                )
                assert_status(result, "NEEDS_USER", rc=1)

    def test_runner_path_override_is_rejected(self) -> None:
        for runner in ("PATH=./bin:$PATH pytest -q", "env PATH=. pytest -q"):
            with self.subTest(runner=runner):
                result = run_cli(
                    "plan-validate",
                    "--repo",
                    self.repo,
                    input_text=plan_markdown(self.spec_head, runner=runner),
                )
                assert_status(result, "FATAL", rc=2)

    def test_runner_wrapper_outside_tester_ownership_is_valid(self) -> None:
        text = plan_markdown(self.spec_head, runner="env bash verify.sh")
        result = run_cli("plan-validate", "--repo", self.repo, input_text=text)
        assert_status(result, "READY", rc=0)

    def test_runner_control_files_cannot_be_builder_owned(self) -> None:
        cases = (
            ("make verify", ["src/**", "Makefile"]),
            ("python3 -m pytest", ["src/**", "conftest.py"]),
            ("cd app && make verify", ["src/**", "app/Makefile"]),
        )
        for runner, builder_write in cases:
            with self.subTest(runner=runner):
                result = run_cli(
                    "plan-validate",
                    "--repo",
                    self.repo,
                    input_text=plan_markdown(
                        self.spec_head,
                        runner=runner,
                        builder_write=builder_write,
                    ),
                )
                assert_status(result, "NEEDS_USER", rc=1)

    def test_boolean_schema_and_revision_are_not_integers(self) -> None:
        for field in ("schema_version", "plan_revision"):
            with self.subTest(field=field):
                original = 2 if field == "schema_version" else 1
                text = plan_markdown(self.spec_head).replace(
                    f"{field}: {original}", f"{field}: true"
                )
                result = run_cli("plan-validate", "--repo", self.repo, input_text=text)
                assert_status(result, "NEEDS_USER", rc=1)

    def test_wrapped_runner_entry_is_protected_from_both_roles(self) -> None:
        for runner in ("env bash verify.sh", "bash -c 'bash verify.sh'"):
            with self.subTest(runner=runner, role="builder"):
                result = run_cli(
                    "plan-validate",
                    "--repo",
                    self.repo,
                    input_text=plan_markdown(
                        self.spec_head,
                        runner=runner,
                        builder_write=["src/**", "verify.sh"],
                    ),
                )
                assert_status(result, "NEEDS_USER", rc=1)
            with self.subTest(runner=runner, role="tester"):
                result = run_cli(
                    "plan-validate",
                    "--repo",
                    self.repo,
                    input_text=plan_markdown(
                        self.spec_head,
                        runner=runner,
                        tester_write=["tests/**", "verify.sh"],
                    ),
                )
                assert_status(result, "NEEDS_USER", rc=1)

    def test_glob_intersection_is_rejected_conservatively(self) -> None:
        ownership = run_cli(
            "plan-validate",
            "--repo",
            self.repo,
            input_text=plan_markdown(
                self.spec_head,
                builder_write=["src/file?.py"],
                tester_write=["src/filea.py", "tests/**"],
            ),
        )
        assert_status(ownership, "NEEDS_USER", rc=1)

        support_text = plan_markdown(
            self.spec_head,
            runner="python3 -m unittest discover -s tests",
            builder_write=["src/**", "fixtures/exp*.txt"],
        ).replace('  support_paths: ["verify.sh"]', '  support_paths: ["fixtures/expected.txt"]')
        support = run_cli("plan-validate", "--repo", self.repo, input_text=support_text)
        assert_status(support, "NEEDS_USER", rc=1)

    def test_builtin_yaml_fallback_accepts_quoted_colon_interface(self) -> None:
        text = plan_markdown(self.spec_head)
        cp = run_process([sys.executable, "-S", CLI, "plan-validate", "--repo", self.repo], input_text=text)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        payload = json.loads(cp.stdout.splitlines()[-1])
        self.assertEqual(payload.get("status"), "READY", payload)

    def test_builtin_yaml_fallback_accepts_documentation_spec(self) -> None:
        cp = run_process(
            [sys.executable, "-S", CLI, "plan-validate", "--repo", self.repo],
            input_text=l1_plan_markdown(self.spec_head),
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        payload = json.loads(cp.stdout.splitlines()[-1])
        self.assertEqual(payload.get("status"), "READY", payload)

    def test_typed_interface_scalar_is_rejected_before_start_side_effects(self) -> None:
        text = plan_markdown(self.spec_head).replace(
            '  - "src/calc.py:add(a, b) -> int"',
            "  - module: src.calc\n"
            "    import: src.calc.add\n"
            '    signature: "add(a, b)"\n'
            "    output: 2026-07-17\n"
            "    errors: []",
        )
        plan = write_plan(self.repo, text, name="typed-interface-plan.md")
        result = run_cli(
            "start",
            "--repo",
            self.repo,
            "--plan",
            plan,
            "--task",
            "typed interface",
            "--session-id",
            "typed-interface-session",
        )
        assert_status(result, "NEEDS_USER", rc=1)
        self.assertEqual(git(self.repo, "worktree", "list", "--porcelain").count("worktree "), 1)

    def test_canonical_target_test_dirs_is_required(self) -> None:
        text = plan_markdown(self.spec_head)
        text = text.replace("target_test_dirs:", "test_dirs:")
        plan = write_plan(self.repo, text)
        result = run_cli("plan-validate", "--repo", self.repo, "--plan", plan)
        assert_status(result, "NEEDS_USER")
        self.assertNotEqual(result.returncode, 0)

    def test_l1_plan_without_unit_test_spec_remains_valid_but_not_parallel(self) -> None:
        text = l1_plan_markdown(self.spec_head)
        result = run_cli("plan-validate", "--repo", self.repo, input_text=text)
        assert_status(result, "READY", rc=0)
        self.assertIs(result.data.get("parallel_ready"), False)
        self.assertEqual(result.data.get("effective_verification_source"), "none")

    def test_l1_plan_cannot_declare_e2e_cases(self) -> None:
        text = l1_plan_markdown(self.spec_head) + "\n".join(
            [
                "<!-- e2e-cases -->",
                "- id: invalid-doc-e2e",
                "<!-- /e2e-cases -->",
            ]
        )
        result = run_cli("plan-validate", "--repo", self.repo, input_text=text)
        assert_status(result, "NEEDS_USER")
        self.assertEqual(result.data.get("code"), "PLAN_L1_E2E_INVALID")

    def test_behavior_ids_must_be_unique(self) -> None:
        text = plan_markdown(self.spec_head).replace(
            "mock_strategy: {}",
            "  - id: add-positive\n"
            '    what: "duplicate behavior id"\n'
            '    boundaries: ["one"]\n'
            '    invariants: ["stable"]\n'
            "mock_strategy: {}",
        )
        result = run_cli("plan-validate", "--repo", self.repo, input_text=text)
        assert_status(result, "NEEDS_USER")
        self.assertTrue(
            any("unique" in str(error) for error in result.data.get("errors", [])),
            result.data,
        )

    def test_non_l1_plan_cannot_omit_unit_test_spec(self) -> None:
        text = "\n".join(
            [
                "# Executable change plan",
                "",
                "<!-- plan-checklist -->",
                "- [ ] Change runtime behavior.",
                "<!-- /plan-checklist -->",
                "",
            ]
        )
        result = run_cli("plan-validate", "--repo", self.repo, input_text=text)
        assert_status(result, "NEEDS_USER")
        self.assertNotEqual(result.returncode, 0)
        errors = result.data.get("errors") or result.data.get("details", {}).get("errors") or []
        self.assertTrue(any("unit-test-spec" in str(item) for item in errors), result.data)


if __name__ == "__main__":
    unittest.main()
