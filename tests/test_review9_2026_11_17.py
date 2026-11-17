import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

from merge import Merge  # noqa: E402
from test_sorting import setup  # noqa: E402

Merge.enabled = True


def _line(w, pid, qty, cost=700, ext='R1'):
    with w._tx() as c:
        po = c.execute("INSERT INTO purchase_order (source, external_id, ordered_at, imported_at) VALUES ('target.com', ?, '2026-10-01', 'x')",
                       (ext,)).lastrowid
        return c.execute('INSERT INTO purchase_line (po_id, line_key, title, qty, unit_cost_cents, product_id) VALUES (?,?,?,?,?,?)',
                         (po, ext, 'Lantern', qty, cost, pid)).lastrowid


def test_buyer_return_after_merge_counts_the_unit_twice_in_bought_tally(tmp_path):
    """S: 2 bought on a line, both received, 1 shipped on an Amazon order. Merge S into T (T never shipped anything).
    The buyer sends the shipped unit back: take() now books CUSTOMER -> box as T, so T's CUSTOMER balance is -1
    (clipped to 0) while S's CUSTOMER +1 still counts as gone through _merged_into. bought_tally(T) then says
    in = 2 on hand + 1 shipped = 3 of 2 bought: the returned unit counts twice."""
    from buyer_returns import BuyerReturns
    from test_buyer_returns import UPS, parse_amazon, report, row
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    br = BuyerReturns(w)
    m = Merge(w, au.commerce, au)
    s = w.save_product({'sku': 'LANTERN-1', 'title': 'Brass camp lantern',
                        'identifiers': [{'kind': 'ASIN', 'value': 'B0LANTERN1'}]})['id']
    ln = _line(w, s, 2)
    w.receive(1, s, 2, 700, boxes['A1'], purchase_line_id=ln)
    a = w.remove(1, boxes['A1'], [{'product_id': s, 'qty': 1}], 'CUSTOMER')['action_id']
    with w._tx() as c:
        o = c.execute("INSERT INTO sales_order (channel, external_id, ordered_at, status, imported_at) VALUES ('amazon', '113-1', '2026-10-30', 'shipped', 'x')").lastrowid
        c.execute("INSERT INTO sales_line (order_id, line_key, sku, asin, title, qty, product_id, shipped_action_id) VALUES (?, 'k', 'LANTERN-1', 'B0LANTERN1', 'Lantern', 1, ?, ?)",
                  (o, s, a))
    br.save(parse_amazon(report([row('113-1', 'RMA1', 'B0LANTERN1', 'LANTERN-1', 1, 'CR-DAMAGED_BY_CARRIER', UPS)])))
    t = w.save_product({'sku': 'LANTERN-KEEP', 'title': 'Brass camp lantern (kept)'})['id']
    m.run(s, {'product_id': t}, 1, seen={'on_hand': 1})
    before = au.commerce.bought_tally(t, 1)
    assert (before['bought'], before['in']) == (2, 2), before
    rid = w._q1('SELECT id FROM buyer_return')['id']
    br.take(rid, 1, 1, 'good', boxes['A1'])
    assert w.get_product(t)['on_hand'] == 2
    after = au.commerce.bought_tally(t, 1)
    assert after['in'] == 2, f"2 bought, both back on the shelf, nothing out: in should be 2, got {after}"
