"""New product photo -> name, brand, our buys (Alex 11-11). Gemini is faked: tests never call the network."""
import http.client
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
from photo_id import PhotoIdError, gtin_ok, rank  # noqa: E402
from test_cover_all import srv  # noqa: E402,F401  (fixture)

JPEG = b'\xff\xd8\xff\xe0' + b'0' * 2000


def fake(title='', brand='', barcode='', is_product=True, fail=None):
    def reader(jpeg, key):
        assert jpeg == JPEG and key == 'k'
        if fail:
            raise PhotoIdError(fail)
        return {'is_product': is_product, 'title': title, 'brand': brand, 'model': '', 'barcode_digits': barcode,
                'confidence': 'high', 'seconds': 0.1, 'tokens_in': 1200, 'tokens_out': 300}
    return reader


def send(server, path, body, ctype='image/jpeg'):
    c = http.client.HTTPConnection('127.0.0.1', server._test_port, timeout=10)
    c.request('POST', path, body, {'Cookie': server._test_cookie, 'X-Boxloft': '1', 'Content-Type': ctype})
    r = c.getresponse()
    return r.status, json.loads(r.read() or b'null')


def buys(server):
    w = server.wms
    with w._tx() as c:
        for i, (asin, title) in enumerate([
                ('B0T80M0F5O', 'NECA - Predator (2018) - 7" Scale Action Figure - Ultimate Fugitive Predator'),
                ('B06UASS0QK', 'NECA - Predator 2: Warrior Predator Ultimate 7" Action Figure'),
                ('B02V6T393K', 'NECA - Chucky 4 inch Scale Action Figure - Ultimate Chucky'),
                ('B000000001', 'NECA Friday the 13th Ultimate Jason 7" Scale Action Figure'),
                ('B000000002', 'NECA Halloween Ultimate Michael Myers 7" Action Figure')]):
            c.execute("""INSERT INTO audit_item (asin, title, image_url, expected, imported_at)
                         VALUES (?,?,?,?, '2026-11-01')""", (asin, title, f'https://m.media-amazon.com/images/I/{i}.jpg', 3))
        c.execute("INSERT INTO purchase_order (source, external_id, imported_at) VALUES ('target.com', 'T-1', '2026-11-01')")
        po = c.execute('SELECT last_insert_rowid()').fetchone()[0]
        c.execute("""INSERT INTO purchase_line (po_id, line_key, title, qty, unit_cost_cents)
                     VALUES (?, 'a', 'Camco Camper/RV Tire Protector Heavy-Duty Vinyl Covers 2-Pack', 2, 1999)""", (po,))


def test_check_digit_stops_misread_barcodes():
    assert gtin_ok('036000291452') and gtin_ok('4006381333931') and gtin_ok('96385074')
    assert not gtin_ok('036000291453') and not gtin_ok('12345') and not gtin_ok('')


def test_rare_words_decide_not_neca_figure_ultimate():
    cands = [{'kind': 'audit', 'id': i, 'title': t, 'left': 1} for i, t in enumerate([
        'NECA Chucky Ultimate 7" Scale Action Figure', 'NECA Predator 2 Ultimate Warrior Predator Action Figure',
        'NECA Jason Ultimate Action Figure', 'NECA Michael Myers Ultimate Action Figure', 'LEGO Star Wars Darth Vader Helmet'])]
    assert [m['id'] for m in rank('NECA The Predator Ultimate Action Figure', cands)] == [1]
    # A figure we never bought matches nothing, though NECA / Ultimate / Action Figure are on every title.
    assert rank("NECA Trick 'r Treat Ultimate Sam 7-Inch Scale Action Figure", cands) == []


