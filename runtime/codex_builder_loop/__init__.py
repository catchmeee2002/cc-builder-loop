"""Deterministic runtime for the Codex-native builder loop."""

from __future__ import annotations

import sys


def main(*args, **kwargs):
    argv = kwargs.pop("argv", None)
    if args:
        argv = args[0]
        args = args[1:]
    effective = list(sys.argv[1:] if argv is None else argv)
    if effective[:1] == ["assurance"]:
        from .assurance_v4.cli import main as assurance_main

        return assurance_main(effective[1:])
    if effective[:1] == ["native-driver"]:
        from .native_driver.cli import main as native_driver_main

        return native_driver_main(effective[1:])
    if effective[:1] == ["dev-worktree"]:
        from .dev_worktree import main as dev_worktree_main

        return dev_worktree_main(effective[1:])
    from .core import main as runtime_main

    return runtime_main(effective, *args, **kwargs)


__all__ = ["main"]
