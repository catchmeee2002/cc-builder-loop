from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from harness import ROOT, git, run_process


RUNNER = ROOT / "experiments" / "agent-behavior" / "runner.py"
BUILDER_SKILL = ROOT / "skills" / "builder" / "SKILL.md"
VARIANTS = ROOT / "experiments" / "agent-behavior" / "variants.json"


def json_values(text: str) -> list[Any]:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise AssertionError("behavior runner produced no JSON")
    if len(lines) == 1:
        value = json.loads(lines[0])
        return value if isinstance(value, list) else [value]
    return [json.loads(line) for line in lines]


def recursive_values(value: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        if key in value:
            found.append(value[key])
        for child in value.values():
            found.extend(recursive_values(child, key))
    elif isinstance(value, list):
        for child in value:
            found.extend(recursive_values(child, key))
    return found


class AgentBehaviorLabTest(unittest.TestCase):
    def test_builder_current_binds_the_frozen_builder_skill(self) -> None:
        variants = json.loads(VARIANTS.read_text())
        matches = [
            item for item in variants["variants"] if item["id"] == "builder-current"
        ]
        self.assertEqual(len(matches), 1, matches)
        current = matches[0]
        source = current["instruction_source"]
        actual_sha256 = hashlib.sha256(BUILDER_SKILL.read_bytes()).hexdigest()

        self.assertEqual(source["path"], "skills/builder/SKILL.md")
        self.assertEqual(source["revision"], "WORKTREE")
        self.assertEqual(source["sha256"], actual_sha256)
        self.assertEqual(
            git(ROOT, "rev-parse", "HEAD:skills/builder/SKILL.md"),
            git(ROOT, "hash-object", "skills/builder/SKILL.md"),
        )

    def test_prepare_and_score_are_deterministic_offline_and_ephemeral(self) -> None:
        self.assertTrue(RUNNER.is_file(), RUNNER)
        before_status = git(ROOT, "status", "--porcelain", "--untracked-files=all")
        before_ignored = git(
            ROOT, "ls-files", "--others", "--ignored", "--exclude-standard"
        )
        before_tracked = git(ROOT, "ls-files", "experiments/agent-behavior")
        variants_before = VARIANTS.read_bytes()

        with tempfile.TemporaryDirectory(prefix="behavior-lab-offline-") as tmp:
            guard = Path(tmp)
            network_marker = guard / "network-used"
            (guard / "sitecustomize.py").write_text(
                "from pathlib import Path\n"
                "import socket\n"
                f"marker = Path({str(network_marker)!r})\n"
                "def forbidden(*args, **kwargs):\n"
                "    marker.write_text('network')\n"
                "    raise RuntimeError('network disabled by contract test')\n"
                "socket.create_connection = forbidden\n"
                "class GuardedSocket(socket.socket):\n"
                "    def connect(self, *args, **kwargs):\n"
                "        return forbidden(*args, **kwargs)\n"
                "socket.socket = GuardedSocket\n"
            )
            env = {
                "PYTHONPATH": str(guard),
                "PYTHONDONTWRITEBYTECODE": "1",
                "HOME": str(guard),
                "HTTP_PROXY": "http://127.0.0.1:9",
                "HTTPS_PROXY": "http://127.0.0.1:9",
                "ALL_PROXY": "http://127.0.0.1:9",
                "NO_PROXY": "",
            }
            prepare_runs = [
                run_process([sys.executable, RUNNER, "prepare"], cwd=ROOT, env=env)
                for _ in range(2)
            ]
            for result in prepare_runs:
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(prepare_runs[0].stdout, prepare_runs[1].stdout)
            prepared = json_values(prepare_runs[0].stdout)
            scenario_ids = recursive_values(prepared, "scenario_id")
            self.assertTrue(scenario_ids, prepared)
            self.assertEqual(len(scenario_ids), len(set(scenario_ids)))
            suite = prepared[0]
            requests = suite["requests"]
            self.assertEqual(len(requests), len(scenario_ids))
            current_roles = {
                "builder-current": "skills/builder/SKILL.md",
                "reviewer-current": "agents/reviewer.toml",
                "tester-current": "agents/tester.toml",
            }
            observed_current_roles: dict[str, str] = {}
            for request in requests:
                source = request["request"]["instruction_source"]
                source_path = ROOT / source["path"]
                self.assertTrue(source_path.is_file(), source)
                self.assertEqual(
                    source["sha256"], hashlib.sha256(source_path.read_bytes()).hexdigest()
                )
                self.assertRegex(request["input_digest"], r"^[0-9a-f]{64}$")
                self.assertRegex(request["variant_id"], r"^[a-z0-9-]+$")
                if request["variant_id"] in current_roles:
                    observed_current_roles[request["variant_id"]] = source["path"]
            self.assertEqual(observed_current_roles, current_roles)

            score_input = {
                "schema_version": 1,
                "responses": [
                    {
                        "scenario_id": request["scenario_id"],
                        "variant_id": request["variant_id"],
                        "input_digest": request["input_digest"],
                        "response": " ".join(request["mechanical_checks"]["contains"]),
                    }
                    for request in requests
                ],
            }

            score_runs = [
                run_process(
                    [sys.executable, RUNNER, "score"],
                    cwd=ROOT,
                    env=env,
                    input_text=json.dumps(score_input, ensure_ascii=False),
                )
                for _ in range(2)
            ]
            for result in score_runs:
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(score_runs[0].stdout, score_runs[1].stdout)
            scored = json_values(score_runs[0].stdout)
            self.assertTrue(recursive_values(scored, "scenario_id"), scored)
            self.assertTrue(recursive_values(scored, "response_digest"), scored)
            semantic = recursive_values(scored, "semantic_pending")
            self.assertTrue(semantic, scored)
            self.assertTrue(all(value is True for value in semantic), semantic)
            self.assertFalse(network_marker.exists())

            builder_request = next(
                request
                for request in requests
                if request["variant_id"] == "builder-current"
            )
            targeted = run_process(
                [
                    sys.executable,
                    RUNNER,
                    "prepare",
                    "--scenario-id",
                    builder_request["scenario_id"],
                    "--variant-id",
                    "builder-current",
                ],
                cwd=ROOT,
                env=env,
            )
            self.assertEqual(targeted.returncode, 0, targeted.stderr)
            targeted_requests = json_values(targeted.stdout)
            self.assertEqual(
                {request["variant_id"] for request in targeted_requests},
                {"builder-current"},
            )
            for request in targeted_requests:
                source = request["request"]["instruction_source"]
                self.assertEqual(source["path"], "skills/builder/SKILL.md")
                self.assertEqual(
                    source["sha256"], hashlib.sha256(BUILDER_SKILL.read_bytes()).hexdigest()
                )

        self.assertEqual(
            git(ROOT, "status", "--porcelain", "--untracked-files=all"),
            before_status,
        )
        self.assertEqual(
            git(ROOT, "ls-files", "--others", "--ignored", "--exclude-standard"),
            before_ignored,
        )
        self.assertEqual(
            git(ROOT, "ls-files", "experiments/agent-behavior"), before_tracked
        )
        self.assertEqual(VARIANTS.read_bytes(), variants_before)


if __name__ == "__main__":
    unittest.main()
