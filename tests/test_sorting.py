"""Sorting the audit pile into boxes by scanning (backend/sorting.py). Alex 11-11: boxes, not bins."""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from audit import Audit  # noqa: E402
from codes import make_code  # noqa: E402
from commerce import Commerce  # noqa: E402
from inbound import Inbound  # noqa: E402
from sorting import Sorting, expand_ranges  # noqa: E402
from wms import Wms, WmsError  # noqa: E402

ASIN = 'B0KY03WLY1'
UPC = '012345678905'
UNLISTED_UPC = '036000291452'


class FakeCatalog:
    """Keepa stand-in: UPC -> ASIN records. Counts calls so tests see when the network would be used."""
    keepa_key = 'fake'

    def __init__(self):
        self.calls = []
        self.db = {UPC: {'asin': ASIN, 'title': 'Toploader 3x4 10ct', 'brand': 'Ultra Pro', 'image_url': None, 'upcs': [UPC], 'eans': []},
                   UNLISTED_UPC: {'asin': 'B0UNLISTED', 'title': 'Kleenex Tissues 3 Pack', 'brand': 'Kleenex', 'image_url': None,
                                  'upcs': [UNLISTED_UPC], 'eans': []}}

    def lookup(self, code):
        self.calls.append(code)
        code = code.strip().upper()
        if code in self.db:
            return [self.db[code]]
        return [p for p in self.db.values() if p['asin'] == code]

    def save_image(self, *a):
        return None


def setup(tmp_path, expected=3):
    w = Wms(str(tmp_path / 'w.db'))
    com = Commerce(w)
    inbound = Inbound(w)
    cat = FakeCatalog()
    au = Audit(w, com, cat)
    with w._tx() as c:
        po = c.execute("INSERT INTO purchase_order (source, external_id, ordered_at, imported_at) VALUES ('amazon', '112-0000001-0000000', '2025-06-01', 'x')").lastrowid
        c.execute('INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents) VALUES (?,?,?,?,?,?)',
                  (po, ASIN, ASIN, 'Toploader', expected, 500))
    au.import_snapshot([{'asin': ASIN, 'title': 'Toploader 10ct', 'expected': expected, 'cost_cents': 500 * expected,
                         'avg_cost_cents': 500, 'first_buy': '2025-06-01', 'last_buy': '2025-06-01',
                         'orders': [{'id': '112-0000001-0000000', 'date': '2025-06-01', 'units': expected}]}], 'test')
    so = Sorting(w, com, au, cat)
    bins = {b['code']: {'box_id': b['id']} for b in so.boxes('A1-A3', create=True)['boxes']}
    return w, au, so, cat, inbound, bins


def units_in(w, to, pid=None):
    sql = 'SELECT COALESCE(SUM(qty), 0) AS n FROM stock_level WHERE box_id = ? AND location_id = 0'
    return w._q1(sql + (' AND product_id = ?' if pid else ''), (to['box_id'], pid) if pid else (to['box_id'],))['n']


def test_expand_ranges():
    assert expand_ranges('A1-A3, E1 - E2') == ['A1', 'A2', 'A3', 'E1', 'E2']
    assert expand_ranges('b9-7 c4') == ['B7', 'B8', 'B9', 'C4']
    assert len(expand_ranges('A1-A199')) == 199
    with pytest.raises(WmsError):
        expand_ranges('shelf')


def test_boxes_by_name_and_typed_name_sets_the_box(tmp_path):
    w, au, so, cat, inbound, bins = setup(tmp_path)
    r = so.boxes('A1-A5', create=True)
    assert (r['created'], r['existing'], [b['code'] for b in r['boxes']]) == (2, 3, ['A1', 'A2', 'A3', 'A4', 'A5'])
    assert so.scan('a2', {}, 1)['target']['id'] == bins['A2']['box_id']
    t = so.scan(make_code('box', bins['A3']['box_id']), {}, 1)['target']
    assert (t['type'], t['code'], t['where']) == ('box', 'A3', 'Main warehouse')
    # New boxes sit at the place "Main warehouse", made once.
    home = w._q('SELECT id FROM location WHERE site = ? AND name = ?', ('Main warehouse', 'Main warehouse'))
    assert len(home) == 1 and {b['location_id'] for b in w.list_boxes()} == {home[0]['id']}


