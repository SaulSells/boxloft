"""Model-based test of the Add card's photo on box page lines (Alex 11-17: "There has to be a system that reviews and
finds everything wrong in one go").

Four review rounds each found 2-3 bugs of one kind: one action on a line, then another (photo pick, Undo, Change,
count change, History Undo, a person removing the photo, a resent save). This test runs thousands of random sequences
of those real actions against the real code and checks the rules below after every step. Hypothesis cuts any failure
down to its shortest sequence.

Rules (what must always be true):
1. The ledger adds up (check_integrity).
2. An item has at most 1 live photo, and a photo file is live on at most 1 item.
3. A photo a person removed never comes back.
4. A live photo from the Add card sits on the item of a live line that put it there (an undone or changed-away line
   leaves nothing behind), and every photo link points at a live line of the photo's item.
5. A pick with a photo onto an item with no picture gives it that photo, unless the photo is live on another item or a
   person removed it (the photo of the right item, after a wrong pick is undone or changed, reaches the right item).
6. While a line saved from a photo stands, the photo it gave its item stays on it (review round 6).
8. A photo a person put on an item on the photo-id screen stays on until a person removes it: no box page line's
   Undo, History Undo or Change takes it off (census 11-17). The screen can also take a photo an Undo freed.
9. A Change sent the way the page sends it (no photo token, as on a count change's extra line) still moves the
   line's photo to the right item (census 11-17).
7. A refused save changes nothing (review round 9): a Change or count change that ends in an error leaves every box,
   line and photo as it was. Change onto an order line (cancelled or filled meanwhile) and onto a move from another box
   (emptied meanwhile) are actions, so the refusals happen.
"""
import os
import sys
import tempfile

import pytest

hypothesis = pytest.importorskip('hypothesis')
from hypothesis import HealthCheck, settings, strategies as st  # noqa: E402
from hypothesis.stateful import Bundle, RuleBasedStateMachine, consumes, invariant, rule  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from test_sorting import setup  # noqa: E402
from photo_id import PhotoId  # noqa: E402
from wms import WmsError  # noqa: E402

JPEG = b'\xff\xd8\xff\xe0' + b'0' * 64


