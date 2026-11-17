import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

from audit import Audit  # noqa: E402
from commerce import Commerce  # noqa: E402
from merge import Merge  # noqa: E402

Merge.enabled = True
ASIN_X = 'B0STEGO001'


def _store_row_confirmed(tmp_path):
    """An approved store-email buy of 8 (no ASIN) whose Stock audit row LINE-<id> a person matched to an Amazon listing
    (listing_mark confirm). Nobody found a unit yet, so its buy line has no item; the row's item is the one with the
    listing's ASIN (audit._rows: product_id through the mark ASIN)."""
    import listings
    from test_store_mail import FakeJev, TOYS, make
    w, inbound, so = make(tmp_path)
    w._conn().executescript(listings.SCHEMA) if hasattr(w, '_conn') else w._db().executescript(listings.SCHEMA)
    au = Audit(w, Commerce(w))
    jev = FakeJev('TOY12345', '$236.45', {'Stegosaurus': ('$30.80', '$246.40', '8')})
    po = so.take('<m1@toys>', 'Toys <orders@toys.example.com>', 'Order confirmed', '2026-05-04T01:40:37Z', TOYS, jev, '4242')['po_id']
    so.approve(po)
    [it] = [i for i in au.list()['items'] if i['line_id']]
    t = w.save_product({'sku': ASIN_X, 'title': 'Stegosaurus figure', 'identifiers': [{'kind': 'ASIN', 'value': ASIN_X}]})['id']
    with w._tx() as c:
        lid = c.execute("""INSERT INTO listing (channel, sku, asin, title, status, qty, first_seen, seen_at)
                           VALUES ('amazon', 'STEGO-1', ?, 'Stegosaurus', 'Active', 3, 'x', 'x')""", (ASIN_X,)).lastrowid
        c.execute("INSERT INTO listing_mark (item_id, kind, listing_id, user_id, at) VALUES (?, 'confirm', ?, 1, 'x')", (it['id'], lid))
    it = au.item(it['id'])
    assert it['product_id'] == t and it['expected'] - it['accounted'] == 8, (it['product_id'], t, it['expected'], it['accounted'])
    box = w.create_boxes(1, 1)[0]['id']
    return w, au, it['id'], t, box


def test_merge_misses_store_row_matched_by_listing(tmp_path):
    """Target T is the item of a store-email row through the listing a person confirmed on it (the row's buy line has
    no item yet). S (a duplicate) holds 2 units scanned in as extra stock. Merge S into T: _row_for looks for the
    target's rows by i.asin (a store row's is LINE-<id>) and then by purchase_line.product_id (NULL here), so it never
    sees the row; _free() drops the row's line as audited. The 2 units go to T as extra stock while the row still
    expects all 8 (counted twice: on hand as extras and still to find). A person's Found it on that row would give the
    units to T, so the merge must find them there (as test_review21 asks for rows linked by the buy line)."""
    w, au, row, t, box = _store_row_confirmed(tmp_path)
    m = Merge(w, au.commerce, au)
    s = w.save_product({'sku': 'S-1', 'title': 'Stegosaurus (scanned)', 'identifiers': [{'kind': 'EAN', 'value': '4006381333931'}]})['id']
    w.receive(1, s, 2, 900, {'box_id': box})
    pv = m.preview(s, {'product_id': t})
    m.run(s, {'product_id': t}, 1)
    it = au.item(row)
    extras_on_target = w._q1("""SELECT COALESCE(SUM(r.qty), 0) AS n FROM receipt r JOIN action a ON a.id = r.action_id
                                 WHERE r.product_id = ? AND r.reference = 'merge' AND a.reversed_by IS NULL""", (t,))['n']
    assert (pv['to_row'], it['expected'] - it['accounted'], extras_on_target) == (2, 6, 0), \
        (pv, it['expected'] - it['accounted'], extras_on_target)
