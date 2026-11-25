import os, sys
REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))
import http.client  # noqa: E402
from test_cover_all import srv  # noqa: E402,F401
from test_labels import order, package, USPS  # noqa: E402

# USPS (and buyer-return 9322...) tracking IDs are 22 digits, all digits: past too_big's 18-digit id rule.


def _usps_box(server):
    w = server.wms
    po, (lego,) = order(w, '112-USPS', [('LEGO Bonsai Tree', 'B0LEGO0001', 2)])
    package(w, po, 'S2', USPS, 'USPS', [(lego, 2)])
    box = w.create_boxes(1, 1)[0]
    return w, box, lego


def test_label_put_from_a_usps_card_is_refused_as_too_big(srv):  # noqa: F811
    """Box page: scan a USPS label, tap 'Put 2 in A1' -> the web posts tracking_id = the 22-digit USPS ID ->
    400 'One of the numbers sent is too big', nothing put away. Every USPS package's card is dead."""
    server, call = srv
    w, box, lego = _usps_box(server)
    st, r = call('Sam', '/api/put', {'box_id': box['id'], 'scan_id': 'u1', 'code': USPS})
    assert st == 200 and r['kind'] == 'label', r
    st, a = call('Sam', '/api/labels/put', {'box_id': box['id'], 'scan_id': 'u2', 'line_id': lego, 'qty': 2,
                                            'tracking_id': USPS})
    assert st == 200, (st, a)


def test_label_card_reload_for_a_usps_package_is_refused_as_too_big(srv):  # noqa: F811
    """BoxAdd reloads the card with GET /api/labels/card?tracking_id=<USPS> after each put: too_big(query) -> 400."""
    server, call = srv
    _usps_box(server)
    st, c = call('Sam', f'/api/labels/card?tracking_id={USPS}')
    assert st == 200 and c.get('kind') == 'label', (st, c)


def test_label_pick_with_a_usps_tracking_id_is_refused_as_too_big(srv):  # noqa: F811
    """LabelPage / BoxAdd 'pick' when two IDs tie posts {code, tracking_id}: a USPS pick is a 400 'too big'."""
    server, call = srv
    _usps_box(server)
    st, r = call('Sam', '/api/labels/pick', {'code': USPS, 'tracking_id': USPS})
    assert 'too big' not in str((r or {}).get('error', '')), (st, r)


def test_deeply_nested_json_body_is_a_500(srv):  # noqa: F811
    """_json_peek swallows RecursionError and returns None, then the route's json_body() hits RecursionError
    (not JSONDecodeError) -> 'Server error' 500 instead of 400 'Bad JSON'."""
    server, call = srv
    body = b'{"a":' + b'[' * 200000 + b']' * 200000 + b'}'
    conn = http.client.HTTPConnection('127.0.0.1', server._test_port, timeout=30)
    conn.request('POST', '/api/boxes', body=body, headers={'Cookie': server._test_cookie, 'X-Boxloft': '1',
                                                           'Content-Type': 'application/json'})
    r = conn.getresponse()
    r.read()
    assert r.status == 400, r.status
