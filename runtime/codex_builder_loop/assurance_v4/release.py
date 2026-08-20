from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import jsonschema

from .core import AssuranceError, retrospective_status
from .models import digest
from .store import atomic_write_json, locked, now, resolve_repo, state_root


RELEASE_STAGES = ("tag", "github-release", "install-smoke")


def _schema() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[3] / "schema" / "assurance-v4-release.schema.json"
    return json.loads(path.read_text())


def _validate(value: dict[str, Any]) -> dict[str, Any]:
    jsonschema.Draft202012Validator(_schema()).validate(value)
    return value


def _intent_path(repo: Path, intent_id: str) -> Path:
    return state_root(repo) / "release-intents" / f"{intent_id}.json"


def _read_intent(repo: Path, intent_id: str) -> dict[str, Any]:
    path = _intent_path(repo, intent_id)
    try:
        return _validate(json.loads(path.read_text()))
    except FileNotFoundError as exc:
        raise AssuranceError(
            "release intent was not found",
            code="RELEASE_INTENT_NOT_FOUND",
            status="FAIL",
        ) from exc
    except (OSError, json.JSONDecodeError, jsonschema.ValidationError) as exc:
        raise AssuranceError(
            "release intent is invalid",
            code="RELEASE_INTENT_INVALID",
            status="FATAL",
        ) from exc


def _version_identity(repo: Path) -> dict[str, Any]:
    from ..core import capture_runtime_identity, BUILDER_LOOP_VERSION

    identity = capture_runtime_identity()
    identity["version"] = BUILDER_LOOP_VERSION
    return identity


def release_preflight(
    repo_value: str | Path,
    *,
    session_id: str,
    version: str,
    tag: str,
    release_commit: str,
    remote: str = "origin",
    replace_intent: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    session_id = session_id.strip()
    if not session_id:
        raise AssuranceError("session id is required", code="SESSION_ID_REQUIRED", status="FAIL")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", version):
        raise AssuranceError("release version is not SemVer", code="RELEASE_VERSION_INVALID", status="FAIL")
    if tag != f"v{version}":
        raise AssuranceError("release tag must be v<version>", code="RELEASE_TAG_INVALID", status="FAIL")
    if not re.fullmatch(r"[0-9a-f]{40}", release_commit):
        raise AssuranceError("release commit must be a full SHA-1", code="RELEASE_COMMIT_INVALID", status="FAIL")
    with locked(repo):
        status = retrospective_status(repo, session_id)
        if status.get("status") != "READY":
            raise AssuranceError(
                "retrospective is not READY for release",
                code="RELEASE_RETROSPECTIVE_NOT_READY",
                status="NEEDS_USER",
                details={"retrospective": status},
            )
        if status.get("derivation_status") != "verified":
            raise AssuranceError(
                "retrospective derivation identity is not verified",
                code="RELEASE_RETROSPECTIVE_DERIVATION_UNVERIFIED",
                status="NEEDS_USER",
            )
        snapshot = status["snapshot"]
        report = status["report"]
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        ).stdout.strip()
        if head != release_commit:
            raise AssuranceError(
                "release commit does not match current HEAD",
                code="RELEASE_COMMIT_HEAD_MISMATCH",
                status="FAIL",
                details={"head": head, "release_commit": release_commit},
            )
        identity = _version_identity(repo)
        if identity.get("version") != version or identity.get("adapter_commit") != release_commit:
            raise AssuranceError(
                "runtime version identity does not match release",
                code="RELEASE_RUNTIME_IDENTITY_MISMATCH",
                status="FAIL",
                details={"identity": identity, "version": version, "release_commit": release_commit},
            )
        intent_id = digest({
            "repo_root": str(repo),
            "owner_session_id": session_id,
            "version": version,
            "tag": tag,
            "release_commit": release_commit,
            "snapshot_digest": snapshot["snapshot_digest"],
            "report_digest": report["report_digest"],
            "derivation_identity_digest": snapshot["derivation_identity_digest"],
        })
        intent = {
            "schema_version": 1,
            "intent_id": intent_id,
            "repo_root": str(repo),
            "owner_session_id": session_id,
            "version": version,
            "tag": tag,
            "release_commit": release_commit,
            "snapshot_digest": snapshot["snapshot_digest"],
            "report_digest": report["report_digest"],
            "derivation_identity_digest": snapshot["derivation_identity_digest"],
            "state": "prepared",
            "next_stage": "tag",
            "receipts": [],
            "created_at": now(),
            "updated_at": now(),
        }
        if replace_intent:
            if not reason or not reason.strip():
                raise AssuranceError("replacement reason is required", code="RELEASE_REPLACE_REASON_REQUIRED", status="FAIL")
            old = _read_intent(repo, replace_intent)
            old["state"] = "superseded"
            old["superseded_at"] = now()
            old["superseded_by"] = intent_id
            old["updated_at"] = now()
            atomic_write_json(_intent_path(repo, replace_intent), old)
        atomic_write_json(_intent_path(repo, intent_id), intent)
        return {"status": "READY", "intent": intent, "remote": remote}


