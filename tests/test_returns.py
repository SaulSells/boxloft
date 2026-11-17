import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from audit import Audit  # noqa: E402
from commerce import Commerce  # noqa: E402
from inbound import Inbound  # noqa: E402
from returns import Returns, amazon_return_state  # noqa: E402
from wms import Wms, WmsError  # noqa: E402

ASIN = 'B0DI1QY3R0'
OID = '112-4131442-0452873'


def setup(tmp_path):
    w = Wms(str(tmp_path / 'w.db'))
    com = Commerce(w)
    rt = Returns(w, com)
    inb = Inbound(w, rt)
    au = Audit(w, com, None, rt)
    return w, com, rt, inb, au


def page(statuses):
    """An order page as extension 1.4.0 sends it: one item per package block, each with its own status."""
    return {'order_id': OID, 'from_detail': True, 'ordered_at': '2026-07-16', 'seen_at': '2026-11-10',
            'status_text': statuses[-1][1], 'subtotal_cents': 119992, 'total_cents': 119992,
            'items': [{'asin': ASIN, 'title': 'EoE Collector Box', 'qty': q, 'price_cents': 29998, 'status': st}
                      for q, st in statuses]}


def line_id(w):
    return w._q1('SELECT id FROM purchase_line WHERE asin = ?', (ASIN,))['id']


def test_amazon_states():
    assert amazon_return_state('Return started Your refund will be processed when we receive your item.') == 'requested'
    assert amazon_return_state('Return received A refund will appear on your original payment method') == 'seller_received'
    assert amazon_return_state('Return complete Your return is complete. Your refund has been issued.') == 'refunded'
    assert amazon_return_state('Delivered July 21 Package was left in a secure location') is None
    assert amazon_return_state('Return window closed on August 20, 2026') is None


def test_partial_return_from_order_page(tmp_path):
    w, com, rt, inb, au = setup(tmp_path)
    inb.sync([page([(3, 'Delivered July 21 Package was left in a secure location'),
                    (1, 'Return started Your refund will be processed when we receive your item.')])])
    o = w._q1('SELECT state FROM purchase_order WHERE external_id = ?', (OID,))
    assert o['state'] == 'delivered'                       # the order isn't "returned": 3 boxes stayed
    ln = com.get_line(line_id(w))
    assert (ln['qty'], ln['returned'], ln['left_to_receive']) == (4, 1, 3)
    r = rt.for_line(line_id(w))
    assert (r['state'], r['qty'], r['expected_cents'], r['owed_cents']) == ('requested', 1, 29998, 29998)
    assert inb.known()['orders'][OID]['state'] == 'returning'   # stays on the re-read list until refunded
    # The refund comes: same page, the package now says Return complete.
    inb.sync([page([(3, 'Delivered July 21'), (1, 'Return complete Your return is complete. Your refund has been issued.')])])
    r = rt.for_line(line_id(w))
    assert (r['state'], r['owed_cents']) == ('refunded', 0)
    assert inb.known()['orders'][OID]['state'] == 'delivered'
    assert [e['state'] for e in rt.get(r['id'])['events']] == ['requested', 'refunded']


def test_receiving_respects_returns(tmp_path):
    w, com, rt, inb, au = setup(tmp_path)
    inb.sync([page([(3, 'Delivered July 21'), (1, 'Return started')])])
    pid = w.save_product({'sku': 'EOE', 'title': 'EoE', 'identifiers': [{'kind': 'ASIN', 'value': ASIN}], 'owner_id': 1})['id']
    box = w.create_boxes(1, 1)[0]['id']
    with pytest.raises(WmsError, match='only 3 left'):
        w.receive(1, pid, 4, 29998, {'box_id': box}, purchase_line_id=line_id(w))
    w.receive(1, pid, 3, 29998, {'box_id': box}, purchase_line_id=line_id(w))
    assert com.open_lines_for(pid) == []


