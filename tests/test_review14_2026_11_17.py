import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

from merge import Merge  # noqa: E402
from test_sorting import setup  # noqa: E402
from wms import WmsError  # noqa: E402

Merge.enabled = True


def _shelf_units(w, pid):
    return w._q1("""SELECT COALESCE(SUM(s.qty), 0) AS n FROM stock_level s LEFT JOIN location l ON l.id = s.location_id
                     WHERE s.product_id = ? AND (s.box_id != 0 OR l.kind != 'virtual')""", (pid,))['n']


def test_history_undo_of_sources_pre_merge_removal_puts_unit_on_archived_source(tmp_path):
    """S had 2 in A1; 1 was taken out as damaged. S is merged into T (1 unit moves over). History's Undo of the old
    'damaged' removal then puts the unit back in A1 as S, an archived item that the merge folded away: the box now holds
    a unit of an item nobody can see, T's page shows 1, and a later merge Undo can't restore S's count (it has 2)."""
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    m = Merge(w, au.commerce, au)
    s = w.save_product({'sku': 'S-1', 'title': 'Mug (scanned)', 'identifiers': [{'kind': 'EAN', 'value': '4006381333931'}]})['id']
    t = w.save_product({'sku': 'T-1', 'title': 'Mug'})['id']
    w.receive(1, s, 2, 900, boxes['A1'], reference='box page')
    gone = w.remove(1, boxes['A1'], [{'product_id': s, 'qty': 1}], 'DAMAGED')['action_id']
    m.run(s, {'product_id': t}, 1, seen={'on_hand': 1})
    assert _shelf_units(w, t) == 1 and _shelf_units(w, s) == 0
    try:
        w.undo(1, gone)          # the History page's Undo on the old removal (server /api/undo calls this)
    except WmsError:
        pass                     # refusing it is a fine fix
    archived = w._q1('SELECT archived FROM product WHERE id = ?', (s,))['archived']
    assert not (archived and _shelf_units(w, s) > 0), \
        f'archived merged-away item S now holds {_shelf_units(w, s)} in a box; the kept item T holds {_shelf_units(w, t)}'
