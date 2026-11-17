import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from audit import Audit  # noqa: E402
from commerce import Commerce  # noqa: E402
from inbound import Inbound  # noqa: E402
from wms import Wms, WmsError  # noqa: E402

ASIN = 'B0KY03WLY1'


def setup(tmp_path):
    w = Wms(str(tmp_path / 'w.db'))
    com = Commerce(w)
    Inbound(w)
    au = Audit(w, com)
    # Two real order lines for the ASIN: 3 at $5.00 (older) and 2 at $6.00.
    with w._tx() as c:
        for i, (day, qty, cents) in enumerate((('2025-06-01', 3, 500), ('2025-07-01', 2, 600))):
            po = c.execute("INSERT INTO purchase_order (source, external_id, ordered_at, imported_at) VALUES ('amazon', ?, ?, 'x')",
                           (f'112-000000{i}-0000000', day)).lastrowid
            c.execute('INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents) VALUES (?,?,?,?,?,?)',
                      (po, ASIN, ASIN, 'Toploader', qty, cents))
    r = au.import_snapshot([{'asin': ASIN, 'title': 'Toploader 10ct', 'expected': 7, 'exp_unlisted': 7,
                             'cost_cents': 3700, 'avg_cost_cents': 529, 'orders': []}], 'test')
    assert (r['items'], r['units'], r['cost_cents']) == (1, 7, 3700)
    box = w.create_boxes(1, 1)[0]['id']
    return w, au, au.list()['items'][0]['id'], box


def test_found_receives_against_orders_oldest_first(tmp_path):
    w, au, iid, box = setup(tmp_path)
    it = au.found(iid, 6, {'box_id': box}, 1, note='shelf A')
    assert (it['found'], it['status']) == (6, 'partial')
    costs = [r['unit_cost_cents'] for r in w._q('SELECT unit_cost_cents FROM receipt ORDER BY id')]
    assert costs == [500, 600, 529]   # 3 + 2 from the orders, 1 past them at the average
    assert w._q('SELECT SUM(qty) AS q FROM stock_level WHERE box_id = ?', (box,))[0]['q'] == 6
    assert w.check_integrity()['ok']
    assert [h['qty'] for h in it['history'] if h['kind'] == 'found'] == [6]   # one save, one line
    it = au.found(iid, 1, {'box_id': box}, 1)
    assert it['status'] == 'found'
    s = au.list()['summary']
    assert (s['found_units'], s['checked'], s['by_status']['found']) == (7, 1, 1)


def test_undo_stops_counting(tmp_path):
    w, au, iid, box = setup(tmp_path)
    it = au.found(iid, 5, {'box_id': box}, 1)   # split across both orders
    cid = [h for h in it['history'] if h['kind'] == 'found'][0]['id']
    it = au.undo_found(iid, cid, 1)
    assert it['found'] == 0 and it['status'] == 'todo'
    assert w.check_integrity()['ok']


def test_close_and_reopen(tmp_path):
    w, au, iid, box = setup(tmp_path)
    assert au.close(iid, 'not_found', 1, 'checked aisle 3')['status'] == 'missing'
    au.found(iid, 2, {'box_id': box}, 1)
    it = au.item(iid)
    assert (it['status'], it['close_reason']) == ('short', 'Not found')
    assert au.reopen(iid, 1)['status'] == 'partial'
    with pytest.raises(WmsError):
        au.close(iid, 'bogus', 1)


def test_counts_are_append_only(tmp_path):
    w, au, iid, box = setup(tmp_path)
    au.close(iid, 'sold', 1)
    with pytest.raises(sqlite3.DatabaseError):
        with w._tx() as c:
            c.execute('DELETE FROM audit_count')


def test_reimport_keeps_counts(tmp_path):
    w, au, iid, box = setup(tmp_path)
    au.found(iid, 3, {'box_id': box}, 1)
    au.import_snapshot([{'asin': ASIN, 'title': 'Toploader 10ct (new title)', 'expected': 7, 'cost_cents': 3700, 'avg_cost_cents': 529}], 'v2')
    it = au.item(iid)
    assert (it['found'], it['title']) == (3, 'Toploader 10ct (new title)')


def test_found_at_a_place_in_a_site(tmp_path):
    w, au, iid, box = setup(tmp_path)
    house = w.create_place("Alex's house")
    room = w.create_place('Main warehouse', 'Back room')
    assert (house['code'], room['code']) == ('ALEXSHOUSE', 'MW-BACKROOM')
    with pytest.raises(WmsError):
        w.create_place('main warehouse', 'back room')
    au.found(iid, 2, {'location_id': room['id']}, 1)
    it = au.found(iid, 1, {'location_id': house['id']}, 1)
    assert [h['location_name'] for h in it['history'] if h['kind'] == 'found'] == ["Alex's house", 'Main warehouse · Back room']
    assert w._q('SELECT SUM(qty) AS q FROM stock_level WHERE location_id = ?', (room['id'],))[0]['q'] == 2
    assert w.check_integrity()['ok']


