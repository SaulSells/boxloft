"""eBay listings (Alex 11-11): every active listing from the Trading API's GetMyeBaySelling, keyed on the item ID.
A Custom label that is a Boxloft code links the listing and its sales with no tap; a listing without one is linked
once by a person (a title suggestion), and its sales follow by ITEM-<item id>.
The XML here follows eBay's documented GetMyeBaySelling response; it has not been checked against a live account yet."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import pytest  # noqa: E402

import listings as listings_mod  # noqa: E402
from connections import ConnectionError_  # noqa: E402
from listings import parse_ebay_selling  # noqa: E402
from sales_sync import ebay_order  # noqa: E402
from test_cover_all import srv  # noqa: E402,F401
from test_listings import setup  # noqa: E402
from test_stock_vs_listed import box, product  # noqa: E402

NS = 'urn:ebay:apis:eBLBaseComponents'


def item(item_id, title, qty_avail, price='24.99', sku='', variations=''):
    return f"""<Item><BuyItNowPrice currencyID="USD">0.0</BuyItNowPrice><ItemID>{item_id}</ItemID>
      <ListingDetails><StartTime>2026-10-02T15:04:05.000Z</StartTime><ViewItemURL>https://www.ebay.com/itm/{item_id}</ViewItemURL></ListingDetails>
      <ListingType>FixedPriceItem</ListingType><Quantity>{qty_avail + 1}</Quantity>
      <SellingStatus><CurrentPrice currencyID="USD">{price}</CurrentPrice><QuantitySold>1</QuantitySold></SellingStatus>
      {f'<SKU>{sku}</SKU>' if sku else ''}<Title>{title}</Title><QuantityAvailable>{qty_avail}</QuantityAvailable>
      {variations}</Item>"""


def page(items, page_no=1, pages=1, ack='Success'):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<GetMyeBaySellingResponse xmlns="{NS}"><Timestamp>2026-11-12T01:00:00.000Z</Timestamp><Ack>{ack}</Ack>
  <Version>1271</Version><Build>E1271_CORE_API</Build>
  <ActiveList><ItemArray>{''.join(items)}</ItemArray>
    <PaginationResult><TotalNumberOfPages>{pages}</TotalNumberOfPages><TotalNumberOfEntries>{len(items)}</TotalNumberOfEntries></PaginationResult>
  </ActiveList></GetMyeBaySellingResponse>"""


VARIATIONS = """<Variations>
  <Variation><SKU>TEE-S</SKU><StartPrice currencyID="USD">12.00</StartPrice><Quantity>3</Quantity>
    <SellingStatus><QuantitySold>1</QuantitySold></SellingStatus></Variation>
  <Variation><SKU>TEE-L</SKU><StartPrice currencyID="USD">14.00</StartPrice><Quantity>2</Quantity>
    <SellingStatus><QuantitySold>2</QuantitySold></SellingStatus></Variation></Variations>"""


def test_parse_items_and_variations():
    rows, pages = parse_ebay_selling(page([
        item('256001', 'Vintage Star Wars Card Lot', 4, sku='P-EBAY'),
        item('256002', 'NECA Creepshow The Creep Clothed Figure', 1),
        item('256003', 'Band Tee', 0, variations=VARIATIONS)], pages=3))
    assert pages == 3
    got = {r['sku']: (r['listing_id'], r['qty'], r['price_cents'], r['code_type'], r['code']) for r in rows}
    assert got == {'ITEM-256001': ('256001', 4, 2499, 'SKU', 'P-EBAY'),
                   'ITEM-256002': ('256002', 1, 2499, '', ''),
                   'ITEM-256003/TEE-S': ('256003', 2, 1200, 'SKU', 'TEE-S'),
                   'ITEM-256003/TEE-L': ('256003', 0, 1400, 'SKU', 'TEE-L')}
    assert all(r['status'] == 'Active' and r['fulfillment'] == 'FBM' and r['asin'] == '' for r in rows)
    assert rows[0]['opened_on'] == '2026-10-02' and rows[0]['title'] == 'Vintage Star Wars Card Lot'


def test_an_ebay_error_is_raised_with_its_message():
    bad = f"""<GetMyeBaySellingResponse xmlns="{NS}"><Ack>Failure</Ack><Errors><ShortMessage>Auth token is invalid.</ShortMessage>
      <LongMessage>Validation of the authentication token in API request failed.</LongMessage></Errors></GetMyeBaySellingResponse>"""
    with pytest.raises(ConnectionError_, match='authentication token'):
        parse_ebay_selling(bad)
    with pytest.raises(ConnectionError_, match='not XML'):
        parse_ebay_selling('network error: timed out')


class FakeConn:
    def ebay_user_token(self):
        return 'v^1.1#fake'


