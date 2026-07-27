from __future__ import annotations

import sys
from pathlib import Path


TESTS_ROOT = str(Path(__file__).resolve().parent)
if TESTS_ROOT not in sys.path:
    sys.path.insert(0, TESTS_ROOT)
