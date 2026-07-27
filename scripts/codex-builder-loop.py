#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path


sys.dont_write_bytecode = True
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "runtime"))

from codex_builder_loop import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
