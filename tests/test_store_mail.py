import pytest
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
from commerce import Commerce  # noqa: E402
from connections import Connections  # noqa: E402
from inbound import Inbound  # noqa: E402
from sales_sync import Sales  # noqa: E402
import store_mail as sm  # noqa: E402
from wms import Wms  # noqa: E402

# Made-up emails in the shapes seen on 2026-11-10 (Mattel-style lines; Walmart puts names only in img alt).
TOYS = """Order Confirmed
May 03, 2026
Order #TOY12345
Order Total
$236.45
Your Order
Dino Collection Stegosaurus Action Figure
Item #JCH08
Quantity: 8
Price: $30.80
Subtotal: $246.40
You May Also Like
Monster Doll Two Pack Special Edition
Subtotal
$221.76
Tax
$14.69
Total
$236.45
Payment Method
Visa ending with •••• •••• •••• 4242"""

WALMART_HTML = """<html><head><style>.x{color:red}</style></head><body><table><tr><td>Order number: #2000145-74623142</td></tr>
<tr><td><img src="a.jpg" alt="quantity 1 item Pretty Concealer 198 Light"></td></tr>
<tr><td><img src="b.jpg" alt="quantity 1 item Pretty Foundation Medium"></td></tr>
<tr><td>Order total $15.94</td></tr><tr><td>Ending in 4242 | $15.94</td></tr></table></body></html>"""


class FakeJev:
    """Answers like Jev would for these emails, from a small answer key. No network."""

    def __init__(self, order_number, total, items, is_order=0.95):
        self.key = {'order_number': order_number, 'total': total}
        self.items, self.is_order = items, is_order   # items: {name fragment: (unit, line, qty)}
        self.model, self.usage = 'fake', {'requests': 0, 'input_tokens': 0, 'output_tokens': 0}

    def ask(self, state, questions):
        self.usage['requests'] += 1
        out = {}
        for name, q in questions.items():
            if name in ('is_order', 'complete'):
                out[name] = {'type': 'noul', 'noul': self.is_order if name == 'is_order' else 0.9}
            elif name in self.key:
                want = self.key[name]
                out[name] = {'type': 'choice', 'choice': next((c for c in q['criteria'] if c.lstrip('#') == want or c == want), sm.NONE)}
            elif name.startswith('item_'):
                i = int(name[5:])
                line = state.splitlines()[i]
                out[name] = {'type': 'noul', 'noul': 0.9 if any(f in line for f in self.items) else 0.05}
            else:
                kind, k = name.split('_')
                item = state['items'][int(k)]['name']
                unit, line, qty = next(v for f, v in self.items.items() if f in item)
                want = {'unit': unit, 'line': line, 'qty': qty}[kind]
                out[name] = {'type': 'choice', 'choice': next((c for c in q['criteria'] if want is not None and c.replace('$', '') == str(want).replace('$', '')), sm.NONE)}
        return out


def test_card_digits_come_from_code():
    assert sm.card_last4('Visa ending with •••• •••• •••• 4242') == ['4242']
    assert sm.card_last4('Total charged to x -4242 $44.59') == ['4242']
    assert sm.card_last4('Payment:\nVISA 4242') == ['4242']
    assert sm.card_last4('AMEX card image Ending in 1005 $42.52') == ['1005']
    assert sm.card_last4('AMEX\n****1005') == ['1005']  # Macy's (spot-check 11-10)
    assert sm.card_last4('Call 1-800-555-4242 for help') == []


def test_html_keeps_image_alt_text():
    t = sm.text_from_html(WALMART_HTML)
    assert 'quantity 1 item Pretty Concealer 198 Light' in t and 'color:red' not in t
    assert sm.store_of('Walmart.com <help@walmart.com>') == 'walmart.com'
    assert sm.store_of('STANLEY1913 <Orders@stanley1913.narvar.com>') == 'stanley1913'
    assert sm.clean_text('[Dino Figure](https://x.example/p/1)\nDino Figure') == 'Dino Figure'


