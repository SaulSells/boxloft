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


def test_count_up_unit_moved_by_merge_hides_a_shipped_bought_unit(tmp_path):
    """S: 3 bought on a line, 2 received on it, 1 shipped, a Count finds 1 more in the box (ADJUST, no receipt): 2 on
    hand. Before the merge bought_tally(S) says in = 2 here + 1 shipped = 3 (a 4th unit asks). Merge S into T: both
    on-hand units move as counted (merged_from=S), so `moved` = 2 includes the Count's unit, which never came in on a
    line. T's tally then subtracts it from S's buy-line units: S's shipped unit drops out (in = 2), and a 4th unit of
    3 bought goes in without the "past what was bought" ask."""
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    m = Merge(w, au.commerce, au)
    s = w.save_product({'sku': 'MUG-S', 'title': 'Mug'})['id']
    ln = _line(w, s, 3)
    w.receive(1, s, 2, 700, boxes['A1'], purchase_line_id=ln)
    w.remove(1, boxes['A1'], [{'product_id': s, 'qty': 1}], 'CUSTOMER')
    w.count(1, boxes['A1'], [{'product_id': s, 'qty': 2}])          # found 1 more on a count
    before = au.commerce.bought_tally(s, 1)
    assert (before['bought'], before['in'], before['over']) == (3, 3, True), before
    t = w.save_product({'sku': 'MUG-T', 'title': 'Mug T'})['id']
    m.run(s, {'product_id': t}, 1, seen={'on_hand': 2})
    after = au.commerce.bought_tally(t, 1)
    assert after['bought'] == 3, after
    assert (after['in'], after['over']) == (3, True), \
        f"same units, same buys: in was 3 before the merge, now {after['in']} (over={after['over']})"


def test_buyer_return_after_merge_lands_on_the_archived_source(tmp_path):
    """Merges leave shipped sales on the source (by design) and archive it. A buyer then sends back a unit shipped
    before the merge: BuyerReturns._product takes the sale line's product, the archived source, so 'Put it back in A1'
    puts the unit on an archived item (hidden from Inventory and search). The kept item, the one every code now opens,
    shows 1 fewer than the box holds."""
    from buyer_returns import BuyerReturns
    from test_buyer_returns import UPS, parse_amazon, report, row
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    br = BuyerReturns(w)
    m = Merge(w, au.commerce, au)
    s = w.save_product({'sku': 'LANTERN-1', 'title': 'Brass camp lantern', 'identifiers': [{'kind': 'ASIN', 'value': 'B0LANTERN1'}]})['id']
    w.receive(2, s, 3, 1500, boxes['A1'])
    a = w.remove(2, boxes['A1'], [{'product_id': s, 'qty': 2}], 'CUSTOMER')['action_id']
    with w._tx() as c:
        o = c.execute("INSERT INTO sales_order (channel, external_id, ordered_at, status, imported_at) VALUES ('amazon', '113-1', '2026-10-30', 'shipped', 'x')").lastrowid
        c.execute("INSERT INTO sales_line (order_id, line_key, sku, asin, title, qty, product_id, shipped_action_id) VALUES (?, 'k', 'LANTERN-1', 'B0LANTERN1', 'Lantern', 2, ?, ?)",
                  (o, s, a))
    br.save(parse_amazon(report([row('113-1', 'RMA1', 'B0LANTERN1', 'LANTERN-1', 2, 'CR-DAMAGED_BY_CARRIER', UPS)])))
    t = w.save_product({'sku': 'LANTERN-KEEP', 'title': 'Brass camp lantern (kept)'})['id']
    m.run(s, {'product_id': t}, 1, seen={'on_hand': 1})
    rid = w._q1('SELECT id FROM buyer_return')['id']
    br.take(rid, 1, 1, 'good', boxes['A1'])
    src = w.get_product(s)
    assert not (src['archived'] and src['on_hand']), \
        f"the returned unit sits on archived item {s} ({src['on_hand']} on hand); kept item {t} shows {w.get_product(t)['on_hand']}"
