"""Alex 11-10, the LeapFrog globe: 20 bought at Walmart (store email, no ASIN), listed on Amazon FBM, 2 sold. Confirming
the listing on the audit row must give the row that ASIN: the 2 sales count, the photo and product follow, and
18 are left to find. Bought in several places: each Amazon sale goes to the oldest buy first (11-11 night: a unit
at a time, no sales date, nothing to record by hand)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
sys.path.insert(0, os.path.dirname(__file__))

from listings import Listings  # noqa: E402
from sorting import Sorting  # noqa: E402
from test_usability import ASIN, _listing, sale, setup  # noqa: E402

GLOBE = 'B0FS1PNOX0'


def _store_row(w, au, title, qty, cents, day, source='walmart.com'):
    with w._tx() as c:
        po = c.execute("""INSERT INTO purchase_order (source, external_id, account, ordered_at, imported_at, state, review)
                          VALUES (?, ?, 'gmail', ?, 'x', 'ordered', '')""", (source, f'W-{day}', day)).lastrowid
        line = c.execute("""INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents, image_url)
                            VALUES (?, 'k', '', ?, ?, ?, 'https://i5.walmartimages.com/globe.jpg')""", (po, title, qty, cents)).lastrowid
    [it] = [i for i in au.list()['items'] if i['line_id'] == line]
    return it, line


def test_confirmed_listing_gives_a_store_row_its_asin_sales_and_product(tmp_path):
    w, com, s, au = setup(tmp_path)
    li = Listings(w, com)
    it, line = _store_row(w, au, 'LeapFrog Magic Adventures Globe with Accessories', 20, 4925, '2026-08-13')
    for i, day in enumerate(('2026-02-16', '2026-02-20', '2026-03-02')):           # sold from an earlier buy
        sale(s, f'111-OLD-{i}', day, 1, sku='Target-48.10-11.6.25', asin=GLOBE, price=12999)
    sale(s, '111-NOV-1', '2026-11-02', 1, sku='Target-48.10-11.6.25', asin=GLOBE, price=12999)
    sale(s, '111-NOV-2', '2026-11-09', 1, sku='Target-48.10-11.6.25', asin=GLOBE, price=12999)
    lid = _listing(w, 'Target-48.10-11.6.25', GLOBE)
    with w._tx() as c:   # live row 707: made before the email photo was read, so only the purchase line has it
        c.execute('UPDATE audit_item SET image_url = NULL WHERE id = ?', (it['id'],))
    assert (it['match_asin'], it['amazon_sold'], it['expected']) == ('', 0, 20)   # no match yet: no ASIN, no sales
    assert next(r for r in li.list()['listings'] if r['id'] == lid)['audit_id'] is None

    li.mark(it['id'], {'kind': 'confirm', 'listing_id': lid}, 1)
    it = au.item(it['id'])
    assert it['match_asin'] == GLOBE
    # Only the sales after the Aug 13 buy (the Feb/Mar ones sold an earlier buy): 18 left to find.
    assert (it['bought'], it['amazon_sold'], it['expected'], it['accounted']) == (20, 2, 18, 0)

    row = next(r for r in li.list()['listings'] if r['id'] == lid)
    assert (row['audit_id'], row['image_url']) == (it['id'], 'https://i5.walmartimages.com/globe.jpg')

    box = w.create_boxes(1, 1)[0]['id']
    it = au.found(it['id'], 18, {'box_id': box}, 1)
    assert (it['status'], it['accounted']) == ('found', 18)
    p = w.get_product(it['product_id'])                                               # made with the listing's ASIN
    assert {'kind': 'ASIN', 'value': GLOBE} in [dict(kind=i['kind'], value=i['value']) for i in p['identifiers']]
    assert w._q1('SELECT product_id FROM purchase_line WHERE id = ?', (line,))['product_id'] == p['id']
    assert next(r for r in li.list()['listings'] if r['id'] == lid)['on_hand'] == 18   # Listings: on our shelves
    assert w.check_integrity()['ok']


def test_undo_the_match_takes_the_asin_back(tmp_path):
    w, com, s, au = setup(tmp_path)
    li = Listings(w, com)
    it, _ = _store_row(w, au, 'LeapFrog Magic Adventures Globe', 20, 4925, '2026-08-13')
    sale(s, '111-NOV-1', '2026-11-02', 1, sku='GLOBE-SKU', asin=GLOBE)
    lid = _listing(w, 'GLOBE-SKU', GLOBE)
    m = li.mark(it['id'], {'kind': 'confirm', 'listing_id': lid}, 1)
    assert au.item(it['id'])['match_asin'] == GLOBE
    li.undo_mark(it['id'], m['id'], 1)
    it = au.item(it['id'])
    assert (it['match_asin'], it['amazon_sold'], it['expected']) == ('', 0, 20)
    assert next(r for r in li.list()['listings'] if r['id'] == lid)['audit_id'] is None


def test_a_scan_of_the_listing_asin_finds_the_matched_store_row(tmp_path):
    w, com, s, au = setup(tmp_path)
    li = Listings(w, com)
    it, _ = _store_row(w, au, 'LeapFrog Magic Adventures Globe', 20, 4925, '2026-08-13')
    lid = _listing(w, 'GLOBE-SKU', GLOBE)
    li.mark(it['id'], {'kind': 'confirm', 'listing_id': lid}, 1)
    rows = Sorting(w, com, au, listings=li)._rows([], {GLOBE}, set())
    assert [r['id'] for r in rows] == [it['id']]


def test_sales_of_one_asin_bought_in_two_places_go_to_the_oldest_buy_first(tmp_path):
    """The Filtrete snapshot row (7 bought in June and July, 2 sold 8-01) and a Target buy of 3 on 10-01 matched to the
    same ASIN. 6 more sold: the snapshot row's 5 go first, then 1 of Target's 3."""
    w, com, s, au = setup(tmp_path, expected=5)
    li = Listings(w, com)
    store, _ = _store_row(w, au, 'Filtrete 14x14x1 Air Filter 6 pack', 3, 900, '2026-10-01', source='target.com')
    lid = _listing(w, 'FILTRETE-T', ASIN)
    li.mark(store['id'], {'kind': 'confirm', 'listing_id': lid}, 1)
    sale(s, '111-A', '2026-09-20', 2)          # before the Target buy: June/July units
    sale(s, '111-B', '2026-10-05', 2)
    sale(s, '111-C', '2026-10-20', 2)          # June/July have 1 left: that one, then 1 of Target's
    items = au.list()['items']
    assert ASIN not in [r['asin'] for r in items]                       # all 7 sold, no save: nothing to check
    store = au.item(store['id'])
    assert (store['bought'], store['amazon_sold'], store['expected']) == (3, 1, 2)
    assert w.check_integrity()['ok']


def test_a_sale_the_sheet_already_took_off_is_not_counted_again(tmp_path):
    """Alex 11-10, Pooh mobile B0HJKZWYS2: 3 bought on Amazon + 12 at Walmart on 7-19, 1 sold 7-28, 14 listed.
    Matching the Walmart row to the listing must leave it at 12: the sales take the Amazon buys first."""
    w, com, s, au = setup(tmp_path)              # 7 bought on Amazon, 2 sold 8-01
    li = Listings(w, com)
    snap = next(i for i in au.list()['items'] if i['asin'] == ASIN)
    it, _ = _store_row(w, au, 'Filtrete 14x14x1 Air Filter 6 pack', 12, 853, '2026-07-19')
    sale(s, '111-JUL', '2026-07-28', 1)
    sale(s, '111-OCT', '2026-10-05', 1)
    lid = _listing(w, '6pkFiltrete', ASIN)
    li.mark(it['id'], {'kind': 'confirm', 'listing_id': lid}, 1)
    rows = {r['id']: r for r in au.list()['items']}
    assert (rows[snap['id']]['amazon_sold'], rows[snap['id']]['expected']) == (4, 3)
    assert (rows[it['id']]['amazon_sold'], rows[it['id']]['expected'] - rows[it['id']]['accounted']) == (0, 12)
