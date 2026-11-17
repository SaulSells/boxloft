import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

from test_sorting import ASIN, setup  # noqa: E402
from merge import Merge  # noqa: E402

Merge.enabled = True
ASIN2 = 'B0SECOND22'


def _save_cost(w, pid, cents):
    p = w.get_product(pid)
    w.save_product({'sku': p['sku'], 'title': p['title'], 'default_cost_cents': cents,
                    'identifiers': [{'kind': i['kind'], 'value': i['value']} for i in p['identifiers']]}, pid)


def test_typed_cost_equal_to_usual_cost_is_later_overwritten(tmp_path):
    """A person types $5.00 on a line whose cost the Usual unit cost (also $5.00) had filled. set_receipt_cost skips
    the receipt (old == cents) and writes no cost_set row, so the receipt still counts as filled: the next Usual unit
    cost edit overwrites the typed $5.00. CLAUDE.md: a typed cost is never overwritten by a Usual unit cost."""
    w, au, so, cat, inbound, b = setup(tmp_path)
    p = w.save_product({'sku': 'MUG-9', 'title': 'Green mug', 'identifiers': [{'kind': 'UPC', 'value': '0712345678935'}]})
    r = so.put(b['A1'], 1, scan_id='m1', code='0712345678935')
    assert r['how'] == 'new' and so.line_cost(r['add_id'])['cost_cents'] is None
    _save_cost(w, p['id'], 500)                                  # usual cost fills the line: $5.00
    assert so.line_cost(r['add_id'])['cost_cents'] == 500
    so.set_cost(r['add_id'], 500, 1)                             # Alex types $5.00: that is the real buy cost
    _save_cost(w, p['id'], 800)                                  # later the usual cost changes
    assert so.line_cost(r['add_id'])['cost_cents'] == 500        # typed cost must stay


def _two_rows(tmp_path):
    w, au, so, cat, inbound, b = setup(tmp_path)
    with w._tx() as c:
        po = c.execute("INSERT INTO purchase_order (source, external_id, ordered_at, imported_at) VALUES ('amazon', '112-0000002-0000000', '2025-06-01', 'x')").lastrowid
        c.execute('INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents) VALUES (?,?,?,?,?,?)',
                  (po, ASIN2, ASIN2, 'Sleeve', 2, 300))
    au.import_snapshot([{'asin': ASIN2, 'title': 'Sleeve 100ct', 'expected': 2, 'cost_cents': 600, 'avg_cost_cents': 300,
                         'first_buy': '2025-06-01', 'last_buy': '2025-06-01',
                         'orders': [{'id': '112-0000002-0000000', 'date': '2025-06-01', 'units': 2}]}], 'test')
    return w, au, so, cat, inbound, b


def test_merge_into_picked_audit_row_finds_on_another_row_preview_disagrees(tmp_path):
    """S (older, ASIN2, open row ASIN2) has 1 extra in a box. Alex picks "Same item as" Stock audit row ASIN whose item
    is T. Preview says the unit is found on row ASIN; run() looks for the row by identifier id order after the codes
    moved, so S's older ASIN2 comes first and the unit is found on row ASIN2 instead: the save does not do what the
    confirm screen (and the row picked) said."""
    w, au, so, cat, inbound, b = _two_rows(tmp_path)
    s = w.save_product({'sku': 'S-1', 'title': 'Sleeve scanned', 'identifiers': [{'kind': 'ASIN', 'value': ASIN2}]})['id']
    w.receive(1, s, 1, 900, b['A1'], reference='box page')
    row1 = w._q1('SELECT id FROM audit_item WHERE asin = ?', (ASIN,))['id']
    au.found(row1, 1, b['A2'], 1)                                # makes T with ASIN; row ASIN 2 left
    t = au.item(row1)['product_id']
    assert t != s
    pv = Merge(w, so.commerce, au).preview(s, {'audit_id': row1})
    assert pv['row']['id'] == row1 and pv['to_row'] == 1
    res = Merge(w, so.commerce, au).run(s, {'audit_id': row1}, 1, seen={'on_hand': 1})
    assert res['row']['id'] == pv['row']['id']
