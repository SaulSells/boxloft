"""Scan a carrier label, see what is inside (Alex 11-16, labels.py).

The tracking IDs are real ones from live (UPS, USPS, Amazon TBA, a Walmart FedEx number). The scanned text is built
the way each carrier's barcode carries it: USPS IMpb = 420 + ZIP + GS + the ID (camera) or with ]C1 and no GS
(keyboard scanner), FedEx's 34-digit barcode ending in the 12-digit ID, UPS and Amazon = the ID itself.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from labels import Labels, clean, looks_like  # noqa: E402
from test_sorting import UPC, setup, units_in  # noqa: E402
from wms import WmsError  # noqa: E402

UPS, USPS, TBA, FEDEX = '1ZI2QDR00374260031', '9233400472350650946389', 'TBA803460054292', '901323479428'


def order(w, ext, lines, source='amazon', state='delivered'):
    """lines: [(title, asin, qty)] -> po id, [line ids]"""
    with w._tx() as c:
        po = c.execute("""INSERT INTO purchase_order (source, external_id, ordered_at, imported_at, state, delivered_on, last_seen_at)
                          VALUES (?, ?, '2026-11-05', 'x', ?, '2026-11-12', '2026-11-12')""", (source, ext, state)).lastrowid
        ids = [c.execute("INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents) VALUES (?,?,?,?,?,1000)",
                         (po, f'{ext}-{i}', asin, title, qty)).lastrowid for i, (title, asin, qty) in enumerate(lines)]
    return po, ids


def package(w, po, shipment, tracking, carrier, contents):
    """contents: [(line id, qty)] in one Amazon package with this tracking ID."""
    with w._tx() as c:
        for n, (lid, qty) in enumerate(contents):
            c.execute("""INSERT INTO purchase_package (po_id, line_id, pkg, qty, state, seen_at, shipment_id, package_index)
                         VALUES (?,?,?,?, 'delivered', 'x', ?, 0)""", (po, lid, 1, qty, shipment))
        c.execute("""INSERT INTO package_track (po_id, shipment_id, read_at, carrier, tracking_id, state, delivered_on)
                     VALUES (?,?, 'x', ?, ?, 'delivered', '2026-11-12')""", (po, shipment, carrier, tracking))


@pytest.fixture
def wh(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    lb = Labels(w)
    so.labels = lb
    # One Amazon order, 2 packages: the UPS box holds 2 LEGO + 1 Funko, the USPS box the other 2 LEGO.
    po, (lego, funko) = order(w, '112-SPLIT', [('LEGO Bonsai Tree', 'B0LEGO0001', 4), ('Funko Batman', 'B0FUNKO001', 1)])
    package(w, po, 'S1', UPS, 'UPS', [(lego, 2), (funko, 1)])
    package(w, po, 'S2', USPS, 'USPS', [(lego, 2)])
    return {'w': w, 'so': so, 'cat': cat, 'lb': lb, 'boxes': boxes, 'po': po, 'lego': lego, 'funko': funko}


def items(card):
    return sorted((i['title'], i['in_package'], i['left']) for o in card['orders'] for i in o['items'])


def test_each_carrier_barcode_finds_its_package(wh):
    lb = wh['lb']
    assert items(lb.match(UPS)) == [('Funko Batman', 1, 1), ('LEGO Bonsai Tree', 2, 2)]
    # USPS IMpb from a phone camera (GS between ZIP and ID) and from a keyboard scanner (]C1, no GS)
    for scanned in (f'42090210\x1d{USPS}', f']C1420902101234{USPS}', USPS):
        c = lb.match(scanned)
        assert (c['kind'], c['tracking_id'], c['carrier'], items(c)) == ('label', USPS, 'USPS', [('LEGO Bonsai Tree', 2, 2)])
    assert lb.match(UPS.lower())['tracking_id'] == UPS   # case and spaces never matter
    assert clean(' ' + ' '.join((UPS[:5], UPS[5:9], UPS[9:13], UPS[13:])).lower() + ' ') == UPS


def test_one_box_with_two_orders_shows_both(wh):
    w, lb = wh['w'], wh['lb']
    po2, (cable,) = order(w, '112-OTHER', [('USB cable', 'B0CABLE001', 3)])
    po3, (mug,) = order(w, '112-THIRD', [('Blue mug', 'B0MUG00001', 1)])
    package(w, po2, 'S9', TBA, 'Amazon', [(cable, 3)])
    package(w, po3, 'S7', TBA, 'Amazon', [(mug, 1)])   # Amazon put both orders in one box
    c = lb.match(TBA)
    assert [o['order_id'] for o in c['orders']] == ['112-OTHER', '112-THIRD'] and (c['units'], c['left']) == (4, 4)


def test_walmart_tracking_comes_from_its_page(tmp_path):
    from test_sweep_inbound import _walmart_po
    w, so = _walmart_po(tmp_path)
    lb = Labels(w)
    page = {'order_id': '2000145-74623142', 'subtotal_cents': 1495, 'total_cents': 1594, 'cards': ['4242'], 'items': [
        {'name': 'Pretty Concealer 198 Light', 'item_id': '1', 'qty': 1, 'line_cents': 497, 'status': 'Delivered Mon, Apr 27',
         'tracking': FEDEX}]}
    so.walmart_pages([page])
    so.walmart_pages([page])   # a second read replaces, never doubles
    c = lb.match('9622001900000000000000' + FEDEX)   # FedEx's 34-digit barcode ends with the 12-digit ID
    assert (c['kind'], c['carrier'], c['orders'][0]['source']) == ('label', 'FedEx', 'walmart.com')
    assert [(i['in_package'], i['left']) for i in c['orders'][0]['items']] == [(1, 1)]


def test_the_zip_barcode_says_scan_the_long_one(wh):
    from labels import is_routing
    r = wh['so'].put(wh['boxes']['A1'], 1, scan_id='z1', code='42090210')   # 420 + ZIP 90210 (Springfield)
    assert (r['kind'], r.get('routing')) == ('label_unknown', True) and wh['cat'].calls == []
    assert not is_routing('42000006')   # a valid EAN-8 that starts with 420 is an item


def test_two_ids_that_fit_equally_are_held_for_a_person(wh):
    w, lb = wh['w'], wh['lb']
    po2, (cable,) = order(w, '112-AMB', [('USB cable', 'B0CABLE001', 1)])
    package(w, po2, 'S5', '420902109200', 'USPS', [(cable, 1)])   # a short ID inside a long scan, as long as another
    scanned = '420902109200' + '1ZI2QDR00374'
    with w._tx() as c:   # a second 12-character ID the same scan contains
        c.execute("UPDATE package_track SET tracking_id = '1ZI2QDR00374' WHERE tracking_id = ?", (UPS,))
    r = lb.scan(scanned, 1)
    assert r['kind'] == 'label_ambiguous' and sorted(ch['tracking_id'] for ch in r['choices']) == ['1ZI2QDR00374', '420902109200']
    with pytest.raises(WmsError):
        lb.pick(scanned, USPS, 1)   # not one of the two
    assert lb.pick(scanned, '420902109200', 1)['orders'][0]['order_id'] == '112-AMB'
    assert [r['result'] for r in w._q('SELECT result FROM label_scan ORDER BY id')] == ['ambiguous', 'picked']


def test_a_carrier_label_nobody_has_is_said_and_never_looked_up(wh):
    so, cat, w = wh['so'], wh['cat'], wh['w']
    r = so.put(wh['boxes']['A1'], 1, scan_id='u1', code='1ZZZZZZZ0300000000')
    assert (r['kind'], r['carrier']) == ('label_unknown', 'UPS') and cat.calls == []
    assert w._q1('SELECT result FROM label_scan')['result'] == 'unknown' and units_in(w, wh['boxes']['A1']) == 0
    assert looks_like(UPC) == '' and so.put(wh['boxes']['A1'], 1, scan_id='u2', code=UPC)['kind'] == 'added'   # a UPC is an item


def test_box_page_scan_shows_the_package_and_puts_items_on_its_order(wh):
    so, w, box = wh['so'], wh['w'], wh['boxes']['A1']
    r = so.put(box, 2, scan_id='b1', code=UPS)
    assert r['kind'] == 'label' and items(r) == [('Funko Batman', 1, 1), ('LEGO Bonsai Tree', 2, 2)]
    assert w._q1('SELECT user_id, result, tracking_id, po_ids FROM label_scan') == \
        {'user_id': 2, 'result': 'matched', 'tracking_id': UPS, 'po_ids': str(wh['po'])}
    # "Put 1 in A1" on the LEGO row (server: /api/labels/put): a receipt on its order line, a box page line with Undo
    opt = {'kind': 'order', 'line_id': wh['lego'], 'left': 4, 'label': 'order', 'sub': ''}
    a = so.add(box, opt, 1, 2, scan_id='b2')
    so.labels.record_put(a['add_id'], UPS)
    assert units_in(w, box) == 1 and w._q1('SELECT purchase_line_id FROM receipt')['purchase_line_id'] == wh['lego']
    assert items(so.labels.match(UPS)) == [('Funko Batman', 1, 1), ('LEGO Bonsai Tree', 2, 1)]
    q = so.put_qty(a['add_id'], 2, 2)   # tap the count, type 2: the line keeps its package
    after = so.labels.match(UPS)
    assert items(after) == [('Funko Batman', 1, 1), ('LEGO Bonsai Tree', 2, 0)]   # this box's LEGO are in
    assert items(so.labels.match(USPS)) == [('LEGO Bonsai Tree', 2, 2)]           # the other box's are not
    assert after['scanned_before'][0]['user_id'] == 2
    so.undo_add(q['add_id'], 2)
    assert units_in(w, box) == 0 and items(so.labels.match(UPS))[1] == ('LEGO Bonsai Tree', 2, 2) and w.check_integrity()['ok']


def test_items_scanned_in_without_the_label_come_off_the_first_delivered_box(wh):
    w, so, lb = wh['w'], wh['so'], wh['lb']
    with w._tx() as c:   # the USPS box came a day earlier
        c.execute("UPDATE purchase_package SET delivered_on = '2026-11-10' WHERE shipment_id = 'S2'")
        c.execute("UPDATE purchase_package SET delivered_on = '2026-11-12' WHERE shipment_id = 'S1'")
    so.add(wh['boxes']['A1'], {'kind': 'order', 'line_id': wh['lego'], 'left': 4}, 2, 1, scan_id='n1')   # item scans, no label
    assert items(lb.match(USPS)) == [('LEGO Bonsai Tree', 2, 0)]
    assert items(lb.match(UPS)) == [('Funko Batman', 1, 1), ('LEGO Bonsai Tree', 2, 2)]


def test_stock_audit_never_takes_a_label_as_an_extra(wh):
    r = wh['so'].scan(UPS, wh['boxes']['A1'], 1)
    assert r['kind'] == 'wrong' and 'carrier label' in r['message']
    assert not wh['w']._q('SELECT id FROM audit_extra')


def test_arriving_names_who_scanned_the_label(wh):
    wh['lb'].scan(UPS, 2)
    assert wh['lb'].last_scans({wh['po']})[wh['po']]['user_id'] == 2


from test_cover_all import srv  # noqa: E402,F401  (fixture)


def test_end_to_end_on_the_server(srv):  # noqa: F811
    """Sam scans a UPS label on box A1's page, puts the 2 LEGO from the card, the card counts them, Arriving says
    her name, and Overview counts no refused save."""
    server, call = srv
    w = server.wms
    po, (lego, funko) = order(w, '112-E2E', [('LEGO Bonsai Tree', 'B0LEGO0001', 2), ('Funko Batman', 'B0FUNKO001', 1)])
    package(w, po, 'S1', UPS, 'UPS', [(lego, 2), (funko, 1)])
    import inbound as inbound_mod   # delivered today, so Arriving lists it whatever the real date is
    today = inbound_mod.today_et().isoformat()
    with w._tx() as cx:
        cx.execute('UPDATE purchase_order SET delivered_on = ? WHERE id = ?', (today, po))
        cx.execute('UPDATE purchase_package SET delivered_on = ? WHERE po_id = ?', (today, po))
    box = w.create_boxes(1, 1)[0]
    st, r = call('Sam', '/api/put', {'box_id': box['id'], 'scan_id': 'e1', 'code': UPS})
    assert st == 200 and r['kind'] == 'label' and (r['units'], r['left']) == (3, 3), r
    st, a = call('Sam', '/api/labels/put', {'box_id': box['id'], 'scan_id': 'e2', 'line_id': lego, 'qty': 2, 'tracking_id': UPS})
    assert st == 200 and (a['kind'], a['how'], a['qty']) == ('added', 'order', 2), a
    st, c = call('Sam', f'/api/labels/card?tracking_id={UPS}')
    assert (c['left'], c['scanned_before'][0]['by']) == (1, 'Sam'), c
    st, arr = call('Alex', '/api/inbound/arriving')
    o = next(x for x in arr['orders'] if x['id'] == po)
    assert o['label_scanned']['by'] == 'Sam'
    st, s = call('Sam', '/api/scan?code=' + UPS)
    assert s['kind'] == 'label'
    assert call('Alex', '/api/summary')[1]['refused']['count'] == 0
