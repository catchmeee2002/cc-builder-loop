from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import issue_triage_eval as evaluator  # noqa: E402
import issue_triage_shadow as shadow  # noqa: E402


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


def commit(repo: Path, message: str) -> str:
    run_git(repo, "add", ".")
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
        f"test(issue-triage): [cr_id_skip] {message.capitalize()}",
    )
    return run_git(repo, "rev-parse", "HEAD")


def capture_body(repository: str, head: str, *, dirty: bool) -> str:
    return f"""复现 `src/demo.py:1` 与 `run_step`。

<!-- issue-capture:v1 -->
```json
{{
  "captured_at": "2026-07-27T00:00:00Z",
  "repository": "{repository}",
  "incident_head": "{head}",
  "branch": "main",
  "dirty": {str(dirty).lower()},
  "root_cause_status": "unknown"
}}
```
<!-- /issue-capture:v1 -->
"""


class FakeClient:
    def __init__(self) -> None:
        self.calls = 0

    def request(self, **kwargs):
        self.calls += 1
        schema_name = kwargs["schema_name"]
        issue_id = kwargs["task_data"]["issues"][0]["issue_id"]
        if schema_name == "issue_triage_shadow_diagnosis":
            value = {
                "issue_assessments": [
                    {
                        "issue_id": issue_id,
                        "principle_ids": ["P1"],
                        "invariant": "失败不能冒充成功",
                        "root_cause": "异常状态被折叠",
                        "root_cause_status": "established",
                        "surviving_alternatives": [],
                        "diagnostic_missing_evidence": [],
                        "scope_notes": [],
                        "flags": {
                            "goal_or_taste": False,
                            "new_or_changed_principle": False,
                            "principle_conflict": False,
                            "public_contract_or_role_boundary": False,
                            "wide_scope": False,
                            "hard_to_reverse": False,
                            "deterministic_acceptance": True,
                        },
                        "proposed_cluster_id": "single-issue",
                    }
                ],
                "clusters": [
                    {
                        "cluster_id": "single-issue",
                        "issue_ids": [issue_id],
                        "shared_invariant": "失败不能冒充成功",
                        "why_same": "单例",
                    }
                ],
            }
        else:
            value = {
                "attacks": [
                    {
                        "issue_id": issue_id,
                        "diagnosis_verdict": "stands",
                        "cluster_verdict": "stands",
                        "cluster_reason": "单例",
                        "human_attention_escalation": "none",
                        "reason": "证据闭合",
                        "surviving_alternative": "none",
                        "surviving_alternative_reason": "没有竞争解释",
                        "diagnostic_missing_evidence": [],
                        "scope_notes": [],
                        "scope_inventory_required": False,
                        "principle_conflict": False,
                    }
                ]
            }
        return evaluator.meta.ApiResult(value=value, request_hash=f"hash-{self.calls}")