def test_damaged_on_shelf_then_pulled(tmp_path):
    """Arrived empty: received, then sent back. The unit shows as 'to pull' until it leaves the shelf."""
    w, com, rt, inb, au = setup(tmp_path)
    inb.sync([page([(1, 'Delivered July 21')])])
    pid = w.save_product({'sku': 'EOE', 'title': 'EoE', 'identifiers': [{'kind': 'ASIN', 'value': ASIN}], 'owner_id': 1})['id']
    box = w.create_boxes(1, 1)[0]['id']
    w.receive(1, pid, 1, 29998, {'box_id': box}, purchase_line_id=line_id(w))
    assert [x['id'] for x in rt.lines_for_product(pid)] == [line_id(w)]
    r = rt.create(line_id(w), 1, 1, reason='Damaged or defective', note='box arrived empty', on_shelf=True)
    assert (r['to_pull'], r['in_stock'][0]['box_id']) == (1, box)
    with pytest.raises(WmsError):
        rt.create(line_id(w), 1, 1)                           # the line only had 1
    r = rt.pull(r['id'], 1, {'box_id': box}, 1)
    assert r['to_pull'] == 0 and r['left_to_receive'] == 0
    assert w._q1('SELECT COALESCE(SUM(qty), 0) AS q FROM stock_level WHERE box_id = ?', (box,))['q'] == 0
    assert w.check_integrity()['ok']
    act = w._q1('SELECT action_id FROM return_pull')['action_id']
    w.undo(1, act)                                            # pulled by mistake: it's back, and to pull again
    assert rt.get(r['id'])['to_pull'] == 1
    r = rt.update(r['id'], 1, state='kept')                   # decided to keep it after all
    assert (r['to_pull'], com.get_line(line_id(w))['returned']) == (0, 0)


def test_from_shelf_on_part_received_line(tmp_path):
    """4 bought, 2 received, 1 of those 2 arrived empty: still 2 to receive, 1 to pull."""
    w, com, rt, inb, au = setup(tmp_path)
    inb.sync([page([(4, 'Delivered July 21')])])
    pid = w.save_product({'sku': 'EOE', 'title': 'EoE', 'identifiers': [{'kind': 'ASIN', 'value': ASIN}], 'owner_id': 1})['id']
    box = w.create_boxes(1, 1)[0]['id']
    w.receive(1, pid, 2, 29998, {'box_id': box}, purchase_line_id=line_id(w))
    with pytest.raises(WmsError, match='Only 2'):
        rt.create(line_id(w), 3, 1, on_shelf=True)
    r = rt.create(line_id(w), 1, 1, reason='Arrived empty', on_shelf=True)
    assert (r['to_pull'], r['left_to_receive']) == (1, 2)
    assert [x['id'] for x in rt.list('pull')['returns']] == [r['id']]
    r = rt.pull(r['id'], 1, {'box_id': box}, 1)
    assert (r['to_pull'], r['left_to_receive'], r['state']) == (0, 2, 'sent')
    w.receive(1, pid, 2, 29998, {'box_id': box}, purchase_line_id=line_id(w))   # the other 2 arrive
    assert com.get_line(line_id(w))['left_to_receive'] == 0
    assert w.check_integrity()['ok']


