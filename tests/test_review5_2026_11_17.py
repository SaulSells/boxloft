import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

from merge import Merge  # noqa: E402
from test_sorting import setup  # noqa: E402

Merge.enabled = True


def _line(w, pid, qty=3, cost=700):
    with w._tx() as c:
        po = c.execute("INSERT INTO purchase_order (source, external_id, ordered_at, imported_at) VALUES ('target.com', 'T9', '2026-10-01', 'x')").lastrowid
        return c.execute('INSERT INTO purchase_line (po_id, line_key, title, qty, unit_cost_cents, product_id) VALUES (?,?,?,?,?,?)',
                         (po, 'k', 'Mug', qty, cost, pid)).lastrowid


def test_extra_on_a_buy_line_vanishes_from_extras_after_merge(tmp_path):
    """A Stock audit extra received against the item's open buy line is still an extra after its item merges into
    another item (no audit row anywhere): the unit just moves over as counted. The extras list drops it."""
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    m = Merge(w, au.commerce, au)
    s = w.save_product({'sku': 'MUG-S', 'title': 'Mug'})['id']
    _line(w, s)
    r = so.extra('MUG-S', boxes['A1'], 1, scan_id='x1')
    assert r['extra']['purchase_line_id']
    before = so.extras()['units']
    assert before == 1
    t = w.save_product({'sku': 'MUG-T', 'title': 'Mug (dup)'})['id']
    m.run(s, {'product_id': t}, 1, seen={'on_hand': 1})
    assert w._q1('SELECT SUM(qty) AS n FROM stock_level WHERE product_id = ? AND box_id = ?', (t, boxes['A1']['box_id']))['n'] == 1
    assert so.extras()['units'] == before, 'the extra unit is still in A1 (now as the target), but the extras list lost it'


def test_merged_source_sales_with_no_buys_inflate_target_bought_tally(tmp_path):
    """bought_tally now adds the CUSTOMER balance of every item merged in. A source with no buys at all (older stock,
    an eBay-made item) that shipped 3 makes the target's 'in' 3 too high: the 'past what was bought' ask fires on the
    target's first real units."""
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    m = Merge(w, au.commerce, au)
    s = w.save_product({'sku': 'MUG-S', 'title': 'Mug'})['id']
    w.receive(1, s, 3, 400, boxes['A1'], reference='old stock')
    w.remove(1, boxes['A1'], [{'product_id': s, 'qty': 3}], 'CUSTOMER')
    t = w.save_product({'sku': 'MUG-T', 'title': 'Mug (dup)'})['id']
    _line(w, t, qty=5)
    assert au.commerce.bought_tally(t, 3)['over'] is False
    m.run(s, {'product_id': t}, 1, seen={'on_hand': 0})
    tally = au.commerce.bought_tally(t, 3)
    assert tally['in'] == 0 and tally['over'] is False, \
        f"none of the 5 bought came in yet, tally says in={tally['in']} over={tally['over']}"
