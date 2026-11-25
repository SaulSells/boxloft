# Round 3 proofs (review 2026-11-24-230842-320a27bc). The four that asserted an unsaved preview never moves now copy the
# SKU first (save_sell_sku) and assert the saved SKU stays: a SKU is fixed when first copied; a preview may move.
import os, sys
REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

from test_sell_sku import setup, item, listing  # noqa: E402


def _po(w, pid, ext, day, cents, imported_at, source='amazon', account='', review=''):
    with w._tx() as c:
        po = c.execute("INSERT INTO purchase_order (source, external_id, ordered_at, imported_at, state, hidden, account, review) "
                       "VALUES (?, ?, ?, ?, 'ordered', 0, ?, ?)", (source, ext, day, imported_at, account, review)).lastrowid
        return c.execute("INSERT INTO purchase_line (po_id, line_key, product_id, title, qty, unit_cost_cents) "
                         "VALUES (?, ?, ?, 'x', 1, ?)", (po, ext, pid, cents)).lastrowid


def test_c14_relinked_old_import_moves_a_shown_sku(tmp_path):
    """A is received and shows the base SKU (Alex pastes it). An old buy line imported months ago with no product
    gets linked to B (relink / Receive picking it). Its imported_at is older than A's receipt, so B jumps ahead in
    the turns and A's shown SKU flips to -2."""
    w, li, box = setup(tmp_path)
    a = item(w, 'A', 'B0AAAAAAA1')
    b = item(w, 'B', 'B0BBBBBBB1')
    w.receive(1, a, 1, 3999, box, purchased_at='2025-08-01')
    before = li.save_sell_sku(a, 1)['sku']   # Alex copies it: the first copy saves it for good (round 3 redesign; an unsaved preview may move)
    assert before == '8.1.25-39.99'
    line = _po(w, None, '112-OLD', '2025-08-01', 3999, '2025-08-02T00:00:00Z')
    with w._tx() as c:   # what relink_buys / Receive's pick does
        c.execute("UPDATE purchase_line SET product_id = ? WHERE id = ?", (b, line))
    assert li.sell_sku(a)['sku'] == before


def test_c8_store_order_waiting_for_approval_gives_a_sku(tmp_path):
    """A Gmail store order not yet approved (review set) is not inventory yet, but its priced line gives a SKU."""
    w, li, box = setup(tmp_path)
    a = item(w, 'A', 'B0AAAAAAA1')
    _po(w, a, 'W-1', '2025-08-01', 3999, '2025-08-02T00:00:00Z', source='walmart.com', account='gmail',
        review='New store order')
    assert li.sell_sku(a)["estimated"]


def test_c8b_unapproved_store_order_takes_a_turn_then_hide_flips_shown_sku(tmp_path):
    """The unapproved store order takes the base turn, so B (received) shows -2 and Alex pastes it. A person then
    marks the store order Not inventory on Arriving: B's shown SKU flips to the base."""
    w, li, box = setup(tmp_path)
    a = item(w, 'A', 'B0AAAAAAA1')
    b = item(w, 'B', 'B0BBBBBBB1')
    _po(w, a, 'W-1', '2025-08-01', 3999, '2000-01-01T00:00:00Z', source='walmart.com', account='gmail',
        review='New store order')
    w.receive(1, b, 1, 3999, box, purchased_at='2025-08-01')
    before = li.sell_sku(b)['sku']
    with w._tx() as c:
        c.execute("UPDATE purchase_order SET hidden = 1 WHERE external_id = 'W-1'")
    assert li.sell_sku(b)['sku'] == before


def test_c24_every_name_taken_returns_a_taken_sku(tmp_path):
    """Listings of other ASINs hold base, -2 ... -199: the loop ends without a free name and returns base-199,
    which another ASIN's listing holds (Amazon refuses it)."""
    w, li, box = setup(tmp_path)
    a = item(w, 'A', 'B0AAAAAAA1')
    w.receive(1, a, 1, 3999, box, purchased_at='2025-08-01')
    base = '8.1.25-39.99'
    for n in range(1, 200):
        listing(w, base if n == 1 else f'{base}-{n}', f'B0OTH{n:05d}')
    s = li.sell_sku(a)
    if s:
        held = {x['asin'] for x in w._q("SELECT asin FROM listing WHERE channel='amazon' AND sku=?", (s['sku'],))}
        assert not held, (s['sku'], held)


from hypothesis import settings, HealthCheck, strategies as st  # noqa: E402
from hypothesis.stateful import rule  # noqa: E402
from test_sell_sku_model import SellSkus, DAYS, CENTS, ITEMS  # noqa: E402


class SellSkusWithRelink(SellSkus):
    """Patch: an old buy line (imported before any save of this run) gets linked to an item, as relink_buys or
    Receive's pick does. Rule 4 must still hold: no other item's shown SKU moves."""

    @rule(i=st.integers(0, ITEMS - 1), day=st.sampled_from(DAYS), cents=st.sampled_from(CENTS))
    def link_old_line(self, i, day, cents):
        self.n_po += 1
        with self.w._tx() as c:
            po = c.execute("""INSERT INTO purchase_order (source, external_id, ordered_at, imported_at, state, hidden)
                              VALUES ('amazon', ?, ?, '2000-01-01T00:00:00Z', 'ordered', 0)""",
                           (f'112-R{self.n_po}', day)).lastrowid
            ln = c.execute("""INSERT INTO purchase_line (po_id, line_key, title, qty, unit_cost_cents)
                              VALUES (?, ?, 'x', 1, ?)""", (po, f'R{self.n_po}', cents)).lastrowid
            c.execute("UPDATE purchase_line SET product_id = ? WHERE id = ?", (self.items[i], ln))
        self._step(self.items[i])


SellSkusWithRelink.TestCase.settings = settings(max_examples=200, stateful_step_count=15, deadline=None,
                                                suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much])
TestSellSkusWithRelink = SellSkusWithRelink.TestCase
