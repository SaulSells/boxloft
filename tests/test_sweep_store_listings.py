"""Sweep 11-11 group D (store emails, listings, photo link): each test failed on a9449e7 and passes now."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import store_mail as sm  # noqa: E402
from audit import Audit  # noqa: E402
from commerce import Commerce  # noqa: E402
from photo_id import PhotoId  # noqa: E402
from returns import Returns  # noqa: E402
from test_listings import setup  # noqa: E402
from test_stock_vs_listed import L, box, product  # noqa: E402
from test_store_mail import FakeJev, make, plush_order  # noqa: E402
from wms import LINE_LEFT_SQL, Conflict  # noqa: E402


@pytest.mark.parametrize('body', ['Some items in your order have been canceled. See your order for details.',
                                  'One item in your order has been cancelled. See your order for details.'])
def test_d1_part_of_order_wording_cancels_nothing(tmp_path, body):
    wms, inbound, so, po = plush_order(tmp_path)
    r = so.take_cancel('<x@wm>', 'help@walmart.com', 'Canceled: items from order', None,
                       'Order number: 2000144-30544199\n' + body)
    assert r['outcome'] == 'unclear'
    assert [x['cancelled'] for x in wms._q('SELECT cancelled FROM purchase_line WHERE po_id = ?', (po,))] == [0, 0, 0]
    # the whole-order wording still cancels everything
    assert sm.cancel_plan('cancel', 'Your order has been cancelled.', {1: 'a b c'})['whole']


def test_d2_refund_email_for_a_returned_unit_takes_nothing_more(tmp_path):
    wms, inbound, so, po = plush_order(tmp_path)
    Audit(wms, Commerce(wms))
    rt = Returns(wms, Commerce(wms))
    count = [x for x in wms._q('SELECT id, title FROM purchase_line WHERE po_id = ? ORDER BY id', (po,)) if 'Count' in x['title']][0]
    rt.create(count['id'], 1, 1, state='refunded')
    mail = 'Order number: 2000144-30544199\nRefund issued\n' + count['title'] + '\nRefund: 1 unit refunded\n-$11.20'
    so.take_cancel('<rf1@wm>', 'Walmart.com <help@walmart.com>', 'Your refund for order 2000144-30544199', None, mail)
    row = wms._q1(f'SELECT l.qty, {LINE_LEFT_SQL} AS left_n FROM purchase_line l WHERE l.id = ?', (count['id'],))
    assert (row['qty'], row['left_n']) == (3, 2)


def test_d3_number_after_the_price_is_not_a_quantity():
    text = '\n'.join(['Invoice', 'Order #MX12345', 'Mountain horse female Puffer jacket NEW', '$20.00', '18',
                      'Canon camera case with strap', '$5.00', '62', 'Total', '$25.00'])
    jev = FakeJev('MX12345', '$25.00', {'Mountain horse': ('$20.00', '$20.00', None), 'Canon camera': ('$5.00', '$5.00', None)})
    r = sm.extract(text, jev)
    assert [(i['qty'], i['unit_price']) for i in r['items']] == [(1, 20.0), (1, 5.0)]
    # a QTY column before the price still reads (The Boxery)
    text = '\n'.join(['Order #BX777', '12x10x8 Corrugated Boxes', 'SKU 12108', '900', '$0.54', '$486.00', 'Total', '$486.00'])
    r = sm.extract(text, FakeJev('BX777', '$486.00', {'Corrugated': ('$0.54', '$486.00', None)}))
    assert r['items'][0]['qty'] == 900


def test_d4_undo_of_find_its_listing_lets_every_listing_go(tmp_path):
    w, com, au, li = setup(tmp_path)
    a1 = box(w, 'A1')
    p = product(w, 'P-CREEP', 'NECA Creepshow The Creep 8-Inch Clothed Action Figure')
    w.receive(1, p, 3, 500, {'box_id': a1})
    li.upsert('amazon', [L('S-NEW', 'B0CREEP001', 'NECA Creepshow The Creep 8 Inch Clothed Action Figure', 2),
                         L('S-USED', 'B0CREEP001', 'NECA Creepshow The Creep 8 Inch Clothed Action Figure', 1)])
    ids = {r['sku']: r['id'] for r in w._q('SELECT id, sku FROM listing')}
    k = li.link(p, ids['S-NEW'], 1)
    assert {r['product_id'] for r in w._q('SELECT product_id FROM listing')} == {p}
    li.unlink(k['link_id'], 1)
    assert [r['product_id'] for r in w._q('SELECT product_id FROM listing ORDER BY id')] == [None, None]


def test_d5_photo_link_refuses_an_asin_another_product_holds(tmp_path):
    w, com, au, li = setup(tmp_path)
    pr = PhotoId(w, None, str(tmp_path))
    row = w._q1("SELECT id FROM audit_item WHERE asin = 'B0POKE0001'")
    w.save_product({'sku': 'B0POKE0001', 'title': 'Pokemon Booster Box', 'owner_id': 1,
                    'identifiers': [{'kind': 'ASIN', 'value': 'B0POKE0001'}]})
    y = w.save_product({'sku': 'SAM-1', 'title': 'Pokemon booster box', 'owner_id': 1, 'identifiers': []})['id']
    with pytest.raises(Conflict, match='already on Pokemon Booster Box'):
        pr.link(y, 1, 'audit', row['id'])
    assert w._q1("SELECT COUNT(*) n FROM product_identifier WHERE value = 'B0POKE0001'")['n'] == 1


def test_d6_ebay_listing_is_not_amazon_fbm(tmp_path):
    w, com, au, li = setup(tmp_path)
    li.upsert('amazon', [L('SKU-POKE', 'B0POKE0001', 'Pokemon Booster Box', 3)])
    pid = w.save_product({'sku': 'B0POKE0001', 'title': 'Pokemon Booster Box', 'owner_id': 1,
                          'identifiers': [{'kind': 'ASIN', 'value': 'B0POKE0001'}]})['id']
    li.upsert('ebay', [{'sku': 'ITEM-111', 'asin': '', 'title': 'Pokemon Booster Box sealed', 'fulfillment': 'FBM',
                        'status': 'Active', 'qty': 5, 'price_cents': 15000, 'listing_id': '111', 'opened_on': None,
                        'condition': 'New', 'code_type': 'SKU', 'code': 'B0POKE0001'}])
    s = li.list()['summary']
    assert (s['active_fbm'], s['units_fbm']) == (1, 3)
    items = w._q("SELECT * FROM audit_item WHERE asin = 'B0POKE0001'")
    for it in items:
        it['product_id'] = pid
    li.annotate(items)
    assert {(e['sku'], e['platform'], e['label']) for e in items[0]['listed']} == {
        ('SKU-POKE', 'amazon_fbm', 'Amazon FBM'), ('ITEM-111', 'ebay', 'eBay')}


def test_d7_store_order_day_is_the_et_day(tmp_path):
    assert sm.et_day('2026-05-04T01:40:37Z') == '2026-05-03'
    assert sm.et_day('2026-05-04T15:00:00Z') == '2026-05-04'
    wms, inbound, so = make(tmp_path)
    jev = FakeJev('TOY12345', '$236.45', {'Stegosaurus': ('$30.80', '$246.40', '8')})
    so.take('<m1@toys>', 'Toys <orders@toys.example.com>', 'Order confirmed', '2026-05-04T01:40:37Z', sm_toys(), jev, '4242')
    assert wms._q1('SELECT ordered_at FROM purchase_order')['ordered_at'] == '2026-05-03'


def sm_toys():
    from test_store_mail import TOYS
    return TOYS


def test_d8_item_list_read_twice_counts_once(tmp_path):
    wms, inbound, so = make(tmp_path)
    r = {'order_number': 'CB-1', 'total': 65.05, 'cards': ['4242'], 'flags': [], 'items': [
        {'name': 'Prep Trays, Set of 2', 'qty': 1, 'unit_price': 34.95, 'line_total': 34.95},
        {'name': 'Mirror Serving Tongs', 'qty': 1, 'unit_price': 25.0, 'line_total': 25.0},
        {'name': 'Prep Trays, Set of 2', 'qty': 1, 'unit_price': 34.95, 'line_total': 34.95},
        {'name': 'Mirror Serving Tongs', 'qty': 1, 'unit_price': 25.0, 'line_total': 25.0}]}
    po = so._save('crateandbarrel.com', '<cb@x>', '2026-10-01T15:00:00Z', r, '4242')
    assert sorted(x['qty'] for x in wms._q('SELECT qty FROM purchase_line WHERE po_id = ?', (po,))) == [1, 1]
    assert 'listed twice' in wms._q1('SELECT review FROM purchase_order WHERE id = ?', (po,))['review']
    # two real lines of one product (two domain registrations) stay two, with a note to check the quantities
    r2 = {'order_number': 'GD-1', 'total': 26.38, 'cards': ['4242'], 'flags': [], 'items': [
        {'name': '.COM Domain Registration', 'qty': 1, 'unit_price': 13.19, 'line_total': 13.19},
        {'name': 'acmehub.com', 'qty': 1, 'unit_price': 22.99, 'line_total': 22.99},
        {'name': '.COM Domain Registration', 'qty': 1, 'unit_price': 13.19, 'line_total': 13.19},
        {'name': 'acmedashboard.com', 'qty': 1, 'unit_price': 22.99, 'line_total': 22.99}]}
    po2 = so._save('godaddy.com', '<gd@x>', '2026-10-01T15:00:00Z', r2, '4242')
    assert sum(x['qty'] for x in wms._q('SELECT qty FROM purchase_line WHERE po_id = ?', (po2,))) == 4
    assert 'check the quantities' in wms._q1('SELECT review FROM purchase_order WHERE id = ?', (po2,))['review']
