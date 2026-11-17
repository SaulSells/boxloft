"""Merge items (Alex 11-17): "I scanned a product that's on the audit and it linked to a different ASIN than the audit
has. So now I have this item in inventory but the audit is still open." Live case: item 620 (EAN 4573102631374, Keepa
ASIN B0O7GVQGT5), 10 extra units in a box; Stock audit row 297 (ASIN B0O22N80A0) still expected 10."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from merge import Merge  # noqa: E402
from test_sorting import ASIN, setup  # noqa: E402
from wms import Conflict, WmsError, now_utc  # noqa: E402

Merge.enabled = True   # the tests exercise the merge itself, whatever the live switch says


def left(au, item_id):
    it = au.item(item_id)
    return it['expected'] - it['accounted']


def in_box(w, pid, box):
    return w._q1('SELECT COALESCE(SUM(qty), 0) AS n FROM stock_level WHERE product_id = ? AND box_id = ?', (pid, box['box_id']))['n']


def owner_of(w, code):
    return w._q1('SELECT product_id FROM product_identifier WHERE value = ?', (code,))['product_id']


def made_by_scan(w, box, qty=3):
    """The new item card's item: another ASIN for the same thing, its units in as extra stock (no buy line)."""
    p = w.save_product({'sku': 'B0OTHER001', 'title': 'Toploader (other listing)',
                        'identifiers': [{'kind': 'ASIN', 'value': 'B0OTHER001'}, {'kind': 'EAN', 'value': '4573102631374'}]})
    w.receive(1, p['id'], qty, None, box, reference='box page')
    return p['id']


def test_scanned_item_folds_into_its_audit_row(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path, expected=3)
    m = Merge(w, au.commerce, au)
    row = w._q1('SELECT id FROM audit_item WHERE asin = ?', (ASIN,))['id']
    pid = made_by_scan(w, boxes['A1'])
    assert left(au, row) == 3 and in_box(w, pid, boxes['A1']) == 3
    pre = m.preview(pid, {'audit_id': row})
    assert pre['target_id'] == pid and pre['to_row'] == 3
    with pytest.raises(Conflict):
        m.run(pid, {'audit_id': row}, 1, seen={'on_hand': 2})                   # the screen showed other numbers
    r = m.run(pid, {'audit_id': row}, 1, seen={'on_hand': 3})
    assert r['target_id'] == pid and left(au, row) == 0                          # the row counts them as found
    assert in_box(w, pid, boxes['A1']) == 3                                      # still 3 in the box
    rec = w._q("""SELECT r.qty, r.purchase_line_id FROM receipt r JOIN action a ON a.id = r.action_id
                   WHERE r.product_id = ? AND a.reversed_by IS NULL AND r.purchase_line_id IS NOT NULL""", (pid,))
    assert sum(x['qty'] for x in rec) == 3                                       # against the row's order line
    assert au.item(row)['product_id'] == pid                                     # the row's ASIN is on the item now
    assert w.check_integrity(record=False)['ok']
    m.undo(r['merge_id'], 1)
    assert left(au, row) == 3 and in_box(w, pid, boxes['A1']) == 3
    assert not w._q1("SELECT 1 AS x FROM product_identifier WHERE product_id = ? AND value = ?", (pid, ASIN))
    assert w.check_integrity(record=False)['ok']
    with pytest.raises(WmsError, match='Already undone'):
        m.undo(r['merge_id'], 1)


def test_two_items_become_one(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path, expected=3)
    m = Merge(w, au.commerce, au)
    t = w.save_product({'sku': 'MUG-1', 'title': 'Blue mug', 'identifiers': [{'kind': 'UPC', 'value': '036000291452'}]})['id']
    w.receive(1, t, 1, 800, boxes['A2'])
    s = w.save_product({'sku': 'MUG-DUP', 'title': 'Blue mug (made twice)',
                        'identifiers': [{'kind': 'EAN', 'value': '4006381333931'}, {'kind': 'UPC', 'value': '036000291452'}]})['id']
    w.receive(1, s, 2, 600, boxes['A1'])
    with w._tx() as c:
        o = c.execute("INSERT INTO sales_order (channel, external_id, ordered_at, status, imported_at) VALUES ('ebay', '1-1', '2026-11-17', 'open', ?)",
                      (now_utc(),)).lastrowid
        sl = c.execute("INSERT INTO sales_line (order_id, line_key, title, qty, product_id) VALUES (?, 'L', 'mug', 1, ?)", (o, s)).lastrowid
    r = m.run(s, {'product_id': t}, 1, seen={'on_hand': 2})
    assert in_box(w, s, boxes['A1']) == 0 and in_box(w, t, boxes['A1']) == 2 and in_box(w, t, boxes['A2']) == 1
    assert w._q1('SELECT archived FROM product WHERE id = ?', (s,))['archived'] == 1
    assert w._q1('SELECT product_id FROM sales_line WHERE id = ?', (sl,))['product_id'] == t
    assert owner_of(w, '4006381333931') == t                       # the next scan of its barcode is the target
    cost = w._q1("""SELECT r.unit_cost_cents FROM receipt r JOIN action a ON a.id = r.action_id
                     WHERE r.product_id = ? AND r.reference = 'merge' AND a.reversed_by IS NULL""", (t,))
    assert cost['unit_cost_cents'] == 600                                        # at the source's cost
    assert w.check_integrity(record=False)['ok']
    m.undo(r['merge_id'], 1)
    assert in_box(w, s, boxes['A1']) == 2 and in_box(w, t, boxes['A1']) == 0
    assert w._q1('SELECT archived FROM product WHERE id = ?', (s,))['archived'] == 0
    assert w._q1('SELECT product_id FROM sales_line WHERE id = ?', (sl,))['product_id'] == s
    assert owner_of(w, '4006381333931') == s
    assert w._q1("SELECT COUNT(*) AS n FROM product_identifier WHERE product_id = ? AND value = '036000291452'", (s,))['n'] == 1
    assert w.check_integrity(record=False)['ok']


def test_refusals(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    m = Merge(w, au.commerce, au)
    a = w.save_product({'sku': 'A-1', 'title': 'A'})['id']
    with pytest.raises(WmsError, match='same item'):
        m.run(a, {'product_id': a}, 1)
    with pytest.raises(WmsError, match='Pick'):
        m.run(a, {}, 1)
    with pytest.raises(WmsError):
        m.undo(999, 1)
