#!/usr/bin/env python3
"""Standalone topic listener CLI. Every command accepts a JSON object on stdin.

Use --state-dir DIR COMMAND --request -. subscription_id is required except help.
See the Skill for operator workflow; protocol fields live in command handlers here.
"""
from __future__ import annotations
import argparse
import copy
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid
from store import ListenerError, Store, alive, digest, now, process_start
import transport
import worker

COMMANDS = ('start', 'receive', 'reply', 'status', 'stop')

def required(r, *keys):
    if any(not isinstance(r.get(k), str) or not r[k] for k in keys):
        raise ListenerError('REQUIRED_FIELD_' + '_'.join(keys))

def verify_reply(s, mid):
    m = transport.mget(s,mid)
    if not m or m.get('chat_id') != s.get('chat_id') or m.get('thread_id') != s.get('thread_id'):
        raise ListenerError('REPLY_TARGET_MISMATCH')
    return m

def reply(store, r):
    required(r,'request_id','reply_to','text')
    if r.get('receipt_message_id'):
        return reconcile(store, {**r, 'message_id':r['receipt_message_id']})
    with store.transaction() as data:
        s = copy.deepcopy(store.subscription(data,r,True))
        if not s['active']:
            raise ListenerError('SUBSCRIPTION_STOPPED')
        key = digest([s['subscription_id'],s['owner_session'],r['request_id']])
        prior = data['sends'].get(key)
        if prior:
            if prior.get('text_digest',digest(prior.get('text'))) != digest(r['text']) or prior['reply_to'] != r['reply_to']:
                raise ListenerError('SEND_REQUEST_CONFLICT')
            if prior['state'] == 'sending' and not alive(prior.get('process')):
                prior['state'] = 'unknown'
            return {'status':'OK' if prior['state']=='sent' else 'NEEDS_USER','subscription_id':s['subscription_id'],'request_id':r['request_id'],'state':prior['state'],'message_id':prior.get('message_id')}
    ident=transport.identity(s)
    if ident['app']!=s['app'] or ident['profile']!=s['account'] or ident['sender_id']!=s['sender_id']:raise ListenerError('LARK_ACCOUNT_DRIFT')
    verify_reply(s,r['reply_to'])
    with store.transaction() as data:
        current = store.subscription(data,r,True)
        if not current['active'] or current['generation'] != s['generation']:
            raise ListenerError('SUBSCRIPTION_CHANGED')
        if key in data['sends']:
            raise ListenerError('SEND_REQUEST_IN_FLIGHT')
        data['sends'][key] = {'subscription_id':s['subscription_id'],'origin':s['owner_session'],'request_id':r['request_id'],
            'text':r['text'],'text_digest':digest(r['text']),'reply_to':r['reply_to'],'idempotency_key':'ll-'+key[:44], 'state':'sending','created_at':now(),
            'process':{'pid':os.getpid(),'starttime':process_start(os.getpid())}}
    try:
        result = transport.lark(s,['im','+messages-reply'],'--message-id',r['reply_to'],'--reply-in-thread',
            '--idempotency-key','ll-'+key[:44],'--text',r['text'])
        mid = result.get('message_id')
        if not isinstance(mid,str) or not mid.startswith('om_'):
            raise ListenerError('SEND_RECEIPT_INVALID')
    except ListenerError:
        with store.transaction() as data:
            data['sends'][key]['state'] = 'unknown'
        return {'status':'NEEDS_USER','subscription_id':s['subscription_id'],'request_id':r['request_id'],'state':'unknown'}
    original = None
    try:
        m = verify_reply(s,mid)
        if (m.get('sender') or {}).get('id') != s['sender_id'] or transport.text(m.get('content')) != r['text']:
            raise ListenerError('SEND_READBACK_MISMATCH')
        original = worker.version(m)
    except ListenerError:
        pass  # successful send receipt still binds message identity; never resend
    with store.transaction() as data:
        intent = data['sends'][key]
        intent.update(state='sent',message_id=mid,original_version=original,completed_at=now())
        worker.release_echoes(data,data['subscriptions'][s['subscription_id']])
    return {'status':'OK','subscription_id':s['subscription_id'],'request_id':r['request_id'],'state':'sent','message_id':mid}

