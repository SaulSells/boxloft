import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from commerce import Commerce, fits  # noqa: E402
from wms import Wms, WmsError  # noqa: E402

U = 1

AMAZON_HISTORY = '''ASIN,Order Date,Order ID,Order Status,Original Quantity,Product Name,Total Amount,Website
B0AAAAAAAA,2026-09-01T10:00:00Z,111-1,Closed,2,Lego set,"53.00",Amazon.com
B0AAAAAAAA,2026-09-01T10:00:00Z,111-1,Closed,1,Lego set,"26.50",Amazon.com
B0BBBBBBBB,2026-09-02T10:00:00Z,111-2,Cancelled,1,Never came,"9.99",Amazon.com
'''
TRACKER = '''Order ID,Date,Account Email,Address,Total,COG,Box Status,Shipment Status,Product ASIN,Product Title,Product Quantity
111-1,09/01/2026,buyer1@example.com,x,79.50,26.50,Delivered,Delivered,B0AAAAAAAA,Lego set,3
222-9,09/05/2026,buyer2@example.com,x,40.00,10.00,Delivered,Delivered,B0CCCCCCCC,Socks,4
333-1,09/06/2026,buyer2@example.com,x,5.00,5.00,Returned,,B0DDDDDDDD,Returned thing,1
'''
GENERIC = '''Retailer,Order #,Date,Item,UPC,Qty,Unit Cost
Walmart,W-77,10/02/2026,Hot Wheels 5 pack,027084046788,6,$4.97
'''


@pytest.fixture
def env(tmp_path):
    w = Wms(str(tmp_path / 'wms.db'))
    return w, Commerce(w)


def test_imports_three_formats_and_dedupe(env):
    w, c = env
    r = c.import_purchases(AMAZON_HISTORY)
    assert (r['format'], r['new_lines'], r['spend_cents']) == ('amazon_history', 1, 7950)
    r = c.import_purchases(TRACKER)  # 111-1 already known from Amazon's own file; returned line dropped
    assert (r['format'], r['new_lines'], r['already_had']) == ('tracker', 1, 1)
    r = c.import_purchases(GENERIC)
    assert (r['format'], r['new_lines'], r['spend_cents']) == ('generic', 1, 2982)
    lines = {l['order_id']: l for l in c.list_purchases('all')['lines']}
    assert lines['111-1']['qty'] == 3 and lines['111-1']['unit_cost_cents'] == 2650
    assert lines['W-77']['source'] == 'walmart' and lines['W-77']['upc'] == '027084046788'
    assert c.import_purchases(AMAZON_HISTORY)['new_lines'] == 0


def test_receive_against_purchase_line(env):
    w, c = env
    c.import_purchases(AMAZON_HISTORY)
    loc = w.create_locations(['A-01-01'])[0]['id']
    box = w.create_boxes(1, U, loc)[0]['id']
    pid = w.save_product({'sku': 'L1', 'title': 'Lego set', 'identifiers': [{'kind': 'ASIN', 'value': 'B0AAAAAAAA'}]})['id']
    [line] = c.open_lines_for(pid)
    w.receive(U, pid, 2, line['unit_cost_cents'], {'box_id': box}, purchase_line_id=line['id'])
    assert c.open_lines_for(pid)[0]['received'] == 2
    with pytest.raises(WmsError, match='only 1 left'):
        w.receive(U, pid, 2, 2650, {'box_id': box}, purchase_line_id=line['id'])
    r = w.receive(U, pid, 1, 2650, {'box_id': box}, purchase_line_id=line['id'])
    assert c.open_lines_for(pid) == []
    assert w.get_product(pid)['receipts'][0]['purchased_at'] == '2026-09-01'
    w.undo(U, r['action_id'])  # undoing a receipt reopens the line
    assert c.open_lines_for(pid)[0]['received'] == 2


