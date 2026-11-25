import os, sys
REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

import pytest  # noqa: E402
from test_sell_sku import setup, item, listing  # noqa: E402
from test_amazon_approval import srv, _boxes, _product, approval_mod  # noqa: E402,F401


def test_c17_lower_item_pushed_to_2_by_listing_and_higher_item_also_gets_2(tmp_path):
    """a (lower id) and b bought the same day at the same price. A listing of another ASIN holds the base SKU, so a
    moves to -2. b counts a as 'lower' and also starts at -2: both items show the same SKU."""
    w, li, box = setup(tmp_path)
    a = item(w, 'A', 'B0AAAAAAA1')
    b = item(w, 'B', 'B0BBBBBBB1')
    for p in (a, b):
        w.receive(1, p, 1, 3999, box, purchased_at='2025-08-01')
    listing(w, '8.1.25-39.99', 'B0ZZZZZZZ1')
    sa, sb = li.sell_sku(a)['sku'], li.sell_sku(b)['sku']
    assert sa != sb, (sa, sb)


def test_c17b_higher_items_own_listing_holds_base_both_get_2(tmp_path):
    """b's own Amazon listing already uses the base SKU. a (lower id) sees another ASIN's listing on the base and moves
    to -2; b always starts at -2 because a is lower: both show -2, and b's SKU is not the one its listing uses."""
    w, li, box = setup(tmp_path)
    a = item(w, 'A', 'B0AAAAAAA1')
    b = item(w, 'B', 'B0BBBBBBB1')
    for p in (a, b):
        w.receive(1, p, 1, 3999, box, purchased_at='2025-08-01')
    listing(w, '8.1.25-39.99', 'B0BBBBBBB1')
    sa, sb = li.sell_sku(a)['sku'], li.sell_sku(b)['sku']
    assert sa != sb, (sa, sb)


def test_empty_run_id_that_dies_midway_stays_running_forever(srv, monkeypatch):
    """Fix regression: the 'died midway' cleanup now looks the run up in recheck_runs, where an empty run id is never
    kept. A check sent with no run id (an old open tab) that fails midway leaves the newest state running: True."""
    monkeypatch.setattr(approval_mod, 'PACE', 0)
    server, call, calls, uid = srv
    a, e = _boxes(server, uid)
    server.wms.receive(uid, _product(server, 'BRANDED', 'B0BRANDED1', '012345678905'), 1, 100, {'box_id': a['id']})
    server.approval.refresh()

    def boom(*a, **k):
        raise RuntimeError('network died')
    monkeypatch.setattr(server.approval, 'ask', boom)
    with pytest.raises(RuntimeError):
        server.approval.recheck(['B0BRANDED1'], '')
    st, s = call('/api/amazon/approval/recheck')
    assert s['running'] is False, s
