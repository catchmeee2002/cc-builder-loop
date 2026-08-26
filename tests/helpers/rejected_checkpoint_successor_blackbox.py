from __future__ import annotations

import json
import os
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__:
    from tests.helpers.checkout_snapshot import clone_checkout_snapshot
else:
    from checkout_snapshot import clone_checkout_snapshot


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from harness import add_v4_progress_contract  # noqa: E402
CANDIDATE_WORKTREE = ROOT
RUNTIME_FIXTURE = (
    "runtime/codex_builder_loop/assurance_v4/rejected_checkpoint_fixture.py"
)
BOUND_TEST_FIXTURE = "tests/test_bound_rejected_candidate.py"
SOURCE_CARRYOVER_FIXTURE = "src/publication_source.py"
PUBLIC_PREREQUISITE = "contracts/public.json"
PROBLEM_KEY = "runtime-preparation-required"
REJECTION_VARIANTS = (
    "dirty-worktree",
    "branch-diverged",
    "unauthorized-change",
    "stale-problem",
    "consumed-dispatch",
    "wrong-head",
    "terminal-source",
    "target-drift",
    "invalid-inherited-carryover",
)


def require(condition: bool, message: str, value: Any = None) -> None:
    if not condition:
        suffix = "" if value is None else f": {value!r}"
        raise AssertionError(message + suffix)


