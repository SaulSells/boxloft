"""eBay listings become Boxloft items (Alex 11-12, Fable review before the build).

Sam lists on eBay from her phone and never copies a SKU: Boxloft makes the item from the listing (or asks a person
when the title looks like something it has), the box page's strip puts the listed units in an E box with one tap,
eBay sales leave from E boxes only, and backlog items that were never in a box here close by themselves when eBay
ships them.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from audit import Audit  # noqa: E402
from commerce import Commerce  # noqa: E402
from connections import Connections  # noqa: E402
from ebay_link import EbayLink  # noqa: E402
from inbound import Inbound  # noqa: E402
from listings import Listings, parse_ebay_selling  # noqa: E402
from sales_sync import Sales  # noqa: E402
from sorting import Sorting  # noqa: E402
from wms import Conflict, Wms, WmsError, now_utc  # noqa: E402

NS = 'urn:ebay:apis:eBLBaseComponents'


def item(item_id, title, qty=1, pic=True):
    return f"""<Item><ItemID>{item_id}</ItemID>
      <ListingDetails><StartTime>2026-11-0{item_id % 9 + 1}T15:04:05.000Z</StartTime></ListingDetails>
      <ListingType>FixedPriceItem</ListingType><Quantity>{qty}</Quantity>
      <SellingStatus><CurrentPrice currencyID="USD">19.99</CurrentPrice><QuantitySold>0</QuantitySold></SellingStatus>
      {'<PictureDetails><GalleryURL>https://i.ebayimg.com/images/g/abc' + str(item_id) + '/s-l140.jpg</GalleryURL></PictureDetails>' if pic else ''}
      <Title>{title}</Title><QuantityAvailable>{qty}</QuantityAvailable></Item>"""


def page(items):
    return f"""<?xml version="1.0" encoding="UTF-8"?><GetMyeBaySellingResponse xmlns="{NS}"><Ack>Success</Ack>
      <ActiveList><ItemArray>{''.join(items)}</ItemArray><PaginationResult><TotalNumberOfPages>1</TotalNumberOfPages>
      </PaginationResult></ActiveList></GetMyeBaySellingResponse>"""


def setup(tmp_path):
    w = Wms(str(tmp_path / 'w.db'))
    com = Commerce(w)
    Inbound(w)
    Sales(w, com, Connections(str(tmp_path)))
    au = Audit(w, com)
    li = Listings(w, com)
    so = Sorting(w, com, au, None, li)
    eb = EbayLink(w, com, au, li, so)
    return w, com, au, li, so, eb


def read(li, items):
    rows, _ = parse_ebay_selling(page(items))
    return li.upsert('ebay', rows)


def box(w, name):
    return {'box_id': w.create_boxes(1, 1, name=name)[0]['id']}


def sale(w, ext, sku, qty=1, shipped_on_ebay=True):
    with w._tx() as c:
        for col in ('market_status', 'review'):
            w._add_column(c, 'sales_order', col, "TEXT NOT NULL DEFAULT ''")
        o = c.execute("""INSERT INTO sales_order (channel, external_id, ordered_at, status, imported_at)
                         VALUES ('ebay', ?, '2026-11-12', 'open', ?)""", (ext, now_utc())).lastrowid
        if shipped_on_ebay:
            c.execute('UPDATE sales_order SET review = ? WHERE id = ?', (Commerce.MARKED_SHIPPED + ' (ebay: FULFILLED)', o))
        lid = c.execute("INSERT INTO sales_line (order_id, line_key, sku, asin, title, qty) VALUES (?, 'L1', ?, '', 'eBay item', ?)",
                        (o, sku, qty)).lastrowid
    return o, lid


def product_of(w, sku):
    r = w._q1('SELECT product_id FROM listing WHERE sku = ?', (sku,))
    return r['product_id'] if r else None


def test_photo_is_read_at_500px(tmp_path):
    rows, _ = parse_ebay_selling(page([item(101, 'Minecraft Diamond Level Skeleton Action Figure'), item(102, 'No photo here at all', pic=False)]))
    assert rows[0]['image_url'] == 'https://i.ebayimg.com/images/g/abc101/s-l500.jpg'
    assert rows[1]['image_url'] == ''
    w, com, au, li, so, eb = setup(tmp_path)
    li.upsert('ebay', rows)
    assert w._q1("SELECT image_url FROM listing WHERE sku = 'ITEM-101'")['image_url'].endswith('s-l500.jpg')


def arrived(w, pid, name='A1'):
    """The item came in: 1 received into a box (only arrived things are offered as matches, Alex 11-13)."""
    w.receive(1, pid, 1, 500, box(w, name))
    return pid


def test_new_listings_get_items_and_lookalikes_wait_for_a_person(tmp_path):
    w, com, au, li, so, eb = setup(tmp_path)
    arrived(w, w.save_product({'sku': 'B0HELMET01', 'title': 'Daytona Helmets Full Face Motorcycle Helmet Retro DOT', 'owner_id': 1,
                               'identifiers': [{'kind': 'ASIN', 'value': 'B0HELMET01'}]})['id'])
    read(li, [item(201, 'Minecraft Diamond Level Skeleton Action Figure Bedrock Code'),
              item(202, 'Daytona Retro DOT Approved Dull Black Full Face Motorcycle Helmet')])
    _, ln = sale(w, '07-1', 'ITEM-201', shipped_on_ebay=False)
    r = eb.make_missing()
    assert r == {'made': 1, 'waiting': 1}
    pid = product_of(w, 'ITEM-201')
    p = w.get_product(pid)
    assert p['sku'] == 'EBAY-201' and p['image_url'].endswith('abc201/s-l500.jpg')
    assert {(i['kind'], i['value']) for i in p['identifiers']} == {('EBAY_ITEM', 'ITEM-201')}
    assert w._q1('SELECT product_id FROM sales_line WHERE id = ?', (ln,))['product_id'] == pid   # its order follows
    # The helmet looks like a product we have: a person answers, nothing is made.
    assert not product_of(w, 'ITEM-202')
    m = eb.to_match()['listings']
    assert [x['sku'] for x in m] == ['ITEM-202'] and m[0]['candidates'][0]['sku'] == 'B0HELMET01'
    assert eb.gaps()['unmatched'] == 1
    assert eb.make_missing() == {'made': 0, 'waiting': 1}                       # a rerun makes nothing new


def test_same_item_links_and_survives_a_relist(tmp_path):
    w, com, au, li, so, eb = setup(tmp_path)
    hel = w.save_product({'sku': 'B0HELMET01', 'title': 'Daytona Helmets Full Face Motorcycle Helmet Retro DOT', 'owner_id': 1,
                          'identifiers': [{'kind': 'ASIN', 'value': 'B0HELMET01'}]})['id']
    read(li, [item(202, 'Daytona Retro DOT Approved Dull Black Full Face Motorcycle Helmet')])
    eb.match(product_of(w, 'ITEM-202') or w._q1("SELECT id FROM listing WHERE sku = 'ITEM-202'")['id'], 1, product_id=hel)
    assert product_of(w, 'ITEM-202') == hel
    lid = w._q1("SELECT id FROM listing WHERE sku = 'ITEM-202'")['id']
    with pytest.raises(Conflict):
        eb.match(lid, 2, new=True)                                               # the other person, a stale screen
    # The listing ends (sold out); a sale arriving later still finds the item by its eBay number.
    read(li, [item(203, 'Something else entirely different listing words')])
    assert w._q1("SELECT gone_at FROM listing WHERE sku = 'ITEM-202'")['gone_at']
    _, ln = sale(w, '08-2', 'ITEM-202')
    with w._tx() as c:
        com.relink(c)
    assert w._q1('SELECT product_id FROM sales_line WHERE id = ?', (ln,))['product_id'] == hel
    # Undo of the link takes the eBay number off again.
    k = w._q1('SELECT id FROM listing_link WHERE listing_id = ?', (lid,))['id']
    li.unlink(k, 1)
    assert not w._q1("SELECT 1 AS x FROM product_identifier WHERE kind = 'EBAY_ITEM' AND value = 'ITEM-202'")


def test_strip_puts_the_listed_units_in_the_box_once(tmp_path):
    w, com, au, li, so, eb = setup(tmp_path)
    read(li, [item(301, 'Funko Pop Marvel Spider-Man Noir Vinyl Figure', qty=3)])
    eb.make_missing()
    e1 = box(w, 'E1')
    s = eb.to_box()['listings']
    assert len(s) == 1 and s[0]['short'] == 3 and s[0]['in_boxes'] == 0
    lid = s[0]['listing_row']
    r = eb.put(e1, lid, 2, 1, scan_id='tap-1', seen_short=3)
    assert r['kind'] == 'added' and r['qty'] == 2
    assert eb.put(e1, lid, 2, 1, scan_id='tap-1', seen_short=3)['repeat']       # a resend adds nothing
    assert eb.to_box()['listings'][0]['short'] == 1
    with pytest.raises(Conflict):
        eb.put(e1, lid, 3, 2, scan_id='tap-2', seen_short=3)                     # Sam's screen still said 3 missing
    eb.put(e1, lid, 1, 2, scan_id='tap-3', seen_short=1)
    assert eb.to_box()['listings'] == []                                         # all 3 listed units are in E boxes
    assert w.check_integrity(record=False)['ok']


def test_ebay_sale_leaves_from_an_e_box_only(tmp_path):
    w, com, au, li, so, eb = setup(tmp_path)
    read(li, [item(401, 'Hasbro Marvel Legends Wolverine Six Inch Figure', qty=2)])
    eb.make_missing()
    pid = product_of(w, 'ITEM-401')
    a1, e2 = box(w, 'A1'), box(w, 'E2')
    w.receive(1, pid, 1, 500, a1)                                                # older stock, in an Amazon box
    o, ln = sale(w, '09-3', 'ITEM-401')
    with w._tx() as c:
        com.relink(c)
    com.auto_ship()
    assert "only in other markets' boxes (A1)" in w._q1('SELECT review FROM sales_order WHERE id = ?', (o,))['review']
    assert w.get_product(pid)['on_hand'] == 1                                    # the A box kept its unit
    w.receive(1, pid, 1, 500, e2)
    com.auto_ship()
    assert w._q1('SELECT status FROM sales_order WHERE id = ?', (o,))['status'] == 'shipped'
    left = w._q('SELECT box_id, qty FROM stock_level WHERE product_id = ? AND qty > 0 AND box_id != 0', (pid,))
    assert left == [{'box_id': a1['box_id'], 'qty': 1}]
    assert w.check_integrity(record=False)['ok']


def test_backlog_item_shipped_by_ebay_closes_with_no_shelf_change(tmp_path):
    w, com, au, li, so, eb = setup(tmp_path)
    read(li, [item(501, 'All Star Batman Robin Volume One First Print Comic'),
              item(502, 'Energizer Outdoor LED Motion Activated Wireless Spotlight')])
    eb.make_missing()
    b, e = product_of(w, 'ITEM-501'), product_of(w, 'ITEM-502')
    e3 = box(w, 'E3')
    w.receive(1, e, 1, 900, e3)                                                  # the spotlight was boxed once
    w.remove(1, e3, [{'product_id': e, 'qty': 1}], 'ADJUST')
    o1, l1 = sale(w, '08-15415', 'ITEM-501')
    o2, l2 = sale(w, '26-15379', 'ITEM-502')
    with w._tx() as c:
        com.relink(c)
    com.auto_ship()
    assert w._q1('SELECT status FROM sales_order WHERE id = ?', (o1,))['status'] == 'shipped'
    act = w._q1('SELECT shipped_action_id FROM sales_line WHERE id = ?', (l1,))['shipped_action_id']
    moves = w._q('SELECT * FROM stock_move WHERE action_id = ?', (act,))
    assert len(moves) == 1 and moves[0]['to_box_id'] == 0                       # supplier -> customer, no box
    assert w._q1('SELECT cost_missing FROM receipt WHERE action_id = ?', (act,))['cost_missing'] == 1
    # The spotlight sat in a box once: a missing unit is a person's call, never closed silently.
    r2 = w._q1('SELECT status, review FROM sales_order WHERE id = ?', (o2,))
    assert r2['status'] == 'open' and 'none on the shelves' in r2['review']
    assert eb.gaps()['shipped_open'] == 1
    assert w.check_integrity(record=False)['ok']


def test_listed_after_the_first_read_is_not_backlog(tmp_path):
    w, com, au, li, so, eb = setup(tmp_path)
    read(li, [item(601, 'Old listing from before Boxloft knew eBay')])
    with w._tx() as c:   # the first read was earlier; this listing appears on a later read
        c.execute("UPDATE listing SET first_seen = '2026-11-12T15:22:29Z'")
    read(li, [item(601, 'Old listing from before Boxloft knew eBay'), item(602, 'Brand new Lego Star Wars set listed today')])
    eb.make_missing()
    o, ln = sale(w, '10-1', 'ITEM-602')
    with w._tx() as c:
        com.relink(c)
    com.auto_ship()
    # Not backlog (no "listed on eBay before Boxloft" note), but never in a box here either: Alex 11-17, "box what you
    # have", so the sale closes as sold before it came in and a later box keeps its count.
    act = w._q1('SELECT a.note FROM sales_line l JOIN action a ON a.id = l.shipped_action_id WHERE l.id = ?', (ln,))
    assert 'sold before it came in' in act['note'] and 'before Boxloft' not in act['note']


def test_strip_routes_need_a_product_first(tmp_path):
    w, com, au, li, so, eb = setup(tmp_path)
    arrived(w, w.save_product({'sku': 'B0HELMET01', 'title': 'Daytona Helmets Full Face Motorcycle Helmet Retro DOT', 'owner_id': 1})['id'])
    read(li, [item(701,'Daytona Retro DOT Approved Dull Black Full Face Motorcycle Helmet')])
    eb.make_missing()
    s = eb.to_box()['listings']
    assert s[0]['product_id'] is None and s[0]['candidates']
    with pytest.raises(WmsError):
        eb.put(box(w, 'E4'), s[0]['listing_row'], 1, 1)
    r = eb.match(s[0]['listing_row'], 1, new=True)
    assert r['made'] and product_of(w, 'ITEM-701') == r['product_id']


def test_product_markets_lists_every_marketplace_an_item_is_on(tmp_path):
    """Alex 11-12: Inventory by marketplace; an item listed on Amazon and eBay is on both."""
    w, com, au, li, so, eb = setup(tmp_path)
    both = w.save_product({'sku': 'B0BOTH0001', 'title': 'Both places', 'owner_id': 1,
                           'identifiers': [{'kind': 'ASIN', 'value': 'B0BOTH0001'}]})['id']
    li.upsert('amazon', [{'sku': 'AMZ-1', 'asin': 'B0BOTH0001', 'title': 'Both places', 'fulfillment': 'FBM', 'status': 'Active',
                          'qty': 2, 'price_cents': 999, 'listing_id': 'x', 'opened_on': None, 'condition': 'New',
                          'code_type': '', 'code': ''}])
    read(li, [item(901, 'Both places listed on eBay too words'), item(902, 'Only on eBay listing words here')])
    eb.make_missing()
    lid = w._q1("SELECT id FROM listing WHERE sku = 'ITEM-901'")['id']
    w._conn().execute('UPDATE listing SET product_id = NULL WHERE id = ?', (lid,))
    with w._tx() as c:
        c.execute("DELETE FROM product_identifier WHERE value = 'ITEM-901'")
    li.link(both, lid, 1)
    m = li.product_markets()
    assert m[both] == {'amazon', 'ebay'}
    assert m[product_of(w, 'ITEM-902')] == {'ebay'}


def test_sold_out_before_boxloft_closes_like_history(tmp_path):
    """Batman and the Energizer light sold their last unit before Boxloft first read eBay (11-12): their listings
    were never live here, so eBay's shipped orders close with no stock move. A listing still live waits for a person."""
    w, com, au, li, so, eb = setup(tmp_path)
    read(li, [item(801, 'Daytona Retro Helmet listing still for sale words')])
    with w._tx() as c:
        c.execute("UPDATE listing SET first_seen = '2026-11-12T15:22:29Z'")
    o1, l1 = sale(w, '08-15415', 'ITEM-336810899068')            # sold out before the first read
    o2, l2 = sale(w, '26-99999', 'ITEM-801')                     # its listing is live: not history
    with w._tx() as c:
        c.execute("UPDATE sales_order SET ordered_at = '2026-11-10'")
    acts = w._q1('SELECT COUNT(*) AS n FROM action')['n']
    out = com.auto_ship()
    assert out.get('history') == 1
    assert w._q1('SELECT status, review FROM sales_order WHERE id = ?', (o1,)) == {'status': 'shipped', 'review': ''}
    assert w._q1('SELECT COUNT(*) AS n FROM action')['n'] == acts                 # no stock move
    assert w._q1('SELECT status FROM sales_order WHERE id = ?', (o2,))['status'] == 'open'
    # Alex 11-12: it tops the Shipped tab (closed now), not under every order scanned out here
    pid = w.save_product({'sku': 'P-OLD', 'title': 'Scanned earlier', 'owner_id': 1})['id']
    e9 = box(w, 'E9')
    w.receive(1, pid, 1, 100, e9)
    o4, l4 = sale(w, '01-OLD', 'P-OLD', shipped_on_ebay=False)
    with w._tx() as c:
        c.execute("UPDATE sales_line SET product_id = ? WHERE id = ?", (pid, l4))
    com.ship_line(1, l4, e9)
    with w._tx() as c:   # that one closed yesterday
        c.execute("UPDATE sales_order SET closed_at = '2026-11-11T12:00:00Z' WHERE id = ?", (o4,))
        c.execute("UPDATE sales_order SET closed_at = '2099-01-01' WHERE id = ?", (o1,))   # o1 closed "now" (after o4)
    assert [o['external_id'] for o in com.pick_list('shipped')][:2] == ['08-15415', '01-OLD']
    o3, l3 = sale(w, '07-15424', 'ITEM-336808944152')            # ordered on the first read day: never history
    com.auto_ship()
    assert w._q1('SELECT status FROM sales_order WHERE id = ?', (o3,))['status'] == 'open'


