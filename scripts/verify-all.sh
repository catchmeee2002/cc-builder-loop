#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 -c 'import jsonschema' >/dev/null 2>&1 || {
  echo "missing development dependency: python3 -m pip install -r requirements-dev.txt" >&2
  exit 2
}

PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py' -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s experiments/issue-triage/tests -p 'test_*.py' -v
