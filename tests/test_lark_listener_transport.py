"""Isolated wire peers and process harness for the frozen listener CLI.

The wire grammar is the public same-thread protocol clarification.  No listener
module is imported.  Only remote executables and wall time are substituted;
locks, process identities, storage, scheduling and listener entrypoints are real.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
OWNER = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
THREAD = "omt_fixturenew"
CHAT = "oc_fixture"
REMOTE = "ws://127.0.0.1:9"
DAY = 86400

# Executed only as an isolated external peer, never as another test runner.
PEER = r'''
import fcntl, json, os, pathlib, sys, time, uuid
root = pathlib.Path(__file__).resolve().parent
args = sys.argv[1:]

def option(name, default=None):
    if name not in args:
        return default
    i = args.index(name)
    if i + 1 >= len(args):
        raise ValueError("missing option value: " + name)
    return args[i + 1]

def identity(pid):
    p = pathlib.Path('/proc') / str(pid)
    try:
        fields = (p / 'stat').read_text().rsplit(')', 1)[1].split()
        return {'pid': pid, 'starttime': fields[19],
                'cmdline': (p / 'cmdline').read_bytes().decode().replace('\x00', ' ')}
    except (FileNotFoundError, ProcessLookupError):
        return None

kind = pathlib.Path(__file__).name
op = 'queue' if kind == 'codex-peer' else next(
    (a for a in args if a in ('whoami', '+messages-mget',
     '+threads-messages-list', '+messages-reply')), 'unsupported')
if kind == 'codex-peer' and args == ['queue', '--help']:
    world = json.loads((root / 'world.json').read_text())
    print('Usage: codex queue\n  --remote ADDR\n  --thread THREAD\n  --message TEXT' + ('\n  --resume' if world.get('queue_resume_supported', True) else ''))
    sys.exit(0)
rc, out, err = 0, '', ''
with (root / 'peer.lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    world = json.loads((root / 'world.json').read_text())
    trace = {'op': op, 'args': args, 'parent': identity(os.getppid()),
             'pid': os.getpid(), 'monotonic': time.monotonic(),
             'wall': time.time(), 'no_proxy': os.environ.get('LARK_CLI_NO_PROXY')}
    log = root / 'calls.jsonl'
    old = log.read_text().splitlines() if log.exists() else []
    trace['index'] = len(old)
    selected_as = option('--as', 'user')
    trace['identity'] = selected_as
    def success(data):
        return json.dumps({'ok': True, 'identity': selected_as, 'data': data})
    def failure(subtype, code):
        return json.dumps({'ok': False, 'identity': selected_as,
            'error': {'type': 'authorization' if subtype == 'missing_scope' else 'api',
                      'subtype': subtype, 'code': code,
                      'message': 'fixture-private-error-envelope-do-not-forward'}})
    if op == 'whoami':
        out = json.dumps({'available': True, 'profile': 'fixture-profile',
            'appId': 'cli_fixture_app', 'onBehalfOf': {'openId': 'ou_fixture_actor'}})
    elif op == '+messages-mget':
        ids = option('--message-ids', '').split(',')
        trace['message_ids'] = ids
        if '--no-reactions' not in args or not ids or any(i not in world['messages'] for i in ids):
            rc, err = 2, failure('not_found', 230001)
        else:
            out = success({'messages': [world['messages'][i] for i in ids]})
    elif op == '+threads-messages-list':
        thread = option('--thread')
        token = option('--page-token', '')
        trace.update(thread=thread, token=token)
        if (option('--page-size') != '50' or option('--order') != 'desc'
                or '--no-reactions' not in args or thread not in world['pages']):
            rc, err = 2, failure('bad_request', 99991400)
        elif world.get('list_error') == 'network':
            rc, err = 1, 'lark-cli: network request failed: Connection reset by peer'
        elif world.get('list_error'):
            rc, err = 1, failure('missing_scope', 99991679)
        elif world.get('list_non_json'):
            out = 'upstream returned an invalid response'
        elif token and token in world.get('invalid_tokens', []):
            rc, err = 1, failure('invalid_page_token', 99991400)
        else:
            page = int(token.split(':')[1]) if token else 0
            pages = world['pages'][thread]
            messages = pages[page] if page < len(pages) else []
            more = page + 1 < len(pages)
            out = success({'messages': messages, 'has_more': more,
                'page_token': 'opaque:' + str(page + 1) if more else '',
                'thread_id': thread, 'total': sum(len(p) for p in pages)})
    elif op == '+messages-reply':
        target, key, text = option('--message-id'), option('--idempotency-key'), option('--text')
        trace.update(target=target, key=key, text=text)
        if not key or len(key) > 50 or target not in world['messages'] or '--reply-in-thread' not in args or text is None:
            rc, err = 2, failure('bad_request', 99991400)
        else:
            prior = world.setdefault('sent', {}).get(key)
            if prior:
                mid = prior
            else:
                mid = 'om_sent_' + world.get('send_namespace', '') + str(len(world['sent']) + 1)
                parent = world['messages'][target]
                msg = {'message_id': mid, 'chat_id': parent['chat_id'],
                    'thread_id': parent['thread_id'], 'root_id': 'om_root',
                    'parent_id': target, 'message_type': 'text',
                    'content': json.dumps({'text': text}), 'deleted': False,
                    'create_time': str(int(time.time() * 1000)),
                    'update_time': str(int(time.time() * 1000)),
                    'sender': {'id': 'cli_fixture_app' if selected_as == 'bot' else 'ou_fixture_actor',
                        'id_type': 'app_id' if selected_as == 'bot' else 'open_id',
                        'sender_type': 'app' if selected_as == 'bot' else 'user'}}
                world['messages'][mid] = msg
                world['sent'][key] = mid
                world['pages'][parent['thread_id']][0].append(msg)
            trace['message_id'] = mid
            out = success({k: world['messages'][mid][k] for k in ('message_id', 'chat_id', 'create_time')})
    elif op == 'queue':
        owner, remote, message = option('--thread'), option('--remote'), option('--message')
        trace.update(owner=owner, remote=remote, message=message)
        if (args[:1] != ['queue'] or '--resume' not in args or remote != world['remote']
                or not remote.startswith(('ws://', 'wss://', 'unix://')) or not message):
            rc, err = 2, 'fixture: invalid queue invocation'
        else:
            mode = world.get('queue_mode', 'ok')
            qid = str(uuid.uuid4())
            trace['queue_id'] = qid
            if mode == 'empty':
                out = 'Command finished successfully.'
            elif mode == 'wrong-owner':
                out = 'Queued message ' + qid + ' for thread 33333333-3333-4333-8333-333333333333.'
            elif mode == 'malformed-id':
                out = 'Queued message not-a-uuid for thread ' + owner + '.'
            else:
                out = 'Queued message ' + qid + ' for thread ' + owner + '.'
            if mode == 'wake-failed':
                err = 'warning: resume failed: fixture endpoint unavailable'
    else:
        rc, err = 2, 'fixture: unsupported external operation'
    trace.update(phase='response_prepared', returncode=rc, stdout=out, stderr=err)
    with log.open('a') as f:
        f.write(json.dumps(trace) + '\n')
        f.flush()
        os.fsync(f.fileno())
    temp = root / 'world.next'
    temp.write_text(json.dumps(world))
    temp.replace(root / 'world.json')
    block = world.get('block')
# Network latches deliberately run outside both peer and listener state locks.
if block and block['op'] == op and (not block.get('token') or block['token'] == trace.get('token')):
    (root / (block['name'] + '.entered')).write_text(str(os.getpid()))
    deadline = time.monotonic() + 20
    while not (root / (block['name'] + '.release')).exists():
        if time.monotonic() >= deadline:
            sys.exit(4)
        time.sleep(.01)
if out:
    print(out, flush=True)
if err:
    print(err, file=sys.stderr, flush=True)
sys.exit(rc)
'''

CLOCK_SITE = r'''
import os
import time
_path = os.environ.get("LISTENER_FIXTURE_CLOCK")
if _path:
    def _fixture_wall_time():
        with open(_path, encoding="utf-8") as source:
            return float(source.read())
    time.time = _fixture_wall_time
'''


def atomic_json(path, value):
    path = Path(path)
    tmp = path.with_suffix('.next')
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)


def message(mid, text, *, thread=THREAD, stamp=1000, sender='ou_other', deleted=False,
            resources=None):
    content = resources if resources is not None else {'text': text}
    return {'message_id': mid, 'chat_id': CHAT, 'thread_id': thread,
            'root_id': 'om_root', 'parent_id': 'om_root',
            'message_type': 'file' if resources else 'text',
            'content': json.dumps(content, ensure_ascii=False),
            'create_time': str(stamp), 'update_time': str(stamp), 'deleted': deleted,
            'sender': {'id': sender, 'id_type': 'open_id', 'sender_type': 'user'}}


def nodes(value):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from nodes(item)


def named(value, key):
    return [node[key] for node in nodes(value) if isinstance(node, dict) and key in node]


def text_of(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def batch_id(text):
    # Accept JSON or labelled human-readable notifications; do not fix a layout.
    decoder = json.JSONDecoder()
    for i, char in enumerate(text):
        if char != '{':
            continue
        try:
            obj, _ = decoder.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        ids = named(obj, 'batch_id')
        if ids and isinstance(ids[0], str) and ids[0]:
            return ids[0]
    match = re.search(r'\bbatch_id[\s`"\'*]*[:=][\s`"\'*]*([A-Za-z0-9_.:-]+)', text)
    if not match:
        raise AssertionError('Notification has no labelled immutable batch_id: ' + text)
    return match.group(1)


def proc_identity(pid):
    try:
        p = Path('/proc') / str(pid)
        stat = (p / 'stat').read_text().rsplit(')', 1)[1].split()
        return {'pid': int(pid), 'starttime': stat[19], 'state': stat[0],
                'cmdline': (p / 'cmdline').read_bytes().decode().replace('\x00', ' ')}
    except (FileNotFoundError, ProcessLookupError):
        return None


class Sandbox:
    """Owned temporary files and processes; never calls a real remote binary."""
    def __init__(self, *, copy_skill=True):
        import fcntl
        self.fcntl = fcntl
        self.temp = tempfile.TemporaryDirectory(prefix='listener-independent-')
        self.root = Path(self.temp.name)
        self.state = self.root / 'private-state'
        self.skill = self.root / 'copied-skill'
        self.processes = []
        self.clock = 1800000000.0
        atomic_json(self.root / 'clock.json', self.clock)
        (self.root / 'sitecustomize.py').write_text(CLOCK_SITE)
        self.env = dict(os.environ)
        # Only child processes receive the clock seam; the unittest/proof host
        # retains its own clock, imports, environment and proof descriptors.
        self.env.update(PYTHONPATH=str(self.root), PYTHONNOUSERSITE='1',
                        PYTHONDONTWRITEBYTECODE='1',
                        LISTENER_FIXTURE_CLOCK=str(self.root / 'clock.json'),
                        PATH=str(self.root) + ':/usr/bin:/bin')
        for name in ('lark-peer', 'codex-peer'):
            path = self.root / name
            path.write_text('#!' + sys.executable + '\n' + PEER)
            path.chmod(0o700)
        atomic_json(self.root / 'world.json', {
            'remote': REMOTE, 'messages': {'om_root': message('om_root', 'root')},
            'pages': {THREAD: [[]]}, 'queue_mode': 'ok'})
        if copy_skill:
            source = ROOT / 'skills/lark-listener'
            if not (source / 'scripts/listener.py').is_file():
                self.temp.cleanup()
                raise AssertionError('Public listener entrypoint is absent: ' + str(source))
            shutil.copytree(source, self.skill)
        self.entry = self.skill / 'scripts/listener.py'
        self.request = {'subscription_id': 'fixture-sub', 'source': 'om_root',
            'owner_session': OWNER, 'remote': REMOTE, 'identity': 'user',
            'lark_bin': str(self.root / 'lark-peer'), 'codex_bin': str(self.root / 'codex-peer')}

    def world(self):
        return json.loads((self.root / 'world.json').read_text())

    def change(self, **values):
        with (self.root / 'peer.lock').open('a') as lock:
            self.fcntl.flock(lock, self.fcntl.LOCK_EX)
            world = self.world()
            world.update(values)
            atomic_json(self.root / 'world.json', world)

    def pages(self, pages, *, thread=THREAD):
        with (self.root / 'peer.lock').open('a') as lock:
            self.fcntl.flock(lock, self.fcntl.LOCK_EX)
            world = self.world()
            world['pages'][thread] = pages
            for page in pages:
                for msg in page:
                    world['messages'][msg['message_id']] = msg
            atomic_json(self.root / 'world.json', world)

    def advance(self, seconds):
        self.clock += seconds
        atomic_json(self.root / 'clock.json', self.clock)

    def calls(self, op=None):
        path = self.root / 'calls.jsonl'
        if not path.exists():
            return []
        # Read while the peer is between atomic complete records.
        with (self.root / 'peer.lock').open('a') as lock:
            self.fcntl.flock(lock, self.fcntl.LOCK_SH)
            result = [json.loads(line) for line in path.read_text().splitlines()]
        return result if op is None else [row for row in result if row['op'] == op]

    def command(self, operation, request=None, *, timeout=8, expected=0):
        proc = self.launch(operation, request)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            raise AssertionError(f'{operation} did not return within {timeout}s; stdout={out!r}, stderr={err!r}')
        if proc.returncode != expected:
            raise AssertionError(f'{operation}: expected exit {expected}, got {proc.returncode}; stdout={out!r}, stderr={err!r}')
        try:
            result = json.loads(out)
        except json.JSONDecodeError as exc:
            raise AssertionError(f'{operation} must emit exactly one JSON result: {out!r}; stderr={err!r}') from exc
        if not isinstance(result, dict):
            raise AssertionError(f'{operation} result is not a JSON object: {result!r}')
        return result

    def launch(self, operation, request=None):
        req = dict(self.request if operation == 'start' else {
            'subscription_id': self.request['subscription_id'], 'owner_session': self.request['owner_session']})
        if request is not None:
            req.update(request)
        proc = subprocess.Popen([sys.executable, '-B', str(self.entry), '--state-dir',
            str(self.state), operation, '--request', '-'], cwd=self.root,
            env=self.env, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True)
        self.processes.append(proc)
        proc.stdin.write(json.dumps(req))
        proc.stdin.close()
        proc.stdin = None
        return proc

    def wait(self, predicate, *, timeout=6, description='observable condition'):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            result = predicate()
            if result:
                return result
            time.sleep(.025)
        raise AssertionError('Timed out waiting for ' + description + '; diagnostics=' + str(self.diagnostics()))

    def start(self, **request):
        return self.command('start', request)

    def stop(self):
        return self.command('stop')

    def status(self):
        return self.command('status')

    def read_once(self):
        before = len(self.calls('+threads-messages-list'))
        result = self.start()
        self.wait(lambda: len(self.calls('+threads-messages-list')) > before,
                  description='first successful worker request')
        return result

    def queues(self):
        return self.calls('queue')

    def batches(self):
        return [row for row in self.queues() if 'batch_id' in row['message']]

    def receive(self, row, owner=None):
        return self.command('receive', {'batch_id': batch_id(row['message']),
            'owner_session': owner or row['owner']}, expected=0 if owner in (None, row['owner']) else 2)

    def restart(self):
        self.stop()
        return self.read_once()

    def latest_worker(self):
        rows = self.calls('+threads-messages-list')
        if not rows:
            raise AssertionError('No worker has made a read')
        return rows[-1]['parent']

    def kill_identity(self, ident):
        actual = proc_identity(ident['pid'])
        if actual and actual['starttime'] == ident['starttime'] and str(self.skill) in actual['cmdline']:
            os.kill(ident['pid'], signal.SIGKILL)
        else:
            raise AssertionError('Refuse to kill unverified/non-owned process')

    def state_bytes(self):
        return b'\n'.join(p.read_bytes() for p in self.state.rglob('*') if p.is_file())

    def owned_processes(self):
        # Exact private executable paths, not account, PID alone or basename.
        executables = {str(self.entry), str(self.skill / 'scripts/worker.py'),
                       str(self.root / 'lark-peer'), str(self.root / 'codex-peer')}
        result = []
        for path in Path('/proc').glob('[0-9]*/cmdline'):
            try:
                argv = os.fsdecode(path.read_bytes()).split('\x00')
                if not executables.intersection(argv):
                    continue
                ident = proc_identity(int(path.parent.name))
                if ident and ident['state'] != 'Z':
                    result.append(ident)
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                pass
        return result

    def diagnostics(self):
        # Never wait on peer.lock when diagnosing a possibly stalled peer.
        destination = Path(tempfile.mkdtemp(prefix='listener-failure-'))
        snapshot = {'monotonic': time.monotonic(), 'sandbox': str(self.root), 'processes': []}
        for ident in self.owned_processes():
            row = dict(ident)
            for name in ('wchan', 'syscall'):
                try:
                    row[name] = (Path('/proc') / str(ident['pid']) / name).read_text()
                except OSError as exc:
                    row[name] = type(exc).__name__
            snapshot['processes'].append(row)
        for source, name in ((self.state / 'state.json', 'state.json'),
                             (self.state / 'worker.log', 'worker.log'),
                             (self.root / 'calls.jsonl', 'calls.jsonl'),
                             (self.root / 'world.json', 'world.json')):
            try:
                (destination / name).write_bytes(source.read_bytes())
            except OSError as exc:
                snapshot[name] = type(exc).__name__
        (destination / 'snapshot.json').write_text(json.dumps(snapshot, indent=2))
        return destination

    def close(self):
        if not self.root.exists():
            return
        world = self.world()
        if world.get('block'):
            (self.root / (world['block']['name'] + '.release')).touch()
        deadline = time.monotonic() + 5
        while True:
            owned = self.owned_processes()
            if not owned:
                break
            if time.monotonic() >= deadline:
                # Keep the sandbox if a writer cannot be proven stopped.
                self.temp._finalizer.detach()
                raise AssertionError('Owned processes did not exit; diagnostics=' + str(self.diagnostics()))
            for ident in owned:
                actual = proc_identity(ident['pid'])
                if actual and actual['starttime'] == ident['starttime'] and actual['cmdline'] == ident['cmdline']:
                    try:
                        os.kill(ident['pid'], signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            time.sleep(.01)
        for proc in self.processes:
            proc.communicate(timeout=5)
        self.temp.cleanup()


class WireFixtureTests(unittest.TestCase):
    """Author-time checks of fixture grammar; these are not product evidence."""
    def setUp(self):
        self.s = Sandbox(copy_skill=False)
        self.addCleanup(self.s.close)

    def peer(self, name, *args):
        return subprocess.run([str(self.s.root / name), *args], env=self.s.env,
            cwd=self.s.root, text=True, capture_output=True, timeout=5)

    def test_public_success_and_permission_envelopes(self):
        who = self.peer('lark-peer', 'whoami', '--as', 'user')
        self.assertEqual(who.returncode, 0)
        self.assertEqual(json.loads(who.stdout)['onBehalfOf']['openId'], 'ou_fixture_actor')
        args = ('im', '+threads-messages-list', '--thread', THREAD,
                '--page-size', '50', '--order', 'desc', '--no-reactions')
        ok = self.peer('lark-peer', *args)
        self.assertEqual(ok.returncode, 0)
        self.assertEqual(json.loads(ok.stdout)['data']['messages'], [])
        self.s.change(list_error=True)
        failed = self.peer('lark-peer', *args)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(json.loads(failed.stderr)['error']['code'], 99991679)

    def test_queue_receipt_and_warning_are_independent(self):
        self.assertTrue(REMOTE.startswith(('ws://', 'wss://', 'unix://')))
        self.s.change(queue_mode='wake-failed')
        result = self.peer('codex-peer', 'queue', '--remote', REMOTE,
            '--thread', OWNER, '--message', 'batch_id: fixture', '--resume')
        self.assertEqual(result.returncode, 0)
        self.assertRegex(result.stdout, r'^Queued message [0-9a-f-]{36} for thread ' + OWNER + r'\.')
        self.assertIn('warning', result.stderr)

    def test_queue_fixture_rejects_unsupported_http_remote(self):
        invalid = 'http://127.0.0.1:9'
        self.s.change(remote=invalid)
        result = self.peer('codex-peer', 'queue', '--remote', invalid,
            '--thread', OWNER, '--message', 'batch_id: fixture', '--resume')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')

    def test_clock_is_child_only_and_does_not_replace_monotonic(self):
        self.s.advance(30 * DAY)
        result = subprocess.run([sys.executable, '-B', '-c',
            'import json,time; print(json.dumps([time.time(),time.monotonic()]))'],
            env=self.s.env, cwd=self.s.root, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        wall, mono = json.loads(result.stdout)
        self.assertEqual(wall, self.s.clock)
        self.assertLess(abs(mono - time.monotonic()), 5)
        self.assertNotEqual(time.time(), wall)

    def test_notification_parser_does_not_depend_on_json_layout(self):
        self.assertEqual(batch_id('Notice ' + json.dumps({'delivery': {'batch_id': 'b-1'}})), 'b-1')
        self.assertEqual(batch_id('subscription_id: s\nbatch_id: b-2'), 'b-2')
        with self.assertRaises(AssertionError):
            batch_id('No identifiable batch')
