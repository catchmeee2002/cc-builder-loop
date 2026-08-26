#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = ROOT / "runtime"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(RUNTIME_ROOT))

from codex_builder_loop.assurance_v4 import core, driver  # noqa: E402
from codex_builder_loop.assurance_v4.models import digest  # noqa: E402
from tests.test_native_driver_v1 import native_contract  # noqa: E402
from harness import cleanup_repo, commit_all, init_repo  # noqa: E402


def _native_runtime() -> dict[str, Any]:
    return {
        "kind": "native",
        "protocol_version": 1,
        "transport": "codex_app_server",
        "runtime_version": "blackbox",
        "protocol_schema_digest": "a" * 64,
        "protocol_canary_digest": None,
        "native_transport": None,
        "root_session_identity": None,
    }


def _turns() -> list[dict[str, Any]]:
    return [
        {
            "id": "blackbox-reviewer-turn-3",
            "status": "failed",
            "items": [],
            "error": {
                "codexErrorInfo": {"responseStreamDisconnected": {}}
            },
        }
    ]


def main() -> int:
    repo = init_repo()
    try:
        run_id = "reviewer-replacement-blackbox"
        contract = native_contract(repo)
        contract["assurance"]["required"] = ["reviewer"]
        core.start(
            repo,
            run_id,
            "blackbox-session",
            contract,
            driver_runtime=_native_runtime(),
        )
        builder = driver.next_action(repo, run_id)
        core.prepare_builder(
            repo,
            run_id,
            "blackbox-builder",
            thread_id="blackbox-builder-thread",
        )
        candidate = Path(core.driver_context(repo, run_id)["candidate_worktree"])
        (candidate / "src" / "reviewer_blackbox.py").write_text(
            "VALUE = 1\n", encoding="utf-8"
        )
        commit_all(candidate, "blackbox reviewer replacement candidate")
        checkpoint = driver.next_action(repo, run_id)
        core.checkpoint_builder(repo, run_id, action_id=checkpoint["action_id"])
        scan = driver.next_action(repo, run_id)
        if scan["action"] == "scan_doc_references":
            core.scan_doc_references(repo, run_id)
        review = driver.next_action(repo, run_id)
        if review["action"] != "reviewer_final":
            raise RuntimeError(f"unexpected reviewer action: {review}")
        core.prepare_reviewer(repo, run_id, "blackbox-reviewer-old", "blackbox-reviewer-old-thread")
        review = driver.next_action(repo, run_id)
        core.begin_dispatch(
            repo,
            run_id,
            action_id=review["action_id"],
            action=review["action"],
            role="reviewer",
            thread_id="blackbox-reviewer-old-thread",
            prompt_digest="b" * 64,
            output_schema_digest="c" * 64,
            driver_runtime_kind="native",
        )
        for index in range(3):
            core.bind_dispatch_turn(
                repo,
                run_id,
                action_id=review["action_id"],
                turn_id=f"blackbox-reviewer-turn-{index + 1}",
            )
            if index < 2:
                core.retry_dispatch(
                    repo,
                    run_id,
                    action_id=review["action_id"],
                    failure_code="responseStreamDisconnected",
                )
        try:
            core.retry_dispatch(
                repo,
                run_id,
                action_id=review["action_id"],
                failure_code="responseStreamDisconnected",
            )
        except core.AssuranceError as exc:
            if exc.code != "NATIVE_DISPATCH_RETRY_EXHAUSTED":
                raise
        ledger = json.loads(
            (
                repo
                / ".git"
                / "builder-loop-assurance-v4"
                / "runs"
                / run_id
                / "ledger.json"
            ).read_text()
        )
        pending = ledger["dispatch_intent"]
        turns = _turns()
        observation = {
            "candidate_head": ledger["facets"]["execution"]["candidate_head"],
            "target_start_head": ledger["target_start_head"],
            "evidence": ledger["evidence"],
            "publication": ledger["publication"],
            "deployment_transaction": ledger["deployment_transaction"],
        }

        class FakeCore:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def call(self, command: str, *args: str, input_value=None):
                self.calls.append(command)
                if command == "driver-context":
                    return {
                        **core.driver_context(repo, run_id),
                        "dispatch_intent": pending,
                    }
                if command == "begin-reviewer-replacement":
                    return {"status": "ACTIVE"}
                raise AssertionError(command)

        class FakeTransport:
            def read_thread(self, thread_id: str) -> dict[str, Any]:
                if thread_id != "blackbox-reviewer-old-thread":
                    raise AssertionError(thread_id)
                return {"turns": turns}

        from codex_builder_loop.native_driver.coordinator import NativeCoordinator

        native = NativeCoordinator(
            repo=repo,
            run_id=run_id,
            core=FakeCore(),
            transport=FakeTransport(),
            project_root=ROOT,
            thread_compaction_available=False,
        )
        native.current_action = review
        if not native._start_reviewer_replacement(review["action_id"]):
            raise RuntimeError("eligible Reviewer replacement was not started")
        if native._start_reviewer_replacement(review["action_id"]) is not True:
            raise RuntimeError("persisted replacement was not replayable")

        # The same source is ineligible once a later thread turn appears.
        changed = FakeTransport()
        changed.read_thread = lambda _thread_id: {
            "turns": [*turns, {"id": "later", "status": "completed", "items": []}]
        }
        drift_native = NativeCoordinator(
            repo=repo,
            run_id=run_id,
            core=FakeCore(),
            transport=changed,
            project_root=ROOT,
            thread_compaction_available=False,
        )
        try:
            drift_native._assert_reviewer_replacement_source(
                {
                    "thread_id": "blackbox-reviewer-old-thread",
                    "turn_id": "blackbox-reviewer-turn-3",
                    "failure_code": "responseStreamDisconnected",
                    "thread_observation_digest": digest(turns),
                }
            )
        except Exception as exc:
            if getattr(exc, "code", None) != "NATIVE_REVIEWER_REPLACEMENT_SOURCE_DRIFT":
                raise
        else:
            raise RuntimeError("thread drift was accepted")

        print(
            json.dumps(
                {
                    "result": "pass",
                    "builder_action": builder["action"],
                    "replacement_action": "replace_reviewer",
                    "source_turn_count": len(turns),
                    "compaction_preferred": True,
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        cleanup_repo(repo)


if __name__ == "__main__":
    raise SystemExit(main())