def test_store_order_joins_audit_and_nothing_falls_through(tmp_path):
    """Alex 11-10: approved store orders that never get scanned in must land on the audit; nothing may sit on no list."""
    from test_store_mail import FakeJev, TOYS, make
    w, inbound, so = make(tmp_path)
    au = Audit(w, Commerce(w))
    jev = FakeJev('TOY12345', '$236.45', {'Stegosaurus': ('$30.80', '$246.40', '8')})
    po = so.take('<m1@toys>', 'Toys <orders@toys.example.com>', 'Order confirmed', '2026-05-04T01:40:37Z', TOYS, jev, '4242')['po_id']
    assert au.unaccounted(inbound)['lines'] == 0              # waiting in the review queue counts as a place
    assert not [i for i in au.list()['items'] if i['line_id']]
    so.approve(po)
    [it] = [i for i in au.list()['items'] if i['line_id']]    # ordered in May, no delivery news: due on the audit
    line = w._q1('SELECT id, unit_cost_cents FROM purchase_line WHERE po_id = ?', (po,))
    assert (it['asin'], it['store'], it['expected'], it['status']) == (f"LINE-{line['id']}", 'example.com', 8, 'todo')
    with w._tx() as c:   # a price fixed after the row was made shows on the row
        c.execute('UPDATE purchase_line SET unit_cost_cents = ? WHERE id = ?', (line['unit_cost_cents'] + 100, line['id']))
    [it] = [i for i in au.list()['items'] if i['line_id']]
    assert it['avg_cost_cents'] == line['unit_cost_cents'] + 100
    with w._tx() as c:
        c.execute('UPDATE purchase_line SET unit_cost_cents = ? WHERE id = ?', (line['unit_cost_cents'], line['id']))
    box = w.create_boxes(1, 1)[0]['id']
    it = au.found(it['id'], 3, {'box_id': box}, 1)
    assert (it['found'], it['status']) == (3, 'partial')
    pid = w._q1('SELECT product_id FROM purchase_line WHERE id = ?', (line['id'],))['product_id']
    assert pid and w._q1('SELECT sku FROM product WHERE id = ?', (pid,))['sku'] == f"EXAMPLE-L{line['id']}"
    assert [r['unit_cost_cents'] for r in w._q('SELECT unit_cost_cents FROM receipt')] == [line['unit_cost_cents']]
    it = au.sold(it['id'], 2, 'ebay', 1, price_cents=5000)
    it = au.close(it['id'], 'not_found', 1)
    assert (it['accounted'], it['expected'], it['status']) == (5, 8, 'short')
    assert w.check_integrity()['ok']
    assert au.unaccounted(inbound)['lines'] == 0
    # An order marked returned with no return on its line sits on no list: the check finds it.
    with w._tx() as c:
        po2 = c.execute("""INSERT INTO purchase_order (source, external_id, account, ordered_at, imported_at, state, review, last_seen_at)
                           VALUES ('example.com', 'TOY999', 'gmail', '2026-06-01', 'x', 'returned', '', 'x')""").lastrowid
        c.execute("INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents) VALUES (?, 'k', '', 'Raptor', 2, 900)", (po2,))
    u = au.unaccounted(inbound)
    assert (u['lines'], u['units'], u['cost_cents'], u['items'][0]['order_id']) == (1, 2, 1800, 'TOY999')


