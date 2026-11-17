"""Amazon listings get a photo (Alex 11-13: "Orders tab. Why no pictures?").

An order line with no Boxloft item shows its listing's photo, and Amazon's listings report has none. The app has no
Catalog role (SP-API 403), so the photo comes from Keepa by ASIN: the cache first, then one call per 100 ASINs.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from commerce import Catalog, Commerce  # noqa: E402
from listings import Listings  # noqa: E402
from sales_sync import Sales  # noqa: E402
from connections import Connections  # noqa: E402
from wms import Wms, WmsError, now_utc  # noqa: E402


def amazon(li, sku, asin, image_url=''):
    li.upsert('amazon', [{'sku': sku, 'asin': asin, 'title': sku, 'fulfillment': 'FBM', 'status': 'Active', 'qty': 1,
                          'price_cents': 999, 'listing_id': '', 'opened_on': '2026-10-01', 'condition': 'New',
                          'code_type': '1', 'code': asin, 'image_url': image_url}], full=False)


def photo(w, sku):
    return w._q1('SELECT image_url FROM listing WHERE sku = ?', (sku,))['image_url']


def setup(tmp_path, monkeypatch, keepa):
    monkeypatch.setenv('KEEPA_API_KEY', 'test')
    w = Wms(str(tmp_path / 'w.db'))
    com = Commerce(w)
    Sales(w, com, Connections(str(tmp_path)))
    li = Listings(w, com)
    cat = Catalog(w, str(tmp_path / 'photos'))
    calls = []

    def fake(path, **params):
        calls.append(params['asin'].split(','))
        return keepa(params['asin'].split(','))
    cat._keepa = fake
    li.catalog = cat
    return w, com, li, cat, calls


def keepa_ok(asins):
    return {'products': [{'asin': a, 'title': f'Item {a}', 'imagesCSV': f'{a}.jpg,other.jpg'} for a in asins]}


def test_cache_first_then_one_keepa_call_and_kept_photos_stay(tmp_path, monkeypatch):
    w, com, li, cat, calls = setup(tmp_path, monkeypatch, keepa_ok)
    with w._tx() as c:
        c.execute('INSERT INTO catalog_cache (code, fetched_at, data) VALUES (?,?,?)',
                  ('B0CACHED01', now_utc(), json.dumps([{'asin': 'B0CACHED01', 'image_url': 'https://m.media-amazon.com/images/I/c.jpg'}])))
    amazon(li, 'ElviraBLKLT-7.90-6-25', 'B0BPDQDZ6A')
    amazon(li, 'CACHED', 'B0CACHED01')
    amazon(li, 'HAS', 'B0HASPHOTO', 'https://example.com/mine.jpg')
    out = li.fill_photos()
    assert calls == [['B0BPDQDZ6A']]                     # the cached one and the one with a photo cost nothing
    assert out == {'missing': 2, 'filled': 2, 'keepa_found': 1}
    assert photo(w, 'ElviraBLKLT-7.90-6-25') == 'https://m.media-amazon.com/images/I/B0BPDQDZ6A.jpg'
    assert photo(w, 'CACHED') == 'https://m.media-amazon.com/images/I/c.jpg'
    assert photo(w, 'HAS') == 'https://example.com/mine.jpg'
    amazon(li, 'ElviraBLKLT-7.90-6-25', 'B0BPDQDZ6A')     # the next hourly read keeps it
    assert photo(w, 'ElviraBLKLT-7.90-6-25').endswith('B0BPDQDZ6A.jpg')
    assert li.fill_photos() == {'missing': 0} and len(calls) == 1


def test_order_card_with_no_item_shows_the_listing_photo(tmp_path, monkeypatch):
    w, com, li, cat, calls = setup(tmp_path, monkeypatch, keepa_ok)
    amazon(li, 'NecaMBV11-4', 'B0SOZ5XDRM')
    li.fill_photos()
    with w._tx() as c:
        o = c.execute("""INSERT INTO sales_order (channel, external_id, ordered_at, status, imported_at)
                         VALUES ('amazon', '111-0514792-1452602', '2026-11-12', 'open', 'x')""").lastrowid
        c.execute("INSERT INTO sales_line (order_id, line_key, sku, asin, title, qty) VALUES (?, 'k1', 'NecaMBV11-4', 'B0SOZ5XDRM', 'Miner', 1)", (o,))
    line = com.pick_list('open')[0]['lines'][0]
    assert line['product_id'] is None and line['image_url'] == 'https://m.media-amazon.com/images/I/B0SOZ5XDRM.jpg'


def test_out_of_keepa_tokens_fills_what_the_cache_has_and_tries_next_hour(tmp_path, monkeypatch):
    def broke(asins):
        raise WmsError('Product lookup is out of Keepa tokens for a minute. Try again shortly.')
    w, com, li, cat, calls = setup(tmp_path, monkeypatch, broke)
    amazon(li, 'X', 'B0BPDQDZ6A')
    out = li.fill_photos()
    assert out['filled'] == 0 and 'Keepa tokens' in out['error'] and photo(w, 'X') is None
