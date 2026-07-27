from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import issue_triage_poller as poller  # noqa: E402


HEAD = "a" * 40
FIXED_HEAD = "b" * 40


def run_git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {completed.stderr}")
    return completed.stdout.strip()


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    run_git(repo, "init")
    run_git(repo, "remote", "add", "origin", "https://github.com/catch/repo.git")
    (repo / "README").write_text("test\n", encoding="utf-8")
    run_git(repo, "add", "README")
    run_git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgSign=false",
        "commit",
        "-m",
        "test(issue-triage): [cr_id_skip] Init",
    )
    return repo


def capture_body() -> str:
    return f"""事故正文。

<!-- issue-capture:v1 -->
```json
{{
  "captured_at": "2026-07-27T00:00:00Z",
  "repository": "catch/repo",
  "incident_head": "{HEAD}",
  "branch": "main",
  "dirty": false,
  "root_cause_status": "unknown"
}}
```
<!-- /issue-capture:v1 -->
"""


def resolution_body(
    *,
    kinds: list[str] | None = None,
    deterministic: bool = True,
    outcome: str = "fixed",
    root_cause_status: str = "confirmed",
    residual: list[str] | None = None,
) -> str:
    kinds = kinds or []
    residual = residual or []
    value = {
        "resolved_at": "2026-07-27T02:00:00Z",
        "outcome": outcome,
        "incident_head": HEAD,
        "resolved_head": FIXED_HEAD,
        "fix_commits": [FIXED_HEAD],
        "root_cause_status": root_cause_status,
        "root_cause": "状态被错误折叠",
        "violated_invariant": "失败不能冒充成功",
        "human_decision": {
            "required": bool(kinds),
            "kinds": kinds,
            "evidence": ["decision"] if kinds else [],
        },
        "acceptance": {"deterministic": deterministic, "evidence": ["tests pass"]},
        "residual_uncertainty": residual,
    }
    return (
        "<!-- issue-resolution:v1 -->\n```json\n"
        + json.dumps(value, ensure_ascii=False, indent=2)
        + "\n```\n<!-- /issue-resolution:v1 -->"
    )


def issue(number: int, *, state: str = "OPEN", resolution: str | None = None) -> dict:
    comments = [{"body": resolution, "createdAt": "2026-07-27T02:00:00Z"}] if resolution else []
    return {
        "number": number,
        "title": "事故",
        "body": capture_body(),
        "state": state,
        "createdAt": "2026-07-27T00:01:00Z",
        "updatedAt": "2026-07-27T02:00:00Z",
        "url": f"https://example.invalid/{number}",
        "author": {"login": "tester"},
        "comments": comments,
    }


class FakeShadowRunner:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        run_dir = self.root / f"run-{self.calls}"
        run_dir.mkdir(parents=True, exist_ok=True)
        return {
            "run_dir": str(run_dir),
            "input_sha256": "input",
            "recommended_axes": {
                "diagnosis_state": "established",
                "human_attention": "none",
                "scope_inventory_required": False,
            },
            "recommended_work_queue": "agent_execute",
        }


