import os, sys
REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

import pytest  # noqa: E402
from test_sell_sku import setup, item, listing  # noqa: E402
import approval as approval_mod  # noqa: E402


def _po(w, pid, ext, day, cents, state='ordered', hidden=0):
    with w._tx() as c:
        po = c.execute("INSERT INTO purchase_order (source, external_id, ordered_at, imported_at, state, hidden) "
                       "VALUES ('amazon', ?, ?, 'x', ?, ?)", (ext, day, state, hidden)).lastrowid
        c.execute("INSERT INTO purchase_line (po_id, line_key, product_id, title, qty, unit_cost_cents) "
                  "VALUES (?, ?, ?, 'x', 1, ?)", (po, ext, pid, cents))


def test_lower_item_pushed_by_listing_and_next_item_get_the_same_sku(tmp_path):
    """a and b share day and price. Another ASIN's listing holds the base, so a moves to -2; b counts 1 lower item
    and also takes -2: two items show one SKU (Amazon refuses a SKU on two ASINs)."""
    w, li, box = setup(tmp_path)
    a = item(w, 'A', 'B0AAAAAAA1')
    b = item(w, 'B', 'B0BBBBBBB1')
    for p in (a, b):
        w.receive(1, p, 1, 3999, box, purchased_at='2025-08-01')
    listing(w, '8.1.25-39.99', 'B0OTHER001')
    sa, sb = li.sell_sku(a)['sku'], li.sell_sku(b)['sku']
    assert sa != sb, (sa, sb)


def test_cancelled_order_buy_line_gives_no_sku(tmp_path):
    w, li, box = setup(tmp_path)
    p = item(w, 'A', 'B0AAAAAAA1')
    _po(w, p, '112-C', '2026-10-12', 1015, state='cancelled')
    assert li.sell_sku(p)["estimated"]


def test_hidden_not_inventory_order_gives_no_sku(tmp_path):
    w, li, box = setup(tmp_path)
    p = item(w, 'A', 'B0AAAAAAA1')
    _po(w, p, '112-H', '2026-10-12', 1015, hidden=1)
    assert li.sell_sku(p)["estimated"]


def test_recheck_without_run_id_that_dies_never_stops_running(tmp_path, monkeypatch):
    """An old tab posts with no run id; Amazon's ask raises midway. The newest state stays running forever."""
    w, li, box = setup(tmp_path)
    ap = approval_mod.Approval(w, None)
    monkeypatch.setattr(ap, 'ready', lambda: True)

    def boom(*a, **k):
        raise RuntimeError('network')
    monkeypatch.setattr(ap, 'ask', boom)
    with pytest.raises(RuntimeError):
        ap.recheck(['B0AAAAAAA1'], '')
    st = ap.run_state('')
    assert st['running'] is False, st


def test_second_items_sku_changes_when_the_first_gets_a_newer_buy(tmp_path):
    """b showed -2 (and was pasted into Seller Central); a later buy of a makes b show the base text instead."""
    w, li, box = setup(tmp_path)
    a = item(w, 'A', 'B0AAAAAAA1')
    b = item(w, 'B', 'B0BBBBBBB1')
    for p in (a, b):
        w.receive(1, p, 1, 3999, box, purchased_at='2025-08-01')
    before = li.sell_sku(b)['sku']
    assert before == '8.1.25-39.99-2'
    w.receive(1, a, 1, 1234, box, purchased_at='2025-09-01')
    assert li.sell_sku(b)['sku'] == before
