"""Merge items: "this item is the same as that one" (Alex 11-17).

Why: a barcode scan on the box page made item 620 (Gundam Full Armor, ASIN B0O7GVQGT5 from Keepa) while its units sat
on Stock audit row 297 (ASIN B0O22N80A0, bought on 2 Amazon orders). The 10 units went in as extra stock and the row
still expected 10.

A merge keeps one item (the target) and folds the other (the source) into it, in one save with one Undo:
- The source's barcodes and ASINs move to the target, so the next scan of that code is the target. Open sales lines,
  listings and buys that point at the source point at the target.
- Units (review 11-17): only units that came in as extra stock (a receipt with no buy line, not counted on the Stock
  audit) are re-booked: Found on the target's open Stock audit row first (its order lines, so cost and the row's
  count are right), then the target's open buy lines. Their old extra receipt leaves Avg cost (wms receipt_void).
  Units that came in on a buy line, a Stock audit row or an earlier merge's counted units were counted once already:
  they move over as they are (re-booking them counted the same units twice). When shipping makes it unclear which
  kind left, the units on hand count as the counted kind first, so nothing is ever found twice. The ledger never edits
  a move: per box the source's units go out to VENDOR and come back in as the target in the same place.
- The source is archived; its history (receipts, sales shipped as it) stays on it.
- Target = an audit row with no item yet: the source becomes the row's item (it takes the row's ASIN), and only the
  extras the row and buys can take are re-booked.
Each merge is a `product_merge` row (append-only, undone_at set once). Undo takes back every action (all or none),
then gives back what is still the merge's: a code still on the target (or on no item), sales lines not shipped since,
buy lines the target has not received on since, listings still on the target. It never takes a code off an item a
later merge or edit gave it to. Tests: test_merge.py, test_merge_review.py (the review's 8 bugs), test_merge_grid.py.
"""
import json
from datetime import datetime, timedelta

from wms import VOIDED_SQL, Conflict, WmsError, now_utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS product_merge (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    source_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    item_id INTEGER,
    action_ids TEXT NOT NULL,
    moved TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    undone_at TEXT
);
CREATE TRIGGER IF NOT EXISTS product_merge_no_delete BEFORE DELETE ON product_merge
BEGIN SELECT RAISE(ABORT, 'product_merge is append-only'); END;
CREATE TRIGGER IF NOT EXISTS product_merge_update_once BEFORE UPDATE ON product_merge
WHEN OLD.undone_at IS NOT NULL OR NEW.id IS NOT OLD.id OR NEW.source_id IS NOT OLD.source_id
  OR NEW.target_id IS NOT OLD.target_id OR NEW.action_ids IS NOT OLD.action_ids OR NEW.moved IS NOT OLD.moved