def test_extract_picks_only_found_values():
    jev = FakeJev('TOY12345', '$236.45', {'Stegosaurus': ('$30.80', '$246.40', '8')})
    r = sm.extract(TOYS, jev)
    assert r['order_number'] == 'TOY12345' and r['total'] == 236.45 and r['cards'] == ['4242']
    assert r['items'] == [{'name': 'Dino Collection Stegosaurus Action Figure', 'qty': 8, 'unit_price': 30.8, 'line_total': 246.4}]
    assert r['flags'] == []


def test_order_total_is_never_one_items_price():
    jev = FakeJev('2000145-74623142', '$15.94', {'Concealer': ('$15.94', '$15.94', None), 'Foundation': (None, None, None)})
    r = sm.extract(sm.text_from_html(WALMART_HTML), jev)
    assert [i['unit_price'] for i in r['items']] == [None, None]
    assert 'some items have no price' in r['flags']


def make(tmp_path):
    wms = Wms(str(tmp_path / 'wms.db'))
    Sales(wms, Commerce(wms), Connections(str(tmp_path)))  # sync_state lives with Sales
    inbound = Inbound(wms)
    return wms, inbound, sm.StoreOrders(wms, Connections(str(tmp_path)))


def test_take_holds_order_for_review_then_arriving(tmp_path):
    wms, inbound, so = make(tmp_path)
    jev = FakeJev('TOY12345', '$236.45', {'Stegosaurus': ('$30.80', '$246.40', '8')})
    r = so.take('<m1@toys>', 'Toys <orders@toys.example.com>', 'Order confirmed', '2026-05-04T01:40:37Z', TOYS, jev, '4242')
    assert r['outcome'] == 'order'
    assert so.take('<m1@toys>', 'x', 'x', None, TOYS, jev, '4242')['outcome'] == 'seen'
    assert jev.usage['requests'] == 2  # the second read made no TypeSafe call
    [o] = so.to_review()
    # sent 01:40 UTC = 9:40 PM ET on May 3: the order day is May 3 (sweep D7)
    assert (o['store'], o['order_id'], o['total_cents'], o['ordered_at']) == ('example.com', 'TOY12345', 23645, '2026-05-03')
    # Tax, shipping and the discount spread over the line: 3080 x 23645/24640.
    assert o['lines'][0]['qty'] == 8 and o['lines'][0]['unit_cost_cents'] == round(3080 * 23645 / 24640)
    assert inbound.arriving()['orders'] == []          # not on Arriving until a person checks it
    so.approve(o['id'], {o['lines'][0]['id']: 3000})
    [a] = inbound.arriving()['orders']
    assert a['source'] == 'example.com' and a['lines'][0]['unit_cost_cents'] == 3000
    assert so.to_review() == []


def test_the_emails_item_link_is_saved_clean_and_shows_on_arriving(tmp_path):
    # Alex 11-13 "grab the link wherever possible": the link on the item, through the store's tracker, no tokens.
    wms, inbound, so = make(tmp_path)
    hops = {'https://trk.toys.example.com/ls/click?upn=X1': 'https://shop.toys.example.com/products/stegosaurus-jch08?_kx=T&utm_source=k'}
    so.resolve = lambda u: sm.resolve_link(u, follow=hops.get)
    html = ('<a href="https://trk.toys.example.com/ls/click?upn=X1">Jurassic World Hammond Collection Lost World Jurassic Park '
            'Stegosaurus Action Figure</a>')
    jev = FakeJev('TOY12345', '$236.45', {'Stegosaurus': ('$30.80', '$246.40', '8')})
    assert so.take('<m9@toys>', 'orders@toys.example.com', 'Order confirmed', None, TOYS, jev, '4242', html=html)['outcome'] == 'order'
    [o] = so.to_review()
    assert o['lines'][0]['store_url'] == 'https://shop.toys.example.com/products/stegosaurus-jch08'
    so.approve(o['id'])
    assert inbound.arriving()['orders'][0]['lines'][0]['url'] == 'https://shop.toys.example.com/products/stegosaurus-jch08'


