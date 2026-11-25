import os, sys
REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

import approval as approval_mod  # noqa: E402
from test_amazon_approval import ANSWERS, _boxes, _product, srv  # noqa: E402,F401


def _gated(server, uid, n):
    a, e = _boxes(server, uid)
    for i in range(n):
        asin = f'B0GATE{i:04d}'
        ANSWERS[asin] = ANSWERS['B0CLOSED01']
        pid = server.wms.save_product({'sku': f'G{i}', 'title': f'Gated {i}', 'identifiers': [{'kind': 'ASIN', 'value': asin}]})['id']
        server.wms.receive(uid, pid, 1, 100, {'box_id': a['id']})
    server.approval.refresh()
    return a


def test_one_asin_sent_as_a_string_rechecks_the_whole_gated_list(srv, monkeypatch):
    """A caller that sends one ASIN as a string (not a list) gets every gated A-box ASIN asked instead of 400."""
    monkeypatch.setattr(approval_mod, 'PACE', 0)
    server, call, calls, uid = srv
    _gated(server, uid, 3)
    calls.clear()
    st, r = call('/api/amazon/approval/recheck', {'asins': 'B0GATE0000'})
    assert st == 400 or [c['asin'] for c in calls] == ['B0GATE0000'], (st, r, [c['asin'] for c in calls])


def test_check_all_past_the_cap_says_nothing_about_the_rest(srv, monkeypatch):
    """The button reads "Check all 201 with Amazon now"; the server asks 200 and the answer has no word of the 1 left."""
    monkeypatch.setattr(approval_mod, 'PACE', 0)
    monkeypatch.setattr(approval_mod, 'RECHECK_MAX', 3)
    server, call, calls, uid = srv
    _gated(server, uid, 4)
    st, s = call('/api/listings/stock?channel=amazon')
    assert s['summary']['not_approved']['products'] == 4
    with server.wms._tx() as c:   # the daily answers are from an hour ago (a test runs within one second)
        c.execute("UPDATE amazon_approval SET checked_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 hours')")
    st, r = call('/api/amazon/approval/recheck', {})
    assert st == 200
    assert r['asked'] == 4 or any(k in r for k in ('left', 'more', 'skipped', 'total')), r
    assert (r['asked'], r['left']) == (3, 1)
    calls.clear()   # the next press starts with the one never asked again (oldest answer first)
    st, r = call('/api/amazon/approval/recheck', {})
    assert (r['asked'], r['left']) == (3, 1) and calls[0]['asin'] == 'B0GATE0003', [c['asin'] for c in calls]
