import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

from audit import Audit  # noqa: E402
from commerce import Commerce  # noqa: E402
from merge import Merge  # noqa: E402

Merge.enabled = True


def _store_row(tmp_path):
    """An approved store-email buy of 8 (no ASIN): its Stock audit row LINE-<id>, 3 found, so the row's item exists."""
    from test_store_mail import FakeJev, TOYS, make
    w, inbound, so = make(tmp_path)
    au = Audit(w, Commerce(w))
    jev = FakeJev('TOY12345', '$236.45', {'Stegosaurus': ('$30.80', '$246.40', '8')})
    po = so.take('<m1@toys>', 'Toys <orders@toys.example.com>', 'Order confirmed', '2026-05-04T01:40:37Z', TOYS, jev, '4242')['po_id']
    so.approve(po)
    [it] = [i for i in au.list()['items'] if i['line_id']]
    box = w.create_boxes(1, 1)[0]['id']
    it = au.found(it['id'], 3, {'box_id': box}, 1)
    pid = it['product_id']
    assert pid and it['expected'] - it['accounted'] == 5
    return w, au, it['id'], pid, box


def test_merge_into_item_of_a_store_row_leaves_extras_off_the_row_and_its_line(tmp_path):
    """Target = the item of a store-email Stock audit row (LINE-<id>, no ASIN). The source's 2 extras should be found on
    that row (or at least received on its buy line). _row_for looks rows up by ASIN only, so it never sees the store
    row, and _free() drops the row's line as 'audited': the 2 units stay extra stock while the row still expects them
    (counted twice: on hand as extras, and still to find on the Stock audit)."""
    w, au, row, pid, box = _store_row(tmp_path)
    m = Merge(w, au.commerce, au)
    s = w.save_product({'sku': 'S-1', 'title': 'Stegosaurus (scanned)', 'identifiers': [{'kind': 'EAN', 'value': '4006381333931'}]})['id']
    w.receive(1, s, 2, 900, {'box_id': box})
    pv = m.preview(s, {'product_id': pid})
    m.run(s, {'product_id': pid}, 1)
    it = au.item(row)
    extras_on_target = w._q1("""SELECT COALESCE(SUM(r.qty), 0) AS n FROM receipt r JOIN action a ON a.id = r.action_id
                                 WHERE r.product_id = ? AND r.reference = 'merge' AND a.reversed_by IS NULL""", (pid,))['n']
    # The 2 units are this row's: found there, not booked as extra stock with the row still waiting for them.
    assert (pv['to_row'] + pv['to_lines'], it['expected'] - it['accounted'], extras_on_target) == (2, 3, 0), (pv, it['accounted'], extras_on_target)


def test_merge_into_own_store_row_puts_line_key_on_the_item_as_an_asin(tmp_path):
    """The store row's item P has 2 extra units (scanned as extra stock before anyone tied them to the row). "Same item
    as" its own Stock audit row: run() gives the source the row's ASIN, but a store-email row's `asin` is its key
    'LINE-<line id>'. P gets a fake ASIN 'LINE-n' (shown as its ASIN, sent to Amazon's approval checks, matched against
    listings and buys)."""
    w, au, row, pid, box = _store_row(tmp_path)
    m = Merge(w, au.commerce, au)
    w.receive(1, pid, 2, 900, {'box_id': box})   # extras of the same item
    m.run(pid, {'audit_id': row}, 1)
    bad = [i['value'] for i in w._q("SELECT value FROM product_identifier WHERE product_id = ? AND kind = 'ASIN'", (pid,))
           if i['value'].upper().startswith('LINE-')]
    assert bad == [], bad


def test_box_line_undo_archives_kept_item_and_drops_the_merged_codes(tmp_path):
    """Sam's new item card makes N (1 unit in A1, put_made). Alex then merges S (a duplicate with its barcode, an open
    buy and no stock here) into N: S is archived, its barcode and buy now N's. Sam taps Undo on her line: _undo_made sees
    no later stock move on N (the merge moved none), so it archives N and deletes every code on it, the merged barcode
    too, and unlinks the buy. Both items end archived and the scan of S's barcode finds no item."""
    from test_sorting import setup
    from test_merge_grid import po
    w, au, so, cat, inbound, b = setup(tmp_path)
    m = Merge(w, au.commerce, au)
    r = so.put_new(b['A1'], 1, scan_id='n1', code='0799999999990', title='Dino figure')
    n = r['product_id']
    assert w._q1('SELECT product_id FROM put_made WHERE add_id = ?', (r['add_id'],))
    s = w.save_product({'sku': 'S-1', 'title': 'Dino figure (old)', 'identifiers': [{'kind': 'EAN', 'value': '4006381333931'}]})['id']
    ln = po(w, 'target.com', 'T9', 2, 700, s)
    m.run(s, {'product_id': n}, 1)
    so.undo_add(r['add_id'], 1)
    live = [p['id'] for p in w.lookup('4006381333931') if not p['archived']]
    line_owner = w._q1('SELECT product_id FROM purchase_line WHERE id = ?', (ln,))['product_id']
    assert live and line_owner, (live, line_owner, w._q1('SELECT archived FROM product WHERE id = ?', (n,)))