def test_other_card_and_non_orders_make_no_purchase(tmp_path):
    wms, inbound, so = make(tmp_path)
    jev = FakeJev('TOY12345', '$236.45', {'Stegosaurus': ('$30.80', '$246.40', '8')})
    other = TOYS.replace('4242', '1005')
    assert so.take('<m2@toys>', 'orders@toys.example.com', 's', None, other, jev, '4242')['outcome'] == 'other_card'
    shipped = FakeJev('TOY12345', '$236.45', {}, is_order=0.05)
    assert so.take('<m3@toys>', 'orders@toys.example.com', 's', None, TOYS, shipped, '4242')['outcome'] == 'not_order'
    assert so.take('<m4@toys>', 'orders@toys.example.com', 's', None, 'Thanks for joining!', jev, '4242')['outcome'] == 'no_money'
    assert wms._q1("SELECT COUNT(*) AS n FROM purchase_order")['n'] == 0
    rows = {r['message_id']: r['outcome'] for r in wms._q('SELECT message_id, outcome FROM store_mail')}
    assert rows == {'<m2@toys>': 'other_card', '<m3@toys>': 'not_order', '<m4@toys>': 'no_money'}


def test_resent_confirmation_keeps_one_order(tmp_path):
    wms, inbound, so = make(tmp_path)
    jev = FakeJev('TOY12345', '$236.45', {'Stegosaurus': ('$30.80', '$246.40', '8')})
    a = so.take('<a@toys>', 'orders@toys.example.com', 's', None, TOYS, jev, '4242')
    b = so.take('<b@toys>', 'orders@toys.example.com', 's', None, TOYS, jev, '4242')
    assert a['po_id'] == b['po_id']
    assert wms._q1("SELECT COUNT(*) AS n FROM purchase_line")['n'] == 1


def test_error_is_retried_not_recorded(tmp_path):
    wms, inbound, so = make(tmp_path)

    class Down:
        model, usage = 'x', {'requests': 0}

        def ask(self, *a):
            raise sm.StoreMailError('TypeSafe unreachable')
    assert so.take('<e@toys>', 'orders@toys.example.com', 's', None, TOYS, Down(), '4242')['outcome'] == 'error'
    assert wms._q1('SELECT COUNT(*) AS n FROM store_mail')['n'] == 0
    assert re.search('TypeSafe', sm.StoreMailError('TypeSafe unreachable').args[0])


def test_walmart_page_fills_prices_status_and_drops_cancelled(tmp_path):
    from datetime import date
    wms, inbound, so = make(tmp_path)
    so.connections.save('gmail', {'email': 'a@b.c', 'app_password': 'x', 'card_last4': '4242'})
    jev = FakeJev('2000145-74623142', '$15.94', {'Concealer': ('$15.94', '$15.94', None), 'Foundation': (None, None, None)})
    po = so.take('<w@wm>', 'Walmart.com <help@walmart.com>', 's', '2026-04-26T13:00:00Z', sm.text_from_html(WALMART_HTML), jev, '4242')['po_id']
    assert so.walmart_wanted() == {'orders': ['2000145-74623142']}
    page = {'order_id': '2000145-74623142', 'subtotal_cents': 1495, 'total_cents': 1594, 'cards': ['4242'], 'items': [
        {'name': 'Pretty Concealer 198 Light', 'item_id': '5821564490', 'qty': 1, 'line_cents': 497, 'status': 'Delivered Mon, Apr 27, 5:24pm'},
        {'name': 'Pretty Foundation Medium', 'item_id': '5630285949', 'qty': 1, 'line_cents': 998, 'status': 'Canceled'}]}
    assert so.walmart_pages([page, {'order_id': '999'}]) == {'orders': 2, 'updated': 1, 'unknown': 1, 'no_items': 0}
    lines = wms._q('SELECT title, unit_cost_cents, cancelled, store_item_id FROM purchase_line WHERE po_id = ? ORDER BY id', (po,))
    # 497 x 1594/1495 (tax spread); the cancelled item is off the order.
    assert [(l['unit_cost_cents'], l['cancelled'], l['store_item_id']) for l in lines] == [(round(497 * 1594 / 1495), 0, '5821564490'), (0, 1, None)]
    o = wms._q1('SELECT state, delivered_on, review FROM purchase_order WHERE id = ?', (po,))
    assert o['state'] == 'delivered' and o['delivered_on'].endswith('-04-27') and o['review'] == 'prices from the walmart.com order page'
    assert so.walmart_wanted() == {'orders': []}   # read and finished
    assert sm.walmart_status('Arrives Wed, Nov 4', date(2026, 11, 2))['to'] == '2026-11-04'


