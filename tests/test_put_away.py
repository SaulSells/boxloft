"""Put away (flow audit 11-12, redesign 1): one scan = one save, the server picks the record with one rule.

Before: Receive took 4 to 6 steps (scan, fix the order or quantity, scan box, scroll, Receive) and the box page 2 per
item (scan, Add). Now a scan with a box set saves 1 unit at once, and asks only when 2 answers are both likely.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from test_sorting import UNLISTED_UPC, UPC, setup, units_in  # noqa: E402
from wms import WmsError  # noqa: E402


def order(w, ext, qty, ordered='2026-11-01', delivered=None, asin='B0UNLISTED', cost=333):
    with w._tx() as c:
        po = c.execute("""INSERT INTO purchase_order (source, external_id, ordered_at, imported_at, state, delivered_on, last_seen_at)
                          VALUES ('amazon', ?, ?, 'x', ?, ?, ?)""",
                       (ext, ordered, 'delivered' if delivered else 'shipped', delivered, '2026-11-12')).lastrowid
        return c.execute("INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents) VALUES (?, ?, ?, 'Kleenex', ?, ?)",
                         (po, ext, asin, qty, cost)).lastrowid


def kleenex_code(w):
    with w._tx() as c:
        c.execute("INSERT OR REPLACE INTO catalog_cache (code, fetched_at, data) VALUES (?, 'x', '[{\"asin\": \"B0UNLISTED\"}]')", (UNLISTED_UPC,))


def test_three_scans_are_three_saves_with_no_taps(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path, expected=3)
    out = [so.put(boxes['A1'], 1, scan_id=f's{i}', code=UPC) for i in range(3)]
    assert [r['kind'] for r in out] == ['added'] * 3
    assert [r['how'] for r in out] == ['audit'] * 3 and out[-1]['here'] == 3
    assert out[0]['said'] == 'Stock audit: 2 still to find' and out[-1]['said'] == 'Stock audit: all found'
    assert w.check_integrity()['ok']


def test_a_resent_scan_is_saved_once(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path, expected=3)
    a = so.put(boxes['A1'], 1, scan_id='same', code=UPC)
    b = so.put(boxes['A1'], 1, scan_id='same', code=UPC)
    assert b['repeat'] and a['add_id'] == b['add_id'] and units_in(w, boxes['A1']) == 1


def test_an_item_on_an_order_goes_on_the_delivered_package_first(tmp_path, monkeypatch):
    import inbound as inbound_mod
    from datetime import date
    monkeypatch.setattr(inbound_mod, 'today_et', lambda: date(2026, 11, 12))
    import audit as audit_mod   # audit imports today_et by name: patch its copy too, or the test follows the real date
    monkeypatch.setattr(audit_mod, 'today_et', lambda: date(2026, 11, 12))
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    kleenex_code(w)
    old = order(w, '112-OLD', 12, ordered='2026-10-06')                       # still on the way per Amazon
    new = order(w, '112-NEW', 4, ordered='2026-10-23', delivered='2026-11-10')   # under 5 days: not on the audit yet
    r = so.put(boxes['A1'], 1, scan_id='k1', code=UNLISTED_UPC)
    assert (r['kind'], r['how'], r['qty']) == ('added', 'order', 1)
    assert w._q1('SELECT purchase_line_id FROM receipt')['purchase_line_id'] == new
    assert r['said'] == 'order of 10/23, delivered 11/10. 3 still to come'
    # A pile: the line becomes 6, spread over the orders in the same order (4 from the new one, 2 from the old).
    p = so.put_qty(r['add_id'], 6, 1)
    got = {x['purchase_line_id']: x['n'] for x in w._q(
        'SELECT r.purchase_line_id, SUM(r.qty) AS n FROM receipt r JOIN action a ON a.id = r.action_id '
        'WHERE a.reversed_by IS NULL AND a.reverses_action_id IS NULL GROUP BY 1')}
    assert got == {new: 4, old: 2} and units_in(w, boxes['A1']) == 6 and p['qty'] == 6
    big = so.put_qty(p['add_id'], 17, 1)   # 16 on the orders: the 17th passes what was bought, so it asks first
    assert big['kind'] == 'over' and big['question'] == 'That makes 17 of 16 bought. Put 11 more in?'
    assert big['detail'].endswith('16 go on its orders, 1 as extra.')
    assert units_in(w, boxes['A1']) == 6 and w.check_integrity()['ok']


def test_one_other_place_is_a_move_two_places_ask(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    p = w.save_product({'sku': 'MUG-1', 'title': 'Blue mug', 'default_cost_cents': 450, 'identifiers': [{'kind': 'UPC', 'value': '0712345678911'}]})
    w.receive(1, p['id'], 2, 450, boxes['A2'])
    r = so.put(boxes['A1'], 1, scan_id='m1', code='0712345678911')
    assert (r['how'], r['here']) == ('move', 1) and r['said'].startswith('moved from A2, 1 left there')
    w.receive(1, p['id'], 1, 450, boxes['A3'])
    r2 = so.put(boxes['A1'], 1, scan_id='m2', code='0712345678911')
    assert r2['kind'] == 'choose' and 'other places' in r2['question']
    assert units_in(w, boxes['A1']) == 1   # nothing saved while it asks


def test_nothing_expected_and_nowhere_else_is_extra_stock(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    p = w.save_product({'sku': 'MUG-2', 'title': 'Red mug', 'default_cost_cents': 300, 'identifiers': [{'kind': 'UPC', 'value': '0712345678928'}]})
    r = so.put(boxes['A1'], 1, scan_id='e1', code='0712345678928')
    assert (r['how'], r['said']) == ('new', '1 more than the orders show. Saved as extra')
    assert w._q1('SELECT unit_cost_cents FROM audit_extra')['unit_cost_cents'] == 300
    so.undo_add(r['add_id'], 1)
    assert units_in(w, boxes['A1']) == 0 and w.check_integrity()['ok']


def test_an_item_with_an_order_waiting_says_so(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    p = w.save_product({'sku': 'TEE-1', 'title': 'Calvin Klein tee', 'identifiers': [{'kind': 'UPC', 'value': '0712345678935'}]})
    with w._tx() as c:
        o = c.execute("""INSERT INTO sales_order (channel, external_id, ordered_at, ship_by, imported_at)
                         VALUES ('amazon', '113-1', '2026-11-11', '2026-11-13', 'x')""").lastrowid
        c.execute("INSERT INTO sales_line (order_id, line_key, sku, qty) VALUES (?, 'a', 'TEE-1', 1)", (o,))
    r = so.put(boxes['A1'], 1, scan_id='t1', code='0712345678935')
    assert [(s['external_id'], s['ship_by']) for s in r['open_sales']] == [('113-1', '2026-11-13')]
    look = so.look('0712345678935')
    assert look['kind'] == 'look' and look['open_sales'][0]['qty'] == 1


def test_look_offers_the_box_that_holds_it(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path, expected=3)
    so.put(boxes['A2'], 1, scan_id='l1', code=UPC)
    r = so.look(UPC)
    assert r['kind'] == 'look' and r['goes_as']['kind'] == 'audit'
    assert r['boxes'][0]['code'] == 'A2' and r['boxes'][0]['qty'] == 1
    assert units_in(w, boxes['A2']) == 1   # look writes nothing


# Past what was bought (Alex 11-12 1:55 PM): Sam's test scans of one Batmobile added units until on hand said 6 of 3
# bought. An extra-stock scan that passes units bought now asks; every other scan still saves at once.

def test_a_scan_past_what_was_bought_asks_and_saves_nothing_until_yes(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path, expected=3)
    out = [so.put(boxes['A1'], 1, scan_id=f'b{i}', code=UPC) for i in range(3)]
    assert [r['kind'] for r in out] == ['added'] * 3   # the 3 bought: no question
    r = so.put(boxes['A1'], 1, scan_id='b3', code=UPC)
    assert r['kind'] == 'over' and r['question'] == 'That makes 4 of 3 bought. Another one?'
    assert r['detail'].startswith('Now: 3 in stock.') and r['option']['kind'] == 'new'
    assert units_in(w, boxes['A1']) == 3 and not w._q1('SELECT 1 AS x FROM box_add WHERE scan_id = ?', ('b3',))
    y = so.put(boxes['A1'], 1, scan_id='b4', code=UPC, option=r['option'], over_ok=True)   # Yes
    assert (y['kind'], y['how']) == ('added', 'new') and units_in(w, boxes['A1']) == 4
    # A bigger pile of that extra line asks again; Yes makes it.
    q = so.put_qty(y['add_id'], 3, 1)
    assert q['kind'] == 'over' and q['question'] == 'That makes 6 of 3 bought. Put 2 more in?' and units_in(w, boxes['A1']) == 4
    q = so.put_qty(y['add_id'], 3, 1, over_ok=True)
    assert q['kind'] == 'added' and units_in(w, boxes['A1']) == 6
    assert so.put_qty(q['add_id'], 2, 1)['kind'] == 'added'   # a smaller pile never asks
    assert units_in(w, boxes['A1']) == 5 and w.check_integrity()['ok']


def test_shipped_units_count_as_in_and_a_count_fix_does_not(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path, expected=3)
    for i in range(3):
        so.put(boxes['A1'], 1, scan_id=f'c{i}', code=UPC)
    pid = w._q1('SELECT product_id FROM box_add LIMIT 1')['product_id']
    w.remove(1, boxes['A1'], [{'product_id': pid, 'qty': 1}], 'CUSTOMER')   # 1 sold and shipped: 2 on hand
    r = so.put(boxes['A1'], 1, scan_id='c3', code=UPC)
    assert r['kind'] == 'over' and r['question'].startswith('That makes 4 of 3 bought')
    assert r['detail'].startswith('Now: 2 in stock, 1 shipped or gone.')
    w.count(1, boxes['A1'], [{'product_id': pid, 'qty': 1}])   # a count fix: 1 lost
    r = so.put(boxes['A1'], 1, scan_id='c4', code=UPC)   # found again: 1 + 1 shipped + 1 = 3 of 3
    assert (r['kind'], r['how']) == ('added', 'new') and w.check_integrity()['ok']


def test_a_pile_bigger_than_its_row_fills_the_row_and_the_rest_goes_in_as_extra(tmp_path):
    """Alex 11-12: 30 in hand, scan one, type 30. Before, a pile past the row's units left was refused and the rest
    had to be scanned one by one."""
    w, au, so, cat, inbound, boxes = setup(tmp_path, expected=3)
    r = so.put(boxes['A1'], 1, scan_id='p1', code=UPC)
    assert r['how'] == 'audit'
    ask = so.put_qty(r['add_id'], 5, 1)
    assert ask['kind'] == 'over' and ask['detail'].endswith('3 go on its Stock audit row, 2 as extra.')
    assert units_in(w, boxes['A1']) == 1   # nothing changed while it asks
    out = so.put_qty(r['add_id'], 5, 1, over_ok=True)
    assert (out['how'], out['qty'], out['said']) == ('audit', 3, 'Stock audit: all found')
    x = out['extra_line']
    assert (x['how'], x['qty']) == ('new', 2) and units_in(w, boxes['A1']) == 5
    so.undo_add(x['add_id'], 1)   # each part has its own Undo
    assert units_in(w, boxes['A1']) == 3 and w.check_integrity()['ok']


def test_a_move_pile_past_the_other_box_says_how_many_are_there(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    p = w.save_product({'sku': 'MUG-4', 'title': 'Gray mug', 'identifiers': [{'kind': 'UPC', 'value': '0712345678942'}]})
    w.receive(1, p['id'], 2, 300, boxes['A2'])
    r = so.put(boxes['A1'], 1, scan_id='mv1', code='0712345678942')
    with pytest.raises(WmsError, match='Only 2 are in A2'):
        so.put_qty(r['add_id'], 5, 1)


def test_an_item_with_no_buys_never_asks(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    p = w.save_product({'sku': 'MUG-3', 'title': 'Green mug', 'identifiers': [{'kind': 'UPC', 'value': '0712345678935'}]})
    w.receive(1, p['id'], 5, 300, boxes['A2'])
    assert so.over_card(p['id'], 10) == {}


def test_put_away_card_into_a_new_box_adds_a_unit_never_moves_one(tmp_path):
    """Alex 11-24: an item already in A2, scanned on Put away and sent to another box from the card, is one more unit
    (as the card said), not one taken out of A2. The box page's own scan still moves it (the rule above)."""
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    p = w.save_product({'sku': 'MUG-9', 'title': 'Green mug', 'default_cost_cents': 450, 'identifiers': [{'kind': 'UPC', 'value': '0712345678911'}]})
    w.receive(1, p['id'], 2, 450, boxes['A2'])
    look = so.look('0712345678911')
    assert look['goes_as']['kind'] == 'new' and [b['code'] for b in look['boxes']] == ['A2']
    r = so.put(boxes['A1'], 1, scan_id='f1', code='0712345678911', fresh=True)
    assert (r['kind'], r['how']) == ('added', 'new')
    assert units_in(w, boxes['A1']) == 1 and units_in(w, boxes['A2']) == 2
    r2 = so.put(boxes['A1'], 1, scan_id='f2', product_id=p['id'], fresh=True)   # the item page's way in (add=)
    assert r2['how'] == 'new' and units_in(w, boxes['A2']) == 2
    w.receive(1, p['id'], 1, 450, boxes['A3'])   # 2 other places: still a new unit, no question
    assert so.put(boxes['A1'], 1, scan_id='f3', code='0712345678911', fresh=True)['how'] == 'new'
    assert units_in(w, boxes['A1']) == 3 and w.check_integrity()['ok']
