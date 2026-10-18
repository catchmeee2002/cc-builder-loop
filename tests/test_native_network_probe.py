from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from runtime.codex_builder_loop.native_driver import app_server, cli


FAKE_SERVER = r'''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
mode = os.environ.get('PROBE_TEST_MODE', 'ready')
if sys.argv[1:] == ['--version']:
    print('codex-cli probe-fixture'); raise SystemExit(0)
if sys.argv[1:3] == ['app-server', 'generate-json-schema']:
    Path(sys.argv[-1], 'codex_app_server_protocol.schemas.json').write_text(json.dumps([
        'thread/start','thread/resume','thread/read','turn/start','turn/interrupt',
        'developerInstructions','outputSchema','clientUserMessageId']))
    raise SystemExit(0)
assert sys.argv[1:] == ['app-server', '--stdio']
turn = None
text = '{"ready":true}'
for line in sys.stdin:
    m = json.loads(line); method = m.get('method'); params = m.get('params', {})
    def reply(value):
        print(json.dumps({'id':m['id'],'result':value}), flush=True)
    if method == 'initialize':
        if mode == 'init-flood':
            deadline = time.monotonic() + 0.3
            while time.monotonic() < deadline:
                print(json.dumps({'method':'health','params':{}}),flush=True); time.sleep(0.01)
        reply({})
    elif method == 'thread/start':
        if mode in ('null-error','string-error'):
            print(json.dumps({'id':m['id'],'error':None if mode=='null-error' else 'bad'}),flush=True)
        else: reply({'thread':{'id':'probe-thread'}})
    elif method == 'turn/start':
        assert 'Do not call tools' in params['input'][0]['text']
        if mode == 'bad-json': text = 'ready'
        if mode == 'numeric-true': text = '{"ready":1}'
        if mode == 'extra-field': text = '{"ready":true,"extra":1}'
        if mode == 'spaced-key': text = '{"rea dy":true}'
        if mode == 'duplicate-key': text = '{"ready":false,"ready":true}'
        if mode == 'invalid-boolean': text = '{"ready":tr ue}'
        turn = {'id':'probe-turn','status':'completed','error':None,
                'items':[{'type':'agentMessage','text':text}]}
        reply({'turn':{'id':'probe-turn'}})
        if mode == 'timeout': time.sleep(10); continue
        if mode == 'streamed-tool':
            print(json.dumps({'method':'item/started','params':{'threadId':'probe-thread',
                'turnId':'probe-turn','item':{'type':'commandExecution','command':'ignored'}}}),flush=True)
        if mode in ('routing','auth'):
            error = ('workspace routing discovery timed out' if mode == 'routing' else 'authentication expired')
            turn.update(status='failed',items=[],error={'message':error,'codexErrorInfo':'other'})
        if mode == 'secret':
            turn.update(status='failed',items=[],error={'message':'http://proxy-user:proxy-pass@localhost:9 token=private-value', 'details':{'access_token':'structured-private-value'}})
            print('https://proxy-user:proxy-pass@localhost:9 Authorization: Bearer secret-value\nProxy-Authorization: Basic dXNlcjpwYXNz',file=sys.stderr,flush=True)
        if mode == 'null-params':
            print(json.dumps({'method':'turn/completed','params':None}),flush=True)
        elif mode == 'null-turn':
            print(json.dumps({'method':'turn/completed','params':{'threadId':'probe-thread','turn':None}}),flush=True)
        else:
            print(json.dumps({'method':'turn/completed','params':{'threadId':'probe-thread','turn':turn}}),flush=True)
    elif method == 'thread/read':
        if mode == 'null-history': reply({'thread':None}); continue
        value = json.loads(json.dumps(turn))
        if mode == 'wrong-turn': value['id']='another-turn'
        if mode == 'wrong-status': value['status']='failed'
        if mode == 'history-tool': value['items'].insert(0,{'type':'mcpToolCall'})
        if mode == 'wrong-text': value['items'][0]['text']='{}'
        reply({'thread':{'id':'wrong-thread' if mode == 'wrong-thread' else 'probe-thread','turns':[value]}})
'''


class NativeNetworkProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="native-network-test-")
        self.root = Path(self.temp.name)
        self.codex = self.root / "codex"
        self.codex.write_text(FAKE_SERVER)
        self.codex.chmod(0o755)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def probe(self, mode="ready", timeout=5):
        with patch.dict(os.environ, {"PROBE_TEST_MODE": mode}):
            result = app_server.probe_model_request(str(self.codex), timeout_seconds=timeout)
        self.assertTrue(result["cleanup"]["process_group_gone"], result)
        self.assertFalse((self.root / ".git").exists())
        return result

    def test_real_protocol_and_matching_model_readback_are_required(self):
        result = self.probe()
        self.assertEqual(result["status"], "READY", result)
        self.assertEqual(result["protocol_status"], "READY")
        self.assertEqual(result["model_request_status"], "READY")
        self.assertEqual(result["runtime_version"], "codex-cli probe-fixture")
        self.assertEqual(result["turn_id"], "probe-turn")

    def test_protocol_success_cannot_mask_routing_or_auth_failure(self):
        for mode, message in [('routing','workspace routing discovery timed out'),('auth','authentication expired')]:
            with self.subTest(mode=mode):
                result = self.probe(mode)
                self.assertEqual(result["status"], "NEEDS_USER")
                self.assertEqual(result["protocol_status"], "READY")
                self.assertEqual(result["model_request_status"], "FAILED")
                self.assertEqual(result["error"]["details"]["turn_error"]["message"], message)

    def test_invalid_response_never_passes(self):
        for mode in ('bad-json','numeric-true','extra-field','spaced-key','duplicate-key','invalid-boolean'):
            with self.subTest(mode=mode):
                self.assertEqual(self.probe(mode)["code"], "NATIVE_PROBE_RESPONSE_INVALID")

    def test_history_identity_status_and_text_must_match(self):
        for mode in ('wrong-thread','wrong-turn','wrong-status','wrong-text','null-history','null-params','null-turn'):
            with self.subTest(mode=mode):
                self.assertEqual(self.probe(mode)["code"], "NATIVE_PROBE_HISTORY_INVALID")

    def test_streamed_or_readback_tool_activity_never_passes(self):
        for mode in ('streamed-tool','history-tool'):
            with self.subTest(mode=mode):
                self.assertEqual(self.probe(mode)["code"], "NATIVE_PROBE_UNEXPECTED_ACTIVITY")

    def test_timeout_reaps_process_and_fails(self):
        result = self.probe('timeout', timeout=0.05)
        self.assertEqual(result["status"], "NEEDS_USER")
        self.assertIn(result["code"], {'NATIVE_APP_SERVER_TURN_TIMEOUT','NATIVE_APP_SERVER_TIMEOUT'})

    def test_proxy_credentials_and_errors_are_redacted(self):
        with patch.dict(os.environ, {'HTTP_PROXY':'http://proxy-user:proxy-pass@localhost:9'}):
            result = self.probe('secret')
        raw = json.dumps(result)
        for secret in ('proxy-user','proxy-pass','private-value','secret-value','structured-private-value','dXNlcjpwYXNz'):
            self.assertNotIn(secret, raw)
        self.assertIn('HTTP_PROXY',result['proxy_environment_keys'])

    def test_missing_binary_fails_without_claiming_protocol_or_model_success(self):
        result = app_server.probe_model_request(str(self.root / 'missing'))
        self.assertEqual(result['status'],'NEEDS_USER')
        self.assertEqual(result['protocol_status'],'FAILED')
        self.assertEqual(result['model_request_status'],'NOT_CHECKED')

    def test_unverified_cleanup_prevents_ready(self):
        close = app_server._ReadinessTransport.close
        def unverified(transport):
            result = close(transport)
            return {**result, 'process_group_gone':False} if result else result
        with patch.object(app_server._ReadinessTransport,'close',unverified):
            result = app_server.probe_model_request(str(self.codex))
        self.assertEqual(result['status'],'NEEDS_USER')
        self.assertEqual(result['code'],'NATIVE_PROBE_CLEANUP_UNVERIFIED')

    def test_unverified_protocol_cleanup_prevents_model_request(self):
        close = app_server.AppServerTransport.close
        def unverified(transport):
            result = close(transport)
            if type(transport) is app_server.AppServerTransport and result:
                transport._cleanup_observation = {**result, 'process_group_gone': False}
                return transport._cleanup_observation
            return result
        with patch.object(app_server.AppServerTransport, 'close', unverified):
            result = app_server.probe_model_request(str(self.codex))
        self.assertEqual(result['status'], 'NEEDS_USER')
        self.assertEqual(result['protocol_status'], 'FAILED')
        self.assertEqual(result['model_request_status'], 'NOT_CHECKED')

    def test_notifications_do_not_extend_initialization_deadline(self):
        clock = [0.0]
        notifications = []
        def read(timeout):
            # Two notifications arrive before the one-second request deadline.
            # A response at 1.2 seconds is too late even though traffic continues.
            if timeout < 0.4:
                raise app_server.AppServerError("read deadline", code="NATIVE_APP_SERVER_TIMEOUT")
            clock[0] += 0.4
            if clock[0] >= 1.2:
                return {"id": 1, "result": {}}
            notifications.append(clock[0])
            return {"method": "health", "params": {}}
        transport = app_server.AppServerTransport(codex_bin=str(self.codex))
        with patch.object(app_server, "INITIALIZE_TIMEOUT_SECONDS", 1.0), \
             patch.object(app_server.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(transport, "_send"), patch.object(transport, "_read", side_effect=read):
            with self.assertRaises(app_server.AppServerError) as caught:
                transport._request("initialize", {})
        self.assertEqual(caught.exception.code, "NATIVE_APP_SERVER_TIMEOUT")
        self.assertEqual(len(notifications), 2)

    def test_malformed_rpc_errors_return_structured_failure(self):
        for mode in ('null-error','string-error'):
            with self.subTest(mode=mode), patch.dict(os.environ, {'PROBE_TEST_MODE':mode}):
                result = app_server.probe_model_request(str(self.codex))
                self.assertEqual(result['status'], 'NEEDS_USER')
                self.assertEqual(result['protocol_status'], 'FAILED')

    def test_cleanup_exception_cannot_leave_success_status(self):
        close = app_server._ReadinessTransport.close
        def failing(transport):
            close(transport)
            raise RuntimeError('cleanup failure')
        with patch.object(app_server._ReadinessTransport, 'close', failing):
            result = app_server.probe_model_request(str(self.codex))
        self.assertEqual(result['status'], 'NEEDS_USER')
        self.assertEqual(result['code'], 'NATIVE_PROBE_CLEANUP_FAILED')

    def test_direct_connection_does_not_require_proxy_variables(self):
        env = {k:v for k,v in os.environ.items() if k.lower() not in
               {'http_proxy','https_proxy','all_proxy','no_proxy'}}
        with patch.dict(os.environ, env, clear=True):
            result = self.probe()
        self.assertEqual(result['status'], 'READY')
        self.assertEqual(result['proxy_environment_keys'], [])

    def test_cli_never_constructs_a_core_port_or_uses_a_run(self):
        for status, expected_rc in [('READY',0),('NEEDS_USER',1)]:
            with self.subTest(status=status), patch.object(cli,'CorePort',side_effect=AssertionError('no ledger')), \
                 patch.object(cli,'probe_model_request',return_value={'status':status}), redirect_stdout(StringIO()) as output:
                self.assertEqual(cli.main(['probe']), expected_rc)
                self.assertEqual(json.loads(output.getvalue())['status'],status)

    def test_timeout_argument_rejects_nonfinite_zero_negative_and_excessive_values(self):
        for value in ('0','-1','nan','inf','301'):
            with self.subTest(value=value), patch('sys.stderr',new=StringIO()):
                with self.assertRaises(SystemExit):
                    cli.parser().parse_args(['probe','--timeout-seconds',value])


if __name__ == '__main__':
    unittest.main()