def reconcile(store, r):
    required(r,'request_id','message_id')
    with store.transaction() as data:
        s = copy.deepcopy(store.subscription(data,r,True))
        key = digest([s['subscription_id'],s['owner_session'],r['request_id']])
        intent = copy.deepcopy(data['sends'].get(key))
        if not intent:
            raise ListenerError('SEND_NOT_FOUND')
    if intent.get('text_digest') != digest(r['text']) or intent['reply_to'] != r['reply_to']:
        raise ListenerError('SEND_REQUEST_CONFLICT')
    m = verify_reply(s,r['message_id'])
    if (m.get('sender') or {}).get('id') != s['sender_id'] or transport.text(m.get('content')) != intent.get('text'):
        raise ListenerError('SEND_READBACK_MISMATCH')
    if m.get('parent_id') and m['parent_id'] != intent['reply_to']:
        raise ListenerError('SEND_PARENT_MISMATCH')
    with store.transaction() as data:
        store.subscription(data,r,True)
        old = data['sends'][key]
        if old.get('message_id') and old['message_id'] != r['message_id']:
            raise ListenerError('SEND_ALREADY_BOUND')
        old.update(state='sent',message_id=r['message_id'],original_version=worker.version(m),completed_at=old.get('completed_at',now()))
        worker.release_echoes(data,data['subscriptions'][s['subscription_id']])
    return {'status':'OK','subscription_id':s['subscription_id'],'state':'sent','message_id':r['message_id']}

def stop(store,r):
    with store.transaction() as data:
        s=store.subscription(data,r,True)
        s['active']=False;s['generation']+=1;s['stopped_at']=now()
        process=copy.deepcopy(s.get('process'))
    if alive(process):
        try:
            raw=Path('/proc',str(process['pid']),'cmdline').read_bytes()
            if not process.get('instance') or str(Path(__file__).with_name('worker.py').resolve()).encode() not in raw or str(store.root).encode() not in raw or (b'--instance' in raw and process['instance'].encode() not in raw):
                raise ListenerError('PROCESS_INSTANCE_MISMATCH')
            os.kill(process['pid'],signal.SIGTERM)
        except ProcessLookupError:pass
    elif process and process_start(process.get('pid')) is not None:
        raise ListenerError('PROCESS_IDENTITY_DRIFT')
    until=time.monotonic()+35
    while time.monotonic()<until:
        with store.transaction() as data:
            current=data['subscriptions'][r['subscription_id']]
            inflight=copy.deepcopy(current.get('notify_inflight'))
            health_inflight=copy.deepcopy(current.get('health_notice_inflight'))
        if not alive(process) and not alive(inflight) and not alive(health_inflight):break
        time.sleep(.05)
    else:raise ListenerError('STOP_DRAIN_PENDING','NEEDS_USER')
    return worker.status(store,r['subscription_id'])

