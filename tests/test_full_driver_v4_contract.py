from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any

from harness import CLI, ROOT, cleanup_repo, commit_all, git, head, init_repo, run_process


SKILL = ROOT / "skills" / "full-driver-v4-experiment" / "SKILL.md"
SKILL_MANIFEST = (
    ROOT / "skills" / "full-driver-v4-experiment" / "agents" / "openai.yaml"
)


def compact(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def contract_for(repo: Path) -> dict[str, Any]:
    return {
        "schema_version": 4,
        "mission": {
            "revision": 1,
            "objective": "Deliver a Full Driver v4 fixture.",
            "behaviors": [
                {"id": "driver-flow", "description": "The driver converges safely."}
            ],
            "interfaces": [],
            "acceptance_cases": [
                {"id": "driver-final", "description": "The candidate is finalized."}
            ],
            "trust_boundaries": [
                {"id": "independent-gates", "description": "Roles remain independent."}
            ],
        },
        "authority": {
            "target_branch": "main",
            "builder_write": ["src/**", "docs/**"],
            "tester_write": ["tests/**"],
            "dirty_intake": [],
        },
        "assurance": {
            "required": ["machine", "tester", "blackbox", "reviewer"],
            "machine_commands": [
                {
                    "id": "fixture-tests",
                    "argv": ["bash", "verify.sh"],
                    "timeout_seconds": 30,
                }
            ],
        },
        "execution": {
            "version": 1,
            "driver_enforced": True,
            "candidate_head": None,
            "builder_files": [],
            "tester_files": [],
            "tester_source": None,
            "dirty_snapshot": [],
            "commands": [
                {
                    "id": "fixture-blackbox",
                    "argv": ["bash", "verify.sh"],
                    "timeout_seconds": 30,
                }
            ],
            "agents": {
                "tester": {
                    "agent_id": "full-driver-tester",
                    "thread_id": "full-driver-tester-thread",
                },
                "reviewer": {
                    "agent_id": "full-driver-reviewer",
                    "thread_id": "full-driver-reviewer-thread",
                },
            },
        },
    }


class FullDriverV4ContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo()
        self.tempdir = tempfile.TemporaryDirectory(prefix="full-driver-v4-contract-")
        self.artifacts = Path(self.tempdir.name)

    def tearDown(self) -> None:
        cleanup_repo(self.repo)
        self.tempdir.cleanup()

    def write_json(self, name: str, value: Any) -> Path:
        path = self.artifacts / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def invoke(
        self, command: str, *args: str | Path, experimental: bool = True
    ) -> tuple[int, dict[str, Any]]:
        argv: list[str | Path] = [sys.executable, CLI, "assurance"]
        if experimental:
            argv.append("--experimental-v4")
        argv.extend([command, *args])
        completed = run_process(argv)
        lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, (completed.returncode, completed.stdout, completed.stderr))
        data = json.loads(lines[-1])
        self.assertIsInstance(data, dict)
        return completed.returncode, data

    def start(
        self, run_id: str, *, contract: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], Path]:
        contract_path = self.write_json(
            f"{run_id}-contract.json", contract or contract_for(self.repo)
        )
        rc, data = self.invoke(
            "start",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--session-id",
            "full-driver-v4-session",
            "--contract",
            contract_path,
        )
        self.assertEqual(rc, 0, data)
        candidate = Path(data["candidate_worktree"])
        return data, candidate.parent

    def load_ledger(self, run_path: Path) -> dict[str, Any]:
        return json.loads((run_path / "ledger.json").read_text(encoding="utf-8"))

    def prepare_and_record_tester(self, run_id: str, run_path: Path) -> dict[str, Any]:
        ledger = self.load_ledger(run_path)
        tester = ledger["facets"]["execution"]["agents"]["tester"]
        rc, prepared = self.invoke(
            "prepare-tester",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--agent-id",
            tester["agent_id"],
            "--thread-id",
            tester["thread_id"],
        )
        self.assertEqual(rc, 0, prepared)
        source = self.load_ledger(run_path)["facets"]["execution"]["tester_source"]
        worktree = Path(source["worktree"])
        (worktree / "tests" / "__init__.py").write_text("", encoding="utf-8")
        test_path = worktree / "tests" / "test_full_driver_fixture.py"
        test_path.write_text(
            "import unittest\n\n"
            "from src.calc import add\n\n\n"
            "class FullDriverFixtureTest(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        self.assertEqual(add(3, 4), 7)\n",
            encoding="utf-8",
        )
        commit_all(worktree, "add Full Driver tester fixture")
        rc, integrated = self.invoke(
            "integrate-tester", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, integrated)
        ledger = self.load_ledger(run_path)
        execution = ledger["facets"]["execution"]
        source = execution["tester_source"]
        report = {
            "schema_version": 1,
            "kind": "tester",
            "status": "pass",
            "candidate_head": execution["candidate_head"],
            "producer": {"role": "tester", **tester},
            "details": {
                "result": "tests_ready",
                "source_head": source["head"],
                "files": source["files"],
            },
        }
        report_path = self.write_json(f"{run_id}-tester-report.json", report)
        rc, recorded = self.invoke(
            "record-evidence",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--kind",
            "tester",
            "--report",
            report_path,
        )
        self.assertEqual(rc, 0, recorded)
        return self.load_ledger(run_path)

    def make_uv_launcher(
        self,
        name: str,
        *,
        execute_child: bool = True,
        invocation_marker: Path | None = None,
    ) -> Path:
        root = self.artifacts / name
        root.mkdir(parents=True, exist_ok=True)
        launcher = root / "uv"
        child = (
            "command = sys.argv[5:]\n"
            "if not command:\n"
            "    raise SystemExit(93)\n"
            "if command[0] == 'python':\n"
            "    command[0] = sys.executable\n"
            "os.execvpe(command[0], command, os.environ)\n"
            if execute_child
            else (
                "print('tests/test_uv_proof.py::test_observation PASSED')\n"
                "print('1 passed in 0.01s')\n"
                "raise SystemExit(0)\n"
            )
        )
        launcher.write_text(
            "#!/usr/bin/env python3\n"
            "import os\n"
            "import sys\n\n"
            + (
                f"open({str(invocation_marker)!r}, 'a', encoding='utf-8').write('invoked\\n')\n"
                if invocation_marker is not None
                else ""
            )
            +
            "if sys.argv[1:5] != ['run', '--frozen', '--offline', '--no-env-file']:\n"
            "    raise SystemExit(92)\n"
            + child,
            encoding="utf-8",
        )
        launcher.chmod(0o755)
        return launcher.resolve()

    def prepare_uv_proof_run(
        self,
        run_id: str,
        *,
        baseline_source: str,
        candidate_source: str,
        test_source: str,
        project_modes: dict[str, str] | None = None,
    ) -> tuple[Path, dict[str, Any], dict[str, Any]]:
        modes = project_modes or {"pyproject.toml": "regular", "uv.lock": "regular"}
        (self.repo / "src" / "calc.py").write_text(baseline_source, encoding="utf-8")
        for name in ("pyproject.toml", "uv.lock"):
            path = self.repo / name
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
            mode = modes.get(name, "missing")
            if mode == "regular":
                path.write_text(
                    (
                        "[project]\nname = 'proof-fixture'\nversion = '0.0.0'\n"
                        "requires-python = '>=3.11'\n"
                        if name == "pyproject.toml"
                        else "version = 1\nrevision = 3\nrequires-python = '>=3.11'\n"
                    ),
                    encoding="utf-8",
                )
            elif mode == "symlink":
                path.symlink_to("README.md")
            elif mode != "missing":
                raise AssertionError(f"unsupported project mode: {mode}")
        commit_all(self.repo, f"prepare uv proof baseline {run_id}")
        contract = contract_for(self.repo)
        contract["assurance"]["required"].insert(2, "proof")
        data, run_path = self.start(run_id, contract=contract)
        candidate = Path(data["candidate_worktree"])
        (candidate / "src" / "calc.py").write_text(
            candidate_source, encoding="utf-8"
        )
        commit_all(candidate, f"implement uv proof candidate {run_id}")
        rc, checkpointed = self.invoke(
            "checkpoint-builder", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, checkpointed)
        ledger = self.load_ledger(run_path)
        tester = ledger["facets"]["execution"]["agents"]["tester"]
        rc, prepared = self.invoke(
            "prepare-tester",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--agent-id",
            tester["agent_id"],
            "--thread-id",
            tester["thread_id"],
        )
        self.assertEqual(rc, 0, prepared)
        source = self.load_ledger(run_path)["facets"]["execution"]["tester_source"]
        worktree = Path(source["worktree"])
        (worktree / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (worktree / "tests" / "test_uv_proof.py").write_text(
            test_source, encoding="utf-8"
        )
        commit_all(worktree, f"author uv proof tests {run_id}")
        rc, integrated = self.invoke(
            "integrate-tester", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, integrated)
        ledger = self.load_ledger(run_path)
        execution = ledger["facets"]["execution"]
        source = execution["tester_source"]
        report_path = self.write_json(
            f"{run_id}-tester-evidence.json",
            {
                "schema_version": 1,
                "kind": "tester",
                "status": "pass",
                "candidate_head": execution["candidate_head"],
                "producer": {"role": "tester", **tester},
                "details": {
                    "result": "tests_ready",
                    "source_head": source["head"],
                    "files": source["files"],
                },
            },
        )
        rc, recorded = self.invoke(
            "record-evidence",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--kind",
            "tester",
            "--report",
            report_path,
        )
        self.assertEqual(rc, 0, recorded)
        rc, action = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.assertEqual(rc, 0, action)
        self.assertEqual(action.get("action"), "tester_proof", action)
        return run_path, tester, action

    def uv_baseline_spec(self, launcher: Path, *, argv: list[str] | None = None) -> dict[str, Any]:
        test_id = "tests/test_uv_proof.py::test_observation"
        return {
            "schema_version": 1,
            "groups": [
                {
                    "behavior_ids": ["driver-flow"],
                    "method": "baseline-red",
                    "argv": argv
                    or [
                        str(launcher),
                        "run",
                        "--frozen",
                        "--offline",
                        "--no-env-file",
                        "python",
                        "-m",
                        "pytest",
                        test_id,
                    ],
                    "test_ids": [test_id],
                    "timeout_seconds": 30,
                    "claimed_failure_kind": "assertion-failure",
                }
            ],
        }

    def test_tester_integration_replay_is_idempotent(self) -> None:
        run_id = "tester-integration-replay"
        contract = contract_for(self.repo)
        contract["execution"]["driver_enforced"] = False
        _data, run_path = self.start(run_id, contract=contract)
        integrated = self.prepare_and_record_tester(run_id, run_path)
        expected_head = integrated["facets"]["execution"]["candidate_head"]
        rc, replayed = self.invoke(
            "integrate-tester", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, replayed)
        self.assertEqual(
            self.load_ledger(run_path)["facets"]["execution"]["candidate_head"],
            expected_head,
        )

    def skill_text(self) -> str:
        return SKILL.read_text(encoding="utf-8")

    def test_experiment_skill_is_installable_explicit_and_never_implicit(self) -> None:
        self.assertTrue(SKILL.is_file())
        self.assertTrue(SKILL_MANIFEST.is_file())
        manifest = compact(SKILL_MANIFEST.read_text(encoding="utf-8"))
        self.assertIn("allow_implicit_invocation:false", manifest)
        self.assertIn("$full-driver-v4-experiment", manifest)

        with tempfile.TemporaryDirectory(prefix="full-driver-install-") as raw_home:
            home = Path(raw_home)
            (home / ".codex").mkdir()
            env = {
                **os.environ,
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
            }
            installed = run_process(["bash", ROOT / "install.sh"], cwd=ROOT, env=env)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            link = home / ".agents" / "skills" / "full-driver-v4-experiment"
            self.assertTrue(link.is_symlink(), link)
            self.assertEqual(link.resolve(), SKILL.parent.resolve())

    def test_experimental_builder_plan_open_without_public_cli_or_legacy_start(self) -> None:
        top_help = run_process([sys.executable, CLI, "--help"])
        self.assertEqual(top_help.returncode, 0, top_help.stderr)
        self.assertNotIn("assurance", top_help.stdout)

        legacy = run_process(
            [
                sys.executable,
                CLI,
                "start",
                "--repo",
                self.repo,
                "--plan",
                self.artifacts / "missing-plan.md",
                "--task",
                "public builder remains closed",
                "--session-id",
                "public-builder-closed",
            ]
        )
        data = json.loads([line for line in legacy.stdout.splitlines() if line][-1])
        self.assertEqual(legacy.returncode, 2, data)
        self.assertEqual(data.get("status"), "FATAL", data)
        self.assertIn(data.get("code"), {"BUILDER_MAINTENANCE_DISABLED", "BUILDER_START_DISABLED"})

        rc, data = self.invoke("status", "--repo", self.repo, "--run", "missing", experimental=False)
        self.assertEqual(rc, 2, data)
        self.assertEqual(data.get("code"), "ASSURANCE_V4_EXPERIMENTAL_REQUIRED")
        builder = (ROOT / "skills" / "builder" / "SKILL.md").read_text()
        planner = (ROOT / "skills" / "builder-loop-planner" / "SKILL.md").read_text()
        self.assertIn("full-driver-v4-experiment", builder)
        self.assertIn("native-driver start", builder)
        self.assertIn("创建 run 前", builder)
        self.assertIn("assurance-v4-contract", planner)
        self.assertIn("BUILDER_HANDOFF_READY", planner)

    def test_skill_automatically_loops_over_the_complete_action_surface(self) -> None:
        text = compact(self.skill_text())
        self.assertRegex(text, r"(?:重复|持续).{0,40}driver-next")
        for action in (
            "builder_implement",
            "builder_fix",
            "tester_author",
            "tester_fix",
            "verify_machine",
            "tester_blackbox",
            "reviewer_final",
            "rematerialize_target",
            "recover_finalize",
            "architecture_review",
            "finalize",
        ):
            self.assertIn(action, text)
        self.assertRegex(text, r"checkpoint[-_]builder")
        self.assertIn("full_driver_v4_result:finalized", text)

    def test_skill_handles_l1_and_authorized_dirty_without_unnecessary_user_stop(self) -> None:
        text = compact(self.skill_text())
        self.assertIn("l1", text)
        self.assertRegex(text, r"dirty.{0,100}(?:snapshot|intake|复制|隔离)")
        self.assertNotRegex(text, r"(?:l1|authorizeddirty|已授权dirty).{0,80}needs_user")

    def test_skill_handles_parallel_and_serial_tester_publication(self) -> None:
        text = compact(self.skill_text())
        self.assertRegex(text, r"(?:parallel_ready|parallel|并行)")
        self.assertRegex(text, r"(?:parallel_ready:true|parallel|并行).{0,200}tester")
        self.assertRegex(text, r"(?:parallel_ready:false|serial|串行).{0,240}(?:publication|发布)")
        self.assertRegex(text, r"(?:manifest|blob).{0,120}(?:publication|发布)")

    def test_skill_supports_protected_preparation_and_single_use_continuation(self) -> None:
        text = compact(self.skill_text())
        self.assertNotRegex(text, r"protectedpreparation.{0,40}unsupported")
        self.assertRegex(text, r"protected.{0,160}(?:prepare|preparation|准备)")
        self.assertRegex(text, r"continuation.{0,160}(?:single|一次|单次|消费)")
        self.assertRegex(text, r"(?:token|continuation).{0,160}(?:replay|重复|二次).{0,80}(?:拒绝|reject)")

    def test_skill_preserves_role_threads_and_explicit_replacement(self) -> None:
        text = compact(self.skill_text())
        self.assertRegex(text, r"tester.{0,120}(?:same-thread|同一thread|同thread|续接)")
        self.assertRegex(text, r"reviewer.{0,120}(?:same-thread|同一thread|同thread|续接)")
        self.assertIn("prepare-tester--replace", text)
        self.assertRegex(text, r"(?:dirty|漂移).{0,100}(?:保留|停止)")

    def test_skill_routes_structured_problems_without_redefining_mission(self) -> None:
        text = compact(self.skill_text())
        self.assertRegex(text, r"(?:problem_report|problem-report|结构化问题)")
        for owner in ("builder", "tester", "plan", "current_project", "builder_loop"):
            self.assertIn(owner, text)
        self.assertRegex(text, r"tester.{0,120}(?:tester_fix|回到tester)")
        self.assertRegex(text, r"builder.{0,120}(?:builder_fix|回到builder)")
        self.assertRegex(text, r"(?:普通测试修正|testfix|fixture).{0,100}(?:不修改mission|mission不变)")

    def test_skill_orders_proof_blackbox_and_reviewer_prerequisites(self) -> None:
        text = compact(self.skill_text())
        text = text[text.index("##原生持续循环") :]
        tester_index = text.index("tester_author")
        proof_match = re.search(
            r"(?:prove-tests|tester_proof|proof|测试证明|证明门禁)",
            text[tester_index:],
        )
        self.assertIsNotNone(proof_match)
        if proof_match is None:
            raise AssertionError("proof gate contract missing")
        order = [
            tester_index,
            tester_index + proof_match.start(),
            text.index("verify_machine"),
            text.index("tester_blackbox"),
            text.index("reviewer_final"),
        ]
        self.assertEqual(order, sorted(order), order)
        self.assertRegex(text, r"reviewer.{0,160}(?:前置|prerequisite).{0,100}(?:齐全|current|完整)")

    def test_needs_user_is_limited_to_frozen_decision_boundaries(self) -> None:
        text = compact(self.skill_text())
        marker = text.index("full_driver_v4_result:needs_user")
        boundary = text[max(0, marker - 700) : marker + 300]
        for pattern in (
            r"(?:mission|目标)",
            r"(?:authority|授权)",
            r"(?:product|产品)",
            r"git",
            r"(?:no-progress|无进展)",
            r"(?:continuity|连续性)",
        ):
            self.assertRegex(boundary, pattern)
        self.assertRegex(
            text,
            r"checkpoint_builder.{0,240}recover_finalize.{0,100}(?:不输出needs_user|不请求用户|不形成用户中断)",
        )

    def test_publication_records_exact_prerequisite_manifest(self) -> None:
        run_id = "full-driver-publication"
        contract = contract_for(self.repo)
        contract["authority"]["public_prerequisites"] = ["src/calc.py"]
        data, run_path = self.start(run_id, contract=contract)
        candidate = Path(data["candidate_worktree"])
        (candidate / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\nPUBLIC_API = 1\n",
            encoding="utf-8",
        )
        commit_all(candidate, "publish serial prerequisite")
        rc, checkpointed = self.invoke(
            "checkpoint-builder", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, checkpointed)

        rc, published = self.invoke(
            "publish-prerequisites", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, published)
        ledger = self.load_ledger(run_path)
        publication = ledger.get("publication")
        self.assertIsInstance(publication, dict)
        encoded = json.dumps(publication, sort_keys=True)
        self.assertIn("src/calc.py", encoded)
        self.assertRegex(encoded, r"[0-9a-f]{40}")
        self.assertRegex(encoded, r"[0-9a-f]{64}")

    def test_reviewer_identity_requires_same_thread_or_explicit_replacement(self) -> None:
        run_id = "full-driver-reviewer-continuity"
        contract = contract_for(self.repo)
        contract["mission"]["objective"] = "Review an L1 fixture."
        contract["authority"]["builder_write"] = ["README.md"]
        contract["authority"]["tester_write"] = []
        contract["assurance"] = {"required": ["reviewer"], "machine_commands": []}
        contract["execution"]["commands"] = []
        _data, run_path = self.start(run_id, contract=contract)
        rc, checkpointed = self.invoke(
            "checkpoint-builder", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, checkpointed)
        rc, action = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, action)
        self.assertEqual(action.get("action"), "reviewer_final", action)
        rc, prepared = self.invoke(
            "prepare-reviewer",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--agent-id",
            "full-driver-reviewer",
            "--thread-id",
            "full-driver-reviewer-thread",
            "--action-id",
            action["action_id"],
        )
        self.assertEqual(rc, 0, prepared)
        rc, rejected = self.invoke(
            "prepare-reviewer",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--agent-id",
            "replacement-reviewer",
            "--thread-id",
            "replacement-reviewer-thread",
        )
        self.assertEqual(rc, 1, rejected)
        self.assertEqual(rejected.get("status"), "NEEDS_USER", rejected)
        rc, replaced = self.invoke(
            "prepare-reviewer",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--agent-id",
            "replacement-reviewer",
            "--thread-id",
            "replacement-reviewer-thread",
            "--replace",
        )
        self.assertEqual(rc, 0, replaced)
        reviewer = self.load_ledger(run_path)["facets"]["execution"]["agents"][
            "reviewer"
        ]
        self.assertEqual(
            reviewer,
            {
                "agent_id": "replacement-reviewer",
                "thread_id": "replacement-reviewer-thread",
            },
        )

    def test_structured_problem_report_is_persisted_with_role_identity(self) -> None:
        run_id = "full-driver-problems"
        core_contract = contract_for(self.repo)
        core_contract["execution"]["driver_enforced"] = False
        _data, run_path = self.start(run_id, contract=core_contract)
        report = {
            "schema_version": 1,
            "problems": [
                {
                    "key": "tester-fixture-failure",
                    "summary": "Tester fixture needs correction",
                    "details": "The frozen target is unchanged; repair Tester-owned code.",
                    "owner": "tester",
                }
            ],
        }
        report_path = self.write_json("full-driver-problems.json", report)
        rc, recorded = self.invoke(
            "record-problems",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--report",
            report_path,
            "--role",
            "tester",
            "--agent-id",
            "full-driver-tester",
            "--thread-id",
            "full-driver-tester-thread",
        )
        self.assertEqual(rc, 0, recorded)
        problems = self.load_ledger(run_path).get("problems")
        self.assertIsInstance(problems, list)
        self.assertEqual(problems[0]["owner"], "tester")
        self.assertIn("tester-fixture-failure", json.dumps(problems))

    def test_problem_report_replay_is_idempotent_conflict_safe_and_candidate_scoped(self) -> None:
        run_id = "problem-replay-boundary"
        contract = contract_for(self.repo)
        contract["execution"]["driver_enforced"] = False
        data, run_path = self.start(run_id, contract=contract)
        report = {
            "schema_version": 1,
            "problems": [
                {
                    "key": "builder-regression",
                    "summary": "Builder behavior regressed.",
                    "details": "The current candidate fails the frozen behavior.",
                    "owner": "builder",
                }
            ],
        }
        report_path = self.write_json("problem-replay-report.json", report)

        def record(path: Path) -> tuple[int, dict[str, Any]]:
            return self.invoke(
                "record-problems",
                "--repo",
                self.repo,
                "--run",
                run_id,
                "--report",
                path,
                "--role",
                "builder",
                "--agent-id",
                "problem-replay-builder",
                "--thread-id",
                "problem-replay-builder-thread",
            )

        rc, first = record(report_path)
        self.assertEqual(rc, 0, first)
        first_ledger = self.load_ledger(run_path)
        self.assertEqual(len(first_ledger["problems"]), 1)
        before_replay = (run_path / "ledger.json").read_bytes()

        rc, replay = record(report_path)

        self.assertEqual(rc, 0, replay)
        self.assertEqual((run_path / "ledger.json").read_bytes(), before_replay)
        self.assertEqual(len(self.load_ledger(run_path)["problems"]), 1)

        conflicting = deepcopy(report)
        conflicting["problems"][0]["details"] = (
            "The same identity reported materially different facts."
        )
        conflicting_path = self.write_json("problem-replay-conflict.json", conflicting)
        before_conflict = (run_path / "ledger.json").read_bytes()
        rc, rejected = record(conflicting_path)
        self.assertNotEqual(rc, 0, rejected)
        self.assertEqual(rejected.get("code"), "PROBLEM_REPLAY_MISMATCH", rejected)
        self.assertEqual((run_path / "ledger.json").read_bytes(), before_conflict)

        candidate = Path(data["candidate_worktree"])
        (candidate / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\nPROBLEM_RECURRENCE = 1\n",
            encoding="utf-8",
        )
        new_head = commit_all(candidate, "advance candidate for problem recurrence")
        rc, checkpointed = self.invoke(
            "checkpoint-builder", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, checkpointed)

        rc, recurrence = record(report_path)

        self.assertEqual(rc, 0, recurrence)
        problems = self.load_ledger(run_path)["problems"]
        self.assertEqual(len(problems), 2)
        self.assertEqual(problems[0]["status"], "resolved")
        self.assertEqual(problems[1]["status"], "open")
        self.assertNotEqual(problems[0]["candidate_head"], problems[1]["candidate_head"])
        self.assertEqual(problems[1]["candidate_head"], new_head)

    def test_failed_evidence_routes_repair_until_its_dependencies_change(self) -> None:
        run_id = "failed-machine-dependency"
        contract = contract_for(self.repo)
        contract["execution"]["driver_enforced"] = False
        contract["assurance"]["machine_commands"] = [
            {
                "id": "fixture-tests",
                "argv": [sys.executable, "-c", "raise SystemExit(9)"],
                "timeout_seconds": 30,
            }
        ]
        _data, run_path = self.start(run_id, contract=contract)
        rc, checkpointed = self.invoke(
            "checkpoint-builder", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, checkpointed)
        self.prepare_and_record_tester(run_id, run_path)
        rc, failed = self.invoke(
            "verify-machine", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, failed)
        self.assertEqual(failed["readiness"]["states"]["machine"], "failed")
        rc, repair = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.assertEqual(rc, 0, repair)
        self.assertEqual(repair.get("action"), "builder_fix", repair)

        assurance = deepcopy(self.load_ledger(run_path)["facets"]["assurance"])
        assurance["machine_commands"][0]["argv"] = [
            sys.executable,
            "-c",
            "raise SystemExit(7)",
        ]
        assurance_path = self.write_json("changed-machine-command.json", assurance)
        rc, changed = self.invoke(
            "update-facet",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--facet",
            "assurance",
            "--value",
            assurance_path,
            "--authorize-downgrade",
        )
        self.assertEqual(rc, 0, changed)
        self.assertEqual(changed["readiness"]["states"]["machine"], "stale")
        rc, rerun = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.assertEqual(rc, 0, rerun)
        self.assertEqual(rerun.get("action"), "verify_machine", rerun)

    def test_failed_machine_is_stale_after_candidate_or_tester_source_changes(self) -> None:
        for change in ("candidate", "tester-source"):
            with self.subTest(change=change):
                run_id = f"failed-machine-{change}"
                contract = contract_for(self.repo)
                contract["execution"]["driver_enforced"] = False
                contract["assurance"]["machine_commands"] = [
                    {
                        "id": "fixture-tests",
                        "argv": [sys.executable, "-c", "raise SystemExit(9)"],
                        "timeout_seconds": 30,
                    }
                ]
                data, run_path = self.start(run_id, contract=contract)
                rc, checkpointed = self.invoke(
                    "checkpoint-builder", "--repo", self.repo, "--run", run_id
                )
                self.assertEqual(rc, 0, checkpointed)
                before = self.load_ledger(run_path)
                candidate_before = before["facets"]["execution"]["candidate_head"]
                rc, failed = self.invoke(
                    "verify-machine", "--repo", self.repo, "--run", run_id
                )
                self.assertEqual(rc, 0, failed)
                self.assertEqual(
                    failed["readiness"]["states"]["machine"], "failed"
                )

                if change == "candidate":
                    candidate = Path(data["candidate_worktree"])
                    (candidate / "src" / "calc.py").write_text(
                        "def add(a, b):\n    return a + b\n\nCANDIDATE_CHANGED = 1\n",
                        encoding="utf-8",
                    )
                    commit_all(candidate, "advance failed-machine candidate")
                    rc, changed = self.invoke(
                        "checkpoint-builder", "--repo", self.repo, "--run", run_id
                    )
                    self.assertEqual(rc, 0, changed)
                else:
                    tester = before["facets"]["execution"]["agents"]["tester"]
                    rc, changed = self.invoke(
                        "prepare-tester",
                        "--repo",
                        self.repo,
                        "--run",
                        run_id,
                        "--agent-id",
                        tester["agent_id"],
                        "--thread-id",
                        tester["thread_id"],
                    )
                    self.assertEqual(rc, 0, changed)
                    self.assertEqual(
                        self.load_ledger(run_path)["facets"]["execution"][
                            "candidate_head"
                        ],
                        candidate_before,
                    )

                rc, status_value = self.invoke(
                    "status", "--repo", self.repo, "--run", run_id
                )
                self.assertEqual(rc, 0, status_value)
                self.assertEqual(
                    status_value["readiness"]["states"]["machine"], "stale"
                )

    def test_failed_blackbox_is_stale_after_execution_dependency_change(self) -> None:
        run_id = "failed-blackbox-dependency"
        contract = contract_for(self.repo)
        contract["execution"]["driver_enforced"] = False
        _data, run_path = self.start(run_id, contract=contract)
        rc, checkpointed = self.invoke(
            "checkpoint-builder", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, checkpointed)
        ledger = self.prepare_and_record_tester(run_id, run_path)
        rc, machine = self.invoke(
            "verify-machine", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, machine)
        ledger = self.load_ledger(run_path)
        candidate = ledger["facets"]["execution"]["candidate_head"]
        command = ledger["facets"]["execution"]["commands"][0]
        tester = ledger["facets"]["execution"]["agents"]["tester"]
        failed_report = {
            "schema_version": 1,
            "kind": "blackbox",
            "status": "fail",
            "candidate_head": candidate,
            "producer": {"role": "tester", **tester},
            "details": {
                "result": "fail",
                "worktree": ledger["candidate_worktree"],
                "before_head": candidate,
                "after_head": candidate,
                "executions": [
                    {
                        "id": command["id"],
                        "argv": command["argv"],
                        "returncode": 9,
                        "timed_out": False,
                    }
                ],
            },
        }
        report_path = self.write_json("failed-blackbox-report.json", failed_report)
        rc, recorded = self.invoke(
            "record-evidence",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--kind",
            "blackbox",
            "--report",
            report_path,
        )
        self.assertEqual(rc, 0, recorded)
        self.assertEqual(recorded["readiness"]["states"]["blackbox"], "failed")
        rc, repair = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.assertEqual(rc, 0, repair)
        self.assertEqual(repair.get("action"), "builder_fix", repair)

        execution = deepcopy(self.load_ledger(run_path)["facets"]["execution"])
        execution["version"] += 1
        execution["commands"][0]["argv"] = ["bash", "verify.sh", "--blackbox"]
        execution_path = self.write_json("changed-blackbox-command.json", execution)
        rc, changed = self.invoke(
            "update-facet",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--facet",
            "execution",
            "--value",
            execution_path,
        )
        self.assertEqual(rc, 0, changed)
        self.assertEqual(changed["readiness"]["states"]["blackbox"], "stale")
        rc, rerun = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.assertEqual(rc, 0, rerun)
        self.assertEqual(rerun.get("action"), "tester_blackbox", rerun)

    def test_proof_is_a_distinct_gate_after_tester_and_before_blackbox(self) -> None:
        run_id = "full-driver-proof"
        contract = contract_for(self.repo)
        contract["assurance"]["required"].insert(2, "proof")
        _data, run_path = self.start(run_id, contract=contract)
        rc, checkpointed = self.invoke(
            "checkpoint-builder", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, checkpointed)
        ledger = self.prepare_and_record_tester(run_id, run_path)
        execution = ledger["facets"]["execution"]
        tester = execution["agents"]["tester"]
        arbitrary_summary = {
            "schema_version": 1,
            "kind": "proof",
            "status": "pass",
            "candidate_head": execution["candidate_head"],
            "producer": {"role": "tester", **tester},
            "details": {
                "result": "pass",
                "source_head": execution["tester_source"]["head"],
                "report_digest": "a" * 64,
                "behaviors": ["driver-flow"],
            },
        }
        arbitrary_path = self.write_json(
            "full-driver-arbitrary-proof.json", arbitrary_summary
        )
        rc, rejected = self.invoke(
            "record-evidence",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--kind",
            "proof",
            "--report",
            arbitrary_path,
        )
        self.assertNotEqual(rc, 0, rejected)
        self.assertNotIn("proof", self.load_ledger(run_path)["evidence"])

        rc, decision = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, decision)
        self.assertEqual(decision.get("action"), "tester_proof", decision)

        spec = {
            "schema_version": 1,
            "groups": [
                {
                    "behavior_ids": ["driver-flow"],
                    "method": "mutation",
                    "argv": [
                        sys.executable,
                        "-m",
                        "unittest",
                        "tests.test_full_driver_fixture.FullDriverFixtureTest.test_add",
                    ],
                    "test_ids": [
                        "tests.test_full_driver_fixture.FullDriverFixtureTest.test_add"
                    ],
                    "timeout_seconds": 30,
                    "patch": (
                        "diff --git a/src/calc.py b/src/calc.py\n"
                        "--- a/src/calc.py\n"
                        "+++ b/src/calc.py\n"
                        "@@ -1,2 +1,2 @@\n"
                        " def add(a, b):\n"
                        "-    return a + b\n"
                        "+    return a + b + 1\n"
                    ),
                }
            ],
        }
        spec_path = self.write_json("full-driver-mutation-proof.json", spec)
        rc, proved = self.invoke(
            "prove-tests",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--spec",
            spec_path,
            "--agent-id",
            tester["agent_id"],
            "--thread-id",
            tester["thread_id"],
            "--action-id",
            decision["action_id"],
        )
        self.assertEqual(rc, 0, proved)
        self.assertEqual(proved["readiness"]["states"]["proof"], "pass")
        proof = self.load_ledger(run_path)["evidence"]["proof"]
        self.assertEqual(proof["status"], "pass")
        self.assertNotEqual(proof["details"]["report_digest"], "a" * 64)
        self.assertRegex(proof["details"]["report_digest"], r"^[0-9a-f]{64}$")

        rc, advanced = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, advanced)
        self.assertEqual(advanced.get("action"), "verify_machine", advanced)

    def test_absolute_frozen_offline_uv_proof_accepts_only_real_assertion_counterexample(self) -> None:
        run_id = "uv-proof-assertion"
        launcher = self.make_uv_launcher("uv-proof-assertion")
        run_path, tester, action = self.prepare_uv_proof_run(
            run_id,
            baseline_source="def add(a, b):\n    return a + b - 1\n",
            candidate_source="def add(a, b):\n    return a + b\n",
            test_source=(
                "from src.calc import add\n\n"
                "def test_observation():\n"
                "    assert add(1, 2) == 3\n"
            ),
        )
        spec_path = self.write_json(
            "uv-proof-assertion-spec.json", self.uv_baseline_spec(launcher)
        )

        rc, proved = self.invoke(
            "prove-tests",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--spec",
            spec_path,
            "--agent-id",
            tester["agent_id"],
            "--thread-id",
            tester["thread_id"],
            "--action-id",
            action["action_id"],
        )

        self.assertEqual(rc, 0, proved)
        proof = self.load_ledger(run_path)["evidence"]["proof"]
        result = proof["details"]["results"][0]
        self.assertEqual(result["candidate"]["test_result"]["classification"], "pass")
        self.assertEqual(
            result["counterexample"]["test_result"]["classification"],
            "assertion-failure",
        )
        self.assertEqual(
            proof["details"]["spec"]["groups"][0]["argv"][0], str(launcher)
        )

    def test_uv_baseline_import_collection_and_zero_collector_failures_are_not_counterexamples(self) -> None:
        cases = (
            (
                "import-error",
                "def add(a, b):\n    return a + b\n",
                "READY = True\n\ndef add(a, b):\n    return a + b\n",
                "from src.calc import READY\n\ndef test_observation():\n    assert READY\n",
                "import-error",
            ),
            (
                "collection-error",
                "MODE = 'baseline'\n\ndef add(a, b):\n    return a + b\n",
                "MODE = 'candidate'\n\ndef add(a, b):\n    return a + b\n",
                (
                    "from src import calc\n\n"
                    "if calc.MODE != 'candidate':\n"
                    "    raise RuntimeError('collection failed')\n\n"
                    "def test_observation():\n"
                    "    assert calc.add(1, 2) == 3\n"
                ),
                "collection-error",
            ),
            (
                "zero-collectors",
                "COLLECT = False\n\ndef add(a, b):\n    return a + b\n",
                "COLLECT = True\n\ndef add(a, b):\n    return a + b\n",
                (
                    "from src import calc\n\n"
                    "if calc.COLLECT:\n"
                    "    def test_observation():\n"
                    "        assert calc.add(1, 2) == 3\n"
                ),
                "zero-tests",
            ),
        )
        for label, baseline, candidate, source, expected_classification in cases:
            with self.subTest(label=label):
                run_id = f"uv-counterexample-{label}"
                launcher = self.make_uv_launcher(run_id)
                run_path, tester, action = self.prepare_uv_proof_run(
                    run_id,
                    baseline_source=baseline,
                    candidate_source=candidate,
                    test_source=source,
                )
                spec_path = self.write_json(
                    f"{run_id}-spec.json", self.uv_baseline_spec(launcher)
                )
                before = (run_path / "ledger.json").read_bytes()

                rc, rejected = self.invoke(
                    "prove-tests",
                    "--repo",
                    self.repo,
                    "--run",
                    run_id,
                    "--spec",
                    spec_path,
                    "--agent-id",
                    tester["agent_id"],
                    "--thread-id",
                    tester["thread_id"],
                    "--action-id",
                    action["action_id"],
                )

                self.assertNotEqual(rc, 0, rejected)
                self.assertEqual(
                    rejected.get("code"), "TEST_PROOF_COUNTEREXAMPLE_INVALID", rejected
                )
                self.assertEqual(
                    rejected.get("result", {})
                    .get("test_result", {})
                    .get("classification"),
                    expected_classification,
                    rejected,
                )
                self.assertEqual((run_path / "ledger.json").read_bytes(), before)
                self.assertNotIn("proof", self.load_ledger(run_path)["evidence"])

    def test_uv_proof_command_and_project_binding_fail_closed_before_recording(self) -> None:
        run_id = "uv-command-fail-closed"
        marker = self.artifacts / "invalid-uv-invoked"
        launcher = self.make_uv_launcher(
            run_id, invocation_marker=marker
        )
        run_path, tester, action = self.prepare_uv_proof_run(
            run_id,
            baseline_source="def add(a, b):\n    return a + b - 1\n",
            candidate_source="def add(a, b):\n    return a + b\n",
            test_source=(
                "from src.calc import add\n\n"
                "def test_observation():\n"
                "    assert add(1, 2) == 3\n"
            ),
        )
        test_id = "tests/test_uv_proof.py::test_observation"
        suffix = ["python", "-m", "pytest", test_id]
        variants = {
            "relative-uv": ["uv", "run", "--frozen", "--offline", "--no-env-file", *suffix],
            "missing-frozen": [str(launcher), "run", "--offline", "--no-env-file", *suffix],
            "missing-offline": [str(launcher), "run", "--frozen", "--no-env-file", *suffix],
            "missing-no-env-file": [str(launcher), "run", "--frozen", "--offline", *suffix],
            "extra-env": [str(launcher), "run", "--frozen", "--offline", "--no-env-file", "--env-file", ".env", *suffix],
            "extra-index": [str(launcher), "run", "--frozen", "--offline", "--no-env-file", "--index", "https://example.invalid/simple", *suffix],
            "extra-with": [str(launcher), "run", "--frozen", "--offline", "--no-env-file", "--with", "pytest", *suffix],
            "extra-directory": [str(launcher), "run", "--frozen", "--offline", "--no-env-file", "--directory", ".", *suffix],
        }
        for label, argv in variants.items():
            with self.subTest(label=label):
                marker.unlink(missing_ok=True)
                spec_path = self.write_json(
                    f"uv-command-{label}.json",
                    self.uv_baseline_spec(launcher, argv=argv),
                )
                before = (run_path / "ledger.json").read_bytes()
                rc, rejected = self.invoke(
                    "prove-tests",
                    "--repo",
                    self.repo,
                    "--run",
                    run_id,
                    "--spec",
                    spec_path,
                    "--agent-id",
                    tester["agent_id"],
                    "--thread-id",
                    tester["thread_id"],
                    "--action-id",
                    action["action_id"],
                )
                self.assertNotEqual(rc, 0, rejected)
                self.assertEqual((run_path / "ledger.json").read_bytes(), before)
                self.assertNotIn("proof", self.load_ledger(run_path)["evidence"])
                self.assertFalse(marker.exists(), (label, rejected))

        for bad_path in ("pyproject.toml", "uv.lock"):
            for mode in ("missing", "symlink"):
                with self.subTest(path=bad_path, mode=mode):
                    invalid_run = f"uv-project-{bad_path.split('.')[0]}-{mode}"
                    project_modes = {
                        "pyproject.toml": "regular",
                        "uv.lock": "regular",
                    }
                    project_modes[bad_path] = mode
                    invalid_path, invalid_tester, invalid_action = self.prepare_uv_proof_run(
                        invalid_run,
                        baseline_source="def add(a, b):\n    return a + b - 1\n",
                        candidate_source="def add(a, b):\n    return a + b\n",
                        test_source=(
                            "from src.calc import add\n\n"
                            "def test_observation():\n"
                            "    assert add(1, 2) == 3\n"
                        ),
                        project_modes=project_modes,
                    )
                    invalid_launcher = self.make_uv_launcher(invalid_run)
                    spec_path = self.write_json(
                        f"{invalid_run}-spec.json",
                        self.uv_baseline_spec(invalid_launcher),
                    )
                    before = (invalid_path / "ledger.json").read_bytes()
                    rc, rejected = self.invoke(
                        "prove-tests",
                        "--repo",
                        self.repo,
                        "--run",
                        invalid_run,
                        "--spec",
                        spec_path,
                        "--agent-id",
                        invalid_tester["agent_id"],
                        "--thread-id",
                        invalid_tester["thread_id"],
                        "--action-id",
                        invalid_action["action_id"],
                    )
                    self.assertNotEqual(rc, 0, rejected)
                    self.assertEqual(
                        (invalid_path / "ledger.json").read_bytes(), before
                    )
                    self.assertNotIn(
                        "proof", self.load_ledger(invalid_path)["evidence"]
                    )

        silent_run = "uv-structured-event-missing"
        silent_path, silent_tester, silent_action = self.prepare_uv_proof_run(
            silent_run,
            baseline_source="def add(a, b):\n    return a + b - 1\n",
            candidate_source="def add(a, b):\n    return a + b\n",
            test_source=(
                "from src.calc import add\n\n"
                "def test_observation():\n"
                "    assert add(1, 2) == 3\n"
            ),
        )
        silent_launcher = self.make_uv_launcher(silent_run, execute_child=False)
        silent_spec = self.write_json(
            "uv-structured-event-missing-spec.json",
            self.uv_baseline_spec(silent_launcher),
        )
        before_silent = (silent_path / "ledger.json").read_bytes()
        rc, silent = self.invoke(
            "prove-tests",
            "--repo",
            self.repo,
            "--run",
            silent_run,
            "--spec",
            silent_spec,
            "--agent-id",
            silent_tester["agent_id"],
            "--thread-id",
            silent_tester["thread_id"],
            "--action-id",
            silent_action["action_id"],
        )
        self.assertNotEqual(rc, 0, silent)
        self.assertEqual((silent_path / "ledger.json").read_bytes(), before_silent)
        self.assertNotIn("proof", self.load_ledger(silent_path)["evidence"])

    def test_prove_tests_rejects_unbound_reviewed_boundary_ids(self) -> None:
        run_id = "full-driver-reviewed-boundaries"
        contract = contract_for(self.repo)
        contract["assurance"]["required"].insert(2, "proof")
        _data, run_path = self.start(run_id, contract=contract)
        rc, checkpointed = self.invoke(
            "checkpoint-builder", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, checkpointed)
        ledger = self.prepare_and_record_tester(run_id, run_path)
        tester = ledger["facets"]["execution"]["agents"]["tester"]
        rc, action = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, action)
        self.assertEqual(action.get("action"), "tester_proof", action)

        test_id = (
            "tests.test_full_driver_fixture.FullDriverFixtureTest.test_add"
        )
        spec_path = self.write_json(
            "full-driver-unbound-reviewed-boundary.json",
            {
                "schema_version": 1,
                "groups": [
                    {
                        "behavior_ids": ["driver-flow"],
                        "method": "reviewed-boundaries",
                        "argv": [
                            sys.executable,
                            "-m",
                            "unittest",
                            test_id,
                        ],
                        "test_ids": [test_id],
                        "timeout_seconds": 30,
                        "reason": "Exercise all frozen observable boundaries.",
                        "reviewed_boundaries": {
                            "positive_test_ids": [test_id],
                            "negative_test_ids": [test_id],
                            "boundary_test_ids": [test_id],
                            "invariant_test_ids": [test_id, "fictional.test_id"],
                        },
                    }
                ],
            },
        )
        before = (run_path / "ledger.json").read_bytes()
        rc, rejected = self.invoke(
            "prove-tests",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--spec",
            spec_path,
            "--agent-id",
            tester["agent_id"],
            "--thread-id",
            tester["thread_id"],
            "--action-id",
            action["action_id"],
        )
        self.assertNotEqual(rc, 0, rejected)
        self.assertEqual(
            rejected.get("code"), "TEST_PROOF_BOUNDARY_TEST_IDS_INVALID", rejected
        )
        self.assertEqual((run_path / "ledger.json").read_bytes(), before)
        self.assertNotIn("proof", self.load_ledger(run_path)["evidence"])

    def test_prove_tests_rejects_false_out_of_scope_and_stale_proofs_without_evidence(self) -> None:
        run_id = "full-driver-false-proof"
        contract = contract_for(self.repo)
        contract["assurance"]["required"].insert(2, "proof")
        _data, run_path = self.start(run_id, contract=contract)
        rc, checkpointed = self.invoke(
            "checkpoint-builder", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, checkpointed)
        ledger = self.prepare_and_record_tester(run_id, run_path)
        execution = ledger["facets"]["execution"]
        tester = execution["agents"]["tester"]
        rc, action = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, action)
        self.assertEqual(action.get("action"), "tester_proof", action)

        def assert_rejected(
            label: str,
            patch: str,
            expected_code: str,
            expected_classification: str | None = None,
        ) -> None:
            spec = {
                "schema_version": 1,
                "groups": [
                    {
                        "behavior_ids": ["driver-flow"],
                        "method": "mutation",
                        "argv": [
                            sys.executable,
                            "-m",
                            "unittest",
                            "tests.test_full_driver_fixture.FullDriverFixtureTest.test_add",
                        ],
                        "test_ids": [
                            "tests.test_full_driver_fixture.FullDriverFixtureTest.test_add"
                        ],
                        "timeout_seconds": 30,
                        "patch": patch,
                    }
                ],
            }
            spec_path = self.write_json(f"{label}-proof.json", spec)
            rc, rejected = self.invoke(
                "prove-tests",
                "--repo",
                self.repo,
                "--run",
                run_id,
                "--spec",
                spec_path,
                "--agent-id",
                tester["agent_id"],
                "--thread-id",
                tester["thread_id"],
                "--action-id",
                action["action_id"],
            )
            self.assertNotEqual(rc, 0, rejected)
            self.assertEqual(rejected.get("code"), expected_code, rejected)
            if expected_classification is not None:
                self.assertEqual(
                    rejected.get("result", {})
                    .get("test_result", {})
                    .get("classification"),
                    expected_classification,
                    rejected,
                )
            self.assertNotIn("proof", self.load_ledger(run_path)["evidence"])

        assert_rejected(
            "surviving-mutation",
            (
                "diff --git a/src/calc.py b/src/calc.py\n"
                "--- a/src/calc.py\n"
                "+++ b/src/calc.py\n"
                "@@ -1,2 +1,2 @@\n"
                " def add(a, b):\n"
                "-    return a + b\n"
                "+    return a + b + 0\n"
            ),
            "TEST_PROOF_COUNTEREXAMPLE_INVALID",
            "pass",
        )
        assert_rejected(
            "non-assertion-mutation",
            (
                "diff --git a/src/calc.py b/src/calc.py\n"
                "--- a/src/calc.py\n"
                "+++ b/src/calc.py\n"
                "@@ -1,2 +1,2 @@\n"
                " def add(a, b):\n"
                "-    return a + b\n"
                "+    raise RuntimeError('mutation error')\n"
            ),
            "TEST_PROOF_COUNTEREXAMPLE_INVALID",
            "non-assertion-test-failure",
        )
        assert_rejected(
            "out-of-scope-mutation",
            (
                "diff --git a/README.md b/README.md\n"
                "--- a/README.md\n"
                "+++ b/README.md\n"
                "@@ -1 +1 @@\n"
                "-fixture\n"
                "+out of scope\n"
            ),
            "TEST_PROOF_MUTATION_AUTHORITY_VIOLATION",
        )

        problem_path = self.write_json(
            "stale-proof-problem.json",
            {
                "schema_version": 1,
                "problems": [
                    {
                        "key": "stale-proof-action",
                        "summary": "Invalidate the previously derived proof action.",
                        "details": "A same-thread Tester correction changes the next action.",
                        "owner": "tester",
                    }
                ],
            },
        )
        rc, recorded = self.invoke(
            "record-problems",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--report",
            problem_path,
            "--role",
            "tester",
            "--agent-id",
            tester["agent_id"],
            "--thread-id",
            tester["thread_id"],
            "--action-id",
            action["action_id"],
        )
        self.assertEqual(rc, 0, recorded)
        stale_spec = self.write_json(
            "stale-proof.json",
            {
                "schema_version": 1,
                "groups": [
                    {
                        "behavior_ids": ["driver-flow"],
                        "method": "mutation",
                        "argv": [
                            sys.executable,
                            "-m",
                            "unittest",
                            "tests.test_full_driver_fixture.FullDriverFixtureTest.test_add",
                        ],
                        "test_ids": [
                            "tests.test_full_driver_fixture.FullDriverFixtureTest.test_add"
                        ],
                        "timeout_seconds": 30,
                        "patch": (
                            "diff --git a/src/calc.py b/src/calc.py\n"
                            "--- a/src/calc.py\n"
                            "+++ b/src/calc.py\n"
                            "@@ -1,2 +1,2 @@\n"
                            " def add(a, b):\n"
                            "-    return a + b\n"
                            "+    return a + b + 1\n"
                        ),
                    }
                ],
            },
        )
        before_stale = (run_path / "ledger.json").read_bytes()
        rc, stale = self.invoke(
            "prove-tests",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--spec",
            stale_spec,
            "--agent-id",
            tester["agent_id"],
            "--thread-id",
            tester["thread_id"],
            "--action-id",
            action["action_id"],
        )
        self.assertNotEqual(rc, 0, stale)
        self.assertEqual(stale.get("code"), "DRIVER_ACTION_STALE", stale)
        self.assertEqual((run_path / "ledger.json").read_bytes(), before_stale)
        self.assertNotIn("proof", self.load_ledger(run_path)["evidence"])

    def test_protected_preparation_continuation_is_consumed_once(self) -> None:
        preparation_run = "full-driver-preparation"
        preparation = contract_for(self.repo)
        preparation["mission"]["delivery_kind"] = "preparation"
        preparation["mission"]["objective"] = "Prepare protected support once."
        preparation["authority"]["builder_write"] = ["src/support.py"]
        preparation["authority"]["tester_write"] = []
        preparation["authority"]["protected_support_paths"] = ["src/support.py"]
        preparation["assurance"] = {
            "required": ["reviewer"],
            "machine_commands": [],
        }
        preparation["execution"]["commands"] = []
        data, preparation_path = self.start(preparation_run, contract=preparation)
        candidate = Path(data["candidate_worktree"])
        (candidate / "src" / "support.py").write_text(
            "SUPPORT_VERSION = 1\n", encoding="utf-8"
        )
        commit_all(candidate, "checkpoint protected preparation")
        rc, checkpointed = self.invoke(
            "checkpoint-builder",
            "--repo",
            self.repo,
            "--run",
            preparation_run,
        )
        self.assertEqual(rc, 0, checkpointed)
        ledger = self.load_ledger(preparation_path)
        execution = ledger["facets"]["execution"]
        reviewer = execution["agents"]["reviewer"]
        review = {
            "schema_version": 1,
            "kind": "reviewer",
            "status": "pass",
            "candidate_head": execution["candidate_head"],
            "producer": {"role": "reviewer", **reviewer},
            "details": {
                "result": "pass",
                "reviewed_head": execution["candidate_head"],
            },
        }
        review_path = self.write_json("preparation-review.json", review)
        rc, reviewed = self.invoke(
            "record-evidence",
            "--repo",
            self.repo,
            "--run",
            preparation_run,
            "--kind",
            "reviewer",
            "--report",
            review_path,
        )
        self.assertEqual(rc, 0, reviewed)
        rc, finalized = self.invoke(
            "finalize",
            "--repo",
            self.repo,
            "--run",
            preparation_run,
            "--message",
            "test(full-driver): [cr_id_skip] Finalize preparation",
        )
        self.assertEqual(rc, 0, finalized)
        preparation_ledger = self.load_ledger(preparation_path)
        preparation_head = preparation_ledger["final_head"]

        delivery = contract_for(self.repo)
        delivery["execution"]["continuation"] = {
            "preparation_run_id": preparation_run,
            "preparation_final_head": preparation_head,
            "support_paths": ["src/support.py"],
        }
        _data, delivery_path = self.start(
            "full-driver-continuation", contract=delivery
        )
        preparation_after_start = self.load_ledger(preparation_path)
        consumed = preparation_after_start["continuation_consumed_by"]
        self.assertEqual(consumed, "full-driver-continuation")

        preparation_after_start["continuation_consumed_by"] = None
        preparation_after_start["continuation_consume_intent"] = {
            "business_run_id": "full-driver-continuation",
            "target_head": preparation_head,
            "contract_digest": hashlib.sha256(
                json.dumps(
                    delivery,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        }
        (preparation_path / "ledger.json").write_text(
            json.dumps(preparation_after_start, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        business_before = (delivery_path / "ledger.json").read_bytes()
        continuation_path = self.write_json("continuation-retry.json", delivery)
        rc, recovered = self.invoke(
            "start",
            "--repo",
            self.repo,
            "--run",
            "full-driver-continuation",
            "--session-id",
            "full-driver-v4-session",
            "--contract",
            continuation_path,
        )
        self.assertEqual(rc, 0, recovered)
        self.assertEqual(
            self.load_ledger(preparation_path)["continuation_consumed_by"],
            "full-driver-continuation",
        )
        self.assertEqual((delivery_path / "ledger.json").read_bytes(), business_before)

        second_path = self.write_json("second-continuation.json", delivery)
        rc, rejected = self.invoke(
            "start",
            "--repo",
            self.repo,
            "--run",
            "full-driver-continuation-replay",
            "--session-id",
            "full-driver-v4-session",
            "--contract",
            second_path,
        )
        self.assertNotEqual(rc, 0, rejected)
        self.assertEqual(
            self.load_ledger(preparation_path)["continuation_consumed_by"],
            "full-driver-continuation",
        )

    def test_dirty_intake_starts_without_mutating_target_or_requesting_user(self) -> None:
        path = "src/intake.py"
        content = "INTAKE = 1\n"
        (self.repo / path).write_text(content, encoding="utf-8")
        digest = hashlib.sha256(content.encode()).hexdigest()
        contract = contract_for(self.repo)
        contract["authority"]["dirty_intake"] = [{"path": path, "sha256": digest}]

        data, run_path = self.start("full-driver-dirty", contract=contract)
        ledger = self.load_ledger(run_path)
        self.assertEqual(ledger["facets"]["execution"]["dirty_snapshot"][0]["sha256"], digest)
        self.assertIs(ledger["builder_checkpointed"], False)
        self.assertEqual((self.repo / path).read_text(), content)
        rc, decision = self.invoke(
            "driver-next", "--repo", self.repo, "--run", "full-driver-dirty"
        )
        self.assertEqual(rc, 0, decision)
        self.assertNotEqual(decision.get("status"), "NEEDS_USER", decision)
        self.assertEqual(decision.get("action"), "builder_implement", decision)
        candidate = Path(data["candidate_worktree"])
        candidate_head_before = head(candidate)
        self.assertEqual(git(candidate, "status", "--porcelain=v1"), "")
        rc, checkpointed = self.invoke(
            "checkpoint-builder",
            "--repo",
            self.repo,
            "--run",
            "full-driver-dirty",
        )
        self.assertEqual(rc, 0, checkpointed)
        after = self.load_ledger(run_path)
        self.assertIs(after["builder_checkpointed"], True)
        self.assertEqual(head(candidate), candidate_head_before)

        rc, advanced = self.invoke(
            "driver-next", "--repo", self.repo, "--run", "full-driver-dirty"
        )
        self.assertEqual(rc, 0, advanced)
        self.assertEqual(advanced.get("action"), "tester_author", advanced)

    def test_checkpoint_builder_commits_and_classifies_uncommitted_builder_work(self) -> None:
        run_id = "full-driver-checkpoint"
        data, run_path = self.start(run_id)
        rc, initial = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, initial)
        self.assertEqual(initial.get("action"), "builder_implement", initial)

        candidate = Path(data["candidate_worktree"])
        (candidate / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b + 1\n", encoding="utf-8"
        )
        commit_all(candidate, "checkpoint Full Driver builder work")
        rc, checkpoint_action = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, checkpoint_action)
        self.assertEqual(
            checkpoint_action.get("action"), "checkpoint_builder", checkpoint_action
        )
        rc, decision = self.invoke(
            "checkpoint-builder", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, decision)
        ledger = self.load_ledger(run_path)
        self.assertEqual(ledger["facets"]["execution"]["candidate_head"], head(candidate))
        self.assertEqual(
            ledger["facets"]["execution"]["builder_files"], ["src/calc.py"]
        )
        self.assertEqual(git(candidate, "status", "--porcelain=v1"), "")
        rc, advanced = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, advanced)
        self.assertEqual(advanced.get("action"), "tester_author", advanced)

    def test_uncheckpointed_builder_fix_precedes_open_problem_redispatch_and_converges(self) -> None:
        run_id = "builder-fix-progress"
        contract = contract_for(self.repo)
        contract["execution"]["driver_enforced"] = False
        data, run_path = self.start(run_id, contract=contract)
        rc, checkpointed = self.invoke(
            "checkpoint-builder", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, checkpointed)
        report_path = self.write_json(
            "builder-fix-progress-problem.json",
            {
                "schema_version": 1,
                "problems": [
                    {
                        "key": "builder-fix-required",
                        "summary": "Builder correction is required.",
                        "details": "The committed correction must advance the candidate once.",
                        "owner": "builder",
                    }
                ],
            },
        )
        rc, recorded = self.invoke(
            "record-problems",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--report",
            report_path,
            "--role",
            "builder",
            "--agent-id",
            "builder-fix-agent",
            "--thread-id",
            "builder-fix-thread",
        )
        self.assertEqual(rc, 0, recorded)
        rc, routed = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.assertEqual(rc, 0, routed)
        self.assertEqual(routed.get("action"), "builder_fix", routed)

        candidate = Path(data["candidate_worktree"])
        (candidate / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\nFIXED_ONCE = 1\n",
            encoding="utf-8",
        )
        committed = commit_all(candidate, "commit builder fix once")

        rc, recovery = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)

        self.assertEqual(rc, 0, recovery)
        self.assertEqual(recovery.get("action"), "checkpoint_builder", recovery)
        rc, applied = self.invoke(
            "checkpoint-builder", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, applied)
        ledger = self.load_ledger(run_path)
        self.assertEqual(ledger["facets"]["execution"]["candidate_head"], committed)
        problem = next(
            item for item in ledger["problems"] if item["key"] == "builder-fix-required"
        )
        self.assertEqual(problem["status"], "resolved")
        rc, advanced = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.assertEqual(rc, 0, advanced)
        self.assertEqual(advanced.get("action"), "tester_author", advanced)
        rc, repeated = self.invoke("driver-next", "--repo", self.repo, "--run", run_id)
        self.assertEqual(rc, 0, repeated)
        self.assertNotEqual(repeated.get("action"), "builder_fix", repeated)

    def test_driver_action_id_authorizes_current_mutation_and_rejects_stale_replay(self) -> None:
        run_id = "full-driver-action-id"
        data, run_path = self.start(run_id)
        candidate = Path(data["candidate_worktree"])
        (candidate / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\nACTION_ID = 1\n",
            encoding="utf-8",
        )
        committed_head = commit_all(candidate, "checkpoint action-id fixture")

        rc, current = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, current)
        self.assertEqual(current.get("action"), "checkpoint_builder", current)
        action_id = current.get("action_id")
        self.assertIsInstance(action_id, str, current)
        self.assertTrue(action_id, current)

        before = self.load_ledger(run_path)
        rc, checkpointed = self.invoke(
            "checkpoint-builder",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action_id,
        )
        self.assertEqual(rc, 0, checkpointed)
        after = self.load_ledger(run_path)
        self.assertNotEqual(after["digests"]["execution"], before["digests"]["execution"])
        self.assertEqual(after["facets"]["execution"]["candidate_head"], committed_head)

        ledger_before_replay = (run_path / "ledger.json").read_bytes()
        candidate_head_before_replay = head(candidate)
        candidate_status_before_replay = git(
            candidate, "status", "--porcelain=v1", "--untracked-files=all"
        )
        target_head_before_replay = head(self.repo)
        worktrees_before_replay = git(self.repo, "worktree", "list", "--porcelain")

        rc, stale = self.invoke(
            "checkpoint-builder",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--action-id",
            action_id,
        )
        self.assertNotEqual(rc, 0, stale)
        self.assertEqual(stale.get("code"), "DRIVER_ACTION_STALE", stale)
        self.assertEqual((run_path / "ledger.json").read_bytes(), ledger_before_replay)
        self.assertEqual(head(candidate), candidate_head_before_replay)
        self.assertEqual(
            git(candidate, "status", "--porcelain=v1", "--untracked-files=all"),
            candidate_status_before_replay,
        )
        self.assertEqual(head(self.repo), target_head_before_replay)
        self.assertEqual(
            git(self.repo, "worktree", "list", "--porcelain"),
            worktrees_before_replay,
        )

    def test_driver_enforcement_rejects_wrong_order_without_action_id(self) -> None:
        run_id = "full-driver-implicit-action-guard"
        _data, run_path = self.start(run_id)
        ledger = self.load_ledger(run_path)
        execution = ledger["facets"]["execution"]
        reviewer = execution["agents"]["reviewer"]
        report_path = self.write_json(
            "wrong-order-reviewer-report.json",
            {
                "schema_version": 1,
                "kind": "reviewer",
                "status": "pass",
                "candidate_head": execution["candidate_head"],
                "producer": {"role": "reviewer", **reviewer},
                "details": {
                    "result": "pass",
                    "reviewed_head": execution["candidate_head"],
                },
            },
        )

        before_evidence = (run_path / "ledger.json").read_bytes()
        rc, rejected_evidence = self.invoke(
            "record-evidence",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--kind",
            "reviewer",
            "--report",
            report_path,
        )
        self.assertNotEqual(rc, 0, rejected_evidence)
        self.assertEqual(
            rejected_evidence.get("code"), "DRIVER_ACTION_STALE", rejected_evidence
        )
        self.assertEqual((run_path / "ledger.json").read_bytes(), before_evidence)

        before_mutation = (run_path / "ledger.json").read_bytes()
        rc, rejected_mutation = self.invoke(
            "prepare-reviewer",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--agent-id",
            reviewer["agent_id"],
            "--thread-id",
            reviewer["thread_id"],
        )
        self.assertNotEqual(rc, 0, rejected_mutation)
        self.assertEqual(
            rejected_mutation.get("code"), "DRIVER_ACTION_STALE", rejected_mutation
        )
        self.assertEqual((run_path / "ledger.json").read_bytes(), before_mutation)

    def test_driver_enforcement_locks_generic_execution_facet_updates(self) -> None:
        run_id = "full-driver-execution-facet-lock"
        _data, run_path = self.start(run_id)
        before = (run_path / "ledger.json").read_bytes()
        execution = deepcopy(self.load_ledger(run_path)["facets"]["execution"])
        execution["version"] += 1
        execution["driver_enforced"] = False
        execution["candidate_head"] = head(self.repo)
        value = self.write_json("driver-execution-bypass.json", execution)

        rc, rejected = self.invoke(
            "update-facet",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--facet",
            "execution",
            "--value",
            value,
        )

        self.assertNotEqual(rc, 0, rejected)
        self.assertEqual(rejected.get("code"), "DRIVER_EXECUTION_FACET_LOCKED", rejected)
        self.assertEqual((run_path / "ledger.json").read_bytes(), before)
        ledger = self.load_ledger(run_path)
        self.assertIs(ledger["facets"]["execution"]["driver_enforced"], True)
        self.assertIs(ledger["builder_checkpointed"], False)

    def test_l1_delivery_skips_tester_machine_and_blackbox(self) -> None:
        run_id = "full-driver-l1"
        contract = contract_for(self.repo)
        contract["mission"]["objective"] = "Update stable documentation only."
        contract["authority"]["builder_write"] = ["README.md"]
        contract["assurance"] = {"required": ["reviewer"], "machine_commands": []}
        contract["execution"]["commands"] = []
        data, run_path = self.start(run_id, contract=contract)
        candidate = Path(data["candidate_worktree"])
        (candidate / "README.md").write_text("L1 documentation fixture\n", encoding="utf-8")
        commit_all(candidate, "update L1 documentation")
        rc, checkpointed = self.invoke(
            "checkpoint-builder", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, checkpointed)

        rc, decision = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, decision)
        self.assertEqual(decision.get("action"), "reviewer_final", decision)
        self.assertNotIn(
            decision.get("action"), {"tester_author", "verify_machine", "tester_blackbox"}
        )

    def test_target_drift_routes_to_rematerialization_not_user(self) -> None:
        run_id = "full-driver-target-drift"
        data, run_path = self.start(run_id)
        candidate = Path(data["candidate_worktree"])
        (candidate / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\nVALUE = 4\n", encoding="utf-8"
        )
        commit_all(candidate, "checkpoint candidate")
        rc, checkpointed = self.invoke(
            "checkpoint-builder", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, checkpointed)
        (self.repo / "README.md").write_text("fixture\ntarget drift\n", encoding="utf-8")
        commit_all(self.repo, "advance target independently")

        rc, decision = self.invoke(
            "driver-next", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, decision)
        self.assertEqual(decision.get("action"), "rematerialize_target", decision)
        self.assertNotEqual(decision.get("status"), "NEEDS_USER", decision)

    def test_serial_publication_and_tester_source_rematerialize_on_target_drift(self) -> None:
        run_id = "full-driver-serial-rematerialize"
        contract = contract_for(self.repo)
        contract["authority"]["public_prerequisites"] = ["src/calc.py"]
        data, run_path = self.start(run_id, contract=contract)
        candidate = Path(data["candidate_worktree"])
        (candidate / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\nSERIAL_API = 1\n",
            encoding="utf-8",
        )
        commit_all(candidate, "checkpoint serial prerequisite")
        rc, checkpointed = self.invoke(
            "checkpoint-builder", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, checkpointed)
        rc, published = self.invoke(
            "publish-prerequisites", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, published)
        ledger = self.load_ledger(run_path)
        tester = ledger["facets"]["execution"]["agents"]["tester"]
        rc, prepared = self.invoke(
            "prepare-tester",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--agent-id",
            tester["agent_id"],
            "--thread-id",
            tester["thread_id"],
        )
        self.assertEqual(rc, 0, prepared)

        before = self.load_ledger(run_path)
        publication_before = deepcopy(before["publication"])
        tester_source_before = deepcopy(before["facets"]["execution"]["tester_source"])
        self.assertEqual(tester_source_before["base_head"], publication_before["head"])
        mission_before = deepcopy(before["facets"]["mission"])
        mission_digest_before = before["digests"]["mission"]

        (self.repo / "README.md").write_text(
            "fixture\nnonconflicting target advance\n", encoding="utf-8"
        )
        target_advanced = commit_all(self.repo, "advance target outside prerequisite")
        rc, rematerialized = self.invoke(
            "rematerialize-target", "--repo", self.repo, "--run", run_id
        )
        self.assertEqual(rc, 0, rematerialized)
        self.assertNotEqual(rematerialized.get("status"), "NEEDS_USER", rematerialized)

        after = self.load_ledger(run_path)
        publication_after = after["publication"]
        tester_source_after = after["facets"]["execution"]["tester_source"]
        self.assertEqual(after["target_start_head"], target_advanced)
        self.assertNotEqual(publication_after["head"], publication_before["head"])
        self.assertNotEqual(publication_after["tree"], publication_before["tree"])
        self.assertNotEqual(
            publication_after["candidate_head"], publication_before["candidate_head"]
        )
        self.assertEqual(publication_after["paths"], publication_before["paths"])
        self.assertEqual(publication_after["files"], publication_before["files"])
        self.assertEqual(
            publication_after["manifest_digest"],
            publication_before["manifest_digest"],
        )
        self.assertEqual(tester_source_after["base_head"], publication_after["head"])
        self.assertEqual(tester_source_after["head"], publication_after["head"])
        self.assertEqual(after["facets"]["mission"], mission_before)
        self.assertEqual(after["digests"]["mission"], mission_digest_before)


if __name__ == "__main__":
    unittest.main()
