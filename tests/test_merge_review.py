"""Review of merge.py (session 33822a4f, 11-17): 8 bugs, one failing test each before the fix."""
import os
import sys

import pytest

REPO = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, os.path.join(REPO, 'tests'))
sys.path.insert(0, os.path.join(REPO, 'backend'))

from merge import Merge  # noqa: E402
from test_sorting import ASIN, setup  # noqa: E402
from wms import LINE_LEFT_SQL, WmsError, now_utc  # noqa: E402

Merge.enabled = True


def left(au, item_id):
    it = au.item(item_id)
    return it['expected'] - it['accounted']


def row_id(w):
    return w._q1('SELECT id FROM audit_item WHERE asin = ?', (ASIN,))['id']


def line_left(w, line_id):
    return w._q1(f'SELECT {LINE_LEFT_SQL} AS n FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id WHERE l.id = ?',
                 (line_id,))['n']


# 1. Units already found on a Stock audit row are found AGAIN when their item merges into another item.
def test_found_units_counted_twice_on_row(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path, expected=3)
    m = Merge(w, au.commerce, au)
    row = row_id(w)
    au.found(row, 2, boxes['A1'], 1)                      # 2 of 3 really found; makes item S with the row's ASIN
    s = au.item(row)['product_id']
    assert left(au, row) == 1
    t = w.save_product({'sku': 'T-1', 'title': 'Toploader (dup)'})['id']
    pre = m.preview(s, {'product_id': t})
    m.run(s, {'product_id': t}, 1, seen={'on_hand': 2})
    assert w._q1('SELECT SUM(qty) AS n FROM stock_level WHERE product_id = ? AND box_id = ?', (t, boxes['A1']['box_id']))['n'] == 2
    assert left(au, row) == 1, f'row should still have 1 to find: only 2 units exist (preview said row={pre["row"]})'


# 2. Units already received on a buy line are received on that line AGAIN (line was moved to T first).
def test_received_units_counted_twice_on_buy_line(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path, expected=3)
    m = Merge(w, au.commerce, au)
    s = w.save_product({'sku': 'S-1', 'title': 'Mug'})['id']
    with w._tx() as c:
        po = c.execute("INSERT INTO purchase_order (source, external_id, ordered_at, imported_at) VALUES ('target.com', 'T9', '2026-10-01', 'x')").lastrowid
        ln = c.execute('INSERT INTO purchase_line (po_id, line_key, title, qty, unit_cost_cents, product_id) VALUES (?,?,?,?,?,?)',
                       (po, 'k', 'Mug', 5, 700, s)).lastrowid
    w.receive(1, s, 2, 700, boxes['A1'], purchase_line_id=ln)
    assert line_left(w, ln) == 3
    t = w.save_product({'sku': 'T-1', 'title': 'Mug (dup)'})['id']
    m.run(s, {'product_id': t}, 1, seen={'on_hand': 2})
    assert line_left(w, ln) == 3, f'3 are still coming; line says {line_left(w, ln)}'


# 3. Same-item audit merge: the source's old extra receipt stays live, so avg cost is wrong right after the merge.
def test_avg_cost_counts_rebooked_extra(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path, expected=3)   # row line at 500
    m = Merge(w, au.commerce, au)
    row = row_id(w)
    s = w.save_product({'sku': 'B0OTHER001', 'title': 'Toploader other', 'identifiers': [{'kind': 'ASIN', 'value': 'B0OTHER001'}]})['id']
    w.receive(1, s, 3, 900, boxes['A1'], reference='box page')
    m.run(s, {'audit_id': row}, 1, seen={'on_hand': 3})
    assert left(au, row) == 0
    assert w.get_product(s)['avg_cost_cents'] == 500, f"avg {w.get_product(s)['avg_cost_cents']}"


