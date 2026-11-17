"""Alex 11-12: the home-screen web app sends what the phone does at the top of the screen (tapProbe.ts). The route
keeps one JSON line per report with who sent it, never lets a client overwrite who or when, and stops at 1 MB."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from test_cover_all import srv  # noqa: E402,F401  (fixture)


def test_report_is_kept_with_who_sent_it(srv):  # noqa: F811
    server, call = srv
    st, out = call('Sam', '/api/client-diag', {'kind': 'taps', 'user': 'alex', 'safe_top': 0,
                                               'events': [{'type': 'touchstart', 'x': 20, 'y': 30, 'hit': 'header'}]})
    assert st == 200 and out == {'ok': True}
    with open(os.path.join(server.DATA, 'client-diag.jsonl')) as f:
        rows = [json.loads(x) for x in f]
    assert len(rows) == 1
    assert rows[0]['user'] == 'sam' and rows[0]['events'][0]['hit'] == 'header' and rows[0]['safe_top'] == 0


def test_signed_out_is_refused_and_full_file_stops(srv):  # noqa: F811
    server, call = srv
    import urllib.error
    import urllib.request
    req = urllib.request.Request(f'http://127.0.0.1:{server._test_port}/api/client-diag', method='POST', data=b'{}',
                                 headers={'X-Boxloft': '1', 'Content-Type': 'application/json'})
    try:
        urllib.request.urlopen(req)
        assert False, 'signed-out report was taken'
    except urllib.error.HTTPError as e:
        assert e.code == 401
    path = os.path.join(server.DATA, 'client-diag.jsonl')
    with open(path, 'w') as f:
        f.write('x' * server.CLIENT_DIAG_MAX)
    st, out = call('Alex', '/api/client-diag', {'kind': 'load'})
    assert st == 200 and out['full'] is True
    assert os.path.getsize(path) == server.CLIENT_DIAG_MAX
