import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from audit import Audit  # noqa: E402
from commerce import Commerce  # noqa: E402
from inbound import Inbound  # noqa: E402
from listings import Listings, parse_merchant_listings, title_score  # noqa: E402
from sales_sync import Sales  # noqa: E402
from connections import Connections  # noqa: E402
from wms import Wms, WmsError  # noqa: E402

HEAD = ('﻿item-name\titem-description\tlisting-id\tseller-sku\tprice\tquantity\topen-date\timage-url\titem-is-marketplace\t'
        'product-id-type\tzshop-shipping-fee\titem-note\titem-condition\tzshop-category1\tzshop-browse-path\t'
        'zshop-storefront-feature\tasin1\tasin2\tasin3\twill-ship-internationally\texpedited-shipping\tzshop-boldface\t'
        'product-id\tbid-for-featured-placement\tadd-delete\tpending-quantity\tfulfillment-channel\tmerchant-shipping-group\tstatus')


def line(name, sku, price, qty, asin, fc='DEFAULT', status='Active'):
    cols = [name, '', 'L' + sku, sku, price, qty, '2026-01-05 10:00:00 PST', '', 'y', '1', '', '', '11', '', '', '',
            asin, '', '', '', '', '', asin, '', '', '', fc, 'Migrated Template', status]
    return '\t'.join(cols)


REPORT = '\n'.join([HEAD,
                    line('Pokemon Scarlet Violet Booster Box 36 Packs', 'SKU-POKE', '149.99', '3', 'B0POKE0001'),
                    line("Speedo Men's Swimsuit Brief Endurance+", 'SKU-SPEEDO', '26.09', '', 'B0YIJLHFEJ', 'AMAZON_NA'),
                    line('Old thing', 'SKU-OLD', '9.99', '0', 'B0OLD00001', status='Inactive')])


def setup(tmp_path):
    w = Wms(str(tmp_path / 'w.db'))
    com = Commerce(w)
    Inbound(w)
    Sales(w, com, Connections(str(tmp_path)))
    au = Audit(w, com)
    li = Listings(w, com)
    au.import_snapshot([
        {'asin': 'B0POKE0001', 'title': 'Pokemon Booster Box', 'expected': 4, 'orders': []},
        {'asin': 'B0LEGO0001', 'title': 'LEGO Star Wars Millennium Falcon 75257 Building Kit', 'expected': 1, 'orders': []},
        {'asin': 'B0NONE0001', 'title': 'Something nobody listed', 'expected': 2, 'orders': []}], 'test')
    return w, com, au, li


def test_parse_real_header_shape():
    rows = parse_merchant_listings(REPORT)
    assert [r['sku'] for r in rows] == ['SKU-POKE', 'SKU-SPEEDO', 'SKU-OLD']
    poke, speedo, old = rows
    assert poke['title'].startswith('Pokemon') and poke['qty'] == 3 and poke['price_cents'] == 14999
    assert poke['fulfillment'] == 'FBM' and poke['condition'] == 'New' and poke['opened_on'] == '2026-01-05'
    assert speedo['fulfillment'] == 'FBA' and speedo['qty'] == 0
    assert old['status'] == 'Inactive'


def test_upsert_fba_qty_and_gone(tmp_path):
    w, com, au, li = setup(tmp_path)
    r = li.upsert('amazon', parse_merchant_listings(REPORT),
                  {'SKU-SPEEDO': {'fulfillable': 2, 'inbound': 1, 'reserved': 0, 'unfulfillable': 0}})
    assert (r['new'], r['fbm_active'], r['fba_active'], r['gone']) == (3, 1, 1, 0)
    sp = w._q1("SELECT * FROM listing WHERE sku = 'SKU-SPEEDO'")
    assert (sp['qty'], sp['fba_inbound'], sp['fulfillment']) == (2, 1, 'FBA')
    # Next full report without SKU-OLD: it is marked gone, not deleted.
    r = li.upsert('amazon', parse_merchant_listings(REPORT)[:2])
    assert (r['new'], r['gone']) == (0, 1)
    assert li.list()['summary']['listings'] == 2
    assert len(li.list(include_gone=True)['listings']) == 3
    # It comes back if it shows up again.
    li.upsert('amazon', parse_merchant_listings(REPORT))
    assert w._q1("SELECT gone_at FROM listing WHERE sku = 'SKU-OLD'")['gone_at'] is None


