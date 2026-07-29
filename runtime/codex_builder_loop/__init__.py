"""Deterministic runtime for the Codex-native builder loop."""


def main(*args, **kwargs):
    from .core import main as runtime_main

    return runtime_main(*args, **kwargs)


__all__ = ["main"]