def test_export_import(tmp_path):
    w, com, rt, inb, au = setup(tmp_path)
    with w._tx() as c:
        for oid, asin, qty, cents in (('112-7534433-4007123', 'B0417NENH0', 5, 799), (OID, ASIN, 4, 29998)):
            po = c.execute("INSERT INTO purchase_order (source, external_id, ordered_at, imported_at) VALUES ('amazon', ?, '2025-05-01', 'x')",
                           (oid,)).lastrowid
            c.execute('INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents) VALUES (?,?,?,?,?,?)',
                      (po, asin, asin, asin, qty, cents))
    # Amazon's Refund Details repeats each refund for each quantity row (2 refunds x 2 rows here).
    refunds = '''"Creation Date",Currency,"Direct Debit Refund Amount","Disbursement Type","Order ID","Payment Status",Quantity,"Refund Amount","Refund Date","Reversal Amount State","Reversal Reason","Reversal Status",Website
2025-05-12T18:21:01.003Z,USD,0,Refund,112-7534433-4007123,Completed,1,31.96,2025-05-12T22:32:41.478Z,Final,"Wrong item was sent",Completed,Amazon.com
2025-05-12T18:21:01.003Z,USD,0,Refund,112-7534433-4007123,Completed,1,7.99,2025-05-12T21:37:08.787Z,Final,"Wrong item was sent",Completed,Amazon.com
2025-05-12T18:21:00.111Z,USD,0,Refund,112-7534433-4007123,Completed,4,31.96,2025-05-12T22:32:41.478Z,Final,"Wrong item was sent",Completed,Amazon.com
2025-05-12T18:21:00.111Z,USD,0,Refund,112-7534433-4007123,Completed,4,7.99,2025-05-12T21:37:08.787Z,Final,"Wrong item was sent",Completed,Amazon.com
2024-01-30T03:29:28.026Z,USD,0,Refund,112-0000000-0000000,Completed,1,9.99,2024-01-30T06:24:27.110Z,Final,"Customer return",Completed,Amazon.com
'''
    status = '''"Carrier Package ID","Carrier Tracking ID","Charge Status","Contract Creation Date","Contract ID","Contract Unit ID","Date of Return","Gift Card","Order ID","Payment Method Service Type","Replacement Order","Retrocharge Resolution","Return Amount","Return Amount Currency","Return Authorization ID","Return Creation Date","Return Reason","Return Receivable Creation Reason","Return Receivable Initiation Date","Return Receivable State","Return Receivable Tracking ID","Return Resolution","Return Ship Option"
"Not Available",1Z6WL4KD9006584329,Not Applicable,Not Applicable,"6e42",Not Applicable,2026-07-22T21:29:20Z,Not Applicable,112-4131442-0452873,Not Applicable,Not Applicable,Not Applicable,"No Refund","Not Available",DYw5,2026-07-22T21:30:10Z,"Different from what was ordered",Not Applicable,Not Applicable,Not Applicable,Not Applicable,Refund,UPSDropOff
'''
    res = rt.import_export({'Refund Details.csv': '﻿' + refunds, 'Returns Status.csv': status})
    assert (res['refunds'], res['returns'], res['refunded_cents']) == (1, 1, 3995)
    rows = {r['asin']: r for r in rt.list('all')['returns']}
    assert (rows['B0417NENH0']['qty'], rows['B0417NENH0']['state'], rows['B0417NENH0']['refund_cents']) == (5, 'refunded', 3995)
    eoe = rows[ASIN]
    assert (eoe['qty'], eoe['state'], eoe['opened_on'], eoe['reason']) == (1, 'requested', '2026-07-22', 'Different from what was ordered')
    again = rt.import_export({'Refund Details.csv': refunds, 'Returns Status.csv': status})   # safe to repeat
    assert again['refunds'] == 1 and len(rt.list('all')['returns']) == 2
    assert rt.list()['summary']['owed_cents'] == 29998


def snapshot(au, expected=4):
    au.import_snapshot([{'asin': ASIN, 'title': 'EoE', 'expected': expected, 'cost_cents': expected * 29998,
                         'avg_cost_cents': 29998, 'orders': [], 'last_buy': '2026-08-27'}], 'test')
    return au.list()['items'][0]['id']


