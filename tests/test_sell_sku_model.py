"""Copy SKU (Alex 11-24) as a state machine: items get buys (receipts and buy lines, some on cancelled, Not inventory
or not yet approved store orders), receipts are undone or get a new cost, items are erased and made again, orders are cancelled, hidden or approved later, old buy lines
with no item get relinked, Amazon listings appear, and Alex copies SKUs (the first copy saves one for good).
After every step:
1. no two items show the same SKU, and no item shows a SKU another item saved;
2. a SKU not saved yet is never one an Amazon listing of another ASIN holds;
3. an item's SKU is its own newest dated, priced buy (or one of its -n turns);
4. a SAVED SKU never changes: while the item's newest buy is the one it was saved for, it shows that SKU, and the
   saved rows stay as written (review 11-24 round 3: relink, approve, cancel and hide moved a pasted SKU; previews
   may move by design, a saved SKU may not);
5. cancelled, Not inventory and not yet approved orders never give a dated, priced SKU;
6. every item has a SKU to copy (Alex 11-25: "regardless always have a copy button");
7. an item with no dated, priced buy gets M.D.YY[-price]-S<id> from what there is, and once one is copied the newest
   copied one shows;
8. a price paid (a typed receipt cost or a buy line) always wins over the Usual unit cost, even where the usual cost
   filled a receipt (review 11-25).
Review 11-24 found rules 1, 4 and 5 broken one at a time; this guards the class."""
import os
import re
import sys
import tempfile

import pytest

hypothesis = pytest.importorskip('hypothesis')
from hypothesis import HealthCheck, settings, strategies as st  # noqa: E402
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from commerce import Commerce  # noqa: E402
from listings import NOT_FILLED, Listings, sku_text  # noqa: E402
import wms as wms_mod  # noqa: E402
from wms import Wms, WmsError  # noqa: E402

ITEMS = 4
DAYS = ['2025-08-01', '2025-08-02']
CENTS = [3999, 4000]


