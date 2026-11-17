import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

from merge import Merge  # noqa: E402
from test_sorting import ASIN, setup  # noqa: E402
from test_merge_grid import row_of  # noqa: E402

Merge.enabled = True
S_EAN = '4006381333931'


def test_usual_cost_saved_while_merged_into_own_row_never_fills_after_undo(tmp_path):
    """S has 2 units scanned in as extra stock with no cost. Same item as its own Stock audit row: both are found there
    and their extra receipt is voided. Alex then saves S's Usual unit cost $7: _fill_costs skips the voided receipt
    (qty - voided = 0). He undoes the merge: the receipt is live again, still "cost not entered", although S's Usual
    unit cost is $7 and every other not-entered receipt of S took it. Without the merge the same edit fills it."""
    w, au, so, cat, inbound, b = setup(tmp_path, expected=5)
    m = Merge(w, au.commerce, au)
    s = w.save_product({'sku': 'S', 'title': 'Toploader scan', 'identifiers': [{'kind': 'EAN', 'value': S_EAN}]})['id']
    w.receive(1, s, 2, None, b['A1'])                       # extra stock, cost not entered
    r = m.run(s, {'audit_id': row_of(w)}, 1)
    p = w.get_product(s)
    p['default_cost_cents'] = 700
    w.save_product({**p, 'identifiers': [{'kind': i['kind'], 'value': i['value']} for i in p['identifiers']]}, s, 1)
    m.undo(r['merge_id'], 1)
    missing = w._q1("""SELECT COALESCE(SUM(r.qty), 0) AS n FROM receipt r JOIN action a ON a.id = r.action_id
                        WHERE r.product_id = ? AND a.reversed_by IS NULL AND r.cost_missing = 1""", (s,))['n']
    assert (w.get_product(s)['default_cost_cents'], missing) == (700, 0), missing


def test_merge_undo_takes_the_row_asin_off_an_item_found_on_the_row_since(tmp_path):
    """S (2 extras) is merged into its own Stock audit row: it takes the row's ASIN and is the row's item. Sam then
    taps Found it on that row for 1 more unit (booked as S). Alex undoes the merge: the ASIN comes off S, so the row
    has no item while S holds the unit Sam found there; the next Found it makes a second item for the same thing."""
    w, au, so, cat, inbound, b = setup(tmp_path, expected=5)
    m = Merge(w, au.commerce, au)
    s = w.save_product({'sku': 'S', 'title': 'Toploader scan', 'identifiers': [{'kind': 'EAN', 'value': S_EAN}]})['id']
    w.receive(1, s, 2, 900, b['A1'])
    row = row_of(w)
    r = m.run(s, {'audit_id': row}, 1)
    au.found(row, 1, b['A2'], 2)                            # Sam, after the merge
    assert w._q1('SELECT product_id FROM stock_level WHERE box_id = ?', (b['A2']['box_id'],))['product_id'] == s
    m.undo(r['merge_id'], 1)
    assert au.item(row)['product_id'] == s, au.item(row)['product_id']


def test_extras_list_after_a_chain_drops_the_shipped_extra_and_keeps_the_one_found(tmp_path):
    """S has 2 extras: 1 older in A1 at $9, 1 newer in A2 at $3. The A2 unit ships. Merge S into T (no row, no buys):
    the unit in A1 goes over as T's extra stock at $9 (its own receipt's cost: run() takes this place's extras first).
    Then T into U, whose Stock audit row finds it. The Stock audit's extras list should keep only the extra that shipped
    (A2, $3), as it would with no merge; Sorting.extras() drops the newest S row first instead, so it lists the A1 unit
    (found on U's row now) at $9 and drops the shipped one."""
    w, au, so, cat, inbound, b = setup(tmp_path)
    m = Merge(w, au.commerce, au)
    s = w.save_product({'sku': 'S', 'title': 'Toploader scan', 'identifiers': [{'kind': 'EAN', 'value': S_EAN}]})['id']
    so.add(b['A1'], {'kind': 'new', 'product_id': s}, 1, 1, cost_cents=900, scan_id='e1')
    so.add(b['A2'], {'kind': 'new', 'product_id': s}, 1, 1, cost_cents=300, scan_id='e2')
    w.remove(1, b['A2'], [{'product_id': s, 'qty': 1}], 'CUSTOMER')
    t = w.save_product({'sku': 'T', 'title': 'Toploader b', 'identifiers': [{'kind': 'UPC', 'value': '036000291452'}]})['id']
    m.run(s, {'product_id': t}, 1)
    carried = w._q1("SELECT unit_cost_cents FROM receipt WHERE product_id = ? AND reference = 'merge'", (t,))['unit_cost_cents']
    assert carried == 900                                   # the A1 unit went over, as run() says
    u = w.save_product({'sku': 'U', 'title': 'Toploader', 'identifiers': [{'kind': 'ASIN', 'value': ASIN}]})['id']
    m.run(t, {'product_id': u}, 1)
    ex = so.extras()
    assert [(r['bin'], r['qty'], r['unit_cost_cents']) for r in ex['rows']] == [('A2', 1, 300)], ex['rows']