def test_audit_sold_elsewhere(tmp_path):
    w, com, rt, inb, au = setup(tmp_path)
    inb.sync([page([(4, 'Delivered July 21')])])
    iid = snapshot(au)
    it = au.sold(iid, 2, 'ebay', 1, price_cents=41000, sold_on='2026-09-30', order_ref='12-34567-89012')
    assert (it['sold'], it['accounted'], it['status']) == (2, 2, 'partial')
    assert com.get_line(line_id(w))['received'] == 2           # received against the order, at its cost
    cust = w._q1("SELECT id FROM location WHERE code = 'CUSTOMER'")['id']
    assert w._q1('SELECT qty FROM stock_level WHERE location_id = ?', (cust,))['qty'] == 2
    sl = w._q1('SELECT l.qty, l.price_cents, o.channel, o.status FROM sales_line l JOIN sales_order o ON o.id = l.order_id')
    assert (sl['qty'], sl['price_cents'], sl['channel'], sl['status']) == (2, 41000, 'ebay', 'shipped')
    out = au.outcomes()['outcomes']
    assert [(o['outcome'], o['channel'], o['units'], o['cost_cents'], o['sales_cents']) for o in out] == \
        [('sold_elsewhere', 'ebay', 2, 59996, 82000)]
    with pytest.raises(WmsError, match='Only 2 of 4'):
        au.sold(iid, 3, 'ebay', 1)
    oid = [h for h in it['history'] if h['kind'] == 'sold'][0]['outcome_id']
    it = au.undo_outcome(iid, oid, 1)
    assert it['sold'] == 0 and w.check_integrity()['ok']
    assert w._q1('SELECT status FROM sales_order')['status'] == 'cancelled'


def test_audit_sent_back_claims_amazon_return_first(tmp_path):
    w, com, rt, inb, au = setup(tmp_path)
    inb.sync([page([(3, 'Delivered July 21'), (1, 'Return started')])])
    iid = snapshot(au)
    it = au.sent_back(iid, 1, 1)
    assert it['sent_back'] == 1
    assert rt.for_line(line_id(w))['qty'] == 1                 # Amazon's return, claimed, not doubled
    it = au.sent_back(iid, 1, 1, state='refunded', reason='Wrong item sent')
    assert rt.for_line(line_id(w))['qty'] == 2                 # a second one is a new unit going back
    assert com.get_line(line_id(w))['left_to_receive'] == 2
    oid = it['history'][0]['outcome_id']
    it = au.undo_outcome(iid, oid, 1)
    assert (it['sent_back'], rt.for_line(line_id(w))['qty']) == (1, 1)


def test_cancelled_orders_leave_the_audit(tmp_path):
    w, com, rt, inb, au = setup(tmp_path)
    inb.sync([dict(page([(1, 'Delivered July 21')]), order_id='112-8481257-3915513', status_text='Cancelled',
                   items=[{'asin': ASIN, 'title': 'x', 'qty': 1, 'price_cents': 100900, 'status': 'Cancelled'}])])
    au.import_snapshot([{'asin': ASIN, 'title': 'EoE', 'expected': 3, 'cost_cents': 3 * 29998, 'avg_cost_cents': 29998,
                         'last_buy': '2026-08-27', 'orders': [{'id': '112-8481257-3915513', 'date': '2026-08-22', 'units': 1},
                                                              {'id': '112-0000000-0000001', 'date': '2026-08-01', 'units': 2}]}], 'test')
    it = au.list()['items'][0]
    assert (it['expected'], it['exp_cancelled'], it['cost_cents']) == (2, 1, 2 * 29998)
    assert [o.get('cancelled', False) for o in it['orders']] == [True, False]