def test_photo_fills_and_finds_the_buy_and_becomes_the_product_photo(srv, monkeypatch):  # noqa: F811
    server, call = srv
    buys(server)
    monkeypatch.setenv('GEMINI_API_KEY', 'k')
    monkeypatch.setattr(server.photo_reader, 'reader', fake('NECA The Predator Ultimate Fugitive Predator Action Figure', 'NECA'))
    st, r = send(server, '/api/photo-id', JPEG)
    assert st == 200 and r['error'] is None and r['read']['brand'] == 'NECA'
    assert r['matches'][0]['asin'] == 'B0T80M0F5O' and r['matches'][0]['kind'] == 'audit'
    assert all('Chucky' not in m['title'] for m in r['matches'])
    # The photo is served before any product holds it, and only by its exact token.
    c = http.client.HTTPConnection('127.0.0.1', server._test_port, timeout=10)
    c.request('GET', f"/api/photo-id/{r['photo']}", headers={'Cookie': server._test_cookie})
    got = c.getresponse()
    assert got.status == 200 and got.read() == JPEG
    assert call('Sam', '/api/photo-id/..%2F..%2Fwms.db')[0] == 404
    # Create product with the token: the photo is the product's photo, not the catalog image.
    st, p = call('Sam', '/api/products', {'sku': 'B0T80M0F5O', 'title': 'NECA Predator (2018) Fugitive', 'owner_id': 1,
                                          'identifiers': [{'kind': 'ASIN', 'value': 'B0T80M0F5O'}], 'photo_token': r['photo']})
    assert st == 200 and len(p['photos']) == 1
    log = server.wms._q1('SELECT * FROM photo_read')
    assert log['product_id'] == p['id'] and log['saved_title'] == 'NECA Predator (2018) Fugitive' and log['tokens_in'] == 1200
    # The same token again (a reused link) adds no second copy.
    st, p2 = call('Sam', '/api/products', {'sku': 'X2', 'title': 'Other', 'owner_id': 1, 'photo_token': r['photo']})
    assert st == 200 and p2['photos'] == []
    # Next photo of the same item: the product now comes first, so no duplicate gets made.
    st, r2 = send(server, '/api/photo-id', JPEG)
    assert r2['matches'][0] == {**r2['matches'][0], 'kind': 'product', 'id': p['id']}


def test_barcode_on_the_box_finds_the_product(srv, monkeypatch):  # noqa: F811
    server, call = srv
    monkeypatch.setenv('GEMINI_API_KEY', 'k')
    st, p = call('Sam', '/api/products', {'sku': 'S1', 'title': 'Some Thing', 'owner_id': 1,
                                          'identifiers': [{'kind': 'UPC', 'value': '036000291452'}]})
    monkeypatch.setattr(server.photo_reader, 'reader', fake('Totally different words', 'X', barcode='036000291452'))
    st, r = send(server, '/api/photo-id', JPEG)
    assert r['matches'][0]['id'] == p['id'] and r['matches'][0]['by'] == 'barcode'
    # A misread barcode (bad check digit) is dropped, never looked up.
    monkeypatch.setattr(server.photo_reader, 'reader', fake('Totally different words', 'X', barcode='036000291453'))
    st, r = send(server, '/api/photo-id', JPEG)
    assert r['read']['barcode_digits'] == '' and r['matches'] == []
    # Tapping an existing product with no photo gives it this photo; one with a photo keeps its own.
    assert call('Sam', '/api/photo-id/attach', {'photo_token': r['photo'], 'product_id': p['id']})[1] == {'attached': True}
    st, r3 = send(server, '/api/photo-id', JPEG)
    assert call('Sam', '/api/photo-id/attach', {'photo_token': r3['photo'], 'product_id': p['id']})[1] == {'attached': False}


def test_no_product_no_key_and_reader_errors_still_keep_the_photo(srv, monkeypatch):  # noqa: F811
    server, call = srv
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    st, r = send(server, '/api/photo-id', JPEG)
    assert st == 200 and 'not set up' in r['error'] and r['photo']
    monkeypatch.setenv('GEMINI_API_KEY', 'k')
    monkeypatch.setattr(server.photo_reader, 'reader', fake(is_product=False))
    st, r = send(server, '/api/photo-id', JPEG)
    assert 'No product in that photo' in r['error'] and r['matches'] == []
    monkeypatch.setattr(server.photo_reader, 'reader', fake(fail='The photo reader did not answer in time.'))
    st, r = send(server, '/api/photo-id', JPEG)
    assert 'did not answer' in r['error'] and r['photo']
    assert send(server, '/api/photo-id', b'GIF89a', 'image/gif')[0] == 415
    assert send(server, '/api/photo-id', b'not a jpeg', 'image/jpeg')[0] == 400
    assert server.wms._q1('SELECT COUNT(*) AS n FROM photo_read')['n'] == 3