def test_receive_page_units_count_on_the_snapshot_row(tmp_path):
    """Units checked in on Receive against the row's own order count as found there (no double count)."""
    w = Wms(str(tmp_path / 'w.db'))
    com = Commerce(w)
    Inbound(w)
    au = Audit(w, com)
    with w._tx() as c:
        po = c.execute("INSERT INTO purchase_order (source, external_id, ordered_at, imported_at) VALUES ('amazon', '112-1', '2025-06-01', 'x')").lastrowid
        line = c.execute('INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents) VALUES (?,?,?,?,?,?)',
                         (po, ASIN, ASIN, 'Toploader', 5, 500)).lastrowid
        other = c.execute("INSERT INTO purchase_order (source, external_id, ordered_at, imported_at) VALUES ('amazon', '112-9', '2026-10-01', 'x')").lastrowid
        line9 = c.execute('INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents) VALUES (?,?,?,?,?,?)',
                          (other, ASIN, ASIN, 'Toploader', 2, 500)).lastrowid
    au.import_snapshot([{'asin': ASIN, 'title': 'Toploader', 'expected': 5, 'avg_cost_cents': 500, 'cost_cents': 2500,
                         'orders': [{'id': '112-1', 'date': '2025-06-01', 'units': 5}]}], 'test')
    pid = w.save_product({'sku': ASIN, 'title': 'Toploader', 'identifiers': [{'kind': 'ASIN', 'value': ASIN}], 'owner_id': 1})['id']
    box = w.create_boxes(1, 1)[0]['id']
    r = w.receive(1, pid, 3, 500, {'box_id': box}, purchase_line_id=line)
    w.receive(1, pid, 2, 500, {'box_id': box}, purchase_line_id=line9)    # another order: not this row's units
    it = au.list()['items'][0]
    assert (it['received'], it['found'], it['accounted'], it['status']) == (3, 0, 3, 'partial')
    s = au.list()['summary']
    assert (s['found_units'], s['found_cents'], s['accounted_units']) == (3, 1500, 3)
    shelf = [o for o in au.outcomes()['outcomes'] if o['outcome'] == 'in_stock'][0]
    assert (shelf['units'], shelf['cost_cents']) == (3, 1500)
    it = au.found(it['id'], 2, {'box_id': box}, 1)      # the audit's own receipts are not counted twice
    assert (it['received'], it['found'], it['accounted'], it['status']) == (3, 2, 5, 'found')
    w.undo(1, r['action_id'])                             # undone check-in stops counting
    assert au.list()['items'][0]['received'] == 0
    assert w.check_integrity()['ok']


def test_where_comes_from_amazon_order_status_not_the_sheet(tmp_path):
    """Alex 11-10, Mattel Indominus Rex: the 10-02 sheet said "11 never listed, 3 never marked received"; Amazon
    shows all 7 orders delivered. The card's line comes from Amazon's live order status, with dates."""
    w = Wms(str(tmp_path / 'w.db'))
    com = Commerce(w)
    Inbound(w)
    au = Audit(w, com)
    orders = [('112-D1', '2026-08-20', 'delivered', 'Delivered August 26', '2026-08-26'),
              ('112-D2', '2026-08-20', 'delivered', 'Delivered September 16', '2026-09-16'),
              ('112-S1', '2026-10-01', 'shipped', 'Arriving Monday', None),
              ('112-R1', '2026-08-22', 'returned', 'Return received', None),
              ('112-X1', '2026-08-23', 'cancelled', 'Cancelled', None),
              ('112-OLD', '2025-03-01', 'ordered', '', None)]                # CSV import: no status read
    with w._tx() as c:
        for oid, day, st, text, on in orders:
            po = c.execute("""INSERT INTO purchase_order (source, external_id, ordered_at, imported_at, state, status_text,
                              delivered_on, last_seen_at) VALUES ('amazon', ?, ?, 'x', ?, ?, ?, '2026-11-09T19:59:32Z')""",
                           (oid, day, st, text, on)).lastrowid
            c.execute('INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents) VALUES (?,?,?,?,?,?)',
                      (po, ASIN, ASIN, 'Rex', 2, 5939))
    au.import_snapshot([{'asin': ASIN, 'title': 'Rex', 'expected': 13, 'exp_unlisted': 8, 'exp_transit': 3, 'exp_problem': 2,
                         'avg_cost_cents': 5939, 'cost_cents': 13 * 5939,
                         'orders': [{'id': o[0], 'date': o[1], 'units': 2} for o in orders] +
                                   [{'id': '112-GONE', 'date': '2025-04-02', 'units': 1}]}], 'test')
    it = au.list()['items'][0]
    assert it['where'] == [
        {'kind': 'delivered', 'units': 4, 'from': '2026-08-26', 'to': '2026-09-16'},
        {'kind': 'on_way', 'units': 2, 'from': '2026-11-09', 'to': '2026-11-09'},
        {'kind': 'returned', 'units': 2, 'from': '2026-11-09', 'to': '2026-11-09'},
        {'kind': 'no_record', 'units': 3, 'from': '2025-03-01', 'to': '2025-04-02'}]   # 112-GONE: no order in Boxloft
    assert (it['expected'], it['exp_cancelled']) == (11, 2)                   # the cancelled order is left out
    assert sum(x['units'] for x in it['where']) == it['expected']
    tags = {o['id']: (o.get('amazon'), o.get('amazon_on')) for o in it['orders']}
    assert tags['112-D2'] == ('delivered', '2026-09-16') and tags['112-OLD'] == ('', '')
