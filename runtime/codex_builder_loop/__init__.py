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
    from .core import main as runtime_main

    return runtime_main(effective, *args, **kwargs)


__all__ = ["main"]