def test_refresh_reads_every_page_links_by_custom_label_and_ends_gone_listings(tmp_path, monkeypatch):
    w, com, au, li = setup(tmp_path)
    li.connections = FakeConn()
    pid = product(w, 'P-EBAY', 'Vintage Star Wars Card Lot')
    w.receive(1, pid, 7, 500, {'box_id': box(w, 'E1')})
    pages = {1: page([item('256001', 'Vintage Star Wars Card Lot', 4, sku='P-EBAY')], 1, 2),
             2: page([item('256002', 'Pokemon Binder', 1)], 2, 2)}
    asked = []

    def fake_http(method, url, headers, data, timeout=20, raw_text=False):
        assert url == listings_mod.EBAY_TRADING and raw_text
        assert headers['X-EBAY-API-CALL-NAME'] == 'GetMyeBaySelling' and headers['X-EBAY-API-IAF-TOKEN'] == 'v^1.1#fake'
        n = int(data.decode().split('<PageNumber>')[1].split('<')[0])
        asked.append(n)
        return 200, pages[n]
    monkeypatch.setattr(listings_mod, '_http', fake_http)
    monkeypatch.setattr(listings_mod.time, 'sleep', lambda s: None)
    out = li.run(channel='ebay')
    assert asked == [1, 2] and out['listings'] == 2 and out['new'] == 2
    rows = {r['sku']: r for r in w._q("SELECT * FROM listing WHERE channel = 'ebay'")}
    assert rows['ITEM-256001']['product_id'] == pid            # Custom label = the Boxloft SKU
    assert rows['ITEM-256002']['product_id'] is None
    assert w._q1("SELECT last_ok_at FROM sync_state WHERE channel = 'ebay_listings'")['last_ok_at']
    assert not li.due(60, 'ebay') and li.due(60, 'amazon')    # each channel keeps its own clock
    e = li.stock_vs_listed('ebay')
    assert e['synced'] and [(r['reason'], r['listed'], r['unlisted']) for r in e['rows']] == [('short', 4, 3)]
    # The binder sold out: it is missing from the next read, so it is gone, not deleted.
    pages[1], pages[2] = page([item('256001', 'Vintage Star Wars Card Lot', 3, sku='P-EBAY')]), None
    out = li.run(channel='ebay')
    assert out['gone'] == 1 and asked[-1] == 1
    assert w._q1("SELECT gone_at FROM listing WHERE sku = 'ITEM-256002'")['gone_at']


def test_a_failed_read_is_kept_for_the_page(tmp_path, monkeypatch):
    w, com, au, li = setup(tmp_path)
    li.connections = FakeConn()
    monkeypatch.setattr(listings_mod, '_http', lambda *a, **k: (503, 'Service Unavailable'))
    with pytest.raises(Exception, match='eBay listings 503'):
        li.run(channel='ebay')
    st = w._q1("SELECT * FROM sync_state WHERE channel = 'ebay_listings'")
    assert 'eBay listings 503' in st['last_error'] and st['last_ok_at'] is None


def test_ebay_sale_without_custom_label_carries_the_item_key():
    o = ebay_order({'orderId': '12-34567-89012', 'creationDate': '2026-11-11T18:00:00.000Z',
                    'orderFulfillmentStatus': 'NOT_STARTED', 'cancelStatus': {'cancelState': 'NONE_REQUESTED'},
                    'lineItems': [{'lineItemId': '1001', 'legacyItemId': '256002', 'title': 'Pokemon Binder', 'quantity': 1,
                                   'lineItemCost': {'value': '19.99'}},
                                  {'lineItemId': '1002', 'legacyItemId': '256001', 'sku': 'P-EBAY', 'title': 'Card Lot',
                                   'quantity': 1, 'lineItemCost': {'value': '24.99'}}]})
    assert [(l['sku'], l['price_cents']) for l in o['lines']] == [('ITEM-256002', 1999), ('P-EBAY', 2499)]


def test_link_once_and_the_sale_follows_to_its_box(srv):
    """No Custom label: the eBay product shows a title suggestion; one tap links it, the open sale finds the product,
    and the pick list names the E box it sits in."""
    server, call = srv
    w, li = server.wms, server.listings
    pid = product(w, 'P-BINDER', 'Pokemon Binder 9 Pocket Pikachu')
    w.receive(1, pid, 2, 400, {'box_id': box(w, 'E7')})
    li.upsert('ebay', parse_ebay_selling(page([item('256002', 'Pokemon 9-Pocket Binder Pikachu', 1)]))[0])
    server.sales.upsert('ebay', [ebay_order({
        'orderId': '12-1', 'creationDate': '2026-11-11T18:00:00.000Z', 'orderFulfillmentStatus': 'NOT_STARTED',
        'lineItems': [{'lineItemId': '9', 'legacyItemId': '256002', 'title': 'Pokemon Binder', 'quantity': 1,
                       'lineItemCost': {'value': '19.99'}}]})])
    st, out = call('Sam', '/api/listings/stock?channel=ebay')
    row = next(r for r in out['rows'] if r['product_id'] == pid)
    assert row['reason'] == 'not_listed' and row['suggestions'][0]['sku'] == 'ITEM-256002'
    st, k = call('Sam', '/api/listings/link', {'product_id': pid, 'listing_id': row['suggestions'][0]['listing_id'],
                                               'seen': {'updated_at': row['updated_at'], 'asin': row['asin']}})
    assert st == 200 and k['asin'] == '' and k['sales_lines_linked'] == 1
    assert w._q1("SELECT product_id FROM sales_line WHERE sku = 'ITEM-256002'")['product_id'] == pid
    after = next(r for r in call('Sam', '/api/listings/stock?channel=ebay')[1]['rows'] if r['product_id'] == pid)
    assert (after['reason'], after['listed']) == ('short', 1)
    line = next(ln for o in server.commerce.pick_list() for ln in o['lines'] if ln['sku'] == 'ITEM-256002')
    assert line['product_id'] == pid and 'E7' in str(line['where'])   # the order card shows the box
