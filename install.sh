#!/usr/bin/env bash
# Install the Codex-native builder-loop surfaces without touching Claude Code config.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"
SKILLS_HOME="${HOME}/.agents/skills"
LOCAL_BIN="${HOME}/.local/bin"
HOOKS_FILE="${CODEX_HOME}/hooks.json"
GLOBAL_AGENTS="${CODEX_HOME}/AGENTS.md"
GLOBAL_AGENTS_OVERRIDE="${CODEX_HOME}/AGENTS.override.md"
DOC_POLICY="${CODEX_HOME}/builder-loop/doc-policy.md"

LINK_SOURCES=(
  "$REPO_DIR/skills/builder-loop-planner"
  "$REPO_DIR/skills/builder"
  "$REPO_DIR/agents/tester.toml"
  "$REPO_DIR/agents/reviewer.toml"
  "$REPO_DIR/hooks/builder-loop.py"
  "$REPO_DIR/scripts/codex-builder-loop.py"
  "$REPO_DIR/policies/doc-policy.md"
  "$REPO_DIR/skills/file-github-issue"
  "$REPO_DIR/skills/full-driver-v4-experiment"
  "$REPO_DIR/agents/builder.toml"
)
LINK_TARGETS=(
  "$SKILLS_HOME/builder-loop-planner"
  "$SKILLS_HOME/builder"
  "$CODEX_HOME/agents/tester.toml"
  "$CODEX_HOME/agents/reviewer.toml"
  "$CODEX_HOME/hooks/builder-loop.py"
  "$LOCAL_BIN/codex-builder-loop"
  "$DOC_POLICY"
  "$SKILLS_HOME/file-github-issue"
  "$SKILLS_HOME/full-driver-v4-experiment"
  "$CODEX_HOME/agents/builder.toml"
)
LINK_PREEXISTED=()

preflight_link() {
  local source="$1"
  local target="$2"
  if [ ! -e "$source" ]; then
    echo "missing install source: $source" >&2
    return 1
  fi
  if [ -L "$target" ]; then
    local actual
    local expected
    if ! actual="$(readlink -f -- "$target")" || \
      ! expected="$(readlink -f -- "$source")" || \
      [ "$actual" != "$expected" ]; then
      echo "refusing to replace foreign symlink: $target" >&2
      return 1
    fi
    LINK_PREEXISTED+=(1)
  elif [ -e "$target" ]; then
    echo "refusing to replace non-symlink: $target" >&2
    return 1
  else
    LINK_PREEXISTED+=(0)
  fi
}

install_link() {
  local source="$1"
  local target="$2"
  ln -sfn "$source" "$target"
  echo "linked $target"
}

for index in "${!LINK_TARGETS[@]}"; do
  preflight_link "${LINK_SOURCES[$index]}" "${LINK_TARGETS[$index]}"
done

python3 - "$HOOKS_FILE" "$REPO_DIR/hooks/hooks.json" \
  "$GLOBAL_AGENTS" "$REPO_DIR/agents/AGENTS.md.block" \
  "$GLOBAL_AGENTS_OVERRIDE" <<'PY'
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


hooks_target = Path(sys.argv[1])
hooks_template = Path(sys.argv[2])
agents_target = Path(sys.argv[3])
agents_block = Path(sys.argv[4])
agents_override = Path(sys.argv[5])
start = "<!-- BEGIN cc-builder-loop-codex -->"
end = "<!-- END cc-builder-loop-codex -->"
pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)


def reject_dangling(path: Path, label: str) -> None:
    if not path.is_symlink():
        return
    try:
        path.resolve(strict=True)
    except OSError as exc:
        raise SystemExit(f"dangling {label} symlink; refusing to edit {path}: {exc}")


def load_json(path: Path, label: str) -> object:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid {label} JSON; refusing to edit {path}: {exc}")


def validate_hooks(config: object, path: Path, label: str) -> None:
    if not isinstance(config, dict):
        raise SystemExit(f"{label} root must be an object: {path}")
    hooks = config.get("hooks", {})
    if not isinstance(hooks, dict):
        raise SystemExit(f"hooks must be an object: {path}")
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            raise SystemExit(f"hooks.{event} must be a list: {path}")


