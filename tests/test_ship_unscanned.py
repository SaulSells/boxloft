"""Ship from wherever the item is, even if that is nowhere yet (flow audit 11-12, redesign 3).

Before: 3 of 3 open orders showed 0 on the shelves, so shipping meant Found it (say a box), save, then the ship dialog:
4 steps, and the ledger read "Received 1 into A36" and "1 out of A36" in the same minute for an item that never sat
there. Now: one "Packed and shipped" = one action, supplier -> customer, tied to the sale and the Stock audit row.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from test_put_away import order as buy  # noqa: E402
from test_sorting import ASIN, setup  # noqa: E402
from wms import WmsError, now_utc  # noqa: E402


def sale(w, ext, asin=ASIN, qty=1, status='open', review=''):
    with w._tx() as c:   # the sync's columns (sales_sync starts them on live)
        for col in ('market_status', 'review'):
            w._add_column(c, 'sales_order', col, "TEXT NOT NULL DEFAULT ''")
        o = c.execute("""INSERT INTO sales_order (channel, external_id, ordered_at, status, imported_at)
                         VALUES ('amazon', ?, '2026-11-12', ?, ?)""", (ext, status, now_utc())).lastrowid
        if review:
            c.execute('UPDATE sales_order SET review = ? WHERE id = ?', (review, o))
        pid = c.execute("SELECT product_id FROM product_identifier WHERE kind = 'ASIN' AND value = ?", (asin,)).fetchone()
        return c.execute("INSERT INTO sales_line (order_id, line_key, asin, title, qty, product_id, price_cents) VALUES (?, 'L1', ?, 'Toploader', ?, ?, 1999)",
                         (o, asin, qty, pid[0] if pid else None)).lastrowid


def left(au, item_id):
    it = au.item(item_id)
    return it['expected'] - it['accounted']


def moves(w, action_id):
    return w._q('SELECT * FROM stock_move WHERE action_id = ?', (action_id,)), w._q('SELECT * FROM box_move WHERE action_id = ?', (action_id,))


def test_ship_with_none_on_shelves_is_one_action_and_takes_one_off_the_audit_row(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path, expected=3)
    row = w._q1('SELECT id FROM audit_item WHERE asin = ?', (ASIN,))['id']
    au._product_for(au.item(row), 1)   # the order card's product (autolink makes it from the audit row)
    ln = sale(w, '113-NOSHELF')
    before = left(au, row)
    acts = w._q1('SELECT COUNT(*) AS n FROM action')['n']
    r = au.ship_unscanned(ln, 1)
    assert w._q1('SELECT COUNT(*) AS n FROM action')['n'] == acts + 1           # 1 action
    sm, bm = moves(w, r['action_id'])
    assert not bm and len(sm) == 1 and sm[0]['to_box_id'] == 0                   # no box move, nothing into a box
    assert left(au, row) == before - 1                                           # the audit row: 1 fewer to find
    assert w._q1('SELECT status FROM sales_order WHERE external_id = ?', ('113-NOSHELF',))['status'] == 'shipped'
    assert w.get_product(au.item(row)['product_id'])['on_hand'] == 0
    with pytest.raises(WmsError):
        au.ship_unscanned(ln, 1)                                                 # twice: refused
    assert w.check_integrity(record=False)['ok']
    # Undo: the order is open again, the audit row back where it was.
    u = au.undo_unscanned(ln, 1)
    assert u['reopened'] and left(au, row) == before
    assert w._q1('SELECT status FROM sales_order WHERE external_id = ?', ('113-NOSHELF',))['status'] == 'open'
    assert w._q1('SELECT shipped_action_id FROM sales_line WHERE id = ?', (ln,))['shipped_action_id'] is None
    assert w.check_integrity(record=False)['ok']


def test_stock_on_a_shelf_is_shipped_from_there(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path, expected=3)
    r = so.put(boxes['A1'], 1, scan_id='s1', code='012345678905')   # 1 found into A1
    ln = sale(w, '113-ONSHELF')
    with pytest.raises(WmsError, match='on the shelves'):
        au.ship_unscanned(ln, 1)
    assert r['here'] == 1


def test_delivered_order_not_on_the_audit_yet(tmp_path, monkeypatch):
    import inbound as inbound_mod
    from datetime import date
    monkeypatch.setattr(inbound_mod, 'today_et', lambda: date(2026, 11, 12))
    import audit as audit_mod   # audit imports today_et by name: patch its copy too, or the test follows the real date
    monkeypatch.setattr(audit_mod, 'today_et', lambda: date(2026, 11, 12))
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    line = buy(w, '112-NEW', 2, ordered='2026-11-05', delivered='2026-11-10', asin='B0UNLISTED', cost=444)
    pid = au.product_for_purchase_line(line, 1)['product_id']
    ln = sale(w, '113-KLEENEX', asin='B0UNLISTED')
    r = au.ship_unscanned(ln, 1)
    rc = w._q1('SELECT purchase_line_id, unit_cost_cents, qty FROM receipt WHERE action_id = ?', (r['action_id'],))
    assert rc == {'purchase_line_id': line, 'unit_cost_cents': 444, 'qty': 1} and r['item_id'] is None
    assert w.get_product(pid)['on_hand'] == 0
    au.undo_unscanned(ln, 1)
    assert not w._q1('SELECT 1 AS x FROM receipt r JOIN action a ON a.id = r.action_id WHERE a.reversed_by IS NULL AND a.reverses_action_id IS NULL')


def test_nothing_says_it_is_here(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    p = w.save_product({'sku': 'MUG-1', 'title': 'Blue mug', 'identifiers': [{'kind': 'ASIN', 'value': 'B0MUG00001'}]})
    ln = sale(w, '113-MUG', asin='B0MUG00001')
    with pytest.raises(WmsError, match='nothing says it is here'):
        au.ship_unscanned(ln, 1)
    assert p['id']


def test_auto_ship_takes_a_marketplace_shipped_order_with_none_on_shelves(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path, expected=3)
    row = w._q1('SELECT id FROM audit_item WHERE asin = ?', (ASIN,))['id']
    au._product_for(au.item(row), 1)
    before = left(au, row)
    com = au.commerce
    ln = sale(w, '113-AUTO', review=com.MARKED_SHIPPED)
    with w._tx() as c:
        c.execute("UPDATE sales_order SET fulfilled_by = 'me'")
    out = com.auto_ship()
    assert (out['orders'], out['lines'], out['review']) == (1, 1, 0)
    o = w._q1("SELECT status, review FROM sales_order WHERE external_id = '113-AUTO'")
    assert o == {'status': 'shipped', 'review': ''} and left(au, row) == before - 1
    act = w._q1('SELECT a.user_id, a.device FROM sales_line l JOIN action a ON a.id = l.shipped_action_id WHERE l.id = ?', (ln,))
    assert (act['user_id'], act['device']) == (0, 'auto')
    line = [x for o in com.pick_list('shipped') for x in o['lines'] if x['id'] == ln][0]
    assert line['unscanned_id']
    # Undo of a marketplace-shipped one: stock back, the order stays shipped, and (as for any undone audit save tied to
    # a sale) the sale stays off every row, so the row expects the unit again.
    u = au.undo_unscanned(ln, 1)
    assert not u['reopened'] and w._q1("SELECT status FROM sales_order WHERE external_id = '113-AUTO'")['status'] == 'shipped'
    assert left(au, row) == before
    assert w.check_integrity(record=False)['ok']


# Alex 11-17 (EA SPORTS College Football 27): 2 sold and shipped from Seller Central, none on any buy Boxloft reads,
# so "nothing says it is here" kept both orders open for good. A person can say it was bought outside Boxloft.

def test_bought_outside_boxloft_ships_with_cost_not_entered_and_undoes(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    p = w.save_product({'sku': 'CFB-27', 'title': 'College Football 27', 'identifiers': [{'kind': 'ASIN', 'value': 'B0N2Z6LI1O'}]})
    ln = sale(w, '113-CFB', asin='B0N2Z6LI1O')   # open, not marked shipped yet: the card's button
    r = au.ship_unscanned(ln, 1, outside=True)
    assert r['order_done']
    rec = w._q1('SELECT qty, unit_cost_cents, cost_missing, purchase_line_id FROM receipt WHERE action_id = ?', (r['action_id'],))
    assert rec == {'qty': 1, 'unit_cost_cents': 0, 'cost_missing': 1, 'purchase_line_id': None}
    sm, bm = moves(w, r['action_id'])
    assert not bm and len(sm) == 1 and sm[0]['to_box_id'] == 0
    assert w.get_product(p['id'])['on_hand'] == 0
    assert w._q1("SELECT status FROM sales_order WHERE external_id = '113-CFB'")['status'] == 'shipped'
    assert w._q1('SELECT auto FROM ship_unscanned WHERE sale_line_id = ?', (ln,))['auto'] == 0
    assert w.check_integrity(record=False)['ok']
    # The item's Usual unit cost fills the unit later, like any receipt saved with no cost.
    with w._tx() as c:
        assert w._fill_costs(c, p['id'], 3500, 1, now_utc())['units'] == 1
    assert w._q1('SELECT unit_cost_cents FROM receipt WHERE action_id = ?', (r['action_id'],))['unit_cost_cents'] == 3500
    u = au.undo_unscanned(ln, 1)
    assert u['reopened']
    assert w._q1("SELECT status FROM sales_order WHERE external_id = '113-CFB'")['status'] == 'open'
    assert w.check_integrity(record=False)['ok']


def test_bought_outside_with_a_typed_cost(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    w.save_product({'sku': 'CFB-27', 'title': 'College Football 27', 'identifiers': [{'kind': 'ASIN', 'value': 'B0N2Z6LI1O'}]})
    ln = sale(w, '113-CFB2', asin='B0N2Z6LI1O', qty=2)
    with pytest.raises(WmsError, match='above'):
        au.ship_unscanned(ln, 1, outside=True, cost_cents=-5)
    r = au.ship_unscanned(ln, 1, outside=True, cost_cents=4200, bought_on=' 112-0000000-0000001 ')
    rec = w._q1('SELECT qty, unit_cost_cents, cost_missing FROM receipt WHERE action_id = ?', (r['action_id'],))
    assert rec == {'qty': 2, 'unit_cost_cents': 4200, 'cost_missing': 0}
    act = w._q1('SELECT r.reference, a.note FROM receipt r JOIN action a ON a.id = r.action_id WHERE a.id = ?', (r['action_id'],))
    assert act['reference'] == '112-0000000-0000001' and 'bought on 112-0000000-0000001' in act['note']


def test_bought_outside_is_refused_when_boxloft_knows_where_it_is(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path, expected=3)
    row = w._q1('SELECT id FROM audit_item WHERE asin = ?', (ASIN,))['id']
    au._product_for(au.item(row), 1)
    ln = sale(w, '113-AUD', qty=5)   # the audit row covers 3 of 5: still not "outside"
    with pytest.raises(WmsError, match='Stock audit expects'):
        au.ship_unscanned(ln, 1, outside=True)
    so.put(boxes['A1'], 1, scan_id='s9', code='012345678905')
    ln2 = sale(w, '113-SHELF')
    with pytest.raises(WmsError, match='on the shelves'):
        au.ship_unscanned(ln2, 1, outside=True)


def test_store_buy_sold_before_boxing_box_keeps_what_is_there(tmp_path):
    """Alex 11-17: "bought 50, sold 10 on Amazon, I only have 40 to box." The sync closes each sale Amazon shipped
    while Boxloft held none as sold before it came in, so boxing the 40 leaves 40 (before: the next sync pulled the
    10 out of the new box, 30 shown)."""
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    p = w.save_product({'sku': 'STORE-1', 'title': 'Store buy', 'identifiers': [{'kind': 'ASIN', 'value': 'B0STORE001'}]})
    com = au.commerce
    lines = [sale(w, f'113-ST{i}', asin='B0STORE001', review=com.MARKED_SHIPPED) for i in range(10)]
    with w._tx() as c:
        c.execute("UPDATE sales_order SET fulfilled_by = 'me'")
    out = com.auto_ship()
    assert (out['orders'], out['lines'], out['review']) == (10, 10, 0)
    notes = {r['note'] for r in w._q("""SELECT a.note, a.user_id FROM sales_line l JOIN action a ON a.id = l.shipped_action_id
                                         WHERE l.order_id IN (SELECT order_id FROM sales_line WHERE product_id = ?)""", (p['id'],))}
    assert all('sold before it came in' in n and 'held none when amazon shipped it' in n for n in notes)
    w.receive(1, p['id'], 40, 1500, boxes['A1'])
    assert com.auto_ship()['lines'] == 0
    assert w.get_product(p['id'])['on_hand'] == 40
    assert all(w._q1('SELECT shipped_action_id FROM sales_line WHERE id = ?', (l,))['shipped_action_id'] for l in lines)
    assert w.check_integrity(record=False)['ok']
    # An auto close undone: stock back to the supplier side, the order stays shipped (Amazon shipped it).
    u = au.undo_unscanned(lines[0], 1)
    assert not u['reopened'] and w.get_product(p['id'])['on_hand'] == 40


def test_sync_still_takes_stock_already_in_a_box(tmp_path):
    """When Boxloft holds the item, a marketplace-shipped sale still comes out of the box (the unit left from there)."""
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    p = w.save_product({'sku': 'STORE-2', 'title': 'Boxed', 'identifiers': [{'kind': 'ASIN', 'value': 'B0STORE002'}]})
    w.receive(1, p['id'], 5, 1500, boxes['A1'])
    com = au.commerce
    sale(w, '113-BOXED', asin='B0STORE002', review=com.MARKED_SHIPPED)
    with w._tx() as c:
        c.execute("UPDATE sales_order SET fulfilled_by = 'me'")
    assert com.auto_ship()['lines'] == 1 and w.get_product(p['id'])['on_hand'] == 4
