"""Review 2026-11-24-233530-f1ba0cff: ids past 64 bits or 4300 digits gave 500s; an erased item's saved SKU went to
the next item with its id. A copied SKU now counts as history (archive, never erase), so the last proof's erase is
refused here and the item is archived instead."""
import os, sys
REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))
from test_amazon_approval import srv  # noqa: E402,F401


def test_c25_id_past_sqlite_int_is_not_500(srv):
    """A path id of 20 nines passes the router's isdigit check; sqlite can't bind it (OverflowError) -> 500."""
    server, call, calls, uid = srv
    for path, body in (('/api/products/99999999999999999999', None),
                       ('/api/products/99999999999999999999/sell-sku', {}),
                       ('/api/boxes/99999999999999999999', None)):
        st, r = call(path, body)
        assert st in (400, 404), (path, st, r)


def test_c25_id_of_5000_digits_is_not_500(srv):
    """int() refuses more than 4300 digits (ValueError) after the router let the part through -> 500."""
    server, call, calls, uid = srv
    st, r = call('/api/products/' + '9' * 5000, None)
    assert st in (400, 404, 414), (st, r)


def _line(w, ext, asin, pid, day='2026-10-12', cents=1015):
    with w._tx() as c:
        po = c.execute("""INSERT INTO purchase_order (source, external_id, ordered_at, imported_at)
                          VALUES ('amazon', ?, ?, ?)""", (ext, day, day + 'T00:00:00Z')).lastrowid
        c.execute("""INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents, product_id)
                     VALUES (?, 'L1', ?, 'x', 1, ?, ?)""", (po, asin, cents, pid))


def test_c29_new_item_with_reused_id_inherits_deleted_items_saved_sku(tmp_path):
    """Copy SKU on item A (a buy line only, so A has no history and can be erased) -> erase A -> a new item C gets
    the same row id (INTEGER PRIMARY KEY reuses the max id) -> C's own buy on that day and price: C shows A's saved
    SKU as saved, because sell_sku rows are keyed by product_id and nothing clears them on delete."""
    from test_sell_sku import setup, item
    w, li, box = setup(tmp_path)
    a = item(w, 'A', 'B0AAAAAAA1')
    _line(w, '112-A', 'B0AAAAAAA1', a)
    assert li.save_sell_sku(a, 1)['sku'] == '10.12.26-10.15'
    import pytest
    from wms import WmsError
    with pytest.raises(WmsError, match='Archive it instead'):   # a copied SKU is history: no erase
        w.delete_product(a, 1)
    w.archive_product(a, 1)
    c = item(w, 'C', 'B0CCCCCCC1')
    assert c != a   # A still holds its id
    _line(w, '112-C', 'B0CCCCCCC1', c)
    s = li.sell_sku(c)
    assert not s['saved'], s   # C never copied; A's saved row must not be C's