def test_walmart_page_name_differs_from_email_keeps_one_line(tmp_path):
    """Alex 11-11: the LeapFrog globe showed twice on the audit (20 + 20). The email and walmart.com name it
    differently; the page read must update the email's line, not add a second one."""
    wms, inbound, so = make(tmp_path)
    with wms._tx() as c:
        po = c.execute("""INSERT INTO purchase_order (source, external_id, ordered_at, imported_at, account)
                          VALUES ('walmart.com', '2000151-51142246', '2026-08-13', 'x', 'gmail')""").lastrowid
        c.execute("""INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents) VALUES (?, ?, '', ?, 20, 4925)""",
                  (po, 'leapfrog magic adventures globe with accessories baby and to',
                   'LeapFrog Magic Adventures Globe with Accessories Baby and Toddler Toys'))
        c.execute("""INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents) VALUES (?, 'crayola crayons 24 pack', '', 'Crayola Crayons 24 Pack', 2, 199)""", (po,))
    page = {'order_id': '2000151-51142246', 'subtotal_cents': 98898, 'total_cents': 98898, 'items': [
        {'name': 'LeapFrog Magic Adventures Globe Child 5-9 Years Academic Interactive Multicolor 1 Each', 'item_id': '597635045',
         'qty': 20, 'line_cents': 98500, 'status': 'Delivered Mon, Aug 17, 10:35am'},
        {'name': 'Crayola Crayons 24 Pack', 'item_id': '111', 'qty': 2, 'line_cents': 398, 'status': 'Delivered Mon, Aug 17, 10:35am'}]}
    so.walmart_pages([page])
    so.walmart_pages([page])   # a second read changes nothing
    lines = wms._q('SELECT title, qty, cancelled, store_item_id FROM purchase_line WHERE po_id = ? ORDER BY id', (po,))
    assert [(l['qty'], l['cancelled'], l['store_item_id']) for l in lines] == [(20, 0, '597635045'), (2, 0, '111')]
    assert lines[0]['title'].startswith('LeapFrog Magic Adventures Globe Child')


def test_skipped_store_never_reaches_the_ai_and_hides_its_queue(tmp_path):
    wms, inbound, so = make(tmp_path)
    assert {'shipbob.com', 'stripe.com', 'godaddy.com'} <= set(so.skipped_stores())
    jev = FakeJev('TOY12345', '$236.45', {'Stegosaurus': ('$30.80', '$246.40', '8')})
    r = so.take('<s1>', 'Klaviyo Inc <invoice+statements+x@stripe.com>', 'Your receipt', None, TOYS, jev, '4242')
    assert r == {'outcome': 'skipped_store', 'po_id': None} and jev.usage['requests'] == 0
    po = so.take('<t1>', 'Toys <orders@toys.example.com>', 'Order confirmed', None, TOYS, jev, '4242')['po_id']
    assert so.skip_store('Example.com') == {'store': 'example.com', 'hidden': 1}
    assert so.to_review() == [] and wms._q1('SELECT hidden FROM purchase_order WHERE id = ?', (po,))['hidden'] == 1
    assert so.take('<t2>', 'Toys <orders@toys.example.com>', 'Order confirmed', None, TOYS, jev, '4242')['outcome'] == 'skipped_store'
    so.skip_store('example.com', skip=False)
    assert 'example.com' not in so.skipped_stores()


