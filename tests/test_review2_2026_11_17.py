import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

import pytest  # noqa: E402
from merge import Merge  # noqa: E402
from test_sorting import setup  # noqa: E402
from test_card_listings import with_listings  # noqa: E402
from test_box_line_matrix import _card, _extra  # noqa: E402
from wms import WmsError  # noqa: E402

Merge.enabled = True


def _set_default(w, pid, cents):
    p = w.get_product(pid)
    w.save_product({'sku': p['sku'], 'title': p['title'], 'default_cost_cents': cents,
                    'identifiers': [{'kind': i['kind'], 'value': i['value']} for i in p['identifiers']]}, pid)


def test_pile_change_gives_a_not_entered_line_the_items_avg_cost(tmp_path):
    """A new item card line goes in with cost not entered. Later another unit of the item is received at $9. Sam
    changes the line's count to 2: put_qty re-saves it with line_cost() = None, and add() then takes the item's avg
    cost, so the 2 units now carry $9 each as if someone typed it, and a Usual unit cost will never fill them."""
    w, so, boxes, lid = with_listings(tmp_path)
    r, _ = _card(w, so, so.catalog, boxes, lid)
    pid = r['product_id']
    assert so.line_cost(r['add_id'])['cost_cents'] is None
    w.receive(1, pid, 1, 900, boxes['A2'])
    q = so.put_qty(r['add_id'], 2, 1, over_ok=True)
    assert so.line_cost(q['add_id'])['cost_cents'] is None, \
        f"nobody entered a cost; the pile change gave the line {so.line_cost(q['add_id'])['cost_cents']}"


def test_pile_change_after_blanking_the_cost_freezes_it(tmp_path):
    """Sam blanks a line's cost (set_cost None = not entered, so the Usual unit cost fills it). A pile change then
    saves the line at the item's usual cost as a typed cost; the next Usual unit cost edit no longer reaches it."""
    w, so, boxes, lid = with_listings(tmp_path)
    r, _ = _extra(w, so, so.catalog, boxes, lid)       # Red mug, usual cost $3
    pid = r['product_id']
    so.set_cost(r['add_id'], None, 1)
    q = so.put_qty(r['add_id'], 2, 1, over_ok=True)
    _set_default(w, pid, 700)
    assert so.line_cost(q['add_id'])['cost_cents'] == 700, \
        f"blank = follows the usual cost; after a pile change the line stays at {so.line_cost(q['add_id'])['cost_cents']}"


def test_preview_into_an_audit_row_with_no_item_misses_the_rows_asin_buy_lines(tmp_path):
    """Merge an item with no ASIN (4 extras in A1) into a Stock audit row with no item (3 left). run() first gives the
    source the row's ASIN, so its open buy lines include a later order of that ASIN, and the 4th unit is received on
    it. preview() counts buy lines for the source before it has the ASIN: it says 0 go on buy lines and 1 stays as is.
    The confirm screen tells the person something other than what the save does."""
    from test_sorting import ASIN
    w, au, so, cat, inbound, boxes = setup(tmp_path, expected=3)
    with w._tx() as c:
        po = c.execute("INSERT INTO purchase_order (source, external_id, ordered_at, imported_at) "
                       "VALUES ('amazon', '112-0000002-0000000', '2025-07-01', 'x')").lastrowid
        line2 = c.execute('INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents) VALUES (?,?,?,?,?,?)',
                          (po, ASIN + '-2', ASIN, 'Toploader', 2, 700)).lastrowid
    s = w.save_product({'sku': 'TL-DUP', 'title': 'Toploader (no code)'})['id']
    w.receive(1, s, 4, 500, boxes['A1'])
    row = w._q1('SELECT id FROM audit_item WHERE asin = ?', (ASIN,))
    it = au.item(row['id'])
    assert not it['product_id'] and it['expected'] - it['accounted'] == 3, it
    m = Merge(w, au.commerce, au)
    pv = m.preview(s, {'audit_id': row['id']})
    m.run(s, {'audit_id': row['id']}, 1, seen={'on_hand': 4})
    on_line2 = w._q1("""SELECT COALESCE(SUM(r.qty), 0) AS n FROM receipt r JOIN action a ON a.id = r.action_id
                         WHERE r.purchase_line_id = ? AND a.reversed_by IS NULL""", (line2,))['n']
    assert au.item(row['id'])['accounted'] == 3
    assert (pv['to_row'], pv['to_lines'], pv['as_is']) == (3, on_line2, 4 - 3 - on_line2), (pv, 'run put', on_line2, 'on the later buy line')