# 4. Edit of the target between merge and undo: undo leaves the source's barcodes on the target.
def test_undo_after_target_edit_keeps_codes_on_target(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    m = Merge(w, au.commerce, au)
    t = w.save_product({'sku': 'T-1', 'title': 'Mug'})['id']
    s = w.save_product({'sku': 'S-1', 'title': 'Mug dup', 'identifiers': [{'kind': 'EAN', 'value': '4006381333931'}]})['id']
    w.receive(1, s, 1, 600, boxes['A1'])
    r = m.run(s, {'product_id': t}, 1, seen={'on_hand': 1})
    w.save_product({'sku': 'X-1', 'title': 'Other item', 'identifiers': [{'kind': 'UPC', 'value': '036000291452'}]})
    p = w.get_product(t)
    w.save_product({'sku': p['sku'], 'title': 'Blue mug',
                    'identifiers': [{'kind': i['kind'], 'value': i['value']} for i in p['identifiers']]}, t)
    m.undo(r['merge_id'], 1)
    owner = w._q1("SELECT product_id FROM product_identifier WHERE value = '4006381333931'")['product_id']
    assert owner == s, 'source unarchived with its stock back, but its barcode still opens the target'


# 5. Merge into an audit row already linked to the source: undo deletes the source's own ASIN.
def test_undo_deletes_ident_source_already_had(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path, expected=3)
    m = Merge(w, au.commerce, au)
    row = row_id(w)
    s = w.save_product({'sku': 'S-1', 'title': 'Toploader', 'identifiers': [{'kind': 'ASIN', 'value': ASIN}]})['id']
    assert au.item(row)['product_id'] == s
    w.receive(1, s, 2, 900, boxes['A1'], reference='extra')
    r = m.run(s, {'audit_id': row}, 1, seen={'on_hand': 2})
    m.undo(r['merge_id'], 1)
    assert w._q1("SELECT 1 AS x FROM product_identifier WHERE product_id = ? AND value = ?", (s, ASIN)), 'S lost its ASIN'


# 6. Undo moves a sales line back to the source after the target shipped it.
def test_undo_moves_shipped_sale_back_to_source(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    m = Merge(w, au.commerce, au)
    t = w.save_product({'sku': 'T-1', 'title': 'Mug'})['id']
    w.receive(1, t, 2, 800, boxes['A2'])
    s = w.save_product({'sku': 'S-1', 'title': 'Mug dup'})['id']
    with w._tx() as c:
        o = c.execute("INSERT INTO sales_order (channel, external_id, ordered_at, status, imported_at) VALUES ('ebay', '1-1', '2026-11-17', 'open', ?)",
                      (now_utc(),)).lastrowid
        sl = c.execute("INSERT INTO sales_line (order_id, line_key, title, qty, product_id) VALUES (?, 'L', 'mug', 1, ?)", (o, s)).lastrowid
    r = m.run(s, {'product_id': t}, 1, seen={'on_hand': 0})
    au.commerce.ship_line(1, sl, boxes['A2'])
    m.undo(r['merge_id'], 1)
    row = w._q1('SELECT product_id, shipped_action_id FROM sales_line WHERE id = ?', (sl,))
    moved = w._q1('SELECT product_id FROM stock_move WHERE action_id = ?', (row['shipped_action_id'],))['product_id']
    assert row['product_id'] == moved == t, f"sale points at {row['product_id']}, its stock move is product {moved}"


# 7. A later merge T->U, then undo of S->T: S's barcode is pulled off U.
def test_undo_pulls_code_off_later_owner(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    m = Merge(w, au.commerce, au)
    s = w.save_product({'sku': 'S-1', 'title': 'Mug a', 'identifiers': [{'kind': 'EAN', 'value': '4006381333931'}]})['id']
    t = w.save_product({'sku': 'T-1', 'title': 'Mug b'})['id']
    u = w.save_product({'sku': 'U-1', 'title': 'Mug c'})['id']
    r1 = m.run(s, {'product_id': t}, 1)
    m.run(t, {'product_id': u}, 1)
    with pytest.raises(WmsError, match='later merge'):   # review 11-17 (25th): merges come undone newest first
        m.undo(r1['merge_id'], 1)
    owner = w._q1("SELECT product_id FROM product_identifier WHERE value = '4006381333931'")['product_id']
    assert owner == u, f'barcode moved off live item U onto S ({owner}) while merge T->U stands'


# 8. Audit-row target with a product of another owner: no owner check (product target has one).
def test_audit_target_other_owner(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path, expected=3)
    m = Merge(w, au.commerce, au)
    with w._tx() as c:
        c.execute("INSERT INTO owner (name, created_at) VALUES ('Client Co', ?)", (now_utc(),))
        oid = c.execute("SELECT id FROM owner WHERE name = 'Client Co'").fetchone()[0]
    row = row_id(w)
    w.save_product({'sku': 'CL-1', 'title': 'Client toploader', 'owner_id': oid, 'identifiers': [{'kind': 'ASIN', 'value': ASIN}]})
    s = w.save_product({'sku': 'S-1', 'title': 'ours'})['id']
    w.receive(1, s, 1, 900, boxes['A1'])
    with pytest.raises(WmsError, match='owners'):
        m.run(s, {'audit_id': row}, 1)
