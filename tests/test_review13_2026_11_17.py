import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

from merge import Merge  # noqa: E402
from test_sorting import ASIN, setup  # noqa: E402
from wms import now_utc  # noqa: E402

Merge.enabled = True


def row_of(w):
    return w._q1('SELECT id FROM audit_item WHERE asin = ?', (ASIN,))['id']


def test_voided_units_still_show_as_came_in_with_no_cost(tmp_path):
    """Merge into the item's own Stock audit row: its 3 extras (no cost) are found on the row at the order's cost and
    their old receipt is voided. Avg cost leaves them out, but the product page's receipts (what Product.tsx counts for
    "N units came in with no cost") still list all 3 as no cost, and saving a Usual unit cost says it filled 3 units."""
    w, au, so, cat, inbound, b = setup(tmp_path, expected=3)
    m = Merge(w, au.commerce, au)
    s = w.save_product({'sku': 'S-1', 'title': 'Toploader (scanned)'})['id']
    w.receive(1, s, 3, None, b['A1'])
    m.run(s, {'audit_id': row_of(w)}, 1)
    p = w.get_product(s)
    assert p['avg_cost_cents'] == 500 and p['on_hand'] == 3        # all 3 now carry the order's cost
    # Product.tsx: receipts.filter(!reversed_by && unit_cost_cents === null).reduce(qty)
    no_cost = sum(r['qty'] for r in p['receipts'] if not r['reversed_by'] and r['unit_cost_cents'] is None)
    assert no_cost == 0, f'product page says {no_cost} units came in with no cost'
    saved = w.save_product({'sku': p['sku'], 'title': p['title'], 'default_cost_cents': 2000,
                            'identifiers': [{'kind': i['kind'], 'value': i['value']} for i in p['identifiers']]}, s)
    assert saved['cost_filled']['units'] == 0, saved['cost_filled']


def test_client_item_merge_finds_client_units_on_boxlofts_audit_row(tmp_path):
    """Two client (3PL) items, the target carrying an ASIN that is on Boxloft's Stock audit. _resolve refuses a client
    item as an audit row's item (same), but a client -> client merge finds the client's units on Boxloft's row and
    receives them on Boxloft's Amazon order line: client stock booked as Boxloft's buy."""
    w, au, so, cat, inbound, b = setup(tmp_path, expected=3)
    m = Merge(w, au.commerce, au)
    with w._tx() as c:
        c.execute("INSERT INTO owner (name, created_at) VALUES ('Client Co', ?)", (now_utc(),))
        client = c.execute("SELECT id FROM owner WHERE name = 'Client Co'").fetchone()[0]
    t = w.save_product({'sku': 'CL-T', 'title': 'Client toploader', 'owner_id': client,
                        'identifiers': [{'kind': 'ASIN', 'value': ASIN}]})['id']
    s = w.save_product({'sku': 'CL-S', 'title': 'Client toploader dup', 'owner_id': client})['id']
    w.receive(1, s, 2, 400, b['A1'])
    row = row_of(w)
    before = au.item(row)['accounted']
    pre = m.preview(s, {'product_id': t})
    m.run(s, {'product_id': t}, 1)
    on_boxloft_line = w._q1("""SELECT COALESCE(SUM(r.qty), 0) AS n FROM receipt r JOIN action a ON a.id = r.action_id
                                 JOIN purchase_line l ON l.id = r.purchase_line_id
                                WHERE r.product_id = ? AND a.reversed_by IS NULL""", (t,))['n']
    assert (pre['to_row'], au.item(row)['accounted'] - before, on_boxloft_line) == (0, 0, 0)