ALBUM_BUY = 'DREAMCATCHER Apocalypse : Follow us 7th Mini Album Limited T Version CD+Photoboo'
ALBUM_KEEPA = ('Apocalypse : Follow Us - Limited Edition - incl. Photo Book, Spin Card, Monochrome Postcard, Four-Cut Photo, '
               'Photocard, Agent Card + Poster')


def test_two_rare_shared_words_find_a_buy_under_a_long_catalog_title():
    """Alex 11-11: Keepa's title for the importer's sticker barcode shares only a fifth of its word weight with the
    buy, but "apocalypse" and "follow" are on no other buy title."""
    cands = [{'kind': 'audit', 'id': 1, 'title': ALBUM_BUY, 'asin': 'B08EQTCIKG', 'left': 1}] + [
        {'kind': 'audit', 'id': i, 'title': t, 'asin': '', 'left': 1} for i, t in enumerate([
            'ONE PIECE CARD GAME Premium Card Collection Limited Edition', 'Terrifier ARTcade Game Limited Edition',
            'NECA Chucky Ultimate 7" Scale Action Figure', 'NECA Predator 2 Ultimate Action Figure', 'Star Wars Black Series Vader',
            'Camco RV Tire Protector', 'Pokemon Booster Box', 'LEGO Star Wars Helmet'], start=2)]
    assert [m['id'] for m in rank(ALBUM_KEEPA, cands)] == [1]
    assert rank("NECA Trick 'r Treat Ultimate Sam 7-Inch Scale Action Figure", cands) == []


def test_receive_links_a_product_to_the_buy_it_came_from(srv):  # noqa: F811
    server, call = srv
    w = server.wms
    with w._tx() as c:
        c.execute("INSERT INTO audit_item (asin, title, expected, imported_at) VALUES ('B08EQTCIKG', ?, 1, '2026-09-18')", (ALBUM_BUY,))
        c.execute("INSERT INTO purchase_order (source, external_id, ordered_at, imported_at) VALUES ('amazon', '112-3924050-2283641', '2025-12-19', '2026-11-01')")
        po = c.execute('SELECT last_insert_rowid()').fetchone()[0]
        c.execute("INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents) VALUES (?, 'B08EQTCIKG', 'B08EQTCIKG', ?, 1, 2499)",
                  (po, ALBUM_BUY))
        c.execute("INSERT INTO catalog_cache (code, fetched_at, data) VALUES ('8803581202687', '2026-11-11', ?)",
                  (json.dumps([{'asin': 'B0UT0P69MB', 'title': ALBUM_KEEPA}]),))
    # The scan's catalog ASIN was never bought: the look-alike buy is offered. Our own ASIN would offer nothing.
    st, r = call('Sam', '/api/buy-match?' + __import__('urllib.parse').parse.urlencode({'q': ALBUM_KEEPA, 'asin': 'B0UT0P69MB'}))
    assert st == 200 and not r['bought'] and [m['asin'] for m in r['matches']][:1] == ['B08EQTCIKG']
    st, r = call('Sam', '/api/buy-match?q=x&asin=B08EQTCIKG')
    assert r == {'bought': True, 'matches': []}
    # The product made from the sticker (as on live 11-11) finds no order until it is linked.
    st, p = call('Sam', '/api/products', {'sku': 'SS-1', 'title': ALBUM_KEEPA, 'owner_id': 1,
                                          'identifiers': [{'kind': 'ASIN', 'value': 'B0UT0P69MB'}, {'kind': 'EAN', 'value': '8803581202687'}]})
    assert call('Sam', f"/api/purchases/open?product_id={p['id']}")[1]['lines'] == []
    st, r = call('Sam', f"/api/buy-match?product_id={p['id']}")
    pick = r['matches'][0]
    assert pick['asin'] == 'B08EQTCIKG' and pick['kind'] in ('audit', 'line')
    st, out = call('Sam', f"/api/products/{p['id']}/link-buy", {'kind': pick['kind'], 'id': pick['id']})
    assert st == 200 and 'B08EQTCIKG' in out['linked']
    lines = call('Sam', f"/api/purchases/open?product_id={p['id']}")[1]['lines']
    assert [(x['order_id'], x['left_to_receive']) for x in lines] == [('112-3924050-2283641', 1)]
    # Linked once, the product no longer asks.
    assert all(m['asin'] != 'B08EQTCIKG' for m in call('Sam', f"/api/buy-match?product_id={p['id']}")[1]['matches'])