def test_ended_listing_is_read_once_for_its_photo(tmp_path, monkeypatch):
    import listings as listings_mod
    w, com, au, li, so, eb = setup(tmp_path)
    sale(w, '07-15424', 'ITEM-336808944152', shipped_on_ebay=False)

    def fake_http(method, url, headers=None, data=None, timeout=20, raw_text=False):
        return 200, f"""<?xml version="1.0"?><GetItemResponse xmlns="{NS}"><Ack>Success</Ack><Item><ItemID>336808944152</ItemID>
          <Title>Minecraft Diamond Level Skeleton Action Figure</Title><PictureDetails><GalleryURL>https://i.ebayimg.com/images/g/mc/s-l140.jpg</GalleryURL>
          </PictureDetails></Item></GetItemResponse>"""
    monkeypatch.setattr(listings_mod, '_http', fake_http)
    monkeypatch.setattr(listings_mod.time, 'sleep', lambda s: None)
    assert li._ebay_ended('tok') == 1
    assert li._ebay_ended('tok') == 0                                             # read once
    row = w._q1("SELECT * FROM listing WHERE sku = 'ITEM-336808944152'")
    assert row['gone_at'] and row['image_url'] == 'https://i.ebayimg.com/images/g/mc/s-l500.jpg'
    card = [ln for o in com.pick_list('open') for ln in o['lines']][0]
    assert card['image_url'] == row['image_url']                                  # the order card shows it
    assert eb.to_box()['listings'] == [] and not eb.gaps()['unmatched']           # an ended listing asks nothing
