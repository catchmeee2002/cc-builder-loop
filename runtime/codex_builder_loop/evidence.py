from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _glob_regex(pattern: str) -> re.Pattern[str]:
    pieces: list[str] = ["^"]
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    index += 1
                    pieces.append("(?:.*/)?")
                else:
                    pieces.append(".*")
                continue
            pieces.append("[^/]*")
        elif char == "?":
            pieces.append("[^/]")
        elif char == "[":
            end = pattern.find("]", index + 1)
            if end == -1:
                pieces.append(r"\[")
            else:
                pieces.append(pattern[index : end + 1])
                index = end
        else:
            pieces.append(re.escape(char))
        index += 1
    pieces.append("$")
    return re.compile("".join(pieces))


def path_matches(path: str, patterns: Iterable[str]) -> bool:
    return any(_glob_regex(pattern).fullmatch(path) for pattern in patterns)


def tree_entries(repo: Path, head: str, patterns: Iterable[str]) -> list[dict[str, str]]:
    selected = tuple(patterns)
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "-z", head],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-8000:])
    entries: list[dict[str, str]] = []
    for raw in result.stdout.split("\0"):
        if not raw or "\t" not in raw:
            continue
        metadata, path = raw.split("\t", 1)
        if not path_matches(path, selected):
            continue
        mode, object_type, oid = metadata.split()
        entries.append(
            {"path": path, "mode": mode, "type": object_type, "oid": oid}
        )
    return entries


def input_digest(
    repo: Path,
    head: str,
    *,
    patterns: Iterable[str],
    plan_sha256: str,
    context: dict[str, Any],
) -> str:
    normalized = tuple(sorted(set(patterns)))
    return canonical_digest(
        {
            "patterns": normalized,
            "tree": tree_entries(repo, head, normalized),
            "plan_sha256": plan_sha256,
            "context": context,
        }
    )


def record_head(record: Any) -> str | None:
    if not isinstance(record, dict):
        return None
    value = record.get("accepted_head")
    return str(value) if isinstance(value, str) and value else None


def make_record(
    *,
    kind: str,
    observed_head: str,
    accepted_head: str,
    input_sha256: str,
    scope: Iterable[str],
    provenance: dict[str, Any],
) -> dict[str, Any]:
    return {
        "kind": kind,
        "observed_head": observed_head,
        "accepted_head": accepted_head,
        "input_digest": input_sha256,
        "scope": sorted(set(scope)),
        "provenance": provenance,
    }


ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
TIMESTAMP_RE = re.compile(
    r"\b(?:20\d\d[-/]\d\d[-/]\d\d[ T]\d\d:\d\d(?::\d\d(?:\.\d+)?)?|\d+(?:\.\d+)?s)\b"
)
TEMP_PATH_RE = re.compile(r"/(?:tmp|var/tmp)/[^\s:'\"]+")


def failure_digests(text: str, *, stage: str, returncode: int) -> tuple[str, str]:
    raw = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    normalized = ANSI_RE.sub("", text)
    normalized = TIMESTAMP_RE.sub("<time>", normalized)
    normalized = TEMP_PATH_RE.sub("<tmp>", normalized)
    normalized = "\n".join(line.rstrip() for line in normalized.splitlines() if line.strip())
    normalized = normalized[-24000:]
    signature = canonical_digest(
        {"stage": stage, "returncode": returncode, "log": normalized}
    )
    return raw, signature
