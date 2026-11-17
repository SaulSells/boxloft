import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

from merge import Merge  # noqa: E402
from test_merge_grid import po, row_of  # noqa: E402
from test_sorting import ASIN, setup  # noqa: E402

Merge.enabled = True


def _older_line(w, pid, qty):
    with w._tx() as c:
        o = c.execute("INSERT INTO purchase_order (source, external_id, ordered_at, imported_at) VALUES ('walmart.com', 'W-OLD', '2025-05-01', 'x')").lastrowid
        return c.execute('INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents, product_id) VALUES (?,?,?,?,?,?,?)',
                         (o, 'W-OLD', '', 'Toploader', qty, 650, pid)).lastrowid


def test_merge_into_item_with_row_and_older_buy_line_counts_row_once(tmp_path):
    """T has the audit row's ASIN (3 left) and an older Walmart buy line of 4. S holds 5 extras. The preview says 3 go on
    the row and 2 on open orders. The row must gain exactly 3, never more than it expects."""
    w, au, so, cat, inbound, b = setup(tmp_path, expected=3)
    m = Merge(w, au.commerce, au)
    t = w.save_product({'sku': 'T-1', 'title': 'Toploader 10ct', 'identifiers': [{'kind': 'ASIN', 'value': ASIN}]})['id']
    _older_line(w, t, 4)
    s = w.save_product({'sku': 'S-1', 'title': 'Toploader (scanned)'})['id']
    w.receive(1, s, 5, 900, b['A1'])
    row = row_of(w)
    acc0 = au.item(row)['accounted']
    pre = m.preview(s, {'product_id': t})
    assert (pre['to_row'], pre['to_lines'], pre['extra']) == (3, 2, 0), pre
    m.run(s, {'product_id': t}, 1, seen={'on_hand': 5})
    it = au.item(row)
    assert it['accounted'] - acc0 == 3 and it['accounted'] <= it['expected'], it