def start(store, r):
    """Bind source/owner/remote/identity/lark_bin/codex_bin, then start or resume.

    Existing bindings accept subscription_id and owner_session only. After index
    expiry backlog must be explicitly all or new. No ownership transfer exists.
    """
    required(r, 'subscription_id', 'owner_session')
    if not re.fullmatch(transport.UUID,r['owner_session']):
        raise ListenerError('OWNER_INVALID')
    with store.transaction() as data:
        existing=copy.deepcopy(data['subscriptions'].get(r['subscription_id']))
    if existing:
        if existing['owner_session']!=r['owner_session']:
            raise ListenerError('OWNER_MISMATCH')
        for key in ('source','remote','identity','lark_bin','codex_bin'):
            value=transport.executable(r[key]) if key in r and key in ('lark_bin','codex_bin') else r.get(key)
            if key in r and value!=existing[key]:raise ListenerError('SUBSCRIPTION_CONFLICT')
        if alive(existing.get('process')):
            return worker.status(store,r['subscription_id'])
        if existing.get('index_cleared_at') and r.get('backlog') not in ('all','new'):
            raise ListenerError('NEW_READ_BOUNDARY_REQUIRED')
    else:
        required(r,'source','remote','identity','lark_bin','codex_bin')
        if r['identity'] not in ('bot','user') or not r['remote'].startswith(('unix://','ws://','wss://')):
            raise ListenerError('BINDING_INVALID')
        transport.source_id(r['source'])
    if r.get('backlog','all') not in ('all','new'):raise ListenerError('BACKLOG_INVALID')
    spec=existing or {k:r[k] for k in ('subscription_id','source','owner_session','remote','identity','lark_bin','codex_bin')}
    spec['lark_bin']=transport.executable(spec['lark_bin']);spec['codex_bin']=transport.executable(spec['codex_bin'])
    transport.queue_capability(spec)
    account=transport.identity(spec)
    if not account.get('sender_id'):raise ListenerError('SENDER_IDENTITY_MISSING')
    if existing and (account['profile']!=existing['account'] or account['app']!=existing['app'] or account['sender_id']!=existing['sender_id']):
        raise ListenerError('LARK_ACCOUNT_DRIFT')
    with store.transaction() as data:
        old=data['subscriptions'].get(r['subscription_id'])
        if old!=existing:raise ListenerError('SUBSCRIPTION_CHANGED')
        if not old:
            old={**spec,'account':account['profile'],'app':account['app'],'sender_id':account['sender_id'],
                 'generation':0,'created_at':now(),'coverage':'unknown','cursor':None,'failures':0}
            data['subscriptions'][r['subscription_id']]=old
        if not existing or old.get('index_cleared_at'):
            old.update(backlog=r.get('backlog','all'),bootstrap=True,cursor=None,
                       first_read_boundary={'mode':r.get('backlog','all'),'observed_at':now()})
            old.pop('index_cleared_at',None)
        old['active']=True;old['generation']+=1
        old['coverage']='unknown';old['last_read_at']=None
    instance=str(uuid.uuid4())
    logfd=store.open_private(store.root/'worker.log',os.O_WRONLY|os.O_CREAT|os.O_APPEND)
    try:
        child=subprocess.Popen([sys.executable,'-B',str(Path(__file__).with_name('worker.py').resolve()),
            '--state-dir',str(store.root),'--subscription',r['subscription_id'],'--instance',instance],
            stdin=subprocess.DEVNULL,stdout=logfd,stderr=logfd,start_new_session=True)
    finally:os.close(logfd)
    until=time.monotonic()+3
    while time.monotonic()<until:
        with store.transaction() as data: exists=r['subscription_id'] in data['subscriptions']
        if exists:worker.collect_expired(store,r['subscription_id'])
        result=worker.status(store,r['subscription_id'])
        process=result.get('process')
        if process and process.get('instance')==instance and result['running']:
            return result
        if child.poll() is not None:break
        time.sleep(.05)
    raise ListenerError('WORKER_START_FAILED')

def receive(store,r):
    """Confirm the immutable batch already delivered to this owner, not business completion."""
    required(r,'owner_session','batch_id')
    with store.transaction() as data:
        s=store.subscription(data,r,True)
        batch=data['batches'].get(r['batch_id'])
        if not batch or batch['subscription_id']!=s['subscription_id'] or batch['owner_session']!=s['owner_session']:
            raise ListenerError('BATCH_BINDING_INVALID')
        if not batch.get('received_at'):
            batch['received_at']=now()
            for did in batch['delivery_ids']:
                data['deliveries'][did].update(state='received',received_at=batch['received_at'])
        return {'status':'OK','subscription_id':s['subscription_id'],'batch_id':r['batch_id'],
                'received_at':batch['received_at'],'message_count':len(batch['delivery_ids'])}

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, epilog="""JSON requests (stdin):
  All: subscription_id; mutations also owner_session.
  start (first): source, remote, identity(bot|user), lark_bin, codex_bin;
                optional backlog(all|new). Repeat start resumes same binding.
  receive: batch_id from the notification already read by this session.
  reply: request_id, reply_to, text; optional receipt_message_id to reconcile
         an unknown send without sending again. Keep request_id stable.
  status / stop: subscription_id (stop also owner_session).
Results distinguish started, healthy, queued and received; none means business done.
""")
    parser.add_argument('--state-dir',required=True)
    parser.add_argument('command',choices=COMMANDS)
    parser.add_argument('--request',default='-',choices=['-'])
    args=parser.parse_args(argv)
    try:
        r=json.load(sys.stdin)
        if not isinstance(r,dict):raise ListenerError('REQUEST_OBJECT_REQUIRED')
        required(r,'subscription_id')
        # Bound foreground lock contention; the worker keeps its durable transaction.
        store=Store(args.state_dir, lock_timeout=2)
        with store.transaction() as data: exists=r['subscription_id'] in data['subscriptions']
        if exists and args.command=='start':worker.collect_expired(store,r['subscription_id'])
        result=worker.status(store,r['subscription_id']) if args.command=='status' else globals()[args.command](store,r)
        print(json.dumps(result,ensure_ascii=False));return 0
    except ListenerError as e:
        print(json.dumps({'status':e.status,'code':e.code}));return 2
    except (ValueError,TypeError,KeyError,OSError):
        print(json.dumps({'status':'ERROR','code':'INVALID_INPUT_OR_LOCAL_STATE'}));return 2

if __name__=='__main__':raise SystemExit(main())
