"""eBay matching offers only things that arrived (Alex 11-13).

"For the eBay item matching. Don't offer matches to other eBay listings. It should offer matches to items that have
arrived, either received already or not." His screen: WALKING DEAD #41 offered "Same as: Walking Dead #59" and "#52",
items that exist only because other eBay listings made them.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from test_ebay_link import arrived, item, product_of, read, setup  # noqa: E402

HELMET = 'Daytona Helmets Full Face Motorcycle Helmet Retro DOT'


def delivered_buy(w, title, asin='', product_id=None, qty=1, state='delivered'):
    with w._tx() as c:
        po = c.execute("""INSERT INTO purchase_order (source, external_id, ordered_at, imported_at, state)
                          VALUES ('amazon', ?, '2026-10-01', 'x', ?)""", (f'112-{asin or title[:6]}', state)).lastrowid
        return c.execute("""INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents, product_id)
                            VALUES (?,?,?,?,?,?,?)""", (po, asin or title[:10], asin, title, qty, 700, product_id)).lastrowid


def listing_row(w, sku):
    return w._q1('SELECT id FROM listing WHERE sku = ?', (sku,))['id']


def test_other_ebay_listings_are_never_offered(tmp_path):
    w, com, au, li, so, eb = setup(tmp_path)
    read(li, [item(301, 'The Walking Dead # 59 NM 1st Print Image Comic Book Kirkman')])
    assert eb.make_missing()['made'] == 1
    read(li, [item(301, 'The Walking Dead # 59 NM 1st Print Image Comic Book Kirkman'),
              item(302, 'WALKING DEAD #41 CVR A 1st Print Carol Bitten NM Image Comics 2007')])
    assert eb.to_match()['listings'][0]['candidates'] == []        # #59 is only another listing's item
    assert eb.make_missing() == {'made': 1, 'waiting': 0}           # so #41 becomes its own item


def test_an_item_that_never_arrived_is_not_offered_and_arrived_ones_say_how(tmp_path):
    w, com, au, li, so, eb = setup(tmp_path)
    never = w.save_product({'sku': 'P-NEVER', 'title': HELMET, 'owner_id': 1})['id']
    read(li, [item(401, 'Daytona Retro DOT Approved Dull Black Full Face Motorcycle Helmet')])
    lid = listing_row(w, 'ITEM-401')
    assert eb.to_match()['listings'][0]['candidates'] == []
    delivered_buy(w, 'Daytona helmet', product_id=never)              # delivered, not scanned in yet: offered
    c = eb.to_match()['listings'][0]['candidates']
    assert [(x['id'], x['why']) for x in c] == [(never, 'Delivered, not scanned in yet')]
    arrived(w, never, 'A2')
    assert eb.to_match()['listings'][0]['candidates'][0]['why'] == '1 on the shelves'
    assert eb.match(lid, 1, product_id=never)['product_id'] == never


def test_a_delivered_buy_with_no_item_is_offered_and_its_link_makes_the_item(tmp_path):
    w, com, au, li, so, eb = setup(tmp_path)
    line = delivered_buy(w, HELMET, asin='B0HELMET01')
    delivered_buy(w, HELMET + ' Gloss', asin='B0HELMET02', state='ordered')
    read(li, [item(501, 'Daytona Retro DOT Approved Dull Black Full Face Motorcycle Helmet')])
    lid = listing_row(w, 'ITEM-501')
    c = eb.to_match()['listings'][0]['candidates']
    assert [(x['kind'], x['id']) for x in c] == [('line', line)]      # the one still on its way is not offered
    assert c[0]['why'].startswith('Delivered') and c[0]['why'].endswith('not scanned in yet')
    r = eb.match(lid, 1, line_id=line)
    assert w._q1('SELECT product_id FROM purchase_line WHERE id = ?', (line,))['product_id'] == r['product_id']
    assert product_of(w, 'ITEM-501') == r['product_id']


def test_common_words_alone_are_not_a_guess(tmp_path):
    # Live 11-13: "Marvel Dazzler Action Figure" was offered for a Transformers Optimus Prime (shared: action figure).
    w, com, au, li, so, eb = setup(tmp_path)
    for i, name in enumerate(['Marvel Dazzler Action Figure', 'Hulk Legends Action Figure', 'Batman Classic Action Figure',
                              'Spawn Deluxe Action Figure', 'Godzilla Kaiju Action Figure', 'Daytona Retro DOT Full Face Helmet Dull Black']):
        arrived(w, w.save_product({'sku': f'P{i}', 'title': name, 'owner_id': 1})['id'], f'A{i + 1}')
    read(li, [item(601, 'Transformers Cyberverse Optimus Prime Scout Class Action Figure'),
              item(602, 'Daytona Retro DOT Approved Dull Black Full Face Motorcycle Helmet')])
    got = {r['sku']: [c['title'] for c in r['candidates']] for r in eb.to_match()['listings']}
    assert got == {'ITEM-601': [], 'ITEM-602': ['Daytona Retro DOT Full Face Helmet Dull Black']}


# Alex 11-12 10:41 PM ET: "These can be items received as well. Anything in the audit. Anything currently listed on
# amazon or Walmart. Anything in a box. Anything marked delivered even if not put away yet."
FIGURE = 'NECA Creepshow Tales From The Crypt Creep Ultimate Action Figure 8 inch'
EBAY_FIGURE = 'NECA Creepshow The Creep Ultimate 8 inch Action Figure Tales Crypt'


def amazon_listing(li, sku, asin, title, qty=2, status='Active', fulfillment='FBM'):
    row = {'sku': sku, 'asin': asin, 'title': title, 'fulfillment': fulfillment, 'status': status, 'qty': qty,
           'price_cents': 2999, 'listing_id': '', 'opened_on': '2026-10-01', 'condition': 'New', 'code_type': '1',
           'code': asin, 'image_url': ''}
    return li.upsert('amazon', [row], full=False)


def audit_row(w, asin, title):
    with w._tx() as c:
        return c.execute("INSERT INTO audit_item (asin, title, expected, imported_at) VALUES (?,?,1,'x')", (asin, title)).lastrowid


def test_a_live_amazon_listing_with_no_item_is_offered_and_its_link_makes_the_item(tmp_path):
    w, com, au, li, so, eb = setup(tmp_path)
    amazon_listing(li, 'CreepshowTC8-6', 'B0CREEP001', FIGURE)
    amazon_listing(li, 'OLD-1', 'B0CREEP002', FIGURE + ' Variant', status='Inactive')   # not for sale now: left out
    read(li, [item(701, EBAY_FIGURE)])
    lid = listing_row(w, 'ITEM-701')
    c = eb.to_match()['listings'][0]['candidates']
    amz = listing_row(w, 'CreepshowTC8-6')
    assert [(x['kind'], x['id'], x['why']) for x in c] == [('listing', amz, 'Listed on Amazon FBM, 2 for sale')]
    assert eb.make_missing() == {'made': 0, 'waiting': 1}
    r = eb.match(lid, 1, market_listing=amz)
    assert product_of(w, 'ITEM-701') == product_of(w, 'CreepshowTC8-6') == r['product_id']
    assert w._q1('SELECT sku FROM product WHERE id = ?', (r['product_id'],))['sku'] == 'B0CREEP001'
    assert eb.to_match()['listings'] == []


def test_one_item_shows_once_with_every_reason(tmp_path):
    w, com, au, li, so, eb = setup(tmp_path)
    pid = arrived(w, w.save_product({'sku': 'P-CREEP', 'title': FIGURE, 'owner_id': 1,
                                     'identifiers': [{'kind': 'ASIN', 'value': 'B0CREEP001'}]})['id'], 'A3')
    audit_row(w, 'B0CREEP001', FIGURE)
    amazon_listing(li, 'CreepshowTC8-6', 'B0CREEP001', FIGURE)
    read(li, [item(702, EBAY_FIGURE)])
    c = eb.to_match()['listings'][0]['candidates']
    assert [(x['kind'], x['id'], x['why']) for x in c] == [('product', pid, '1 on the shelves · On the Stock audit')]


def test_an_audit_row_and_its_amazon_listing_are_one_guess(tmp_path):
    w, com, au, li, so, eb = setup(tmp_path)
    row = audit_row(w, 'B0CREEP001', FIGURE)
    amazon_listing(li, 'CreepshowTC8-6', 'B0CREEP001', FIGURE, fulfillment='FBA')
    with w._tx() as c:
        c.execute("UPDATE listing SET qty = 3 WHERE sku = 'CreepshowTC8-6'")
    read(li, [item(703, EBAY_FIGURE)])
    c = eb.to_match()['listings'][0]['candidates']
    assert [(x['kind'], x['id'], x['why']) for x in c] == [
        ('audit', row, 'On the Stock audit: bought, not found yet · Listed on Amazon FBA, 3 for sale')]


def test_an_audit_item_that_never_arrived_is_offered_but_never_another_ebay_listings(tmp_path):
    w, com, au, li, so, eb = setup(tmp_path)
    pid = w.save_product({'sku': 'B0CREEP001', 'title': FIGURE, 'owner_id': 1,
                          'identifiers': [{'kind': 'ASIN', 'value': 'B0CREEP001'}]})['id']
    audit_row(w, 'B0CREEP001', FIGURE)
    read(li, [item(704, EBAY_FIGURE)])
    c = eb.to_match()['listings'][0]['candidates']
    assert [(x['kind'], x['id'], x['why']) for x in c] == [('product', pid, 'On the Stock audit')]
    eb.match(listing_row(w, 'ITEM-704'), 1, product_id=pid)        # now it is that eBay listing's item
    read(li, [item(704, EBAY_FIGURE), item(705, EBAY_FIGURE + ' Sealed')])
    assert eb.to_match()['listings'][0]['candidates'] == []
