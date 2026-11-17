"""Add items on a box's page (Alex 11-11 1:10 PM): scan or search, pick what it is, how many, Add."""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from codes import make_code  # noqa: E402
from test_sorting import ASIN, UNLISTED_UPC, UPC, setup, units_in  # noqa: E402
from wms import WmsError  # noqa: E402


def kinds(r):
    return [o['kind'] for o in r['options']]


def test_audit_item_by_upc_goes_in_as_found_and_undo_takes_it_back(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path, expected=3)
    r = so.add_check(boxes['A1'], code=UPC)
    assert r['kind'] == 'options' and kinds(r) == ['audit'] and r['options'][0]['left'] == 3
    assert not w._q('SELECT 1 FROM receipt')   # a check writes nothing
    a = so.add(boxes['A1'], r['options'][0], 2, 1, scan_id='b1', code=UPC)
    assert (a['how'], a['qty'], a['here']) == ('audit', 2, 2)
    it = au.item(r['options'][0]['item_id'])
    assert it['found'] == 2
    rc = w._q1('SELECT reference, unit_cost_cents, purchase_line_id FROM receipt')
    assert rc['reference'] == '112-0000001-0000000' and rc['purchase_line_id']   # against the order, at its cost
    # The product exists now: the same check offers the row's last unit, the units in A1 to move, or more stock.
    r2 = so.add_check(boxes['A2'], code=UPC)
    assert kinds(r2) == ['audit', 'move', 'new'] and r2['options'][0]['left'] == 1
    with pytest.raises(WmsError):   # more than the row has left is refused
        so.add(boxes['A2'], r2['options'][0], 2, 1, scan_id='b2')
    # A resent tap (same scan_id) adds nothing.
    again = so.add(boxes['A1'], r['options'][0], 2, 1, scan_id='b1', code=UPC)
    assert again['repeat'] and au.item(it['id'])['found'] == 2
    u = so.undo_add(a['add_id'], 1)
    assert u['here'] == 0 and au.item(it['id'])['found'] == 0
    assert w.check_integrity()['ok'] and au.unaccounted(inbound)['lines'] == 0