def test_box_fit_and_recommendation(env):
    w, c = env
    assert fits([10, 4, 2], [5, 11, 3])
    assert not fits([10, 4, 2], [10, 5, 3])  # no room for padding on the long side
    for name, dims, stock in (('Small', (6, 4, 4), 50), ('Medium', (12, 9, 4), 0), ('Large', (14, 10, 6), 10)):
        c.save_ship_box({'name': name, 'inner_l_in': dims[0], 'inner_w_in': dims[1], 'inner_h_in': dims[2], 'stock': stock})
    assert c.recommend_box([([5, 3, 2], 1)])['box']['name'] == 'Small'
    rec = c.recommend_box([([11, 8, 3], 1)])  # Medium fits but is out of stock
    assert rec['box']['name'] == 'Large'
    assert c.recommend_box([(None, 1)])['box'] is None


def test_sales_import_link_and_ship(env):
    w, c = env
    loc = w.create_locations(['A-01-01'])[0]['id']
    box = w.create_boxes(1, U, loc)[0]['id']
    pid = w.save_product({'sku': 'MY-SKU', 'title': 'Thing', 'pkg_l_mm': 100, 'pkg_w_mm': 80, 'pkg_h_mm': 50})['id']
    w.receive(U, pid, 3, 500, {'box_id': box})
    sb = c.save_ship_box({'name': 'Small', 'inner_l_in': 6, 'inner_w_in': 5, 'inner_h_in': 4, 'stock': 2})
    report = ('order-id\torder-item-id\tpurchase-date\tsku\tproduct-name\tquantity-purchased\n'
              '114-1\t9001\t2026-10-30T12:00:00Z\tMY-SKU\tThing\t2\n'
              '114-2\t9002\t2026-10-30T12:00:00Z\tOTHER\tUnknown thing\t1\n')
    r = c.import_sales(report, 'amazon')
    assert (r['new_orders'], r['new_lines'], r['unmatched']) == (2, 2, 1)
    orders = {o['external_id']: o for o in c.pick_list()}
    line = orders['114-1']['lines'][0]
    assert line['where'][0]['box_id'] == box
    assert orders['114-1']['recommendation']['box']['name'] == 'Small'
    res = c.ship_line(U, line['id'], {'box_id': box}, ship_box_id=sb['id'], box_fit='ok')
    assert res['order_done']
    assert w.get_product(pid)['on_hand'] == 1
    assert c.list_ship_boxes()[0]['stock'] == 1
    with pytest.raises(WmsError, match='Link this line'):
        c.ship_line(U, orders['114-2']['lines'][0]['id'], {'box_id': box})
    assert w.check_integrity(record=False)['ok']


def test_purchase_totals_count_stock_only(env):
    """Alex 11-11: 12,000 units for $95,000 was under $10 a unit. 9,800 of those units were ship boxes (supplies)
    and $18,000 was store emails hidden as Not inventory. The cards and the list count stock only."""
    w, c = env
    c.import_purchases(GENERIC)                                    # 6 Hot Wheels at $4.97: stock
    c.import_purchases('''Retailer,Order #,Date,Item,UPC,Qty,Unit Cost
The Boxery,BX-1,10/03/2026,8x6x4 Corrugated Shipping Boxes,,1500,$0.43
Stripe,ST-1,10/04/2026,Monthly invoice,,1,$900.00
Shop,PEND-1,10/05/2026,Waiting for a person,,2,$10.00
Shop,CXL-1,10/06/2026,Amazon cancelled it,,30,$20.00
''')
    with w._tx() as t:
        t.execute("UPDATE purchase_order SET kind = 'supplies' WHERE external_id = 'BX-1'")
        t.execute("UPDATE purchase_order SET hidden = 1 WHERE external_id = 'ST-1'")
        t.execute("UPDATE purchase_order SET review = 'New store order' WHERE external_id = 'PEND-1'")
        t.execute("UPDATE purchase_order SET state = 'cancelled' WHERE external_id = 'CXL-1'")
    out = c.list_purchases('all')
    t = out['totals']
    assert (t['lines'], t['bought'], t['spend']) == (1, 6, 2982)
    assert [l['order_id'] for l in out['lines']] == ['W-77']
    assert (t['supplies']['orders'], t['supplies']['units'], t['supplies']['spend']) == (1, 1500, 64500)
