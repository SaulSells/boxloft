import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

from merge import Merge  # noqa: E402
from test_sorting import ASIN, setup  # noqa: E402
from test_card_listings import with_listings  # noqa: E402
from test_box_line_matrix import _card  # noqa: E402
from wms import WmsError  # noqa: E402
import pytest  # noqa: E402

Merge.enabled = True


def box_total(w, box):
    return w._q1('SELECT COALESCE(SUM(qty), 0) AS n FROM stock_level WHERE box_id = ?', (box['box_id'],))['n']


def test_history_undo_of_one_merge_action_doubles_units(tmp_path):
    """History's Undo (/api/undo -> wms.undo) of the merge's 're-booked as that item' move brings the source's units
    back while the target keeps its copy: 2 physical units show as 4. Only /api/merges/<id>/undo (all or none) may
    take a merge's actions back."""
    w, au, so, cat, inbound, boxes = setup(tmp_path, expected=3)
    m = Merge(w, au.commerce, au)
    t = w.save_product({'sku': 'T-1', 'title': 'Mug'})['id']
    s = w.save_product({'sku': 'S-1', 'title': 'Mug dup'})['id']
    w.receive(1, s, 2, 600, boxes['A1'])
    m.run(s, {'product_id': t}, 1, seen={'on_hand': 2})
    assert box_total(w, boxes['A1']) == 2
    out = w._q1("""SELECT a.id FROM action a JOIN stock_move sm ON sm.action_id = a.id
                    WHERE sm.product_id = ? AND sm.from_box_id = ? AND a.reversed_by IS NULL""", (s, boxes['A1']['box_id']))['id']
    try:
        w.undo(1, out)
    except WmsError:
        pass   # refusing is the fix
    assert box_total(w, boxes['A1']) == 2, f'2 units in the box, ledger says {box_total(w, boxes["A1"])}'


def _set_default(w, pid, cents):
    p = w.get_product(pid)
    w.save_product({'sku': p['sku'], 'title': p['title'], 'default_cost_cents': cents,
                    'identifiers': [{'kind': i['kind'], 'value': i['value']} for i in p['identifiers']]}, pid)


def test_pile_change_freezes_a_filled_usual_cost(tmp_path):
    """A line with cost not entered is filled by the Usual unit cost (a filled receipt follows later edits). put_qty now
    re-saves the line at line_cost(), i.e. the filled cost as if typed at receiving, so the next Usual unit cost edit no
    longer reaches those units: the line keeps the old cost."""
    w, so, boxes, lid = with_listings(tmp_path)
    r, _ = _card(w, so, so.catalog, boxes, lid)
    pid = r['product_id']
    assert so.line_cost(r['add_id'])['cost_cents'] is None
    _set_default(w, pid, 500)
    assert so.line_cost(r['add_id'])['cost_cents'] == 500        # filled
    q = so.put_qty(r['add_id'], 2, 1, over_ok=True)
    _set_default(w, pid, 700)
    assert so.line_cost(q['add_id'])['cost_cents'] == 700, \
        f"usual cost changed to $7: filled units should follow, line shows {so.line_cost(q['add_id'])['cost_cents']}"


def test_extras_list_still_shows_units_a_merge_found_on_the_row(tmp_path):
    """A box page scan put 1 Red mug in as extra stock (audit_extra). The person then merges it into its Stock audit row:
    the unit is Found on the row and its extra receipt is voided. Stock audit's extras list still lists it as an extra,
    so the same unit shows as found on the row and as extra stock."""
    from test_box_line_matrix import _extra
    w, so, boxes, lid = with_listings(tmp_path)
    r, _ = _extra(w, so, so.catalog, boxes, lid)
    assert r['how'] == 'new'
    pid = r['product_id']
    au = so.audit
    row = w._q1('SELECT id FROM audit_item ORDER BY id LIMIT 1')
    it = au.item(row['id'])
    assert it['expected'] - it['accounted'] >= 1 and not it['product_id'], it
    assert any(x['product_id'] == pid for x in so.extras()['rows'])
    m = Merge(w, au.commerce, au)
    m.run(pid, {'audit_id': row['id']}, 1, seen={'on_hand': 1})
    it2 = au.item(row['id'])
    assert it2['accounted'] == it['accounted'] + 1                       # found on the row
    assert not any(x['product_id'] == pid for x in so.extras()['rows']), 'still listed as extra stock after the merge found it'


def test_history_undo_of_a_merge_action_takes_the_whole_merge_back(tmp_path):
    """The fix's other half: History's Undo (/api/undo) finds the merge an action belongs to (Merge.of_action) and
    undoes all of it, so the box holds the source's 2 again and the target none."""
    w, au, so, cat, inbound, boxes = setup(tmp_path, expected=3)
    m = Merge(w, au.commerce, au)
    t = w.save_product({'sku': 'T-1', 'title': 'Mug'})['id']
    s = w.save_product({'sku': 'S-1', 'title': 'Mug dup'})['id']
    w.receive(1, s, 2, 600, boxes['A1'])
    r = m.run(s, {'product_id': t}, 1, seen={'on_hand': 2})
    act = w._q1("""SELECT a.id FROM action a JOIN stock_move sm ON sm.action_id = a.id
                    WHERE sm.product_id = ? AND sm.from_box_id = ? AND a.reversed_by IS NULL""", (s, boxes['A1']['box_id']))['id']
    mid = m.of_action(act)
    assert mid == r['merge_id']
    m.undo(mid, 1)
    assert m.of_action(act) is None
    per = {x['product_id']: x['qty'] for x in w._q('SELECT product_id, qty FROM stock_level WHERE box_id = ? AND qty != 0',
                                                    (boxes['A1']['box_id'],))}
    assert per == {s: 2}, per
    assert w.check_integrity()['ok']