def _verify_tag(repo: Path, intent: dict[str, Any], remote: str) -> dict[str, Any]:
    local = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", f"refs/tags/{intent['tag']}^{{commit}}"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if local.returncode != 0 or local.stdout.strip() != intent["release_commit"]:
        raise AssuranceError("tag does not point to release commit", code="RELEASE_TAG_COMMIT_MISMATCH", status="FAIL")
    remote_result = subprocess.run(
        ["git", "-C", str(repo), "ls-remote", remote, f"refs/tags/{intent['tag']}^{{}}"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    observed = {
        "local_commit": local.stdout.strip(),
        "remote": remote,
        "remote_ref": remote_result.stdout.strip(),
    }
    if remote_result.returncode != 0 or not any(
        line.split()[0] == intent["release_commit"]
        for line in remote_result.stdout.splitlines()
        if line.split()
    ):
        raise AssuranceError("remote tag does not point to release commit", code="RELEASE_REMOTE_TAG_MISMATCH", status="FAIL", details=observed)
    return observed


def _verify_github_release(repo: Path, intent: dict[str, Any]) -> dict[str, Any]:
    result = subprocess.run(
        ["gh", "release", "view", intent["tag"], "--json", "tagName,targetCommitish,isDraft,isLatest"],
        cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode != 0:
        raise AssuranceError("GitHub Release could not be read back", code="RELEASE_GITHUB_READBACK_FAILED", status="FAIL", details={"stderr": result.stderr.strip()})
    try:
        observed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssuranceError("GitHub Release returned invalid JSON", code="RELEASE_GITHUB_INVALID_JSON", status="FAIL") from exc
    if observed.get("tagName") != intent["tag"] or observed.get("targetCommitish") not in {intent["release_commit"], intent["tag"]}:
        raise AssuranceError("GitHub Release target does not match release commit", code="RELEASE_GITHUB_COMMIT_MISMATCH", status="FAIL", details={"observed": observed})
    return observed


def _verify_install_smoke(intent: dict[str, Any], cli_path: str | None) -> dict[str, Any]:
    import os
    path = cli_path or os.path.expanduser("~/.local/bin/codex-builder-loop")
    result = subprocess.run([path, "version", "--json"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        raise AssuranceError("installed CLI smoke failed", code="RELEASE_INSTALL_SMOKE_FAILED", status="FAIL", details={"stderr": result.stderr.strip()})
    try:
        observed = json.loads([line for line in result.stdout.splitlines() if line.strip()][-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise AssuranceError("installed CLI returned invalid version JSON", code="RELEASE_INSTALL_SMOKE_INVALID_JSON", status="FAIL") from exc
    identity = observed.get("runtime_identity") or {}
    if observed.get("version") != intent["version"] or identity.get("adapter_commit") != intent["release_commit"]:
        raise AssuranceError("installed CLI identity does not match release", code="RELEASE_INSTALL_IDENTITY_MISMATCH", status="FAIL", details={"observed": observed})
    return {"path": path, "version": observed.get("version"), "runtime_identity": identity}


def release_verify(
    repo_value: str | Path,
    *,
    session_id: str,
    intent_id: str,
    stage: str,
    remote: str = "origin",
    installed_cli: str | None = None,
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    with locked(repo):
        intent = _read_intent(repo, intent_id)
        if intent["owner_session_id"] != session_id.strip():
            raise AssuranceError("release intent belongs to another session", code="RELEASE_SESSION_MISMATCH", status="FAIL")
        if stage not in RELEASE_STAGES:
            raise AssuranceError("unknown release stage", code="RELEASE_STAGE_INVALID", status="FAIL")
        index = RELEASE_STAGES.index(stage)
        if intent["next_stage"] != stage:
            if any(item.get("stage") == stage for item in intent["receipts"]):
                return {"status": "READY", "intent": intent, "idempotent": True}
            raise AssuranceError("release stages must be completed in order", code="RELEASE_STAGE_OUT_OF_ORDER", status="FAIL", details={"next_stage": intent["next_stage"]})
        status = retrospective_status(repo, intent["owner_session_id"])
        if (
            status.get("status") != "READY"
            or status.get("derivation_status") != "verified"
            or status.get("snapshot", {}).get("snapshot_digest")
            != intent["snapshot_digest"]
            or status.get("report", {}).get("report_digest")
            != intent["report_digest"]
        ):
            raise AssuranceError("release retrospective changed after preflight", code="RELEASE_RETROSPECTIVE_DRIFT", status="FAIL")
        if stage == "tag":
            observed = _verify_tag(repo, intent, remote)
        elif stage == "github-release":
            observed = _verify_github_release(repo, intent)
        else:
            observed = _verify_install_smoke(intent, installed_cli)
        receipt = {"stage": stage, "observed": observed, "observed_digest": digest(observed), "verified_at": now()}
        intent["receipts"].append(receipt)
        intent["next_stage"] = RELEASE_STAGES[index + 1] if index + 1 < len(RELEASE_STAGES) else "complete"
        intent["state"] = "ready" if intent["next_stage"] == "complete" else "in_progress"
        intent["updated_at"] = now()
        atomic_write_json(_intent_path(repo, intent_id), intent)
        return {"status": "RELEASE_READY" if intent["next_stage"] == "complete" else "READY", "intent": intent}