def test_typed_name_with_no_box_offers_to_make_it_and_names_are_unique(tmp_path):
    w, au, so, cat, inbound, bins = setup(tmp_path)
    r = so.scan('a31', {}, 1)
    assert r == {'kind': 'no_target', 'name': 'A31', 'retired': False}
    b = so.make_box('a-31', 1)
    assert (b['code'], b['new'], b['type']) == ('A31', True, 'box')
    assert so.make_box('A31', 1)['id'] == b['id'] and not so.make_box('A31', 1)['new']   # same box, never a second
    with pytest.raises(WmsError, match='already a box named A31'):
        w.create_boxes(1, 1, name='a31')
    with pytest.raises(WmsError, match='already a box named A1'):
        w.update_box(b['id'], name='A1')
    with pytest.raises(WmsError):
        w.create_boxes(2, 1, name='A40')   # two boxes can't share one name
    # With a box set, a typed box name still switches the box (nothing is recorded).
    assert so.scan('A31', bins['A1'], 1)['target']['code'] == 'A31'
    # A shelf of open shelving: items loose, in retail packaging.
    s = so.make_shelf('s1')
    assert (s['type'], s['code'], s['new']) == ('shelf', 'S1', True)
    assert so.find('S1')['target']['id'] == s['id']
    f = so.scan(UPC, {'location_id': s['id']}, 1, scan_id='sh1')
    assert f['kind'] == 'found' and f['target']['type'] == 'shelf' and f['here'] == 1
    assert w.check_integrity()['ok']


def test_resolve_by_upc_through_the_catalog(tmp_path):
    w, au, so, cat, inbound, bins = setup(tmp_path)
    r = so.resolve(UPC)
    assert [x['asin'] for x in r['rows']] == [ASIN]
    assert r['info']['title'] == 'Toploader 3x4 10ct' and r['info']['from_catalog']
    assert cat.calls == [UPC]
    # Once the product exists (made on the first found), the code is never sent to the catalog again.
    so.scan(UPC, bins['A1'], 1, scan_id='s1')
    cat.calls.clear()
    assert [x['asin'] for x in so.resolve(UPC)['rows']] == [ASIN]
    assert cat.calls == []


def test_found_one_per_scan_and_same_sku_in_two_bins(tmp_path):
    w, au, so, cat, inbound, bins = setup(tmp_path, expected=3)
    a = so.scan(UPC, bins['A1'], 1, scan_id='s1')
    assert a['kind'] == 'found' and a['item']['found'] == 1 and a['target']['units'] == 1 and a['here'] == 1
    b = so.scan(UPC, bins['A1'], 1, scan_id='s2')
    assert (b['item']['found'], b['here']) == (2, 2)
    c = so.scan(ASIN, bins['A2'], 1, scan_id='s3')   # the ASIN works too
    assert (c['item']['found'], c['item']['status'], c['target']['code'], c['here']) == (3, 'found', 'A2', 1)
    pid = c['item']['product_id']
    assert (units_in(w, bins['A1'], pid), units_in(w, bins['A2'], pid)) == (2, 1)
    by_box = w._q("SELECT box_id, SUM(qty) AS n FROM audit_count WHERE kind = 'found' GROUP BY 1 ORDER BY 1")
    assert [(x['box_id'], x['n']) for x in by_box] == [(bins['A1']['box_id'], 2), (bins['A2']['box_id'], 1)]
    # The UPC from the scan is on the product now (Orders and Receive find it by barcode).
    assert any(i['value'] == UPC for i in w.get_product(pid)['identifiers'])
    assert w.check_integrity()['ok']
    assert au.unaccounted(inbound)['lines'] == 0


def test_retried_scan_id_is_not_counted_twice(tmp_path):
    w, au, so, cat, inbound, bins = setup(tmp_path)
    so.scan(UPC, bins['A1'], 1, scan_id='same')
    again = so.scan(UPC, bins['A1'], 1, scan_id='same')
    assert again['repeat'] and again['item']['found'] == 1 and units_in(w, bins['A1']) == 1
    so.extra(UNLISTED_UPC, bins['A1'], 1, scan_id='x1')
    assert so.extra(UNLISTED_UPC, bins['A1'], 1, scan_id='x1')['repeat']
    assert so.extras()['units'] == 1 and units_in(w, bins['A1']) == 2


