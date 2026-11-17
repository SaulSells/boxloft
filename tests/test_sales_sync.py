import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
from commerce import Commerce  # noqa: E402
from connections import Connections  # noqa: E402
from sales_sync import Sales, amazon_order, ebay_order  # noqa: E402
from wms import Wms  # noqa: E402


def make(tmp_path):
    wms = Wms(str(tmp_path / 'wms.db'))
    com = Commerce(wms)
    return wms, Sales(wms, com, Connections(str(tmp_path)))


def order(ext, state='open', by='me', lines=None, ms=''):
    return {'external_id': ext, 'ordered_at': '2026-11-01', 'ship_by': '2026-11-03', 'state': state, 'fulfilled_by': by,
            'market_status': ms, 'lines': lines or [{'line_key': 'L1', 'sku': 'SKU1', 'asin': 'B0POFT56O2', 'title': 'Thing',
                                                     'qty': 2, 'price_cents': 1250}]}


def test_rules_and_idempotent(tmp_path):
    wms, s = make(tmp_path)
    r = s.upsert('amazon', [order('A-FBM'), order('A-FBA', 'shipped', 'amazon'), order('A-OLD', 'shipped'),
                            order('A-CXL', 'cancelled')])
    assert r['new_orders'] == 4 and r['new_lines'] == 4 and r['fba'] == 1
    st = {o['external_id']: o for o in wms._q('SELECT * FROM sales_order')}
    assert st['A-FBM']['status'] == 'open' and st['A-FBA']['status'] == 'shipped' and st['A-FBA']['fulfilled_by'] == 'amazon'
    assert st['A-OLD']['status'] == 'shipped' and st['A-CXL']['status'] == 'cancelled'
    r = s.upsert('amazon', [order('A-FBM'), order('A-FBA', 'shipped', 'amazon')])  # same data again: nothing new
    assert r['new_orders'] == 0 and r['new_lines'] == 0
    assert wms._q1('SELECT COUNT(*) n FROM sales_line')['n'] == 4
    assert wms._q1("SELECT price_cents FROM sales_line WHERE line_key = 'L1' LIMIT 1")['price_cents'] == 1250


def test_cancel_and_shipped_elsewhere_need_no_stock_guess(tmp_path):
    wms, s = make(tmp_path)
    s.upsert('ebay', [order('E1'), order('E2')])
    r = s.upsert('ebay', [order('E1', 'cancelled'), order('E2', 'shipped')])
    assert r['cancelled'] == 1 and r['review'] == 1
    e1 = wms._q1("SELECT * FROM sales_order WHERE external_id = 'E1'")
    e2 = wms._q1("SELECT * FROM sales_order WHERE external_id = 'E2'")
    assert e1['status'] == 'cancelled'
    assert e2['status'] == 'open' and 'not scanned out' in e2['review']
    assert wms._q1('SELECT COUNT(*) n FROM action')['n'] == 0  # nothing moved stock
    r = s.upsert('ebay', [order('E2', 'shipped')])
    assert r['review'] == 0  # flagged once


def test_amazon_and_ebay_parsers():
    a = amazon_order({'AmazonOrderId': '111-1', 'PurchaseDate': '2026-11-02T03:30:00Z', 'LatestShipDate': '2026-11-04T06:59:59Z',
                      'OrderStatus': 'Unshipped', 'FulfillmentChannel': 'MFN'},
                     [{'OrderItemId': 'OI1', 'SellerSKU': 'S1', 'ASIN': 'B0X', 'Title': 'T', 'QuantityOrdered': 2,
                       'ItemPrice': {'Amount': '25.00'}}])
    assert a['ordered_at'] == '2026-11-01'  # 3:30 AM UTC is the evening before in ET
    assert a['state'] == 'open' and a['fulfilled_by'] == 'me' and a['lines'][0]['price_cents'] == 1250
    assert amazon_order({'AmazonOrderId': 'x', 'OrderStatus': 'Shipped', 'FulfillmentChannel': 'AFN'}, [])['fulfilled_by'] == 'amazon'
    e = ebay_order({'orderId': '12-3', 'creationDate': '2026-11-01T15:00:00.000Z', 'orderFulfillmentStatus': 'NOT_STARTED',
                    'cancelStatus': {'cancelState': 'NONE_REQUESTED'},
                    'lineItems': [{'lineItemId': '99', 'sku': 'MTG-EOE', 'title': 'Box', 'quantity': 1,
                                   'lineItemCost': {'value': '299.99'},
                                   'lineItemFulfillmentInstructions': {'shipByDate': '2026-11-04T04:59:59.000Z'}}]})
    assert e['state'] == 'open' and e['ship_by'] == '2026-11-03' and e['lines'][0]['price_cents'] == 29999
    assert ebay_order({'orderId': 'c', 'cancelStatus': {'cancelState': 'CANCELED'}, 'lineItems': []})['state'] == 'cancelled'


def test_lines_match_products_by_asin(tmp_path):
    wms, s = make(tmp_path)
    pid = wms.save_product({'sku': 'MYSKU', 'title': 'Thing', 'identifiers': [{'kind': 'ASIN', 'value': 'B0POFT56O2'}]})['id']
    r = s.upsert('amazon', [order('A1', lines=[{'line_key': 'x', 'sku': 'NOPE', 'asin': 'b0poft56o2', 'title': 't', 'qty': 1}])])
    assert r['unmatched'] == 0
    assert wms._q1('SELECT product_id FROM sales_line')['product_id'] == pid


def test_run_cursor_moves_only_on_success(tmp_path):
    import pytest
    from connections import ConnectionError_
    from wms import WmsError
    wms, s = make(tmp_path)
    s._amazon_orders = lambda since, progress=None: [order('R1')]
    s.run('amazon')
    st = s.state()['amazon']
    assert st['last_error'] is None and st['cursor'] and '"new_orders": 1' in st['last_result']
    good_cursor = st['cursor']

    def boom(since, progress=None):
        raise ConnectionError_('Amazon SP-API 403: denied')
    s._amazon_orders = boom
    with pytest.raises(WmsError):
        s.run('amazon')
    st = s.state()['amazon']
    assert st['cursor'] == good_cursor and '403' in st['last_error'] and st['last_ok_at']
