from __future__ import annotations

import json
import os
import re
import shlex
import sys
import unittest
from pathlib import Path

from harness import (
    assert_status,
    blackbox_report_details,
    cleanup_repo,
    commit_all,
    finish_agent_turn,
    git,
    head,
    init_repo,
    load_ledger,
    plan_markdown,
    register_agent,
    run_cli,
    run_process,
    start_agent_turn,
    start_run,
    worktrees_from,
    write_plan,
)
from proof_harness import baseline_group, create_proof_fixture, prove


ROOT = Path(__file__).resolve().parents[1]


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


class TesterSourceTrustContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repos: list[Path] = []

    def tearDown(self) -> None:
        for repo in self.repos:
            cleanup_repo(repo)

    def test_proof_and_reviewer_snapshot_bind_same_tester_source_identity(self) -> None:
        fixture = create_proof_fixture()
        self.repos.append(fixture.repo)
        proof = prove(fixture, baseline_group())
        self.assertEqual(proof.data.get("status"), "READY", proof.data)
        tester_manifest = proof.data["tester_manifest_sha256"]
        self.assertEqual(proof.data["tester_source_head"], fixture.tester_author_head)

        verified = run_cli("verify", "--run", fixture.run_path)
        assert_status(verified, "PASS", rc=0)
        register_agent(
            fixture.run_path,
            "tester",
            agent_id=fixture.tester_agent_id,
            result="pass",
        )
        command = [
            sys.executable,
            "-m",
            "unittest",
            "tests.test_proof_target",
        ]
        head_before = head(fixture.builder)
        blackbox_run = run_process(
            command,
            cwd=fixture.builder,
            env={"PYTHONDONTWRITEBYTECODE": "1"},
        )
        head_after = head(fixture.builder)
        self.assertEqual(blackbox_run.returncode, 0, blackbox_run.stderr)
        candidate_dirty = bool(
            git(
                fixture.builder,
                "status",
                "--porcelain",
                "--untracked-files=all",
            )
            or git(
                fixture.builder,
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
            )
        )
        self.assertFalse(candidate_dirty)
        blackbox = run_cli(
            "record-evidence",
            "--run",
            fixture.run_path,
            "--kind",
            "e2e_verified",
            "--head",
            fixture.integrated_head,
            "--agent-id",
            fixture.tester_agent_id,
            "--details",
            json.dumps(
                blackbox_report_details(
                    load_ledger(fixture.run_path),
                    candidate_worktree=fixture.builder,
                    head_before=head_before,
                    head_after=head_after,
                    command=shlex.join(command),
                    returncode=blackbox_run.returncode,
                    candidate_dirty=candidate_dirty,
                )
            ),
        )
        assert_status(blackbox, "READY", rc=0)
        register_agent(
            fixture.run_path, "reviewer", agent_id="source-reviewer", result="pass"
        )
        reviewer = load_ledger(fixture.run_path)["agents"]["reviewer"]
        snapshot = proof.data
        self.assertEqual(snapshot["tester_source_head"], fixture.tester_author_head)
        self.assertEqual(snapshot["tester_manifest_sha256"], tester_manifest)
        for reviewer_snapshot in (
            reviewer["review_prerequisites"]["start"],
            reviewer["review_prerequisites"]["completion"],
        ):
            self.assertEqual(
                reviewer_snapshot["test_effectiveness_head"], fixture.integrated_head
            )
            self.assertTrue(
                reviewer_snapshot["tester_integration_completed"], reviewer_snapshot
            )

    def test_tester_source_must_be_committed_regular_owned_files(self) -> None:
        linked_test = (
            "import unittest\n"
            "from src.proof_fixture import VALUE\n\n"
            "class SymlinkContract(unittest.TestCase):\n"
            "    def test_frozen_invariant(self):\n"
            "        self.assertEqual(VALUE, 1)\n"
        )
        repo = init_repo({"src/symlink_test_target.py": linked_test})
        self.repos.append(repo)
        plan = write_plan(
            repo,
            plan_markdown(
                head(repo),
                builder_write=["src/**"],
                tester_write=["tests/**"],
            ),
        )
        started, run_path = start_run(repo, plan, task="unsafe tester source")
        _builder, tester = worktrees_from(started, run_path)
        agent_id, turn_id = start_agent_turn(run_path, "tester", agent_id="unsafe-tester")
        link = tester / "tests" / "test_link.py"
        os.symlink("../src/symlink_test_target.py", link)
        commit_all(tester, "commit unsafe tester symlink")

        finish_agent_turn(
            run_path,
            "tester",
            agent_id=agent_id,
            turn_id=turn_id,
            result="tests_ready",
        )
        integrated = run_cli("integrate-tests", "--run", run_path)
        assert_status(integrated, "READY", rc=0)
        verified = run_cli("verify", "--run", run_path)
        assert_status(verified, "PASS", rc=0)
        patch = (
            "diff --git a/src/proof_fixture.py b/src/proof_fixture.py\n"
            "--- a/src/proof_fixture.py\n"
            "+++ b/src/proof_fixture.py\n"
            "@@ -1 +1 @@\n"
            "-VALUE = 1\n"
            "+VALUE = 0\n"
        )
        proof = run_cli(
            "prove-tests",
            "--repo",
            repo,
            "--run",
            run_path,
            "--spec",
            "-",
            input_text=json.dumps(
                {
                    "schema_version": 1,
                    "groups": [
                        {
                            "behavior_ids": ["add-positive"],
                            "method": "mutation",
                            "argv": [
                                "python3",
                                "-m",
                                "unittest",
                                "tests.test_link."
                                "SymlinkContract.test_frozen_invariant",
                            ],
                            "test_ids": [
                                "tests.test_link."
                                "SymlinkContract.test_frozen_invariant"
                            ],
                            "timeout_seconds": 30,
                            "patch": patch,
                        }
                    ],
                }
            ),
        )
        self.assertNotEqual(proof.data.get("status"), "READY", proof.data)
        self.assertNotEqual(proof.returncode, 0, proof.data)
        self.assertIn("tests.test_link", str(proof.data))
        self.assertRegex(str(proof.data).lower(), r"source|regular|symlink|manifest")
        self.assertTrue(load_ledger(run_path)["tester_integration"]["completed"])

    def test_role_contracts_define_trusted_source_without_claiming_os_sandbox(self) -> None:
        paths = (
            "skills/builder-loop-planner/SKILL.md",
            "agents/tester.toml",
            "agents/reviewer.toml",
            "docs/design-philosophy.md",
            "docs/architecture.md",
            "AGENTS.md",
            "CHANGELOG.md",
        )
        texts = {path: (ROOT / path).read_text() for path in paths}
        for path, text in texts.items():
            value = compact(text)
            with self.subTest(path=path):
                self.assertTrue(
                    "tester" in value and ("manifest" in value or "清单" in value),
                    f"{path} omits Tester source manifest trust",
                )
                self.assertTrue(
                    "reviewer" in value and ("审查" in value or "review" in value),
                    f"{path} omits Reviewer source-integrity responsibility",
                )

        combined = "\n".join(texts.values())
        self.assertRegex(
            compact(combined),
            r"(?:不承诺|不提供|不得宣称|不是).{0,80}(?:os|操作系统).{0,30}(?:沙箱|隔离)",
        )
        self.assertRegex(
            compact(combined),
            r"(?:supervisor|监督进程).{0,100}(?:不是|不作为|不得描述).{0,40}(?:安全边界|沙箱)",
        )

    def test_tester_correction_remains_same_thread_owned_and_recommitted(self) -> None:
        tester = (ROOT / "agents" / "tester.toml").read_text()
        value = compact(tester)
        self.assertIn("correction", value)
        self.assertTrue("同一thread" in value or "原thread" in value)
        self.assertTrue("tester-owned" in value or "testerowned" in value)
        self.assertTrue("重新提交" in value or "recommit" in value)
        self.assertTrue("不得" in value and ("放宽断言" in value or "relax" in value))

    def test_role_contracts_cover_real_patch_targets_and_aggregate_failures(self) -> None:
        tester = compact((ROOT / "agents" / "tester.toml").read_text())
        reviewer = compact((ROOT / "agents" / "reviewer.toml").read_text())

        self.assertRegex(tester, r"(?:patch|替换|注入)")
        self.assertRegex(
            tester, r"(?:调用点|调用处|callsite|resolvedsymbol|实际解析)"
        )
        self.assertRegex(tester, r"(?:公开|public)")
        self.assertRegex(tester, r"(?:异常|exception|失败语义|failuresemantics)")
        self.assertRegex(tester, r"(?:自包含|self-contained|selfcontained)")
        self.assertRegex(tester, r"(?:user-site|usersite|ambient|用户站点|用户site)")

        self.assertRegex(reviewer, r"(?:patch|替换|注入)")
        self.assertRegex(
            reviewer, r"(?:调用点|调用处|callsite|resolvedsymbol|实际解析)"
        )
        self.assertRegex(reviewer, r"(?:公开|public)")
        self.assertRegex(reviewer, r"(?:异常|exception|失败语义|failuresemantics)")
        self.assertRegex(reviewer, r"(?:自包含|self-contained|selfcontained)")
        self.assertRegex(reviewer, r"(?:failures|失败列表|聚合失败)")
        self.assertRegex(
            reviewer,
            r"(?:每个|全部|所有|every|all).{0,100}(?:failure|失败|group|组)",
        )

    def test_blackbox_helpers_do_not_embed_a_nested_test_runner(self) -> None:
        for relative in (
            "tests/helpers/proof_readiness_blackbox.py",
            "tests/helpers/native_proof_recovery_blackbox.py",
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            compact_source = compact(source)
            with self.subTest(path=relative):
                self.assertNotIn("importsubprocess", compact_source)
                self.assertNotIn("pytest.main", compact_source)
                self.assertNotIn("unittest.main", compact_source)
                self.assertNotRegex(
                    compact_source,
                    r"ledger(?:\.json)?.{0,80}(?:write_text|write_bytes|json\.dump)",
                )

    def test_proof_input_correction_does_not_require_tester_source_commit(self) -> None:
        tester = compact((ROOT / "agents" / "tester.toml").read_text())
        self.assertIn("phase=proof_diagnose", tester)
        self.assertRegex(tester, r"replacement.{0,20}proof_spec")
        self.assertIn("result=tests_ready", tester)
        self.assertRegex(tester, r"不改文件.{0,120}不自行重跑")
        self.assertRegex(tester, r"testersource.{0,180}(?:problem|问题)")


if __name__ == "__main__":
    unittest.main()
