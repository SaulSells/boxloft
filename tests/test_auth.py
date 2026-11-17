"""Sign-in and permissions over real HTTP (skeptical review 11-17: breaking the password check, the admin check, the
lockout, session expiry or the device token's scope left every other test green). Each test here fails if one of
those checks is taken out."""
import importlib
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

PASSWORD = 'correct horse battery'


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setenv('BOXLOFT_DATA', str(tmp_path))
    import server
    importlib.reload(server)
    import auth as auth_mod
    monkeypatch.setattr(auth_mod.time, 'sleep', lambda s: None)   # the 0.5 s pause after a wrong password
    from http.server import ThreadingHTTPServer
    for name, role in (('boss', 'admin'), ('floor', 'staff')):
        server.auth.complete_setup(server.auth.create_user(name, name.title(), role), PASSWORD)
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{httpd.server_address[1]}'

    def call(path, body=None, cookie=None, ip=None, headers=None, csrf=True):
        h = {'Content-Type': 'application/json'}
        if csrf:
            h['X-Boxloft'] = '1'
        if cookie:
            h['Cookie'] = cookie
        if ip:
            h['X-Forwarded-For'] = ip
        h.update(headers or {})
        req = urllib.request.Request(base + path, method='POST' if body is not None else 'GET',
                                     data=json.dumps(body).encode() if body is not None else None, headers=h)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b'null'), r.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'null'), e.headers
    yield server, call
    httpd.shutdown()


def login(call, user, password=PASSWORD, ip='203.0.113.5'):
    st, body, headers = call('/api/auth/login', {'username': user, 'password': password}, ip=ip)
    cookie = (headers.get('Set-Cookie') or '').split(';')[0]
    return st, body, cookie


def test_wrong_password_is_refused_and_right_one_signs_in(site):
    server, call = site
    st, body, cookie = login(call, 'floor', 'not the password')
    assert st != 200 and 'Wrong username or password' in body['error'] and not cookie.endswith('=')
    assert call('/api/auth/me', cookie=cookie or None)[0] == 401
    st, body, cookie = login(call, 'floor')
    assert st == 200 and body['user']['username'] == 'floor'
    assert call('/api/auth/me', cookie=cookie)[1]['user']['username'] == 'floor'


def test_unknown_user_and_empty_password_are_refused(site):
    _, call = site
    assert login(call, 'nobody')[0] != 200
    assert login(call, 'floor', '')[0] != 200


def test_no_session_means_401(site):
    _, call = site
    assert call('/api/products')[0] == 401
    assert call('/api/auth/me', cookie='boxloft_session=made-up-token')[0] == 401


def test_staff_cannot_use_admin_routes(site):
    _, call = site
    staff = login(call, 'floor')[2]
    admin = login(call, 'boss')[2]
    for path, body in (('/api/users', None), ('/api/tokens', None), ('/api/connections', None),
                       ('/api/users', {'username': 'x2', 'display_name': 'X'}), ('/api/tokens', {'name': 'phone'})):
        assert call(path, body, cookie=staff)[0] == 403, path
    assert call('/api/users', cookie=admin)[0] == 200
    assert call('/api/tokens', cookie=admin)[0] == 200


def test_lockout_after_five_wrong_tries_from_one_address(site):
    _, call = site
    for _ in range(5):
        assert login(call, 'floor', 'wrong', ip='198.51.100.7')[0] != 200
    st, body, _ = login(call, 'floor', ip='198.51.100.7')            # even the right password waits now
    assert st != 200 and 'Too many tries' in body['error']
    assert login(call, 'floor', ip='198.51.100.8')[0] == 200         # a stranger elsewhere can't lock the user out


def test_lockout_per_address_across_usernames(site):
    _, call = site
    for i in range(20):
        login(call, f'guess{i}', 'wrong', ip='198.51.100.9')
    st, body, _ = login(call, 'boss', ip='198.51.100.9')
    assert st != 200 and 'Too many tries' in body['error']


def test_lockout_counts_the_proxys_hop_not_what_the_client_typed(site):
    """A proxy that appends to X-Forwarded-For keeps the client's own value in front. Rotating that value must not
    reset the count (black hat 11-17: the first value was used)."""
    _, call = site
    for i in range(5):
        login(call, 'floor', 'wrong', ip=f'10.0.0.{i}, 198.51.100.20')
    st, body, _ = login(call, 'floor', ip='10.0.0.99, 198.51.100.20')
    assert st != 200 and 'Too many tries' in body['error']


def test_expired_session_is_refused(site):
    server, call = site
    cookie = login(call, 'floor')[2]
    assert call('/api/auth/me', cookie=cookie)[0] == 200
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
    server.auth._write('UPDATE sessions SET expires_at = ?', (past,))
    assert call('/api/auth/me', cookie=cookie)[0] == 401


def test_deactivated_user_is_signed_out(site):
    server, call = site
    cookie = login(call, 'floor')[2]
    uid = {v: k for k, v in server.auth.user_names().items()}['Floor']
    server.auth.set_active(uid, False)
    assert call('/api/auth/me', cookie=cookie)[0] == 401
    assert login(call, 'floor')[0] != 200


def test_logout_ends_the_session(site):
    _, call = site
    cookie = login(call, 'floor')[2]
    assert call('/api/auth/logout', {}, cookie=cookie)[0] == 200
    assert call('/api/auth/me', cookie=cookie)[0] == 401


def test_device_token_works_only_under_inbound(site):
    server, call = site
    uid = {v: k for k, v in server.auth.user_names().items()}['Boss']
    token = server.auth.create_token(uid, 'house computer')
    bearer = {'Authorization': f'Bearer {token}'}
    assert call('/api/inbound/known', headers=bearer)[0] == 200
    for path in ('/api/products', '/api/users', '/api/connections', '/api/auth/me'):
        assert call(path, headers=bearer)[0] == 401, path
    tid = server.auth.list_tokens()[0]['id']
    server.auth.revoke_token(tid)
    assert call('/api/inbound/known', headers=bearer)[0] == 401
    assert call('/api/inbound/known', headers={'Authorization': 'Bearer ss_made-up'})[0] == 401


def test_post_without_the_app_header_is_refused(site):
    _, call = site
    cookie = login(call, 'boss')[2]
    st, body, _ = call('/api/print-jobs', {'labels': []}, cookie=cookie, csrf=False)
    assert st == 403 and body['error'] == 'Missing request header'
