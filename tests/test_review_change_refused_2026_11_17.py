"""Census replay finding c14 (review-gate session 11-17) without photos, for live main (879e454): a Change whose new save is refused must leave the old
line's unit where it was."""
import os
import sys

import pytest

REPO = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, os.path.join(REPO, 'tests'))
sys.path.insert(0, os.path.join(REPO, 'backend'))
from test_card_listings import with_listings  # noqa: E402
from wms import WmsError  # noqa: E402


def _here(w, pid, box):
    return w._q1('SELECT COALESCE(SUM(qty),0) AS n FROM stock_level WHERE product_id = ? AND box_id = ?',
                 (pid, box['box_id']))['n']


def test_refused_change_keeps_the_old_lines_unit(tmp_path):
    w, so, boxes, lid = with_listings(tmp_path)
    mug = w.save_product({'sku': 'MUG-9', 'title': 'Blue mug', 'default_cost_cents': 300})
    r = so.put(boxes['A1'], 1, scan_id='s1', product_id=mug['id'], over_ok=True)
    assert r['kind'] == 'added' and _here(w, mug['id'], boxes['A1']) == 1
    cup = w.save_product({'sku': 'CUP-1', 'title': 'Red cup', 'default_cost_cents': 100})
    a2 = [v for k, v in boxes.items() if k != 'A1'][0]
    w.receive(1, cup['id'], 1, 100, a2)
    mv = next(o for o in so.add_check(boxes['A1'], product_id=cup['id'])['options'] if o['kind'] == 'move')
    w.remove(1, a2, [{'product_id': cup['id'], 'qty': 1}], 'DAMAGED')    # someone empties the other box meanwhile
    with pytest.raises(WmsError):
        so.put(boxes['A1'], 1, scan_id='s2', product_id=cup['id'], option=mv, over_ok=True, replaces=r['add_id'])
    assert _here(w, mug['id'], boxes['A1']) == 1, "a refused Change took the old line's unit out"
    assert w.check_integrity()['ok'] if isinstance(w.check_integrity(), dict) else True
