"""version() normalizes a missing update_time to create_time; echo suppression survives the fingerprint race.

Covers run lark-listener-update-time-normalization-20261113-063112-3e19:
- B1: missing update_time participates in the version digest as create_time.
- B2: a sent intent whose original_version came from a readback without
  update_time still suppresses the echo when the patrol message carries
  update_time == create_time.
- B3: update_time != create_time is never judged echo, even with identical body.
- B4: update_time remains a component of the version digest.
"""
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/lark-listener/scripts'
sys.path.insert(0, str(SCRIPTS))

import transport  # noqa: E402
import worker  # noqa: E402

CONTENT = json.dumps({'text': 'hello'}, ensure_ascii=False)
CHANGED_CONTENT = json.dumps({'text': 'changed body'}, ensure_ascii=False)


def base_message(**overrides):
    """A patrol/readback message dict: create_time='T', update_time absent unless given."""
    m = {'message_id': 'M', 'chat_id': 'chat', 'thread_id': 'topic', 'content': CONTENT,
         'create_time': 'T', 'deleted': False, 'sender': {'id': 'sender'}}
    m.update(overrides)
    return m


def make_subscription():
    return {'subscription_id': 'sub', 'owner_session': 'sess', 'chat_id': 'chat',
            'thread_id': 'topic', 'account': 'acct', 'identity': 'ident',
            'sender_id': 'sender', 'backlog': 'all', 'bootstrap': False}


def sent_intent():
    """A sent intent whose original_version was recorded from the send readback
    (message_id='M', update_time missing, create_time='T')."""
    readback = base_message()
    return {'request_id': 'r1', 'subscription_id': 'sub', 'origin': 'sess',
            'message_id': 'M', 'state': 'sent', 'text': transport.text(CONTENT),
            'original_version': worker.version(readback)}


def make_data():
    return {'messages': {}, 'deliveries': {}, 'sends': {}}


def sole_delivery(data):
    deliveries = list(data['deliveries'].values())
    assert len(deliveries) == 1
    return deliveries[0]


# --- B1: missing update_time is normalized to create_time in version() ---

def test_b1_missing_update_time_matches_create_time_variant():
    variant_a = base_message()  # update_time 缺失
    variant_b = base_message(update_time='T')  # update_time='T'
    assert worker.version(variant_a) == worker.version(variant_b)


def test_b1_no_normalization_when_create_time_also_missing():
    both_missing = base_message()
    del both_missing['create_time']  # update_time 与 create_time 均缺失
    with_create_time = base_message(update_time='T')  # create_time='T'
    assert worker.version(both_missing) != worker.version(with_create_time)
    # 缺失的 update_time 按 None 参与摘要，与显式 None 一致
    explicit_none = base_message(update_time=None)
    del explicit_none['create_time']
    assert worker.version(both_missing) == worker.version(explicit_none)


def test_b1_real_update_time_wins_over_create_time():
    # update_time 存在时一律用真实值，不与 create_time 比较
    assert worker.version(base_message(update_time='T2')) != worker.version(base_message(update_time='T'))
    assert worker.version(base_message(update_time='T2', create_time='T')) == \
        worker.version(base_message(update_time='T2', create_time='OTHER'))


def test_b1_digest_still_tracks_content_deleted_resources():
    normalized = base_message()  # update_time 缺失，归一化场景
    assert worker.version(normalized) != worker.version(base_message(content=CHANGED_CONTENT))
    assert worker.version(normalized) != worker.version(base_message(deleted=True))
    assert worker.version(normalized) != \
        worker.version(base_message(resources=[{'type': 'image', 'key': 'img_sample'}]))


# --- B2: normalized fingerprint restores echo suppression for the sent intent ---

def test_b2_echo_delivery_when_normalized_version_matches_sent_intent():
    data = make_data()
    data['sends']['r1'] = sent_intent()
    worker.ingest(data, make_subscription(), base_message(update_time='T'))
    assert sole_delivery(data)['state'] == 'echo'


def test_b2_pending_when_intent_scope_mismatches():
    for field, value in (('subscription_id', 'other-sub'), ('origin', 'other-session')):
        data = make_data()
        intent = sent_intent()
        intent[field] = value
        data['sends']['r1'] = intent
        worker.ingest(data, make_subscription(), base_message(update_time='T'))
        assert sole_delivery(data)['state'] == 'pending', field


def test_b2_pending_without_send_intent_even_for_own_messages():
    data = make_data()  # 无任何 send intent
    worker.ingest(data, make_subscription(), base_message(update_time='T'))
    assert sole_delivery(data)['state'] == 'pending'


# --- B3: update_time != create_time is never echo ---

def test_b3_pending_when_update_time_differs_and_body_changed():
    data = make_data()
    data['sends']['r1'] = sent_intent()
    m = base_message(update_time='T2', content=CHANGED_CONTENT)
    worker.ingest(data, make_subscription(), m)
    assert sole_delivery(data)['state'] == 'pending'


def test_b3_not_echo_even_when_body_matches_intent_text():
    data = make_data()
    data['sends']['r1'] = sent_intent()
    worker.ingest(data, make_subscription(), base_message(update_time='T2'))
    assert sole_delivery(data)['state'] == 'pending'


# --- B4: update_time stays part of the version digest ---

def test_b4_update_time_remains_in_version_digest():
    assert worker.version(base_message(update_time='T')) != \
        worker.version(base_message(update_time='T2'))
