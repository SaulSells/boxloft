"""Returns to the seller: units bought that went back (wrong item, damaged, never arrived, changed mind).

One purchase_return row per purchase line holds how many of that line went back and where the return
stands: requested -> sent -> seller_received -> refunded, or no_refund (gone, no money back), or kept
(return cancelled, the units are ours again). Every change is a return_event row (append-only), so the
history and the analytics come from the same place.

What a return changes:
- The line's units: kept = bought - returned. Receiving, Purchases, Arriving and the stock audit use
  "left to receive" = kept - received + pulled (wms.LINE_LEFT_SQL), so a returned unit is never
  expected on the shelf.
- Stock: when the returned unit was already received into a box (started from the product page, or
  Amazon shows a return on a line that was fully received), it has to come off the shelf. "Pull" is a
  wms.remove() to VENDOR, tied to the return in return_pull. Undo the action to put it back.
- Money: expected refund = qty x the line's unit cost (tax and shipping in). Open returns show what is
  still owed and for how long.

Where returns come from:
- Amazon order pages (extension 1.4.0+): each package has its own status, so "3 delivered + 1 Return
  started" on one order becomes a return of 1 on that line. inbound.sync() calls apply_amazon().
- Amazon's data request files (Your Returns & Refunds: Returns Status.csv, Refund Details.csv):
  import_export() matches each order to its line and sets refunds and reasons.
- People: the Returns page ("Send back to seller" on a purchase line) and the audit ("Sent back").
"""
import csv
import io
import re
from collections import defaultdict
from datetime import date

from wms import LINE_LEFT_SQL, LINE_PULLED_SQL, LINE_RECEIVED_SQL, LINE_SHELF_SQL, Wms, WmsError, now_utc

STATES = {
    'requested': 'Return started',
    'sent': 'Sent back',
    'seller_received': 'Seller has it',
    'refunded': 'Refunded',
    'no_refund': 'Closed, no refund',
    'kept': 'Kept (return cancelled)',
}
OPEN = ('requested', 'sent', 'seller_received')
RANK = {'requested': 1, 'sent': 2, 'seller_received': 3, 'refunded': 4}
REASONS = ['Wrong item sent', 'Damaged or defective', 'Not as described', 'Never arrived', 'Missing parts',
           'Better price elsewhere', 'No longer needed', 'Other']

# Refund reasons in Amazon's Refund Details.csv that mean the units left (or never came).
# Price adjustments and late-shipping credits don't: the item stayed.
UNITS_GONE = re.compile(r'return|wrong item|undeliverable|unable to deliver|not received|damaged|cancel|'
                        r'not satisfactory|out of stock|defective|lost', re.I)


def amazon_return_state(text: str):
    """One package's status line on an Amazon order page -> return state, or None if it isn't a return."""
    low = re.sub(r'\s+', ' ', text or '').strip().lower()
    if not low or low.startswith('return window') or low.startswith('delivered'):
        return None
    if 'return complete' in low or 'refund has been issued' in low or 'refund issued' in low or low.startswith('refunded'):
        return 'refunded'
    if 'return received' in low:
        return 'seller_received'
    if low.startswith('return') and any(k in low for k in ('in transit', 'dropped off', 'on its way', 'shipped')):
        return 'sent'
    if low.startswith('return') or low.startswith('replacement') or low.startswith('refund'):
        return 'requested'
    return None


def _cents(v) -> int:
    s = str(v or '').replace('$', '').replace(',', '').strip()
    try:
        return int(round(float(s) * 100))
    except ValueError:
        return 0


def _day(v):
    m = re.match(r'(\d{4}-\d{2}-\d{2})', str(v or ''))
    return m.group(1) if m else None


