import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

from merge import Merge  # noqa: E402
from test_sorting import setup  # noqa: E402

Merge.enabled = True


def _line(w, pid, qty, cost=700, ext='T9'):
    with w._tx() as c:
        po = c.execute("INSERT INTO purchase_order (source, external_id, ordered_at, imported_at) VALUES ('target.com', ?, '2026-10-01', 'x')",
                       (ext,)).lastrowid
        return c.execute('INSERT INTO purchase_line (po_id, line_key, title, qty, unit_cost_cents, product_id) VALUES (?,?,?,?,?,?)',
                         (po, ext, 'Mug', qty, cost, pid)).lastrowid


def test_merged_source_buy_line_units_counted_twice_in_bought_tally(tmp_path):
    """The source's buy-line units still on hand move over as counted units (so they are in the target's 'have'), and
    bought_tally ALSO adds the source's shipped units up to its buy-line receipts. The same 2 units count twice."""
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    m = Merge(w, au.commerce, au)
    s = w.save_product({'sku': 'MUG-S', 'title': 'Mug'})['id']
    ln_s = _line(w, s, 2, ext='S1')
    w.receive(1, s, 2, 700, boxes['A1'], purchase_line_id=ln_s)            # 2 bought, on the shelf
    w.receive(1, s, 3, 400, boxes['A2'], reference='box page')             # 3 extras (no buy) ...
    w.remove(1, boxes['A2'], [{'product_id': s, 'qty': 3}], 'CUSTOMER')     # ... that shipped
    t = w.save_product({'sku': 'MUG-T', 'title': 'Mug (dup)'})['id']
    ln_t = _line(w, t, 3, ext='T1')
    w.receive(1, t, 1, 700, boxes['A3'], purchase_line_id=ln_t)            # 1 of T's 3 in, 2 still coming
    m.run(s, {'product_id': t}, 1, seen={'on_hand': 2})
    tally = au.commerce.bought_tally(t, 1)
    # Bought 5 (3 + 2). Here: 3 (T's 1 + S's 2 buy-line units). The 3 that shipped were the extras (no buy).
    assert tally['in'] == 3 and tally['over'] is False, \
        f"2 of T's order are still coming, but tally says in={tally['in']} of {tally['bought']}, over={tally['over']}"