def test_no_box_asks_for_one(tmp_path):
    w, au, so, cat, inbound, bins = setup(tmp_path)
    r = so.scan(UPC, {}, 1)
    assert r['kind'] == 'need_target' and r['rows'][0]['asin'] == ASIN
    assert not w._q('SELECT 1 FROM receipt')


def test_fully_found_asks_then_takes_the_extra(tmp_path):
    w, au, so, cat, inbound, bins = setup(tmp_path, expected=1)
    so.scan(UPC, bins['A1'], 1, scan_id='s1')
    r = so.scan(UPC, bins['A1'], 1, scan_id='s2')
    assert r['kind'] == 'full' and r['rows'][0]['accounted'] == 1 and r['in_stock'][0]['qty'] == 1
    with pytest.raises(WmsError, match='already accounted'):
        so.found(r['rows'][0]['id'], bins['A1'], 1, scan_id='s3')
    x = so.extra(UPC, bins['A1'], 1, scan_id='s4', item_id=r['rows'][0]['id'])
    assert x['extra']['item_id'] == r['rows'][0]['id'] and x['extra']['unit_cost_cents'] == 500   # the row's average
    assert x['target']['units'] == 2
    it = au.item(r['rows'][0]['id'])
    assert (it['found'], it['expected']) == (1, 1)   # the row is not inflated; the extra is listed on its own
    assert w.check_integrity()['ok']


def test_unlisted_item_made_received_and_listed(tmp_path):
    w, au, so, cat, inbound, bins = setup(tmp_path)
    r = so.scan(UNLISTED_UPC, bins['A3'], 1, scan_id='u1')
    assert r['kind'] == 'not_listed' and r['info']['title'] == 'Kleenex Tissues 3 Pack' and not r['rows']
    assert not w._q('SELECT 1 FROM receipt')   # nothing moves until a person taps Add
    x = so.extra(UNLISTED_UPC, bins['A3'], 1, scan_id='u2', cost_cents=250)
    pid = x['extra']['product_id']
    p = w.get_product(pid)
    assert p['title'] == 'Kleenex Tissues 3 Pack' and {(i['kind'], i['value']) for i in p['identifiers']} >= {('UPC', UNLISTED_UPC), ('ASIN', 'B0UNLISTED')}
    rc = w._q1('SELECT reference, unit_cost_cents FROM receipt WHERE product_id = ?', (pid,))
    assert (rc['reference'], rc['unit_cost_cents']) == ('audit-extra', 250)
    so.extra(UNLISTED_UPC, bins['A3'], 1, scan_id='u3')   # no cost typed: uses the average so far
    ex = so.extras()
    assert (ex['units'], len(ex['rows']), ex['rows'][0]['bin']) == (2, 2, 'A3')
    assert units_in(w, bins['A3'], pid) == 2
    # The next scan knows the product (no catalog call) and says where it already is.
    cat.calls.clear()
    again = so.scan(UNLISTED_UPC, bins['A3'], 1, scan_id='u4')
    assert again['kind'] == 'not_listed' and again['info']['product']['id'] == pid and again['in_stock'][0]['qty'] == 2
    assert cat.calls == []
    # A code nobody knows becomes a product named by its code.
    y = so.extra('ZZTOP-77', bins['A3'], 1, scan_id='u5', title='Blue mug')
    assert w.get_product(y['extra']['product_id'])['sku'] == 'ZZTOP-77'
    assert w.check_integrity()['ok']
    assert au.unaccounted(inbound)['lines'] == 0


def test_extra_matches_an_open_order_line(tmp_path):
    """Bought on an order that never made the list (still on Arriving): the unit is received against that line."""
    w, au, so, cat, inbound, bins = setup(tmp_path)
    with w._tx() as c:
        po = c.execute("INSERT INTO purchase_order (source, external_id, ordered_at, imported_at) VALUES ('amazon', '112-9', '2026-11-01', 'x')").lastrowid
        c.execute("INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents) VALUES (?, 'k', 'B0UNLISTED', 'Kleenex', 2, 333)", (po,))
    x = so.extra(UNLISTED_UPC, bins['A1'], 1, scan_id='m1')
    assert x['extra']['purchase_line_id'] and x['extra']['unit_cost_cents'] == 333
    assert w._q1('SELECT reference FROM receipt')['reference'] == '112-9'


