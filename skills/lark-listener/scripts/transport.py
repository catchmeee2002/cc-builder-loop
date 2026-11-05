"""Only lark-cli/Codex subprocess adapters; no credentials or alternate HTTP client."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import parse_qs, urlsplit
from store import ListenerError

UUID = r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}'

def executable(value):
    if not isinstance(value, str) or not value:
        raise ListenerError('EXECUTABLE_REQUIRED')
    path = shutil.which(value)
    if path is None:
        raise ListenerError('EXECUTABLE_UNAVAILABLE')
    return str(Path(path).resolve())

def call(argv, cwd=None):
    try:
        r = subprocess.run(argv, cwd=cwd, env={**os.environ, 'LARK_CLI_NO_PROXY': '1'}, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise ListenerError('TRANSPORT_UNKNOWN', 'DEGRADED')
    return r

def lark(s, command, *args, cwd=None):
    r = call([s['lark_bin'], *command, *args, '--as', s['identity'], *(['--profile',s['account']] if s.get('account') else [])], cwd)
    try:
        result = json.loads(r.stdout)
    except ValueError:
        raise ListenerError('LARK_RESPONSE_INVALID', 'DEGRADED')
    if r.returncode or result.get('ok') is not True:
        # Never persist stderr, auth envelopes, environment, or token responses.
        raise ListenerError('LARK_REQUEST_FAILED', 'DEGRADED')
    return result.get('data', {})

def identity(s):
    r = call([s['lark_bin'], 'whoami'])
    try:
        x = json.loads(r.stdout)
    except ValueError:
        raise ListenerError('LARK_IDENTITY_INVALID')
    if r.returncode or x.get('available') is False:
        raise ListenerError('LARK_IDENTITY_UNAVAILABLE')
    x = x.get('data', x)
    app = x.get('appId') or x.get('profile')
    user = (x.get('onBehalfOf') or {}).get('openId')
    if not app:
        raise ListenerError('LARK_IDENTITY_MISSING')
    return {'app': app, 'profile': x.get('profile', app), 'sender_id': app if s['identity'] == 'bot' else user}

def source_id(source):
    if not isinstance(source, str):
        raise ListenerError('SOURCE_INVALID')
    if re.fullmatch(r'om[t]?_[A-Za-z0-9]+', source):
        return source
    u = urlsplit(source)
    if u.scheme != 'https' or not u.hostname or not any(u.hostname == d or u.hostname.endswith('.' + d) for d in ('feishu.cn', 'larksuite.com')):
        raise ListenerError('SOURCE_URL_INVALID')
    q = parse_qs(u.query)
    for key in ('open_thread_id', 'openthreadid', 'message_id', 'open_message_id'):
        if key in q:
            return source_id(q[key][0])
    match = re.search(r'/message/(om_[A-Za-z0-9]+)', u.path)
    if match:
        return match[1]
    raise ListenerError('SOURCE_MESSAGE_REQUIRED')

def mget(s, mid):
    x = lark(s, ['im', '+messages-mget'], '--message-ids', mid, '--no-reactions')
    rows = x.get('messages', [])
    return next((m for m in rows if m.get('message_id') == mid), None)

def resolve(s):
    source = source_id(s['source'])
    if source.startswith('omt_'):
        x = page(s, source, None)
        rows = x.get('messages', [])
        return source, rows[0].get('chat_id') if rows else x.get('chat_id')
    message = mget(s, source)
    if not message:
        raise ListenerError('SOURCE_NOT_VISIBLE', 'DEGRADED')
    return message.get('thread_id'), message.get('chat_id')

def page(s, topic, cursor):
    args = ['--thread', topic, '--page-size', '50', '--order', 'desc', '--no-reactions']
    if cursor:
        args += ['--page-token', cursor]
    x = lark(s, ['im', '+threads-messages-list'], *args)
    if not isinstance(x.get('messages'), list) or not isinstance(x.get('has_more'), bool):
        raise ListenerError('PAGE_RESPONSE_INVALID', 'DEGRADED')
    if x['has_more'] and not x.get('page_token'):
        raise ListenerError('PAGE_TOKEN_MISSING', 'DEGRADED')
    return x

def queue(s, body):
    r = call([s['codex_bin'], 'queue', '--remote', s['remote'], '--thread', s['owner_session'], '--message', body, '--resume'])
    receipt = re.search(r'^Queued message (' + UUID + r') for thread (' + UUID + r')\.?$', r.stdout, re.M)
    if r.returncode or not receipt or receipt[2].lower() != s['owner_session'].lower():
        return {'state': 'unknown', 'error': 'QUEUE_RECEIPT_UNAVAILABLE'}
    return {'state': 'queued', 'queue_id': receipt[1], 'owner_session': receipt[2], 'wake_warning': bool(r.stderr.strip())}

def text(value):
    if isinstance(value, str):
        try:
            x = json.loads(value)
            if isinstance(x, dict) and isinstance(x.get('text'), str):
                return x['text']
        except ValueError:
            pass
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False)

def resources(m):
    content = m.get('content', '')
    if not isinstance(content, str):
        content = json.dumps(content)
    found = {('image' if key.startswith('img_') else 'file', key) for key in re.findall(r'(?:file|img)_[A-Za-z0-9_-]+', content)}
    for item in m.get('resources', []) or []:
        if item.get('key'):
            found.add((item.get('type', 'file'), item['key']))
    return [{'type': kind, 'key': key, 'state': 'not_downloaded'} for kind, key in sorted(found)]