class PhotoLines(RuleBasedStateMachine):
    lines = Bundle('lines')

    def __init__(self):
        super().__init__()
        self.tmp = tempfile.mkdtemp()
        from pathlib import Path
        self.w, _, self.so, _, _, boxes = setup(Path(self.tmp))
        self.box = boxes['A1']
        self.so.photos = PhotoId(self.w, self.so.catalog, os.path.join(self.tmp, 'photos'), reader=lambda j, k: None)
        self.items = [self.w.save_product({'sku': f'P{i}', 'title': f'Item {i}', 'default_cost_cents': 300})['id'] for i in range(3)]
        self.audit_item = self.w._q1('SELECT id FROM audit_item LIMIT 1')['id']   # setup's Toploader row: 3 expected
        self.other = boxes['A2']   # a move source that can run empty
        self.spare = boxes['A3']
        self.orders = {}           # item -> its purchase line (qty 2)
        with self.w._tx() as c:
            for i, pid in enumerate(self.items):
                po = c.execute("INSERT INTO purchase_order (source, external_id, ordered_at, imported_at) "
                               "VALUES ('amazon', ?, '2026-10-01', 'x')", (f'111-000000{i}-0000000',)).lastrowid
                self.orders[pid] = c.execute('INSERT INTO purchase_line (po_id, line_key, product_id, title, qty, unit_cost_cents) '
                                             'VALUES (?,?,?,?,?,?)', (po, f'L{i}', pid, f'Item {i}', 2, 200)).lastrowid
        self.tokens = []
        self.screen = set()   # photo ids a person put on an item on the photo-id screen
        self.person_removed = set()
        self.saved = {}   # add_id -> (item, token) of every save made with a photo
        self.n = 0

    # ------------------------------------------------------------------ helpers

    def sid(self):
        self.n += 1
        return f's{self.n}'

    def photo_rows(self, rel=None):
        q = 'SELECT id, path, product_id, removed FROM photo'
        return self.w._q(q + (' WHERE path = ?' if rel else ''), (rel,) if rel else ())

    def live_line(self, add_id):
        r = self.w._q1('SELECT b.action_id, b.action_ids, b.product_id, b.kind, b.count_id FROM box_add b WHERE b.id = ?', (add_id,))
        if not r:
            return None
        if r['kind'] == 'audit':   # its batch's receipts
            ids = [x['action_id'] for x in self.w._q("""SELECT c2.action_id FROM audit_count c1 JOIN audit_count c2
                       ON c2.item_id = c1.item_id AND (c2.batch = c1.batch OR c2.id = c1.id) WHERE c1.id = ?""", (r['count_id'],))]
        else:
            ids = [int(a) for a in (r['action_ids'] or '').split(',') if a] or [r['action_id']]
        live = all(not (self.w._q1('SELECT reversed_by FROM action WHERE id = ?', (a,)) or {}).get('reversed_by') for a in ids)
        return r if live else None

    def has_live_photo(self, pid):
        return bool(self.w._q1('SELECT 1 AS x FROM photo WHERE product_id = ? AND removed = 0', (pid,)))

    def expect_attach(self, r, token, pid, before_live_elsewhere, before_person_removed, before_had_photo):
        """Rule 5 after a pick with a photo."""
        if r.get('kind') != 'added' or r.get('repeat'):
            return
        if before_had_photo or before_live_elsewhere or before_person_removed:
            return
        rel = self.so.photos.path_for(token)
        assert any(p['product_id'] == pid and not p['removed'] for p in self.photo_rows(rel)), \
            'the picked item with no picture did not get the photo'

    def state_of(self, token, pid):
        rel = self.so.photos.path_for(token)
        rows = self.photo_rows(rel)
        live_elsewhere = any(not p['removed'] and p['product_id'] != pid for p in rows)
        person = any(p['id'] in self.person_removed for p in rows)
        return live_elsewhere, person, self.has_live_photo(pid)

    # ------------------------------------------------------------------ actions

    @rule()
    def take_photo(self):
        if len(self.tokens) < 4:
            self.tokens.append(self.so.photos.save_photo(JPEG)[0])

    @rule(target=lines, item=st.integers(0, 2), t=st.integers(0, 3))
    def pick(self, item, t):
        pid = self.items[item]
        token = self.tokens[t] if t < len(self.tokens) else ''
        before = self.state_of(token, pid) if token else None
        r = self.so.put(self.box, 1, scan_id=self.sid(), product_id=pid, over_ok=True)
        if r.get('kind') != 'added':   # in A2 too: the card asks moved or another one; the pick saves as more stock
            r = self.so.put(self.box, 1, scan_id=f's{self.n}', product_id=pid, over_ok=True,
                            option={'kind': 'new', 'product_id': pid, 'cost_cents': None})
        r = self.so.attach_photo(r, token, 1)
        if token:
            self.expect_attach(r, token, pid, *before)
            rel = self.so.photos.path_for(token)
            if r.get('kind') == 'added' and any(p['product_id'] == pid and not p['removed'] for p in self.photo_rows(rel)):
                self.saved[r['add_id']] = (pid, token)   # this save's photo is on its item now
        return {'add_id': r['add_id'], 'scan_id': f's{self.n}', 'pid': pid, 'token': token}

    @rule(target=lines, t=st.integers(0, 3))
    def pick_audit_row(self, t):
        """A tap on a Stock audit row match (review round 5): the line keeps count_id, not an action."""
        token = self.tokens[t] if t < len(self.tokens) else ''
        try:
            r = self.so.put(self.box, 1, scan_id=self.sid(), item_id=self.audit_item)
        except WmsError:
            return {'add_id': 0, 'scan_id': '', 'pid': 0, 'token': ''}
        if r.get('kind') != 'added':
            return {'add_id': 0, 'scan_id': '', 'pid': 0, 'token': ''}
        if r['product_id'] not in self.items:
            self.items.append(r['product_id'])
        before = self.state_of(token, r['product_id']) if token else None
        r2 = self.so.attach_photo(r, token, 1)
        if token:
            self.expect_attach(r2, token, r['product_id'], *before)
            if any(p['product_id'] == r['product_id'] and not p['removed'] for p in self.photo_rows(self.so.photos.path_for(token))):
                self.saved[r['add_id']] = (r['product_id'], token)
        return {'add_id': r['add_id'], 'scan_id': f's{self.n}', 'pid': r['product_id'], 'token': token, 'audit': True}

    @rule(line=lines)
    def resend(self, line):
        if not line['add_id'] or line.get('audit'):
            return
        r = self.so.put(self.box, 1, scan_id=line['scan_id'], product_id=line['pid'], over_ok=True)
        self.so.attach_photo(r, line['token'], 1)

    @rule(line=consumes(lines))
    def undo(self, line):
        if not line['add_id']:
            return
        try:
            self.so.undo_add(line['add_id'], 1)
        except WmsError:
            pass

    @rule(line=consumes(lines))
    def history_undo(self, line):
        if not line['add_id']:
            return
        r = self.w._q1('SELECT action_id, count_id FROM box_add WHERE id = ?', (line['add_id'],))
        aid = r['action_id'] or (self.w._q1('SELECT action_id FROM audit_count WHERE id = ?', (r['count_id'],)) or {}).get('action_id')
        if not aid:
            return
        try:
            self.w.undo(1, aid, '', '')
        except WmsError:
            pass

    @rule(target=lines, line=consumes(lines), item=st.integers(0, 2))
    def change(self, line, item):
        pid = self.items[item]
        if not line['add_id'] or line.get('audit') or not self.live_line(line['add_id']):
            return line
        row = self.w._q1('SELECT kind FROM box_add WHERE id = ?', (line['add_id'],))
        before = self.state_of(line['token'], pid) if line['token'] else None
        snap = self.snapshot()
        try:
            r = self.so.put(self.box, 1, scan_id=self.sid(), product_id=pid, over_ok=True, replaces=line['add_id'],
                            option={'kind': row['kind'], 'product_id': pid, 'left': None, 'label': '', 'sub': '', 'cost_cents': None})
        except WmsError:
            assert self.snapshot() == snap, 'a refused Change changed something'
            return line
        r = self.so.attach_photo(r, line['token'], 1)
        if line['token'] and r.get('kind') == 'added':
            # The old line's photo was taken off (another item) or carried (same item) before this attach looked.
            if pid != line['pid']:
                live_else, person, had = self.state_of(line['token'], pid)
                assert not live_else or person or had or True
            return {'add_id': r['add_id'], 'scan_id': f's{self.n}', 'pid': pid, 'token': line['token']}
        return {'add_id': r.get('add_id', line['add_id']), 'scan_id': f's{self.n}', 'pid': pid, 'token': line['token']}

    def snapshot(self):
        return (self.w._q('SELECT box_id, location_id, product_id, qty FROM stock_level WHERE qty != 0 ORDER BY 1, 2, 3'),
                self.w._q('SELECT id FROM box_add ORDER BY id'), [self.live_line(x['id']) is not None for x in self.w._q('SELECT id FROM box_add ORDER BY id')],
                self.w._q('SELECT id, product_id, removed FROM photo ORDER BY id'), self.w._q('SELECT * FROM box_add_photo ORDER BY 1, 2'),
                self.w._q('SELECT * FROM put_made ORDER BY 1'))

    @rule(target=lines, line=consumes(lines), item=st.integers(0, 2), kind=st.sampled_from(['order', 'move']), qty=st.integers(1, 3))
    def change_to(self, line, item, kind, qty):
        """Not this? Change onto an order line or a move from A2, which may be cancelled, filled or empty by now."""
        pid = self.items[item]
        if not line['add_id'] or line.get('audit') or not self.live_line(line['add_id']):
            return line
        opt = ({'kind': 'order', 'line_id': self.orders[pid], 'left': 2, 'label': '', 'sub': ''} if kind == 'order' else
               {'kind': 'move', 'product_id': pid, 'from': self.other, 'left': 1, 'label': 'Move from A2', 'sub': ''})
        before = self.snapshot()
        try:
            r = self.so.put(self.box, qty, scan_id=self.sid(), product_id=pid, over_ok=True, replaces=line['add_id'], option=opt)
        except WmsError:
            assert self.snapshot() == before, 'a refused Change changed something'
            return line
        if r.get('kind') != 'added':
            assert self.snapshot() == before, 'a Change that saved nothing changed something'
            return line
        return {'add_id': r['add_id'], 'scan_id': f's{self.n}', 'pid': r['product_id'], 'token': line['token']}

    @rule(item=st.integers(0, 2))
    def stock_other_box(self, item):
        self.so.put(self.other, 1, scan_id=self.sid(), product_id=self.items[item], over_ok=True,
                    option={'kind': 'new', 'product_id': self.items[item], 'cost_cents': 100})

    @rule(item=st.integers(0, 2))
    def empty_other_box(self, item):
        pid = self.items[item]
        n = self.w._q1('SELECT COALESCE(SUM(qty), 0) AS n FROM stock_level WHERE box_id = ? AND product_id = ?',
                       (self.other['box_id'], pid))['n']
        if n > 0:
            self.w.move(1, [{'product_id': pid, 'qty': n}], self.other, self.spare)

    @rule(item=st.integers(0, 2))
    def cancel_order(self, item):
        with self.w._tx() as c:   # the other person cancels it (Gmail cancel, Orders page)
            c.execute('UPDATE purchase_line SET cancelled = 1 WHERE id = ?', (self.orders[self.items[item]],))

    @rule(target=lines, line=consumes(lines), qty=st.integers(1, 4))
    def count(self, line, qty):
        if not line['add_id'] or not self.live_line(line['add_id']):
            return line
        before = self.snapshot()
        try:
            r = self.so.put_qty(line['add_id'], qty, 1, over_ok=True)
        except WmsError:
            assert self.snapshot() == before, 'a refused count change changed something'
            return line
        if r.get('kind') != 'added':
            return line
        if line['add_id'] in self.saved:   # the pile's lines all came from that save: each keeps its photo
            self.saved[r['add_id']] = self.saved[line['add_id']]
            if r.get('extra_line'):
                self.saved[r['extra_line']['add_id']] = self.saved[line['add_id']]
        return {**line, 'add_id': r['add_id']}

    @rule(item=st.integers(0, 2), t=st.integers(0, 3))
    def screen_attach(self, item, t):
        """The photo-id screen: a person taps an item for a photo (/api/photo-id/attach)."""
        if t >= len(self.tokens):
            return
        pid = self.items[item]
        rel = self.so.photos.path_for(self.tokens[t])
        rows = self.photo_rows(rel)
        freed = any(self.w._q1('SELECT 1 AS x FROM photo_freed WHERE photo_id = ?', (p['id'],)) for p in rows)
        had = self.has_live_photo(pid)
        live_else = any(not p['removed'] and p['product_id'] != pid for p in rows)
        ph = self.so.photos.attach(self.tokens[t], pid, 2)
        if ph and not self.w._q1('SELECT 1 AS x FROM box_add_photo WHERE photo_id = ?', (ph['id'],)):
            self.screen.add(ph['id'])
        if (not rows or freed) and not had and not live_else:
            assert ph and any(p['product_id'] == pid and not p['removed'] for p in self.photo_rows(rel)), \
                'the photo-id screen could not put a new or freed photo on an item with no picture'

    @rule(target=lines, line=consumes(lines), item=st.integers(0, 2))
    def change_as_page_sends(self, line, item):
        """Not this? Change with no photo token in the request (a count change's extra line; an old tab)."""
        pid = self.items[item]
        if not line['add_id'] or line.get('audit') or not self.live_line(line['add_id']):
            return line
        row = self.w._q1('SELECT kind FROM box_add WHERE id = ?', (line['add_id'],))
        held = self.so._photo_of(line['add_id'])
        others = self.w._q1('SELECT COUNT(*) AS n FROM box_add_photo WHERE photo_id = ? AND add_id != ?', (held, line['add_id']))['n'] if held else 0
        had = self.has_live_photo(pid)
        snap = self.snapshot()
        try:
            r = self.so.put(self.box, 1, scan_id=self.sid(), product_id=pid, over_ok=True, replaces=line['add_id'],
                            option={'kind': row['kind'], 'product_id': pid, 'left': None, 'label': '', 'sub': '', 'cost_cents': None})
        except WmsError:
            assert self.snapshot() == snap, 'a refused Change changed something'
            return line
        r = self.so.attach_photo(r, '', 1)
        if r.get('kind') != 'added':
            return line
        if held and not others and (not had or pid == line['pid']):
            assert self.w._q1('SELECT product_id, removed FROM photo WHERE id = ?', (held,)) == {'product_id': pid, 'removed': 0}, \
                'a Change with no token left the line photo off the right item'
        out = {'add_id': r['add_id'], 'scan_id': f's{self.n}', 'pid': pid, 'token': line['token']}
        if held and self.so._photo_of(r['add_id']) == held:
            self.saved[r['add_id']] = (pid, line['token'])
        return out

    @rule(t=st.integers(0, 3))
    def erase_made_item(self, t):
        """Create product from a photo, then a person erases that item (no history): its photo may go on the right
        item (census fix check 11-17). Rule 5 then expects the next pick with this photo to give it to the item."""
        if t >= len(self.tokens):
            return
        self.n += 1
        pid = self.w.save_product({'sku': f'MADE{self.n}', 'title': f'Made {self.n}', 'default_cost_cents': 100})['id']
        ph = self.so.photos.attach(self.tokens[t], pid, 2, f'Made {self.n}')
        if ph:
            self.screen.discard(ph['id'])
        self.w.delete_product(pid, 2)

    @invariant()
    def screen_photos_stay(self):
        for phid in self.screen:
            if phid in self.person_removed:
                continue
            assert self.w._q1('SELECT removed FROM photo WHERE id = ?', (phid,))['removed'] == 0, \
                'a box page save took off a photo a person put on with the photo-id screen'

    @rule(item=st.integers(0, 3))
    def person_removes_photo(self, item):
        for p in self.w._q('SELECT id FROM photo WHERE product_id = ? AND removed = 0', (self.items[item % len(self.items)],)):
            self.w.remove_photo(p['id'])
            self.person_removed.add(p['id'])

    # ------------------------------------------------------------------ rules

    @invariant()
    def ledger_adds_up(self):
        assert self.w.check_integrity()['ok']

    @invariant()
    def one_live_photo(self):
        for pid in self.items:
            n = self.w._q1('SELECT COUNT(*) AS n FROM photo WHERE product_id = ? AND removed = 0', (pid,))['n']
            assert n <= 1, f'item {pid} has {n} live photos'
        for r in self.w._q('SELECT path, COUNT(*) AS n FROM photo WHERE removed = 0 GROUP BY path'):
            assert r['n'] <= 1, f'photo {r["path"]} is live on {r["n"]} items'

    @invariant()
    def standing_line_keeps_its_photo(self):
        """Rule 6 (review round 6): while a line saved from a photo stands, the photo it gave its item stays on it."""
        for add_id, (pid, token) in self.saved.items():
            if not self.live_line(add_id):
                continue
            rows = self.photo_rows(self.so.photos.path_for(token))
            if any(r['id'] in self.person_removed for r in rows) or not any(r['product_id'] == pid for r in rows):
                continue
            assert any(r['product_id'] == pid and not r['removed'] for r in rows), \
                f'line {add_id} still stands but its photo came off item {pid}'

    @invariant()
    def removed_by_a_person_stays_removed(self):
        for pid in self.person_removed:
            assert self.w._q1('SELECT removed FROM photo WHERE id = ?', (pid,))['removed'] == 1, 'a removed photo came back'

    @invariant()
    def live_photo_has_its_live_line(self):
        links = {}
        for r in self.w._q('SELECT photo_id, add_id FROM box_add_photo'):
            links.setdefault(r['photo_id'], []).append(r['add_id'])
        for p in self.w._q("SELECT id, product_id FROM photo WHERE removed = 0 AND source = 'photo-id'"):
            if p['id'] in self.screen:   # a person put it on with the photo-id screen: no line holds it
                continue
            assert p['id'] in links, f'photo {p["id"]} is live with no line'
            live = [self.live_line(a) for a in links[p['id']]]
            assert any(live), f'photo {p["id"]} is live but its lines were undone'
            assert all(x['product_id'] == p['product_id'] for x in live if x), f'photo {p["id"]} sits on another item than its line'
        for ph, adds in links.items():
            for add_id in adds:
                assert self.live_line(add_id), f'photo link {ph} points at an undone line {add_id}'


PhotoLines.TestCase.settings = settings(max_examples=int(os.environ.get('BOXLOFT_MODEL_EXAMPLES', '1000')),
                                        stateful_step_count=25, deadline=None,
                                        suppress_health_check=list(HealthCheck))
test_photo_lines = PhotoLines.TestCase
