"""Contract / old-data review of 7bced72d..90490a96. This tree branches from 879e454, before the deployed commit
7bced72d, so deploying it rolls back fixes and features that live data (box_add_photo rows) already depends on."""
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
                         (po, f'L{pid}{ext}', pid, 'Red cup', qty, 100)).lastrowid


def test_g2_16_undo_after_deploy_leaves_the_add_card_photo_on_the_item(tmp_path):
    """Live data saved by 7bced72d holds box_add_photo rows: a line saved from the Add card's photo. After this tree is
    deployed, the line's Undo no longer takes that photo off, so the undone save's photo stays on the item."""
    w, so, boxes, lid = with_listings(tmp_path)
    cup = w.save_product({'sku': 'CUP-1', 'title': 'Red cup', 'default_cost_cents': 100})
    r = so.put(boxes['A1'], 1, scan_id='s1', product_id=cup['id'], over_ok=True)
    ph = w.add_photo('photo-id/abc.jpg', 1, cup['id'], source='photo-id')
    with w._tx() as c:   # the deployed schema and the row its attach_photo wrote
        c.execute('CREATE TABLE IF NOT EXISTS box_add_photo (add_id INTEGER PRIMARY KEY, photo_id INTEGER NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS photo_freed (photo_id INTEGER PRIMARY KEY)')
        c.execute('INSERT INTO box_add_photo (add_id, photo_id) VALUES (?, ?)', (r['add_id'], ph['id']))
    so.undo_add(r['add_id'], 1)
    left = w._q1('SELECT removed FROM photo WHERE id = ?', (ph['id'],))
    assert left['removed'] == 1, "the undone save's photo is still on the item"
