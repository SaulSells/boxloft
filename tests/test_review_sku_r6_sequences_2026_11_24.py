"""Review 2026-11-24-234650-f7e8aa6a: numbers past 64 bits in a query or body gave 500s (now a 400 for every route),
and erased items' ids came back for new items, which inherited their listing_link and ebay_made rows. A new item
now never takes an erased id, so the two id-reuse proofs assert the id differs instead of c == a."""
import os, sys
REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))
from test_sell_sku import setup, item, listing  # noqa: E402


def test_c6_old_link_undo_after_erase_and_id_reuse_unlinks_the_new_item(tmp_path):
    """Same class as the sell_sku reuse bug the fix closed: listing_link rows are keyed by product_id and survive an
    erase. Link listing L to item A (no history, so it can be erased) -> erase A -> new item C gets A's id -> link L
    to C -> an old tab's Undo of the first link (still not undone) takes L and its ASIN off C."""
    w, li, box = setup(tmp_path)
    a = w.save_product({'sku': 'A', 'title': 'Item A'})['id']
    listing(w, 'L-1', 'B0LLLLLLL1')
    lid = w._q1("SELECT id FROM listing WHERE sku = 'L-1'")['id']
    first = li.link(a, lid, 1)['link_id']
    w.delete_product(a, 1)    # a listing is only a pointer: erase allowed
    c = w.save_product({'sku': 'C', 'title': 'Item C'})['id']
    assert c != a             # an erased item's id never comes back (review 11-24)
    li.link(c, lid, 1)
    try:
        li.unlink(first, 1)   # old tab: Undo of A's link
    except Exception:
        pass                  # refusing is the fix
    assert w._q1('SELECT product_id FROM listing WHERE id = ?', (lid,))['product_id'] == c
    assert w._q1("SELECT 1 FROM product_identifier WHERE product_id = ? AND value = 'B0LLLLLLL1'", (c,)), \
        "C's ASIN, put on by C's own link, was taken off by the undo of erased A's link"


def test_c6b_new_item_with_reused_id_inherits_erased_items_ebay_made_backlog(tmp_path):
    """Same class: ebay_made is keyed by product_id and survives an erase. Item A made from an eBay listing seen on
    the first read (make_missing writes ebay_made) -> A is erased (a listing is only a pointer) -> new item C gets
    A's id -> backlog_ship(C) is True, so C's eBay sales close as supplier -> customer with cost not entered,
    though C was never made from a listing."""
    import types
    from ebay_link import EbayLink
    w, li, box = setup(tmp_path)
    el = EbayLink(w, li.commerce, types.SimpleNamespace(), li, None)
    a = w.save_product({'sku': 'EBAY-1', 'title': 'Item A'})['id']
    with w._tx() as c:
        lid = c.execute("""INSERT INTO listing (channel, sku, asin, title, status, qty, first_seen, seen_at, product_id)
                           VALUES ('ebay', 'ITEM-1', '', 'x', 'Active', 1, '2026-11-12T00:00:00Z', 'x', ?)""", (a,)).lastrowid
        c.execute("INSERT OR IGNORE INTO ebay_made (product_id, listing_id, how, user_id, at) VALUES (?,?,?,?,?)",
                  (a, lid, 'auto', 0, '2026-11-12T00:00:00Z'))
    assert el.backlog_ship(a)
    w.delete_product(a, 1)
    c = w.save_product({'sku': 'C', 'title': 'Item C'})['id']
    assert c != a             # an erased item's id never comes back (review 11-24)
    assert not el.backlog_ship(c), "C inherits erased A's eBay backlog pass"
