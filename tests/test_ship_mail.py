"""Store shipping emails -> packages a carrier label scan finds (Alex 11-16, ship_mail.py).

The emails are made up in the layouts read from the business Gmail on 11-16 (Walmart 'Part N of M' with the items
before the number, Shopify '<Carrier> tracking number:' + 'Items in this shipment', Macy's 'Waiting to ship', Mattel
'Order Items Not Part of this Shipment'). Tracking numbers are made with each carrier's check digit. No network.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import ship_mail as sh  # noqa: E402
from labels import Labels  # noqa: E402
from test_store_mail import make  # noqa: E402


def fedex12(body11: str) -> str:
    d = [int(c) for c in body11]
    return body11 + str(sum(x * (1, 3, 7)[i % 3] for i, x in enumerate(reversed(d))) % 11 % 10)


def ups(body16: str) -> str:
    v = [int(c) if c.isdigit() else (ord(c) - 63) % 10 for c in body16[:15]]
    return '1Z' + body16[:15] + str((10 - (sum(v[0::2]) + 2 * sum(v[1::2])) % 10) % 10)


def usps(body21: str) -> str:
    d = [int(c) for c in body21]
    return body21 + str((10 - sum(x * (3 if i % 2 == 0 else 1) for i, x in enumerate(reversed(d))) % 10) % 10)


F1, F2 = fedex12('52418959678'), fedex12('52418959679')
U1 = ups('279Y50034603209')
P1 = usps('936121101530103389272')
WM_OWN = '75931404050719406084'   # Walmart's own 20-digit number: no carrier format, counts only where labelled

WALMART_2 = f"""Packages are headed your way!
Order date: Sun, Jul 19, 2026
Order number: 2000150-05808642
Your packages shipped, alex!
What to expect
Part 1 of 2
Arrives Fri, Jul 24 Arrives early
4 items See All
4
[img: quantity 4 item Taylor 22lb Ultra Precise Digital Kitchen Scale]
Sold and shipped by Walmart
Fedex tracking number {F1}
Part 2 of 2
Arrives Fri, Jul 24
3 items See All
2
[img: quantity 2 item Taylor 22lb Ultra Precise Digital Kitchen Scale]
1
[img: quantity 1 item Camco RV Tire Cover Pair]
Sold and shipped by Walmart
Fedex tracking number {F2}
Recently viewed items
Camco RV Tire Cover Pair Deluxe
Item 123456789012"""

SHOPIFY = f"""Your order is on the way
Order 105554
Your order is on the way. Track your shipment to see the delivery status.
UPSN tracking number: {U1}
Items in this shipment
Bratz Plush Keychain Blind Box × 4
Monster High Skullector M3GAN Doll × 1 of 2
If you have any questions, reply to this email"""


def order(wms, source, ext, lines, state='ordered', hidden=0):
    with wms._tx() as c:
        po = c.execute("""INSERT INTO purchase_order (source, external_id, ordered_at, imported_at, state, last_seen_at, account,
                                                      review, hidden) VALUES (?,?, '2026-07-19', 'x', ?, 'x', 'gmail', '', ?)""",
                       (source, ext, state, hidden)).lastrowid
        ids = [c.execute("INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents) VALUES (?,?, '', ?,?, 1000)",
                         (po, f'{ext}-{i}', t, q)).lastrowid for i, (t, q) in enumerate(lines)]
    return po, ids


def setup(tmp_path):
    wms, inbound, so = make(tmp_path)
    return wms, inbound, sh.ShipMail(wms), Labels(wms)


def test_a_number_counts_only_next_to_a_tracking_label_or_in_a_carrier_link():
    text = (f"Order number: 2000150-05808642\nUPC: 194145739069\nItem 123456789012\nFedex tracking number {F1}\n"
            f"Tracking #: 333333333333336\nTracking #{fedex12('48920221218')[:11]}0\n"
            f"Track your package: {WM_OWN}\n[Track](https://www.ups.com/track?tracknum={U1}&loc=en_US)")
    got = {t['tracking_id']: t['carrier'] for t in sh.tracking_numbers(text, '', not_these=['2000150-05808642'])}
    # The labelled FedEx, Walmart's own labelled number and the UPS link. Not the UPC, the item id, the order number,
    # the all-3s filler or a FedEx-shaped number whose check digit is wrong (unless the wrong one happens to be right).
    assert got[F1] == 'FedEx' and got[U1] == 'UPS' and got[WM_OWN] == ''
    assert '194145739069' not in got and '123456789012' not in got and '333333333333336' not in got
    assert sh.carrier_format(U1) == ('UPS', True) and sh.carrier_format(P1) == ('USPS', True)
    assert sh.carrier_format(F1[:-1] + str((int(F1[-1]) + 1) % 10)) == ('FedEx', False)


def test_walmart_parts_put_each_item_in_its_own_box():
    r = sh.parse('Shipped: Taylor 22lb Ultra-Prec... and 6 other items', WALMART_2, '', {11: 'Taylor 22lb Ultra Precise Digital Kitchen Scale',
                 12: 'Camco RV Tire Cover Pair'}, {11: 6, 12: 1}, '2000150-05808642')
    assert r['kind'] == 'ship' and r['state'] == 'shipped' and r['more']
    assert [(p['tracking_id'], p['carrier'], p['items']) for p in r['packages']] == [(F1, 'FedEx', {11: 4}), (F2, 'FedEx', {11: 2, 12: 1})]
    assert r['expected_on'] == 'Jul 24'


def test_shopify_shipment_counts_one_of_two_and_waiting_items_stay_out():
    r = sh.parse('A shipment from order 105554 is on the way', SHOPIFY, '', {1: 'Bratz Plush Keychain Blind Box', 2: 'Monster High Skullector M3GAN Doll'},
                 {1: 4, 2: 2})
    assert [(p['tracking_id'], p['carrier'], p['items']) for p in r['packages']] == [(U1, 'UPS', {1: 4, 2: 1})]
    macys = f"Order #:\n4660942726\nShipping now: arrives by Mon. Nov. 10\nTracking #:\n{P1}\nLeapFrog Wooden Activity Cube\nQty: 1\n" \
            "Waiting to ship\nChat and Count Emoji Phone\nQty: 1"
    r = sh.parse('Part of your order has shipped, #4660942726', macys, '', {1: 'LeapFrog Wooden Activity Cube', 2: 'Chat and Count Emoji Phone'}, {1: 1, 2: 1})
    assert [(p['tracking_id'], p['carrier'], p['items']) for p in r['packages']] == [(P1, 'USPS', {1: 1})]
    mattel = f"Tracking #{F1}\nOrder #CHP8716955\nWhat's Inside\nMonster High M3GAN Doll\nQuantity: 2\n" \
             "Order Items Not Part of this Shipment\nMonster High Lost Boys Doll\nQuantity: 2"
    r = sh.parse('Your Mattel Creations Order Has Shipped', mattel, '', {1: 'Monster High Lost Boys Doll', 2: 'Monster High M3GAN Doll'}, {1: 2, 2: 2})
    assert r['packages'][0]['items'] == {2: 2}


def test_one_box_as_big_as_the_order_holds_every_line():
    text = f"Your package shipped, alex!\nOrder number: 2000150-33563507\n20 items See All\n5\n[img: quantity 5 item tonies Pikachu Audio Toy]\n+15\n" \
           f"Track your package: {WM_OWN}"
    lines = {1: 'tonies Pikachu Audio Toy', 2: 'tonies Bulbasaur Audio Toy', 3: 'tonies Squirtle Audio Toy', 4: 'tonies Charmander Audio Toy'}
    r = sh.parse('Shipped: tonies Pikachu Audio T... and 19 other items', text, '', lines, {k: 5 for k in lines})
    assert r['packages'][0]['items'] == {1: 5, 2: 5, 3: 5, 4: 5}


def test_return_and_newsletters_never_give_inbound_tracking():
    assert sh.parse('1 Return(s) delivered', f'Your return was delivered\nFedex tracking number {F1}', '', {}, {})['kind'] == 'return'
    assert sh.parse('Tips to get your shipping setup holiday-ready', 'Sell more this season', '', {}, {})['kind'] == 'not_ship'
    assert sh.parse('Canceled: delivery from order #2000150', 'Canceled', '', {}, {})['kind'] == 'not_ship'


def test_email_ties_packages_to_its_order_and_a_label_scan_opens_it(tmp_path):
    wms, inbound, shm, lb = setup(tmp_path)
    po, (scale, camco) = order(wms, 'walmart.com', '2000150-05808642', [('Taylor 22lb Ultra Precise Digital Kitchen Scale', 6),
                                                                           ('Camco RV Tire Cover Pair', 1)])
    r = shm.take('<s1@wm>', 'Walmart.com <help@walmart.com>', 'Shipped: Taylor 22lb Ultra-Prec... and 6 other items',
                 '2026-07-21T03:59:02Z', WALMART_2)
    assert (r['outcome'], r['po_id'], r['tracking']) == ('tracking', po, [F1, F2])
    assert shm.take('<s1@wm>', 'x', 'x', None, WALMART_2)['outcome'] == 'seen'   # each email once
    c = lb.match('9622001900000000000000' + F2)   # FedEx's 34-digit barcode ends with the 12-digit number
    assert (c['kind'], c['carrier'], c['orders'][0]['order_id']) == ('label', 'FedEx', '2000150-05808642')
    assert sorted((i['title'], i['in_package']) for i in c['orders'][0]['items']) == [('Camco RV Tire Cover Pair', 1),
                                                                                      ('Taylor 22lb Ultra Precise Digital Kitchen Scale', 2)]
    o = wms._q1('SELECT state, status_text, expected_to FROM purchase_order WHERE id = ?', (po,))
    assert o['state'] == 'shipped' and F1 in o['status_text'] and o['expected_to'] == '2026-07-24'
    card = next(x for x in inbound.arriving()['orders'] if x['id'] == po)
    assert [(t['carrier'], t['tracking_id']) for t in card['tracks']] == [('FedEx', F1), ('FedEx', F2)]
    assert card['tracks'][1]['headline'] == '2 x Taylor 22lb Ultra Precise Digital Kitche, 1 x Camco RV Tire Cover Pair'
    assert card['tracks'][0]['note'] == 'Shipped Jul 20 (store email)'   # 03:59 UTC = 11:59 PM ET the day before


def test_order_number_must_be_a_known_order_of_that_store(tmp_path):
    wms, inbound, shm, lb = setup(tmp_path)
    order(wms, 'walmart.com', '2000150-05808642', [('Taylor Scale', 1)])
    order(wms, 'mgae.com', '105554', [('Bratz Plush Keychain Blind Box', 4)], hidden=1)   # Not inventory
    assert shm.take('<a@x>', 'Bratz <bratzstore@mgae.com>', 'A shipment from order 105554 is on the way', None, SHOPIFY)['outcome'] == 'hidden'
    other = SHOPIFY.replace('105554', '999999')
    assert shm.take('<b@x>', 'Bratz <bratzstore@mgae.com>', 'A shipment from order 999999 is on the way', None, other)['outcome'] == 'no_order'
    # The Walmart order's number in another store's email ties nothing.
    assert shm.take('<c@x>', 'Shop <x@target.com>', 'Shipped', None, WALMART_2)['outcome'] == 'no_order'
    assert not wms._q('SELECT * FROM mail_package') and not lb.match(U1).get('orders')


def test_delivered_only_when_the_delivered_boxes_hold_every_unit_and_never_back(tmp_path):
    wms, inbound, shm, lb = setup(tmp_path)
    po, _ = order(wms, 'walmart.com', '2000150-05808642', [('Taylor 22lb Ultra Precise Digital Kitchen Scale', 6),
                                                           ('Camco RV Tire Cover Pair', 1)])
    shm.take('<s1@wm>', 'help@walmart.com', 'Shipped: Taylor', '2026-07-21T15:00:00Z', WALMART_2)
    first = WALMART_2.split('Part 2 of 2')[0].replace('Packages are headed your way!', 'Your package arrived, alex!')
    shm.take('<d1@wm>', 'help@walmart.com', 'Delivered: Taylor 22lb Ultra-Prec...', '2026-07-24T18:00:00Z', first)
    assert wms._q1('SELECT state FROM purchase_order WHERE id = ?', (po,))['state'] == 'shipped'   # box 2 is still coming
    second = 'Your package arrived, alex!\nOrder number: 2000150-05808642\n' + WALMART_2.split('Part 2 of 2')[1]
    shm.take('<d2@wm>', 'help@walmart.com', 'Delivered: Taylor 22lb Ultra-Prec... +1 item', '2026-07-25T18:00:00Z', second)
    o = wms._q1('SELECT state, delivered_on FROM purchase_order WHERE id = ?', (po,))
    assert (o['state'], o['delivered_on']) == ('delivered', '2026-07-25')
    shm.take('<late@wm>', 'help@walmart.com', 'Your package is delayed', '2026-07-26T18:00:00Z',
             'Your package is delayed\nOrder number: 2000150-05808642\nFedex tracking number ' + F1)
    assert wms._q1('SELECT state FROM purchase_order WHERE id = ?', (po,))['state'] == 'delivered'
    assert wms._q1('SELECT state FROM mail_package WHERE tracking_id = ?', (F1,))['state'] == 'delivered'
    assert lb.match(F1)['orders'][0]['state'] == 'delivered'


def test_walmart_page_and_email_for_one_box_count_it_once(tmp_path):
    from test_sweep_inbound import _walmart_po
    wms, so = _walmart_po(tmp_path)
    lb, shm = Labels(wms), sh.ShipMail(wms)
    page = {'order_id': '2000145-74623142', 'subtotal_cents': 1495, 'total_cents': 1594, 'cards': ['4242'], 'items': [
        {'name': 'Pretty Concealer 198 Light', 'item_id': '1', 'qty': 1, 'line_cents': 497, 'status': 'Shipped', 'tracking': F1}]}
    so.walmart_pages([page])
    mail = f'Your package shipped, alex!\nOrder number: 2000145-74623142\n1\n[img: quantity 1 item Pretty Concealer 198 Light]\n' \
           f'Fedex tracking number {F1}'
    assert shm.take('<p@wm>', 'help@walmart.com', 'Shipped: Pretty Concealer 1...', None, mail)['outcome'] == 'tracking'
    c = lb.match(F1)
    assert [(i['in_package'], i['left']) for i in c['orders'][0]['items']] == [(1, 1)] and c['units'] == 1
    line = c['orders'][0]['items'][0]['line_id']
    assert len(lb._packages_of_line(line)) == 1
    so.walmart_pages([page])   # a later page read replaces its own rows and leaves the email's alone
    assert wms._q1('SELECT COUNT(*) AS n FROM mail_package')['n'] == 1 and lb.match(F1)['units'] == 1


def test_delivered_email_with_no_number_that_names_every_unit(tmp_path):
    wms, inbound, shm, lb = setup(tmp_path)
    po, _ = order(wms, 'stanley1913', '13785578', [('Classic Easy-Pour Growler 64 OZ - Hammertone Black', 3),
                                                   ('Classic Easy-Pour Growler 64 OZ - Hammertone Green', 10)])
    text = 'Order: 13785578\nYour Package Has Arrived!\nIn This Package\nClassic Easy-Pour Growler 64 OZ - Hammertone Black\nQuantity: 3\n' \
           'Classic Easy-Pour Growler 64 OZ - Hammertone Green\nQuantity: 10'
    assert shm.take('<st@x>', 'Stanley <info@stanley1913.narvar.com>', 'Your Package Has Arrived!', '2026-11-09T20:00:00Z', text)['outcome'] == 'no_tracking'
    assert wms._q1('SELECT state FROM purchase_order WHERE id = ?', (po,))['state'] == 'delivered'
