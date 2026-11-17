"""Red line 11-11: bugs found before the first live Stock audit, one test each."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
sys.path.insert(0, os.path.dirname(__file__))

from test_cover_all import srv  # noqa: E402,F401  (fixture)
from test_sorting import setup  # noqa: E402
from wms import WmsError  # noqa: E402

NEW_UPC = '049000050103'


def test_next_box_gives_two_people_two_boxes(tmp_path):
    w, au, so, cat, inbound, bins = setup(tmp_path)          # A1-A3 empty
    alex, sam = so.next_box('A', 1), so.next_box('A', 2)
    assert alex['code'] == 'A1' and sam['code'] == 'A2'
    assert so.next_box('A', 1)['code'] == 'A1'                 # asking again keeps your own box
    so.next_box('A', 3)                                        # A3 to a third phone
    made = so.next_box('A', 4)
    assert made['code'] == 'A4' and made['new'] is True        # none free: a new one, not someone else's


def test_undo_of_a_wrong_pick_takes_the_barcode_off_again(tmp_path):
    w, au, so, cat, inbound, bins = setup(tmp_path)
    item = au._rows()[0]
    r = so.found(item['id'], bins['A1'], 1, scan_id='s-1', code=NEW_UPC)   # picked from a guess
    assert [p['id'] for p in w.lookup(NEW_UPC)] == [r['item']['product_id']]
    au.undo_found(item['id'], r['count_id'], 1)
    so.forget_code(r['count_id'])
    assert w.lookup(NEW_UPC) == []
    # A barcode the product already had is never taken off by an undo.
    w2 = so.found(item['id'], bins['A1'], 1, scan_id='s-2', code=NEW_UPC)
    so.found(item['id'], bins['A1'], 1, scan_id='s-3', code=NEW_UPC)  # it already has it: nothing remembered
    au.undo_found(item['id'], w2['count_id'] + 1, 1)
    so.forget_code(w2['count_id'] + 1)
    assert w.lookup(NEW_UPC)


def test_move_line_with_qty_0_moves_nothing(tmp_path):
    w, au, so, cat, inbound, bins = setup(tmp_path)
    pid = w.save_product({'sku': 'T-1', 'title': 'Thing', 'owner_id': 1})['id']
    p2 = w.save_product({'sku': 'T-2', 'title': 'Other', 'owner_id': 1})['id']
    w.receive(1, pid, 2, 500, bins['A1'])
    w.receive(1, p2, 1, 500, bins['A1'])
    w.move(1, [{'product_id': pid, 'qty': 1}, {'product_id': p2, 'qty': 0}], bins['A1'], bins['A2'])
    there = {r['product_id']: r['qty'] for r in w._q('SELECT product_id, qty FROM stock_level WHERE box_id = ? AND qty > 0', (bins['A2']['box_id'],))}
    assert there == {pid: 1}
    with pytest.raises(WmsError):
        w.move(1, [{'product_id': p2, 'qty': 0}], bins['A1'], bins['A2'])
    with pytest.raises(WmsError):
        w.remove(1, bins['A1'], [{'product_id': p2, 'qty': 0}], 'DAMAGED')


def test_history_undo_of_a_ship_is_refused(srv):  # noqa: F811
    server, call = srv
    w, com = server.wms, server.commerce
    pid = w.save_product({'sku': 'T-1', 'title': 'Thing', 'owner_id': 1})['id']
    box = w.create_boxes(1, 1)[0]['id']
    w.receive(1, pid, 2, 500, {'box_id': box})
    server.sales.upsert('amazon', [{'external_id': 'O-1', 'ordered_at': '2026-11-10', 'state': 'open', 'fulfilled_by': 'me',
                                    'lines': [{'line_key': 'L1', 'sku': 'T-1', 'asin': '', 'title': 'Thing', 'qty': 1, 'price_cents': 999}]}])
    line = w._q1('SELECT id FROM sales_line')
    com.link_line(line['id'], pid)
    assert call('Sam', f"/api/orders/lines/{line['id']}/ship", {'from': {'box_id': box}})[0] == 200
    act = w._q1('SELECT shipped_action_id AS a FROM sales_line WHERE id = ?', (line['id'],))['a']
    st, body = call('Sam', '/api/undo', {'action_id': act})
    assert st == 400 and 'shipped an order line' in body['error']
    assert w._q1('SELECT reversed_by FROM action WHERE id = ?', (act,))['reversed_by'] is None