class IssueTriageShadowTests(unittest.TestCase):
    def test_capture_and_resolution_markers_are_exact_and_validated(self):
        head = "a" * 40
        body = capture_body("catch/repo", head, dirty=False)
        resolution_body = f"""<!-- issue-resolution:v1 -->
```json
{{
  "resolved_at": "2026-07-27T01:00:00Z",
  "outcome": "fixed",
  "incident_head": "{head}",
  "resolved_head": "{'b' * 40}",
  "fix_commits": ["{'b' * 40}"],
  "root_cause_status": "confirmed",
  "root_cause": "状态被错误折叠",
  "violated_invariant": "失败不能冒充成功",
  "human_decision": {{"required": false, "kinds": [], "evidence": []}},
  "acceptance": {{"deterministic": true, "evidence": ["tests pass"]}},
  "residual_uncertainty": []
}}
```
<!-- /issue-resolution:v1 -->"""

        capture = shadow.parse_capture(body, expected_repository="catch/repo")
        resolution = shadow.parse_latest_resolution([{"body": resolution_body}], capture=capture)

        self.assertEqual(capture["incident_head"], head)
        self.assertEqual(resolution["outcome"], "fixed")
        with self.assertRaises(evaluator.meta.RunnerError):
            shadow.parse_capture(body + body, expected_repository="catch/repo")
        with self.assertRaises(evaluator.meta.RunnerError):
            shadow.parse_marker_json(
                "<!-- /issue-capture:v1 -->\n<!-- issue-capture:v1 -->\n```json\n{}\n```",
                shadow.CAPTURE_MARKER,
            )

    def test_remote_credentials_are_redacted_and_slug_is_stable(self):
        remote = "https://user:ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ@github.com/catch/repo.git"

        sanitized = shadow.sanitize_remote(remote)

        self.assertEqual(sanitized, "https://github.com/catch/repo.git")
        self.assertEqual(shadow.github_repo_from_remote(remote), "catch/repo")
        self.assertNotIn("ghp_", sanitized)

    def test_file_evidence_is_scoped_to_repo(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "src").mkdir()
            (repo / "src" / "demo.py").write_text("one\ntwo\nthree\n", encoding="utf-8")

            excerpt = shadow._safe_file_excerpt(repo, "src/demo.py:2")
            escaped = shadow._safe_file_excerpt(repo, "../secret.txt")

        self.assertEqual(excerpt["line_start"], 1)
        self.assertIn("2: two", excerpt["excerpt"])
        self.assertIsNone(escaped)

    def test_collect_evidence_reads_explicit_paths_and_identifiers(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            run_git(repo, "init")
            (repo / "src").mkdir()
            (repo / "src" / "demo.py").write_text("def run_step():\n    return 1\n", encoding="utf-8")
            run_git(repo, "add", "src/demo.py")
            issue = {"body": "检查 `src/demo.py:1` 与 `run_step`。"}

            evidence = shadow.collect_evidence(repo, issue, {"head": "abc"})

        self.assertEqual(evidence["file_refs"][0]["path"], "src/demo.py")
        self.assertEqual(evidence["identifier_searches"][0]["identifier"], "run_step")
        self.assertTrue(evidence["identifier_searches"][0]["hits"])

    def test_stage_cache_prevents_repeat_model_calls(self):
        project = evaluator.Project(
            project_id="p",
            goal="目标",
            principles=(evaluator.Principle("P1", "原则"),),
            cases=(
                evaluator.Case(
                    id="issue-1",
                    source_url="https://example.invalid/1",
                    title="问题",
                    facts=("事实",),
                    gold=evaluator.Gold(
                        diagnosis_state="established",
                        human_attention="none",
                        scope_inventory_required=False,
                        cluster_id="shadow-single",
                        principle_ids=("P1",),
                    ),
                ),
            ),
        )
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)

            first = shadow._single_pass(
                client=client,
                project=project,
                effort="high",
                run_dir=run_dir,
                prefix="main",
            )
            second = shadow._single_pass(
                client=client,
                project=project,
                effort="high",
                run_dir=run_dir,
                prefix="main",
            )

        self.assertEqual(first["work_queue"], "agent_execute")
        self.assertEqual(second["work_queue"], "agent_execute")
        self.assertEqual(client.calls, 2)

    def test_profile_extracts_referenced_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "principles.md").write_text(
                "# Root\n\n## Keep\nA\n\n## Skip\nB\n",
                encoding="utf-8",
            )
            profiles = repo / "profiles.json"
            profiles.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "profiles": {
                            "catch/repo": {
                                "goal": "目标",
                                "principle_sources": [
                                    {"path": "principles.md", "heading_patterns": ["^Keep$"]}
                                ],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            goal, principles = shadow.load_profile(repo, "catch/repo", profiles)

        self.assertEqual(goal, "目标")
        self.assertEqual(len(principles), 1)
        self.assertIn("## Keep", principles[0].text)
        self.assertNotIn("## Skip", principles[0].text)

    def test_historical_shadow_uses_exact_incident_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            run_git(repo, "init")
            (repo / "principles.md").write_text("# Root\n\n## Keep\n原则\n", encoding="utf-8")
            (repo / "src").mkdir()
            (repo / "src" / "demo.py").write_text("def run_step():\n    return 'incident'\n", encoding="utf-8")
            incident_head = commit(repo, "incident")
            (repo / "src" / "demo.py").write_text("def run_step():\n    return 'fixed'\n", encoding="utf-8")
            commit(repo, "fixed")
            profiles = root / "profiles.json"
            profiles.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "profiles": {
                            "catch/repo": {
                                "goal": "目标",
                                "principle_sources": [
                                    {"path": "principles.md", "heading_patterns": ["^Keep$"]}
                                ],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            body = capture_body("catch/repo", incident_head, dirty=False)
            capture = shadow.parse_capture(body, expected_repository="catch/repo")
            client = FakeClient()
            result = shadow.run_shadow_from_issue(
                repo=repo,
                github_repo="catch/repo",
                issue={
                    "number": 1,
                    "title": "问题",
                    "body": body,
                    "createdAt": "2026-07-27T00:00:00Z",
                    "url": "https://example.invalid/1",
                    "author": {"login": "tester"},
                    "comments": [{"body": "不能泄漏的事后结论"}],
                },
                capture=capture,
                run_root=root / "runs",
                main_effort="high",
                boundary_effort=None,
                profiles_path=profiles,
                work_root=root / "worktrees",
                client=client,
            )
            repeated = shadow.run_shadow_from_issue(
                repo=repo,
                github_repo="catch/repo",
                issue={
                    "number": 1,
                    "title": "问题",
                    "body": body,
                    "createdAt": "2026-07-27T00:00:00Z",
                    "url": "https://example.invalid/1",
                    "author": {"login": "tester"},
                    "comments": [{"body": "不同的事后评论仍不能进入历史输入"}],
                },
                capture=capture,
                run_root=root / "runs",
                main_effort="high",
                boundary_effort=None,
                profiles_path=profiles,
                work_root=root / "worktrees",
                client=client,
            )
            payload = json.loads((Path(result["run_dir"]) / "input.json").read_text(encoding="utf-8"))

        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertIn("incident", serialized)
        self.assertNotIn("return 'fixed'", serialized)
        self.assertNotIn("不能泄漏的事后结论", serialized)
        self.assertEqual(repeated["run_dir"], result["run_dir"])
        self.assertEqual(client.calls, 2)

    def test_dirty_capture_disables_file_and_identifier_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            run_git(repo, "init")
            (repo / "principles.md").write_text("# Root\n\n## Keep\n原则\n", encoding="utf-8")
            (repo / "src").mkdir()
            (repo / "src" / "demo.py").write_text("def run_step():\n    return 'secret-dirty'\n", encoding="utf-8")
            incident_head = commit(repo, "incident")
            profiles = root / "profiles.json"
            profiles.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "profiles": {
                            "catch/repo": {
                                "goal": "目标",
                                "principle_sources": [
                                    {"path": "principles.md", "heading_patterns": ["^Keep$"]}
                                ],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            body = capture_body("catch/repo", incident_head, dirty=True)
            capture = shadow.parse_capture(body, expected_repository="catch/repo")
            result = shadow.run_shadow_from_issue(
                repo=repo,
                github_repo="catch/repo",
                issue={
                    "number": 2,
                    "title": "问题",
                    "body": body,
                    "createdAt": "2026-07-27T00:00:00Z",
                    "url": "https://example.invalid/2",
                    "author": {"login": "tester"},
                },
                capture=capture,
                run_root=root / "runs",
                main_effort="high",
                boundary_effort=None,
                profiles_path=profiles,
                client=FakeClient(),
            )
            payload = json.loads((Path(result["run_dir"]) / "input.json").read_text(encoding="utf-8"))

        self.assertEqual(payload["evidence"]["file_refs"], [])
        self.assertEqual(payload["evidence"]["identifier_searches"], [])
        self.assertIn("未提交 dirty 现场不可重建", payload["evidence"]["repo_identity"]["evidence_limitation"])


if __name__ == "__main__":
    unittest.main()
