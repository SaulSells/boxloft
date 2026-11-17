import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

from merge import Merge  # noqa: E402
from test_merge_grid import po, row_of  # noqa: E402
from test_sorting import ASIN, setup  # noqa: E402
from wms import LINE_LEFT_SQL  # noqa: E402

Merge.enabled = True


def _left(w, ln):
    return w._q1(f'SELECT {LINE_LEFT_SQL} AS n FROM purchase_line l WHERE l.id = ?', (ln,))['n']


def test_merge_rebooks_extra_on_new_buy_line_of_an_audited_asin(tmp_path):
    """The audit row for ASIN X is all found. A new Amazon buy of X (after the snapshot, not delivered yet, so on no
    audit row) is open with 1 left. S holds 1 extra and is merged into X's item: CLAUDE.md says extras go on the
    target's open buy lines. The merge now skips every line whose ASIN appears anywhere on the audit, so the unit
    becomes extra stock AND the line still says 1 coming: one unit counted twice."""
    w, au, so, cat, inbound, b = setup(tmp_path, expected=3)
    row = row_of(w)
    au.found(row, 3, b['A1'], 1)
    t = au.item(row)['product_id']
    ln = po(w, 'amazon', '112-9999999-0000001', 1, 800, None, asin=ASIN)
    assert _left(w, ln) == 1
    acc0 = au.item(row)['accounted']
    s = w.save_product({'sku': 'S-1', 'title': 'Toploader (scanned)'})['id']
    w.receive(1, s, 1, 900, b['A2'])
    m = Merge(w, au.commerce, au)
    pre = m.preview(s, {'product_id': t})
    m.run(s, {'product_id': t}, 1, seen={'on_hand': 1})
    assert au.item(row)['accounted'] == acc0
    assert (pre['to_lines'], _left(w, ln)) == (1, 0), (pre, _left(w, ln))
