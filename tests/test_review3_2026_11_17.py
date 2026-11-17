import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

from merge import Merge  # noqa: E402
from test_sorting import ASIN, setup  # noqa: E402

Merge.enabled = True


def _put_new(so, box, pid, qty=1):
    r = so.put(box, 1, product_id=pid, qty=qty)
    if r.get('kind') == 'over':
        r = so.put(box, 1, product_id=pid, qty=qty, option=r['option'], over_ok=True)
    return r


def _same_merge(tmp_path):
    """Item S (its own ASIN) with 3 units of extra stock; Stock audit row (no item) with 1 left. Merge S into the row:
    S becomes the row's item (product_merge source_id = target_id = S), 1 found, 2 stay extra stock on S."""
    w, au, so, cat, inbound, boxes = setup(tmp_path, expected=1)
    m = Merge(w, au.commerce, au)
    row = w._q1('SELECT id FROM audit_item WHERE asin = ?', (ASIN,))['id']
    s = w.save_product({'sku': 'B0OTHER001', 'title': 'Toploader other',
                        'identifiers': [{'kind': 'ASIN', 'value': 'B0OTHER001'}]})['id']
    r = _put_new(so, boxes['A1'], s, 3)
    assert r['how'] == 'new'
    m.run(s, {'audit_id': row}, 1, seen={'on_hand': 3})
    it = au.item(row)
    assert it['product_id'] == s and it['accounted'] == 1
    return w, au, so, boxes, s, r


def test_same_item_merge_hides_remaining_extras(tmp_path):
    w, au, so, boxes, s, r = _same_merge(tmp_path)
    # 3 extras in, 1 re-booked on the row (voided), 2 still extra stock on S, sitting in A1.
    n = sum(x['qty'] for x in so.extras()['rows'] if x['product_id'] == s)
    assert n == 2, f'Stock audit extras list shows {n} of the 2 extras still in A1'


def test_same_item_merge_locks_every_later_line_of_the_item(tmp_path):
    w, au, so, boxes, s, _ = _same_merge(tmp_path)
    # A new scan of S after the merge: extra stock on a fresh line. Its cost and count must be editable.
    r = _put_new(so, boxes['A1'], s, 1)
    assert r['how'] == 'new'
    out = so.set_cost(r['add_id'], 899, 1)   # raises "This item was merged into <S itself>"
    assert out['cost_cents'] == 899


def test_counted_units_with_no_cost_take_an_unrelated_extras_cost(tmp_path):
    """Counted units move over 'as they are'. Bought on a line with no price (cost not entered), they must stay not
    entered on the target; the merge gives them the cost of an unrelated extra unit instead (all_cost fallback)."""
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    m = Merge(w, au.commerce, au)
    s = w.save_product({'sku': 'S-1', 'title': 'Mug'})['id']
    with w._tx() as c:
        po = c.execute("INSERT INTO purchase_order (source, external_id, ordered_at, imported_at) VALUES ('target.com', 'T9', '2026-10-01', 'x')").lastrowid
        ln = c.execute('INSERT INTO purchase_line (po_id, line_key, title, qty, unit_cost_cents, product_id) VALUES (?,?,?,?,?,?)',
                       (po, 'k', 'Mug', 2, 0, s)).lastrowid
    w.receive(1, s, 2, None, boxes['A1'], purchase_line_id=ln)        # 2 bought, cost not entered
    w.receive(1, s, 1, 900, boxes['A1'], reference='box page')        # 1 extra at $9
    t = w.save_product({'sku': 'T-1', 'title': 'Mug (dup)'})['id']
    m.run(s, {'product_id': t}, 1, seen={'on_hand': 3})
    rows = w._q('SELECT qty, unit_cost_cents, cost_missing FROM receipt WHERE product_id = ? AND merged_from IS NOT NULL', (t,))
    assert rows and rows[0]['qty'] == 2
    assert rows[0]['cost_missing'] == 1, f"2 units with no cost now cost {rows[0]['unit_cost_cents']} cents each as if typed"