def test_suggest_confirm_not_this_manual_undo(tmp_path):
    w, com, au, li = setup(tmp_path)
    li.upsert('amazon', parse_merchant_listings(REPORT + '\n' + line(
        'LEGO Star Wars Millennium Falcon 75257 Starship Building Kit (1351 Pieces)', 'SKU-LEGO', '159.00', '1', 'B0OTHER001')))
    items = au.list()['items']
    li.annotate(items)
    assert [it for it in items if it['asin'] == 'B0LEGO0001'][0]['listed'] == []   # an ASIN row never matches on title
    with w._tx() as c:   # a store-email row does
        c.execute("UPDATE audit_item SET line_id = 999 WHERE asin = 'B0LEGO0001'")
    items = au.list()['items']
    counts = li.annotate(items)
    by = {it['asin']: it for it in items}
    poke = by['B0POKE0001']['listed']
    assert len(poke) == 1 and poke[0]['state'] == 'suggested' and poke[0]['match'] == 'ASIN' and poke[0]['qty'] == 3
    lego = by['B0LEGO0001']['listed']   # different ASIN, same product by title
    assert len(lego) == 1 and lego[0]['match'].startswith('title words')
    assert by['B0NONE0001']['listed'] == []
    assert (counts['rows_for_sale'], counts['rows_for_sale_confirmed']) == (2, 0)
    assert poke[0]['live'] and lego[0]['live']

    iid = {it['asin']: it['id'] for it in items}
    li.mark(iid['B0POKE0001'], {'kind': 'confirm', 'listing_id': poke[0]['listing_id']}, 1)
    li.mark(iid['B0LEGO0001'], {'kind': 'not_this', 'listing_id': lego[0]['listing_id']}, 1)
    m = li.mark(iid['B0NONE0001'], {'kind': 'manual', 'platform': 'ebay', 'qty': 2, 'url': 'https://www.ebay.com/itm/1'}, 1)
    items = au.list()['items']
    counts = li.annotate(items)
    by = {it['asin']: it for it in items}
    assert [x['state'] for x in by['B0POKE0001']['listed']] == ['confirmed']
    assert by['B0LEGO0001']['listed'] == []      # "not this" is remembered
    assert by['B0NONE0001']['listed'][0]['label'] == 'eBay' and by['B0NONE0001']['listed'][0]['qty'] == 2
    assert (counts['rows_for_sale'], counts['rows_for_sale_confirmed'], counts['units_listed']) == (2, 2, 5)
    assert li.list()['summary']['manual'] == 1

    li.undo_mark(iid['B0NONE0001'], m['id'], 1)
    with pytest.raises(WmsError):
        li.undo_mark(iid['B0NONE0001'], m['id'], 1)
    items = au.list()['items']
    li.annotate(items)
    assert {it['asin']: it for it in items}['B0NONE0001']['listed'] == []
    with pytest.raises(WmsError):
        li.mark(iid['B0NONE0001'], {'kind': 'manual', 'platform': 'myspace', 'qty': 1}, 1)
    with pytest.raises(WmsError):
        li.mark(iid['B0NONE0001'], {'kind': 'manual', 'platform': 'ebay', 'qty': 1, 'url': 'javascript:alert(1)'}, 1)
    with pytest.raises(Exception):   # append-only
        w._conn().execute('DELETE FROM listing_mark')


