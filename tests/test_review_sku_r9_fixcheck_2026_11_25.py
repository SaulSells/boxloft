import os, sys
REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))
from test_amazon_approval import srv  # noqa: E402,F401
from test_sell_sku import setup, item  # noqa: E402

BIG = '9' * 20


def _box(server):
    box = server.wms.create_boxes(1, 1, name='A31')[0]
    return box['id'] if isinstance(box, dict) else box


def test_put_extra_cost_cents_string_is_a_500(srv):  # noqa: F811
    """The fix says 'a string a route reads as a number goes through _int', but sorting.put reads the 'more stock'
    option's cost_cents with plain int(): a 20-digit string is past 64 bits, the receipt insert raises OverflowError
    -> 500 'Server error' instead of a 400 with words (same class as the last run's qty/replaces proof)."""
    server, call, calls, uid = srv
    bid = _box(server)
    pid = server.wms.save_product({'sku': 'X1', 'title': 'Extra item'})['id']
    st, r = call('/api/put', {'box_id': bid, 'scan_id': 'c-1', 'qty': 1, 'product_id': pid,
                              'option': {'kind': 'new', 'product_id': pid, 'cost_cents': BIG}})
    assert st != 500, (st, r)


def test_estimate_price_prefers_usual_cost_fill_over_a_real_priced_buy_line(tmp_path):
    """c11: a receipt saved with no cost and then filled by the Usual unit cost (_fill_costs sets cost_missing = 0) is
    read by _sku_estimate as 'a buy' and ranked before a real priced buy line (src 1 before src 2). Item: buy line at
    $39.99 on an order with no date (so no real SKU), one unit received with no cost, Usual unit cost $12.50 -> the
    estimate shows -12.50- labelled 'price: a buy', not the $39.99 paid; the first Copy saves it for good."""
    w, li, box = setup(tmp_path)
    a = item(w, 'A', 'B0AAAAAAA1')
    with w._tx() as c:
        po = c.execute("""INSERT INTO purchase_order (source, external_id, ordered_at, imported_at, review)
                          VALUES ('walmart.com', 'W-9', '', 'x', '')""").lastrowid
        c.execute("""INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents, product_id)
                     VALUES (?, 'L1', 'B0AAAAAAA1', 'x', 1, 3999, ?)""", (po, a))
    w.receive(1, a, 1, None, box)   # came in, cost not entered
    w.save_product({'sku': 'A', 'title': 'Item A', 'default_cost_cents': 1250,
                    'identifiers': [{'kind': 'ASIN', 'value': 'B0AAAAAAA1'}]}, a, 1)   # fills the receipt
    s = li.sell_sku(a)
    assert s['estimated'], s
    assert s['cost_cents'] == 3999, s
