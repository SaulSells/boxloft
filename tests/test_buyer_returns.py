"""Buyer returns (Alex 11-16, buyer_returns.py): scan the return label a buyer's box came back on, see the item and why.

The report text has Amazon's real header (GET_FLAT_FILE_RETURNS_DATA_BY_RETURN_DATE, read on live 11-16); the rows are
made up. The UPS ID is the label's barcode itself; the USPS one arrives inside IMpb (420 + ZIP + the ID).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from buyer_returns import BuyerReturns, parse_amazon  # noqa: E402
from labels import Labels  # noqa: E402
from test_sorting import setup, units_in  # noqa: E402
from wms import Conflict, WmsError  # noqa: E402

HEAD = ['Order ID', 'Order date', 'Return request date', 'Return request status', 'Amazon RMA ID', 'Merchant RMA ID', 'Label type',
        'Label cost', 'Currency code', 'Return carrier', 'Tracking ID', 'Label to be paid by', 'A-to-Z Claim', 'Is prime', 'ASIN',
        'Merchant SKU', 'Item Name', 'Return quantity', 'Return Reason', 'In policy', 'Return type', 'Resolution', 'Invoice number',
        'Return delivery date', 'Order Amount', 'Order quantity', 'SafeT Action reason', 'SafeT claim id', 'SafeT claim state',
        'SafeT claim creation time', 'SafeT claim reimbursement amount', 'Refunded Amount', 'Order Item ID']
UPS, USPS = '1Z9X8W70340000001', '9322190123456789012345'


def report(rows):
    lines = ['\t'.join(HEAD)]
    for r in rows:
        d = dict.fromkeys(HEAD, '')
        d.update(r)
        lines.append('\t'.join(d[h] for h in HEAD))
    return '﻿' + '\n'.join(lines) + '\n'


def row(order, rma, asin, sku, qty, reason, tracking, carrier='UPS', item='11111'):
    return {'Order ID': order, 'Order date': '2026-10-30T14:02:11+00:00', 'Return request date': '2026-11-10T09:00:00+00:00',
            'Return request status': 'Approved', 'Amazon RMA ID': rma, 'Return carrier': carrier, 'Tracking ID': tracking,
            'ASIN': asin, 'Merchant SKU': sku, 'Item Name': 'Lantern', 'Return quantity': str(qty), 'Return Reason': reason,
            'Resolution': 'StandardRefund', 'Refunded Amount': '24.99', 'Order Item ID': item, 'Label type': 'AmazonPrePaidLabel'}


@pytest.fixture
def wh(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    lb = Labels(w)
    so.labels = lb
    br = BuyerReturns(w)
    lb.returns = br
    p = w.save_product({'sku': 'LANTERN-1', 'title': 'Brass camp lantern', 'identifiers': [{'kind': 'ASIN', 'value': 'B0LANTERN1'}]})['id']
    w.receive(2, p, 3, 1500, boxes['A1'])
    # Sold 2 on Amazon order 113-1, shipped here from A1 (the ledger's move to CUSTOMER is the sale's shipped action)
    a = w.remove(2, boxes['A1'], [{'product_id': p, 'qty': 2}], 'CUSTOMER')['action_id']
    with w._tx() as c:
        o = c.execute("INSERT INTO sales_order (channel, external_id, ordered_at, status, imported_at) VALUES ('amazon', '113-1', '2026-10-30', 'shipped', 'x')").lastrowid
        c.execute("INSERT INTO sales_line (order_id, line_key, sku, asin, title, qty, product_id, shipped_action_id) VALUES (?, 'k', 'LANTERN-1', 'B0LANTERN1', 'Lantern', 2, ?, ?)",
                  (o, p, a))
    br.save(parse_amazon(report([row('113-1', 'RMA1', 'B0LANTERN1', 'LANTERN-1', 2, 'CR-DAMAGED_BY_CARRIER', UPS)])))
    return {'w': w, 'so': so, 'lb': lb, 'br': br, 'p': p, 'boxes': boxes}


def test_report_rows_parse_with_reason_and_tracking():
    rows = parse_amazon(report([row('113-1', 'RMA1', 'b0lantern1', 'LANTERN-1', 2, 'AMZ-PG-BAD-DESC', '1z9x8w70340000001')]))
    assert rows[0]['asin'] == 'B0LANTERN1' and rows[0]['tracking_id'] == UPS and rows[0]['qty'] == 2
    assert rows[0]['requested_on'] == '2026-11-10' and rows[0]['refunded_cents'] == 2499


def test_return_label_shows_item_reason_and_where_it_shipped_from(wh):
    r = wh['lb'].scan(UPS, 2)
    assert r['kind'] == 'label_return' and r['channel_name'] == 'Amazon' and r['left'] == 2
    i = r['items'][0]
    assert i['title'] == 'Brass camp lantern' and i['reason'] == 'Item and shipping box both damaged' and i['order_id'] == '113-1'
    assert i['shipped_from']['name'] == 'A1' and i['product_id'] == wh['p']
    assert wh['w']._q1('SELECT result, tracking_id FROM label_scan') == {'result': 'return', 'tracking_id': UPS}
    assert wh['lb'].is_label(UPS)


def test_usps_return_label_matches_inside_impb(wh):
    wh['br'].save(parse_amazon(report([row('113-2', 'RMA2', 'B0LANTERN1', 'LANTERN-1', 1, 'CR-UNWANTED_ITEM', USPS, 'USPS', '2')])))
    r = wh['lb'].match('42090211' + USPS)
    assert r['kind'] == 'label_return' and r['items'][0]['reason'] == 'No longer needed'


def test_box_page_scan_of_a_return_label_shows_the_card(wh):
    r = wh['so'].put(wh['boxes']['A2'], 2, scan_id='s1', code=UPS)
    assert r['kind'] == 'label_return' and r['items'][0]['left'] == 2


def test_restock_one_damage_one_then_undo(wh):
    w, br, boxes, p = wh['w'], wh['br'], wh['boxes'], wh['p']
    rid = br.card(UPS)['items'][0]['return_id']
    r = br.take(rid, 2, 1, 'good', boxes['A1'], seen_left=2)
    assert units_in(w, boxes['A1'], p) == 2 and r['items'][0]['left'] == 1
    with pytest.raises(Conflict):   # a screen that still shows 2 left
        br.take(rid, 3, 1, 'good', boxes['A1'], seen_left=2)
    r = br.take(rid, 2, 1, 'damaged', seen_left=1)
    assert r['left'] == 0 and units_in(w, boxes['A1'], p) == 2
    assert w._q1("SELECT qty FROM stock_level s JOIN location l ON l.id = s.location_id WHERE l.code = 'DAMAGED'")['qty'] == 1
    with pytest.raises(WmsError):
        br.take(rid, 2, 1, 'good', boxes['A1'])
    w.undo(2, r['action_id'])   # History's Undo: the damaged unit is open again
    assert br.card(UPS)['left'] == 1 and w.check_integrity()['ok']
    assert 'Buyer return: 1 x Brass camp lantern back in A1' in [a['summary'] for a in w.list_actions(limit=10)]


def test_a_return_with_no_item_in_the_app_says_so(wh):
    wh['br'].save(parse_amazon(report([row('113-9', 'RMA9', 'B0NOTHERE1', 'X-9', 1, 'CR-DEFECTIVE', '1Z9X8W70340000099', item='9')])))
    c = wh['br'].card('1Z9X8W70340000099')
    assert c['items'][0]['product_id'] is None and c['items'][0]['reason'] == 'Defective, does not work'
    with pytest.raises(WmsError, match='Add the item first'):
        wh['br'].take(c['items'][0]['return_id'], 2, 1, 'good', wh['boxes']['A1'])


def test_a_new_read_updates_status_and_keeps_what_we_took_in(wh):
    br = wh['br']
    rid = br.card(UPS)['items'][0]['return_id']
    br.take(rid, 2, 1, 'good', wh['boxes']['A1'])
    x = row('113-1', 'RMA1', 'B0LANTERN1', 'LANTERN-1', 2, 'CR-DAMAGED_BY_CARRIER', UPS)
    x['Resolution'] = 'RefundAtFirstScan'
    assert br.save(parse_amazon(report([x])))['new'] == 0
    c = br.card(UPS)
    assert c['items'][0]['resolution'] == 'Refunded at the first carrier scan' and c['left'] == 1
