import os, sys
REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

import approval as approval_mod  # noqa: E402
from test_amazon_approval import _boxes, _product, srv  # noqa: E402,F401


def _gated(server, uid, monkeypatch):
    monkeypatch.setattr(approval_mod, 'PACE', 0)
    a, e = _boxes(server, uid)
    for sku, asin, upc in (('BRANDED', 'B0BRANDED1', '012345678905'), ('CLOSED', 'B0CLOSED01', '012345678912')):
        server.wms.receive(uid, _product(server, sku, asin, upc), 1, 100, {'box_id': a['id']})
    server.approval.refresh()


def test_c11_string_asins_rechecks_whole_gated_list(srv, monkeypatch):
    """c11: {asins: 'B0CLOSED01'} (a string) must ask only that ASIN or be refused, not the whole gated list."""
    server, call, calls, uid = srv
    _gated(server, uid, monkeypatch)
    calls.clear()
    st, r = call('/api/amazon/approval/recheck', {'asins': 'B0CLOSED01'})
    assert st == 400 or [c['asin'] for c in calls] == ['B0CLOSED01'], (st, r, [c['asin'] for c in calls])
