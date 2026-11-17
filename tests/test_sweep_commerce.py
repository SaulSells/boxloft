"""Sweep 11-11 group B (commerce, returns): each test failed on a9449e7 and passes after the fix."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from audit import Audit  # noqa: E402
from commerce import Commerce  # noqa: E402
from connections import Connections  # noqa: E402
from inbound import Inbound  # noqa: E402
from returns import Returns  # noqa: E402
from sales_sync import Sales  # noqa: E402
from wms import Wms, WmsError  # noqa: E402

U = 1


@pytest.fixture
def env(tmp_path):
    w = Wms(str(tmp_path / 'w.db'))
    c = Commerce(w)
    r = Returns(w, c)
    s = Sales(w, c, Connections(str(tmp_path)))
    Inbound(w, r)
    Audit(w, c, None, r)
    loc = w.create_locations(['A-01-01'])[0]['id']
    boxes = [b['id'] for b in w.create_boxes(2, U, loc)]
    pid = w.save_product({'sku': 'SKU1', 'title': 'Filter', 'pkg_l_mm': 100, 'pkg_w_mm': 80, 'pkg_h_mm': 50,
                          'identifiers': [{'kind': 'ASIN', 'value': 'B0TEST0001'}]})['id']
    pid2 = w.save_product({'sku': 'SKU2', 'title': 'Other', 'pkg_l_mm': 100, 'pkg_w_mm': 80, 'pkg_h_mm': 50})['id']
    return w, c, r, s, boxes, pid, pid2


def line(c, w, qty, cost, asin='B0TEST0001', oid=None):
    c.add_purchase({'order_id': oid or f'111-{qty}{cost}{asin[-2:]}', 'source': 'amazon', 'asin': asin, 'title': 'Filter',
                    'qty': qty, 'unit_cost_cents': cost, 'date': '2026-10-01'})
    return w._q1('SELECT id FROM purchase_line ORDER BY id DESC LIMIT 1')['id']


def test_b1_cancelled_order_lines_not_offered_or_received(env):
    w, c, r, s, boxes, pid, pid2 = env
    lid = line(c, w, 3, 1000, oid='112-CANCEL')
    with w._tx() as cx:
        cx.execute("UPDATE purchase_order SET state = 'cancelled' WHERE external_id = '112-CANCEL'")
    assert c.open_lines_for_code('B0TEST0001') == []
    assert c.open_lines_for(pid) == []
    with pytest.raises(WmsError, match='cancelled'):
        w.receive(U, pid, 1, 1000, {'box_id': boxes[0]}, purchase_line_id=lid)


def test_b2_new_return_after_kept_starts_fresh(env):
    w, c, r, s, boxes, pid, pid2 = env
    lid = line(c, w, 5, 1000)
    ret = r.create(lid, 3, U)
    r.update(ret['id'], U, state='kept')
    ret = r.create(lid, 2, U)
    assert (ret['qty'], ret['expected_cents']) == (2, 2000)
    assert c.get_line(lid)['left_to_receive'] == 3


def test_b3_ship_refuses_cancelled_order(env):
    w, c, r, s, boxes, pid, pid2 = env
    w.receive(U, pid, 3, 500, {'box_id': boxes[0]})
    c.add_sale({'product_id': pid, 'qty': 1, 'order_id': 'X-1', 'channel': 'ebay'})
    oid = w._q1("SELECT id FROM sales_order WHERE external_id = 'X-1'")['id']
    sl = w._q1('SELECT id FROM sales_line WHERE order_id = ?', (oid,))['id']
    c.set_order_status(oid, 'cancelled')
    with pytest.raises(WmsError, match='cancelled'):
        c.ship_line(U, sl, {'box_id': boxes[0]})
    assert w._q1('SELECT status FROM sales_order WHERE id = ?', (oid,))['status'] == 'cancelled'
    assert w.get_product(pid)['on_hand'] == 3


def test_b4_refund_import_never_lowers_recorded_return(env):
    w, c, r, s, boxes, pid, pid2 = env
    lid = line(c, w, 4, 1000)
    oid = w._q1('SELECT o.external_id FROM purchase_order o JOIN purchase_line l ON l.po_id = o.id WHERE l.id = ?',
                (lid,))['external_id']
    ret = r.create(lid, 3, U, state='sent')
    csv = ('Order ID,Refund Amount,Quantity,Reversal Reason,Refund Date,Creation Date,Payment Status\n'
           f'{oid},10.00,1,Customer Return,2026-10-20,2026-10-15,Completed\n')
    r.import_export({'Refund Details.csv': csv})
    assert r.get(ret['id'])['qty'] == 3
    assert c.get_line(lid)['left_to_receive'] == 1


def test_b5_receive_refuses_line_of_another_product(env):
    w, c, r, s, boxes, pid, pid2 = env
    lid = line(c, w, 2, 9999, asin='B0OTHER001')
    with pytest.raises(WmsError, match='not on this product'):
        c.receive_lines(U, pid, {'box_id': boxes[0]}, [{'purchase_line_id': lid, 'qty': 2}])
    assert c.get_line(lid)['product_id'] is None
    # a line already held by another product is refused too, and not offered to this one
    mine = line(c, w, 2, 1000)
    w.receive(U, pid, 1, 1000, {'box_id': boxes[0]}, purchase_line_id=mine)
    with pytest.raises(WmsError, match='another product'):
        w.receive(U, pid2, 1, 1000, {'box_id': boxes[0]}, purchase_line_id=mine)
    with w._tx() as cx:
        cx.execute("INSERT INTO product_identifier (product_id, kind, value) VALUES (?, 'ASIN', 'B0TEST0001')", (pid2,))
    assert [x['id'] for x in c.open_lines_for(pid2)] == []


def test_b6_one_ship_box_per_order(env):
    w, c, r, s, boxes, pid, pid2 = env
    w.receive(U, pid, 3, 500, {'box_id': boxes[0]})
    w.receive(U, pid2, 3, 500, {'box_id': boxes[1]})
    c.save_ship_box({'name': 'Mid', 'inner_l_in': 12, 'inner_w_in': 10, 'inner_h_in': 8, 'stock': 10})
    order = {'external_id': '113-1', 'ordered_at': '2026-11-11', 'ship_by': None, 'state': 'open', 'fulfilled_by': 'me',
             'market_status': 'Unshipped', 'lines': [
                 {'line_key': 'L1', 'sku': 'SKU1', 'asin': '', 'title': 'Filter', 'qty': 1, 'price_cents': 1999},
                 {'line_key': 'L2', 'sku': 'SKU2', 'asin': '', 'title': 'Other', 'qty': 1, 'price_cents': 1999}]}
    s.upsert('amazon', [order], live=True)
    order.update(state='shipped', market_status='Shipped')
    s.upsert('amazon', [order], live=True)
    assert c.auto_ship()['lines'] == 2
    assert c.list_ship_boxes()[0]['stock'] == 9


def test_b7_add_sale_never_grows_a_shipped_line(env):
    w, c, r, s, boxes, pid, pid2 = env
    w.receive(U, pid, 5, 500, {'box_id': boxes[0]})
    w.receive(U, pid2, 5, 500, {'box_id': boxes[1]})
    c.add_sale({'product_id': pid, 'qty': 1, 'order_id': 'M-1'})
    c.add_sale({'product_id': pid2, 'qty': 1, 'order_id': 'M-1'})
    oid = w._q1("SELECT id FROM sales_order WHERE external_id = 'M-1'")['id']
    l1 = w._q1('SELECT id FROM sales_line WHERE order_id = ? AND product_id = ?', (oid, pid))['id']
    c.ship_line(U, l1, {'box_id': boxes[0]})
    c.add_sale({'product_id': pid, 'qty': 2, 'order_id': 'M-1'})
    assert w._q1('SELECT qty FROM sales_line WHERE id = ?', (l1,))['qty'] == 1
    new = w._q1('SELECT qty, shipped_action_id FROM sales_line WHERE order_id = ? AND product_id = ? AND id != ?',
                (oid, pid, l1))
    assert (new['qty'], new['shipped_action_id']) == (2, None)


def test_b8_line_with_no_price_is_cost_not_entered(env):
    w, c, r, s, boxes, pid, pid2 = env
    w.receive(U, pid, 1, 2000, {'box_id': boxes[0]})
    l0 = line(c, w, 1, 0)
    c.receive_lines(U, pid, {'box_id': boxes[0]}, [{'purchase_line_id': l0, 'qty': 1}])
    assert w._q1('SELECT cost_missing FROM receipt ORDER BY id DESC LIMIT 1')['cost_missing'] == 1
    assert w.get_product(pid)['avg_cost_cents'] == 2000