def test_undo_found_and_extra(tmp_path):
    w, au, so, cat, inbound, bins = setup(tmp_path)
    a = so.scan(UPC, bins['A1'], 1, scan_id='s1')
    so.scan(UPC, bins['A1'], 1, scan_id='s2')
    it = au.undo_found(a['item']['id'], a['count_id'], 1)
    assert it['found'] == 1 and units_in(w, bins['A1']) == 1
    x = so.extra(UNLISTED_UPC, bins['A1'], 1, scan_id='x1')
    u = so.undo_extra(x['extra']['id'], 1)
    assert u['extras']['units'] == 0 and u['target']['units'] == 1
    with pytest.raises(WmsError):
        so.undo_extra(x['extra']['id'], 1)
    with pytest.raises(sqlite3.DatabaseError):
        with w._tx() as c:
            c.execute('DELETE FROM audit_extra')
    assert w.check_integrity()['ok']


def test_wrong_scans(tmp_path):
    w, au, so, cat, inbound, bins = setup(tmp_path)
    good = make_code('box', bins['A1']['box_id'])
    bad = good[:-1] + str((int(good[-1]) + 1) % 10)
    assert so.scan(bad, {}, 1)['kind'] == 'wrong'
    assert 'not in Boxloft' in so.scan(make_code('location', 999), {}, 1)['message']
    assert 'not in Boxloft' in so.scan(make_code('box', 999), {}, 1)['message']
    assert so.scan('nothing-like-this', bins['A1'], 1)['kind'] == 'not_listed'
    with pytest.raises(WmsError):
        so.found(1, {'box_id': 999}, 1)   # no such box


def test_store_row_is_offered_as_a_guess(tmp_path):
    w, au, so, cat, inbound, bins = setup(tmp_path)
    cat.db['074299000001'] = {'asin': 'B0STEGO001', 'title': 'Large Stegosaurus Dinosaur Toy Figure for Kids', 'brand': '',
                              'image_url': None, 'upcs': [], 'eans': []}
    Wms._add_column(w._conn(), 'purchase_line', 'store_item_id', 'TEXT')   # made by store_mail on a real server
    with w._tx() as c:
        po = c.execute("INSERT INTO purchase_order (source, external_id, ordered_at, imported_at, account) VALUES ('target.com', 'T1', '2025-01-01', 'x', 'gmail')").lastrowid
        ln = c.execute("INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents) VALUES (?, 'k', '', 'Stegosaurus Dinosaur Toy Figure Large', 2, 3080)", (po,)).lastrowid
        c.execute("""INSERT INTO audit_item (asin, title, expected, avg_cost_cents, orders, snapshot, imported_at, line_id, store)
                     VALUES (?, 'Stegosaurus Dinosaur Toy Figure Large', 2, 3080, '[]', 'test', 'x', ?, 'target.com')""", (f'LINE-{ln}', ln))
    r = so.scan('074299000001', bins['A1'], 1)
    assert r['kind'] == 'not_listed' and r['guesses'][0]['line_id'] == ln and r['guesses'][0]['score'] >= 0.5
    f = so.found(r['guesses'][0]['id'], bins['A1'], 1, scan_id='g1')
    assert f['item']['found'] == 1


def test_listing_hint_warns_on_the_wrong_bin_letter(tmp_path):
    w, au, so, cat, inbound, bins = setup(tmp_path)

    class Stub:
        def annotate(self, items):
            for it in items:
                it['listed'] = [{'platform': 'amazon_fbm', 'label': 'Amazon FBM', 'live': True}]
            return {}
    so.listings = Stub()
    e1 = {'box_id': so.make_box('E1', 1)['id']}
    a = so.scan(UPC, bins['A1'], 1, scan_id='h1')
    assert a['hint'] == {'listed_on': ['Amazon FBM'], 'letter': 'A', 'mismatch': False}
    e = so.scan(UPC, e1, 1, scan_id='h2')
    assert e['kind'] == 'found' and e['hint']['mismatch']   # a soft warning: the unit was still recorded