class IssueTriagePollerTests(unittest.TestCase):
    def test_changed_issue_scan_projects_fields_and_filters_pull_requests(self):
        output = "\n".join(
            json.dumps(row)
            for row in (
                {
                    "number": 1,
                    "created_at": "2026-07-27T00:00:00Z",
                    "updated_at": "2026-07-27T00:01:00Z",
                    "state": "open",
                    "html_url": "https://example.invalid/1",
                    "is_pull_request": False,
                },
                {
                    "number": 2,
                    "created_at": "2026-07-27T00:00:00Z",
                    "updated_at": "2026-07-27T00:01:00Z",
                    "state": "open",
                    "html_url": "https://example.invalid/2",
                    "is_pull_request": True,
                },
            )
        )
        command = poller.shadow.CommandResult(0, output, "")
        with mock.patch.object(poller.shadow, "run_command", return_value=command) as run:
            rows = poller.fetch_changed_issue_refs(
                repo=Path("/tmp/repo"),
                github_repo="catch/repo",
                since="2026-07-27T00:00:00Z",
                gh_bin="/usr/bin/gh",
            )

        self.assertEqual([row["number"] for row in rows], [1])
        args = run.call_args.args[0]
        self.assertIn("--paginate", args)
        self.assertIn("--jq", args)
        self.assertNotIn("--slurp", args)
        self.assertNotIn("create", args)
        self.assertNotIn("comment", args)
        self.assertNotIn("close", args)

    def test_prediction_is_idempotent_and_resolution_is_scored_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry: dict = {}
            runner = FakeShadowRunner(root)
            common = {
                "repo": root,
                "github_repo": "catch/repo",
                "entry": entry,
                "state_root": root / "state",
                "enabled_at": "2026-07-27T00:00:00Z",
                "main_effort": "high",
                "boundary_effort": None,
                "profiles_path": root / "profiles.json",
                "now": "2026-07-27T01:00:00Z",
                "shadow_runner": runner,
            }

            first = poller.process_issue(issue=issue(1), **common)
            second = poller.process_issue(issue=issue(1), **common)
            closed = poller.process_issue(issue=issue(1, state="CLOSED", resolution=resolution_body()), **common)
            evaluation_path = Path(entry["evaluation"]["path"])
            repeated = poller.process_issue(issue=issue(1, state="CLOSED", resolution=resolution_body()), **common)
            evaluation_exists = evaluation_path.is_file()

        self.assertEqual((first, second, closed, repeated), ("predicted", "predicted", "evaluated", "evaluated"))
        self.assertEqual(runner.calls, 1)
        self.assertTrue(evaluation_exists)
        self.assertTrue(entry["evaluation"]["queue_exact"])
        self.assertFalse(entry["evaluation"]["unsafe_auto_execute"])

    def test_closed_issue_without_prior_prediction_is_never_predicted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry: dict = {}
            runner = FakeShadowRunner(root)

            status = poller.process_issue(
                repo=root,
                github_repo="catch/repo",
                issue=issue(2, state="CLOSED", resolution=resolution_body()),
                entry=entry,
                state_root=root / "state",
                enabled_at="2026-07-27T00:00:00Z",
                main_effort="high",
                boundary_effort=None,
                profiles_path=root / "profiles.json",
                now="2026-07-27T03:00:00Z",
                shadow_runner=runner,
            )

        self.assertEqual(status, "missed_prediction")
        self.assertEqual(runner.calls, 0)
        self.assertNotIn("prediction", entry)

    def test_resolution_gold_uses_root_cause_decision_and_acceptance_contract(self):
        capture = poller.shadow.parse_capture(capture_body(), expected_repository="catch/repo")
        scope = poller.shadow.validate_resolution(
            poller.shadow.parse_marker_json(resolution_body(kinds=["scope_approval"]), poller.shadow.RESOLUTION_MARKER),
            capture=capture,
        )
        tradeoff = poller.shadow.validate_resolution(
            poller.shadow.parse_marker_json(resolution_body(kinds=["tradeoff"]), poller.shadow.RESOLUTION_MARKER),
            capture=capture,
        )
        corrected = poller.shadow.validate_resolution(
            poller.shadow.parse_marker_json(
                resolution_body(kinds=["root_cause_correction"]),
                poller.shadow.RESOLUTION_MARKER,
            ),
            capture=capture,
        )

        self.assertEqual(poller.resolution_gold(scope)["work_queue"], "batch_approval")
        self.assertEqual(poller.resolution_gold(tradeoff)["work_queue"], "first_principles")
        self.assertEqual(poller.resolution_gold(corrected)["work_queue"], "agent_investigate")

    def test_one_issue_failure_does_not_block_other_issues_and_is_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_repo(root)
            state_root = root / "state"

            def fake_refs(**kwargs):
                return [
                    {
                        "number": 1,
                        "created_at": "2026-07-27T00:01:00Z",
                        "updated_at": "2026-07-27T00:02:00Z",
                        "state": "open",
                        "url": "https://example.invalid/1",
                    },
                    {
                        "number": 2,
                        "created_at": "2026-07-27T00:01:00Z",
                        "updated_at": "2026-07-27T00:02:00Z",
                        "state": "open",
                        "url": "https://example.invalid/2",
                    },
                ]

            def fake_fetch(repo, github_repo, number, gh_bin):
                return issue(number)

            def fake_process(**kwargs):
                if kwargs["issue"]["number"] == 1:
                    raise poller.meta.RunnerError("transport", "temporary", poller.meta.EXIT_TRANSPORT)
                kwargs["entry"]["status"] = "predicted"
                return "predicted"

            with mock.patch.object(poller, "process_issue", side_effect=fake_process):
                result = poller.run_poller(
                    state_root=state_root,
                    repositories=(repo,),
                    gh_bin="gh",
                    main_effort="high",
                    boundary_effort=None,
                    now="2026-07-27T00:00:00Z",
                    fetch_refs=fake_refs,
                    fetch_issue=fake_fetch,
                )
            state = poller.load_state(state_root)

        entries = state["repositories"][str(repo.resolve())]["issues"]
        self.assertEqual(result["processed_issues"], 1)
        self.assertEqual(result["failures"], 1)
        self.assertEqual(entries["1"]["status"], "pending_retry")
        self.assertEqual(entries["1"]["pending_error"]["attempts"], 1)
        self.assertEqual(entries["2"]["status"], "predicted")

    def test_cron_install_is_unique_and_preserves_unmanaged_lines(self):
        existing = "MAILTO=test@example.invalid\n0 1 * * * /bin/backup\n"
        writes: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = poller.install_cron(
                state_root=root / "state",
                repositories=(root / "repo",),
                script=root / "poller.py",
                python_bin="/usr/bin/python3",
                gh_bin="/usr/bin/gh",
                flock_bin="/usr/bin/flock",
                now="2026-07-27T00:00:00Z",
                read_crontab=lambda: existing,
                write_crontab=writes.append,
            )
            rendered = writes[-1]
            second_writes: list[str] = []
            poller.install_cron(
                state_root=root / "state",
                repositories=(root / "repo",),
                script=root / "poller.py",
                python_bin="/usr/bin/python3",
                gh_bin="/usr/bin/gh",
                flock_bin="/usr/bin/flock",
                read_crontab=lambda: rendered,
                write_crontab=second_writes.append,
            )
            removed = poller.render_managed_crontab(rendered, None)

        self.assertTrue(first["installed"])
        self.assertIn("MAILTO=test@example.invalid", rendered)
        self.assertIn("/bin/backup", rendered)
        self.assertEqual(rendered.count(poller.MANAGED_CRON_MARKER), 1)
        self.assertEqual(second_writes, [])
        self.assertNotIn(poller.MANAGED_CRON_MARKER, removed)
        self.assertIn("/bin/backup", removed)


if __name__ == "__main__":
    unittest.main()
