from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from harness import cleanup_repo, git, init_repo
from runtime.codex_builder_loop.assurance_v4 import release
from runtime.codex_builder_loop.assurance_v4.models import digest


class ReleaseContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo()

    def tearDown(self) -> None:
        cleanup_repo(self.repo)

    def test_release_schema_and_preflight_bind_current_retrospective(self) -> None:
        head = git(self.repo, "rev-parse", "HEAD")
        snapshot_digest = "a" * 64
        report_digest = "b" * 64
        derivation_digest = "c" * 64
        retrospective = {
            "status": "READY",
            "derivation_status": "verified",
            "snapshot": {
                "snapshot_digest": snapshot_digest,
                "derivation_identity_digest": derivation_digest,
            },
            "report": {"report_digest": report_digest},
        }
        identity = {
            "version": "0.1.0",
            "adapter_commit": head,
            "capture_status": "captured",
        }
        with (
            patch.object(release, "retrospective_status", return_value=retrospective),
            patch.object(release, "_version_identity", return_value=identity),
        ):
            result = release.release_preflight(
                self.repo,
                session_id="release-session",
                version="0.1.0",
                tag="v0.1.0",
                release_commit=head,
            )
        intent = result["intent"]
        schema = json.loads(
            (
                Path(__file__).parents[1]
                / "schema"
                / "assurance-v4-release.schema.json"
            ).read_text()
        )
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(intent)
        self.assertEqual(intent["next_stage"], "tag")
        self.assertEqual(intent["snapshot_digest"], snapshot_digest)
        self.assertNotIn("runs", json.dumps(intent))

    def test_release_preflight_blocks_stale_retrospective(self) -> None:
        with patch.object(
            release,
            "retrospective_status",
            return_value={"status": "STALE", "message": "stale"},
        ):
            with self.assertRaises(release.AssuranceError) as raised:
                release.release_preflight(
                    self.repo,
                    session_id="release-session",
                    version="0.1.0",
                    tag="v0.1.0",
                    release_commit=git(self.repo, "rev-parse", "HEAD"),
                )
        self.assertEqual(raised.exception.code, "RELEASE_RETROSPECTIVE_NOT_READY")

    def test_release_intent_id_is_deterministic_for_same_inputs(self) -> None:
        value = {
            "repo_root": "/repo",
            "owner_session_id": "session",
            "version": "0.1.0",
            "tag": "v0.1.0",
            "release_commit": "d" * 40,
            "snapshot_digest": "a" * 64,
            "report_digest": "b" * 64,
            "derivation_identity_digest": "c" * 64,
        }
        self.assertEqual(digest(value), digest(value))


if __name__ == "__main__":
    unittest.main()