class Returns:
    def __init__(self, wms: Wms, commerce):
        self.wms = wms
        self.commerce = commerce   # its __init__ made the tables

    # ------------------------------------------------------------------ write (all in the caller's transaction)

    def _line(self, c, line_id: int):
        row = c.execute("""SELECT l.id, l.qty, l.unit_cost_cents, l.cancelled FROM purchase_line l WHERE l.id = ?""",
                        (line_id,)).fetchone()
        if not row:
            raise WmsError('Purchase line not found')
        return row

    def _event(self, c, rid: int, source: str, user_id, state: str, qty: int, refund: int, text: str = ''):
        c.execute("""INSERT INTO return_event (return_id, at, user_id, source, state, qty, refund_cents, text)
                     VALUES (?,?,?,?,?,?,?,?)""", (rid, now_utc(), user_id, source, state, qty, refund, (text or '')[:300]))

    def _save(self, c, line_id: int, source: str, user_id=None, *, qty=None, add_qty=0, state=None, advance=False,
              reason=None, status_text=None, refund_cents=None, refund_set=False, opened_on=None, note=None, text='') -> int:
        """Create or change the line's return. qty sets, add_qty adds (both capped at the line's qty).
        advance=True: only move the state forward (a sync never undoes what a person decided), except
        that a refund always wins."""
        line = self._line(c, line_id)
        row = c.execute("""SELECT id, qty, state, reason, status_text, refund_cents, opened_on, note
                             FROM purchase_return WHERE line_id = ?""", (line_id,)).fetchone()
        today = date.today().isoformat()
        if row:
            rid, cur_qty, cur_state, cur_reason, cur_text, cur_refund, cur_opened, cur_note = row
            reopened = bool(add_qty) and cur_state == 'kept'
            # A kept (cancelled) return's units stayed with us; a new return starts from the new units (sweep B2).
            new_qty = add_qty if reopened else cur_qty + add_qty if add_qty else (qty if qty is not None else cur_qty)
            new_state = cur_state
            if state:
                if not advance:
                    new_state = state
                elif state == 'refunded' or (cur_state in RANK and RANK.get(state, 0) > RANK[cur_state]):
                    new_state = state
            if add_qty and cur_state == 'kept':
                new_state = state or 'requested'
        else:
            rid, cur_qty, cur_state, cur_reason, cur_text, cur_refund, cur_opened, cur_note = (None, 0, None, '', '', 0, None, '')
            reopened = False
            new_qty = add_qty or qty or 0
            new_state = state or 'requested'
        new_qty = max(1, min(int(new_qty), line[1]))
        vals = {
            'qty': new_qty, 'state': new_state,
            'reason': (reason if reason is not None and (reason or not cur_reason) else cur_reason)[:120],
            'status_text': (status_text if status_text is not None else cur_text)[:200],
            'refund_cents': (cur_refund if refund_cents is None else
                             int(refund_cents) if refund_set else max(cur_refund, int(refund_cents))),
            'opened_on': cur_opened or opened_on or today,
            'note': (note if note is not None else cur_note)[:500],
        }
        vals['expected_cents'] = new_qty * line[2]
        vals['closed_on'] = today if new_state in ('refunded', 'no_refund', 'kept') else None
        if rid is None:
            rid = c.execute("""INSERT INTO purchase_return (line_id, qty, state, reason, status_text, expected_cents, refund_cents,
                                  opened_on, closed_on, source, note, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (line_id, vals['qty'], vals['state'], vals['reason'], vals['status_text'], vals['expected_cents'],
                             vals['refund_cents'], vals['opened_on'], vals['closed_on'], source, vals['note'], now_utc(),
                             now_utc())).lastrowid
            self._event(c, rid, source, user_id, vals['state'], vals['qty'], vals['refund_cents'], text or vals['status_text'])
            return rid
        changed = (vals['qty'], vals['state'], vals['refund_cents']) != (cur_qty, cur_state, cur_refund) or \
            vals['status_text'] != cur_text or vals['reason'] != cur_reason or vals['note'] != cur_note
        if changed:
            c.execute("""UPDATE purchase_return SET qty=?, state=?, reason=?, status_text=?, expected_cents=?, refund_cents=?,
                         opened_on=?, closed_on=CASE WHEN ? IS NULL THEN NULL ELSE COALESCE(closed_on, ?) END, note=?, updated_at=?
                         WHERE id=?""",
                      (vals['qty'], vals['state'], vals['reason'], vals['status_text'], vals['expected_cents'],
                       vals['refund_cents'], vals['opened_on'], vals['closed_on'], vals['closed_on'], vals['note'], now_utc(), rid))
            self._event(c, rid, source, user_id, vals['state'], vals['qty'], vals['refund_cents'], text or vals['status_text'])
        if reopened:
            c.execute('UPDATE purchase_return SET shelf_qty = 0 WHERE id = ?', (rid,))
        return rid

    def _qty_up(self, c, line_id: int, qty: int) -> int:
        """Imports and syncs only raise a return's qty, never lower what a person recorded (sweep B4)."""
        cur = c.execute("SELECT qty FROM purchase_return WHERE line_id = ? AND state != 'kept'", (line_id,)).fetchone()
        return max(qty, cur[0] if cur else 0)

    def apply_amazon(self, c, line_id: int, qty: int, state: str, status_text: str):
        """From an Amazon order page: `qty` units of this line sit in packages with a return status.
        Amazon's count can raise the qty, never lower it (a person may have recorded more)."""
        cur = c.execute('SELECT qty FROM purchase_return WHERE line_id = ?', (line_id,)).fetchone()
        self._save(c, line_id, 'amazon', None, qty=max(qty, cur[0] if cur else 0), state=state, advance=True,
                   status_text=status_text)

    # ------------------------------------------------------------------ actions people take

    def create(self, line_id: int, qty: int, user_id: int, state: str = 'requested', reason: str = '', note: str = '',
               refund_cents=None, on_shelf: bool = False) -> dict:
        """Send some of a purchase line back to the seller (adds to the line's return if it has one).
        on_shelf: the units are on our shelves now (arrived damaged or empty, received, going back)."""
        qty = int(qty or 0)
        if qty < 1:
            raise WmsError('How many went back?')
        if state not in STATES or state == 'kept':
            raise WmsError('Pick where the return stands')
        with self.wms._tx() as c:
            line = self._line(c, line_id)
            cur = c.execute("SELECT qty FROM purchase_return WHERE line_id = ? AND state != 'kept'", (line_id,)).fetchone()
            room = line[1] - (cur[0] if cur else 0)
            if qty > room:
                raise WmsError(f'That order line has {line[1]} bought and {line[1] - room} already going back.')
            if on_shelf:
                got = c.execute(f'SELECT {LINE_RECEIVED_SQL} - {LINE_SHELF_SQL} FROM purchase_line l WHERE l.id = ?', (line_id,)).fetchone()[0]
                if qty > got:
                    raise WmsError(f'Only {got} from that order were received and not already going back.')
            rid = self._save(c, line_id, 'manual', user_id, add_qty=qty, state=state, reason=reason, note=note,
                             refund_cents=refund_cents)
            if on_shelf:
                c.execute('UPDATE purchase_return SET shelf_qty = MIN(qty, shelf_qty + ?) WHERE id = ?', (qty, rid))
        return self.get(rid)

    def update(self, rid: int, user_id: int, state: str = None, qty: int = None, refund_cents=None, reason: str = None,
               note: str = None) -> dict:
        if state is not None and state not in STATES:
            raise WmsError('Unknown return state')
        with self.wms._tx() as c:
            row = c.execute('SELECT line_id FROM purchase_return WHERE id = ?', (rid,)).fetchone()
            if not row:
                raise WmsError('Return not found')
            if qty is not None and int(qty) < 1:
                raise WmsError('A return needs at least 1 unit. To cancel it, mark it "Kept".')
            # A person sets the refund exactly (a typo can go down); imports and syncs only raise it.
            self._save(c, row[0], 'manual', user_id, qty=None if qty is None else int(qty), state=state, reason=reason,
                       note=note, refund_cents=None if refund_cents is None else max(0, int(refund_cents)), refund_set=True)
        return self.get(rid)

    def pull(self, rid: int, qty: int, src: dict, user_id: int, device: str = '') -> dict:
        """Take returned units off our shelves (they were received before going back): stock -> VENDOR."""
        r = self.get(rid)
        qty = int(qty or 0)
        if qty < 1:
            raise WmsError('How many are you pulling?')
        if qty > r['to_pull']:
            raise WmsError(f'Only {r["to_pull"]} of this return are on our shelves.')
        if not r['product_id']:
            raise WmsError('This order line has no product yet, so nothing of it is on the shelves.')
        a = self.wms.remove(user_id, src, [{'product_id': r['product_id'], 'qty': qty}], 'VENDOR',
                            note=f'Back to seller, order {r["order_id"]}', device=device)
        with self.wms._tx() as c:
            c.execute('INSERT INTO return_pull (return_id, action_id, qty, at, user_id) VALUES (?,?,?,?,?)',
                      (rid, a['action_id'], qty, now_utc(), user_id))
            self._event(c, rid, 'manual', user_id, r['state'], r['qty'], r['refund_cents'], f'Pulled {qty} from stock')
            if r['state'] == 'requested':   # off the shelf = on its way back
                self._save(c, r['line_id'], 'manual', user_id, state='sent', text='Sent back (pulled from stock)')
        return self.get(rid)

    # ------------------------------------------------------------------ read

    def _select(self, where: str = '1', params=()) -> list:
        rows = self.wms._q(f"""
            SELECT pr.*, l.asin, l.title, l.qty AS line_qty, l.unit_cost_cents,
                   COALESCE(l.image_url, (SELECT ai.image_url FROM audit_item ai WHERE ai.asin = l.asin)) AS image_url,
                   COALESCE(l.product_id, (SELECT pi.product_id FROM product_identifier pi JOIN product p ON p.id = pi.product_id
                                            WHERE pi.kind = 'ASIN' AND pi.value = l.asin AND p.archived = 0 LIMIT 1)) AS product_id,
                   {LINE_RECEIVED_SQL} AS received, {LINE_PULLED_SQL} AS pulled, {LINE_LEFT_SQL} AS left_to_receive,
                   o.source AS order_source, o.external_id AS order_id, o.ordered_at,
                   CAST(julianday('now') - julianday(pr.opened_on) AS INTEGER) AS age_days
              FROM purchase_return pr JOIN purchase_line l ON l.id = pr.line_id JOIN purchase_order o ON o.id = l.po_id
             WHERE {where}
             ORDER BY CASE WHEN pr.state IN ('requested', 'sent', 'seller_received') THEN 0 ELSE 1 END,
                      pr.opened_on, pr.id""", params)
        for r in rows:
            r['state_label'] = STATES.get(r['state'], r['state'])
            r['to_pull'] = max(0, r['shelf_qty'] - r['pulled'], -r['left_to_receive']) if r['state'] != 'kept' else 0
            r['owed_cents'] = max(0, r['expected_cents'] - r['refund_cents']) if r['state'] in OPEN else 0
            r['open'] = r['state'] in OPEN
        return rows

    def get(self, rid: int) -> dict:
        rows = self._select('pr.id = ?', (rid,))
        if not rows:
            raise WmsError('Return not found')
        r = rows[0]
        r['events'] = self.wms._q('SELECT at, user_id, source, state, qty, refund_cents, text FROM return_event WHERE return_id = ? ORDER BY id',
                                  (rid,))
        if r['to_pull'] and r['product_id']:
            r['in_stock'] = self.wms._q("""
                SELECT s.box_id, s.location_id, SUM(s.qty) AS qty,
                       COALESCE((SELECT l2.code FROM location l2 WHERE l2.id = COALESCE(NULLIF(s.location_id, 0),
                                 (SELECT b.location_id FROM box b WHERE b.id = s.box_id))), '') AS place
                  FROM stock_level s LEFT JOIN location lv ON lv.id = s.location_id
                 WHERE s.product_id = ? AND s.qty > 0 AND (s.box_id > 0 OR lv.kind != 'virtual')
                 GROUP BY s.box_id, s.location_id ORDER BY qty DESC""", (r['product_id'],))
        return r

    def list(self, status: str = 'open', q: str = '') -> dict:
        where, params = ['1'], []
        if status == 'open':
            where.append("pr.state IN ('requested', 'sent', 'seller_received')")
        elif status == 'pull':
            where.append(f"pr.state != 'kept' AND ({LINE_LEFT_SQL} < 0 OR pr.shelf_qty > {LINE_PULLED_SQL})")
        elif status == 'done':
            where.append("pr.state IN ('refunded', 'no_refund', 'kept')")
        if q:
            where.append('(l.title LIKE ? OR l.asin = ? OR o.external_id LIKE ?)')
            params += [f'%{q}%', q.upper(), f'%{q}%']
        rows = self._select(' AND '.join(where), params)
        every = self._select()
        s = {'open': 0, 'owed_cents': 0, 'refunded_cents': 0, 'lost_cents': 0, 'to_pull': 0, 'units': 0, 'oldest_open_days': 0,
             'by_state': {k: 0 for k in STATES}, 'by_reason': {}}
        for r in every:
            s['by_state'][r['state']] += 1
            if r['state'] == 'kept':
                continue
            s['units'] += r['qty']
            s['refunded_cents'] += r['refund_cents']
            s['to_pull'] += r['to_pull']
            reason = r['reason'] or 'No reason given'
            s['by_reason'][reason] = s['by_reason'].get(reason, 0) + r['qty']
            if r['open']:
                s['open'] += 1
                s['owed_cents'] += r['owed_cents']
                s['oldest_open_days'] = max(s['oldest_open_days'], r['age_days'] or 0)
            if r['state'] == 'no_refund':
                s['lost_cents'] += max(0, r['expected_cents'] - r['refund_cents'])
        return {'returns': rows, 'summary': s, 'states': STATES, 'reasons': REASONS}

    def for_line(self, line_id: int):
        rows = self._select('pr.line_id = ?', (line_id,))
        return rows[0] if rows else None

    # ------------------------------------------------------------------ Amazon data-request import

    def import_export(self, files: dict) -> dict:
        """Amazon's "Request your data" files: Returns Status.csv and Refund Details.csv (any names; told apart
        by their headers). Orders Boxloft doesn't know are skipped (personal buys, years before Boxloft).
        Safe to run again: quantities and refunds only go up, states only move forward."""
        refunds, returns_ = [], []
        for name, text in (files or {}).items():
            rows = list(csv.DictReader(io.StringIO((text or '').lstrip('﻿'))))
            if not rows:
                continue
            head = set(rows[0].keys())
            if {'Refund Amount', 'Order ID', 'Quantity'} <= head:
                refunds += rows
            elif {'Return Creation Date', 'Order ID', 'Return Amount'} <= head:
                returns_ += rows
        if not refunds and not returns_:
            raise WmsError("Those files aren't Amazon's Returns Status.csv or Refund Details.csv")
        lines = defaultdict(list)
        for r in self.wms._q("""SELECT o.external_id, l.id, l.qty, l.unit_cost_cents FROM purchase_line l
                                  JOIN purchase_order o ON o.id = l.po_id WHERE o.source = 'amazon' OR o.external_id LIKE '___-_______-_______'"""):
            lines[r['external_id']].append(r)

        def match(oid, unit_guess_cents=None):
            ls = lines.get(oid) or []
            if len(ls) == 1:
                return ls[0]
            if unit_guess_cents:
                hits = [x for x in ls if x['unit_cost_cents'] and
                        any(abs(unit_guess_cents - k * x['unit_cost_cents']) <= max(5, 0.03 * unit_guess_cents) for k in range(1, x['qty'] + 1))]
                if len(hits) == 1:
                    return hits[0]
            return None

        # Refund Details repeats rows (each refund date x each quantity row), so count distinct ones.
        per_order = defaultdict(lambda: {'amounts': {}, 'qtys': {}, 'reasons': defaultdict(int), 'first': None})
        for r in refunds:
            oid = (r.get('Order ID') or '').strip()
            reason = r.get('Reversal Reason') or ''
            if oid not in lines or (r.get('Payment Status') or 'Completed') != 'Completed' or not UNITS_GONE.search(reason):
                continue
            g = per_order[oid]
            g['amounts'][(r.get('Refund Date'), r.get('Refund Amount'))] = _cents(r.get('Refund Amount'))
            g['qtys'][r.get('Creation Date')] = int(float(r.get('Quantity') or 1))
            g['reasons'][reason] += 1
            d = _day(r.get('Refund Date')) or _day(r.get('Creation Date'))
            g['first'] = min(filter(None, [g['first'], d])) if d else g['first']
        ret_rows = defaultdict(list)
        for r in returns_:
            oid = (r.get('Order ID') or '').strip()
            if oid in lines:
                ret_rows[oid].append(r)

        out = {'refunds': 0, 'returns': 0, 'unmatched': [], 'refunded_cents': 0}
        with self.wms._tx() as c:
            for oid, g in per_order.items():
                cents = sum(g['amounts'].values())
                qty = sum(g['qtys'].values()) or 1
                line = match(oid, round(cents / qty) if qty else None)
                if not line:
                    out['unmatched'].append(oid)
                    continue
                reason = max(g['reasons'], key=g['reasons'].get)
                rr = ret_rows.get(oid) or []
                why = next((x.get('Return Reason') for x in rr if x.get('Return Reason') and x.get('Return Reason') != 'Unknown Reason'), '')
                self._save(c, line['id'], 'export', None, qty=self._qty_up(c, line['id'], qty), state='refunded', advance=True,
                           reason=why or reason,
                           refund_cents=cents, opened_on=g['first'], text=f'Amazon data request: refunded ${cents / 100:.2f} ({reason})')
                out['refunds'] += 1
                out['refunded_cents'] += cents
            for oid, rows in ret_rows.items():
                if oid in per_order:
                    continue   # the refund above covers it
                no_refund = [x for x in rows if (x.get('Return Amount') or '').strip().lower() in ('no refund', '')]
                if not no_refund:
                    continue
                units = len({x.get('Contract Unit ID') or x.get('Contract ID') or i for i, x in enumerate(no_refund)})
                line = match(oid)
                if not line:
                    out['unmatched'].append(oid)
                    continue
                first = no_refund[0]
                opened = _day(first.get('Return Creation Date')) or _day(first.get('Date of Return'))
                self._save(c, line['id'], 'export', None, qty=self._qty_up(c, line['id'], units), state='requested', advance=True,
                           reason=first.get('Return Reason') or '', opened_on=opened,
                           text=f'Amazon data request: return opened {opened or "?"}, no refund yet')
                out['returns'] += 1
        out['unmatched'] = sorted(set(out['unmatched']))
        return out

    def lines_for_product(self, product_id: int) -> list:
        """Order lines this product was received against (newest first): where a 'send back' can attach."""
        return self.wms._q(f"""
            SELECT l.id, l.asin, l.title, l.qty, l.unit_cost_cents, o.external_id AS order_id, o.ordered_at,
                   {LINE_RECEIVED_SQL} AS received, {LINE_PULLED_SQL} AS pulled,
                   COALESCE((SELECT pr.qty FROM purchase_return pr WHERE pr.line_id = l.id AND pr.state != 'kept'), 0) AS returned,
                   (SELECT pr.id FROM purchase_return pr WHERE pr.line_id = l.id) AS return_id
              FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id
             WHERE l.id IN (SELECT r.purchase_line_id FROM receipt r JOIN action a ON a.id = r.action_id
                             WHERE r.product_id = ? AND a.reversed_by IS NULL AND r.purchase_line_id IS NOT NULL)
             ORDER BY o.ordered_at DESC, l.id DESC""", (product_id,))