def test_quantity_cut_caps_audit_row_with_a_record(tmp_path):
    """Alex 11-10: 'I ordered 18 but 17 were cancelled'. The CSV said 18; Amazon's order now has 1."""
    w, com, rt, inb, au = setup(tmp_path)
    inb.sync([dict(page([(1, 'Delivered July 21')]), order_id='114-0052260-1403182',
                   items=[{'asin': ASIN, 'title': 'x', 'qty': 1, 'price_cents': 6494, 'status': 'Delivered July 21'}])])
    au.import_snapshot([{'asin': ASIN, 'title': 'Watch', 'expected': 20, 'cost_cents': 20 * 6494, 'avg_cost_cents': 6494,
                         'last_buy': '2026-08-27', 'orders': [{'id': '114-0052260-1403182', 'date': '2026-01-13', 'units': 18},
                                                              {'id': '112-0000000-0000009', 'date': '2026-01-01', 'units': 2}]}], 'test')
    iid = au.list()['items'][0]['id']
    # 11-11: the row counts from Amazon's order lines, so the cut is already in it (1 of 18). The sheet's 2 on an order
    # Boxloft has no line for are not counted (on live data every sheet order has its lines).
    it = au.item(iid)
    assert (it['expected'], it['sheet_expected'], it['count'], it['cost_cents']) == (1, 20, 'own', 6494)
    assert au.cap_cuts() == []                                 # nothing left to cap
    # A sheet row with no Amazon lines keeps the old cap: dry run, apply, a second run adds nothing.
    au.import_snapshot([{'asin': 'B0NOLINES1', 'title': 'Clock', 'expected': 5, 'avg_cost_cents': 100, 'cost_cents': 500,
                         'last_buy': '2026-08-27', 'orders': [{'id': '114-0052260-1403182', 'date': '2026-01-13', 'units': 5}]}], 'test')
    with w._tx() as c:
        po = w._q1("SELECT id FROM purchase_order WHERE external_id = '114-0052260-1403182'")['id']
        c.execute("INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents, cancelled) VALUES (?, 'n', 'B0NOLINES1', 'Clock', 2, 100, 1)", (po,))
    assert au.cap_cuts() == []                                 # its only line is cancelled: _cancelled covers it
    with w._tx() as c:   # the old records stay as history, append-only
        c.execute("INSERT INTO audit_adjust (item_id, at, order_ref, units, snap_units, live_units) VALUES (?, 'x', 'o', -1, 2, 1)", (iid,))
    assert au.item(iid)['expected'] == 1                       # an old cut on a counted row changes nothing
    with pytest.raises(Exception):
        w._conn().execute('DELETE FROM audit_adjust')         # the record is append-only


def test_delivered_not_scanned_joins_audit(tmp_path):
    w, com, rt, inb, au = setup(tmp_path)
    inb.sync([page([(4, 'Delivered July 21')])])
    snapshot(au)                                               # snapshot buys run to 2026-08-27
    late = {'order_id': '112-1093125-2013476', 'from_detail': True, 'ordered_at': '2026-09-18', 'seen_at': '2026-11-10',
            'status_text': 'Delivered October 27', 'subtotal_cents': 4998, 'total_cents': 4998,
            'items': [{'asin': 'B0ZVJIOJWS', 'title': 'NECA Ultimate Ash', 'qty': 2, 'price_cents': 2499, 'status': 'Delivered October 27'}]}
    inb.sync([late])
    rows = {r['asin']: r for r in au.list()['items']}
    assert rows[ASIN]['exp_delivered'] == 0                    # the July order is inside the snapshot window
    ash = rows['B0ZVJIOJWS']
    assert (ash['expected'], ash['exp_delivered'], ash['status']) == (2, 2, 'todo')
    assert ash['orders'][0]['delivered'] and ash['orders'][0]['delivered_on'] == '2026-10-27'
    pid = w.save_product({'sku': 'ASH', 'title': 'Ash', 'identifiers': [{'kind': 'ASIN', 'value': 'B0ZVJIOJWS'}], 'owner_id': 1})['id']
    box = w.create_boxes(1, 1)[0]['id']
    lid = w._q1("SELECT id FROM purchase_line WHERE asin = 'B0ZVJIOJWS'")['id']
    w.receive(1, pid, 1, 2499, {'box_id': box}, purchase_line_id=lid)    # scanned in on Receive: leaves the list
    assert au.item(ash['id'])['expected'] == 1
    it = au.found(ash['id'], 1, {'box_id': box}, 1)                         # found by the audit: counts as found
    assert (it['expected'], it['found'], it['status']) == (1, 1, 'found')


def test_audit_histories_match_each_item(tmp_path):
    """The Stock audit page reads every shown card's saves in one request (Alex 11-12: 40 requests queued the app)."""
    w, com, rt, inb, au = setup(tmp_path)
    inb.sync([page([(3, 'Delivered July 21'), (1, 'Return started')])])
    iid = snapshot(au)
    au.sent_back(iid, 1, 1)
    au.sent_back(iid, 1, 1, state='refunded', reason='Wrong item sent')
    hs = au.histories([iid, 999999])
    assert hs[iid] == au.item(iid)['history'] and len(hs[iid]) == 2
    assert hs[999999] == []