def test_open_order_line_is_received_against_and_the_product_is_made(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    with w._tx() as c:
        po = c.execute("INSERT INTO purchase_order (source, external_id, ordered_at, imported_at) VALUES ('amazon', '112-9', '2026-11-01', 'x')").lastrowid
        c.execute("INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents) VALUES (?, 'k', 'B0UNLISTED', 'Kleenex', 4, 333)", (po,))
        c.execute("INSERT INTO catalog_cache (code, fetched_at, data) VALUES (?, 'x', '[{\"asin\": \"B0UNLISTED\"}]')", (UNLISTED_UPC,))
    before = au.unaccounted(inbound)['lines']   # the fake order has no Arriving record: 1 before any add
    r = so.add_check(boxes['A1'], code=UNLISTED_UPC)
    assert kinds(r) == ['order'] and r['product'] is None and r['options'][0]['left'] == 4
    a = so.add(boxes['A1'], r['options'][0], 3, 1, scan_id='o1', code=UNLISTED_UPC)
    assert a['how'] == 'order' and a['here'] == 3
    rc = w._q1('SELECT reference, unit_cost_cents FROM receipt WHERE product_id = ?', (a['product_id'],))
    assert (rc['reference'], rc['unit_cost_cents']) == ('112-9', 333)
    # The scanned UPC is on the product now, so the next scan finds it by that barcode.
    assert [p['id'] for p in w.lookup(UNLISTED_UPC)] == [a['product_id']]
    with pytest.raises(WmsError):   # 1 left on the line: 2 is refused
        so.add(boxes['A1'], so.add_check(boxes['A1'], code=UNLISTED_UPC)['options'][0], 2, 1, scan_id='o2')
    so.undo_add(a['add_id'], 1)
    assert units_in(w, boxes['A1']) == 0
    assert w.lookup(UNLISTED_UPC)   # the barcode came from the catalog with the product, not from this add: it stays
    assert w.check_integrity()['ok'] and au.unaccounted(inbound)['lines'] == before


def test_a_barcode_an_add_put_on_comes_off_with_its_undo(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    p = w.save_product({'sku': 'MUG-1', 'title': 'Blue mug', 'default_cost_cents': 450, 'identifiers': []})
    opt = so.add_check(boxes['A1'], product_id=p['id'])['options'][-1]
    a = so.add(boxes['A1'], opt, 1, 1, scan_id='c1', code='0712345678911')
    assert [x['id'] for x in w.lookup('0712345678911')] == [p['id']]
    so.undo_add(a['add_id'], 1)
    assert not w.lookup('0712345678911') and w.check_integrity()['ok']


def test_unknown_code_asks_for_a_new_product_then_adds_it_as_an_extra(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    r = so.add_check(boxes['A2'], code='NOPE-123')
    assert r['kind'] == 'new_product' and r['code'] == 'NOPE-123'
    # The create product flow makes it (here: save_product), then the page checks by product id.
    p = w.save_product({'sku': 'MUG-1', 'title': 'Blue mug', 'default_cost_cents': 450,
                        'identifiers': [{'kind': 'UPC', 'value': 'NOPE-123'}]})
    r2 = so.add_check(boxes['A2'], product_id=p['id'])
    assert kinds(r2) == ['new'] and r2['options'][0]['cost_cents'] == 450
    a = so.add(boxes['A2'], r2['options'][0], 5, 1, scan_id='n1')
    assert (a['how'], a['here']) == ('new', 5)
    ex = so.extras()
    assert (ex['units'], ex['rows'][0]['unit_cost_cents'], ex['rows'][0]['bin']) == (5, 450, 'A2')
    so.undo_add(a['add_id'], 1)
    assert so.extras()['units'] == 0 and units_in(w, boxes['A2']) == 0
    assert w.check_integrity()['ok']


def test_move_from_another_box_and_labels_and_search(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path, expected=3)
    item = so.resolve(ASIN)['rows'][0]['id']
    so.found(item, boxes['A1'], 1, scan_id='f', qty=3)
    pid = au.item(item)['product_id']
    r = so.add_check(boxes['A3'], product_id=pid)
    assert kinds(r) == ['move', 'new'] and r['options'][0]['from'] == boxes['A1'] and r['options'][0]['left'] == 3
    a = so.add(boxes['A3'], r['options'][0], 2, 1, scan_id='m1')
    assert units_in(w, boxes['A1']) == 1 and units_in(w, boxes['A3']) == 2
    with pytest.raises(WmsError):   # only 1 left in A1
        so.add(boxes['A3'], r['options'][0], 2, 1, scan_id='m2')
    so.undo_add(a['add_id'], 1)
    assert units_in(w, boxes['A1']) == 3
    # A box label scanned on a box page opens that box; a misread is refused.
    lbl = so.add_check(boxes['A3'], code=make_code('box', boxes['A1']['box_id']))
    assert lbl == {'kind': 'target', 'type': 'box', 'id': boxes['A1']['box_id']}
    assert so.add_check(boxes['A3'], code='SB00000019')['kind'] == 'wrong'
    # Search: products by name, plus open audit rows that have no product yet.
    s = so.add_search('toploader')
    assert [p['id'] for p in s['products']] == [pid] and s['rows'] == []
    with pytest.raises(sqlite3.DatabaseError):
        with w._tx() as c:
            c.execute('DELETE FROM box_add')
    assert w.check_integrity()['ok'] and au.unaccounted(inbound)['lines'] == 0


def test_search_finds_audit_rows_with_no_product_and_checks_by_row(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    s = so.add_search('toploader')
    assert s['products'] == [] and len(s['rows']) == 1
    r = so.add_check(boxes['A1'], item_id=s['rows'][0]['id'])
    assert kinds(r) == ['audit'] and r['title'] == 'Toploader 10ct'


def test_receive_with_no_cost_is_saved_and_kept_out_of_the_average(tmp_path):
    """Alex 11-11: "Sam won't always know the price"."""
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    pid = w.save_product({'sku': 'MUG-1', 'title': 'Blue mug', 'identifiers': []})['id']   # no usual cost either
    w.receive(1, pid, 2, 400, boxes['A1'])
    w.receive(1, pid, 3, None, boxes['A1'])
    assert units_in(w, boxes['A1'], pid) == 5
    p = w.get_product(pid)
    assert w._avg_costs(pid)[pid] == 400 and [r['unit_cost_cents'] for r in p['receipts']] == [None, 400]
    opt = so.add_check(boxes['A2'], product_id=pid)['options'][-1]
    so.add(boxes['A2'], opt, 1, 1, scan_id='nc', cost_cents=None)   # box page "more stock": uses the 400 average
    assert w._avg_costs(pid)[pid] == 400 and w.check_integrity()['ok']


def _two_orders(w):
    """Alex 11-11 photo: one item on 2 Amazon orders, 2 left at $27.56 and 1 left at $27.58."""
    with w._tx() as c:
        for ext, day, n, cost in (('112-4497951', '2026-10-01', 2, 2756), ('112-8889741', '2026-10-03', 1, 2758)):
            po = c.execute("INSERT INTO purchase_order (source, external_id, ordered_at, imported_at) VALUES ('amazon', ?, ?, 'x')",
                           (ext, day)).lastrowid
            c.execute("INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents) VALUES (?, 'k', 'B0PREDATOR', 'Predator', ?, ?)",
                      (po, n, cost))
    return [r['id'] for r in w._q("SELECT id FROM purchase_line WHERE asin = 'B0PREDATOR' ORDER BY id")]


def test_receive_one_pile_against_two_orders_and_undo(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    a, b = _two_orders(w)
    pid = w.save_product({'sku': 'B0PREDATOR', 'title': 'Predator', 'identifiers': [{'kind': 'ASIN', 'value': 'B0PREDATOR'}]})['id']
    com = so.commerce
    with pytest.raises(WmsError):   # 2 from the second order: it has 1 left, and nothing is saved
        com.receive_lines(1, pid, boxes['A1'], [{'purchase_line_id': a, 'qty': 1}, {'purchase_line_id': b, 'qty': 2}])
    assert not w._q('SELECT 1 FROM receipt')
    r = com.receive_lines(1, pid, boxes['A1'], [{'purchase_line_id': b, 'qty': 1}, {'purchase_line_id': a, 'qty': 2}])
    assert len(r['action_ids']) == 2 and units_in(w, boxes['A1'], pid) == 3
    got = w._q('SELECT reference, qty, unit_cost_cents FROM receipt ORDER BY id')   # oldest order first, each at its cost
    assert [(g['reference'], g['qty'], g['unit_cost_cents']) for g in got] == [('112-4497951', 2, 2756), ('112-8889741', 1, 2758)]
    assert com.open_lines_for(pid) == []
    for x in reversed(r['action_ids']):
        w.undo(1, x)
    assert [ln['left_to_receive'] for ln in com.open_lines_for(pid)] == [2, 1]
    assert w.check_integrity()['ok']


def test_box_page_offers_all_the_orders_at_once(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    _two_orders(w)
    r = so.add_check(boxes['A2'], code='B0PREDATOR')
    assert kinds(r) == ['order', 'order', 'order'] and r['options'][0]['left'] == 3 and len(r['options'][0]['line_ids']) == 2
    with pytest.raises(WmsError):   # 4: the two orders have 3 left
        so.add(boxes['A2'], r['options'][0], 4, 1, scan_id='p0')
    x = so.add(boxes['A2'], r['options'][0], 3, 1, scan_id='p1', code='B0PREDATOR')
    assert x['here'] == 3
    assert [(g['qty'], g['unit_cost_cents']) for g in w._q('SELECT qty, unit_cost_cents FROM receipt ORDER BY id')] == [(2, 2756), (1, 2758)]
    so.undo_add(x['add_id'], 1)
    assert units_in(w, boxes['A2']) == 0 and w.check_integrity()['ok']


def test_a_rename_is_kept_and_old_lines_say_the_name_now(tmp_path, monkeypatch):
    """Alex 11-12: Sam renamed A55 to A8 right after scanning into it; history said "into A55" with no reason."""
    import wms as wms_module
    w, au, so, cat, inbound, boxes = setup(tmp_path, expected=3)
    r = so.add_check(boxes['A1'], code=UPC)
    so.add(boxes['A1'], r['options'][0], 1, 1, scan_id='r1', code=UPC)
    monkeypatch.setattr(wms_module, 'now_utc', lambda: '2099-01-01T00:00:00Z')   # the rename comes after the scan
    w.update_box(boxes['A1']['box_id'], name='A8', user_id=2)
    b = w.get_box(boxes['A1']['box_id'])
    assert [(x['old_name'], x['new_name'], x['user_id']) for x in b['renames']] == [('A1', 'A8', 2)]
    assert any('A1 (now A8)' in (h['summary'] or '') for h in b['history'])
    w.update_box(boxes['A1']['box_id'], name='A8', user_id=2)   # saving the same name again is not a rename
    assert len(w.get_box(boxes['A1']['box_id'])['renames']) == 1


def test_changes_says_which_web_build_is_served(tmp_path, monkeypatch):
    """A phone on an older app reloads (LiveSync) when the server serves a newer index-<hash>.js."""
    import server
    d = tmp_path / 'dist'
    d.mkdir()
    (d / 'index.html').write_text('<script type="module" crossorigin src="/assets/index-AbC123_x.js"></script>')
    monkeypatch.setattr(server, 'STATIC', str(d))
    server._build.update(mtime=None, name='')
    assert server.web_build() == 'index-AbC123_x.js'


def test_receive_with_no_order_asks_before_going_past_what_was_bought(tmp_path):
    """Sam 11-12: one Batmobile scanned and typed in until on hand said 6, of 3 bought; nothing asked."""
    from commerce import Commerce
    w, au, so, cat, inbound, boxes = setup(tmp_path, expected=3)
    com = Commerce(w)
    r = so.add_check(boxes['A1'], code=UPC)
    so.add(boxes['A1'], r['options'][0], 3, 1, scan_id='o1', code=UPC)    # all 3 bought are in
    pid = au.item(r['options'][0]['item_id'])['product_id']
    assert w.get_product(pid)['on_hand'] == 3
    ask = com.over_receive(pid, 3)
    assert 'bought 3' in ask and 'makes 6' in ask
    assert com.over_receive(pid, 0) == ''
