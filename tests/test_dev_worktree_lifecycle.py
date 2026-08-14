from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "scripts" / "codex-builder-loop.py"
STATE_SCHEMA = REPO_ROOT / "schema" / "codex-dev-worktree-state.schema.json"


class DevWorktreeLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="cbl-dev-worktree-test-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "demo"
        self.repo.mkdir()
        self.git(self.repo, "init", "-q", "-b", "main")
        self.git(self.repo, "config", "user.name", "Dev Worktree Test")
        self.git(self.repo, "config", "user.email", "dev-worktree@test.local")
        self.git(self.repo, "config", "commit.gpgsign", "false")
        self.git(self.repo, "config", "core.hooksPath", "/dev/null")
        (self.repo / ".gitignore").write_text("*.cache\n", encoding="utf-8")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        self.git(self.repo, "add", ".gitignore", "README.md")
        self.git(self.repo, "commit", "--no-verify", "-qm", "initial")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def git(
        self,
        repo: Path,
        *args: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str] | str:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if check and completed.returncode != 0:
            self.fail(
                f"git {' '.join(args)} failed ({completed.returncode}):\n"
                f"stdout={completed.stdout}\nstderr={completed.stderr}"
            )
        return completed.stdout.strip() if check else completed

    def cli(
        self,
        *args: str,
        cwd: Path | None = None,
        expected: int = 0,
        fault: str | None = None,
    ) -> dict[str, object] | None:
        env = {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "CODEX_BUILDER_LOOP_TESTING": "1",
        }
        if fault is not None:
            env["CODEX_BUILDER_LOOP_DEV_WORKTREE_FAULT"] = fault
        completed = subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=cwd or self.repo,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
        self.assertEqual(
            completed.returncode,
            expected,
            msg=(
                f"CLI {' '.join(args)} returned {completed.returncode}\n"
                f"stdout={completed.stdout}\nstderr={completed.stderr}"
            ),
        )
        if not completed.stdout.strip():
            return None
        return json.loads(completed.stdout)

    def create(self, task: str = "task") -> dict[str, object]:
        payload = self.cli(
            "dev-worktree",
            "create",
            "--repo",
            str(self.repo),
            "--task",
            task,
        )
        assert payload is not None
        self.assertEqual(payload["status"], "READY")
        return payload

    def finish(self, worktree_id: str, *, expected: int = 0) -> dict[str, object]:
        payload = self.cli(
            "dev-worktree",
            "finish",
            "--repo",
            str(self.repo),
            "--id",
            worktree_id,
            expected=expected,
        )
        assert payload is not None
        return payload

    def state_path(self, worktree_id: str) -> Path:
        return (
            self.repo
            / ".git"
            / "codex-builder-loop"
            / "dev-worktrees"
            / f"{worktree_id}.json"
        )

    def commit_in_worktree(self, payload: dict[str, object], content: str = "changed\n") -> None:
        worktree = Path(str(payload["path"]))
        (worktree / "README.md").write_text(content, encoding="utf-8")
        self.git(worktree, "add", "README.md")
        self.git(worktree, "commit", "--no-verify", "-qm", "change")

    def merge_to_target(self, payload: dict[str, object]) -> None:
        self.git(self.repo, "merge", "--ff-only", str(payload["branch"]))

    def test_create_uses_neutral_managed_root_and_excludes_target_dirty(self) -> None:
        (self.repo / "README.md").write_text("dirty target\n", encoding="utf-8")
        (self.repo / "local.txt").write_text("untracked\n", encoding="utf-8")

        payload = self.create("layout")
        worktree = Path(str(payload["path"]))

        self.assertEqual((worktree / "README.md").read_text(), "base\n")
        self.assertGreaterEqual(payload["target_state"]["ordinary_count"], 2)
        self.assertTrue(payload["target_state"]["excluded_from_worktree"])
        self.assertFalse({".git", ".claude", ".codex"} & set(worktree.parts))
        self.assertEqual(worktree.parent, Path(str(payload["managed_root"])))
        self.assertEqual(payload["context_policy"]["candidate"]["root"], str(worktree))
        self.assertEqual(
            payload["context_policy"]["host_readonly"]["root"], str(self.repo)
        )

        finished = self.finish(str(payload["id"]))
        self.assertEqual(finished["status"], "COMPLETE")
        self.assertEqual((self.repo / "README.md").read_text(), "dirty target\n")
        self.assertTrue((self.repo / "local.txt").is_file())

    def test_finish_removes_only_merged_clean_worktree_and_exact_branch(self) -> None:
        payload = self.create("finish")
        self.commit_in_worktree(payload)

        blocked = self.finish(str(payload["id"]), expected=1)
        self.assertEqual(blocked["code"], "DEV_WORKTREE_NOT_MERGED")
        self.assertTrue(Path(str(payload["path"])).is_dir())

        self.merge_to_target(payload)
        finished = self.finish(str(payload["id"]))

        self.assertEqual(finished["status"], "COMPLETE")
        self.assertFalse(Path(str(payload["path"])).exists())
        self.assertFalse(self.state_path(str(payload["id"])).exists())
        branch = self.git(
            self.repo,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{payload['branch']}",
            check=False,
        )
        assert isinstance(branch, subprocess.CompletedProcess)
        self.assertNotEqual(branch.returncode, 0)

    def test_preserved_worktree_is_owned_and_can_finish_later(self) -> None:
        payload = self.create("preserve")
        preserved = self.cli(
            "dev-worktree",
            "preserve",
            "--repo",
            str(self.repo),
            "--id",
            str(payload["id"]),
            "--reason",
            "retain for comparison",
        )
        assert preserved is not None
        self.assertEqual(preserved["status"], "COMPLETE")

        status = self.cli("dev-worktree", "status", "--repo", str(self.repo))
        assert status is not None
        managed = next(item for item in status["managed"] if item["id"] == payload["id"])
        self.assertEqual(managed["phase"], "preserved")
        self.assertEqual(managed["preserve_reason"], "retain for comparison")

        finished = self.finish(str(payload["id"]))
        self.assertEqual(finished["status"], "COMPLETE")

    def test_finish_preserves_ordinary_and_ignored_residue(self) -> None:
        payload = self.create("residue")
        worktree = Path(str(payload["path"]))
        (worktree / "ordinary.txt").write_text("keep\n", encoding="utf-8")
        ordinary = self.finish(str(payload["id"]), expected=1)
        self.assertEqual(ordinary["code"], "DEV_WORKTREE_RESIDUE")
        self.assertGreater(ordinary["details"]["residue"]["ordinary_count"], 0)
        (worktree / "ordinary.txt").unlink()

        (worktree / "runtime.cache").write_text("keep\n", encoding="utf-8")
        ignored = self.finish(str(payload["id"]), expected=1)
        self.assertEqual(ignored["code"], "DEV_WORKTREE_RESIDUE")
        self.assertGreater(ignored["details"]["residue"]["ignored_count"], 0)
        (worktree / "runtime.cache").unlink()

        self.assertEqual(self.finish(str(payload["id"]))["status"], "COMPLETE")

    def test_finish_refuses_git_lock_current_cwd_and_process_cwd(self) -> None:
        payload = self.create("in-use")
        worktree = Path(str(payload["path"]))

        self.git(self.repo, "worktree", "lock", "--reason", "active editor", str(worktree))
        locked = self.finish(str(payload["id"]), expected=1)
        self.assertEqual(locked["code"], "DEV_WORKTREE_LOCKED")
        self.git(self.repo, "worktree", "unlock", str(worktree))

        current = self.cli(
            "dev-worktree",
            "finish",
            "--repo",
            str(self.repo),
            "--id",
            str(payload["id"]),
            cwd=worktree,
            expected=1,
        )
        assert current is not None
        self.assertEqual(current["code"], "DEV_WORKTREE_IN_USE")

        sleeper = subprocess.Popen(["sleep", "30"], cwd=worktree)
        try:
            process = self.finish(str(payload["id"]), expected=1)
            self.assertEqual(process["code"], "DEV_WORKTREE_IN_USE")
            self.assertTrue(
                any(user.get("pid") == sleeper.pid for user in process["details"]["users"])
            )
        finally:
            sleeper.terminate()
            sleeper.wait(timeout=5)

        self.assertEqual(self.finish(str(payload["id"]))["status"], "COMPLETE")

    def test_create_intent_recovers_before_and_after_git_add(self) -> None:
        for fault in ("after_create_intent", "after_worktree_add"):
            with self.subTest(fault=fault):
                result = self.cli(
                    "dev-worktree",
                    "create",
                    "--repo",
                    str(self.repo),
                    "--task",
                    fault,
                    expected=97,
                    fault=fault,
                )
                self.assertIsNone(result)
                state_paths = sorted(
                    (self.repo / ".git" / "codex-builder-loop" / "dev-worktrees").glob(
                        "*.json"
                    )
                )
                self.assertEqual(len(state_paths), 1)
                worktree_id = state_paths[0].stem

                recovered = self.cli(
                    "dev-worktree",
                    "recover",
                    "--repo",
                    str(self.repo),
                    "--id",
                    worktree_id,
                )
                assert recovered is not None
                self.assertEqual(recovered["status"], "COMPLETE")
                self.assertEqual(recovered["recovered"][0]["status"], "READY")
                self.assertEqual(self.finish(worktree_id)["status"], "COMPLETE")

    def test_finish_intent_recovers_at_each_mutation_boundary(self) -> None:
        for fault in (
            "after_finish_intent",
            "after_worktree_remove",
            "after_branch_delete",
        ):
            with self.subTest(fault=fault):
                payload = self.create(fault)
                result = self.cli(
                    "dev-worktree",
                    "finish",
                    "--repo",
                    str(self.repo),
                    "--id",
                    str(payload["id"]),
                    expected=97,
                    fault=fault,
                )
                self.assertIsNone(result)
                self.assertTrue(self.state_path(str(payload["id"])).is_file())

                recovered = self.cli(
                    "dev-worktree",
                    "recover",
                    "--repo",
                    str(self.repo),
                    "--id",
                    str(payload["id"]),
                )
                assert recovered is not None
                self.assertEqual(recovered["status"], "COMPLETE")
                self.assertFalse(self.state_path(str(payload["id"])).exists())
                self.assertFalse(Path(str(payload["path"])).exists())

    def test_finish_recovery_preserves_head_drift(self) -> None:
        payload = self.create("drift")
        self.cli(
            "dev-worktree",
            "finish",
            "--repo",
            str(self.repo),
            "--id",
            str(payload["id"]),
            expected=97,
            fault="after_finish_intent",
        )
        worktree = Path(str(payload["path"]))
        self.commit_in_worktree(payload, "drifted after intent\n")

        recovered = self.cli(
            "dev-worktree",
            "recover",
            "--repo",
            str(self.repo),
            "--id",
            str(payload["id"]),
            expected=1,
        )
        assert recovered is not None
        self.assertEqual(recovered["status"], "NEEDS_USER")
        self.assertEqual(recovered["failures"][0]["code"], "DEV_WORKTREE_FINISH_DRIFT")
        self.assertTrue(worktree.is_dir())

    def test_status_classifies_unknown_missing_unregistered_and_branch_only(self) -> None:
        payload = self.create("inventory")
        managed_root = Path(str(payload["managed_root"]))

        unknown = managed_root / "unknown"
        self.git(
            self.repo,
            "worktree",
            "add",
            "-q",
            "-b",
            "codex-native/unknown",
            str(unknown),
            "HEAD",
        )
        (unknown / "uncommitted.txt").write_text("preserve\n", encoding="utf-8")
        stray = managed_root / "stray"
        stray.mkdir()
        self.git(self.repo, "branch", "codex-native/branch-only", "HEAD")
        external = self.root / "external"
        self.git(
            self.repo,
            "worktree",
            "add",
            "-q",
            "-b",
            "external-branch",
            str(external),
            "HEAD",
        )

        status = self.cli("dev-worktree", "status", "--repo", str(self.repo))
        assert status is not None
        self.assertTrue(
            any(item["kind"] == "unknown_managed_root" for item in status["unowned_registered"])
        )
        unknown_fact = next(
            item
            for item in status["unowned_registered"]
            if item["kind"] == "unknown_managed_root"
        )
        self.assertGreater(unknown_fact["residue"]["ordinary_count"], 0)
        self.assertTrue(
            any(item["kind"] == "external_registered" for item in status["unowned_registered"])
        )
        self.assertEqual(status["unregistered_directories"][0]["kind"], "unregistered_directory")
        self.assertEqual(status["branch_only"][0]["branch"], "codex-native/branch-only")

        unrelated = self.create("unrelated-orphan")
        self.assertTrue(Path(str(unrelated["path"])).is_dir())
        self.assertEqual(self.finish(str(unrelated["id"]))["status"], "COMPLETE")

        self.git(self.repo, "worktree", "remove", str(payload["path"]))
        missing = self.cli("dev-worktree", "status", "--repo", str(self.repo))
        assert missing is not None
        managed = next(item for item in missing["managed"] if item["id"] == payload["id"])
        self.assertEqual(managed["finding"], "owned_missing")

    def test_status_does_not_mutate_unknown_worktrees(self) -> None:
        root = self.root / "codex-worktrees" / "manual"
        root.parent.mkdir()
        self.git(
            self.repo,
            "worktree",
            "add",
            "-q",
            "-b",
            "manual-branch",
            str(root),
            "HEAD",
        )
        before = self.git(self.repo, "worktree", "list", "--porcelain")
        status = self.cli("dev-worktree", "status", "--repo", str(self.repo))
        after = self.git(self.repo, "worktree", "list", "--porcelain")
        assert status is not None
        self.assertEqual(status["status"], "READY")
        self.assertEqual(before, after)
        self.assertTrue(root.is_dir())

    def test_root_rejects_repository_internal_and_symlink_paths(self) -> None:
        relative = self.cli(
            "dev-worktree",
            "create",
            "--repo",
            str(self.repo),
            "--task",
            "relative",
            "--root",
            "relative-root",
            expected=1,
        )
        assert relative is not None
        self.assertEqual(relative["code"], "DEV_WORKTREE_ROOT_INVALID")

        internal = self.cli(
            "dev-worktree",
            "create",
            "--repo",
            str(self.repo),
            "--task",
            "internal",
            "--root",
            str(self.repo / ".git" / "worktrees"),
            expected=1,
        )
        assert internal is not None
        self.assertEqual(internal["code"], "DEV_WORKTREE_ROOT_INVALID")

        real_root = self.root / "real-root"
        real_root.mkdir()
        symlink_root = self.root / "linked-root"
        symlink_root.symlink_to(real_root, target_is_directory=True)
        linked = self.cli(
            "dev-worktree",
            "create",
            "--repo",
            str(self.repo),
            "--task",
            "linked",
            "--root",
            str(symlink_root),
            expected=1,
        )
        assert linked is not None
        self.assertEqual(linked["code"], "DEV_WORKTREE_ROOT_INVALID")

    def test_state_schema_and_doctor_recovery_signal(self) -> None:
        payload = self.create("schema")
        state = json.loads(self.state_path(str(payload["id"])).read_text())
        schema = json.loads(STATE_SCHEMA.read_text())
        jsonschema.Draft202012Validator(schema).validate(state)

        self.finish(str(payload["id"]))
        self.cli(
            "dev-worktree",
            "create",
            "--repo",
            str(self.repo),
            "--task",
            "doctor-pending",
            expected=97,
            fault="after_create_intent",
        )
        doctor = self.cli("doctor", "--repo", str(self.repo), expected=1)
        assert doctor is not None
        self.assertEqual(doctor["status"], "NEEDS_USER")
        self.assertTrue(
            any(issue["code"] == "DEV_WORKTREE_RECOVERY_REQUIRED" for issue in doctor["issues"])
        )
        self.assertTrue(doctor["dev_worktrees"]["read_only"])

    def test_tampered_state_cannot_redirect_finish_or_recovery(self) -> None:
        payload = self.create("tampered-state")
        state_path = self.state_path(str(payload["id"]))
        state = json.loads(state_path.read_text())
        external = self.root / "external-target"
        external.mkdir()
        state["path"] = str(external)
        state_path.write_text(json.dumps(state), encoding="utf-8")

        blocked = self.finish(str(payload["id"]), expected=1)
        self.assertEqual(blocked["code"], "DEV_WORKTREE_STATE_INVALID")
        self.assertTrue(external.is_dir())
        self.assertTrue(Path(str(payload["path"])).is_dir())

        state = json.loads(state_path.read_text())
        state["phase"] = "creating"
        state["intent"] = {
            "kind": "create",
            "head": state["base_head"],
            "target_head": None,
            "created_at": state["updated_at"],
        }
        state_path.write_text(json.dumps(state), encoding="utf-8")
        recovered = self.cli(
            "dev-worktree",
            "recover",
            "--repo",
            str(self.repo),
            "--id",
            str(payload["id"]),
            expected=1,
        )
        assert recovered is not None
        self.assertEqual(recovered["failures"][0]["code"], "DEV_WORKTREE_STATE_INVALID")
        self.assertTrue(external.is_dir())


if __name__ == "__main__":
    unittest.main()
