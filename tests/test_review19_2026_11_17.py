import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

from merge import Merge  # noqa: E402
from test_card_listings import with_listings  # noqa: E402
from wms import WmsError  # noqa: E402

Merge.enabled = True


def _resave(w, pid, cents):
    p = w.get_product(pid)
    w.save_product({'sku': p['sku'], 'title': p['title'], 'default_cost_cents': cents,
                    'identifiers': [{'kind': i['kind'], 'value': i['value']} for i in p['identifiers']]}, pid)


def _rcost(w, rid):
    r = w._q1('SELECT unit_cost_cents, cost_missing FROM receipt WHERE id = ?', (rid,))
    return None if r['cost_missing'] else r['unit_cost_cents']


def test_pile_change_on_one_line_gives_a_cost_to_another_line_a_person_just_blanked(tmp_path):
    """Line 1's cost came from the Usual unit cost. Line 2's cost was typed, then a person blanked it (not entered).
    Changing line 1's pile count must not touch line 2: nobody saved a usual cost, and line 2 is not on line 1."""
    w, so, boxes, lid = with_listings(tmp_path)
    p = w.save_product({'sku': 'MUG-9', 'title': 'Green mug', 'identifiers': [{'kind': 'UPC', 'value': '0712345678935'}]})
    pid = p['id']
    l1 = so.put(boxes['A1'], 1, scan_id='a', code='0712345678935')
    assert l1['how'] == 'new' and so.line_cost(l1['add_id'])['cost_cents'] is None
    _resave(w, pid, 500)                                     # usual cost fills line 1
    assert so.line_cost(l1['add_id'])['cost_cents'] == 500
    l2 = so.add(boxes['A2'], {'kind': 'new', 'product_id': pid}, 1, 1, scan_id='b')
    assert l2['how'] == 'new'
    so.set_cost(l2['add_id'], 900, 1)
    so.set_cost(l2['add_id'], None, 1)                       # a person: "cost not entered" on this line
    assert so.line_cost(l2['add_id'])['cost_cents'] is None
    so.put_qty(l1['add_id'], 2, 1, over_ok=True)             # a pile change on the OTHER line
    assert so.line_cost(l2['add_id'])['cost_cents'] is None, \
        'a pile change on line 1 filled line 2 with the usual cost although nobody saved one'
