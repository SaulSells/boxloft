"""Alex 11-24: "a one click sku I can copy and paste into seller central ... the date it was purchased - price paid,
10.12.26-10.15". Listings.sell_sku picks the newest dated, priced buy and keeps the SKU off another ASIN's listing."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from commerce import Commerce  # noqa: E402
from listings import Listings, sku_text  # noqa: E402
from wms import Wms  # noqa: E402


def setup(tmp_path):
    w = Wms(str(tmp_path / 'w.db'))
    li = Listings(w, Commerce(w))
    box = w.create_boxes(1, 1)[0]
    return w, li, {'box_id': box['id']}


def item(w, sku, asin):
    return w.save_product({'sku': sku, 'title': f'Item {sku}', 'identifiers': [{'kind': 'ASIN', 'value': asin}]})['id']


def listing(w, sku, asin):
    with w._tx() as c:
        c.execute("""INSERT INTO listing (channel, sku, asin, title, status, qty, first_seen, seen_at)
                     VALUES ('amazon', ?, ?, 'x', 'Active', 1, 'x', 'x')""", (sku, asin))


def test_sku_text_is_month_day_year_then_price():
    assert sku_text('2026-10-12', 1015) == '10.12.26-10.15'
    assert sku_text('2026-11-05T14:02:00Z', 2806) == '11.5.26-28.06'   # Alex's own: Amazon-11.5.26-28.06
    assert sku_text('2025-08-01', 3999) == '8.1.25-39.99'
    assert sku_text('2026-01-02', 7) == '1.2.26-0.07'


def test_newest_dated_priced_buy_wins_and_cost_not_entered_is_skipped(tmp_path):
    w, li, box = setup(tmp_path)
    p = item(w, 'A', 'B0AAAAAAA1')
    assert li.sell_sku(p)["estimated"]   # no buy at all
    w.receive(1, p, 2, 1015, box, reference='112-1', purchased_at='2026-10-12')
    w.receive(1, p, 1, 0, box, purchased_at='2026-11-01')   # cost 0: not a price paid
    s = li.sell_sku(p)
    assert (s['sku'], s['day'], s['cost_cents'], s['from']) == ('10.12.26-10.15', '2026-10-12', 1015, '112-1')
    w.receive(1, p, 1, 1199, box, reference='112-2', purchased_at='2026-11-03')
    assert li.sell_sku(p)['sku'] == '11.3.26-11.99'
    last = w._q1('SELECT action_id FROM receipt ORDER BY id DESC LIMIT 1')['action_id']
    w.undo(1, last)   # an undone receipt is no buy
    assert li.sell_sku(p)['sku'] == '10.12.26-10.15'
    q = item(w, 'B', 'B0BBBBBBB1')
    w.receive(1, q, 1, 500, box)   # a price with no purchase date: an estimated SKU from that price
    s = li.sell_sku(q)
    assert s["estimated"] and s["sku"].endswith(f"-5.00-S{q}") and s["cost_cents"] == 500, s


def test_two_items_same_day_and_price_and_another_asins_listing_get_distinct_skus(tmp_path):
    w, li, box = setup(tmp_path)
    a = item(w, 'A', 'B0AAAAAAA1')
    b = item(w, 'B', 'B0BBBBBBB1')
    for p in (a, b):
        w.receive(1, p, 1, 3999, box, purchased_at='2025-08-01')
    assert (li.sell_sku(a)['sku'], li.sell_sku(b)['sku']) == ('8.1.25-39.99', '8.1.25-39.99-2')
    listing(w, '8.1.25-39.99', 'B0AAAAAAA1')   # a's own listing: a keeps it
    assert li.sell_sku(a)['sku'] == '8.1.25-39.99'
    listing(w, '8.1.25-39.99-2', 'B0ZZZZZZZ1')   # another ASIN holds b's: b moves on
    assert li.sell_sku(b)['sku'] == '8.1.25-39.99-3'


def test_first_copy_saves_the_sku_for_good(tmp_path):
    """Alex 11-24 + review round 3: a SKU worked out fresh moved when buys were relinked, approved or hidden. The
    first copy saves it: it never changes after, and no other item can take it."""
    import sqlite3
    import pytest
    w, li, box = setup(tmp_path)
    a = item(w, 'A', 'B0AAAAAAA1')
    b = item(w, 'B', 'B0BBBBBBB1')
    w.receive(1, b, 1, 3999, box, purchased_at='2025-08-01')
    s = li.save_sell_sku(b, 1)
    assert (s['sku'], s['saved']) == ('8.1.25-39.99', True)
    assert li.save_sell_sku(b, 1)['sku'] == '8.1.25-39.99'   # copying again keeps it
    # An older buy of a at the same day and price shows up later (a relinked line): a waits behind b's saved SKU.
    with w._tx() as c:
        po = c.execute("""INSERT INTO purchase_order (source, external_id, ordered_at, imported_at)
                          VALUES ('amazon', '112-OLD', '2025-08-01', '2025-08-01T00:00:00Z')""").lastrowid
        c.execute("""INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents, product_id)
                     VALUES (?, 'L1', 'B0AAAAAAA1', 'x', 1, 3999, ?)""", (po, a))
    assert li.sell_sku(b)['sku'] == '8.1.25-39.99' and li.sell_sku(a)['sku'] == '8.1.25-39.99-2'
    assert li.sell_sku(a)['saved'] is False
    with w._tx() as c:   # Not inventory on a's order takes a out; b's saved SKU stays
        c.execute("UPDATE purchase_order SET hidden = 1 WHERE id = ?", (po,))
    assert li.sell_sku(a)["estimated"] and li.sell_sku(b)['sku'] == '8.1.25-39.99'
    with pytest.raises(sqlite3.DatabaseError):   # append-only
        with w._tx() as c:
            c.execute("UPDATE sell_sku SET sku = 'x'")


def test_a_store_order_waiting_for_approval_gives_no_sku(tmp_path):
    w, li, box = setup(tmp_path)
    a = item(w, 'A', 'B0AAAAAAA1')
    with w._tx() as c:
        po = c.execute("""INSERT INTO purchase_order (source, external_id, ordered_at, imported_at, review)
                          VALUES ('walmart.com', 'W-1', '2025-08-01', 'x', 'check the items')""").lastrowid
        c.execute("""INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents, product_id)
                     VALUES (?, 'L1', 'B0AAAAAAA1', 'x', 1, 3999, ?)""", (po, a))
    assert li.sell_sku(a)["estimated"]
    with w._tx() as c:
        c.execute("UPDATE purchase_order SET review = '' WHERE id = ?", (po,))   # approved on Arriving
    assert li.sell_sku(a)['sku'] == '8.1.25-39.99'


def test_every_item_gets_a_sku_made_from_what_there_is(tmp_path):
    """Alex 11-25: "if we don't have data we use what we can. If we have none then we need to create a sku,
    regardless always have a copy button." No dated, priced buy: M.D.YY[-price]-S<item id>."""
    import wms as wms_mod
    w, li, box = setup(tmp_path)
    a = item(w, 'A', 'B0AAAAAAA1')
    w._conn().execute("UPDATE product SET created_at = '2026-11-03T02:00:00Z' WHERE id = ?", (a,))   # 11-02 in ET
    assert li.sell_sku(a)['sku'] == f'11.2.26-S{a}'                       # no buy, no price: the day it was made
    w.save_product({'sku': 'A', 'title': 'Item A', 'default_cost_cents': 1250,
                    'identifiers': [{'kind': 'ASIN', 'value': 'B0AAAAAAA1'}]}, a, 1)
    assert li.sell_sku(a)['sku'] == f'11.2.26-12.50-S{a}'                 # + its Usual unit cost
    real = wms_mod.now_utc
    wms_mod.now_utc = lambda: '2026-11-10T15:00:00Z'
    try:
        w.receive(1, a, 1, None, box)                                    # came in 11-10, cost not entered
    finally:
        wms_mod.now_utc = real
    s = li.sell_sku(a)
    assert (s['sku'], s['estimated'], s['saved']) == (f'11.10.26-12.50-S{a}', True, False), s
    assert 'came in' in s['from'] and 'usual unit cost' in s['from']
    listing(w, f'11.10.26-12.50-S{a}', 'B0ZZZZZZZ1')                     # another ASIN's listing holds it
    assert li.sell_sku(a)['sku'] == f'11.10.26-12.50-S{a}-2'
    saved = li.save_sell_sku(a, 1)
    assert saved['saved'] and saved['sku'] == f'11.10.26-12.50-S{a}-2'
    w.save_product({'sku': 'A', 'title': 'Item A', 'default_cost_cents': 1300,
                    'identifiers': [{'kind': 'ASIN', 'value': 'B0AAAAAAA1'}]}, a, 1)
    assert li.sell_sku(a)['sku'] == saved['sku']                          # copied: it stays while there is no buy
    w.receive(1, a, 1, 1999, box, purchased_at='2026-11-12')              # a real dated, priced buy: its own SKU
    s = li.sell_sku(a)
    assert (s['sku'], s['estimated']) == ('11.12.26-19.99', False), s
