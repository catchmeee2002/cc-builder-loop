#!/usr/bin/env python3
"""Run the same unittest modules serially or in isolated worker processes."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
# These modules inspect installation/process-wide integration boundaries.
SERIAL_MODULES = {"test_install_contract.py", "test_process_runtime.py", "test_hook_runtime_integration.py"}


def test_ids(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from test_ids(item)
        else:
            yield item.id()


def collect(directory, pattern):
    sys.path.insert(0, str(directory))
    sys.path.insert(0, str(ROOT))
    loader = unittest.TestLoader()
    suite = loader.discover(str(directory), pattern=pattern)
    if loader.errors:
        raise RuntimeError("\n".join(loader.errors))
    return suite


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--start-dir", type=Path, default=ROOT / "tests")
    parser.add_argument("--list-json", action="store_true")
    parser.add_argument("--worker")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    directory = args.start_dir.resolve()
    if args.worker:
        suite = collect(directory, args.worker)
        ids = list(test_ids(suite))
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        if args.report:
            args.report.write_text(json.dumps({"ids": ids, "ran": result.testsRun, "successful": result.wasSuccessful()}))
        return 0 if ids and result.testsRun == len(ids) and result.wasSuccessful() else 1
    suite = collect(directory, "test_*.py")
    expected = list(test_ids(suite))
    if not expected or len(set(expected)) != len(expected):
        raise RuntimeError("Fixture collection is empty or contains duplicate test identities")
    if args.list_json:
        print(json.dumps(sorted(expected)))
        return 0
    modules = sorted(p.name for p in directory.glob("test_*.py"))
    with tempfile.TemporaryDirectory(prefix="codex-fixtures-") as raw:
        root = Path(raw)
        def run(module):
            report = root / (module + ".json")
            command = [sys.executable, str(Path(__file__).resolve()), "--start-dir", str(directory), "--worker", module, "--report", str(report)]
            completed = subprocess.run(command, cwd=ROOT, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            value = json.loads(report.read_text()) if report.is_file() else {}
            return module, completed.returncode, completed.stdout, value
        parallel = [module for module in modules if module not in SERIAL_MODULES]
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            results = []
            futures = {pool.submit(run, module): module for module in parallel}
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                results.append(result)
                print(f"Completed {result[0]}: {result[3].get('ran', 0)} tests", flush=True)
        for module in modules:
            if module in SERIAL_MODULES:
                result = run(module)
                results.append(result)
                print(f"Completed {module}: {result[3].get('ran', 0)} tests", flush=True)
        observed = []
        failed = False
        for module, code, output, report in sorted(results):
            ids = report.get("ids", [])
            observed.extend(ids)
            valid = code == 0 and report.get("successful") is True and report.get("ran") == len(ids)
            failed |= not valid
            print(f"{'PASS' if valid else 'FAIL'} {module}: {report.get('ran', 0)} tests", flush=True)
            if not valid:
                print(output, end="" if output.endswith("\n") else "\n")
        if sorted(observed) != sorted(expected):
            print("FAIL: worker test identities differ from full discovery", file=sys.stderr)
            failed = True
        print(f"Fixtures: {len(observed)}/{len(expected)}; jobs={args.jobs}")
        return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
