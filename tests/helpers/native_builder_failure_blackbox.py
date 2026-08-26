#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts" / "codex-builder-loop.py"
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from harness import add_v4_progress_contract  # noqa: E402


def run(argv: Sequence[str | Path], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(item) for item in argv],
        cwd=str(cwd) if cwd is not None else None,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def git(repo: Path, *args: str) -> str:
    result = run(["git", "-C", repo, *args])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def contract() -> dict[str, Any]:
    value = {
        "schema_version": 4,
        "mission": {
            "revision": 1,
            "objective": "Preserve a Builder side effect after Native transport failure.",
            "behaviors": [
                {
                    "id": "failure-safety",
                    "description": "A dirty Builder candidate is never retried blindly.",
                }
            ],
            "interfaces": [
                {
                    "id": "native-driver-cli",
                    "description": "The public Native Driver CLI records the failure.",
                }
            ],
            "acceptance_cases": [
                {
                    "id": "dirty-builder-failure",
                    "description": "A Builder write followed by transport loss becomes a failure.",
                    "observation": {
                        "surface_id": "native-driver-cli",
                        "surface_description": "The public Native Driver start command and status output.",
                        "execution_ids": ["builder-failure-blackbox"],
                        "required_dimensions": ["mechanical", "verify"],
                    },
                }
            ],
            "trust_boundaries": [
                {
                    "id": "candidate-manifest",
                    "description": "The ledger records the candidate worktree observation.",
                }
            ],
        },
        "authority": {
            "target_branch": "main",
            "builder_write": ["src/**"],
            "tester_write": ["tests/**"],
            "dirty_intake": [],
            "public_prerequisites": [],
            "protected_support_paths": [],
            "external_targets": [],
        },
        "assurance": {
            "required": ["machine", "blackbox", "reviewer"],
            "machine_commands": [
                {
                    "id": "fixture-machine",
                    "argv": ["/usr/bin/python3", "-c", "raise SystemExit(0)"],
                    "timeout_seconds": 30,
                    "expected_returncodes": [0],
                    "run_before_full_suite": False,
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
                    "id": "builder-failure-blackbox",
                    "argv": ["/usr/bin/python3", "-c", "raise SystemExit(0)"],
                    "timeout_seconds": 30,
                    "expected_returncodes": [0],
                }
            ],
            "agents": {},
        },
    }
    return add_v4_progress_contract(value)


def fake_codex(path: Path, trace: Path) -> Path:
    path.write_text(
        f"""#!/usr/bin/env python3
import json, os, sys
trace = {str(trace)!r}
if sys.argv[1:] == ['--version']:
    print('codex-cli native-builder-failure-blackbox')
    raise SystemExit(0)
if sys.argv[1:3] == ['app-server', 'generate-json-schema']:
    out = sys.argv[sys.argv.index('--out') + 1]
    os.makedirs(out, exist_ok=True)
    tokens = ['thread/start','thread/resume','thread/read','turn/start',
              'turn/interrupt','developerInstructions','outputSchema',
              'clientUserMessageId']
    open(os.path.join(out, 'codex_app_server_protocol.schemas.json'), 'w').write(
        json.dumps(tokens)
    )
    raise SystemExit(0)
if sys.argv[1:3] != ['app-server', '--stdio']:
    raise SystemExit(2)
for line in sys.stdin:
    message = json.loads(line)
    method = message.get('method')
    if method == 'initialize':
        print(json.dumps({{'id': message['id'], 'result': {{}}}}), flush=True)
    elif method == 'initialized':
        pass
    elif method == 'thread/start':
        print(json.dumps({{'id': message['id'], 'result': {{'thread': {{'id': 'builder-blackbox-thread'}}}}}}), flush=True)
    elif method == 'thread/resume':
        print(json.dumps({{'id': message['id'], 'result': {{'thread': {{'id': message['params']['threadId']}}}}}}), flush=True)
    elif method == 'thread/read':
        print(json.dumps({{'id': message['id'], 'result': {{'thread': {{'id': message['params']['threadId'], 'turns': []}}}}}}), flush=True)
    elif method == 'turn/start':
        with open(trace, 'a', encoding='utf-8') as stream:
            stream.write(message['params']['clientUserMessageId'] + '\\n')
        candidate = os.path.join(message['params']['cwd'], 'src', 'native-side-effect.txt')
        with open(candidate, 'w', encoding='utf-8') as stream:
            stream.write('written before transport loss\\n')
        print(json.dumps({{'id': message['id'], 'result': {{'turn': {{'id': 'builder-blackbox-turn'}}}}}}), flush=True)
        raise SystemExit(0)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def parse_last_json(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(result.stderr.strip() or "public CLI returned no JSON")
    value = json.loads(lines[-1])
    if not isinstance(value, dict):
        raise RuntimeError(f"public CLI returned non-object JSON: {value!r}")
    return value


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="native-builder-failure-blackbox-") as raw:
        root = Path(raw)
        repo = root / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "native-builder-failure@test.local")
        git(repo, "config", "user.name", "native builder failure blackbox")
        (repo / "src").mkdir()
        (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        (repo / "README.md").write_text("blackbox\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "test(blackbox): [cr_id_skip] Seed Fixture")

        contract_path = root / "contract.json"
        contract_path.write_text(json.dumps(contract(), ensure_ascii=False), encoding="utf-8")
        trace = root / "turns.txt"
        codex = fake_codex(root / "codex", trace)
        run_id = "native-builder-failure-blackbox"
        started = run(
            [
                sys.executable,
                str(CLI),
                "native-driver",
                "--codex-bin",
                codex,
                "start",
                "--repo",
                repo,
                "--run",
                run_id,
                "--session-id",
                "native-builder-failure-blackbox-session",
                "--contract",
                contract_path,
            ],
            cwd=ROOT,
        )
        payload = parse_last_json(started)
        if (
            started.returncode != 2
            or payload.get("phase") != "failed"
            or payload.get("driver_failure", {}).get("code")
            != "NATIVE_BUILDER_SIDE_EFFECT_RETRY_BLOCKED"
        ):
            raise RuntimeError(
                f"dirty Builder failure did not fail closed: rc={started.returncode}, payload={payload}"
            )

        status_result = run(
            [
                sys.executable,
                str(CLI),
                "assurance",
                "--experimental-v4",
                "status",
                "--repo",
                repo,
                "--run",
                run_id,
            ],
            cwd=ROOT,
        )
        status = parse_last_json(status_result)
        observation = status.get("driver_failure", {}).get("observation", {})
        manifest = observation.get("candidate_manifest")
        side_effect = (
            Path(status["candidate_worktree"]) / "src" / "native-side-effect.txt"
        )
        if (
            status.get("phase") != "failed"
            or not side_effect.is_file()
            or not isinstance(manifest, dict)
            or "src/native-side-effect.txt" not in manifest.get("dirty_paths", [])
        ):
            raise RuntimeError(f"failure observation was not published: {status}")

        turns = trace.read_text(encoding="utf-8").splitlines()
        if len(turns) != 1:
            raise RuntimeError(f"Builder action was retried: {turns!r}")
        entry = next(
            item
            for item in manifest.get("entries", [])
            if item.get("path") == "src/native-side-effect.txt"
        )
        expected_sha256 = hashlib.sha256(side_effect.read_bytes()).hexdigest()
        if entry.get("sha256") != expected_sha256:
            raise RuntimeError(f"manifest content hash mismatch: {entry}")

        print(
            json.dumps(
                {
                    "result": "pass",
                    "failure_code": status["driver_failure"]["code"],
                    "turn_count": len(turns),
                    "manifest_digest": manifest["manifest_digest"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
