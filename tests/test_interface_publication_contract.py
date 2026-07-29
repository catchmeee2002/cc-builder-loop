from __future__ import annotations

import json
import unittest
from pathlib import Path

from harness import (
    assert_status,
    cleanup_repo,
    head,
    init_repo,
    load_ledger,
    plan_markdown,
    repo_session_id,
    run_cli,
    start_run,
    write_plan,
)


def with_interfaces(text: str, yaml_items: str) -> str:
    return text.replace('  - "src/calc.py:add(a, b) -> int"', yaml_items, 1)


def structured_interface(path: str) -> str:
    return (
        f'  - module: {json.dumps(path)}\n'
        '    import: "from src.calc import add"\n'
        '    signature: "add(a, b) -> int"\n'
        '    output: "arithmetic sum"\n'
        '    errors: ["ValueError"]'
    )


class InterfacePublicationContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repos: list[Path] = []

    def tearDown(self) -> None:
        for repo in self.repos:
            cleanup_repo(repo)

    def repo(self) -> Path:
        repo = init_repo()
        self.repos.append(repo)
        return repo

    def test_parallel_builder_paths_are_rejected_and_normalized(self) -> None:
        repo = self.repo()
        base = plan_markdown(head(repo), builder_write=["src/**", "docs/**"])
        text = with_interfaces(
            base,
            "\n".join(
                [
                    '  - "src/calc.py"',
                    '  - "docs/public-contract.md"',
                    structured_interface("src/structured_api.py"),
                ]
            ),
        )

        result = run_cli("plan-validate", "--repo", repo, input_text=text)

        assert_status(result, "NEEDS_USER", rc=1)
        self.assertEqual(
            result.data.get("code"),
            "PLAN_PARALLEL_INTERFACE_INPUT_UNPUBLISHED",
            result.data,
        )
        self.assertEqual(
            result.data.get("interface_publication_contract_version"), 1
        )
        self.assertEqual(
            result.data.get("interface_input_paths"),
            ["docs/public-contract.md", "src/calc.py", "src/structured_api.py"],
        )
        diagnostic = json.dumps(result.data, ensure_ascii=False).lower()
        self.assertIn("blackbox", diagnostic)
        self.assertIn("serial", diagnostic)

    def test_root_unicode_and_space_paths_are_publication_inputs(self) -> None:
        repo = self.repo()
        owned_paths = [
            "README.md",
            "Makefile",
            "接口/公开 API.py",
            "space dir/public API.py",
        ]
        base = plan_markdown(head(repo), builder_write=owned_paths)
        interfaces = "\n".join(
            [
                '  - "README.md"',
                '  - "Makefile"',
                structured_interface("接口/公开 API.py"),
                '  - "space dir/public API.py"',
            ]
        )
        parallel = with_interfaces(base, interfaces)

        rejected = run_cli("plan-validate", "--repo", repo, input_text=parallel)
        assert_status(rejected, "NEEDS_USER", rc=1)
        self.assertEqual(
            rejected.data.get("code"),
            "PLAN_PARALLEL_INTERFACE_INPUT_UNPUBLISHED",
        )
        self.assertEqual(
            rejected.data.get("interface_input_paths"), sorted(owned_paths)
        )

        serial = parallel.replace("parallel_ready: true", "parallel_ready: false")
        serial = serial.replace(
            "  public_prerequisites: []",
            '  public_prerequisites: ["README.md", "Makefile", '
            '"space dir/public API.py"]',
        )
        missing = run_cli("plan-validate", "--repo", repo, input_text=serial)
        assert_status(missing, "NEEDS_USER", rc=1)
        self.assertEqual(missing.data.get("code"), "PLAN_INTERFACE_INPUT_UNPUBLISHED")
        self.assertIn("接口/公开 API.py", missing.data.get("interface_input_paths", []))

        complete = serial.replace(
            '"space dir/public API.py"]',
            '"space dir/public API.py", "接口/公开 API.py"]',
        )
        ready = run_cli("plan-validate", "--repo", repo, input_text=complete)
        assert_status(ready, "READY", rc=0)
        self.assertEqual(ready.data.get("interface_input_paths"), sorted(owned_paths))

    def test_exact_ownership_does_not_match_substrings_or_blackbox_routes(self) -> None:
        repo = self.repo()
        base = plan_markdown(
            head(repo), builder_write=["README.md", "v1/**", "src/**"]
        )
        text = with_interfaces(
            base,
            "\n".join(
                [
                    '  - "prefixREADME.mdSuffix"',
                    '  - "GET /v1/runs/{run_id}"',
                    '  - "README.md:section"',
                    '  - "codex-builder-loop status --run RUN_ID"',
                    '  - "runtime.codex_builder_loop.cli"',
                    '  - "src/calc.py:add"',
                ]
            ),
        )

        result = run_cli("plan-validate", "--repo", repo, input_text=text)
        assert_status(result, "READY", rc=0)
        self.assertEqual(result.data.get("interface_input_paths"), [])

    def test_non_file_interfaces_and_non_builder_paths_remain_parallel_ready(self) -> None:
        repo = self.repo()
        base = plan_markdown(head(repo), builder_write=["src/**"])
        text = with_interfaces(
            base,
            "\n".join(
                [
                    '  - "src/calc.py:add"',
                    '  - "codex-builder-loop status --run RUN_ID"',
                    '  - "runtime.codex_builder_loop.cli"',
                    '  - "GET /v1/runs/{run_id}"',
                    '  - "tests/test_calc.py"',
                    '  - "verify.sh"',
                ]
            ),
        )

        validated = run_cli("plan-validate", "--repo", repo, input_text=text)
        assert_status(validated, "READY", rc=0)
        self.assertEqual(
            validated.data.get("interface_publication_contract_version"), 1
        )
        self.assertEqual(validated.data.get("interface_input_paths"), [])

        plan = write_plan(repo, text)
        started, run_path = start_run(
            repo, plan, session_id=repo_session_id(repo, "interface-v1")
        )
        self.assertEqual(started.data.get("interface_publication_contract_version"), 1)
        self.assertEqual(started.data.get("interface_input_paths"), [])
        ledger = load_ledger(run_path)
        self.assertEqual(
            ledger["plan"].get("interface_publication_contract_version"), 1
        )
        self.assertEqual(ledger["plan"].get("interface_input_paths"), [])

    def test_serial_publication_requires_exact_complete_paths(self) -> None:
        repo = self.repo()
        base = plan_markdown(
            head(repo),
            parallel_ready=False,
            builder_write=["src/**"],
        )
        interfaces = "\n".join(
            ['  - "src/calc.py"', structured_interface("src/structured_api.py")]
        )
        serial = with_interfaces(base, interfaces)

        glob = with_interfaces(
            base,
            '  - "src/*.py"',
        )
        glob_result = run_cli("plan-validate", "--repo", repo, input_text=glob)
        assert_status(glob_result, "NEEDS_USER", rc=1)
        self.assertEqual(glob_result.data.get("code"), "PLAN_INTERFACE_INPUT_NOT_EXACT")

        missing = serial.replace(
            '  public_prerequisites: ["src/public_api.py"]',
            '  public_prerequisites: ["src/calc.py"]',
        )
        missing_result = run_cli("plan-validate", "--repo", repo, input_text=missing)
        assert_status(missing_result, "NEEDS_USER", rc=1)
        self.assertEqual(
            missing_result.data.get("code"), "PLAN_INTERFACE_INPUT_UNPUBLISHED"
        )
        self.assertEqual(
            missing_result.data.get("interface_input_paths"),
            ["src/calc.py", "src/structured_api.py"],
        )

        complete = serial.replace(
            '  public_prerequisites: ["src/public_api.py"]',
            '  public_prerequisites: ["src/structured_api.py", "src/calc.py"]',
        )
        ready = run_cli("plan-validate", "--repo", repo, input_text=complete)
        assert_status(ready, "READY", rc=0)
        self.assertEqual(ready.data["interface_publication_contract_version"], 1)
        self.assertEqual(
            ready.data["interface_input_paths"],
            ["src/calc.py", "src/structured_api.py"],
        )

    def test_legacy_active_ledger_is_effective_v0_and_read_only(self) -> None:
        repo = self.repo()
        plan = write_plan(repo, plan_markdown(head(repo)))
        _started, run_path = start_run(repo, plan)
        ledger_path = run_path / "ledger.json"
        ledger = load_ledger(run_path)
        ledger["plan"].pop("interface_publication_contract_version", None)
        ledger["plan"].pop("interface_input_paths", None)
        ledger_path.write_text(json.dumps(ledger, ensure_ascii=False, sort_keys=True))
        before = ledger_path.read_bytes()

        status = run_cli("status", "--run", run_path)
        doctor = run_cli("doctor", "--run", run_path)

        self.assertEqual(status.returncode, 0, status.data)
        self.assertEqual(doctor.returncode, 0, doctor.data)
        self.assertEqual(status.data.get("interface_publication_contract_version"), 0)
        self.assertEqual(doctor.data.get("interface_publication_contract_version"), 0)
        self.assertEqual(status.data.get("interface_input_paths"), [])
        self.assertEqual(doctor.data.get("interface_input_paths"), [])
        self.assertEqual(ledger_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
