# Round 3 proofs (review 2026-11-24-230842-320a27bc). The four that asserted an unsaved preview never moves now copy the
# SKU first (save_sell_sku) and assert the saved SKU stays: a SKU is fixed when first copied; a preview may move.
import os, sys
REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

from test_sell_sku import setup, item  # noqa: E402

DAY, CENTS, BASE = '2025-08-01', 3999, '8.1.25-39.99'


def _old_line(w, asin, pid=None, ext='112-OLD', state='ordered'):
    """A buy line imported long ago (tracker/CSV or extension read) on DAY at CENTS."""
    with w._tx() as c:
        po = c.execute("""INSERT INTO purchase_order (source, external_id, ordered_at, imported_at, state)
                          VALUES ('amazon', ?, ?, '2020-01-01T00:00:00Z', ?)""", (ext, DAY, state)).lastrowid
        c.execute("""INSERT INTO purchase_line (po_id, line_key, asin, product_id, title, qty, unit_cost_cents)
                     VALUES (?, ?, ?, ?, 'x', 1, ?)""", (po, ext, asin, pid, CENTS))
        return po


def test_relink_of_an_old_buy_line_moves_a_sku_already_shown(tmp_path):
    """An old buy line (no item yet) sits on DAY/CENTS. Item A is received on DAY/CENTS and shows the base SKU; Alex
    pastes it. Then item C is made with the line's ASIN: relink points the old line at C, whose 'first seen' is the
    line's old imported_at, so C jumps ahead and A's shown SKU turns into -2."""
    w, li, box = setup(tmp_path)
    _old_line(w, 'B0CCCCCCC1')
    a = item(w, 'A', 'B0AAAAAAA1')
    w.receive(1, a, 1, CENTS, box, purchased_at=DAY)
    before = li.save_sell_sku(a, 1)['sku']   # Alex copies it: the first copy saves it for good (round 3 redesign; an unsaved preview may move)
    assert before == BASE
    c = item(w, 'C', 'B0CCCCCCC1')
    from commerce import Commerce
    with w._tx() as cx:
        Commerce.relink_buys(cx)   # runs when a product is made or saved (server) and on each refresh
    assert w._q1('SELECT product_id FROM purchase_line')['product_id'] == c
    assert li.sell_sku(a)['sku'] == before, (li.sell_sku(a), li.sell_sku(c))


def test_cancelling_another_items_order_moves_a_sku_already_shown(tmp_path):
    """A (buy line, first) shows the base, B shows -2 and Alex pastes -2. A's order is cancelled on another page:
    A leaves the group and B's SKU turns into the base (a SKU no longer matching the listing he made)."""
    w, li, box = setup(tmp_path)
    a = item(w, 'A', 'B0AAAAAAA1')
    b = item(w, 'B', 'B0BBBBBBB1')
    po = _old_line(w, 'B0AAAAAAA1', a)
    w.receive(1, b, 1, CENTS, box, purchased_at=DAY)
    before = li.save_sell_sku(b, 1)['sku']   # Alex copies it: the first copy saves it for good (round 3 redesign; an unsaved preview may move)
    assert before == BASE + '-2'
    with w._tx() as cx:
        cx.execute("UPDATE purchase_order SET state = 'cancelled' WHERE id = ?", (po,))
    assert li.sell_sku(b)['sku'] == before


def test_history_undo_of_another_items_receipt_moves_a_sku_already_shown(tmp_path):
    """A received first (base), B second (-2, pasted). A's receipt was a mistake and gets History's Undo: B's SKU
    turns into the base."""
    w, li, box = setup(tmp_path)
    a = item(w, 'A', 'B0AAAAAAA1')
    b = item(w, 'B', 'B0BBBBBBB1')
    ra = w.receive(1, a, 1, CENTS, box, purchased_at=DAY)
    w.receive(1, b, 1, CENTS, box, purchased_at=DAY)
    before = li.save_sell_sku(b, 1)['sku']   # Alex copies it: the first copy saves it for good (round 3 redesign; an unsaved preview may move)
    assert before == BASE + '-2'
    act = w._q1('SELECT action_id FROM receipt WHERE product_id = ?', (a,))['action_id']
    w.undo(1, act)
    assert li.sell_sku(b)['sku'] == before


# Model patch: the model only makes orders cancelled or hidden at creation, and only links lines at creation. Real
# pages cancel or hide an order later, and relink points an old unlinked line at an item later. Both are changes to
# ONE item's buys, so rule 4 (every other item keeps the SKU it showed) must still hold.
from hypothesis import settings, HealthCheck, strategies as st  # noqa: E402
from hypothesis.stateful import rule  # noqa: E402
from test_sell_sku_model import SellSkus  # noqa: E402


class SellSkusLater(SellSkus):
    @rule(n=st.integers(1, 30), kind=st.sampled_from(['cancelled', 'hidden']))
    def order_changes_later(self, n, kind):
        r = self.w._q1("""SELECT o.id, l.product_id FROM purchase_order o JOIN purchase_line l ON l.po_id = o.id
                           WHERE o.external_id = ?""", (f'112-{n}',))
        if not r:
            return
        with self.w._tx() as c:
            c.execute(f"UPDATE purchase_order SET {'state' if kind == 'cancelled' else 'hidden'} = ? WHERE id = ?",
                      ('cancelled' if kind == 'cancelled' else 1, r['id']))
        self._step(r['product_id'])

    @rule(i=st.integers(0, 3), day=st.sampled_from(['2025-08-01', '2025-08-02']), cents=st.sampled_from([3999, 4000]))
    def old_line_relinked(self, i, day, cents):
        """A line imported long ago with no item gets linked to item i by relink."""
        self.n_po += 1
        with self.w._tx() as c:
            po = c.execute("""INSERT INTO purchase_order (source, external_id, ordered_at, imported_at, state)
                              VALUES ('amazon', ?, ?, '2020-01-01T00:00:00Z', 'ordered')""",
                           (f'OLD-{self.n_po}', day)).lastrowid
            c.execute("""INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents, product_id)
                         VALUES (?, ?, ?, 'x', 1, ?, ?)""", (po, f'O{self.n_po}', f'B0ITEM000{i}', cents, self.items[i]))
        self._step(self.items[i])


SellSkusLater.TestCase.settings = settings(max_examples=300, stateful_step_count=25, deadline=None,
                                           suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much])
TestSellSkusLater = SellSkusLater.TestCase
