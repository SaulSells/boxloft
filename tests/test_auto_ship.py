"""Alex 11-11: an order shipped in Seller Central (or eBay) is scanned out here by Boxloft, as the ship dialog's
defaults would; when those defaults can't be right, the order keeps `review` with the reason."""
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
from commerce import Commerce  # noqa: E402
from connections import Connections  # noqa: E402
from sales_sync import Sales  # noqa: E402
from wms import Wms  # noqa: E402

U = 1
TODAY = datetime.now(ZoneInfo('America/New_York')).date()


def make(tmp_path):
    w = Wms(str(tmp_path / 'wms.db'))
    c = Commerce(w)
    s = Sales(w, c, Connections(str(tmp_path)))
    loc = w.create_locations(['A-01-01'])[0]['id']
    boxes = [b['id'] for b in w.create_boxes(2, U, loc)]
    pid = w.save_product({'sku': 'SKU1', 'title': 'Filter', 'pkg_l_mm': 100, 'pkg_w_mm': 80, 'pkg_h_mm': 50})['id']
    return w, c, s, boxes, pid


def order(ext, state='open', qty=2, sku='SKU1', days_ago=0):
    return {'external_id': ext, 'ordered_at': (TODAY - timedelta(days=days_ago)).isoformat(), 'ship_by': None,
            'state': state, 'fulfilled_by': 'me', 'market_status': 'Shipped' if state == 'shipped' else 'Unshipped',
            'lines': [{'line_key': 'L1', 'sku': sku, 'asin': 'B0POFT56O2', 'title': 'Filter', 'qty': qty, 'price_cents': 1999}]}


def so(w, ext):
    return w._q1('SELECT * FROM sales_order WHERE external_id = ?', (ext,))


def test_shipped_on_amazon_is_scanned_out_from_the_dialogs_default(tmp_path):
    w, c, s, boxes, pid = make(tmp_path)
    w.receive(U, pid, 3, 500, {'box_id': boxes[0]})
    sb = c.save_ship_box({'name': 'Small', 'inner_l_in': 6, 'inner_w_in': 5, 'inner_h_in': 4, 'stock': 5})
    s.upsert('amazon', [order('111-1')], live=True)
    assert c.auto_ship() == {'orders': 0, 'lines': 0, 'review': 0}   # still open on Amazon: nothing happens
    s.upsert('amazon', [order('111-1', 'shipped')], live=True)
    assert c.auto_ship() == {'orders': 1, 'lines': 1, 'review': 0}
    o = so(w, '111-1')
    assert o['status'] == 'shipped' and o['review'] == ''
    assert w.get_product(pid)['on_hand'] == 1
    assert c.list_ship_boxes()[0]['stock'] == 4 and sb['id']
    act = w._q1('SELECT a.user_id, a.device FROM sales_line l JOIN action a ON a.id = l.shipped_action_id')
    assert (act['user_id'], act['device']) == (0, 'auto')
    assert c.auto_ship()['lines'] == 0                                  # once only
    s.upsert('amazon', [order('111-1', 'shipped')], live=True)          # next sync: nothing changes
    assert w.get_product(pid)['on_hand'] == 1 and so(w, '111-1')['review'] == ''
    assert w.check_integrity(record=False)['ok']


def test_when_the_default_cant_be_right_a_person_decides(tmp_path):
    w, c, s, boxes, pid = make(tmp_path)
    w.receive(U, pid, 1, 500, {'box_id': boxes[0]})   # oldest stock: 1 unit
    w.receive(U, pid, 5, 500, {'box_id': boxes[1]})
    s.upsert('amazon', [order('111-2'), order('111-3', sku='NOPE', qty=1)], live=True)
    s.upsert('amazon', [order('111-2', 'shipped'), order('111-3', 'shipped', sku='NOPE', qty=1)], live=True)
    r = c.auto_ship()
    assert r == {'orders': 0, 'lines': 0, 'review': 2}
    assert '2 sold, the first place holds 1' in so(w, '111-2')['review']
    assert 'not linked to a product' in so(w, '111-3')['review']
    assert w.get_product(pid)['on_hand'] == 6                           # nothing guessed
    assert so(w, '111-2')['status'] == 'open'
    # Sam moves the single unit next to the rest; the next sync's auto_ship then takes 2 from the first place.
    w.move(U, [{'product_id': pid, 'qty': 1}], {'box_id': boxes[0]}, {'box_id': boxes[1]})
    assert c.auto_ship()['lines'] == 1 and so(w, '111-2')['status'] == 'shipped'
    assert w.get_product(pid)['on_hand'] == 4


def test_none_on_shelves_and_a_person_shipping_first(tmp_path):
    w, c, s, boxes, pid = make(tmp_path)
    s.upsert('amazon', [order('111-4', qty=1), order('111-5', qty=1)], live=True)
    s.upsert('amazon', [order('111-4', 'shipped', qty=1)], live=True)
    c.auto_ship()
    assert 'none on the shelves' in so(w, '111-4')['review']
    # A person scans 111-5 out before Amazon says shipped: the sync then changes nothing.
    w.receive(U, pid, 1, 500, {'box_id': boxes[0]})
    line = w._q1("SELECT l.id FROM sales_line l JOIN sales_order o ON o.id = l.order_id WHERE o.external_id = '111-5'")
    c.ship_line(U, line['id'], {'box_id': boxes[0]})
    s.upsert('amazon', [order('111-5', 'shipped', qty=1)], live=True)
    assert so(w, '111-5')['review'] == '' and c.auto_ship()['lines'] == 0
    assert w.get_product(pid)['on_hand'] == 0


def test_orders_first_seen_already_shipped(tmp_path):
    w, c, s, boxes, pid = make(tmp_path)
    w.receive(U, pid, 5, 500, {'box_id': boxes[0]})
    # Placed and shipped between two live syncs: its stock comes off.
    s.upsert('amazon', [order('111-6', 'shipped', qty=1, days_ago=1)], live=True)
    # Old history (backfill, or a live sync seeing an old order): recorded only, stock untouched.
    s.upsert('amazon', [order('111-7', 'shipped', qty=1, days_ago=10)], live=True)
    s.upsert('amazon', [order('111-8', 'shipped', qty=1)])
    assert so(w, '111-6')['status'] == 'open' and so(w, '111-7')['status'] == 'shipped' and so(w, '111-8')['status'] == 'shipped'
    assert c.auto_ship() == {'orders': 1, 'lines': 1, 'review': 0}
    assert w.get_product(pid)['on_hand'] == 4


def test_shipped_tab_shows_newest_first(tmp_path):
    """Alex 11-11: 1,839 history orders filled the Shipped tab's 300 oldest first; one shipped today never showed."""
    w, c, s, boxes, pid = make(tmp_path)
    w.receive(U, pid, 2, 500, {'box_id': boxes[0]})
    s.upsert('amazon', [order(f'111-H{i:03d}', 'shipped', qty=1, days_ago=400 - i) for i in range(305)])   # history import
    s.upsert('amazon', [order('111-OLD', qty=1, days_ago=20)], live=True)       # ordered weeks ago, shipped today
    s.upsert('amazon', [order('111-OLD', 'shipped', qty=1, days_ago=20)], live=True)
    c.auto_ship()
    shipped = c.pick_list('shipped')
    assert len(shipped) == 300 and shipped[0]['external_id'] == '111-OLD'
    assert shipped[1]['external_id'] == '111-H304'                            # then the newest history order
    assert c.pick_list('open') == []
