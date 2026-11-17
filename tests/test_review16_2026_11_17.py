import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))
import json

import pytest

from merge import Merge
from test_sorting import ASIN, setup
from wms import WmsError, now_utc

Merge.enabled = True


def _row(w):
    return w._q1('SELECT id FROM audit_item WHERE asin = ?', (ASIN,))['id']


def _in_box(w, pid, box):
    return w._q1('SELECT COALESCE(SUM(qty), 0) AS n FROM stock_level WHERE product_id = ? AND box_id = ?',
                 (pid, box['box_id']))['n']


def test_merge_saved_before_receipt_void_still_lets_the_old_undo_take_found_units(tmp_path):
    """Live 11-17 (CLAUDE.md): item 620 was merged into Stock audit row 297 by the code before the pause (027d142),
    which re-booked all 10 extras as found and wrote no receipt_void. The fix only guards merges it makes itself:
    _undo's check looks at receipt_void, and the "merged since" check skips a merge into the item's own row. So the box
    page's old Undo of the original extra receive still runs on that live merge and takes the found units out of the
    box while the row keeps them as found (the bug the fix says it closed: "the box page's old Undo can't take a unit
    twice")."""
    w, au, so, cat, inbound, b = setup(tmp_path, expected=3)
    Merge(w, au.commerce, au)                                   # makes product_merge, as on live
    s = w.save_product({'sku': 'S-1', 'title': 'Toploader (scanned)'})['id']
    old = w.receive(1, s, 3, 900, b['A1'], reference='box page')['action_id']
    row = _row(w)
    # What the pre-pause code saved for "Same item as Stock audit row": the row's ASIN on the item, the units out to
    # VENDOR and Found on the row in the same box, one product_merge row. No receipt_void (the table did not exist).
    with w._tx() as c:
        c.execute("INSERT INTO product_identifier (product_id, kind, value) VALUES (?, 'ASIN', ?)", (s, ASIN))
    acts = [w.remove(1, b['A1'], [{'product_id': s, 'qty': 3}], 'VENDOR', note='re-booked')['action_id']]
    before = {x['action_id'] for x in w._q('SELECT action_id FROM audit_count WHERE item_id = ?', (row,))}
    au.found(row, 3, b['A1'], 1)
    acts += [x['action_id'] for x in w._q('SELECT action_id FROM audit_count WHERE item_id = ?', (row,))
             if x['action_id'] not in before]
    with w._tx() as c:
        c.execute("""INSERT INTO product_merge (at, user_id, source_id, target_id, item_id, action_ids, moved, archived)
                     VALUES (?,?,?,?,?,?,?,0)""",
                  (now_utc(), 1, s, s, row, ','.join(map(str, acts)),
                   json.dumps({'idents': [{'id': None, 'kind': 'ASIN', 'value': ASIN, 'added': True}], 'links': {}})))
    assert au.item(row)['accounted'] == 3 and _in_box(w, s, b['A1']) == 3
    # The old extra receive's Undo must be refused, as it is for a merge made by the new code.
    try:
        w.undo(1, old)
    except WmsError:
        return
    pytest.fail(f"old Undo ran: box A1 now holds {_in_box(w, s, b['A1'])} while the row still counts "
                f"{au.item(row)['accounted']} found")