class SellSkus(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        # A clock that moves each call: real saves are seconds apart, a test's all land in one second.
        self.tick = 0
        self.real_now = wms_mod.now_utc
        wms_mod.now_utc = self.clock
        self.dir = tempfile.TemporaryDirectory()
        self.w = Wms(os.path.join(self.dir.name, 'w.db'))
        self.li = Listings(self.w, Commerce(self.w))
        self.box = {'box_id': self.w.create_boxes(1, 1)[0]['id']}
        self.items = [self.w.save_product({'sku': f'I{i}', 'title': f'Item {i}', 'identifiers': [
            {'kind': 'ASIN', 'value': f'B0ITEM000{i}'}]})['id'] for i in range(ITEMS)]
        self.shown = {}    # item -> SKU shown after the last step
        self.saved = {}    # (item, base) -> SKU the first copy saved
        self.n_po = 0
        self.erased = set()   # ids of items erased so far

    def clock(self):
        self.tick += 1
        return f'2026-11-24T{self.tick // 3600 % 24:02d}:{self.tick // 60 % 60:02d}:{self.tick % 60:02d}Z'

    def teardown(self):
        wms_mod.now_utc = self.real_now
        self.dir.cleanup()

    def _skus(self):
        return {p: (self.li.sell_sku(p) or {}).get('sku') for p in self.items}

    def _step(self, touched=None):
        """After any step: every saved SKU still stands (rule 4). Previews may move."""
        self.shown = self._skus()

    def _po(self, i, day, cents, kind, imported_at=None, product=True):
        self.n_po += 1
        review = kind == 'review'
        with self.w._tx() as c:
            po = c.execute("""INSERT INTO purchase_order (source, external_id, ordered_at, imported_at, state, hidden,
                                                          account, review)
                              VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                           ('walmart.com' if review else 'amazon', f'112-{self.n_po}', day, imported_at or self.clock(),
                            'cancelled' if kind == 'cancelled' else 'ordered', 1 if kind == 'hidden' else 0,
                            'gmail' if review else '', 'New store order' if review else '')).lastrowid
            c.execute("""INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents, product_id, cancelled)
                         VALUES (?, ?, ?, 'x', 1, ?, ?, ?)""",
                      (po, f'L{self.n_po}', f'B0ITEM000{i}', cents, self.items[i] if product else None,
                       1 if kind == 'line_cancelled' else 0))

    @rule(i=st.integers(0, ITEMS - 1), day=st.sampled_from(DAYS), cents=st.sampled_from(CENTS))
    def receive(self, i, day, cents):
        self.w.receive(1, self.items[i], 1, cents, self.box, purchased_at=day)
        self._step(self.items[i])

    @rule(i=st.integers(0, ITEMS - 1), day=st.sampled_from(DAYS + ['']), cents=st.sampled_from(CENTS),
          kind=st.sampled_from(['ok', 'cancelled', 'hidden', 'line_cancelled', 'review']))
    def buy_line(self, i, day, cents, kind):
        """A buy line; '' = an order with no date (a price with no purchase date: an estimate's price)."""
        self._po(i, day, cents, kind)
        self._step(self.items[i])

    @rule(i=st.integers(0, ITEMS - 1), day=st.sampled_from(DAYS), cents=st.sampled_from(CENTS))
    def old_line_relinked(self, i, day, cents):
        """A line imported long ago with no item (oldest imported_at of all) gets linked by relink_buys (review 11-24
        round 3: it jumped ahead in the turns and moved a pasted SKU)."""
        self._po(i, day, cents, 'ok', imported_at='2000-01-01T00:00:00Z', product=False)
        with self.w._tx() as c:
            Commerce.relink_buys(c)
        self._step(self.items[i])

    @rule(n=st.integers(1, 30), kind=st.sampled_from(['cancelled', 'hidden', 'approved']))
    def order_changes_later(self, n, kind):
        """An order is cancelled, marked Not inventory or approved (a store order) on another page."""
        po = self.w._q1('SELECT id FROM purchase_order WHERE external_id = ?', (f'112-{n}',))
        if not po:
            return
        sql = {'cancelled': "state = 'cancelled'", 'hidden': 'hidden = 1', 'approved': "review = ''"}[kind]
        with self.w._tx() as c:
            c.execute(f'UPDATE purchase_order SET {sql} WHERE id = ?', (po['id'],))
        self._step()

    @rule(i=st.integers(0, ITEMS - 1))
    def undo_newest_receipt(self, i):
        r = self.w._q1("""SELECT r.action_id FROM receipt r JOIN action a ON a.id = r.action_id
                           WHERE r.product_id = ? AND a.reversed_by IS NULL AND a.reverses_action_id IS NULL
                           ORDER BY r.id DESC LIMIT 1""", (self.items[i],))
        if r:
            self.w.undo(1, r['action_id'])
            self._step(self.items[i])

    @rule(i=st.integers(0, ITEMS - 1), cents=st.sampled_from(CENTS + [None]))
    def cost_change(self, i, cents):
        """The box page's Cost each changes the newest receipt's cost, or blanks it (review 11-24 model gap): the
        newest buy's price moves, so the item's base does; a SKU saved for the old base stays as it was."""
        r = self.w._q1("""SELECT r.id FROM receipt r JOIN action a ON a.id = r.action_id
                           WHERE r.product_id = ? AND a.reversed_by IS NULL AND a.reverses_action_id IS NULL
                             AND r.purchase_line_id IS NULL ORDER BY r.id DESC LIMIT 1""", (self.items[i],))
        if r:
            self.w.set_receipt_cost([r['id']], cents, 1)
            self._step(self.items[i])

    @rule(i=st.integers(0, ITEMS - 1))
    def erase_and_remake(self, i):
        """Erase an item (only one with no history can go) and make it again: SQLite would give it the same row id
        (review 11-24: an erased item's saved SKU, links and eBay pass went to the next item with its id). A copied SKU
        counts as history, and a new item never takes an erased id."""
        p = self.items[i]
        try:
            self.w.delete_product(p, 1)
        except WmsError:
            assert self.w._q1('SELECT 1 FROM product WHERE id = ?', (p,))
            return
        assert not any(q == p for q, _ in self.saved), (p, self.saved)
        self.items[i] = self.w.save_product({'sku': f'I{i}', 'title': f'Item {i}', 'identifiers': [
            {'kind': 'ASIN', 'value': f'B0ITEM000{i}'}]})['id']
        self.erased.add(p)
        assert self.items[i] not in self.erased, (p, self.items[i])   # an erased id never comes back
        with pytest.raises(WmsError):   # an old tab's Copy SKU on the erased item saves nothing on anyone
            self.li.save_sell_sku(p, 1)
        self._step()

    @rule(i=st.integers(0, ITEMS - 1), cents=st.sampled_from(CENTS + [None]))
    def usual_cost(self, i, cents):
        """The item form's Usual unit cost (it also fills receipts saved with no cost): a price an item with no dated,
        priced buy can use (Alex 11-25)."""
        self.w.save_product({'sku': f'I{i}', 'title': f'Item {i}', 'default_cost_cents': cents, 'identifiers': [
            {'kind': 'ASIN', 'value': f'B0ITEM000{i}'}]}, self.items[i], 1)
        self._step(self.items[i])

    @rule(i=st.integers(0, ITEMS - 1))
    def copy(self, i):
        """Alex taps Copy SKU: the first copy of an item's buy saves it; copying again gives the same SKU."""
        p = self.items[i]
        before = self.li.sell_sku(p)
        s = self.li.save_sell_sku(p, 1)
        assert s['sku'] == before['sku'] and s['saved'], (before, s)   # the copy is what the screen showed
        assert self.saved.setdefault((p, s['base']), s['sku']) == s['sku']
        self._step(p)

    @rule(i=st.integers(0, ITEMS - 1), own=st.booleans())
    def listing_appears(self, i, own):
        """Amazon lists the SKU an item shows: under its own ASIN (Alex pasted it) or another of his items' ASIN."""
        sku = self.shown.get(self.items[i])
        if not sku:
            return
        with self.w._tx() as c:
            c.execute("""INSERT INTO listing (channel, sku, asin, title, status, qty, first_seen, seen_at)
                         VALUES ('amazon', ?, ?, 'x', 'Active', 1, 'x', 'x')
                         ON CONFLICT DO NOTHING""", (sku, f'B0ITEM000{i}' if own else 'B0OTHER001'))
        self._step()

    @invariant()
    def skus_hold(self):
        now = {p: self.li.sell_sku(p) for p in self.items}
        assert all(now.values()), now   # rule 6: every item has a SKU to copy (Alex 11-25)
        got = [s['sku'] for s in now.values()]
        assert len(got) == len(set(got)), now   # rule 1
        rows = {(x['product_id'], x['base']): x['sku'] for x in self.w._q('SELECT product_id, base, sku FROM sell_sku')}
        assert rows == self.saved, (rows, self.saved)   # rule 4: the saved rows stay as written
        owner = {sku: p for (p, _), sku in rows.items()}
        for p, s in now.items():
            base = s['base']
            assert s['sku'] == base or s['sku'].startswith(base + '-'), (p, s)   # rule 3
            assert owner.get(s['sku'], p) == p, (p, s, owner)   # rule 1: never another item's saved SKU
            if (p, base) in self.saved:
                assert s['saved'] and s['sku'] == self.saved[(p, base)], (p, s, self.saved)   # rule 4
            else:
                assert not s['saved'], (p, s)
                mine = f'B0ITEM000{self.items.index(p)}'
                held = {x['asin'] for x in self.w._q(
                    "SELECT asin FROM listing WHERE channel = 'amazon' AND sku = ?", (s['sku'],))}
                assert held <= {mine}, (p, s, held)   # rule 2
            paid = self.w._q("""SELECT r.unit_cost_cents AS c FROM receipt r JOIN action a ON a.id = r.action_id
                                  WHERE r.product_id = ? AND a.reversed_by IS NULL AND r.cost_missing = 0
                                    AND r.unit_cost_cents > 0 AND """ + NOT_FILLED + """
                                  UNION ALL SELECT l.unit_cost_cents FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id
                                  WHERE l.product_id = ? AND l.cancelled = 0 AND o.hidden = 0 AND o.state != 'cancelled'
                                    AND o.review = '' AND l.unit_cost_cents > 0""", (p, p))
            if not s['saved'] and s['cost_cents'] and paid:
                # rule 8: a price paid wins over the Usual unit cost, even one the usual cost filled into a receipt
                assert s['cost_cents'] in {x['c'] for x in paid}, (p, s, paid)
            if s['estimated']:
                # rule 7: no dated, priced buy, so the SKU is made from what there is and carries the item id; once
                # one is copied, the newest copied one shows (it never changes while there is still no such buy)
                assert re.fullmatch(rf'\d+\.\d+\.\d\d(-\d+\.\d\d)?-S{p}', base), (p, s)
                est = [(q, b) for (q, b) in self.saved if q == p and b.endswith(f'-S{p}')]
                if est:
                    newest = self.w._q1("""SELECT sku FROM sell_sku WHERE product_id = ? AND base LIKE ?
                                           ORDER BY id DESC LIMIT 1""", (p, f'%-S{p}'))['sku']
                    assert s['sku'] == newest, (p, s, newest)
                continue
            assert base == sku_text(s['day'], s['cost_cents']), (p, s)
            ok_line = self.w._q1("""SELECT 1 FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id
                                     WHERE l.product_id = ? AND substr(o.ordered_at, 1, 10) = ? AND l.unit_cost_cents = ?
                                       AND l.cancelled = 0 AND o.hidden = 0 AND o.state != 'cancelled'
                                       AND o.review = ''""", (p, s['day'], s['cost_cents']))
            ok_rcpt = self.w._q1("""SELECT 1 FROM receipt r JOIN action a ON a.id = r.action_id WHERE r.product_id = ?
                                     AND substr(r.purchased_at, 1, 10) = ? AND r.unit_cost_cents = ? AND a.reversed_by IS NULL
                                     AND """ + NOT_FILLED,
                                 (p, s['day'], s['cost_cents']))
            assert ok_line or ok_rcpt, (p, s)   # rule 5


SellSkus.TestCase.settings = settings(max_examples=int(os.environ.get('BOXLOFT_MODEL_EXAMPLES', '1000')),
                                      stateful_step_count=25, deadline=None,
                                      suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much])
TestSellSkus = SellSkus.TestCase