def test_one_amount_for_unit_and_line_with_qty_is_the_line_total():
    """Alex 11-10: 10 Barbie dolls showed '$267.52 each'; $267.52 was the order total for all 10."""
    txt = 'Thanks for your order\nOrder# 2000155-93300131\nBarbie Signature LeBron Doll\nQty 10\n$267.52\nTotal $267.52\nVisa ending in 4242'
    jev = FakeJev('2000155-93300131', '$267.52', {'Barbie Signature LeBron Doll': ('$267.52', '$267.52', None)})
    r = sm.extract(txt, jev)
    [it] = r['items']
    assert (it['qty'], it['unit_price'], it['line_total']) == (10, 26.75, 267.52)


def test_approve_refuses_costs_that_cannot_fit_the_total(tmp_path):
    wms, inbound, so = make(tmp_path)
    jev = FakeJev('TOY12345', '$236.45', {'Stegosaurus': ('$30.80', '$246.40', '8')})
    po = so.take('<m1@toys>', 'Toys <orders@toys.example.com>', 'Order confirmed', '2026-05-04T01:40:37Z', TOYS, jev, '4242')['po_id']
    [o] = so.to_review()
    with pytest.raises(sm.StoreMailError, match='add up to'):
        so.approve(po, {o['lines'][0]['id']: 23645})        # the order total typed as the price each
    assert so.to_review()                                    # still waiting
    so.approve(po, {o['lines'][0]['id']: 2956})
    assert not so.to_review()


def test_item_images_by_alt_then_by_position_and_never_noise():
    h = ('<img src="cid:logo.png" height=45><img width=144 alt="Image of Monster High M3GAN Doll" src="https://cdn.shopify.com/a.jpg">'
         '<img width=240 alt="Crate and Barrel" src="https://images.crateandbarrel.com/logo-crateandbarrel.png">'
         '<img width=154 alt="" src="https://images.crateandbarrel.com/is/image/Crate/Trays"><td>Prep and Serve Stacking Grill Trays</td>'
         '<img width=139 height=163 alt="img" src="http://slimages.macys.com/1.tif"><b>Breville BES870XL Barista Express</b>'
         '<img src="https://x.recs.igodigital.com/rr/i/1/BUYER@EMAIL.COM"><img width=1 height=1 src="https://t.example/wf/open?u=1">')
    assert sm.item_images(h, ['Monster High M3GAN Doll', 'Prep and Serve Stacking Grill Trays', 'Breville BES870XL Barista Express',
                              'A thing the email has no photo for']) == [
        'https://cdn.shopify.com/a.jpg', 'https://images.crateandbarrel.com/is/image/Crate/Trays', 'https://slimages.macys.com/1.tif', None]


def test_new_store_order_keeps_item_photos(tmp_path):
    wms, inbound, so = make(tmp_path)
    jev = FakeJev('TOY12345', '$236.45', {'Stegosaurus': ('$30.80', '$246.40', '8')})
    html = '<img width=200 alt="Dino Collection Stegosaurus Action Figure" src="https://cdn.shopify.com/steg.jpg">'
    so.take('<m1@toys>', 'Toys <orders@toys.example.com>', 'Order confirmed', '2026-05-04T01:40:37Z', TOYS, jev, '4242', html)
    [o] = so.to_review()
    assert o['lines'][0]['image_url'] == 'https://cdn.shopify.com/steg.jpg'


