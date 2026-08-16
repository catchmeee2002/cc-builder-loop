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
CLI_PATH = Path("scripts/codex-builder-loop.py")
MANIFEST_PATH = Path(
    "runtime/codex_builder_loop/assurance_v4/runtime-support.json"
)
PROOF_RUNTIME_PATH = "runtime/codex_builder_loop/assurance_v4/core.py"


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


def git(
    repo: Path,
    *args: str,
    check: bool = True,
    input_text: str | None = None,
) -> str:
    returncode, stdout, stderr = run(
        ["git", "-C", repo, *args],
        cwd=repo,
        input_text=input_text,
    )
    if check:
        require(returncode == 0, f"git {' '.join(args)} failed", stderr or stdout)
    return stdout


def clone_current_snapshot(destination: Path) -> str:
    return clone_checkout_snapshot(
        ROOT,
        destination,
        user_email="runtime-preparation@test.local",
        user_name="Runtime Preparation Blackbox",
        commit_message="test(assurance): [cr_id_skip] Freeze Runtime Preparation Snapshot",
        disable_gc=True,
    )


def parse_json(stdout: str, stderr: str) -> dict[str, Any]:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    require(bool(lines), "command produced no JSON", stderr)
    value = json.loads(lines[-1])
    require(isinstance(value, dict), "command JSON is not an object", value)
    return value


def assurance(
    repo: Path,
    command: str,
    *args: str,
    contract: Mapping[str, Any],
) -> tuple[int, dict[str, Any]]:
    runtime_dir = repo.parent / "xdg-runtime"
    runtime_dir.mkdir(mode=0o700, exist_ok=True)
    returncode, stdout, stderr = run(
        [
            sys.executable,
            repo / CLI_PATH,
            "assurance",
            "--experimental-v4",
            command,
            *args,
            "--contract",
            "-",
        ],
        cwd=repo,
        input_text=json.dumps(contract, ensure_ascii=False),
        env={"XDG_RUNTIME_DIR": str(runtime_dir)},
    )
    return returncode, parse_json(stdout, stderr)


def contract(repo: Path, *, preparation: bool) -> dict[str, Any]:
    branch = git(repo, "branch", "--show-current").strip()
    require(bool(branch), "snapshot clone has no current branch")
    required = ["machine", "blackbox", "reviewer"]
    if not preparation:
        required.insert(1, "proof")
    return {
        "schema_version": 4,
        "mission": {
            "delivery_kind": "preparation" if preparation else "code",
            "revision": 1,
            "objective": "Exercise self-hosted runtime preparation admission.",
            "behaviors": [
                {
                    "id": "runtime-preparation",
                    "description": "Runtime support changes use independent evidence.",
                }
            ],
            "interfaces": [
                {
                    "id": "assurance-cli",
                    "description": "The public Assurance v4 CLI admission surface.",
                }
            ],
            "acceptance_cases": [
                {
                    "id": "runtime-preparation-admission",
                    "description": "The CLI classifies self-hosted proof writer changes.",
                    "observation": {
                        "surface_id": "assurance-cli",
                        "surface_description": "The real Assurance v4 CLI output.",
                        "execution_ids": ["runtime-preparation-blackbox"],
                        "required_dimensions": ["verify"],
                    },
                }
            ],
            "trust_boundaries": [
                {
                    "id": "frozen-runtime-head",
                    "description": "Classification comes from the frozen runtime commit.",
                }
            ],
        },
        "authority": {
            "target_branch": branch,
            "builder_write": [PROOF_RUNTIME_PATH],
            "tester_write": [],
            "dirty_intake": [],
            "public_prerequisites": [],
            "protected_support_paths": (
                [PROOF_RUNTIME_PATH] if preparation else []
            ),
            "external_targets": [],
        },
        "assurance": {
            "required": required,
            "machine_commands": [
                {
                    "id": "runtime-preparation-machine",
                    "argv": [sys.executable, "-c", "raise SystemExit(0)"],
                    "expected_returncodes": [0],
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
                    "id": "runtime-preparation-blackbox",
                    "argv": [sys.executable, "-c", "raise SystemExit(0)"],
                    "expected_returncodes": [0],
                    "timeout_seconds": 30,
                }
            ],
            "agents": {},
        },
    }


