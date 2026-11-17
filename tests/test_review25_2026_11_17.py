import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

import pytest

from merge import Merge
from test_sorting import ASIN, setup
from wms import WmsError

Merge.enabled = True


def _row(w):
    return w._q1('SELECT id FROM audit_item WHERE asin = ?', (ASIN,))['id']


def _box(w, b, pid):
    return w._q1('SELECT COALESCE(SUM(qty), 0) AS n FROM stock_level WHERE box_id = ? AND product_id = ?',
                 (b['box_id'], pid))['n']


def test_undo_of_a_merge_into_own_row_after_the_item_was_merged_away(tmp_path):
    """S (3 extras in A1) is merged into its own Stock audit row (all 3 found), then S is merged into T. Undoing the
    first merge while the second stands un-finds the 3 units on the row, though they sit in A1 as T (carried over as
    counted units). The row then asks for 3 more units that are already in the box: a scan would book 6."""
    w, au, so, cat, inbound, b = setup(tmp_path, expected=3)
    m = Merge(w, au.commerce, au)
    s = w.save_product({'sku': 'S-1', 'title': 'Toploader (scanned)',
                        'identifiers': [{'kind': 'EAN', 'value': '4006381333931'}]})['id']
    w.receive(1, s, 3, 900, b['A1'], reference='box page')
    row = _row(w)
    first = m.run(s, {'audit_id': row}, 1)
    assert au.item(row)['accounted'] == 3
    t = w.save_product({'sku': 'T-1', 'title': 'Toploader 10ct'})['id']
    m.run(s, {'product_id': t}, 1)
    assert _box(w, b['A1'], t) == 3 and au.item(row)['accounted'] == 3
    # The older merge's Undo must be refused while the later merge stands (as wms._undo refuses any other older
    # action of an item merged away since). Instead it goes through...
    try:
        m.undo(first['merge_id'], 1)
    except WmsError:
        return
    # ...and the row lost its 3 found units while the box still holds them as T.
    assert (au.item(row)['accounted'], _box(w, b['A1'], t)) == (3, 3)