def run(
    argv: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path,
    input_text: str | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[int, str, str]:
    import subprocess

    child_env = os.environ.copy()
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    if env:
        child_env.update({str(key): str(value) for key, value in env.items()})
    completed = subprocess.run(
        [str(value) for value in argv],
        cwd=cwd,
        env=child_env,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return completed.returncode, completed.stdout, completed.stderr


def git(repo: Path, *args: str) -> str:
    returncode, stdout, stderr = run(["git", "-C", repo, *args], cwd=repo)
    require(returncode == 0, f"git {' '.join(args)} failed", stderr or stdout)
    return stdout.strip()


def clone_snapshot(destination: Path, *, source: Path = ROOT) -> str:
    return clone_checkout_snapshot(
        source,
        destination,
        user_email="successor-blackbox@test.local",
        user_name="Successor Blackbox",
        commit_message="test(assurance): [cr_id_skip] Freeze Rejected Successor Snapshot",
        branch_name="main",
    )


def _runtime():
    module_names = [
        name
        for name in sys.modules
        if name == "runtime" or name.startswith("runtime.")
    ]
    loaded = sys.modules.get("runtime.codex_builder_loop.assurance_v4.core")
    loaded_file = getattr(loaded, "__file__", None)
    loaded_from_candidate = (
        isinstance(loaded_file, str)
        and Path(loaded_file).resolve().is_relative_to(CANDIDATE_WORKTREE.resolve())
    )
    inserted_candidate = False
    if not loaded_from_candidate:
        for name in module_names:
            sys.modules.pop(name, None)
        sys.path.insert(0, str(CANDIDATE_WORKTREE))
        inserted_candidate = True
    from runtime.codex_builder_loop.assurance_v4 import core
    from runtime.codex_builder_loop.assurance_v4.models import digest
    from runtime.codex_builder_loop.assurance_v4.store import read_ledger, save_ledger
    if inserted_candidate:
        sys.path.pop(0)

    return core, digest, read_ledger, save_ledger


def _base_contract(
    repo: Path,
    *,
    tester_write: list[str] | None = None,
) -> dict[str, Any]:
    value = {
        "schema_version": 4,
        "mission": {
            "delivery_kind": "code",
            "revision": 1,
            "objective": "Exercise rejected-checkpoint successor admission.",
            "behaviors": [
                {
                    "id": "rejected-candidate",
                    "description": "A rejected candidate transfers only when ledger and Git facts match.",
                }
            ],
            "interfaces": [
                {
                    "id": "assurance-start",
                    "description": "The public Assurance v4 start transaction.",
                }
            ],
            "acceptance_cases": [
                {
                    "id": "rejected-successor",
                    "description": "Eligible continuity transfers and ineligible variants fail closed.",
                    "observation": {
                        "surface_id": "rejected-successor-cli",
                        "surface_description": "Public Assurance Core transactions in an isolated repository.",
                        "execution_ids": ["rejected-successor-blackbox"],
                        "required_dimensions": ["mechanical", "verify"],
                    },
                }
            ],
            "trust_boundaries": [
                {
                    "id": "ledger-git-only",
                    "description": "Eligibility comes from ledger facts and Git objects only.",
                }
            ],
        },
        "authority": {
            "target_branch": git(repo, "branch", "--show-current"),
            "builder_write": [RUNTIME_FIXTURE],
            "tester_write": tester_write or [],
            "dirty_intake": [],
            "public_prerequisites": [],
            "protected_support_paths": [],
            "external_targets": [],
        },
        "assurance": {
            "required": ["machine"],
            "machine_commands": [
                {
                    "id": "fixture-machine",
                    "argv": [sys.executable, "-c", "raise SystemExit(0)"],
                    "expected_returncodes": [0],
                    "timeout_seconds": 30,
                }
            ],
        },
        "execution": {
            "version": 1,
            "driver_enforced": False,
            "candidate_head": None,
            "builder_files": [],
            "tester_files": [],
            "tester_source": None,
            "continuation": None,
            "carryover": None,
            "deployment": None,
            "dirty_snapshot": [],
            "commands": [
                {
                    "id": "rejected-successor-blackbox",
                    "argv": [sys.executable, "-c", "raise SystemExit(0)"],
                    "expected_returncodes": [0],
                    "timeout_seconds": 30,
                }
            ],
            "agents": {},
        },
    }
    return add_v4_progress_contract(value)


def _snapshot(repo: Path, source: Mapping[str, Any], target_run: str) -> dict[str, Any]:
    source_worktree = Path(str(source["candidate_worktree"]))
    common = Path(git(repo, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = (repo / common).resolve()
    run_path = source_worktree.parent.parent / target_run
    target_branch = f"assurance-v4/{target_run}/candidate"
    return {
        "source_ledger": (source_worktree.parent / "ledger.json").read_bytes(),
        "source_head": git(source_worktree, "rev-parse", "HEAD"),
        "source_branch": git(
            repo,
            "rev-parse",
            f"refs/heads/{source['candidate_branch']}",
        ),
        "target_head": git(repo, "rev-parse", "HEAD"),
        "refs": git(repo, "show-ref"),
        "worktrees": git(repo, "worktree", "list", "--porcelain"),
        "target_run_exists": run_path.exists(),
        "target_ref_exists": run(
            ["git", "-C", repo, "show-ref", "--verify", f"refs/heads/{target_branch}"],
            cwd=repo,
        )[0]
        == 0,
    }


def _successor_contract(
    repo: Path,
    source: Mapping[str, Any],
    rejected_head: str,
    *,
    invalid_classification: bool = False,
    tester_write: list[str] | None = None,
) -> dict[str, Any]:
    core, _digest, read_ledger, _save_ledger = _runtime()
    ledger = read_ledger(repo, str(source["run_id"]))
    contract = _base_contract(repo, tester_write=tester_write)
    contract["mission"]["revision"] = 2
    contract["mission"]["objective"] = (
        "Transfer the exact persisted rejected checkpoint candidate."
    )
    contract["mission"]["supersedes"] = {
        "run_id": source["run_id"],
        "revision": ledger["facets"]["mission"]["revision"],
        "mission_digest": ledger["digests"]["mission"],
        "candidate_head": rejected_head,
    }
    lineage = core.lineage(repo, str(source["run_id"]))
    snapshot_digest, _problems = core._open_problem_snapshot(ledger)
    contract["execution"]["revision_transition"] = {
        "category": "mission_change",
        "predecessor_pressure_digest": lineage["pressure_digest"],
        "architecture_review": None,
    }
    contract["execution"]["prior_problem_dispositions"] = {
        "source_snapshot_digest": snapshot_digest,
        "items": [{"key": PROBLEM_KEY, "disposition": "included"}],
    }
    if invalid_classification:
        contract["execution"]["carryover"] = {
            "source_run_id": source["run_id"],
            "source_candidate_head": rejected_head,
            "files": [
                {
                    "path": "README.md",
                    "blob": git(repo, "rev-parse", f"{rejected_head}:README.md"),
                }
            ],
        }
    return contract


def _prepare_rejected_source(
    root: Path,
    *,
    include_bound_tester: bool = False,
) -> dict[str, Any]:
    repo = root / "repo"
    clone_snapshot(repo)
    core, digest, read_ledger, save_ledger = _runtime()
    contract = _base_contract(
        repo,
        tester_write=[BOUND_TEST_FIXTURE] if include_bound_tester else [],
    )
    source_run = "rejected-source"
    runtime_patch = __import__("unittest.mock", fromlist=["patch"]).patch.object(
        core,
        "_runtime_source_root",
        return_value=repo,
    )
    runtime_patch.start()
    try:
        started = core.start(repo, source_run, "rejected-source-session", contract)
    finally:
        runtime_patch.stop()
    builder = {"agent_id": "rejected-builder", "thread_id": "rejected-builder-thread"}
    ledger = read_ledger(repo, source_run)
    ledger["driver_runtime"] = {
        "kind": "native",
        "protocol_version": 1,
        "transport": "codex_app_server",
        "runtime_version": "fixture-native",
        "protocol_schema_digest": "a" * 64,
    }
    ledger["facets"]["execution"]["agents"]["builder"] = builder
    ledger["facets"]["execution"]["version"] += 1
    ledger["digests"] = core.facet_digests(ledger["facets"])
    save_ledger(repo, ledger)

    candidate = Path(started["candidate_worktree"])
    runtime_path = candidate / RUNTIME_FIXTURE
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_path.write_text("VALUE = 2\n", encoding="utf-8")
    if include_bound_tester:
        tester_path = candidate / BOUND_TEST_FIXTURE
        tester_path.parent.mkdir(parents=True, exist_ok=True)
        tester_path.write_text(
            "import unittest\n\n"
            "class BoundRejectedCandidateTest(unittest.TestCase):\n"
            "    def test_bound_candidate(self):\n"
            "        self.assertEqual(2, 2)\n",
            encoding="utf-8",
        )
    rejected_head = git(candidate, "rev-parse", "HEAD")
    git(candidate, "add", "-A")
    git(
        candidate,
        "-c",
        "commit.gpgSign=false",
        "commit",
        "-q",
        "-m",
        "feat(assurance): [cr_id_skip] Create Rejected Candidate",
    )
    rejected_head = git(candidate, "rev-parse", "HEAD")

    ledger = read_ledger(repo, source_run)
    if include_bound_tester:
        bound_blob = git(repo, "rev-parse", f"{rejected_head}:{BOUND_TEST_FIXTURE}")
        ledger["facets"]["execution"]["tester_files"] = [BOUND_TEST_FIXTURE]
        ledger["facets"]["execution"]["tester_source"] = {
            "head": rejected_head,
            "base_head": ledger["target_start_head"],
            "branch": ledger["candidate_branch"],
            "worktree": ledger["candidate_worktree"],
            "files": [{"path": BOUND_TEST_FIXTURE, "blob": bound_blob}],
            "replaces_files": [],
            "agent": {
                "agent_id": "bound-source-tester",
                "thread_id": "bound-source-tester-thread",
            },
        }
        ledger["facets"]["execution"]["agents"]["tester"] = {
            "agent_id": "bound-source-tester",
            "thread_id": "bound-source-tester-thread",
        }
        ledger["facets"]["execution"]["version"] += 1
        ledger["digests"] = core.facet_digests(ledger["facets"])
    runtime_patch.start()
    try:
        actual_support, required = core._runtime_support_for_changed_paths(
            repo,
            ledger,
            core.changed_files(repo, ledger["target_start_head"], rejected_head),
        )
        try:
            core._assert_runtime_support_contract(
                ledger["facets"], actual_support, required
            )
        except core.AssuranceError as error:
            core._record_runtime_preparation_problem(
                ledger,
                candidate_head=rejected_head,
                error=error,
            )
        else:
            raise AssertionError(
                "fixture did not produce a runtime preparation rejection"
            )
    finally:
        runtime_patch.stop()
    action_id = "b" * 64
    result = {
        "result": "implemented",
        "evidence_report": None,
        "proof_spec": None,
        "problem_report": None,
    }
    artifact = candidate.parent / "artifacts" / f"dispatch-{action_id}.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    ledger["dispatch_intent"] = {
        "action_id": action_id,
        "action": "builder_implement",
        "role": "builder",
        "thread_id": builder["thread_id"],
        "prompt_digest": "c" * 64,
        "output_schema_digest": "d" * 64,
        "state": "completed",
        "attempt": 1,
        "generation": 1,
        "created_at": "2026-08-12T00:00:00+00:00",
        "result_path": str(artifact),
        "result_digest": digest(result),
        "completed_at": "2026-08-12T00:00:01+00:00",
    }
    save_ledger(repo, ledger)
    return {
        "repo": repo,
        "source_run": source_run,
        "source": core.status(repo, source_run),
        "source_ledger": ledger,
        "candidate": candidate,
        "rejected_head": rejected_head,
        "action_id": action_id,
    }


def exercise_valid_rejected_successor(root: Path) -> dict[str, Any]:
    fixture = _prepare_rejected_source(root)
    core, _digest, read_ledger, _save_ledger = _runtime()
    source_before = core.status(fixture["repo"], fixture["source_run"])
    source_problem = next(
        deepcopy(item)
        for item in fixture["source_ledger"]["problems"]
        if item["key"] == PROBLEM_KEY and item["status"] == "open"
    )
    runtime_patch = __import__("unittest.mock", fromlist=["patch"]).patch.object(
        core, "_runtime_source_root", return_value=fixture["repo"]
    )
    runtime_patch.start()
    try:
        target = core.start(
            fixture["repo"],
            "rejected-target",
            "rejected-target-session",
            _successor_contract(
                fixture["repo"],
                source_before,
                fixture["rejected_head"],
            ),
        )
    finally:
        runtime_patch.stop()
    source_after = core.status(fixture["repo"], fixture["source_run"])
    target_ledger = read_ledger(fixture["repo"], "rejected-target")
    target_execution = target_ledger["facets"]["execution"]
    from runtime.codex_builder_loop.assurance_v4 import driver

    next_action = driver.next_action(fixture["repo"], "rejected-target")
    return {
        "source_phase_before": source_before["phase"],
        "source_phase_after": source_after["phase"],
        "target_phase": target["phase"],
        "lineage_revision_count": target["lineage"]["revision_count"],
        "roles_reused": bool(target_execution["agents"]),
        "evidence_reused": bool(target_ledger["evidence"]),
        "dispatch_reused": target_ledger["dispatch_intent"] is not None,
        "source_problem": source_problem,
        "target_problem": next(
            deepcopy(item)
            for item in target_ledger["problems"]
            if item["key"] == PROBLEM_KEY and item["status"] == "open"
        ),
        "target_problem_snapshot_digest": core._open_problem_snapshot(target_ledger)[0],
        "source_problem_snapshot_digest": core._open_problem_snapshot(
            fixture["source_ledger"]
        )[0],
        "next_action": next_action,
    }


def exercise_bound_tester_rejected_successor(root: Path) -> dict[str, Any]:
    fixture = _prepare_rejected_source(root, include_bound_tester=True)
    core, _digest, read_ledger, _save_ledger = _runtime()
    source = core.status(fixture["repo"], fixture["source_run"])
    runtime_patch = __import__("unittest.mock", fromlist=["patch"]).patch.object(
        core, "_runtime_source_root", return_value=fixture["repo"]
    )
    runtime_patch.start()
    try:
        target = core.start(
            fixture["repo"],
            "rejected-bound-target",
            "rejected-bound-target-session",
            _successor_contract(
                fixture["repo"],
                source,
                fixture["rejected_head"],
                tester_write=[BOUND_TEST_FIXTURE],
            ),
        )
    finally:
        runtime_patch.stop()
    ledger = read_ledger(fixture["repo"], "rejected-bound-target")
    execution = ledger["facets"]["execution"]
    return {
        "source_phase": core.status(fixture["repo"], fixture["source_run"])["phase"],
        "target_phase": target["phase"],
        "tester_source_reused": execution["tester_source"] is not None,
        "tester_files_reused": bool(execution["tester_files"]),
        "carryover": deepcopy(execution["carryover"]["files"]),
    }


def exercise_ordinary_successor(root: Path) -> dict[str, Any]:
    repo = root / "repo"
    clone_snapshot(repo)
    core, _digest, read_ledger, save_ledger = _runtime()
    source_run = "ordinary-source"
    runtime_patch = __import__("unittest.mock", fromlist=["patch"]).patch.object(
        core, "_runtime_source_root", return_value=repo
    )
    runtime_patch.start()
    try:
        started = core.start(
            repo,
            source_run,
            "ordinary-source-session",
            _base_contract(repo),
        )
    finally:
        runtime_patch.stop()
    candidate = Path(started["candidate_worktree"])
    changed = candidate / RUNTIME_FIXTURE
    changed.parent.mkdir(parents=True, exist_ok=True)
    changed.write_text("VALUE = 1\n", encoding="utf-8")
    git(candidate, "add", "-A")
    git(
        candidate,
        "-c",
        "commit.gpgSign=false",
        "commit",
        "-q",
        "-m",
        "feat(assurance): [cr_id_skip] Checkpoint Ordinary Candidate",
    )
    ordinary_head = git(candidate, "rev-parse", "HEAD")
    ledger = read_ledger(repo, source_run)
    ledger["facets"]["execution"]["candidate_head"] = ordinary_head
    ledger["facets"]["execution"]["builder_files"] = [RUNTIME_FIXTURE]
    ledger["facets"]["execution"]["version"] += 1
    ledger["digests"] = core.facet_digests(ledger["facets"])
    ledger["builder_checkpointed"] = True
    ledger["problems"].append(
        {
            "key": "ordinary-carryover-problem",
            "summary": "Carry the ordinary problem",
            "details": "The successor must preserve this open problem.",
            "owner": "builder",
            "status": "open",
            "producer": None,
            "candidate_head": ordinary_head,
            "recorded_at": "2026-08-12T00:00:00+00:00",
        }
    )
    save_ledger(repo, ledger)
    source = core.status(repo, source_run)
    contract = _successor_contract(repo, source, ordinary_head)
    contract["execution"]["prior_problem_dispositions"] = {
        "source_snapshot_digest": core._open_problem_snapshot(ledger)[0],
        "items": [
            {
                "key": "ordinary-carryover-problem",
                "disposition": "included",
            }
        ],
    }
    runtime_patch.start()
    try:
        target = core.start(
            repo,
            "ordinary-target",
            "ordinary-target-session",
            contract,
        )
    finally:
        runtime_patch.stop()
    return {
        "source_phase": core.status(repo, source_run)["phase"],
        "target_phase": target["phase"],
        "lineage_revision_count": target["lineage"]["revision_count"],
        "open_problem_keys": target["lineage"]["open_problem_keys"],
    }


def exercise_successor_publication(root: Path) -> dict[str, Any]:
    repo = root / "repo"
    clone_snapshot(repo)
    core, _digest, read_ledger, _save_ledger = _runtime()
    source_run = "publication-source"
    source_contract = _base_contract(repo)
    source_contract["authority"]["builder_write"].append(
        SOURCE_CARRYOVER_FIXTURE
    )
    runtime_patch = __import__("unittest.mock", fromlist=["patch"]).patch.object(
        core, "_runtime_source_root", return_value=repo
    )
    runtime_patch.start()
    try:
        source = core.start(
            repo,
            source_run,
            "publication-source-session",
            source_contract,
        )
    finally:
        runtime_patch.stop()

    source_candidate = Path(source["candidate_worktree"])
    source_file = source_candidate / SOURCE_CARRYOVER_FIXTURE
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("SOURCE_REVISION = 1\n", encoding="utf-8")
    git(source_candidate, "add", SOURCE_CARRYOVER_FIXTURE)
    git(
        source_candidate,
        "-c",
        "commit.gpgSign=false",
        "commit",
        "-q",
        "-m",
        "test(assurance): [cr_id_skip] Record Source Carryover",
    )
    source_head = git(source_candidate, "rev-parse", "HEAD")
    core.checkpoint_builder(repo, source_run)
    source_ledger = read_ledger(repo, source_run)
    source_status = core.status(repo, source_run)

    contract = _base_contract(repo)
    contract["mission"]["revision"] = 2
    contract["mission"]["objective"] = (
        "Publish a new public prerequisite from the successor Builder."
    )
    contract["mission"]["supersedes"] = {
        "run_id": source_run,
        "revision": source_ledger["facets"]["mission"]["revision"],
        "mission_digest": source_ledger["digests"]["mission"],
        "candidate_head": source_head,
    }
    contract["authority"]["builder_write"].extend(
        [SOURCE_CARRYOVER_FIXTURE, PUBLIC_PREREQUISITE]
    )
    contract["authority"]["tester_write"] = [
        "tests/test_public_prerequisite.py"
    ]
    contract["authority"]["public_prerequisites"] = [PUBLIC_PREREQUISITE]
    contract["assurance"]["required"].append("tester")
    contract["execution"]["revision_transition"] = {
        "category": "mission_change",
        "predecessor_pressure_digest": source_status["lineage"][
            "pressure_digest"
        ],
        "architecture_review": None,
    }
    contract["execution"]["prior_problem_dispositions"] = {
        "source_snapshot_digest": source_status["lineage"][
            "open_problem_snapshot_digest"
        ],
        "items": [],
    }

    prerequisite_absent = not (repo / PUBLIC_PREREQUISITE).exists() and not (
        source_candidate / PUBLIC_PREREQUISITE
    ).exists()
    validation = core.validate(contract, repo)
    runtime_patch.start()
    try:
        target = core.start(
            repo,
            "publication-target",
            "publication-target-session",
            contract,
        )
    finally:
        runtime_patch.stop()

    started_ledger = read_ledger(repo, "publication-target")
    started_execution = started_ledger["facets"]["execution"]
    carryover_before = deepcopy(started_execution["carryover"]["files"])
    target_candidate = Path(target["candidate_worktree"])
    public_file = target_candidate / PUBLIC_PREREQUISITE
    public_file.parent.mkdir(parents=True, exist_ok=True)
    public_file.write_text('{"version": 2}\n', encoding="utf-8")
    git(target_candidate, "add", PUBLIC_PREREQUISITE)
    git(
        target_candidate,
        "-c",
        "commit.gpgSign=false",
        "commit",
        "-q",
        "-m",
        "test(assurance): [cr_id_skip] Create Public Prerequisite",
    )
    target_head = git(target_candidate, "rev-parse", "HEAD")
    checkpointed = core.checkpoint_builder(repo, "publication-target")
    checkpointed_ledger = read_ledger(repo, "publication-target")
    published = core.publish_prerequisites(repo, "publication-target")
    published_ledger = read_ledger(repo, "publication-target")
    expected_publication = [
        {
            "path": PUBLIC_PREREQUISITE,
            "blob": git(
                repo,
                "rev-parse",
                f"{target_head}:{PUBLIC_PREREQUISITE}",
            ),
        }
    ]
    return {
        "validation_status": validation["status"],
        "prerequisite_absent_before_validation": prerequisite_absent,
        "source_phase_after_start": core.status(repo, source_run)["phase"],
        "target_phase": target["phase"],
        "source_head": source_head,
        "target_head": target_head,
        "builder_files_at_start": deepcopy(started_execution["builder_files"]),
        "tester_files_at_start": deepcopy(started_execution["tester_files"]),
        "tester_source_at_start": started_execution["tester_source"],
        "carryover_before": carryover_before,
        "public_in_carryover": PUBLIC_PREREQUISITE
        in {item["path"] for item in carryover_before},
        "publication_head_at_start": started_ledger["publication"]["head"],
        "publication_files_at_start": deepcopy(
            started_ledger["publication"]["files"]
        ),
        "checkpoint_builder_files": deepcopy(
            checkpointed_ledger["facets"]["execution"]["builder_files"]
        ),
        "checkpoint_candidate_head": checkpointed_ledger["facets"][
            "execution"
        ]["candidate_head"],
        "checkpoint_publication_head": checkpointed["publication"]["head"],
        "checkpoint_publication_files": deepcopy(
            checkpointed["publication"]["files"]
        ),
        "published_candidate_head": published["publication"][
            "candidate_head"
        ],
        "published_files": deepcopy(published["publication"]["files"]),
        "expected_publication": expected_publication,
        "carryover_after": deepcopy(
            published_ledger["facets"]["execution"]["carryover"]["files"]
        ),
    }


def _reject_variant(root: Path, variant: str) -> dict[str, Any]:
    fixture = _prepare_rejected_source(root)
    repo = fixture["repo"]
    core, _digest, read_ledger, save_ledger = _runtime()
    source = core.status(repo, fixture["source_run"])
    target_run = f"rejected-{variant}"
    contract = _successor_contract(repo, source, fixture["rejected_head"])
    if variant == "dirty-worktree":
        (fixture["candidate"] / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    elif variant == "branch-diverged":
        git(fixture["candidate"], "checkout", "--detach", fixture["rejected_head"])
    elif variant == "unauthorized-change":
        unauthorized = fixture["candidate"] / "README.md"
        unauthorized.write_text("unauthorized\n", encoding="utf-8")
        git(fixture["candidate"], "add", "README.md")
        git(
            fixture["candidate"],
            "-c",
            "commit.gpgSign=false",
            "commit",
            "-q",
            "-m",
            "test(assurance): [cr_id_skip] Add Unauthorized Change",
        )
        contract["mission"]["supersedes"]["candidate_head"] = git(
            fixture["candidate"], "rev-parse", "HEAD"
        )
    elif variant == "stale-problem":
        ledger = read_ledger(repo, fixture["source_run"])
        problem = next(item for item in ledger["problems"] if item["key"] == PROBLEM_KEY)
        problem["details"] = json.dumps(
            {"code": "RUNTIME_PREPARATION_REQUIRED", "message": "stale"},
            sort_keys=True,
            separators=(",", ":"),
        )
        save_ledger(repo, ledger)
    elif variant == "consumed-dispatch":
        ledger = read_ledger(repo, fixture["source_run"])
        ledger["dispatch_intent"] = None
        save_ledger(repo, ledger)
    elif variant == "wrong-head":
        contract["mission"]["supersedes"]["candidate_head"] = source[
            "target_start_head"
        ]
    elif variant == "terminal-source":
        ledger = read_ledger(repo, fixture["source_run"])
        ledger["phase"] = "abandoned"
        ledger["abandon_intent"] = {"reason": "terminal rejection fixture"}
        save_ledger(repo, ledger)
    elif variant == "target-drift":
        target = repo / "README.md"
        target.write_text(target.read_text(encoding="utf-8") + "target drift\n", encoding="utf-8")
        git(repo, "add", "README.md")
        git(
            repo,
            "-c",
            "commit.gpgSign=false",
            "commit",
            "-q",
            "-m",
            "chore(assurance): [cr_id_skip] Advance Target",
        )
    elif variant == "invalid-inherited-carryover":
        contract = _successor_contract(
            repo,
            source,
            fixture["rejected_head"],
            invalid_classification=True,
        )
    before = _snapshot(repo, source, target_run)
    runtime_patch = __import__("unittest.mock", fromlist=["patch"]).patch.object(
        core, "_runtime_source_root", return_value=repo
    )
    runtime_patch.start()
    try:
        core.start(repo, target_run, f"session-{target_run}", contract)
    except (core.AssuranceError, ValueError) as error:
        code = getattr(error, "code", type(error).__name__)
    else:
        return {"rejected": False, "zero_side_effects": False, "code": ""}
    finally:
        runtime_patch.stop()
    after = _snapshot(repo, source, target_run)
    return {
        "rejected": True,
        "zero_side_effects": before == after,
        "code": str(code),
    }


def exercise_rejection_matrix(root: Path) -> dict[str, dict[str, Any]]:
    return {
        variant: _reject_variant(root / variant, variant)
        for variant in REJECTION_VARIANTS
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="rejected-successor-blackbox-") as raw:
        root = Path(raw)
        valid = exercise_valid_rejected_successor(root / "valid")
        bound = exercise_bound_tester_rejected_successor(root / "bound")
        rejections = exercise_rejection_matrix(root / "rejections")
        ordinary = exercise_ordinary_successor(root / "ordinary")
        publication = exercise_successor_publication(root / "publication")
        require(valid["source_phase_after"] == "superseded", "valid source was not superseded", valid)
        require(bound["target_phase"] == "active", "bound candidate did not transfer", bound)
        require(all(item["rejected"] and item["zero_side_effects"] for item in rejections.values()), "a rejection mutated state", rejections)
        require(ordinary["target_phase"] == "active", "ordinary successor regressed", ordinary)
        require(
            publication["validation_status"] == "READY"
            and publication["prerequisite_absent_before_validation"],
            "successor contract was not ready before prerequisite creation",
            publication,
        )
        require(
            publication["source_phase_after_start"] == "superseded"
            and publication["target_phase"] == "active",
            "publication successor did not transfer continuity",
            publication,
        )
        require(
            publication["builder_files_at_start"] == []
            and publication["tester_files_at_start"] == []
            and publication["tester_source_at_start"] is None
            and not publication["public_in_carryover"],
            "successor start pre-classified new role output",
            publication,
        )
        require(
            publication["publication_head_at_start"] is None
            and publication["publication_files_at_start"] == []
            and publication["checkpoint_publication_head"] is None
            and publication["checkpoint_publication_files"] == [],
            "prerequisite was published before the explicit transaction",
            publication,
        )
        require(
            publication["checkpoint_builder_files"] == [PUBLIC_PREREQUISITE]
            and publication["checkpoint_candidate_head"]
            == publication["target_head"],
            "checkpoint did not classify the new prerequisite as Builder output",
            publication,
        )
        require(
            publication["published_candidate_head"] == publication["target_head"]
            and publication["published_files"]
            == publication["expected_publication"],
            "publication did not bind the exact prerequisite blob",
            publication,
        )
        require(
            publication["carryover_before"] == publication["carryover_after"]
            and publication["carryover_before"]
            == [
                {
                    "path": SOURCE_CARRYOVER_FIXTURE,
                    "blob": git(
                        root / "publication" / "repo",
                        "rev-parse",
                        f"{publication['source_head']}:{SOURCE_CARRYOVER_FIXTURE}",
                    ),
                }
            ],
            "source carryover was reclassified during prerequisite publication",
            publication,
        )
        print(
            json.dumps(
                {
                    "status": "pass",
                    "observations": {
                        "valid": valid,
                        "bound_tester": bound,
                        "rejections": rejections,
                        "ordinary": ordinary,
                        "publication": publication,
                    },
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
