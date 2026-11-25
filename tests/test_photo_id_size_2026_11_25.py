"""11-25 (Boxloft persona review): the pre-route number check read every POST body at the 2 MB JSON limit, so a photo
over 2 MB on /api/photo-id (an unlocked route that reads up to MAX_PHOTO) got 413 before the route ran."""
import json
import os
import sys
import urllib.error
import urllib.request

REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))
from test_amazon_approval import srv  # noqa: E402,F401


def test_a_photo_over_2_mb_reaches_photo_id(srv, monkeypatch):
    server, call, calls, uid = srv
    seen = {}
    monkeypatch.setattr(server.photo_reader, 'identify', lambda body, user, prov: seen.setdefault('n', len(body)) and {'ok': 1})
    big = b'\xff\xd8' + b'x' * (3 * 1024 * 1024)
    st, r = _post_raw(call, '/api/photo-id', big, 'image/jpeg')
    assert st == 200 and seen['n'] == len(big), (st, r)


def _post_raw(call, path, data, ctype):
    """The srv fixture's call() sends JSON; this sends raw bytes with the same cookie and header."""
    cells = {c.cell_contents if not callable(c.cell_contents) else None for c in call.__closure__}
    base = next(x for x in cells if isinstance(x, str) and x.startswith('http'))
    cookie = next(x for x in cells if isinstance(x, str) and 'boxloft_session' in x)
    req = urllib.request.Request(base + path, method='POST', data=data,
                                 headers={'Cookie': cookie, 'X-Boxloft': '1', 'Content-Type': ctype})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b'null')
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b'null')


def test_asin_checks_refuse_a_trailing_newline():
    """Boxloft persona review 11-25: '$' matched before a trailing newline, so 'B012345678\\n' passed as an ASIN and went
    into the SP-API URL on Check again."""
    import approval
    import listings
    import sorting
    for rx in (approval.ASIN_RE, listings.ASIN_RE, sorting.ASIN_RE):
        assert rx.match('B012345678') and not rx.match('B012345678\n')