def test_retire_box_moves_contents_then_comes_back(tmp_path):
    w, au, so, cat, inbound, bins = setup(tmp_path)
    a1 = bins['A1']['box_id']
    so.scan(UPC, bins['A1'], 1, scan_id='r1')
    so.scan(UPC, bins['A1'], 1, scan_id='r2')
    with pytest.raises(WmsError, match='still holds 2 units'):
        so.retire(a1, 1)
    with pytest.raises(WmsError, match='no box'):
        so.retire(a1, 1, move_to='Z99')
    r = so.retire(a1, 1, move_to='a2')
    assert (r['retired'], r['moved_units'], r['moved_to']['code'], r['moved_to']['units']) == ('A1', 2, 'A2', 2)
    assert units_in(w, bins['A2']) == 2
    assert 'retired' in so.scan(make_code('box', a1), {}, 1)['message']
    assert so.scan('A1', {}, 1) == {'kind': 'no_target', 'name': 'A1', 'retired': True}
    with pytest.raises(WmsError, match='retired'):
        so.scan(UPC, bins['A1'], 1, scan_id='r3')
    assert so.retire(bins['A3']['box_id'], 1)['moved_units'] == 0   # empty: retired at once
    back = so.boxes('A1-A3', create=True)
    assert (back['created'], back['existing']) == (2, 1)
    assert [b['id'] for b in back['boxes']] == [a1, bins['A2']['box_id'], bins['A3']['box_id']]   # same boxes, no duplicates
    assert w.check_integrity()['ok']
    assert au.item(so.resolve(UPC)['rows'][0]['id'])['found'] == 2   # moving them never touched the count


def test_bins_to_boxes_moves_stock_and_reruns_add_nothing(tmp_path):
    w, au, so, cat, inbound, bins = setup(tmp_path, expected=40)
    locs = {c['code']: c['id'] for c in w.create_locations(['A24', 'A25', 'E7'], 'shelf')}
    item = so.resolve(ASIN)['rows'][0]['id']
    au.found(item, 30, {'location_id': locs['A24']}, 1)        # the live case: one bin holds 30 units
    au.found(item, 2, {'location_id': locs['E7']}, 1)
    w.create_boxes(1, 1, name='E7')                            # a box with that name exists already: it is used
    dry = so.bins_to_boxes()
    assert (dry['bins'], dry['with_stock'], dry['units'], dry['archived']) == (3, 2, 32, 0)
    assert w._q1('SELECT COUNT(*) n FROM location WHERE archived = 1')['n'] == 0
    r = so.bins_to_boxes(apply=True)
    assert (r['with_stock'], r['units'], r['boxes_made'], r['archived']) == (2, 32, 1, 3)
    a24 = so.find('A24')['target']
    assert (a24['type'], a24['units'], a24['where']) == ('box', 30, 'Main warehouse')
    assert so.find('E7')['target']['units'] == 2
    loose = w._q1("SELECT COALESCE(SUM(s.qty), 0) n FROM stock_level s JOIN location l ON l.id = s.location_id WHERE l.code IN ('A24', 'E7')")
    assert loose['n'] == 0
    again = so.bins_to_boxes(apply=True)
    assert (again['bins'], again['archived'], again['units']) == (0, 0, 0)
    assert w.check_integrity()['ok']
    assert au.item(item)['found'] == 32                        # the audit count is unchanged
    assert {a['user_id'] for a in w._q("SELECT user_id FROM action WHERE device = 'migrate'")} == {0}


def test_count_several_at_once_capped_and_undone_as_one(tmp_path):
    w, au, so, cat, inbound, bins = setup(tmp_path, expected=5)
    first = so.scan(UPC, bins['A1'], 1, scan_id='s1')
    more = so.found(first['item']['id'], bins['A1'], 1, scan_id='s2', qty=3)
    assert (more['qty'], more['item']['found'], more['here']) == (3, 4, 4)
    with pytest.raises(WmsError, match='Only 1 left'):
        so.found(first['item']['id'], bins['A1'], 1, scan_id='s3', qty=2)
    again = so.found(first['item']['id'], bins['A1'], 1, scan_id='s2', qty=3)   # a retried save is not doubled
    assert again['repeat'] and again['item']['found'] == 4
    au.undo_found(more['item']['id'], more['count_id'], 1)
    assert au.item(more['item']['id'])['found'] == 1 and units_in(w, bins['A1']) == 1
    assert w.check_integrity()['ok']


