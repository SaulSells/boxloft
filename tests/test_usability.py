"""The dead ends four test users hit on 2026-11-10: an open order with no product, receiving something with no
product, 40 clicks to record Amazon sales, sold units closed as missing."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from audit import Audit  # noqa: E402
from commerce import Commerce  # noqa: E402
from connections import Connections  # noqa: E402
from inbound import Inbound  # noqa: E402
from listings import Listings  # noqa: E402
from sales_sync import Sales  # noqa: E402
from store_mail import StoreOrders  # noqa: E402
from wms import Wms, WmsError  # noqa: E402

ASIN = 'B02HRPV9L9'
SNAP = 'Bought 2024-11-10 on, unsold as of Amazon sales through 2026-09-16 (units.csv built 2026-10-02)'


def setup(tmp_path, expected=5):
    w = Wms(str(tmp_path / 'w.db'))
    com = Commerce(w)
    Inbound(w)
    conns = Connections(str(tmp_path))
    StoreOrders(w, conns)
    s = Sales(w, com, conns)
    au = Audit(w, com)
    Listings(w, com)
    with w._tx() as c:
        for i, (day, qty, cents) in enumerate((('2026-06-01', 3, 500), ('2026-07-01', 4, 600))):
            po = c.execute("INSERT INTO purchase_order (source, external_id, ordered_at, imported_at) VALUES ('amazon', ?, ?, 'x')",
                           (f'112-000000{i}-0000000', day)).lastrowid
            c.execute('INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents) VALUES (?,?,?,?,?,?)',
                      (po, ASIN, ASIN, 'Filtrete 14x14x1 Air Filter', qty, cents))
    au.import_snapshot([{'asin': ASIN, 'title': 'Filtrete 14x14x1 Air Filter, 6 pack', 'brand': 'Filtrete', 'expected': expected,
                         'avg_cost_cents': 550, 'cost_cents': 550 * expected, 'first_buy': '2026-06-01',
                         'last_buy': '2026-07-01', 'orders': []}], SNAP)
    # The sheet's number holds because Amazon sold the rest before its sales day (Boxloft counts from the 7 bought).
    if expected < 7:
        sale(s, '111-PRESOLD', '2026-08-01', 7 - expected)
    return w, com, s, au


def sale(s, ext, day, qty, state='shipped', sku='6pkFiltrete', asin=ASIN, price=1999):
    s.upsert('amazon', [{'external_id': ext, 'ordered_at': day, 'state': state, 'fulfilled_by': 'me',
                         'lines': [{'line_key': 'OI-' + ext, 'sku': sku, 'asin': asin, 'title': 'Filtrete 14x14x1 Air Filter',
                                    'qty': qty, 'price_cents': price}]}])


# ---------------------------------------------------------------- fix 1: an open order with no product

def test_open_order_with_no_product_can_be_shipped(tmp_path):
    w, com, s, au = setup(tmp_path)
    sale(s, '111-OPEN', '2026-11-09', 1, state='open')
    o = au.order_hints(com.pick_list())[0]
    ln = o['lines'][0]
    assert ln['product_id'] is None and ln['where'] == []
    assert ln['audit'] == {'id': 1, 'asin': ASIN, 'left': 5, 'expected': 5, 'accounted': 0}   # the audit still expects it: say so before a product exists
    r = au.product_for_sale_line(ln['id'], 1)
    assert r['made']
    p = w.get_product(r['product_id'])
    assert p['sku'] == ASIN and p['title'].startswith('Filtrete') and p['default_cost_cents'] == 550
    kinds = {(i['kind'], i['value']) for i in p['identifiers']}
    assert ('ASIN', ASIN) in kinds and ('OTHER', '6pkFiltrete') in kinds   # the seller SKU is remembered
    assert au.product_for_sale_line(ln['id'], 1) == {'product_id': p['id'], 'made': False}   # a second tap makes nothing
    assert w._q1('SELECT COUNT(*) n FROM product')['n'] == 1
    assert w._q1('SELECT COUNT(*) n FROM action')['n'] == 0        # linking moves no stock

    ln = au.order_hints(com.pick_list())[0]['lines'][0]            # linked, 0 on hand: the screen has a next step
    assert ln['product_id'] == p['id'] and ln['where'] == []
    assert ln['audit']['left'] == 5 and ln['coming'] == 5    # 7 bought, 2 sold on Amazon before Boxloft: 5 coming
    with pytest.raises(WmsError):                                  # nothing to take: shipping is refused, stock stays right
        com.ship_line(1, ln['id'], {'box_id': w.create_boxes(1, 1)[0]['id']})
    assert w.check_integrity()['ok']

    iid = au.list()['items'][0]['id']
    box = w.create_boxes(1, 1)[0]['id']
    it = au.found(iid, 2, {'box_id': box}, 1)                      # "Found it" uses the same product
    assert it['product_id'] == p['id']
    ln = au.order_hints(com.pick_list())[0]['lines'][0]
    assert ln['where'][0]['box_id'] == box and ln['audit'] is None and ln['coming'] == 0   # pickable: no hint needed
    assert com.ship_line(1, ln['id'], {'box_id': box})['order_done']
    assert w.get_product(p['id'])['on_hand'] == 1
    assert w.check_integrity()['ok']


def test_sale_line_links_to_an_existing_product_and_new_product_links_the_order(tmp_path):
    w, com, s, au = setup(tmp_path)
    sale(s, '111-A', '2026-11-09', 1, state='open')
    sale(s, '111-B', '2026-11-09', 1, state='open', sku='', asin='')   # no codes at all (a hand-made eBay line)
    a, b = [o['lines'][0] for o in com.pick_list()]
    iid = au.list()['items'][0]['id']
    au.found(iid, 1, {'box_id': w.create_boxes(1, 1)[0]['id']}, 1)     # the audit makes the product first
    pid = w._q1('SELECT id FROM product')['id']
    assert w._q1('SELECT product_id FROM sales_line WHERE id = ?', (a['id'],))['product_id'] == pid   # linked at once
    r = au.product_for_sale_line(b['id'], 1)                           # no ASIN, no SKU: still gets a product
    assert r['made'] and w.get_product(r['product_id'])['sku'] == f"SALE-{b['id']}"


def _listing(w, sku, asin=''):
    with w._tx() as c:
        return c.execute("INSERT INTO listing (channel, sku, asin, title, status, qty, first_seen, seen_at) "
                         "VALUES ('amazon', ?, ?, 'Filtrete 14x14x1 Air Filter', 'Active', 11, 'x', 'x')", (sku, asin)).lastrowid


def test_open_order_links_itself_to_the_audit_row(tmp_path):
    """Alex 11-10, order 111-0692245-5122663: the audit had the Filtrete row with its photo, the order said "no product".
    Same ASIN (or a listing SKU on that ASIN) is the same item: the order links itself, no "Add it" tap."""
    w, com, s, au = setup(tmp_path)
    img = 'https://m.media-amazon.com/images/I/71WpFWi9odL._SS568_.jpg'
    au.import_snapshot([{'asin': ASIN, 'title': 'Filtrete 14x14x1 Air Filter, 6 pack', 'brand': 'Filtrete', 'expected': 5,
                         'image_url': img, 'avg_cost_cents': 550, 'cost_cents': 2750, 'orders': []}], SNAP)
    sale(s, '111-ASIN', '2026-11-10', 1, state='open', sku='6pkFiltrete14x14x1-10-28')
    sale(s, '111-SKU', '2026-11-10', 1, state='open', sku='6pkFiltrete-B', asin='')      # SKU only: the listing has the ASIN
    sale(s, '111-ODD', '2026-11-10', 1, state='open', sku='Kenny28', asin='B0XXWD8R09')  # never bought, not audited
    _listing(w, '6pkFiltrete-B', ASIN)
    assert all(ln['product_id'] is None for o in com.pick_list() for ln in o['lines'])

    assert au.autolink_sales() == 2
    [p] = w._q('SELECT id FROM product')
    p = w.get_product(p['id'])
    assert (p['sku'], p['title'], p['image_url'], p['default_cost_cents']) == (ASIN, 'Filtrete 14x14x1 Air Filter, 6 pack', img, 550)
    kinds = {(i['kind'], i['value']) for i in p['identifiers']}
    assert {('ASIN', ASIN), ('OTHER', '6pkFiltrete14x14x1-10-28')} <= kinds   # the SKU-only line follows its listing (relink)
    by = {o['external_id']: o['lines'][0] for o in au.order_hints(com.pick_list())}
    assert by['111-ASIN']['product_id'] == by['111-SKU']['product_id'] == p['id']
    assert by['111-ASIN']['image_url'] == img                      # the card has the photo before the copy is saved
    assert by['111-ODD']['product_id'] is None                     # no exact match: it keeps its buttons
    assert by['111-ASIN']['audit'] == {'id': 1, 'asin': ASIN, 'left': 5, 'expected': 5, 'accounted': 0}
    assert w._q1('SELECT COUNT(*) n FROM action')['n'] == 0        # linking moves no stock
    assert au.autolink_sales() == 0 and w._q1('SELECT COUNT(*) n FROM product')['n'] == 1   # a second pass makes nothing


def test_open_order_links_to_a_store_row_a_person_matched_to_the_listing(tmp_path):
    """A store-email row has no ASIN. When a person confirmed the listing as that row, an order on its SKU is that row."""
    w, com, s, au = setup(tmp_path)
    with w._tx() as c:
        po = c.execute("""INSERT INTO purchase_order (source, external_id, account, ordered_at, imported_at, state, delivered_on, review)
                          VALUES ('target.com', 'T-1', 'gmail', '2026-09-01', 'x', 'delivered', '2026-09-03', '')""").lastrowid
        line = c.execute("""INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents)
                            VALUES (?, 'k', '', 'LEGO Stegosaurus', 4, 3080)""", (po,)).lastrowid
    [it] = [i for i in au.list()['items'] if i['line_id'] == line]
    lid = _listing(w, 'LEGO-STEGO', 'B0DXXXXXXX')
    with w._tx() as c:
        c.execute("INSERT INTO listing_mark (item_id, kind, platform, listing_id, at) VALUES (?, 'confirm', 'amazon_fbm', ?, 'x')",
                  (it['id'], lid))
    sale(s, '111-LEGO', '2026-11-10', 1, state='open', sku='LEGO-STEGO', asin='')
    assert au.autolink_sales() == 1
    pid = w._q1('SELECT product_id FROM purchase_line WHERE id = ?', (line,))['product_id']
    ln = au.order_hints(com.pick_list())[0]['lines'][0]
    assert pid and ln['product_id'] == pid
    assert ln['audit'] == {'id': it['id'], 'asin': f'LINE-{line}', 'left': 4, 'expected': 4, 'accounted': 0}


def test_found_on_the_order_card_then_ship(tmp_path):
    """The audit expects it and the shelves show 0: Sam marks it found right on the order (the Stock audit's own
    found() call, with the row id the order hint carries), and the same card can ship it."""
    w, com, s, au = setup(tmp_path)
    sale(s, '111-0692245-5122663', '2026-11-10', 1, state='open', sku='6pkFiltrete14x14x1-10-28')
    au.autolink_sales()
    ln = au.order_hints(com.pick_list())[0]['lines'][0]
    assert ln['product_id'] and ln['where'] == [] and ln['audit']['left'] == 5
    box = w.create_boxes(1, 1)[0]['id']
    it = au.found(ln['audit']['id'], 5, {'box_id': box}, 1, note='found on order 111-0692245-5122663')
    assert (it['found'], it['status'], it['product_id']) == (5, 'found', ln['product_id'])
    assert [r['unit_cost_cents'] for r in w._q('SELECT unit_cost_cents FROM receipt ORDER BY id')] == [500, 600]   # the real orders
    ln = au.order_hints(com.pick_list())[0]['lines'][0]
    assert ln['audit'] is None and ln['where'][0]['box_id'] == box and ln['where'][0]['qty'] == 5
    assert com.ship_line(1, ln['id'], {'box_id': box})['order_done']
    assert w.get_product(ln['product_id'])['on_hand'] == 4
    assert au.item(it['id'])['found'] == 5                         # shipping one doesn't change what the audit found
    assert w.check_integrity()['ok']


# ---------------------------------------------------------------- fix 2: receiving something with no product

def test_product_from_purchase_line(tmp_path):
    w, com, s, au = setup(tmp_path)
    lines = com.list_purchases(q='filtrete air')['lines']              # every word matches, any order
    assert len(lines) == 2 and com.list_purchases(q='filtrete dunkin')['lines'] == []
    assert len(com.list_purchases(q='112-0000001')['lines']) == 1      # the order number still works
    newest = lines[0]
    r = au.product_for_purchase_line(newest['id'], 1)
    assert r['made'] and r['title'] == 'Filtrete 14x14x1 Air Filter'
    p = w.get_product(r['product_id'])
    assert p['default_cost_cents'] == 600 and {'kind': 'ASIN', 'value': ASIN} in [dict(kind=i['kind'], value=i['value']) for i in p['identifiers']]
    again = au.product_for_purchase_line(lines[1]['id'], 1)            # the other order of the same item: same product
    assert (again['product_id'], again['made']) == (p['id'], False)
    assert [x['id'] for x in com.open_lines_for(p['id'])] == [lines[1]['id'], newest['id']]   # receive form: both orders
    assert au.list()['items'][0]['product_id'] == p['id']             # and the audit row uses it too


def test_product_from_a_store_line_without_asin(tmp_path):
    w, com, s, au = setup(tmp_path)
    with w._tx() as c:
        po = c.execute("INSERT INTO purchase_order (source, external_id, ordered_at, imported_at) VALUES ('target.com', 'T1', '2026-10-01', 'x')").lastrowid
        lid = c.execute("INSERT INTO purchase_line (po_id, line_key, upc, title, qty, unit_cost_cents) VALUES (?, 'k', '012345678905', 'Stanley cup', 2, 2500)",
                        (po,)).lastrowid
    r = au.product_for_purchase_line(lid, 1)
    assert r['made'] and com.get_line(lid)['product_id'] == r['product_id']
    assert w.lookup('012345678905')[0]['id'] == r['product_id']       # scanning the barcode finds it next time
    assert au.product_for_purchase_line(lid, 1)['made'] is False


# ---------------------------------------------------------------- fix 3: Amazon sales come off by themselves

def test_every_amazon_sale_comes_off_oldest_buy_first(tmp_path):
    """Alex 11-11 night: no sales date and nothing to record by hand. 7 bought (3 in June, 4 in July), 1 sold before
    the old sales day and 2 after it: 3 sold on Amazon, 4 should be here, taken off June's 3 first."""
    w, com, s, au = setup(tmp_path, expected=6)
    sale(s, '111-0', '2026-09-20', 2)
    it = au.list()['items'][0]
    assert (it['bought'], it['amazon_sold'], it['expected'], it['accounted'], it['sheet_expected']) == (7, 3, 4, 0, 6)
    assert sorted(r['units'] for r in w._q('SELECT units FROM line_presold')) == [3]   # all June's
    box = w.create_boxes(1, 1)[0]['id']
    it = au.found(it['id'], 2, {'box_id': box}, 1)
    assert [(r['qty'], r['unit_cost_cents']) for r in w._q('SELECT qty, unit_cost_cents FROM receipt')] == [(2, 600)]
    assert (it['expected'], it['accounted']) == (4, 2)
    sale(s, '111-1', '2026-10-01', 1)                  # a new sale: off the count with no tap
    it = au.item(it['id'])
    assert (it['amazon_sold'], it['expected'], it['accounted'], it['status']) == (4, 3, 2, 'partial')
    assert w.check_integrity()['ok']


