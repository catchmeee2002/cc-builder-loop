"""Compatibility boundary for frozen Driver runs and native operation receipts."""
from __future__ import annotations

from . import operations
from .store import read_ledger, resolve_repo


def current(repo_value, run_id, *, action_id=None, _ledger=None):
    repo = resolve_repo(repo_value)
    ledger = read_ledger(repo, run_id) if _ledger is None else _ledger
    if operations.is_native(ledger):
        return operations.find(repo, run_id, action_id=action_id, ledger=ledger)
    from .driver import next_action
    return next_action(repo, run_id, _ledger=ledger)