BOXERY = """ORDER NUMBER: 581204
ITEMS
DESCRIPTION
QTY
PRICE
TOTAL
10''x8''x4'' Corrugated Shipping Boxes
SKU: CXBSS1084
900
$0.54
$486.00
Kraft Paper Rolls 24'' - 40lb
SKU: PPMKFP24
4
$29.22
$116.88
Merchandise:
$602.88
Total:
$602.88"""


def test_boxery_order_feeds_ship_boxes_not_review(tmp_path):
    from commerce import Commerce
    wms, inbound, so = make(tmp_path)
    com = Commerce(wms)
    jev = FakeJev('581204', '$602.88', {'Corrugated': ('$0.54', '$486.00', '900'), 'Kraft': ('$29.22', '$116.88', '4')})
    r = sm.extract(BOXERY, jev)
    assert [(i['qty'], i['unit_price']) for i in r['items']] == [(900, 0.54), (4, 29.22)]   # QTY column read
    so.take('<b1@boxery>', 'theBoxery <orders@theboxery.com>', 'The Boxery Order Number: 581204', '2026-10-21T15:00:00Z',
            BOXERY, jev, '4242')
    assert so.to_review() == [] and inbound.arriving()['orders'] == []
    [box] = com.list_ship_boxes()
    assert (box['name'], box['inner_l_in'], box['stock'], box['coming']) == ('10 x 8 x 4', 10, 0, 900)
    [o] = com.supplies_incoming()
    box_line = next(ln for ln in o['lines'] if ln['ship_box_id'])
    com.receive_supply(box_line['id'], 500)
    [box] = com.list_ship_boxes()
    assert (box['stock'], box['coming']) == (500, 400)
    with pytest.raises(Exception):
        com.receive_supply(box_line['id'], 401)
    from audit import Audit
    assert Audit(wms, com).unaccounted(inbound)['lines'] == 0


# ---- status lines and cancellations (Alex 11-10: "We're on it!" read as an item; cancelled Walmart orders still waiting)

PLUSH = """We’re on it! Fuzzy Monster 9 inch Al... 👍
Order date: Sat, Mar 21, 2026
Order number: #2000144-30544199
Thanks for your order, alex!
3 items See all
[img: quantity 2 item Fuzzy Monster 9 inch Alien Soft Plush Toy by ZURU]
[img: quantity 3 item Fuzzy Monster 9 inch Count Underoo Plush Toy by ZURU]
[img: quantity 2 item Fuzzy Monster 9 inch Wide eyed Weirdo Plush Toy by ZURU]
Order total
Includes all fees, taxes and discounts $44.80
Payment method
Ending in 4242
$44.80"""