def test_found_product_links_listing_and_sold_lines(tmp_path):
    w, com, au, li = setup(tmp_path)
    s = Sales(w, com, Connections(str(tmp_path)))
    s.upsert('amazon', [{'external_id': '111-1', 'ordered_at': '2026-11-01', 'state': 'shipped', 'fulfilled_by': 'me',
                         'lines': [{'line_key': 'OI1', 'sku': 'SKU-POKE', 'asin': 'B0POKE0001', 'title': 'Pokemon', 'qty': 1}]}])
    li.upsert('amazon', parse_merchant_listings(REPORT))
    assert w._q1("SELECT product_id FROM listing WHERE sku = 'SKU-POKE'")['product_id'] is None
    iid = [it for it in au.list()['items'] if it['asin'] == 'B0POKE0001'][0]['id']
    box = w.create_boxes(1, 1)[0]['id']
    assert li.list()['summary']['fbm_over'] == 1    # listed 3, none on our shelves yet
    au.found(iid, 2, {'box_id': box}, 1)           # makes the product (ASIN identifier)
    # No refresh from Amazon needed: the listing and the sold line point at the new product right away.
    pid = w._q1("SELECT product_id FROM product_identifier WHERE value = 'B0POKE0001'")['product_id']
    assert w._q1("SELECT product_id FROM sales_line WHERE sku = 'SKU-POKE'")['product_id'] == pid
    row = [x for x in li.list()['listings'] if x['sku'] == 'SKU-POKE'][0]
    assert row['product_id'] == pid and row['on_hand'] == 2 and row['audit_id'] == iid
    assert li.list()['summary']['fbm_over'] == 1    # listed 3, 2 on our shelves
    au.found(iid, 1, {'box_id': box}, 1)
    assert li.list()['summary']['fbm_over'] == 0
    r = li.upsert('amazon', parse_merchant_listings(REPORT))
    assert r['sales_lines_linked'] == 0             # nothing was left to link


def test_listing_on_hand_without_stored_link(tmp_path):
    """The stored listing.product_id can be empty (product made by a path that never relinked): reads still match."""
    w, com, au, li = setup(tmp_path)
    li.upsert('amazon', parse_merchant_listings(REPORT))
    pid = w.save_product({'sku': 'MINE-1', 'title': 'Pokemon box', 'identifiers': [{'kind': 'ASIN', 'value': 'b0poke0001'}]})['id']
    w.receive(1, pid, 5, 100, {'box_id': w.create_boxes(1, 1)[0]['id']})
    assert w._q1("SELECT product_id FROM listing WHERE sku = 'SKU-POKE'")['product_id'] is None
    row = [x for x in li.list()['listings'] if x['sku'] == 'SKU-POKE'][0]
    assert (row['product_id'], row['on_hand'], row['live']) == (pid, 5, True)
    assert li.list()['summary']['fbm_over'] == 0
    items = [{'id': 1, 'asin': '', 'product_id': pid, 'title': 'x'}]   # an audit row matched by its product
    li.annotate(items)
    assert items[0]['listed'][0]['match'] == 'product'


def test_for_sale_now_is_one_definition(tmp_path):
    """Inactive and 0-qty listings still show on the row, but never count as for sale now."""
    w, com, au, li = setup(tmp_path)
    au.import_snapshot([{'asin': 'B0OLD00001', 'title': 'Old thing', 'expected': 1, 'orders': []},
                        {'asin': 'B0YIJLHFEJ', 'title': 'Speedo', 'expected': 1, 'orders': []}], 'test')
    li.upsert('amazon', parse_merchant_listings(REPORT))    # POKE active 3, SPEEDO active 0 (FBA, none there), OLD inactive
    items = au.list()['items']
    counts = li.annotate(items)
    by = {it['asin']: it for it in items}
    assert [x['live'] for x in by['B0OLD00001']['listed']] == [False]
    assert [x['live'] for x in by['B0YIJLHFEJ']['listed']] == [False]
    assert counts['rows_for_sale'] == 1 == sum(1 for it in items if any(x['live'] for x in it['listed']))
    li.mark(by['B0OLD00001']['id'], {'kind': 'confirm', 'listing_id': by['B0OLD00001']['listed'][0]['listing_id']}, 1)
    items = au.list()['items']
    counts = li.annotate(items)      # confirmed, but inactive: still not for sale now
    assert (counts['rows_for_sale'], counts['rows_for_sale_confirmed'], counts['units_listed']) == (1, 0, 0)


def test_title_score():
    assert title_score('Pokemon Booster Box', 'Pokemon Scarlet Violet Booster Box 36 Packs') == 1.0
    assert title_score('a b', 'a b') == 0.0          # too short to trust
