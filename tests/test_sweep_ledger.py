"""Sweep 11-11 group A (wms, sorting): each test failed on a9449e7 and passes after the fix."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from test_box_add import _two_orders  # noqa: E402
from test_sorting import UPC, setup, units_in  # noqa: E402
from wms import Wms, WmsError  # noqa: E402


def test_a1_resent_scan_gets_its_first_answer(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path, expected=1)
    a = so.scan(UPC, boxes['A1'], 1, scan_id='s1')
    b = so.scan(UPC, boxes['A1'], 1, scan_id='s1')
    assert (a['kind'], b['kind'], b['repeat']) == ('found', 'found', True)
    assert units_in(w, boxes['A1']) == 1
    # an extra resent is the same extra, not a new card
    e = so.extra(UPC, boxes['A1'], 1, scan_id='s2', item_id=a['item']['id'])
    f = so.scan(UPC, boxes['A1'], 1, scan_id='s2')
    assert (f['kind'], f['repeat'], f['extra']['id']) == ('extra', True, e['extra']['id'])
    assert units_in(w, boxes['A1']) == 2


def test_a2_undo_never_puts_stock_in_a_retired_box(tmp_path):
    w = Wms(str(tmp_path / 'x.db'))
    p = w.save_product({'sku': 'S1', 'title': 'Thing'})['id']
    b1, b2 = [b['id'] for b in w.create_boxes(2, 1)]
    w.receive(1, p, 5, 100, {'box_id': b1})
    mv = w.move(1, [{'product_id': p, 'qty': 5}], {'box_id': b1}, {'box_id': b2})
    w.update_box(b1, archived=True)
    with pytest.raises(WmsError, match='retired'):
        w.undo(1, mv['action_id'])
    assert w._q1('SELECT COALESCE(SUM(qty), 0) n FROM stock_level WHERE box_id = ?', (b1,))['n'] == 0
    # an archived place is refused the same way, for stock and for a box moved back into it
    loc = w.create_locations(['OLD1'])[0]['id']
    w.receive(1, p, 2, 100, {'location_id': loc})
    mv2 = w.move(1, [{'product_id': p, 'qty': 2}], {'location_id': loc}, {'box_id': b2})
    with w._tx() as c:
        c.execute('UPDATE location SET archived = 1 WHERE id = ?', (loc,))
    with pytest.raises(WmsError, match='archived'):
        w.undo(1, mv2['action_id'])
    with w._tx() as c, pytest.raises(WmsError, match='archived'):
        w._move_box(c, w._new_action(c, 1, 'move', 'x'), b2, loc)
    assert w.check_integrity(record=False)['ok']


def test_a3_extra_on_a_no_price_line_is_cost_not_entered(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    p = w.save_product({'sku': 'B0NOCOST01', 'title': 'Walmart thing', 'identifiers': [{'kind': 'ASIN', 'value': 'B0NOCOST01'}]})['id']
    w.receive(1, p, 1, 1000, boxes['A1'])
    with w._tx() as c:
        po = c.execute("INSERT INTO purchase_order (source, external_id, ordered_at, imported_at) VALUES ('walmart.com', 'W1', '2026-11-01', 'x')").lastrowid
        c.execute("INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents) VALUES (?, 'k', 'B0NOCOST01', 'Walmart thing', 3, 0)", (po,))
    opt = [o for o in so.add_check(boxes['A2'], product_id=p)['options'] if o['kind'] == 'order'][0]
    so.add(boxes['A2'], opt, 3, 1, scan_id='n1')
    assert w._q1('SELECT cost_missing FROM receipt ORDER BY id DESC LIMIT 1')['cost_missing'] == 1
    assert w._avg_costs(p)[p] == 1000


def test_a5_undo_add_over_several_orders_is_all_or_nothing(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    _two_orders(w)
    opt = so.add_check(boxes['A2'], code='B0PREDATOR')['options'][0]
    x = so.add(boxes['A2'], opt, 3, 1, scan_id='p1', code='B0PREDATOR')
    pid = x['product_id']
    w.move(1, [{'product_id': pid, 'qty': 2}], boxes['A2'], boxes['A1'])
    with pytest.raises(WmsError, match='Only'):
        so.undo_add(x['add_id'], 1)
    assert [r['reversed_by'] for r in w._q('SELECT a.reversed_by FROM receipt r JOIN action a ON a.id = r.action_id')] == [None, None]
    w.move(1, [{'product_id': pid, 'qty': 2}], boxes['A1'], boxes['A2'])
    so.undo_add(x['add_id'], 1)
    assert units_in(w, boxes['A2'], pid) == 0


def test_a6_dashed_box_name_is_the_same_box(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    b = w.create_boxes(1, 1, name='A-31')[0]
    assert b['name'] == 'A31'
    assert so.find('A-31')['kind'] != 'no_target'
    with pytest.raises(WmsError, match='already a box named A31'):
        w.create_boxes(1, 1, name='a31')
    other = w.create_boxes(1, 1, name='A32')[0]
    with pytest.raises(WmsError, match='already'):
        w.update_box(other['id'], name='A-31')
    # a legacy dashed name saved before the fix is still found by the Box bar
    with w._tx() as c:
        c.execute("UPDATE box SET name = 'E-7' WHERE id = ?", (other['id'],))
    assert so._box_by_name('E7')['id'] == other['id']


def test_a7_lookup_and_summary_past_2000_products(tmp_path):
    w = Wms(str(tmp_path / 'x.db'))
    with w._tx() as c:
        for i in range(2001):
            c.execute("INSERT INTO product (sku, title, owner_id, tracking, created_at, updated_at) VALUES (?, 't', 1, 'qty', 'x', ?)",
                      (f'S{i}', '2026-01-01' if i == 0 else '2026-02-01'))
    first = w._q1("SELECT id FROM product WHERE sku = 'S0'")['id']
    b = w.create_boxes(1, 1)[0]['id']
    w.receive(1, first, 4, 250, {'box_id': b})
    with w._tx() as c:   # receiving bumps updated_at; put it back at the bottom of the list
        c.execute("UPDATE product SET updated_at = '2026-01-01' WHERE id = ?", (first,))
    assert [p['id'] for p in w.lookup('S0')] == [first]
    s = w.summary()
    assert (s['units'], s['value_cents']) == (4, 1000)
    assert len(w.list_products()) == 2000


def test_a8_receive_with_line_of_other_product_refused(tmp_path):
    w = Wms(str(tmp_path / 'x.db'))
    from commerce import Commerce
    c = Commerce(w)
    a = w.save_product({'sku': 'A', 'title': 'A', 'identifiers': [{'kind': 'ASIN', 'value': 'B0AAAAAAAA'}]})['id']
    bb = w.save_product({'sku': 'B', 'title': 'B'})['id']
    box = w.create_boxes(1, 1)[0]['id']
    c.add_purchase({'order_id': '111-1', 'source': 'amazon', 'asin': 'B0AAAAAAAA', 'title': 'A', 'qty': 2,
                    'unit_cost_cents': 5000, 'date': '2026-10-01'})
    lid = w._q1('SELECT id FROM purchase_line')['id']
    with pytest.raises(WmsError, match='not on this product'):
        w.receive(1, bb, 2, 5000, {'box_id': box}, purchase_line_id=lid)
    w.receive(1, a, 2, 5000, {'box_id': box}, purchase_line_id=lid)
    assert c.get_line(lid)['left_to_receive'] == 0