def plush_order(tmp_path, approve=True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    wms, inbound, so = make(tmp_path)
    jev = FakeJev('2000144-30544199', '$44.80', {'Fuzzy Monster': (None, None, None)})
    po = so.take('<p@wm>', 'Walmart.com <help@walmart.com>', 'Thanks for your order', '2026-03-21T17:00:00Z', PLUSH, jev, '4242')['po_id']
    if approve:
        so.approve(po)
    return wms, inbound, so, po


def test_status_headline_is_never_an_item(tmp_path):
    # The fake model says yes to every line with "Fuzzy Monster" in it, the headline too: code must drop it.
    wms, inbound, so, po = plush_order(tmp_path, approve=False)
    titles = [ln['title'] for ln in wms._q('SELECT title FROM purchase_line WHERE po_id = ? ORDER BY id', (po,))]
    assert len(titles) == 3 and not any('on it' in t for t in titles)
    assert sm.is_status_line('We’re on it! Star Wars™ Micro Galax... 👍', ['[img: quantity 1 item Star Wars Micro Galaxy Squadron TIE Reaper]'])
    assert sm.is_status_line('Thanks for your order, alex!') and sm.is_status_line('Your order is on its way')
    assert not sm.is_status_line('Lucerne Fresh Grade A Eggs Family pack,...', ['Kirkland Signature Paper Towels'])
    assert not sm.is_status_line('Thank You Card Set 50 Pack')


def test_two_products_with_one_name_stay_two():
    txt = ('Your order has been submitted.\nReference #: 17551039\nTitle\nQty\nPrice\nDungeon Crawler Carl\n(Signed copy - On order)\n'
           '9780593820247 2 $30.00 $60.00\nDungeon Crawler Carl\n(On order)\n9798217192199 1 $65.00 $65.00\nTotal: USD $138.00')
    jev = FakeJev('17551039', '$138.00', {'Dungeon Crawler Carl': ('$30.00', '$60.00', '2'), 'On order': ('$65.00', '$65.00', '1')})
    names = [i['name'] for i in sm.extract(txt, jev)['items']]
    assert names == ['Dungeon Crawler Carl (Signed copy - On order)', 'Dungeon Crawler Carl (On order)']


def test_clean_lines_drops_saved_headlines_and_names_bare_notes(tmp_path):
    wms, inbound, so, po = plush_order(tmp_path)
    with wms._tx() as c:
        c.execute("INSERT INTO purchase_line (po_id, line_key, title, qty, unit_cost_cents) VALUES (?, 'x1', ?, 1, 0)",
                  (po, 'We’re on it! Fuzzy Monster 9 inch Al... 👍'))
        c.execute("INSERT INTO purchase_line (po_id, line_key, title, qty, unit_cost_cents) VALUES (?, 'x2', '(On order)', 1, 6500)", (po,))
    r = so.clean_lines()
    assert (r['status_lines'], r['renamed'], r['orders']) == (1, 1, 1)
    rows = {x['line_key']: x for x in wms._q('SELECT line_key, title, cancelled FROM purchase_line WHERE po_id = ?', (po,))}
    assert rows['x1']['cancelled'] == 1 and rows['x2']['title'] == 'Fuzzy Monster 9 inch Wide eyed Weirdo Plush Toy by ZURU (On order)'
    assert so.clean_lines()['status_lines'] == 0     # once
    # A one-item Walmart order whose headline hid its cost: the item gets the order total.
    one = PLUSH.replace('3 items See all', '1 item').split('[img: quantity 3')[0] + 'Order total $10.48\nEnding in 4242'
    jev = FakeJev('2000144-83772306', '$10.48', {'Fuzzy Monster': (None, None, None)})
    po2 = so.take('<one@wm>', 'help@walmart.com', 'Thanks', None, one.replace('30544199', '83772306'), jev, '4242')['po_id']
    with wms._tx() as c:
        c.execute('UPDATE purchase_line SET unit_cost_cents = 0 WHERE po_id = ?', (po2,))
        c.execute("INSERT INTO purchase_line (po_id, line_key, title, qty, unit_cost_cents) VALUES (?, 'h', 'We’re on it! Fuzzy... 👍', 1, 0)", (po2,))
    assert so.clean_lines()['costed'] == 1
    assert wms._q1('SELECT unit_cost_cents FROM purchase_line WHERE po_id = ? AND cancelled = 0', (po2,))['unit_cost_cents'] == 524   # 2 units


def test_cancellation_email_takes_off_only_the_named_units(tmp_path):
    from audit import Audit
    wms, inbound, so, po = plush_order(tmp_path)
    mail = ('Pickup window expired. You won’t be charged for these items.\nOrder number: 2000144-30544199\n'
            'Your pickup was canceled, alex\nItems canceled\n2 items\n'
            '[img: quantity 1 item Fuzzy Monster 9 inch Alien Soft Plush Toy by ZURU]\n'
            '[img: quantity 1 item Fuzzy Monster 9 inch Wide eyed Weirdo Plush Toy by ZURU]\nVISA Ending in 4242\n$6.40')
    r = so.take_cancel('<c1@wm>', 'Walmart.com <help@walmart.com>', 'Canceled: pickup of Fuzzy Monster 9 in... +1 item 🔴', None, mail)
    assert r == {'outcome': 'partial', 'po_id': po}
    qty = {x['title'].split(' inch ')[1][:5]: (x['qty'], x['cancelled'])
           for x in wms._q('SELECT title, qty, cancelled FROM purchase_line WHERE po_id = ?', (po,))}
    assert qty == {'Alien': (1, 0), 'Count': (3, 0), 'Wide ': (1, 0)}       # 2-1, untouched, 2-1
    assert so.take_cancel('<c1@wm>', 'x', 'x', None, mail)['outcome'] == 'seen'
    # The rest goes: the whole order is cancelled and leaves Arriving.
    rest = 'Order number: 2000144-30544199\nWe’ve canceled your order for you.\nItems canceled\n' + '\n'.join(
        f'[img: quantity {q} item Fuzzy Monster 9 inch {n} Plush Toy by ZURU]'
        for q, n in ((1, 'Alien Soft'), (3, 'Count Underoo'), (1, 'Wide eyed Weirdo')))
    assert so.take_cancel('<c2@wm>', 'help@walmart.com', 'Canceled: delivery from order #200014430544199', None, rest)['outcome'] == 'cancelled'
    assert wms._q1('SELECT state FROM purchase_order WHERE id = ?', (po,))['state'] == 'cancelled'
    assert inbound.arriving()['orders'] == [] and so.to_review() == []
    assert Audit(wms, Commerce(wms)).unaccounted(inbound)['lines'] == 0


def test_cancellation_of_an_order_in_review_and_unclear_ones(tmp_path):
    wms, inbound, so, po = plush_order(tmp_path / 'a', approve=False)
    assert so.to_review()
    whole = 'Your order #2000144-30544199 has been cancelled. You will not be charged.'
    assert so.take_cancel('<w1@wm>', 'help@walmart.com', 'Your order was cancelled', None, whole)['outcome'] == 'cancelled'
    assert so.to_review() == []
    # Another order, approved; an email that names no item and no whole order goes back to a person.
    wms2, inbound2, so2, po2 = plush_order(tmp_path / 'b')
    vague = 'Order number: 2000144-30544199\nSome items were canceled. See your order for details.'
    assert so2.take_cancel('<u1@wm>', 'help@walmart.com', 'Canceled: items from order', None, vague)['outcome'] == 'unclear'
    [o] = so2.to_review()
    assert 'cancel email' in o['review'] and inbound2.arriving()['orders'] == []
    # Not a store order of ours: no change.
    assert so2.take_cancel('<r1>', 'Brokerage <noreply@brokerage.example>', 'Order Canceled', None, 'Your Bitcoin order was canceled')['outcome'] == 'no_order'
    assert so2.take_cancel('<m1>', 'help@walmart.com', 'Your weekly deals', None, 'Order number: 2000144-30544199')['outcome'] == 'not_cancel'


def test_refund_counts_only_named_units():
    titles = {1: 'TAMASHII NATIONS - Berserk - Schierke', 2: 'TAMASHII NATIONS - Dragon Ball Z', 3: 'Super7 ULTIMATES Ninja Turtles Gorilla'}
    mail = ('ORDER ADJUSTMENTS\nOrder Number: #207175013\nTAMASHII NATIONS - Berserk - Schierke\nRefund: 3 units refunded\n-$44.97\n'
            'ORDER DETAILS\nTAMASHII NATIONS - Dragon Ball Z\nQuantity: 1\nSuper7 ULTIMATES Ninja Turtles Gorilla\nQuantity: 1')
    assert sm.cancel_plan('refund', mail, titles)['lines'] == {1: 3}       # the order details below don't count
    price_drop = 'Order 207175013: $5.00 refunded for a price drop on TAMASHII NATIONS - Dragon Ball Z'
    assert sm.cancel_plan('refund', price_drop, titles)['lines'] == {}
