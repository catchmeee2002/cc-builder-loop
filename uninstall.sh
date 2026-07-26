#!/usr/bin/env bash
# Remove only the Codex-native builder-loop surfaces owned by this checkout.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"
SKILLS_HOME="${HOME}/.agents/skills"
LOCAL_BIN="${HOME}/.local/bin"
HOOKS_FILE="${CODEX_HOME}/hooks.json"
GLOBAL_AGENTS="${CODEX_HOME}/AGENTS.md"
INSTALLED_HOOK="${CODEX_HOME}/hooks/builder-loop.py"
DOC_POLICY="${CODEX_HOME}/builder-loop/doc-policy.md"

LINK_TARGETS=(
  "$SKILLS_HOME/builder-loop-planner"
  "$SKILLS_HOME/builder"
  "$CODEX_HOME/agents/tester.toml"
  "$CODEX_HOME/agents/reviewer.toml"
  "$INSTALLED_HOOK"
  "$LOCAL_BIN/codex-builder-loop"
  "$DOC_POLICY"
  "$SKILLS_HOME/file-github-issue"
)
LINK_EXPECTED=(
  "$REPO_DIR/skills/builder-loop-planner"
  "$REPO_DIR/skills/builder"
  "$REPO_DIR/agents/tester.toml"
  "$REPO_DIR/agents/reviewer.toml"
  "$REPO_DIR/hooks/builder-loop.py"
  "$REPO_DIR/scripts/codex-builder-loop.py"
  "$REPO_DIR/policies/doc-policy.md"
  "$REPO_DIR/skills/file-github-issue"
)
LINK_OWNED=()
ANY_LINK_OWNED=0
REMOVED_LINKS=()

preflight_link() {
  local target="$1"
  local expected="$2"
  if [ ! -L "$target" ]; then
    LINK_OWNED+=(0)
    return
  fi
  local actual
  local wanted
  if ! actual="$(readlink -f -- "$target")" || \
    ! wanted="$(readlink -f -- "$expected")" || \
    [ "$actual" != "$wanted" ]; then
    echo "leaving foreign symlink: $target" >&2
    LINK_OWNED+=(0)
    return
  fi
  LINK_OWNED+=(1)
  ANY_LINK_OWNED=1
}

for index in "${!LINK_TARGETS[@]}"; do
  preflight_link "${LINK_TARGETS[$index]}" "${LINK_EXPECTED[$index]}"
done

HOOK_LINK_OWNED="${LINK_OWNED[4]}"
INSTALLATION_OWNED="${LINK_OWNED[1]}"

python3 - "$HOOKS_FILE" "$GLOBAL_AGENTS" "$ANY_LINK_OWNED" <<'PY'
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


hooks_target = Path(sys.argv[1])
agents_target = Path(sys.argv[2])
has_owned_links = sys.argv[3] == "1"
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


def read_text(path: Path, label: str) -> str:
    try:
        return path.read_text()
    except OSError as exc:
        raise SystemExit(f"invalid {label}; refusing to edit {path}: {exc}")


if has_owned_links:
    reject_dangling(hooks_target, "Codex hooks")
    if hooks_target.exists():
        try:
            config = json.loads(hooks_target.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(
                f"invalid Codex hooks JSON; refusing to edit {hooks_target}: {exc}"
            )
        if not isinstance(config, dict):
            raise SystemExit(f"Codex hooks root must be an object: {hooks_target}")
        hooks = config.get("hooks", {})
        if not isinstance(hooks, dict):
            raise SystemExit(f"hooks must be an object: {hooks_target}")
        for event, entries in hooks.items():
            if not isinstance(entries, list):
                raise SystemExit(f"hooks.{event} must be a list: {hooks_target}")

    reject_dangling(agents_target, "global AGENTS")
    if agents_target.exists():
        content = read_text(agents_target, "global AGENTS")
        starts = content.count(start)
        ends = content.count(end)
        matches = len(pattern.findall(content))
        if starts != ends or starts != matches or starts > 1:
            raise SystemExit(f"invalid managed AGENTS block: {agents_target}")
PY

restore_removed_links() {
  local status="$?"
  local index
  trap - ERR
  set +e
  for index in "${REMOVED_LINKS[@]}"; do
    if [ ! -e "${LINK_TARGETS[$index]}" ] && [ ! -L "${LINK_TARGETS[$index]}" ]; then
      ln -s "${LINK_EXPECTED[$index]}" "${LINK_TARGETS[$index]}"
    fi
  done
  exit "$status"
}
trap restore_removed_links ERR

for index in "${!LINK_TARGETS[@]}"; do
  if [ "${LINK_OWNED[$index]}" -eq 1 ]; then
    rm -- "${LINK_TARGETS[$index]}"
    REMOVED_LINKS+=("$index")
    echo "removed ${LINK_TARGETS[$index]}"
  fi
done

CONFIG_ARGS=()
CONFIG_NEEDED=0
if ! { [ "$HOOK_LINK_OWNED" -eq 1 ] && \
  { [ -e "$HOOKS_FILE" ] || [ -L "$HOOKS_FILE" ]; }; }; then
  CONFIG_ARGS+=(--skip-hooks)
else
  CONFIG_NEEDED=1
fi
if ! { [ "$INSTALLATION_OWNED" -eq 1 ] && \
  { [ -e "$GLOBAL_AGENTS" ] || [ -L "$GLOBAL_AGENTS" ]; }; }; then
  CONFIG_ARGS+=(--skip-agents)
else
  CONFIG_NEEDED=1
fi

if [ "$CONFIG_NEEDED" -eq 1 ]; then
  python3 "$REPO_DIR/scripts/codex-builder-loop-config.py" uninstall \
    --hooks-file "$HOOKS_FILE" \
    --hooks-template "$REPO_DIR/hooks/hooks.json" \
    --installed-hook "$INSTALLED_HOOK" \
    --agents-file "$GLOBAL_AGENTS" \
    --agents-block "$REPO_DIR/agents/AGENTS.md.block" \
    "${CONFIG_ARGS[@]}"
fi

trap - ERR
echo "Codex builder-loop uninstalled. Existing run ledgers were preserved."
