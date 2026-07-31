#!/usr/bin/env python3
"""Transactionally register or remove Codex builder-loop configuration."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shlex
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


BLOCK_START = "<!-- BEGIN cc-builder-loop-codex -->"
BLOCK_END = "<!-- END cc-builder-loop-codex -->"
BLOCK_SEPARATOR = "<!-- cc-builder-loop-codex managed separator -->"
BLOCK_PATTERN = re.compile(
    re.escape(BLOCK_START) + r".*?" + re.escape(BLOCK_END), re.DOTALL
)


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Target:
    logical: Path
    destination: Path
    symlink: bool


@dataclass(frozen=True)
class Snapshot:
    path: Path
    existed: bool
    content: bytes | None
    mode: int | None


@dataclass(frozen=True)
class Operation:
    path: Path
    action: Literal["write", "delete"]
    content: bytes | None = None
    mode: int | None = None


def resolve_target(path: Path, label: str) -> Target:
    if path.is_symlink():
        try:
            destination = path.resolve(strict=True)
        except OSError as exc:
            raise ConfigError(f"dangling {label} symlink; refusing to edit {path}: {exc}") from exc
        return Target(path, destination, True)
    return Target(path, path, False)


def read_text(path: Path, label: str) -> str:
    try:
        return path.read_text()
    except OSError as exc:
        raise ConfigError(f"invalid {label}; refusing to edit {path}: {exc}") from exc


def load_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"invalid {label} JSON; refusing to edit {path}: {exc}") from exc


def validate_hooks(config: Any, path: Path, label: str) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ConfigError(f"{label} root must be an object: {path}")
    hooks = config.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ConfigError(f"hooks must be an object: {path}")
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            raise ConfigError(f"hooks.{event} must be a list: {path}")
    return config


def validate_managed_block(content: str, path: Path, *, required: bool) -> None:
    starts = content.count(BLOCK_START)
    ends = content.count(BLOCK_END)
    matches = len(BLOCK_PATTERN.findall(content))
    valid = starts == ends == matches and starts <= 1
    if required:
        valid = valid and starts == 1
    if not valid:
        raise ConfigError(f"invalid managed AGENTS block: {path}")


def substitute(value: Any, replacement: str) -> Any:
    if isinstance(value, str):
        return value.replace("__BUILDER_LOOP_HOOK__", replacement)
    if isinstance(value, list):
        return [substitute(item, replacement) for item in value]
    if isinstance(value, dict):
        return {key: substitute(item, replacement) for key, item in value.items()}
    return value


def managed_commands(template: dict[str, Any]) -> set[str]:
    legacy = template.get("legacyManagedCommands", [])
    if not isinstance(legacy, list) or not all(isinstance(command, str) for command in legacy):
        raise ConfigError("builder-loop legacyManagedCommands must be a string list")
    commands: set[str] = set(legacy)
    for entries in template.get("hooks", {}).values():
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            handlers = entry.get("hooks", [])
            if not isinstance(handlers, list):
                continue
            for handler in handlers:
                if isinstance(handler, dict) and handler.get("type") == "command":
                    command = handler.get("command")
                    if isinstance(command, str):
                        commands.add(command)
    return commands


def strip_managed_handlers(
    hooks: dict[str, Any], commands: set[str]
) -> dict[str, Any]:
    cleaned = copy.deepcopy(hooks)
    for event in list(cleaned):
        kept_entries: list[Any] = []
        for entry in cleaned[event]:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                kept_entries.append(entry)
                continue
            handlers = entry["hooks"]
            kept_handlers = [
                handler
                for handler in handlers
                if not (
                    isinstance(handler, dict)
                    and handler.get("type") == "command"
                    and handler.get("command") in commands
                )
            ]
            if kept_handlers:
                updated = copy.deepcopy(entry)
                updated["hooks"] = kept_handlers
                kept_entries.append(updated)
        if kept_entries:
            cleaned[event] = kept_entries
        else:
            del cleaned[event]
    return cleaned


def rendered_template(template_path: Path, installed_hook: Path) -> dict[str, Any]:
    raw = validate_hooks(load_json(template_path, "builder-loop hooks template"), template_path, "hooks template")
    rendered = substitute(raw, shlex.quote(str(installed_hook)))
    return validate_hooks(rendered, template_path, "hooks template")


def install_hooks(existing: dict[str, Any], template: dict[str, Any]) -> bytes:
    config = copy.deepcopy(existing)
    hooks = strip_managed_handlers(config.get("hooks", {}), managed_commands(template))
    for event, entries in template.get("hooks", {}).items():
        hooks.setdefault(event, []).extend(copy.deepcopy(entries))
    config["hooks"] = hooks
    return (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode()


def uninstall_hooks(existing: dict[str, Any], template: dict[str, Any]) -> bytes:
    config = copy.deepcopy(existing)
    config["hooks"] = strip_managed_handlers(
        config.get("hooks", {}), managed_commands(template)
    )
    return (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode()


def install_agents(existing: str, block: str) -> bytes:
    if BLOCK_PATTERN.search(existing):
        content = BLOCK_PATTERN.sub(block, existing, count=1)
    elif existing:
        content = f"{existing}\n{BLOCK_SEPARATOR}\n{block}\n"
    else:
        content = f"{BLOCK_SEPARATOR}\n{block}\n"
    return content.encode()


def uninstall_agents(existing: str) -> bytes:
    block_match = BLOCK_PATTERN.search(existing)
    if block_match is None:
        return existing.encode()

    marker = f"{BLOCK_SEPARATOR}\n"
    marker_start = block_match.start() - len(marker)
    if marker_start >= 0 and existing[marker_start : block_match.start()] == marker:
        prefix_end = marker_start
        if prefix_end > 0 and existing[prefix_end - 1] == "\n":
            prefix_end -= 1

        suffix_start = block_match.end()
        if suffix_start < len(existing) and existing[suffix_start] == "\n":
            suffix_start += 1

        prefix = existing[:prefix_end]
        suffix = existing[suffix_start:]
        if prefix and suffix and not prefix.endswith("\n") and not suffix.startswith("\n"):
            return f"{prefix}\n{suffix}".encode()
        return f"{prefix}{suffix}".encode()

    return BLOCK_PATTERN.sub("", existing, count=1).encode()


def snapshot(path: Path) -> Snapshot:
    if not path.exists():
        return Snapshot(path, False, None, None)
    if path.is_symlink():
        raise ConfigError(f"refusing to replace symlink transaction target: {path}")
    try:
        data = path.read_bytes()
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise ConfigError(f"cannot snapshot transaction target {path}: {exc}") from exc
    return Snapshot(path, True, data, mode)


def atomic_write(path: Path, content: bytes, mode: int | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        if mode is not None:
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def restore_snapshot(item: Snapshot) -> None:
    if item.existed:
        assert item.content is not None
        atomic_write(item.path, item.content, item.mode)
    elif item.path.exists():
        item.path.unlink()


def apply_transaction(operations: list[Operation]) -> None:
    paths = [operation.path.absolute() for operation in operations]
    if len(paths) != len(set(paths)):
        raise ConfigError("configuration transaction targets overlap")
    snapshots = {operation.path: snapshot(operation.path) for operation in operations}
    applied: list[Operation] = []
    try:
        for operation in operations:
            if operation.action == "write":
                assert operation.content is not None
                atomic_write(operation.path, operation.content, operation.mode)
            else:
                if operation.path.exists():
                    operation.path.unlink()
            applied.append(operation)
    except OSError as exc:
        rollback_errors: list[str] = []
        for operation in reversed(applied):
            try:
                restore_snapshot(snapshots[operation.path])
            except OSError as rollback_exc:
                rollback_errors.append(f"{operation.path}: {rollback_exc}")
        suffix = f"; rollback failed for {rollback_errors}" if rollback_errors else ""
        raise ConfigError(f"configuration transaction failed: {exc}{suffix}") from exc


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("action", choices=("install", "uninstall"))
    value.add_argument("--hooks-file", required=True, type=Path)
    value.add_argument("--hooks-template", required=True, type=Path)
    value.add_argument("--installed-hook", required=True, type=Path)
    value.add_argument("--agents-file", required=True, type=Path)
    value.add_argument("--agents-block", required=True, type=Path)
    value.add_argument("--skip-hooks", action="store_true")
    value.add_argument("--skip-agents", action="store_true")
    return value


def main() -> int:
    args = parser().parse_args()
    template = rendered_template(args.hooks_template, args.installed_hook)
    block = read_text(args.agents_block, "managed AGENTS block").strip()
    validate_managed_block(block, args.agents_block, required=True)

    operations: list[Operation] = []
    if not args.skip_hooks:
        hooks_target = resolve_target(args.hooks_file, "Codex hooks")
        existing_hooks = (
            validate_hooks(load_json(args.hooks_file, "Codex hooks"), args.hooks_file, "Codex hooks")
            if args.hooks_file.exists()
            else {}
        )
        hooks_content = (
            install_hooks(existing_hooks, template)
            if args.action == "install"
            else uninstall_hooks(existing_hooks, template)
        )
        operations.append(
            Operation(
                hooks_target.destination,
                "write",
                hooks_content,
                stat.S_IMODE(hooks_target.destination.stat().st_mode)
                if hooks_target.destination.exists()
                else None,
            )
        )

    if not args.skip_agents:
        agents_target = resolve_target(args.agents_file, "global AGENTS")
        existing_agents = read_text(args.agents_file, "global AGENTS") if args.agents_file.exists() else ""
        validate_managed_block(existing_agents, args.agents_file, required=False)
        agents_content = (
            install_agents(existing_agents, block)
            if args.action == "install"
            else uninstall_agents(existing_agents)
        )
        if args.action == "uninstall" and not agents_content and not agents_target.symlink:
            operations.append(Operation(agents_target.destination, "delete"))
        else:
            operations.append(
                Operation(
                    agents_target.destination,
                    "write",
                    agents_content,
                    stat.S_IMODE(agents_target.destination.stat().st_mode)
                    if agents_target.destination.exists()
                    else None,
                )
            )

    apply_transaction(operations)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigError as exc:
        print(str(exc), file=os.sys.stderr)
        raise SystemExit(1)
