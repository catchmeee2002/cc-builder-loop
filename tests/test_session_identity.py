"""Host identity and side-effect boundaries, independent of the runner's session."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from harness import CLI, ROOT, run_process
from runtime.codex_builder_loop.native_driver import cli
from runtime.codex_builder_loop.session_identity import SessionIdentityError, current_session_id


class SessionIdentityTest(unittest.TestCase):
    def test_source_is_verbatim_and_explicit_argument_cannot_replace_it(self):
        self.assertEqual(current_session_id(environ={"CODEX_THREAD_ID": "host-thread"}), "host-thread")
        self.assertEqual(current_session_id("host-thread", environ={"CODEX_THREAD_ID": "host-thread"}), "host-thread")
        for env, expected, code in [
            ({}, None, "MISSING"), ({}, "claimed-thread", "MISSING"),
            ({"CODEX_THREAD_ID": ""}, "claimed-thread", "MISSING"),
            ({"CODEX_THREAD_ID": " host-thread"}, None, "INVALID"),
            ({"CODEX_THREAD_ID": "host-thread\n"}, None, "INVALID"),
            ({"CODEX_THREAD_ID": "../thread"}, None, "INVALID"),
            ({"CODEX_THREAD_ID": "x" * 129}, None, "INVALID"),
            ({"CODEX_THREAD_ID": "child-thread"}, "parent-thread", "MISMATCH"),
            ({"CODEX_THREAD_ID": "host-thread"}, "", "MISMATCH"),
        ]:
            with self.subTest(env=env, expected=expected), self.assertRaises(SessionIdentityError) as raised:
                current_session_id(expected, environ=env)
            self.assertEqual(raised.exception.code, "NATIVE_SESSION_IDENTITY_" + code)

    def invoke(self, args, env, core=None):
        output = StringIO()
        with patch.dict(os.environ, env, clear=True), patch.object(cli, "CorePort", return_value=core), redirect_stdout(output):
            rc = cli.main(args)
        return rc, json.loads(output.getvalue().splitlines()[-1])

    def test_query_runs_outside_repo_without_core_or_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_process([sys.executable, CLI, "native-driver", "session-identity"],
                                 cwd=tmp, env={"CODEX_THREAD_ID": "query-thread"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {
                "status": "READY", "source": "CODEX_THREAD_ID", "session_id": "query-thread"})
            self.assertEqual(list(Path(tmp).iterdir()), [])
        rc, payload = self.invoke(["session-identity", "--session-id", "other"], {"CODEX_THREAD_ID": "query-thread"})
        self.assertEqual(rc, 1)
        self.assertEqual(payload["code"], "NATIVE_SESSION_IDENTITY_MISMATCH")

    def test_start_rejects_before_contract_read_core_or_transport(self):
        for env, assertion, code in [({}, [], "MISSING"), ({}, ["--session-id", "owner"], "MISSING"),
                                      ({"CODEX_THREAD_ID": "child"}, ["--session-id", "owner"], "MISMATCH")]:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                core = Mock()
                with patch.object(cli, "probe_app_server") as probe, patch.object(cli, "AppServerTransport") as transport:
                    rc, payload = self.invoke(["start", "--repo", tmp, "--run", "identity-probe",
                                               "--contract", str(Path(tmp) / "absent.json"), *assertion], env, core)
                self.assertEqual(rc, 1)
                self.assertEqual(payload["code"], "NATIVE_SESSION_IDENTITY_" + code)
                self.assertEqual(core.mock_calls, [])
                probe.assert_not_called()
                transport.assert_not_called()
                self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_start_without_argument_binds_host_identity(self):
        with patch.object(cli, "load_json_source", return_value={}), \
             patch.object(cli, "builder_runtime_mode", return_value="root_session"), \
             patch.object(cli, "_root_builder_result", return_value=({"status": "BUILDER_HANDOFF"}, 1)) as start:
            rc, payload = self.invoke(["start", "--repo", str(ROOT), "--run", "identity-probe", "--contract", "unused"],
                                      {"CODEX_THREAD_ID": "host-thread"}, Mock())
        self.assertEqual((rc, payload["status"]), (1, "BUILDER_HANDOFF"))
        self.assertEqual(start.call_args.kwargs["root_session_id"], "host-thread")

    def test_wrong_resume_caller_only_reads_context(self):
        context = {"driver_runtime": {"kind": "native", "transport": "root_session",
                                      "root_session_identity": {"session_id": "owner"}}}
        for env, code in [({}, "MISSING"), ({"CODEX_THREAD_ID": "child"}, "MISMATCH")]:
            core = Mock()
            core.call.return_value = context
            with patch.object(cli, "probe_app_server") as probe, patch.object(cli, "_persist_fatal") as fatal:
                rc, payload = self.invoke(["resume", "--repo", str(ROOT), "--run", "original"], env, core)
            self.assertEqual(rc, 1)
            self.assertEqual(payload["code"], "NATIVE_SESSION_IDENTITY_" + code)
            self.assertEqual(len(core.mock_calls), 1)
            self.assertEqual(core.call.call_args.args[0], "driver-context")
            self.assertEqual(context["driver_runtime"]["root_session_identity"]["session_id"], "owner")
            probe.assert_not_called()
            fatal.assert_not_called()

    def test_status_does_not_require_host_identity(self):
        core = Mock()
        core.call.return_value = {"status": "READY"}
        rc, payload = self.invoke(["status", "--repo", str(ROOT), "--run", "original"], {}, core)
        self.assertEqual(rc, 0)
        self.assertEqual(payload["status"], "READY")
