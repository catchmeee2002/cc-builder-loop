from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from runtime.codex_builder_loop.assurance_v4 import release


class GitHubReleaseReadbackTest(unittest.TestCase):
    def test_readback_requests_only_supported_fields(self) -> None:
        intent = {
            "tag": "v0.1.2",
            "release_commit": "a" * 40,
        }
        completed = release.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "tagName": "v0.1.2",
                    "targetCommitish": "a" * 40,
                    "isDraft": False,
                }
            ),
            stderr="",
        )
        with patch.object(release.subprocess, "run", return_value=completed) as run:
            observed = release._verify_github_release(
                release.Path("/tmp/release-readback-test"),
                intent,
            )

        self.assertEqual(observed["tagName"], "v0.1.2")
        argv = run.call_args.args[0]
        self.assertEqual(
            argv,
            [
                "gh",
                "release",
                "view",
                "v0.1.2",
                "--json",
                "tagName,targetCommitish,isDraft",
            ],
        )


if __name__ == "__main__":
    unittest.main()
