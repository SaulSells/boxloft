"""The new item card offers the listing (Alex 11-12 3:20 PM).

Sam's first scan of a Hydro Flask with an Amazon FBM listing showed one button, "Yes, put 1 in A64", and never named
the listing (Yes did link it, by ASIN, without saying so). The Minecraft armadillo's barcode had no ASIN anywhere
(UPCitemdb answered; Keepa's record for the listing's ASIN carries another barcode), so its FBM listing MineArma11-4
was never linked at all.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from test_sorting import setup  # noqa: E402

HYDRO_UPC, HYDRO_ASIN = '810070080853', 'B0CEYVFSXW'
ARMA_UPC = '194735270774'
ARMA_LISTING = ('Minecraft Basic Plush 8 inch Legends Character Soft Dolls, Video Game-Inspired Collectible Toy '
                'Gifts for Kids & Fans Ages 3 Years Old & Up (Armadillo)')
ALEX_LISTING = 'Mattel Minecraft Basic Plush Toy, Alex Soft Doll, 8-inch Collectible Inspired by Video Game'


def with_listings(tmp_path):
    from listings import Listings
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    li = Listings(w, au.commerce)
    so.listings = li

    def row(sku, asin, title, qty=1, status='Active'):
        return {'sku': sku, 'asin': asin, 'title': title, 'fulfillment': 'FBM', 'status': status, 'qty': qty,
                'price_cents': 2275, 'listing_id': '', 'opened_on': None, 'condition': 'New', 'code_type': '1', 'code': asin}
    li.upsert('amazon', [row('HF-9.21.25-7.45', HYDRO_ASIN, 'Hydro Flask Small Tag Along Bottle Sling Birch', 5),
                         row('MineArma11-4', 'B02Y3D2UXE', ARMA_LISTING), row('MineAlex11-4', 'B0Q5RHRUN8', ALEX_LISTING)])
    cat.db[HYDRO_UPC] = {'asin': HYDRO_ASIN, 'title': 'Hydro Flask Small Tag Along Bottle Sling Birch', 'brand': 'Hydro Flask',
                         'image_url': None, 'upcs': [HYDRO_UPC], 'eans': []}
    # UPCitemdb's answer: a name and no ASIN
    cat.db[ARMA_UPC] = {'asin': None, 'title': 'Minecraft Plush Armadillo Toy  8-inch Collectible Soft Doll Inspired by Video Game Character',
                        'brand': 'Minecraft', 'image_url': None, 'upcs': [ARMA_UPC], 'eans': []}
    lid = {r['sku']: r['id'] for r in w._q('SELECT id, sku FROM listing')}
    return w, so, boxes, lid


def idents(w, pid):
    return {(i['kind'], i['value']) for i in w._q('SELECT kind, value FROM product_identifier WHERE product_id = ?', (pid,))}


def test_same_asin_listing_is_offered_and_its_link_is_one_save(tmp_path):
    w, so, boxes, lid = with_listings(tmp_path)
    card = so.new_card(HYDRO_UPC)
    assert [(o['sku'], o['match'], o['market'], o['product_id'], o['live']) for o in card['listings']] == \
        [('HF-9.21.25-7.45', 'asin', 'Amazon', None, True)]
    r = so.put_new(boxes['A1'], 1, scan_id='h1', code=HYDRO_UPC, listing_id=lid['HF-9.21.25-7.45'])
    assert (r['kind'], r['made'], r['here']) == ('added', True, 1)
    assert r['said'] == 'Linked to your Amazon FBM listing HF-9.21.25-7.45. Not on an order' and r['cost_cents'] is None
    assert w._q1('SELECT product_id FROM listing WHERE id = ?', (lid['HF-9.21.25-7.45'],))['product_id'] == r['product_id']
    assert idents(w, r['product_id']) >= {('ASIN', HYDRO_ASIN), ('UPC', HYDRO_UPC)}
    # The next scan finds it: 1 more, no card.
    assert so.put(boxes['A1'], 1, scan_id='h2', code=HYDRO_UPC)['product_id'] == r['product_id']
    assert w.check_integrity()['ok']


def test_an_item_holding_the_asin_is_offered_never_a_second_item(tmp_path):
    w, so, boxes, lid = with_listings(tmp_path)
    first = so.put_new(boxes['A1'], 1, scan_id='d1', code=HYDRO_UPC, listing_id=lid['HF-9.21.25-7.45'])
    other = '0810070080860'   # another barcode Keepa maps to the same ASIN
    so.catalog.db[other] = {**so.catalog.db[HYDRO_UPC], 'upcs': [other]}
    card = so.new_card(other)
    assert (card['listings'][0]['product_id'], card['listings'][0]['match']) == (first['product_id'], 'asin')
    assert all(t['id'] != first['product_id'] for t in card['twins'])   # shown once, on its listing
    r = so.put_new(boxes['A2'], 1, scan_id='d2', code=other, listing_id=lid['HF-9.21.25-7.45'])
    assert (r['product_id'], r.get('made')) == (first['product_id'], None)
    assert len(w._q("SELECT id FROM product WHERE archived = 0 AND title LIKE 'Hydro%'")) == 1
    assert [p['id'] for p in w.lookup(other)] == [first['product_id']]


def test_same_asin_item_with_no_listing_is_a_twin(tmp_path):
    w, so, boxes, lid = with_listings(tmp_path)
    first = so.put_new(boxes['A1'], 1, scan_id='t1', code=HYDRO_UPC, listing_id=lid['HF-9.21.25-7.45'])
    w._conn().execute('UPDATE listing SET gone_at = ? WHERE id = ?', ('2026-11-12T00:00:00Z', lid['HF-9.21.25-7.45']))
    w._conn().commit()
    other = '0810070080877'
    so.catalog.db[other] = {**so.catalog.db[HYDRO_UPC], 'upcs': [other]}
    card = so.new_card(other)
    assert card['listings'] == [] and (card['twins'][0]['id'], card['twins'][0].get('exact')) == (first['product_id'], True)
    r = so.put_new(boxes['A2'], 1, scan_id='t2', code=other, product_id=first['product_id'])
    assert r['product_id'] == first['product_id'] and not r.get('made')


def test_no_asin_offers_look_alike_listings_and_links_only_on_a_tap(tmp_path):
    w, so, boxes, lid = with_listings(tmp_path)
    card = so.new_card(ARMA_UPC)
    assert card['asin'] == ''
    # both score 0.91: a person picks by the photo; nothing is linked by the card itself
    assert [(o['sku'], o['match']) for o in card['listings']] == [('MineArma11-4', 'name'), ('MineAlex11-4', 'name')]
    assert not w._q('SELECT id FROM listing WHERE product_id IS NOT NULL')
    r = so.put_new(boxes['A1'], 1, scan_id='a1', code=ARMA_UPC, listing_id=lid['MineArma11-4'])
    pid = r['product_id']
    assert w._q1('SELECT product_id FROM listing WHERE sku = ?', ('MineArma11-4',))['product_id'] == pid
    assert w._q1('SELECT product_id FROM listing WHERE sku = ?', ('MineAlex11-4',))['product_id'] is None
    # only the scanned barcode goes on, not the other barcodes of the listing's ASIN
    assert idents(w, pid) == {('ASIN', 'B02Y3D2UXE'), ('UPC', ARMA_UPC)}
    # Undo takes back the unit, the item and the link
    u = so.undo_add(r['add_id'], 1)
    assert u['unmade'] and w._q1('SELECT product_id FROM listing WHERE sku = ?', ('MineArma11-4',))['product_id'] is None


def test_none_of_these_makes_an_item_linked_to_no_listing(tmp_path):
    w, so, boxes, lid = with_listings(tmp_path)
    r = so.put_new(boxes['A1'], 1, scan_id='n1', code=ARMA_UPC)
    assert r['made'] and not w._q('SELECT id FROM listing WHERE product_id = ?', (r['product_id'],))


def test_asin_answers_never_match_listings_on_names(tmp_path):
    w, so, boxes, lid = with_listings(tmp_path)
    so.catalog.db['0799999000020'] = {'asin': 'B0OTHERPLU', 'title': ALEX_LISTING, 'brand': '', 'image_url': None,
                                      'upcs': ['0799999000020'], 'eans': []}
    assert so.new_card('0799999000020')['listings'] == []


def test_a_gone_listing_is_refused(tmp_path):
    import pytest
    from wms import WmsError
    w, so, boxes, lid = with_listings(tmp_path)
    w._conn().execute('UPDATE listing SET gone_at = ? WHERE sku = ?', ('2026-11-12T00:00:00Z', 'MineArma11-4'))
    w._conn().commit()
    with pytest.raises(WmsError):
        so.put_new(boxes['A1'], 1, scan_id='g1', code=ARMA_UPC, listing_id=lid['MineArma11-4'])
    assert not w.lookup(ARMA_UPC)
