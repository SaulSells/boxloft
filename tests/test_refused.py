"""Refused saves leave a line (Alex 11-16): the Bratz refusal had no trace for 4 days."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import refused  # noqa: E402
from test_cover_all import srv  # noqa: E402,F401  (fixture)


def test_a_refusal_is_kept_with_only_known_keys(tmp_path):
    body = json.dumps({'code': '0850001234561', 'qty': 3, 'option': {'kind': 'new', 'cost_cents': 5},
                       'password': 'x', 'replaces': 7}).encode()
    refused.record(str(tmp_path), 'POST', '/api/put/12/qty', 400, 'That product is not in Boxloft', 'sam', 'phone', body)
    refused.record(str(tmp_path), 'POST', '/api/move', 409, 'Alex moved it first', 'sam')
    t = refused.today(str(tmp_path))
    assert (t['count'], t['conflicts']) == (2, 1)
    first = t['recent'][-1]
    assert (first['user'], first['path'], first['message']) == ('sam', '/api/put/12/qty', 'That product is not in Boxloft')
    assert first['body'] == {'code': '0850001234561', 'qty': 3, 'replaces': 7, 'option': 'new'}   # no password


def test_sign_in_and_secret_routes_and_no_user_are_not_kept(tmp_path):
    refused.record(str(tmp_path), 'POST', '/api/auth/login', 400, 'Wrong password', 'sam', '', b'{"password": "x"}')
    refused.record(str(tmp_path), 'POST', '/api/connections/ebay', 400, 'Bad key', 'alex')
    refused.record(str(tmp_path), 'POST', '/api/put', 400, 'x', '')
    assert refused.today(str(tmp_path))['count'] == 0


def test_a_bad_body_or_a_full_file_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(refused, 'MAX_BYTES', 200)
    for i in range(5):
        refused.record(str(tmp_path), 'POST', '/api/put', 400, 'refused ' + 'x' * 50, 'sam', '', b'not json')
    assert os.path.exists(tmp_path / 'refused.jsonl.1') and refused.today(str(tmp_path))['count'] >= 1


def test_a_refused_save_on_the_server_shows_on_overview(srv):  # noqa: F811
    """End to end: Sam's refused save is on Alex's Overview with her name."""
    server, call = srv
    st, body = call('Sam', '/api/put/999999/qty', {'qty': 3})
    assert st == 400
    st, s = call('Alex', '/api/summary')
    assert st == 200 and s['refused']['count'] == 1
    r = s['refused']['recent'][0]
    assert (r['user'], r['path'], r['status'], r['message']) == ('sam', '/api/put/999999/qty', 400, body['error'])
