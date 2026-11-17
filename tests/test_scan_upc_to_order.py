"""Alex 11-11 (Tikki plush): Amazon orders carry the ASIN, not the barcode. Scanning the item's UPC must find its
order through the catalog, not send the person to New product. The catalog is faked: no network in tests."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from test_cover_all import srv  # noqa: E402,F401  (fixture)

UPC, ASIN = '043377506911', 'B03DNPJFPO'
HISTORY = ('Order ID,Order Date,ASIN,Product Name,Original Quantity,Total Amount,Order Status,Website\n'
           f'112-8591754-9337000,2026-02-22T18:03:06Z,{ASIN},Miraculous Ladybug Tikki Plush,1,14.99,Closed,Amazon.com\n')


def _fake_catalog(server, calls):
    def lookup(code):
        calls.append(code)
        return [{'asin': ASIN, 'title': 'Miraculous Ladybug Tikki Plush'}] if code == UPC else []
    server.catalog.lookup = lookup


def test_upc_scan_finds_the_amazon_order_by_asin(srv):  # noqa: F811
    server, call = srv
    server.commerce.import_purchases(HISTORY, history_mode='add')
    calls = []
    _fake_catalog(server, calls)
    st, r = call('Sam', f'/api/scan?code={UPC}')
    assert st == 200 and r['kind'] == 'unknown' and calls == [UPC]
    assert [ln['order_id'] for ln in r['lines']] == ['112-8591754-9337000']


def test_upc_scan_opens_a_product_that_has_only_the_asin(srv):  # noqa: F811
    server, call = srv
    owner = server.wms.list_owners()[0]['id']
    p = server.wms.save_product({'sku': ASIN, 'title': 'Tikki', 'owner_id': owner,
                                 'identifiers': [{'kind': 'ASIN', 'value': ASIN}]})
    _fake_catalog(server, [])
    st, r = call('Sam', f'/api/scan?code={UPC}')
    assert st == 200 and r['kind'] == 'product' and [x['id'] for x in r['products']] == [p['id']]


def test_unknown_upc_still_goes_to_new_product(srv):  # noqa: F811
    server, call = srv
    _fake_catalog(server, [])
    st, r = call('Sam', '/api/scan?code=012345678905')
    assert st == 200 and r['kind'] == 'unknown' and r['lines'] == []
