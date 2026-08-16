from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from runtime.codex_builder_loop.native_driver.app_server import AppServerTransport


class NativeAppServerCacheContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="native-cache-contract-")
        self.root = Path(self.tempdir.name)
        self.role = self.root / "role-worktree"
        self.role.mkdir()
        self.source = self.role / "compile_target.py"
        self.source.write_text("VALUE = 1\n", encoding="utf-8")
        self.report = self.root / "report.json"
        self.conflicting = self.role / "caller-cache"
        self.codex = self.root / "codex"
        self.codex.write_text(
            """#!/usr/bin/env python3
import json, os, py_compile, sys
report = os.environ['CACHE_REPORT']
source = os.environ['CACHE_SOURCE']
if sys.argv[1:3] != ['app-server', '--stdio']:
    raise SystemExit(2)
compiled = py_compile.compile(source, doraise=True)
open(report, 'w').write(json.dumps({
    'prefix': os.environ.get('PYTHONPYCACHEPREFIX'),
    'compiled': compiled,
}))
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get('method') == 'initialize':
        print(json.dumps({'id': msg['id'], 'result': {'userAgent': 'fake'}}), flush=True)
""",
            encoding="utf-8",
        )
        self.codex.chmod(0o755)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def ignored_residue(self) -> list[str]:
        return sorted(
            str(path.relative_to(self.role))
            for path in self.role.rglob("*")
            if path.name == "__pycache__" or path.suffix == ".pyc"
        )

    def test_transport_overrides_conflicting_prefix_and_removes_private_cache(self) -> None:
        before = self.ignored_residue()
        previous = {
            "PYTHONPYCACHEPREFIX": os.environ.get("PYTHONPYCACHEPREFIX"),
            "CACHE_REPORT": os.environ.get("CACHE_REPORT"),
            "CACHE_SOURCE": os.environ.get("CACHE_SOURCE"),
        }
        os.environ.update(
            {
                "PYTHONPYCACHEPREFIX": str(self.conflicting),
                "CACHE_REPORT": str(self.report),
                "CACHE_SOURCE": str(self.source),
            }
        )
        transport: AppServerTransport | None = None
        try:
            transport = AppServerTransport(codex_bin=str(self.codex))
            transport.start()
            report = json.loads(self.report.read_text(encoding="utf-8"))
            prefix = Path(report["prefix"])
            compiled = Path(report["compiled"])
            self.assertNotEqual(prefix, self.conflicting)
            self.assertFalse(prefix == self.role or self.role in prefix.parents)
            self.assertTrue(prefix.is_dir())
            self.assertTrue(compiled.is_file())
            self.assertTrue(prefix == compiled or prefix in compiled.parents)
            self.assertEqual(self.ignored_residue(), before)
            transport.close()
            transport = None
            self.assertFalse(prefix.exists())
            self.assertEqual(self.ignored_residue(), before)
        finally:
            if transport is not None:
                transport.close()
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_transport_exposes_no_pattern_cleanup_api(self) -> None:
        self.assertFalse(hasattr(AppServerTransport, "clean_python_residue"))


if __name__ == "__main__":
    unittest.main()
