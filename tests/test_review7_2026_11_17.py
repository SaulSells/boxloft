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


def test_merge_chain_loses_a_shipped_counted_unit_in_bought_tally(tmp_path):
    """A (2 bought on a line, both in A1) merges into B: they move as counted units (merged_from=A). B ships 1 of them.
    B then merges into C. C's bought_tally: bought 2, 1 here, 1 shipped -> in must be 2. The shipped unit was B's
    gone, but B's term only counts gone up to B's OWN buy-line receipts (0), and A's term subtracts all of A's moved
    units (2), so the shipped unit vanishes: in = 1, and a third unit scanned in does not ask 'over'."""
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    m = Merge(w, au.commerce, au)
    a = w.save_product({'sku': 'MUG-A', 'title': 'Mug'})['id']
    ln = _line(w, a, 2, ext='A1')
    w.receive(1, a, 2, 700, boxes['A1'], purchase_line_id=ln)
    b = w.save_product({'sku': 'MUG-B', 'title': 'Mug B'})['id']
    m.run(a, {'product_id': b}, 1, seen={'on_hand': 2})
    w.remove(1, boxes['A1'], [{'product_id': b, 'qty': 1}], 'CUSTOMER')     # 1 of the 2 bought units ships as B
    tb = au.commerce.bought_tally(b, 1)
    assert tb['in'] == 2, tb          # fine before the second merge
    c = w.save_product({'sku': 'MUG-C', 'title': 'Mug C'})['id']
    m.run(b, {'product_id': c}, 1, seen={'on_hand': 1})
    tc = au.commerce.bought_tally(c, 1)
    assert tc['bought'] == 2, tc
    assert tc['in'] == 2 and tc['over'] is True, \
        f"1 bought unit shipped and 1 is here, but after the chain merge tally says in={tc['in']}, over={tc['over']}"