BEGIN SELECT RAISE(ABORT, 'product_merge rows only gain undone_at, once'); END;
"""

# Tables whose open rows follow the item. Shipped sales stay on the source: their stock moves are the source's.
LINKS = (('sales_line', 'shipped_action_id IS NULL'), ('listing', '1'), ('purchase_line', '1'))
# What Undo gives back: rows still on the target that the target has not used since.
GIVE_BACK = {'sales_line': 'shipped_action_id IS NULL',
             'purchase_line': """NOT EXISTS (SELECT 1 FROM receipt r JOIN action a ON a.id = r.action_id
                                  WHERE r.purchase_line_id = purchase_line.id AND r.product_id = ? AND a.reversed_by IS NULL)""",
             'listing': '1'}

BOXLOFT = 1   # owner id of Boxloft's own stock (Stock audit rows are Boxloft's buys)

# A live receipt whose units reached a shelf or box (not one booked straight to CUSTOMER).
LIVE_SHELF_RECEIPT = """a.reversed_by IS NULL AND NOT EXISTS (
    SELECT 1 FROM stock_move m JOIN location vl ON vl.id = m.to_location_id
     WHERE m.action_id = r.action_id AND m.product_id = r.product_id AND m.to_box_id = 0 AND vl.kind = 'virtual')"""
# Units counted once already: on a buy line, on a Stock audit row, or carried over as such by an earlier merge.
COUNTED = """(r.purchase_line_id IS NOT NULL OR r.merged_from IS NOT NULL
              OR r.action_id IN (SELECT action_id FROM audit_count WHERE action_id IS NOT NULL))"""


def _avg(rows):
    """Weighted cost of receipts [{n, cost, missing}], None when no unit has a cost."""
    have = [(x['n'], x['cost']) for x in rows if not x['missing'] and x['n'] > 0]
    n = sum(q for q, _ in have)
    return round(sum(q * c for q, c in have) / n) if n else None


class Merge:
    # Paused 11-17 10:50 AM ET (027d142) after a review proved 8 bugs; back on 11:05 AM. Paused again 11-17 2:45 PM ET
    # (Alex): the deploy review keeps proving merge edge cases (25 rounds, 47 fixed, tests/test_review*_2026_11_17.py).
    # Undo stays on. Turn back on (with MERGE_ON in web/src/pages/Product.tsx) once a full review passes.
    enabled = False

    def __init__(self, wms, commerce, audit):
        self.wms, self.commerce, self.audit = wms, commerce, audit
        wms._conn().executescript(SCHEMA)

    # ------------------------------------------------------------------ helpers

    def _places(self, pid: int) -> list:
        """Where the item sits on our shelves: [{box_id, location_id, unit_id, qty}] (virtual places left out)."""
        return self.wms._q("""SELECT s.box_id, s.location_id, s.unit_id, s.qty FROM stock_level s
                               LEFT JOIN location l ON l.id = s.location_id
                               WHERE s.product_id = ? AND s.qty > 0 AND (s.box_id != 0 OR l.kind != 'virtual')
                               ORDER BY s.box_id, s.location_id""", (pid,))

    def _asins(self, pid: int) -> list:
        return [r['value'].upper() for r in self.wms._q(
            "SELECT value FROM product_identifier WHERE product_id = ? AND kind = 'ASIN' ORDER BY id", (pid,))]

    def _row_for(self, asins: list, pids: tuple):
        """The first open Stock audit row (units left to find) for these ASINs whose item is one of pids. A row of
        another item that shares an ASIN is that item's: units found there would land on it."""
        asins = list(dict.fromkeys(asins))
        rows = self.audit._rows_by('asin', asins) if asins else {}
        for asin in asins:
            for r in rows.get(asin, []):
                if not r['closed'] and r['expected'] > r['accounted'] and r['product_id'] in pids:
                    return r
        # A store-email row (LINE-<id>, no ASIN) belongs to the item the audit resolves for it: its buy line's item or
        # its confirmed listing's ASIN (review 11-17: an ASIN-only lookup missed both).
        for r in sorted(self.audit._rows('i.line_id IS NOT NULL'), key=lambda x: x['id']):
            if not r['closed'] and r['expected'] > r['accounted'] and r['product_id'] in pids:
                return r
        return None

    def _target_row(self, picked, asins: list, pids: tuple):
        """The row units are found on: the row the person picked while it has units left, else the first open row of
        the target's ASINs, then the source's (review 11-17: run() read the codes in id order after they moved, so the
        source's older ASIN came first and the unit was found on another row than the confirm screen named)."""
        if picked:
            r = self.audit.item(picked['id'])
            if r and not r['closed'] and r['expected'] > r['accounted']:
                return r
        return self._row_for(asins, pids)

    def _receipts(self, pid: int) -> dict:
        """The source's units by how they came in: counted (buy line, audit, an earlier merge's counted units) and
        extra (per receipt, newest first, less units a merge took back already)."""
        rows = self.wms._q(f"""SELECT r.id, r.qty - {VOIDED_SQL} AS n, r.unit_cost_cents AS cost, r.cost_missing AS missing,
                                      {COUNTED} AS counted,
                                      (SELECT m.to_box_id FROM stock_move m WHERE m.action_id = r.action_id
                                         AND m.product_id = r.product_id ORDER BY m.id LIMIT 1) AS box_id,
                                      (SELECT m.to_location_id FROM stock_move m WHERE m.action_id = r.action_id
                                         AND m.product_id = r.product_id ORDER BY m.id LIMIT 1) AS location_id
                                 FROM receipt r JOIN action a ON a.id = r.action_id
                                WHERE r.product_id = ? AND {LIVE_SHELF_RECEIPT} ORDER BY r.id DESC""", (pid,))
        counted = [x for x in rows if x['counted']]
        extra = [x for x in rows if not x['counted'] and x['n'] > 0]
        return {'counted': sum(x['n'] for x in counted), 'extra': extra, 'extra_n': sum(x['n'] for x in extra),
                'counted_cost': _avg(counted), 'extra_cost': _avg(extra), 'all_cost': _avg(rows)}

    @staticmethod
    def _rebookable(on_hand: int, rec: dict) -> int:
        """Extras that may be re-booked: never more than the extras, and never so many that a counted unit on hand
        would be re-booked (when shipping took some of either kind, the counted kind stays first)."""
        return max(0, min(rec['extra_n'], on_hand - rec['counted']))

    def _audited(self, asins):
        """(ASIN, order id) pairs and store line ids whose Receive-page units count on a Stock audit row as received
        (audit._received_elsewhere): the merge never re-books extras there, so a row is found once, by the row step
        (review 11-17: found units on an older Walmart line, then more on the row's own Amazon line, counted the row
        past expected). Other lines of an audited ASIN (a later order) stay open to it."""
        rows = [r for rs in self.audit._rows_by('asin', sorted({a.upper() for a in asins if a})).values() for r in rs] if asins else []
        pairs = {(r['asin'].upper(), oid) for r in rows for oid in r.get('own_orders') or []}
        lines = {r['line_id'] for r in self.wms._q('SELECT line_id FROM audit_item WHERE line_id IS NOT NULL')}
        return pairs, lines

    def _free(self, lines: list) -> list:
        pairs, aud_lines = self._audited([ln['asin'] for ln in lines if ln.get('asin')])
        return [ln for ln in lines if ((ln['asin'] or '').upper(), ln['order_id']) not in pairs and ln['id'] not in aud_lines]

    def _free_lines(self, pid, asins: tuple = ()) -> list:
        """Open buy lines of pid a merge may receive extras on: none whose units count on a Stock audit row."""
        return self._free(self.commerce.open_lines_for(pid, asins))

    def _free_after_found(self, pids, asins: tuple, row, to_row: int) -> int:
        """Units the merge's buy-line step can take, as run() does it: the row-found step receives to_row on the open
        lines (the row's own first: audit.row_lines_first), then extras go only on lines the Stock audit does not count."""
        seen, lines = set(), []
        for pid in pids:
            for ln in self.commerce.open_lines_for(pid, asins):
                if ln['id'] not in seen:
                    seen.add(ln['id'])
                    lines.append(dict(ln))
        lines.sort(key=lambda ln: (ln['ordered_at'] or '', ln['id']))
        n = to_row
        for ln in self.audit.row_lines_first(lines, row) if row else lines:
            take = min(n, max(0, ln['left_to_receive']))
            ln['left_to_receive'] -= take
            n -= take
        return sum(max(0, ln['left_to_receive']) for ln in self._free(lines))

    def _resolve(self, source_id: int, target: dict):
        src = self.wms._q1('SELECT * FROM product WHERE id = ?', (int(source_id),))
        if not src:
            raise WmsError('That item is not in Boxloft')
        if src['archived']:
            raise WmsError('That item is archived already')
        row = None
        if target.get('audit_id'):
            row = self.audit.item(int(target['audit_id']))
            tgt_pid = row['product_id'] or int(source_id)
            if row['line_id'] and not row['product_id']:
                raise WmsError('This Stock audit row is a store buy with no item yet. Open the row and tap Found it.')
            if tgt_pid == int(source_id) and src['owner_id'] != BOXLOFT:
                raise WmsError("Stock audit rows are Boxloft's own buys: a client's item can't be one")
        elif target.get('product_id'):
            tgt_pid = int(target['product_id'])
        else:
            raise WmsError('Pick the item it is the same as')
        if tgt_pid != int(source_id):   # the same checks for an item picked directly or through its audit row
            t = self.wms._q1('SELECT archived, owner_id FROM product WHERE id = ?', (tgt_pid,))
            if not t:
                raise WmsError('That item is not in Boxloft')
            if t['archived']:
                raise WmsError('That item is archived: unarchive it first')
            if t['owner_id'] != src['owner_id']:
                raise WmsError('Those items belong to different owners')
        if tgt_pid == int(source_id) and not row:
            raise WmsError('That is the same item')
        return src, tgt_pid, row

    def preview(self, source_id: int, target: dict) -> dict:
        """What a merge would do, for the confirm screen (read only), by run()'s rules."""
        src, tgt_pid, row = self._resolve(source_id, target)
        same = tgt_pid == int(source_id)
        on_hand = sum(p['qty'] for p in self._places(src['id']))
        rebook = self._rebookable(on_hand, self._receipts(src['id']))
        client = src['owner_id'] != BOXLOFT
        if not same:   # the source's ASINs join the target's before run() looks for a row
            row = None if client else self._target_row(row, self._asins(tgt_pid) + self._asins(src['id']), (tgt_pid, src['id']))
        row_left = max(0, row['expected'] - row['accounted']) if row else 0
        to_row = min(rebook, row_left)
        # run() gives the source the row's ASIN first, so that ASIN's buy lines count (review 11-17).
        asins = (row['asin'],) if same and row and row.get('asin') and not row.get('line_id') else ()
        to_lines = 0 if client else min(rebook - to_row, self._free_after_found((tgt_pid, src['id']), asins, row, to_row))
        extra = 0 if same else rebook - to_row - to_lines
        return {'source': {'id': src['id'], 'title': src['title'], 'sku': src['sku'], 'on_hand': on_hand},
                'target_id': tgt_pid, 'row': {'id': row['id'], 'asin': row['asin'], 'left': row_left} if row else None,
                'to_row': to_row, 'to_lines': to_lines, 'extra': extra,
                'as_is': on_hand - to_row - to_lines - extra, 'same': same}

    # ------------------------------------------------------------------ the merge

    def run(self, source_id: int, target: dict, user_id: int, device: str = '', seen: dict = None) -> dict:
        """Call under op_lock. seen = {'on_hand': units the screen showed for the source}: refused if stock moved."""
        if not self.enabled:
            raise WmsError('Merging items is paused while a fix goes in. Nothing was saved.')
        source_id = int(source_id)
        src, tgt_pid, row = self._resolve(source_id, target)
        places = self._places(source_id)
        if any(p['unit_id'] for p in places):
            raise WmsError('This item is tracked one unit at a time: move its units by hand')
        if isinstance(seen, dict) and seen.get('on_hand') not in (None, ''):
            now = sum(p['qty'] for p in places)
            if int(seen['on_hand']) != now:
                raise Conflict(f'This item changed since your screen loaded: {now} on hand now. Nothing was saved: check and save again.')
        # Review 11-17: the row picked was found by someone else meanwhile (the screen promised its units as found).
        if target.get('audit_id') and places and row and (row['closed'] or row['expected'] <= row['accounted']):
            raise Conflict(f"Stock audit row {row['asin']} has nothing left to find now: someone found or closed it since. "
                           'Nothing was saved. If it is the same item, pick the item itself.')
        if isinstance(seen, dict) and any(k in seen for k in ('target_id', 'to_row', 'to_lines', 'extra')):
            pv = self.preview(source_id, target)
            diff = [k for k in ('target_id', 'to_row', 'to_lines', 'extra') if k in seen and seen[k] is not None
                    and int(seen[k]) != pv[k]]
            if diff:
                raise Conflict(f"This merge changed since your screen loaded: now {pv['to_row']} go on the Stock audit row, "
                               f"{pv['to_lines']} on open orders, {pv['extra']} as extra stock. Nothing was saved: check and save again.")
        same = tgt_pid == source_id
        asins = self._asins(tgt_pid) + self._asins(source_id)   # target's first, as preview() reads them
        rec = self._receipts(source_id)
        left = self._rebookable(sum(p['qty'] for p in places), rec)
        extras = [dict(x) for x in rec['extra']]
        moved = {'idents': [], 'links': {}}
        actions = []
        try:
            with self.wms._tx() as c:
                ts = now_utc()
                if row and same and not row.get('line_id'):   # the source becomes the row's item (a store row's key is no ASIN)
                    cur = c.execute("INSERT OR IGNORE INTO product_identifier (product_id, kind, value) VALUES (?, 'ASIN', ?)",
                                    (source_id, row['asin'].upper()))
                    if cur.rowcount:   # an ASIN it had already stays when the merge is undone
                        moved['idents'].append({'id': cur.lastrowid, 'kind': 'ASIN', 'value': row['asin'].upper(), 'added': True})
                if not same:
                    for i in c.execute('SELECT id, kind, value FROM product_identifier WHERE product_id = ?', (source_id,)).fetchall():
                        have = c.execute('SELECT 1 FROM product_identifier WHERE product_id = ? AND kind = ? AND value = ?',
                                         (tgt_pid, i[1], i[2])).fetchone()
                        if have:
                            moved['idents'].append({'id': i[0], 'kind': i[1], 'value': i[2], 'dropped': True})
                            c.execute('DELETE FROM product_identifier WHERE id = ?', (i[0],))
                        else:
                            moved['idents'].append({'id': i[0], 'kind': i[1], 'value': i[2]})
                            c.execute('UPDATE product_identifier SET product_id = ? WHERE id = ?', (tgt_pid, i[0]))
                    for table, cond in LINKS:
                        ids = [r[0] for r in c.execute(f'SELECT id FROM {table} WHERE product_id = ? AND {cond}', (source_id,))]
                        if ids:
                            c.execute(f"UPDATE {table} SET product_id = ? WHERE id IN ({','.join('?' * len(ids))})", (tgt_pid, *ids))
                            moved['links'][table] = ids
                    self._touch(c, (source_id, tgt_pid), user_id, ts)
            # A client's (3PL) units never count on Boxloft's Stock audit or its buy lines (review 11-17).
            client = src['owner_id'] != BOXLOFT
            if not same:
                row = None if client else self._target_row(row, asins, (tgt_pid,))
            row_left = max(0, row['expected'] - row['accounted']) if row else 0
            # Buy lines with no item yet that a receive below links to the target (wms.receive links them): Undo
            # unlinks them again.
            moved['linked'] = [x['id'] for x in self.commerce.open_lines_for(tgt_pid) if not self.wms._q1(
                'SELECT product_id FROM purchase_line WHERE id = ?', (x['id'],))['product_id']]
            name = (self.wms._q1('SELECT title FROM product WHERE id = ?', (tgt_pid,)) or {}).get('title', '')[:50]
            note = f"Same item as {name}" if not same else f"Same item as Stock audit row {row['asin']}"
            for p in places:
                holder = {'box_id': p['box_id']} if p['box_id'] else {'location_id': p['location_id']}
                q, here = p['qty'], min(p['qty'], left)
                # The extras received into this place are the units here (review 11-17: newest first anywhere voided
                # another box's receipt): they go first, then the rest newest first. Same dicts, so counts carry over.
                def elsewhere(x, p=p):
                    return x['box_id'] != p['box_id'] if p['box_id'] else bool(x['box_id']) or x['location_id'] != p['location_id']
                ex = sorted(extras, key=elsewhere)
                # 1. Extras: found on the target's Stock audit row, then received on its open buy lines.
                n_row = min(here, row_left)
                if n_row:
                    before = {x['action_id'] for x in self.wms._q('SELECT action_id FROM audit_count WHERE item_id = ?', (row['id'],))}
                    self.audit.found(row['id'], n_row, holder, user_id, device, note=note)
                    actions += [x['action_id'] for x in self.wms._q('SELECT action_id FROM audit_count WHERE item_id = ? ORDER BY id',
                                                                    (row['id'],)) if x['action_id'] not in before]
                    row_left -= n_row
                n_lines = 0
                for ln in [] if client else self.commerce.rank_for_receiving(self._free_lines(tgt_pid)):
                    take = min(here - n_row - n_lines, max(0, ln['left_to_receive']))
                    if take > 0:
                        r = self.wms.receive(user_id, tgt_pid, take, ln['unit_cost_cents'], holder, reference=ln['order_id'] or '',
                                             note=note, device=device, purchase_line_id=ln['id'])
                        actions.append(r['action_id'])
                        n_lines += take
                found = n_row + n_lines
                # 2. Into another item: extras with no row or line stay extra stock; the rest move as counted units.
                n_extra = 0 if same else here - found
                n_counted = 0 if same else q - here
                if n_extra:   # the extras after the ones just found (newest first), at their own receipts' cost
                    r = self.wms.receive(user_id, tgt_pid, n_extra, self._cost_after(ex, found, n_extra), holder,
                                         reference='merge', note=note, device=device)
                    actions.append(r['action_id'])
                if n_counted:
                    # Counted units with no cost entered stay not entered (never another receipt's cost as if typed).
                    cost = rec['counted_cost'] if rec['counted'] else rec['all_cost']
                    r = self.wms.receive(user_id, tgt_pid, n_counted, cost, holder, reference='merge',
                                         note=f'{note}: counted once already', device=device, merged_from=source_id)
                    actions.append(r['action_id'])
                left -= found if same else here
                out = found + n_extra + n_counted
                if not out:
                    continue
                # 3. The source's units leave this place; the extras just found give their old receipt's cost back.
                r = self.wms.remove(user_id, holder, [{'product_id': source_id, 'qty': out}], 'VENDOR',
                                    note=f'{note}: re-booked as that item', device=device)
                actions.append(r['action_id'])
                self._void(ex, found, r['action_id'])
                self._use(ex, n_extra)
            archived = False
            if not same:
                self.wms.archive_product(source_id, user_id, True)
                archived = True
        except Exception:
            self._take_back(user_id, actions, moved, source_id, tgt_pid, False, device)
            raise
        with self.wms._tx() as c:
            mid = c.execute("""INSERT INTO product_merge (at, user_id, source_id, target_id, item_id, action_ids, moved, archived)
                               VALUES (?,?,?,?,?,?,?,?)""", (now_utc(), user_id, source_id, tgt_pid, row['id'] if row else None,
                                                             ','.join(map(str, actions)), json.dumps(moved), int(archived))).lastrowid
        return {'merge_id': mid, 'target_id': tgt_pid, 'units': sum(p['qty'] for p in places),
                'row': self.audit.item(row['id']) if row else None, 'actions': len(actions)}

    @staticmethod
    def _cost_after(extras: list, skip: int, n: int):
        """Weighted cost of the n extras after the first `skip` (newest first): the units left over once the found ones
        are voided (review 11-17: the average of all extras, voided ones included, gave the target a wrong cost)."""
        take = []
        for x in extras:
            k = min(skip, x['n'])
            skip -= k
            m = min(n, x['n'] - k)
            if m > 0:
                take.append({'n': m, 'cost': x['cost'], 'missing': x['missing']})
                n -= m
        return _avg(take)

    @staticmethod
    def _use(extras: list, n: int):
        """n extras went over to the target as extra stock: the next place takes the ones after them."""
        for x in extras:
            k = min(n, x['n'])
            x['n'] -= k
            n -= k

    def _void(self, extras: list, n: int, action_id: int):
        """n extras were re-booked: that many units of their receipts (newest first) leave Avg cost while `action_id`
        (the move out to VENDOR) stands."""
        if n <= 0:
            return
        with self.wms._tx() as c:
            for x in extras:
                take = min(n, x['n'])
                if take > 0:
                    c.execute('INSERT INTO receipt_void (receipt_id, qty, action_id, at) VALUES (?,?,?,?)',
                              (x['id'], take, action_id, now_utc()))
                    x['n'] -= take
                    n -= take

    @staticmethod
    def _touch(c, pids, user_id: int, ts: str):
        """Both items count as edited: an Edit form loaded before a merge or its Undo would rewrite codes the merge
        moved, so its save gets the 409 (review 11-17). Timestamps are to the second: step past the old one so a form
        loaded this same second still differs."""
        for pid in pids:
            old = (c.execute('SELECT edited_at FROM product WHERE id = ?', (pid,)).fetchone() or [None])[0] or ''
            stamp = ts if ts > old else (datetime.strptime(old, '%Y-%m-%dT%H:%M:%SZ') + timedelta(seconds=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
            c.execute('UPDATE product SET updated_at = ?, edited_at = ?, updated_by = ? WHERE id = ?', (ts, stamp, user_id, pid))

    def _take_back(self, user_id: int, actions: list, moved: dict, source_id: int, tgt_pid: int, archived: bool,
                   device: str = '', keep_added: bool = False):
        """Undo: the actions (all or none), then what is still the merge's. Codes go back by kind and value, never by
        the row id saved at merge time (an edit of the item writes its codes again under new ids)."""
        if actions:
            self.wms.undo_many(user_id, actions, note='Undo: merge items', device=device, merge_ok=True)
        with self.wms._tx() as c:
            for i in moved['idents']:
                k, v = i['kind'], i['value']
                if i.get('added'):
                    if not keep_added:   # the row's ASIN stays while saves made on the row since rely on it
                        c.execute('DELETE FROM product_identifier WHERE product_id = ? AND kind = ? AND value = ?', (source_id, k, v))
                elif i.get('dropped'):   # both had it before: the target keeps its own; the source's comes back
                    # under its old id when free (which of two items an ASIN's audit row shows goes by id order)
                    if not c.execute('INSERT OR IGNORE INTO product_identifier (id, product_id, kind, value) VALUES (?,?,?,?)',
                                     (i['id'], source_id, k, v)).rowcount:
                        c.execute('INSERT OR IGNORE INTO product_identifier (product_id, kind, value) VALUES (?,?,?)', (source_id, k, v))
                elif c.execute('SELECT 1 FROM product_identifier WHERE product_id = ? AND kind = ? AND value = ?',
                               (source_id, k, v)).fetchone():
                    c.execute('DELETE FROM product_identifier WHERE product_id = ? AND kind = ? AND value = ?', (tgt_pid, k, v))
                elif c.execute('UPDATE product_identifier SET product_id = ? WHERE product_id = ? AND kind = ? AND value = ?',
                               (source_id, tgt_pid, k, v)).rowcount == 0 and not c.execute(
                        'SELECT 1 FROM product_identifier WHERE kind = ? AND value = ?', (k, v)).fetchone():
                    # Taken off the target by an edit since, and on no other item: it is the source's again.
                    c.execute('INSERT INTO product_identifier (product_id, kind, value) VALUES (?,?,?)', (source_id, k, v))
            for table, ids in moved['links'].items():
                cond = GIVE_BACK.get(table, '1')
                c.execute(f"""UPDATE {table} SET product_id = ? WHERE product_id = ? AND id IN ({','.join('?' * len(ids))})
                               AND {cond}""", (source_id, tgt_pid, *ids, *((tgt_pid,) if '?' in cond else ())))
            for ln in moved.get('linked', []):
                c.execute(f'UPDATE purchase_line SET product_id = NULL WHERE id = ? AND product_id = ? AND {GIVE_BACK["purchase_line"]}',
                          (ln, tgt_pid, tgt_pid))
            if tgt_pid != source_id:
                self._touch(c, (source_id, tgt_pid), user_id, now_utc())
            if archived:
                c.execute('UPDATE product SET archived = 0, updated_at = ? WHERE id = ?', (now_utc(), source_id))

    def of_action(self, action_id) -> int:
        """The standing merge an action belongs to, else None. History's Undo of any of its actions takes the whole merge
        back (review 11-17: undoing one action alone gave 2 real units as 4)."""
        m = self.wms._q1("""SELECT id FROM product_merge WHERE undone_at IS NULL
                              AND ',' || action_ids || ',' LIKE '%,' || ? || ',%' ORDER BY id DESC LIMIT 1""", (str(int(action_id or 0)),))
        return m['id'] if m else None

    def undo(self, merge_id: int, user_id: int, device: str = '') -> dict:
        m = self.wms._q1('SELECT * FROM product_merge WHERE id = ?', (int(merge_id),))
        if not m:
            raise WmsError('Merge not found')
        if m['undone_at']:
            raise WmsError('Already undone')
        # Merges come undone newest first: a later merge of either item stands on this one's result (review 11-17:
        # undoing the older one under it counted the same units twice).
        later = self.wms._q1("""SELECT l.id, p.title FROM product_merge l JOIN product p ON p.id = l.target_id
                                 WHERE l.id > ? AND l.undone_at IS NULL
                                   AND (l.source_id IN (?, ?) OR l.target_id IN (?, ?)) ORDER BY l.id DESC LIMIT 1""",
                              (m['id'], m['source_id'], m['target_id'], m['source_id'], m['target_id']))
        if later:
            raise WmsError(f"A later merge of this item into {(later['title'] or 'another item')[:60]} stands on this one. "
                           'Undo that merge first (History).')
        acts = [int(a) for a in m['action_ids'].split(',') if a]
        # Review 11-17: a Found it on the row since the merge booked units as this item through the ASIN the merge
        # gave it. Taking the ASIN off would leave those units on an item the row no longer knows.
        later = m['item_id'] and self.wms._q1(f"""SELECT 1 AS x FROM audit_count ac JOIN action a ON a.id = ac.action_id
                                                  WHERE ac.item_id = ? AND a.reversed_by IS NULL AND ac.at >= ?
                                                    AND ac.action_id NOT IN ({','.join('?' * len(acts)) or '0'})""",
                                              (m['item_id'], m['at'], *acts))
        self._take_back(user_id, acts, json.loads(m['moved']), m['source_id'], m['target_id'], bool(m['archived']), device,
                        keep_added=bool(later))
        with self.wms._tx() as c:
            c.execute('UPDATE product_merge SET undone_at = ? WHERE id = ?', (now_utc(), m['id']))
        return {'ok': True, 'source_id': m['source_id']}
