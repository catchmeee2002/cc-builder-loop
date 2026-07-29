from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from harness import (
    CLI,
    ROOT,
    assert_status,
    cleanup_repo,
    commit_all,
    finish_agent_turn,
    head,
    init_repo,
    load_ledger,
    plan_markdown,
    repo_session_id,
    run_cli,
    run_process,
    start_agent_turn,
    start_run,
    worktrees_from,
    write_plan,
)


HOOK = ROOT / "hooks" / "builder-loop.py"


class SerialPublicationLifecycleAttestationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = tempfile.TemporaryDirectory(
            prefix="serial-publication-lifecycle-"
        )
        self.env = {
            "XDG_RUNTIME_DIR": self.runtime.name,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.repos: list[Path] = []

    def tearDown(self) -> None:
        for repo in self.repos:
            cleanup_repo(repo)
        self.runtime.cleanup()

    def start(
        self, *, label: str, parallel_ready: bool = False
    ) -> tuple[Path, Path, Path, Path, str]:
        repo = init_repo()
        self.repos.append(repo)
        plan = write_plan(
            repo,
            plan_markdown(head(repo), parallel_ready=parallel_ready),
        )
        session_id = repo_session_id(repo, label)
        started, run_path = start_run(
            repo,
            plan,
            task=label,
            session_id=session_id,
            env=self.env,
        )
        builder, tester = worktrees_from(started, run_path)
        return repo, run_path, builder, tester, session_id

    def publish(self, run_path: Path, builder: Path):
        (builder / "src" / "public_api.py").write_text("API_VERSION = 1\n")
        return run_cli("publish-prerequisites", "--run", run_path, env=self.env)

    def route_path(self, run_path: Path) -> Path:
        matches: list[Path] = []
        for path in Path(self.runtime.name).rglob("*.json"):
            if not path.is_file():
                continue
            try:
                value = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue
            if (
                isinstance(value, dict)
                and value.get("run_id") == run_path.name
                and "event" not in value
                and "turn_id" not in value
            ):
                matches.append(path)
        self.assertEqual(len(matches), 1, matches)
        return matches[0]

    def route(self, run_path: Path) -> dict:
        return json.loads(self.route_path(run_path).read_text())

    def write_private_json(self, path: Path, value: dict) -> None:
        path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        path.chmod(0o600)

    def call_hook(self, event: dict, *, cwd: Path) -> dict:
        completed = run_process(
            [sys.executable, HOOK],
            cwd=cwd,
            env={
                **self.env,
                "BUILDER_LOOP_CLI": str(CLI),
            },
            input_text=json.dumps(event),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, completed.stdout)
        return json.loads(lines[-1])

    def queued_event(self, turn_id: str) -> dict:
        matches: list[dict] = []
        for path in Path(self.runtime.name).rglob("*.json"):
            if not path.is_file():
                continue
            try:
                value = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("turn_id") == turn_id:
                matches.append(value)
        self.assertEqual(len(matches), 1, matches)
        return matches[0]

    def assert_publication_route(self, run_path: Path, publication_head: str) -> None:
        route = self.route(run_path)
        self.assertEqual(
            route["tester_start_attestation"],
            {
                "kind": "initial-author",
                "expected_head": publication_head,
                "tester_head": publication_head,
                "dirty_paths": [],
            },
        )

    def test_serial_publication_updates_route_before_ready(self) -> None:
        _repo, run_path, builder, tester, _session_id = self.start(
            label="serial-route-ready"
        )
        published = self.publish(run_path, builder)
        assert_status(published, "READY", rc=0)
        publication_head = str(published.data["head"])

        ledger = load_ledger(run_path)
        self.assertEqual(head(tester), publication_head)
        self.assertEqual(
            ledger["tester_integration"]["base_head"], publication_head
        )
        self.assert_publication_route(run_path, publication_head)

    def test_delayed_real_hook_events_bind_original_turn_after_author_commit(
        self,
    ) -> None:
        _repo, run_path, builder, tester, session_id = self.start(
            label="serial-delayed-fold"
        )
        published = self.publish(run_path, builder)
        assert_status(published, "READY", rc=0)
        publication_head = str(published.data["head"])
        turn_id = "serial-delayed-fold-turn"
        agent_id = "serial-delayed-fold-tester"
        event = {
            "cwd": str(tester),
            "session_id": session_id,
            "turn_id": turn_id,
            "agent_id": agent_id,
            "agent_type": "tester",
        }

        start_output = self.call_hook(
            {**event, "hook_event_name": "SubagentStart"}, cwd=tester
        )
        self.assertNotIn("systemMessage", start_output, start_output)
        envelope = self.queued_event(turn_id)
        self.assertEqual(
            envelope["tester_baseline"],
            {
                "kind": "initial-author",
                "expected_head": publication_head,
                "tester_head": publication_head,
                "dirty_paths": [],
            },
        )

        authored = tester / "tests" / "test_published_public_api.py"
        authored.write_text(
            "import unittest\n"
            "from src.public_api import API_VERSION\n\n"
            "class PublishedPublicApiTest(unittest.TestCase):\n"
            "    def test_version(self):\n"
            "        self.assertEqual(API_VERSION, 1)\n"
        )
        author_head = commit_all(tester, "author delayed publication test")
        self.assertNotEqual(author_head, publication_head)

        stop_output = self.call_hook(
            {
                **event,
                "hook_event_name": "SubagentStop",
                "last_assistant_message": (
                    "independent tests committed\nTESTER_RESULT: tests_ready"
                ),
                "stop_hook_active": False,
            },
            cwd=tester,
        )
        self.assertNotIn("decision", stop_output, stop_output)
        self.assertNotIn("systemMessage", stop_output, stop_output)

        folded = run_cli("status", "--run", run_path, env=self.env)
        self.assertEqual(folded.returncode, 0, folded.data)
        self.assertIn(folded.data.get("status"), {"ACTIVE", "READY"}, folded.data)
        ledger = load_ledger(run_path)
        self.assertEqual(ledger["phase"], "active")
        self.assertEqual(ledger["agents"]["tester"]["agent_id"], agent_id)
        self.assertEqual(ledger["agents"]["tester"]["turn_id"], turn_id)
        self.assertEqual(ledger["agents"]["tester"]["result"], "tests_ready")
        self.assertEqual(
            ledger["tester_integration"]["author_agent_id"], agent_id
        )
        self.assertEqual(ledger["tester_integration"]["author_turn_id"], turn_id)

        integrated = run_cli("integrate-tests", "--run", run_path, env=self.env)
        assert_status(integrated, "READY", rc=0)
        after = load_ledger(run_path)
        self.assertEqual(after["tester_integration"]["source_head"], author_head)
        self.assertEqual(after["tester_integration"]["author_turn_id"], turn_id)

    def test_interrupted_route_sync_recovers_only_clean_publication_baseline(
        self,
    ) -> None:
        _repo, run_path, builder, tester, _session_id = self.start(
            label="serial-route-recovery"
        )
        route = self.route_path(run_path)
        route_parent = route.parent
        original_mode = route_parent.stat().st_mode & 0o777
        route_parent.chmod(0o500)
        try:
            first = self.publish(run_path, builder)
        finally:
            route_parent.chmod(original_mode)
        self.assertNotIn(first.data.get("status"), {"READY", "NOOP"}, first.data)
        self.assertNotEqual(first.returncode, 0, first.data)
        persisted = load_ledger(run_path)
        publication = dict(persisted["prerequisite_publication"])
        publication_head = str(publication["head"])
        self.assertEqual(head(tester), publication_head)
        self.assertEqual(
            persisted["tester_integration"]["base_head"], publication_head
        )

        retry = run_cli("publish-prerequisites", "--run", run_path, env=self.env)
        assert_status(retry, "NOOP", rc=0)
        recovered = load_ledger(run_path)
        self.assertEqual(recovered["prerequisite_publication"], publication)
        self.assertEqual(head(tester), publication_head)
        self.assertEqual(head(builder), publication["builder_head"])
        self.assert_publication_route(run_path, publication_head)

    def test_route_repair_rejects_advanced_or_dirty_unbound_tester(self) -> None:
        for state in ("advanced", "ordinary-dirty", "ignored-dirty"):
            with self.subTest(state=state):
                _repo, run_path, builder, tester, _session_id = self.start(
                    label=f"serial-route-unprovable-{state}"
                )
                route = self.route_path(run_path)
                route_parent = route.parent
                original_mode = route_parent.stat().st_mode & 0o777
                route_parent.chmod(0o500)
                try:
                    first = self.publish(run_path, builder)
                finally:
                    route_parent.chmod(original_mode)
                self.assertNotIn(
                    first.data.get("status"), {"READY", "NOOP"}, first.data
                )
                self.assertNotEqual(first.returncode, 0, first.data)
                persisted = load_ledger(run_path)
                publication = dict(persisted["prerequisite_publication"])

                residue = tester / "tests" / f"{state}.tmp"
                residue.write_text(f"{state}\n")
                if state == "advanced":
                    commit_all(tester, "advance tester before route repair")
                elif state == "ignored-dirty":
                    common = run_process(
                        ["git", "rev-parse", "--git-common-dir"], cwd=tester
                    )
                    self.assertEqual(common.returncode, 0, common.stderr)
                    git_common = Path(common.stdout.strip())
                    if not git_common.is_absolute():
                        git_common = (tester / git_common).resolve()
                    exclude = git_common / "info" / "exclude"
                    exclude.parent.mkdir(parents=True, exist_ok=True)
                    exclude.write_text(
                        (exclude.read_text() if exclude.is_file() else "")
                        + f"\n/tests/{residue.name}\n"
                    )

                retry = run_cli(
                    "publish-prerequisites", "--run", run_path, env=self.env
                )
                self.assertNotIn(
                    retry.data.get("status"), {"READY", "NOOP"}, retry.data
                )
                self.assertNotEqual(retry.returncode, 0, retry.data)
                self.assertEqual(
                    retry.data.get("code"), "TESTER_AUTHOR_BASELINE_MISMATCH"
                )
                after = load_ledger(run_path)
                self.assertEqual(after["prerequisite_publication"], publication)
                self.assertIsNone(after["agents"]["tester"])
                route_after = self.route_path_if_present(run_path)
                if route_after is not None:
                    attestation = json.loads(route_after.read_text()).get(
                        "tester_start_attestation"
                    )
                    self.assertNotEqual(
                        attestation,
                        {
                            "kind": "initial-author",
                            "expected_head": publication["head"],
                            "tester_head": publication["head"],
                            "dirty_paths": [],
                        },
                    )

    def route_path_if_present(self, run_path: Path) -> Path | None:
        for path in Path(self.runtime.name).rglob("*.json"):
            if not path.is_file():
                continue
            try:
                value = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue
            if (
                isinstance(value, dict)
                and value.get("run_id") == run_path.name
                and "event" not in value
                and "turn_id" not in value
            ):
                return path
        return None

    def test_invalid_captured_publication_attestations_fail_closed(self) -> None:
        for variant in ("old-head", "missing-field", "dirty"):
            with self.subTest(variant=variant):
                _repo, run_path, builder, tester, session_id = self.start(
                    label=f"serial-invalid-capture-{variant}"
                )
                published = self.publish(run_path, builder)
                assert_status(published, "READY", rc=0)
                route_path = self.route_path(run_path)
                route = json.loads(route_path.read_text())
                attestation = route["tester_start_attestation"]
                if variant == "old-head":
                    attestation["expected_head"] = "0" * 40
                    attestation["tester_head"] = "0" * 40
                elif variant == "missing-field":
                    del attestation["expected_head"]
                else:
                    attestation["dirty_paths"] = ["tests/captured-dirty.tmp"]
                self.write_private_json(route_path, route)
                turn_id = f"serial-invalid-capture-{variant}-turn"
                output = self.call_hook(
                    {
                        "hook_event_name": "SubagentStart",
                        "cwd": str(tester),
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "agent_id": f"serial-invalid-capture-{variant}-tester",
                        "agent_type": "tester",
                    },
                    cwd=tester,
                )
                self.assertNotIn("systemMessage", output, output)
                folded = run_cli("status", "--run", run_path, env=self.env)
                assert_status(folded, "CONTINUITY_FAILURE", rc=1)
                ledger = load_ledger(run_path)
                self.assertEqual(ledger["phase"], "continuity_failure")
                self.assertIsNone(ledger["agents"]["tester"])

    def test_parallel_noop_and_serial_followup_preserve_attestations(self) -> None:
        _repo, run_path, _builder, _tester, _session_id = self.start(
            label="parallel-attestation", parallel_ready=True
        )
        parallel_route = self.route(run_path)["tester_start_attestation"]
        parallel_noop = run_cli(
            "publish-prerequisites", "--run", run_path, env=self.env
        )
        assert_status(parallel_noop, "NOOP", rc=0)
        self.assertEqual(
            self.route(run_path)["tester_start_attestation"], parallel_route
        )

        _repo, run_path, builder, tester, _session_id = self.start(
            label="serial-followup-attestation"
        )
        published = self.publish(run_path, builder)
        assert_status(published, "READY", rc=0)
        agent_id, turn_id = start_agent_turn(
            run_path, "tester", agent_id="serial-followup-tester"
        )
        authored = tester / "tests" / "test_serial_followup.py"
        authored.write_text(
            "import unittest\n"
            "from src.public_api import API_VERSION\n\n"
            "class SerialFollowupTest(unittest.TestCase):\n"
            "    def test_published_version(self):\n"
            "        self.assertEqual(API_VERSION, 1)\n"
        )
        commit_all(tester, "author serial follow-up fixture")
        finish_agent_turn(
            run_path,
            "tester",
            agent_id=agent_id,
            turn_id=turn_id,
            result="tests_ready",
        )
        assert_status(
            run_cli("integrate-tests", "--run", run_path, env=self.env),
            "READY",
            rc=0,
        )
        prepared = run_cli(
            "prepare-follow-up",
            "--run",
            run_path,
            "--role",
            "tester",
            "--agent-id",
            agent_id,
            "--purpose",
            "author",
            env=self.env,
        )
        assert_status(prepared, "READY", rc=0)
        follow_up = self.route(run_path)["tester_start_attestation"]
        self.assertEqual(follow_up["kind"], "follow-up")
        repeated = run_cli(
            "publish-prerequisites", "--run", run_path, env=self.env
        )
        assert_status(repeated, "NOOP", rc=0)
        self.assertEqual(
            self.route(run_path)["tester_start_attestation"], follow_up
        )


if __name__ == "__main__":
    unittest.main()
