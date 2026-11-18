import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))
import pytest
from test_card_listings import with_listings
from wms import WmsError


def _here(w, pid, box):
    return w._q1('SELECT COALESCE(SUM(qty),0) AS n FROM stock_level WHERE product_id = ? AND box_id = ?',
                 (pid, box['box_id']))['n']


def _order_line(w, pid, qty=2, ext='111-0000009-0000009'):
    with w._tx() as c:
        po = c.execute("INSERT INTO purchase_order (source, external_id, ordered_at, imported_at) VALUES ('amazon', ?, '2026-10-01', 'x')",
                       (ext,)).lastrowid
        return c.execute('INSERT INTO purchase_line (po_id, line_key, product_id, title, qty, unit_cost_cents) VALUES (?,?,?,?,?,?)',
                         (po, f'L{pid}', pid, 'Red cup', qty, 100)).lastrowid


def test_change_to_an_order_cancelled_meanwhile_keeps_the_old_unit(tmp_path):
    """_change_fits counts units left on the order but not that the order was cancelled; add() then refuses after
    the old line's units came out."""
    w, so, boxes, lid = with_listings(tmp_path)
    mug = w.save_product({'sku': 'MUG-9', 'title': 'Blue mug', 'default_cost_cents': 300})
    r = so.put(boxes['A1'], 1, scan_id='s1', product_id=mug['id'], over_ok=True)
    assert _here(w, mug['id'], boxes['A1']) == 1
    cup = w.save_product({'sku': 'CUP-1', 'title': 'Red cup', 'default_cost_cents': 100})
    ln = _order_line(w, cup['id'])
    opts = so.add_check(boxes['A1'], product_id=cup['id'])['options']
    od = next(o for o in opts if o['kind'] == 'order' and o.get('line_id') == ln)
    with w._tx() as c:   # the other person cancels the order meanwhile (Gmail cancel / Orders page)
        c.execute('UPDATE purchase_line SET cancelled = 1 WHERE id = ?', (ln,))
    with pytest.raises(WmsError):
        so.put(boxes['A1'], 1, scan_id='s2', product_id=cup['id'], option=od, replaces=r['add_id'])
    assert _here(w, mug['id'], boxes['A1']) == 1, "a refused Change took the old line's unit out"


def test_change_onto_one_of_the_orders_a_pile_spread_over_keeps_the_units(tmp_path):
    """An order line whose pile was spread over 2 orders (put_qty) gives back only 1 unit to each. _change_fits adds
    the whole line's qty to any order it touched, so a Change of 2 onto one of them passes the check, the old units
    come out, and add() refuses: 2 units in the box, 0 in the ledger."""
    w, so, boxes, lid = with_listings(tmp_path)
    cup = w.save_product({'sku': 'CUP-1', 'title': 'Red cup', 'default_cost_cents': 100})
    x = _order_line(w, cup['id'], qty=1, ext='111-0000001-0000001')
    y = _order_line(w, cup['id'], qty=1, ext='111-0000002-0000002')
    r = so.put(boxes['A1'], 1, scan_id='s1', product_id=cup['id'])
    assert r['how'] == 'order'
    q = so.put_qty(r['add_id'], 2, 1)
    assert q['kind'] == 'added' and _here(w, cup['id'], boxes['A1']) == 2
    row = w._q1('SELECT action_ids FROM box_add WHERE id = ?', (q['add_id'],))
    assert len(row['action_ids'].split(',')) == 2   # one receipt on each order
    od = {'kind': 'order', 'line_id': y, 'left': 1, 'label': '', 'sub': ''}
    with pytest.raises(WmsError):
        so.put(boxes['A1'], 1, scan_id='s3', product_id=cup['id'], option=od, qty=2, replaces=q['add_id'])
    assert _here(w, cup['id'], boxes['A1']) == 2, "a refused Change took the old line's units out"
