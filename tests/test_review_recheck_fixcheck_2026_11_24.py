import os, sys
REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

import approval as approval_mod  # noqa: E402
from test_amazon_approval import ANSWERS, _boxes, srv  # noqa: E402,F401


def _gated(server, uid, n, prefix='B0GATE'):
    a, e = _boxes(server, uid)
    out = []
    for i in range(n):
        asin = f'{prefix}{i:04d}'
        ANSWERS[asin] = ANSWERS['B0CLOSED01']
        pid = server.wms.save_product({'sku': f'G{i}', 'title': f'Gated {i}', 'identifiers': [{'kind': 'ASIN', 'value': asin}]})['id']
        server.wms.receive(uid, pid, 1, 100, {'box_id': a['id']})
        out.append(asin)
    server.approval.refresh()
    return out


def test_c11_failing_asins_starve_the_one_left_out(srv, monkeypatch):
    """Check all past the cap: ASINs Amazon never answers keep their old checked_at, so they sort first on every
    press and the one ASIN left out (a newer answer) is never asked, though `left` says press again."""
    monkeypatch.setattr(approval_mod, 'PACE', 0)
    monkeypatch.setattr(approval_mod.time, 'sleep', lambda s: None)
    monkeypatch.setattr(approval_mod, 'RECHECK_MAX', 3)
    server, call, calls, uid = srv
    asins = _gated(server, uid, 4)
    with server.wms._tx() as c:
        c.execute("UPDATE amazon_approval SET checked_at = '2026-11-20T00:00:00Z' WHERE asin != ?", (asins[3],))
        c.execute("UPDATE amazon_approval SET checked_at = '2026-11-23T00:00:00Z' WHERE asin = ?", (asins[3],))
    real = approval_mod._http

    def flaky(method, url, *a, **k):
        asin = url.split('asin=')[1].split('&')[0]
        if asin != asins[3]:
            calls.append({'asin': asin})
            return 503, {'errors': [{'message': 'busy'}]}
        return real(method, url, *a, **k)
    monkeypatch.setattr(approval_mod, '_http', flaky)
    seen = set()
    for _ in range(3):
        calls.clear()
        st, r = call('/api/amazon/approval/recheck', {})
        assert st == 200 and r['left'] == 1
        seen |= {c['asin'] for c in calls}
    assert asins[3] in seen, f'{asins[3]} never asked in 3 presses; asked only {sorted(seen)}'
