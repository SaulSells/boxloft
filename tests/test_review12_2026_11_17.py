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


def test_count_up_units_of_a_source_with_no_buys_count_as_target_bought(tmp_path):
    """S has no buys: 2 units came in by a Count up (ADJUST), 1 shipped, 1 on hand. T has 2 bought on a line, none here.
    Merge S into T. The rule (commerce.bought_tally comment): units that left as a merged source count only as far as
    they came in on buy lines (a source with no buys that shipped must not make the target's real units ask). The adj
    term L + U - M adds count-ups beyond the units moved (U=2 > M=1), so S's shipped count-up unit counts as one of T's
    2 bought: in = 2 right after the merge, and T's first real unit asks "3 of 2 bought"."""
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    m = Merge(w, au.commerce, au)
    s = w.save_product({'sku': 'MUG-S', 'title': 'Mug'})['id']
    w.count(1, boxes['A1'], [{'product_id': s, 'qty': 2}])          # 2 found on a count, no buy
    w.remove(1, boxes['A1'], [{'product_id': s, 'qty': 1}], 'CUSTOMER')
    t = w.save_product({'sku': 'MUG-T', 'title': 'Mug T'})['id']
    _line(w, t, 2)
    assert au.commerce.bought_tally(t, 1)['in'] == 0
    m.run(s, {'product_id': t}, 1, seen={'on_hand': 1})
    after = au.commerce.bought_tally(t, 1)
    assert after['bought'] == 2, after
    assert (after['in'], after['over']) == (1, False), \
        f"S never bought any: its shipped count-up unit counted as T's buy (in={after['in']}, over={after['over']})"