def read_text(path: Path, label: str) -> str:
    try:
        return path.read_text()
    except OSError as exc:
        raise SystemExit(f"invalid {label}; refusing to edit {path}: {exc}")


def validate_managed_block(content: str, path: Path, *, required: bool) -> None:
    starts = content.count(start)
    ends = content.count(end)
    matches = len(pattern.findall(content))
    valid = starts == ends == matches and starts <= 1
    if required:
        valid = valid and starts == 1
    if not valid:
        raise SystemExit(f"invalid managed AGENTS block: {path}")


reject_dangling(hooks_target, "Codex hooks")
if hooks_target.exists():
    validate_hooks(load_json(hooks_target, "Codex hooks"), hooks_target, "Codex hooks")
validate_hooks(load_json(hooks_template, "builder-loop hooks template"), hooks_template, "hooks template")

reject_dangling(agents_target, "global AGENTS")
if agents_target.exists():
    validate_managed_block(read_text(agents_target, "global AGENTS"), agents_target, required=False)
validate_managed_block(read_text(agents_block, "managed AGENTS block"), agents_block, required=True)
reject_dangling(agents_override, "global AGENTS override")
if agents_override.exists() and read_text(
    agents_override, "global AGENTS override"
).strip():
    raise SystemExit(
        "non-empty global AGENTS.override.md shadows AGENTS.md; "
        f"merge or remove {agents_override} before installing"
    )
PY

python3 "$REPO_DIR/scripts/codex-builder-loop.py" --help >/dev/null
python3 "$REPO_DIR/scripts/codex-builder-loop-config.py" --help >/dev/null

mkdir -p "$CODEX_HOME" "$SKILLS_HOME" "$CODEX_HOME/agents" \
  "$CODEX_HOME/hooks" "$CODEX_HOME/builder-loop" "$LOCAL_BIN"

rollback_new_links() {
  local status="$?"
  local actual
  local expected
  trap - ERR
  set +e
  for index in "${!LINK_TARGETS[@]}"; do
    if [ "${LINK_PREEXISTED[$index]}" -eq 0 ] && [ -L "${LINK_TARGETS[$index]}" ]; then
      actual="$(readlink -f -- "${LINK_TARGETS[$index]}")"
      expected="$(readlink -f -- "${LINK_SOURCES[$index]}")"
      if [ "$actual" = "$expected" ]; then
        rm -- "${LINK_TARGETS[$index]}"
      fi
    fi
  done
  exit "$status"
}
trap rollback_new_links ERR

for index in "${!LINK_TARGETS[@]}"; do
  install_link "${LINK_SOURCES[$index]}" "${LINK_TARGETS[$index]}"
done

python3 "$REPO_DIR/scripts/codex-builder-loop-config.py" install \
  --hooks-file "$HOOKS_FILE" \
  --hooks-template "$REPO_DIR/hooks/hooks.json" \
  --installed-hook "$CODEX_HOME/hooks/builder-loop.py" \
  --agents-file "$GLOBAL_AGENTS" \
  --agents-block "$REPO_DIR/agents/AGENTS.md.block"

INSTALLED_IDENTITY="$("$LOCAL_BIN/codex-builder-loop" version --json)"
EXPECTED_COMMIT="$(git -C "$REPO_DIR" rev-parse --verify HEAD 2>/dev/null || true)"
python3 - "$INSTALLED_IDENTITY" "$EXPECTED_COMMIT" <<'PY'
import json
import sys

value = json.loads(sys.argv[1])
identity = value.get("runtime_identity")
version = value.get("builder_loop_version")
expected_commit = sys.argv[2] or None
if (
    not isinstance(version, str)
    or value.get("version") != version
    or not isinstance(identity, dict)
    or identity.get("builder_loop_version") != version
    or identity.get("adapter_commit") != expected_commit
):
    raise SystemExit("installed Builder-loop version identity mismatch")
PY

trap - ERR

case ":${PATH}:" in
  *":${LOCAL_BIN}:"*) ;;
  *) echo "warning: add $LOCAL_BIN to PATH before using \$builder" >&2 ;;
esac

echo "Installed Builder-loop identity: $INSTALLED_IDENTITY"
echo "Codex builder-loop installed. Start a new Codex session and review /hooks trust."
