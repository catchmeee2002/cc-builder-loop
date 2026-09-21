#!/usr/bin/env bash
# builder-loop V8 卸载：移除本仓软链与 bl-hook.sh 的 hook 注册。不删 ledger / worktree / session 指针。
set -euo pipefail
REPO="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
CLAUDE_HOME="${CLAUDE_HOME:-$HOME/.claude}"

python3 - "$REPO" "$CLAUDE_HOME" <<'PY'
import json, os, sys, tempfile
from pathlib import Path

repo = Path(sys.argv[1]).resolve(); home = Path(sys.argv[2])
removed = []
for sub in ("agents", "skills", "commands", "bin", "scripts"):
    d = home / sub
    if not d.is_dir():
        continue
    for entry in d.iterdir():
        if entry.is_symlink():
            target = os.readlink(entry)
            if str(repo) in target or (not entry.exists() and "builder-loop" in target):
                entry.unlink(); removed.append(str(entry))
policy = home / "doc-policy.md"
if policy.is_symlink() and str(repo) in os.readlink(policy):
    policy.unlink(); removed.append(str(policy))

settings = home / "settings.json"
n = 0
if settings.is_file():
    data = json.loads(settings.read_text(encoding="utf-8"))
    hooks = data.get("hooks", {})
    for ev in list(hooks):
        kept = []
        for m in hooks[ev]:
            hs = [h for h in m.get("hooks", []) if "bl-hook.sh" not in h.get("command", "")]
            n += len(m.get("hooks", [])) - len(hs)
            if hs:
                m["hooks"] = hs; kept.append(m)
        if kept:
            hooks[ev] = kept
        else:
            del hooks[ev]
    fd, tmp = tempfile.mkstemp(prefix=".settings.", suffix=".tmp", dir=str(home))
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False); fh.write("\n")
    os.replace(tmp, settings)
print(json.dumps({"removed_symlinks": removed, "removed_hooks": n}, ensure_ascii=False, indent=2))
PY
