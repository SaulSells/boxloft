"""Alex 11-11: "what do we have in an Amazon box that isn't listed, and where do we have 10 but only 6 listed?"
Stock in a market's boxes (the box's letter) against that market's listings, one reason per product, and
"Find its listing" for a product with no ASIN: a person taps a title suggestion; nothing links by title alone."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from test_cover_all import srv  # noqa: E402,F401  (two signed-in users on a real server)
from test_listings import setup  # noqa: E402
from wms import WmsError  # noqa: E402

import pytest  # noqa: E402


def L(sku, asin, title, qty, status='Active', fulfillment='FBM'):
    return {'sku': sku, 'asin': asin, 'title': title, 'fulfillment': fulfillment, 'status': status, 'qty': qty,
            'price_cents': 1999, 'listing_id': 'L' + sku, 'opened_on': None, 'condition': 'New', 'code_type': '1', 'code': asin}


def product(w, sku, title, asin=None):
    ids = [{'kind': 'ASIN', 'value': asin}] if asin else []
    return w.save_product({'sku': sku, 'title': title, 'owner_id': 1, 'identifiers': ids})['id']


def box(w, name):
    return w.create_boxes(1, 1, name=name)[0]['id']


def warehouse(w, li):
    """6 products in Amazon boxes, 1 in an eBay box, 1 in a box with no market."""
    a1, a2, e1, f1 = box(w, 'A1'), box(w, 'A2'), box(w, 'E1'), box(w, 'F1')
    p = {
        'full': product(w, 'P-FULL', 'Pokemon Scarlet Violet Booster Box 36 Packs', 'B0FULL0001'),
        'short': product(w, 'P-SHORT', 'DC Super Friends Nightwing Figure', 'B0SHORT001'),
        'off': product(w, 'P-OFF', 'Funko Advent Calendar Five Nights', 'B0OFF00001'),
        'none': product(w, 'P-NONE', 'Graco Spray Gun Filter 60 Mesh', 'B0NONE0001'),
        'noasin': product(w, 'P-NOASIN', 'NECA Creepshow The Creep 8-Inch Clothed Action Figure'),
        'fba': product(w, 'P-FBA', 'Speedo Swimsuit Brief Endurance', 'B0FBA00001'),
        'ebay': product(w, 'P-EBAY', 'Vintage Star Wars Card Lot', 'B0EBAY0001'),
        'other': product(w, 'P-OTHER', 'Thing in a box with no market', 'B0OTHER001'),
    }
    w.receive(1, p['full'], 3, 500, {'box_id': a1})
    w.receive(1, p['short'], 6, 500, {'box_id': a1})
    w.receive(1, p['short'], 4, 500, {'box_id': a2})      # 10 here, in two boxes
    w.receive(1, p['short'], 5, 500, {'box_id': f1})      # 5 more in a box that is not an Amazon box
    w.receive(1, p['off'], 2, 500, {'box_id': a2})
    w.receive(1, p['none'], 4, 500, {'box_id': a2})
    w.receive(1, p['noasin'], 1, 500, {'box_id': a1})
    w.receive(1, p['fba'], 2, 500, {'box_id': a1})
    w.receive(1, p['ebay'], 7, 500, {'box_id': e1})
    w.receive(1, p['other'], 9, 500, {'box_id': f1})
    li.upsert('amazon', [
        L('S-FULL', 'B0FULL0001', 'Pokemon Booster Box', 3),
        L('S-SHORT', 'B0SHORT001', 'DC Nightwing', 6),
        L('S-OFF', 'B0OFF00001', 'Funko Advent Calendar', 4, status='Inactive'),
        L('S-FBA', 'B0FBA00001', 'Speedo Brief', 0, fulfillment='FBA'),
        L('S-CREEP', 'B0CREEP001', 'NECA Creepshow: The Creep - 8" Clothed Action Figure', 1),
        L('S-CREEP2', 'B0CREEP002', 'NECA Creepshow The Creep Clothed Action Figure 8-Inch (used)', 0, status='Inactive'),
        L('S-FAR', 'B0FAR00001', 'Barbie Horse with Sounds and Music', 2),
    ], {'S-FBA': {'fulfillable': 5, 'inbound': 0, 'reserved': 0, 'unfulfillable': 0}})
    return p


def by_reason(out):
    return {r['sku']: r for r in out['rows']}


def test_one_reason_per_product_in_the_markets_boxes(tmp_path):
    w, com, au, li = setup(tmp_path)
    warehouse(w, li)
    out = li.stock_vs_listed('amazon')
    rows = by_reason(out)
    assert {k: r['reason'] for k, r in rows.items()} == {
        'P-FULL': 'listed', 'P-SHORT': 'short', 'P-OFF': 'inactive', 'P-NONE': 'not_listed', 'P-NOASIN': 'no_asin',
        'P-FBA': 'inactive'}                                   # eBay-box and F-box stock is not Amazon's
    s = rows['P-SHORT']
    assert (s['on_hand'], s['listed'], s['unlisted']) == (10, 6, 4)      # the 5 in F1 don't count
    assert [(b['name'], b['qty']) for b in s['boxes']] == [('A1', 6), ('A2', 4)]
    assert rows['P-FBA']['listed'] == 0 and rows['P-FBA']['listed_fba'] == 5   # FBA units don't cover our box
    sm = out['summary']
    assert sm['all'] == {'products': 6, 'units': 22, 'unlisted': 13}
    assert sm['short'] == {'products': 1, 'units': 10, 'unlisted': 4}
    assert sm['not_listed'] == {'products': 1, 'units': 4, 'unlisted': 4}
    assert sm['inactive'] == {'products': 2, 'units': 4, 'unlisted': 4}
    assert sm['no_asin'] == {'products': 1, 'units': 1, 'unlisted': 1}
    assert sm['none_for_sale'] == {'products': 4, 'units': 9}
    assert out['synced'] and out['letter'] == 'A'
    # The listings page shows the market's boxes next to every box.
    short = next(x for x in li.list()['listings'] if x['sku'] == 'S-SHORT')
    assert (short['on_hand'], short['on_hand_market']) == (15, 10)


def test_ebay_and_walmart_show_their_boxes_before_their_listings_sync(tmp_path):
    w, com, au, li = setup(tmp_path)
    warehouse(w, li)
    e = li.stock_vs_listed('ebay')
    assert not e['synced'] and e['letter'] == 'E'
    assert [(r['sku'], r['reason'], r['on_hand']) for r in e['rows']] == [('P-EBAY', 'not_listed', 7)]
    assert li.stock_vs_listed('walmart')['rows'] == []
    with pytest.raises(WmsError):
        li.stock_vs_listed('mercari')
    # The day eBay listings land in `listing`, the same method covers them.
    li.upsert('ebay', [L('P-EBAY', '', 'Vintage Star Wars Card Lot', 4)])
    e = li.stock_vs_listed('ebay')
    assert e['synced'] and [(r['reason'], r['listed'], r['unlisted']) for r in e['rows']] == [('short', 4, 3)]
    assert by_reason(li.stock_vs_listed('amazon'))['P-FULL']['reason'] == 'listed'   # Amazon is untouched


def test_a_hand_made_mark_counts_as_listed(tmp_path):
    w, com, au, li = setup(tmp_path)
    p = warehouse(w, li)
    au.import_snapshot([{'asin': 'B0NONE0001', 'title': 'Graco filter', 'expected': 4, 'orders': []}], 'test2')
    item = w._q1("SELECT id FROM audit_item WHERE asin = 'B0NONE0001' ORDER BY id DESC")['id']
    mid = li.mark(item, {'kind': 'manual', 'platform': 'amazon_fbm', 'qty': 4}, 1)['id']
    row = by_reason(li.stock_vs_listed('amazon'))['P-NONE']
    assert (row['reason'], row['listed'], row['product_id']) == ('listed', 4, p['none'])
    li.mark(item, {'kind': 'manual', 'platform': 'ebay', 'qty': 9}, 1)   # an eBay mark says nothing about Amazon
    li.undo_mark(item, mid, 1)
    assert by_reason(li.stock_vs_listed('amazon'))['P-NONE']['reason'] == 'not_listed'


def test_receive_form_sees_where_the_item_is_listed(tmp_path):
    """Alex 11-11: scanning an item in, the intake screen says it has a live listing and nothing in the boxes."""
    w, com, au, li = setup(tmp_path)
    p = warehouse(w, li)
    waiting = product(w, 'P-WAIT', 'Barbie Horse with Sounds', 'B0FAR00001')     # listed, live, 0 in any box
    one = lambda pid: {c['channel']: c for c in li.product_listed(pid)['channels']}   # noqa: E731
    a = one(waiting)['amazon']
    assert (a['state'], a['listed'], a['in_market'], a['fills']) == ('live', 2, 0, True)
    assert li.product_listed(waiting)['suggest'] == 'A' and list(one(waiting)) == ['amazon']   # eBay isn't read yet
    a = one(p['short'])['amazon']
    assert (a['state'], a['listed'], a['in_market'], a['fills']) == ('live', 6, 10, False)    # F1's 5 are not Amazon's
    assert (one(p['fba'])['amazon']['state'], one(p['fba'])['amazon']['listed_fba']) == ('fba', 5)
    assert one(p['off'])['amazon']['state'] == 'inactive'
    assert one(p['none'])['amazon']['state'] == 'none' and li.product_listed(p['none'])['suggest'] is None
    li.upsert('ebay', [L('P-EBAY', '', 'Vintage Star Wars Card Lot', 4)])
    assert (one(p['ebay'])['ebay']['state'], one(p['ebay'])['amazon']['state']) == ('live', 'none')
    assert li.product_listed(p['ebay'])['suggest'] == 'E'


def test_suggestions_are_title_matches_with_no_product_best_first(tmp_path):
    w, com, au, li = setup(tmp_path)
    warehouse(w, li)
    rows = by_reason(li.stock_vs_listed('amazon'))
    sug = rows['P-NOASIN']['suggestions']
    assert [x['sku'] for x in sug] == ['S-CREEP', 'S-CREEP2'] and all(x['score'] >= 75 for x in sug)   # live one first
    assert all(not r['suggestions'] for k, r in rows.items() if k != 'P-NOASIN')   # only products with no ASIN
    assert w._q1("SELECT product_id FROM listing WHERE sku = 'S-CREEP'")['product_id'] is None   # nothing linked by title


def test_find_its_listing_links_and_undo_puts_it_back(srv):
    server, call = srv
    w, li = server.wms, server.listings
    p = warehouse(w, li)
    server.sales.upsert('amazon', [{'external_id': 'O-9', 'ordered_at': '2026-11-10', 'state': 'open', 'fulfilled_by': 'me',
                                    'lines': [{'line_key': 'L1', 'sku': 'S-CREEP', 'asin': 'B0CREEP001', 'title': 'Creep', 'qty': 1,
                                               'price_cents': 999}]}])
    st, out = call('Sam', '/api/listings/stock?channel=amazon')
    row = next(r for r in out['rows'] if r['reason'] == 'no_asin')
    lid = row['suggestions'][0]['listing_id']
    moves = w._q1('SELECT COUNT(*) AS n FROM stock_move')['n']
    st, k = call('Sam', '/api/listings/link', {'product_id': p['noasin'], 'listing_id': lid, 'seen': {'updated_at': row['updated_at'], 'asin': row['asin']}})
    assert st == 200 and k['asin'] == 'B0CREEP001' and k['sales_lines_linked'] == 1
    assert w._q1('SELECT product_id FROM listing WHERE id = ?', (lid,))['product_id'] == p['noasin']
    assert w._q1("SELECT 1 FROM product_identifier WHERE product_id = ? AND kind = 'ASIN' AND value = 'B0CREEP001'", (p['noasin'],))
    assert w._q1("SELECT product_id FROM sales_line WHERE sku = 'S-CREEP'")['product_id'] == p['noasin']   # the sale follows
    after = next(r for r in call('Sam', '/api/listings/stock?channel=amazon')[1]['rows'] if r['product_id'] == p['noasin'])
    assert (after['reason'], after['listed'], after['asin']) == ('listed', 1, 'B0CREEP001')
    # Alex's screen is older: his tap on the other suggestion is refused, with who.
    other = row['suggestions'][1]['listing_id']
    st, body = call('Alex', '/api/listings/link', {'product_id': p['noasin'], 'listing_id': other, 'seen': {'updated_at': row['updated_at'], 'asin': row['asin']}})
    assert st == 409 and 'Sam changed this product at' in body['error'] and 'Nothing was saved' in body['error']
    assert w._q1('SELECT product_id FROM listing WHERE id = ?', (other,))['product_id'] is None
    # A listing that has its product can't be given to another one, even with a fresh screen.
    st, body = call('Alex', '/api/listings/link', {'product_id': p['none'], 'listing_id': lid, 'seen': {}})
    assert st == 409 and 'already belongs to' in body['error'] and 'Sam linked it at' in body['error']
    # Undo: the ASIN and the link come off, and the open sale lets go.
    assert call('Sam', f"/api/listings/link/{k['link_id']}/undo", {})[0] == 200
    assert w._q1('SELECT product_id FROM listing WHERE id = ?', (lid,))['product_id'] is None
    assert not w._q1("SELECT 1 FROM product_identifier WHERE product_id = ? AND kind = 'ASIN'", (p['noasin'],))
    assert w._q1("SELECT product_id FROM sales_line WHERE sku = 'S-CREEP'")['product_id'] is None
    back = next(r for r in call('Sam', '/api/listings/stock?channel=amazon')[1]['rows'] if r['product_id'] == p['noasin'])
    assert back['reason'] == 'no_asin' and len(back['suggestions']) == 2
    assert call('Sam', f"/api/listings/link/{k['link_id']}/undo", {})[0] == 400      # only once
    assert w._q1('SELECT COUNT(*) AS n FROM stock_move')['n'] == moves                 # none of it moved stock
    assert server.wms.check_integrity()['ok']
