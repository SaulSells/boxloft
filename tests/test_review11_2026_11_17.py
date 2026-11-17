import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'tests'))
sys.path.insert(0, os.path.join(REPO, 'backend'))

import sorting  # noqa: E402
import wms as wms_mod  # noqa: E402
from test_box_line_matrix import _card  # noqa: E402
from test_card_listings import with_listings  # noqa: E402


def _usual(w, pid, cents):
    p = w.get_product(pid)
    w.save_product({'sku': p['sku'], 'title': p['title'], 'default_cost_cents': cents,
                    'identifiers': [{'kind': i['kind'], 'value': i['value']} for i in p['identifiers']]}, pid)


def test_typed_line_cost_survives_pile_change_in_same_second(tmp_path, monkeypatch):
    """now_utc has 1-second resolution. A Usual unit cost fill and a cost typed on the line in the same second:
    _fill_costs treats the typed cost as typed (s.at >= f.at), but _cost_filled breaks the tie by comparing ids of two
    different tables (cost_set.id vs cost_fill.id), so the pile change thinks the cost was filled, re-enters the line
    as not entered and fills it with the usual cost: the typed $7 becomes $5."""
    fixed = '2026-11-17T17:00:00Z'
    monkeypatch.setattr(wms_mod, 'now_utc', lambda: fixed)
    monkeypatch.setattr(sorting, 'now_utc', lambda: fixed)
    w, so, boxes, lid = with_listings(tmp_path)
    # Another item's not-entered receipt filled first, so cost_fill ids run ahead of cost_set ids (live: many fills).
    other = w.save_product({'sku': 'OTH-1', 'title': 'Other thing'})['id']
    w.receive(1, other, 1, None, boxes['A2'])
    _usual(w, other, 250)
    r, _ = _card(w, so, so.catalog, boxes, lid)
    assert r['how'] == 'new' and so.line_cost(r['add_id'])['cost_cents'] is None
    _usual(w, r['product_id'], 500)                     # Alex saves the usual cost: the line fills to $5
    assert so.line_cost(r['add_id'])['cost_cents'] == 500
    so.set_cost(r['add_id'], 700, 2)                     # Sam types $7 on the line (same second)
    assert so.line_cost(r['add_id'])['cost_cents'] == 700
    q = so.put_qty(r['add_id'], 2, 2, over_ok=True)     # a pile of 2
    assert so.line_cost(q['add_id'])['cost_cents'] == 700   # bug: 500, the typed cost is gone


def test_merge_into_own_row_voids_the_receipt_of_the_unit_it_found(tmp_path):
    """Merge into a Stock audit row with no item (source = target): extras in A1 ($9) and A2 ($7), the row has 1 left.
    run() finds the A1 unit on the row (places go by box id), but _void voids the newest extra receipt (A2's, $7).
    The A2 unit is still there as extra stock, yet its $7 receipt leaves Avg cost and the A1 receipt ($9) of the unit
    that went to the row stays in it; Stock audit's extras list shows the extra in A1, which now holds the row's unit."""
    from merge import Merge
    from test_sorting import setup
    Merge.enabled = True
    w, au, so, cat, inbound, b = setup(tmp_path, expected=1)
    m = Merge(w, au.commerce, au)
    s = w.save_product({'sku': 'S-1', 'title': 'Toploader (scanned)',
                        'identifiers': [{'kind': 'EAN', 'value': '4006381333931'}]})['id']
    so.add(b['A1'], {'kind': 'new', 'product_id': s}, 1, 1, cost_cents=900)
    so.add(b['A2'], {'kind': 'new', 'product_id': s}, 1, 1, cost_cents=700)
    row = w._q1('SELECT id FROM audit_item')['id']
    pre = m.preview(s, {'audit_id': row})
    assert pre['to_row'] == 1
    m.run(s, {'audit_id': row}, 1, seen={'on_hand': 2})
    assert au.item(row)['accounted'] == 1
    # The A1 unit is the row's now (found at 500); the A2 unit stays extra stock at its own $7.
    assert w.get_product(s)['avg_cost_cents'] == round((500 + 700) / 2)         # bug: (500 + 900) / 2 = 700
    assert [(r['bin'], r['qty']) for r in so.extras()['rows']] == [('A2', 1)]   # bug: [('A1', 1)]