def repository_facts(repo: Path) -> dict[str, Any]:
    common = Path(git(repo, "rev-parse", "--git-common-dir").strip())
    if not common.is_absolute():
        common = (repo / common).resolve()
    return {
        "refs": git(repo, "show-ref", check=False),
        "worktrees": git(repo, "worktree", "list", "--porcelain"),
        "status": git(repo, "status", "--porcelain=v1"),
        "state_exists": (common / "builder-loop-assurance-v4").exists(),
    }


def exercise(repo: Path, runtime_head: str) -> dict[str, Any]:
    normal = contract(repo, preparation=False)
    before = repository_facts(repo)
    normal_rc, normal_result = assurance(
        repo,
        "start",
        "--repo",
        str(repo),
        "--run",
        "runtime-preparation-blackbox-normal",
        "--session-id",
        "runtime-preparation-blackbox-session",
        contract=normal,
    )
    after = repository_facts(repo)
    require(normal_rc == 1, "normal self-hosted start returned the wrong code", normal_result)
    require(normal_result.get("status") == "NEEDS_USER", "normal start did not stop", normal_result)
    require(
        normal_result.get("code") == "RUNTIME_PREPARATION_REQUIRED",
        "normal start reported the wrong reason",
        normal_result,
    )
    require(before == after, "rejected start changed repository or run state", {"before": before, "after": after})

    preparation = contract(repo, preparation=True)
    ready_rc, ready = assurance(
        repo,
        "validate",
        "--repo",
        str(repo),
        contract=preparation,
    )
    require(ready_rc == 0, "protected preparation validation failed", ready)
    require(ready.get("status") == "READY", "protected preparation is not READY", ready)
    support = ready.get("runtime_support")
    require(isinstance(support, dict), "READY result omitted runtime support", ready)
    require(support.get("mode") == "self_hosted", "runtime was not classified self-hosted", support)
    require(support.get("runtime_head") == runtime_head, "runtime HEAD was not frozen", support)
    require(
        support.get("manifest_blob")
        == git(repo, "rev-parse", f"HEAD:{MANIFEST_PATH.as_posix()}").strip(),
        "runtime manifest blob was not frozen",
        support,
    )
    require(support.get("affected_paths") == [PROOF_RUNTIME_PATH], "affected path mismatch", support)
    require(support.get("affected_gates") == ["proof"], "affected gate mismatch", support)
    require(repository_facts(repo) == after, "validate changed repository or run state")

    cyclic = deepcopy(preparation)
    cyclic["assurance"]["required"].append("proof")
    cycle_rc, cycle = assurance(
        repo,
        "validate",
        "--repo",
        str(repo),
        contract=cyclic,
    )
    require(cycle_rc == 1, "cyclic preparation returned the wrong code", cycle)
    require(cycle.get("status") == "NEEDS_USER", "cyclic preparation did not stop", cycle)
    require(
        cycle.get("code") == "RUNTIME_PREPARATION_GATE_CYCLE",
        "cyclic preparation reported the wrong reason",
        cycle,
    )
    require(cycle.get("cyclic_gates") == ["proof"], "cyclic gate details changed", cycle)
    require(repository_facts(repo) == after, "cycle rejection changed repository or run state")

    return {
        "normal_start": {
            "code": normal_result["code"],
            "zero_side_effects": True,
        },
        "protected_preparation": {
            "status": ready["status"],
            "runtime_head": support["runtime_head"],
            "affected_gates": support["affected_gates"],
            "affected_paths": support["affected_paths"],
        },
        "gate_cycle": {
            "code": cycle["code"],
            "cyclic_gates": cycle["cyclic_gates"],
        },
    }


def main() -> int:
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    with tempfile.TemporaryDirectory(prefix="runtime-preparation-blackbox-") as raw:
        repo = Path(raw) / "repo"
        runtime_head = clone_current_snapshot(repo)
        observations = exercise(repo, runtime_head)
    print(json.dumps({"status": "pass", "observations": observations}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