def test_merge_extra_left_over_takes_the_average_of_extras_incl_the_voided_one(tmp_path):
    """Source (no buys) holds 2 extras: one received at $1.00, a newer one at $10.00. The target's Stock audit row has 1
    left. The merge finds 1 on the row and voids the NEWEST extra receipt ($10), so the unit left over is the $1 one,
    and the source's own books say so. But the left-over unit goes onto the target at extra_cost, the average of both
    extras ($5.50): the target's Avg cost carries $4.50 of cost no unit has."""
    from test_sorting import ASIN
    w, au, so, cat, inbound, boxes = setup(tmp_path, expected=1)
    t = w.save_product({'sku': 'TL', 'title': 'Toploader', 'identifiers': [{'kind': 'ASIN', 'value': ASIN}]})['id']
    s = w.save_product({'sku': 'TL-DUP', 'title': 'Toploader dup'})['id']
    w.receive(1, s, 1, 100, boxes['A1'])
    w.receive(1, s, 1, 1000, boxes['A1'])
    m = Merge(w, au.commerce, au)
    m.run(s, {'product_id': t}, 1, seen={'on_hand': 2})
    assert w._q1('SELECT COALESCE(SUM(qty), 0) AS n FROM stock_level WHERE product_id = ? AND box_id != 0', (t,))['n'] == 2
    voided = w._q1('SELECT r.unit_cost_cents AS c FROM receipt_void v JOIN receipt r ON r.id = v.receipt_id')['c']
    assert voided == 1000                                    # the $10 extra was the one re-booked on the row
    left_over = w._q1("SELECT unit_cost_cents AS c FROM receipt WHERE product_id = ? AND reference = 'merge'", (t,))['c']
    assert left_over == 100, f'the unit left over is the $1.00 one; the target booked it at ${left_over / 100:.2f}'


def test_cost_typed_on_a_line_after_its_item_was_merged_is_lost(tmp_path):
    """Sam scans a Red mug in as extra stock; her box page shows the line with "Cost each". Alex merges the Red mug into
    its duplicate item B from the product page (the unit becomes B's, as extra stock at the mug's cost). Sam then types
    $12 on her line: set_cost writes it onto the archived Red mug's receipt and says saved, but the unit is B's now and
    B still carries the old cost. The save should be refused (or reach B)."""
    w, so, boxes, lid = with_listings(tmp_path)
    r, _ = _extra(w, so, so.catalog, boxes, lid)        # Red mug at its usual $3.00
    a = r['product_id']
    au = so.audit
    b = w.save_product({'sku': 'MUG-B', 'title': 'Red mug (dup)'})['id']
    Merge(w, au.commerce, au).run(a, {'product_id': b}, 1, seen={'on_hand': 1})
    try:
        so.set_cost(r['add_id'], 1200, 1)
    except WmsError:
        return                                            # refusing is a fix
    assert w.get_product(b)['avg_cost_cents'] == 1200, \
        f"saved as $12.00, but the unit (now item B) still costs {w.get_product(b)['avg_cost_cents']}"


def test_extras_list_after_a_merge_chain_still_shows_a_unit_found_on_the_row(tmp_path):
    """A box page scan put 1 Red mug in as extra stock (audit_extra on item A). A is merged into a duplicate item B
    (no row, no buy: the unit moves over as extra stock on a new B receipt; A's receipt stays live). Then B is merged
    into its Stock audit row: the unit is Found on the row and B's merge receipt is voided. A's audit_extra still
    stands with an unvoided receipt, so Stock audit's extras list shows the unit as extra stock as well as found."""
    w, so, boxes, lid = with_listings(tmp_path)
    r, _ = _extra(w, so, so.catalog, boxes, lid)
    a = r['product_id']
    au = so.audit
    m = Merge(w, au.commerce, au)
    b = w.save_product({'sku': 'MUG-B', 'title': 'Red mug (dup)'})['id']
    m.run(a, {'product_id': b}, 1, seen={'on_hand': 1})
    row = w._q1('SELECT id FROM audit_item ORDER BY id LIMIT 1')
    it = au.item(row['id'])
    assert it['expected'] - it['accounted'] >= 1 and not it['product_id'], it
    m.run(b, {'audit_id': row['id']}, 1, seen={'on_hand': 1})
    assert au.item(row['id'])['accounted'] == it['accounted'] + 1          # found on the row
    total = w._q1('SELECT COALESCE(SUM(qty), 0) AS n FROM stock_level WHERE box_id = ?', (boxes['A1']['box_id'],))['n']
    assert total == 1
    ex = so.extras()
    assert ex['units'] == 0, ('1 unit in the box, found on its row, and still listed as extra stock',
                              [(x['product_id'], x['qty']) for x in ex['rows']])
