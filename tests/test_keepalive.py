"""11-11: Caddy reuses connections to Boxloft. One handler object serves every request on a connection, so a
request must never see the previous request's body, and an unread body must not spoil the next request."""
import http.client
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from test_cover_all import srv  # noqa: E402,F401  (fixture)


def test_each_save_on_one_connection_gets_its_own_body(srv):  # noqa: F811
    server, call = srv
    port, cookie = server._test_port, server._test_cookie
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    h = {'Cookie': cookie, 'X-Boxloft': '1', 'Content-Type': 'application/json'}
    titles = []
    for big in ('FIRST', 'SECOND'):
        c.request('POST', '/api/print-jobs', json.dumps({'labels': [{'kind': 'box', 'code': 'SB00000018', 'big': big}]}), h)
        titles.append(json.loads(c.getresponse().read())['title'])
    assert titles == ['FIRST', 'SECOND']


def test_unread_body_does_not_spoil_the_next_request(srv):  # noqa: F811
    server, call = srv
    port, cookie = server._test_port, server._test_cookie
    h = {'Cookie': cookie, 'X-Boxloft': '1', 'Content-Type': 'application/json'}
    for path, hd in (('/api/nope', h), ('/api/print-jobs', {k: v for k, v in h.items() if k != 'Cookie'}),
                     ('/api/print-jobs', {k: v for k, v in h.items() if k != 'X-Boxloft'})):
        c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        c.request('POST', path, json.dumps({'labels': 'x' * 50}), hd)
        assert c.getresponse().read()
        c.request('GET', '/api/health', headers=h)
        r = c.getresponse()
        assert r.status == 200 and json.loads(r.read()) == {'ok': True}


def test_slow_upload_does_not_hold_other_saves(srv):  # noqa: F811
    """Red line 11-11: a phone photo on weak signal sent its bytes inside the save lock, so every other save
    waited (503 after 30 s). The body is now read before the lock."""
    import socket
    import time
    server, call = srv
    port, cookie = server._test_port, server._test_cookie
    s = socket.create_connection(('127.0.0.1', port), timeout=10)
    s.sendall((f'POST /api/photos HTTP/1.1\r\nHost: x\r\nCookie: {cookie}\r\nX-Boxloft: 1\r\n'
               'Content-Type: image/jpeg\r\nContent-Length: 500000\r\n\r\n').encode() + b'\xff\xd8' + b'0' * 8)
    time.sleep(0.3)   # the upload is now stuck half way
    t = time.time()
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    c.request('POST', '/api/print-jobs', json.dumps({'labels': [{'kind': 'box', 'code': 'SB00000018', 'big': 'X'}]}),
              {'Cookie': cookie, 'X-Boxloft': '1', 'Content-Type': 'application/json'})
    r = c.getresponse()
    assert r.status == 200 and time.time() - t < 3
    s.close()