def test_close_counts_only_the_units_not_sold(tmp_path):
    w, com, s, au = setup(tmp_path, expected=4)
    sale(s, '111-0', '2026-09-20', 1)
    iid = au.list()['items'][0]['id']
    assert au.item(iid)['expected'] == 3                              # 7 bought, 4 sold on Amazon
    it = au.close(iid, 'not_found', 1, sales='record')                # an old screen's choice: accepted, nothing to do
    assert (it['sold'], it['closed'], it['status']) == (0, True, 'missing')
    assert au.outcomes()['closed']['Not found']['units'] == 3
    assert w.check_integrity()['ok']


def test_a_row_where_everything_sold_has_nothing_to_check(tmp_path):
    w, com, s, au = setup(tmp_path, expected=2)
    sale(s, '111-0', '2026-09-20', 2)                                 # all 7 sold on Amazon, no save on the row
    assert au.list()['items'] == []


def test_order_hints_read_the_audit_once_for_all_lines(tmp_path):
    """Speed sweep 11-12: each _rows call rebuilds the whole audit; it ran once per open line (Orders took 2 s live)."""
    w, com, s, au = setup(tmp_path)
    for n in range(3):
        sale(s, f'111-OPEN{n}', '2026-11-09', 1, state='open')
    calls = []
    real = au._rows
    au._rows = lambda *a, **k: calls.append(a) or real(*a, **k)
    orders = au.order_hints(com.pick_list())
    assert len(calls) == 1
    assert [ln['audit']['left'] for o in orders for ln in o['lines']] == [5, 5, 5]