def test_found_says_where_the_same_item_sits_and_joins_it(tmp_path):
    w, au, so, cat, inbound, bins = setup(tmp_path, expected=3)
    so.scan(UPC, bins['A1'], 1, scan_id='s1')
    so.scan(UPC, bins['A1'], 1, scan_id='s2')
    r = so.scan(UPC, bins['A2'], 1, scan_id='s3')
    assert [(e['code'], e['qty'], e['join']) for e in r['elsewhere']] == [('A1', 2, True)]
    pid = r['item']['product_id']
    j = so.join(pid, bins['A2'], bins['A1'], 1, 1)
    assert (j['to'], j['target']['units'], units_in(w, bins['A1'], pid), units_in(w, bins['A2'], pid)) == ('A1', 0, 3, 0)
    with pytest.raises(WmsError):   # nothing left in A2 to move
        so.join(pid, bins['A2'], bins['A1'], 1, 1)
    w.undo(1, j['action_id'])
    assert units_in(w, bins['A2'], pid) == 1 and w.check_integrity()['ok']


def test_search_the_list_by_name_and_guess_amazon_rows(tmp_path):
    w, au, so, cat, inbound, bins = setup(tmp_path)
    assert [r['asin'] for r in so.search('toploader')] == [ASIN]
    assert so.search('kleenex') == []
    # A barcode whose catalog answer is another variant's ASIN: the bought row is offered as a guess by name.
    with w._tx() as c:
        c.execute("UPDATE audit_item SET title = 'Ultra Pro Toploader 3x4 Regular 10ct' WHERE asin = ?", (ASIN,))
    cat.db['099999999999'] = {'asin': 'B0VARIANT1', 'title': 'Ultra Pro Toploader 3x4 Thick 10ct', 'brand': '', 'image_url': None,
                              'upcs': ['099999999999'], 'eans': []}
    r = so.scan('099999999999', bins['A1'], 1, scan_id='g1')
    assert r['kind'] == 'not_listed' and [g['asin'] for g in r['guesses']] == [ASIN]
    assert not w._q('SELECT 1 FROM receipt')


def test_prewarm_caches_barcodes_by_asin(tmp_path):
    from commerce import Catalog
    w = Wms(str(tmp_path / 'w.db'))
    Commerce(w)
    cat = Catalog(w, str(tmp_path))
    calls = []

    def fake_keepa(path, **params):
        calls.append(params['asin'])
        return {'products': [{'asin': a, 'title': f'Item {a}', 'upcList': ['012345678905'] if a == 'B000000001' else [],
                              'eanList': ['0036000291452'] if a == 'B000000002' else []} for a in params['asin'].split(',')]}
    cat._keepa = fake_keepa
    cat.keepa_key = 'fake'
    with w._tx() as c:   # an empty cached answer is replaced; a real one is kept
        c.execute("INSERT INTO catalog_cache (code, fetched_at, data) VALUES ('012345678905', 'x', '[]')")
    r = cat.prewarm(['B000000001', 'b000000002', 'B000000003', 'B000000001'], batch=2)
    assert calls == ['B000000001,B000000002', 'B000000003']
    assert (r['asins'], r['found'], r['with_barcode']) == (3, 3, 2)
    assert [p['asin'] for p in cat.lookup('012345678905')] == ['B000000001']
    assert [p['asin'] for p in cat.lookup('0012345678905')] == ['B000000001']
    assert [p['asin'] for p in cat.lookup('036000291452')] == ['B000000002']
    calls.clear()
    assert cat.prewarm(['B000000001'])['asins'] == 0 and calls == []   # already cached: no call


def test_scan_of_a_bought_item_finds_its_open_order_line(tmp_path):
    w, au, so, cat, inbound, bins = setup(tmp_path, expected=2)
    com = so.commerce
    assert [ln['left_to_receive'] for ln in com.open_lines_for_code(ASIN.lower())] == [2]
    assert com.open_lines_for_code(UPC) == []   # no line UPC and nothing cached: no network call either
    with w._tx() as c:
        c.execute("INSERT INTO catalog_cache (code, fetched_at, data) VALUES (?, 'x', ?)", (UPC, f'[{{"asin": "{ASIN}"}}]'))
    assert [ln['order_id'] for ln in com.open_lines_for_code(UPC)] == ['112-0000001-0000000']
    so.found(so.resolve(ASIN)['rows'][0]['id'], bins['A1'], 1, scan_id='a', qty=2)
    assert com.open_lines_for_code(ASIN) == []   # all received: nothing left to receive against