def test_link_a_store_line_with_no_asin_and_keep_a_scanned_code(srv):  # noqa: F811
    server, call = srv
    w = server.wms
    with w._tx() as c:
        c.execute("INSERT INTO purchase_order (source, external_id, imported_at) VALUES ('target.com', 'T-9', '2026-11-01')")
        po = c.execute('SELECT last_insert_rowid()').fetchone()[0]
        c.execute("INSERT INTO purchase_line (po_id, line_key, title, qty, unit_cost_cents) VALUES (?, 'a', 'Camco RV Tire Protector 2-Pack', 2, 1999)", (po,))
        line = c.execute('SELECT last_insert_rowid()').fetchone()[0]
    st, p = call('Sam', '/api/products', {'sku': 'S2', 'title': 'Camco tire covers', 'owner_id': 1})
    st, other = call('Sam', '/api/products', {'sku': 'S3', 'title': 'Other', 'owner_id': 1})
    st, linked = call('Sam', f"/api/products/{p['id']}/link-buy", {'kind': 'line', 'id': line, 'code': '036000291452'})
    assert st == 200 and linked['line_id'] == line and linked['added'] == [{'kind': 'UPC', 'value': '036000291452'}]
    assert [x['id'] for x in call('Sam', f"/api/purchases/open?product_id={p['id']}")[1]['lines']] == [line]
    assert server.wms.lookup('036000291452')[0]['id'] == p['id']
    # Undo (a wrong look-alike tapped) takes off exactly what the link added.
    assert call('Sam', f"/api/products/{p['id']}/unlink-buy", linked)[0] == 200
    assert call('Sam', f"/api/purchases/open?product_id={p['id']}")[1]['lines'] == [] and server.wms.lookup('036000291452') == []
    assert call('Sam', f"/api/products/{p['id']}/link-buy", {'kind': 'line', 'id': line})[0] == 200
    st, r = call('Alex', f"/api/products/{other['id']}/link-buy", {'kind': 'line', 'id': line})
    assert st == 400 and 'another product' in r['error']


def test_photo_read_does_not_wait_for_the_save_lock(srv, monkeypatch):  # noqa: F811
    """Gemini takes 5-10 s: the read must run outside op_lock, so Alex's saves never wait on Sam's photo."""
    server, call = srv
    monkeypatch.setenv('GEMINI_API_KEY', 'k')
    seen = {}

    def reader(jpeg, key):
        seen['lock_free'] = server.wms.op_lock.acquire(blocking=False)
        if seen['lock_free']:
            server.wms.op_lock.release()
        return fake('x y z')(jpeg, key)
    monkeypatch.setattr(server.photo_reader, 'reader', reader)
    import threading
    hold = threading.Thread(target=lambda: (server.wms.op_lock.acquire(), threading.Event().wait(1.5), server.wms.op_lock.release()))
    hold.start()
    import time
    time.sleep(0.1)
    t = time.time()
    st, _ = send(server, '/api/photo-id', JPEG)
    assert st == 200 and time.time() - t < 1.0
    hold.join()
