"""Buyer returns: scan the return label on a box a buyer sent back, see what is inside and why (Alex 11-16).

A marketplace return comes back on a prepaid label the marketplace made. Amazon's returns report
(GET_FLAT_FILE_RETURNS_DATA_BY_RETURN_DATE, one row per returned order item) holds that label's tracking ID with the
order, ASIN, SKU, quantity and the buyer's reason code. Checked on live 11-16: 24 of 24 returns in 90 days had a
tracking ID (18 UPS 1Z, 6 USPS 9322...), all on Amazon's prepaid label.

- refresh_amazon() reads the last 60 days (30 per report) and upserts buyer_return, keyed on (channel, rma, order
  item). The report is the truth for status and refund; what we took in is ours (buyer_return_in).
- A label scan (labels.Labels.match) asks match() first: a return's tracking ID gives the kind 'label_return' card.
- take() puts the units back: a CUSTOMER -> box move (restock), or CUSTOMER -> DAMAGED. One action, so History's
  Undo takes it back, and the card counts only takes whose action was not undone.
eBay and Walmart returns come next (their return APIs carry the tracking ID too); the table has a channel for them.
"""
import csv
import io
from datetime import datetime, timedelta, timezone

from wms import Conflict, WmsError, now_utc, short_title

SCHEMA = """
CREATE TABLE IF NOT EXISTS buyer_return (
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL,
    rma_id TEXT NOT NULL,
    order_item_id TEXT NOT NULL DEFAULT '',
    order_id TEXT NOT NULL,
    ordered_on TEXT,
    requested_on TEXT,
    status TEXT NOT NULL DEFAULT '',
    asin TEXT NOT NULL DEFAULT '',
    sku TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    qty INTEGER NOT NULL,
    reason_code TEXT NOT NULL DEFAULT '',
    resolution TEXT NOT NULL DEFAULT '',
    carrier TEXT NOT NULL DEFAULT '',
    tracking_id TEXT NOT NULL DEFAULT '',
    delivered_on TEXT,
    refunded_cents INTEGER,
    first_seen_at TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    UNIQUE (channel, rma_id, order_item_id)
);
CREATE INDEX IF NOT EXISTS buyer_return_track ON buyer_return(tracking_id);
CREATE TABLE IF NOT EXISTS buyer_return_in (      -- append-only: units we took back in, one row per save
    id INTEGER PRIMARY KEY,
    return_id INTEGER NOT NULL REFERENCES buyer_return(id),
    action_id INTEGER NOT NULL REFERENCES action(id),
    qty INTEGER NOT NULL CHECK (qty > 0),
    condition TEXT NOT NULL CHECK (condition IN ('good', 'damaged')),
    at TEXT NOT NULL,
    user_id INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS buyer_return_in_no_update BEFORE UPDATE ON buyer_return_in
BEGIN SELECT RAISE(ABORT, 'buyer_return_in is append-only'); END;
CREATE TRIGGER IF NOT EXISTS buyer_return_in_no_delete BEFORE DELETE ON buyer_return_in
BEGIN SELECT RAISE(ABORT, 'buyer_return_in is append-only'); END;
"""

# Amazon's reason codes, in the words the buyer picked on Amazon's return page. Live 11-16 had the first 9.
REASONS = {
    'AMZ-PG-BAD-DESC': 'Not as described on the website',
    'CR-UNWANTED_ITEM': 'No longer needed',
    'CR-DAMAGED_BY_FC': 'Item damaged, shipping box OK',
    'CR-DAMAGED_BY_CARRIER': 'Item and shipping box both damaged',
    'POOR_FIT': 'Does not fit',
    'CR-ORDERED_WRONG_ITEM': 'Bought by mistake',
    'CR-NOT_COMPATIBLE': 'Not compatible with what the buyer has',
    'CR-MISSING_PARTS': 'Missing parts or accessories',
    'CR-DEFECTIVE': 'Defective, does not work',
    'CR-SWITCHEROO': 'Wrong item was sent',
    'CR-QUALITY_UNACCEPTABLE': 'Quality not good enough',
    'CR-FOUND_BETTER_PRICE': 'Found a better price',
    'CR-MISSED_ESTIMATED_DELIVERY': 'Arrived too late',
    'CR-EXTRA_ITEM': 'Extra item the buyer did not order',
    'CR-UNAUTHORIZED_PURCHASE': 'Not bought by the account holder',
    'CR-NO_REASON_GIVEN': 'No reason given',
    'AMZ-PG-APP-TOO-LARGE': 'Too large',
    'AMZ-PG-APP-TOO-SMALL': 'Too small',
}
RESOLUTIONS = {'AutomatedRefund': 'Refunded by Amazon', 'StandardRefund': 'Refund when it arrives',
               'RefundAtFirstScan': 'Refunded at the first carrier scan', 'Replacement': 'Buyer gets a replacement'}
CHANNELS = {'amazon': 'Amazon', 'ebay': 'eBay', 'walmart': 'Walmart'}


def reason_text(code: str) -> str:
    return REASONS.get(code or '', (code or '').replace('CR-', '').replace('_', ' ').capitalize() or 'No reason given')


def _day(v: str):
    """'2026-11-03T08:12:44+00:00' / '11/03/2026' / '' -> '2026-11-03' or None."""
    v = (v or '').strip()
    if not v:
        return None
    for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%d-%b-%Y'):
        try:
            return datetime.strptime(v[:11].strip() if fmt == '%d-%b-%Y' else v[:10], fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue
    return None


def parse_amazon(text: str) -> list:
    """The report's tab-separated rows -> buyer_return dicts (channel amazon)."""
    out = []
    for r in csv.DictReader(io.StringIO((text or '').lstrip('﻿')), delimiter='\t'):
        g = lambda k: (r.get(k) or '').strip()   # noqa: E731
        if not g('Order ID') or not g('Amazon RMA ID'):
            continue
        try:
            qty = max(1, int(g('Return quantity') or 1))
        except ValueError:
            qty = 1
        refund = g('Refunded Amount')
        out.append({'channel': 'amazon', 'rma_id': g('Amazon RMA ID'), 'order_item_id': g('Order Item ID'),
                    'order_id': g('Order ID'), 'ordered_on': _day(g('Order date')), 'requested_on': _day(g('Return request date')),
                    'status': g('Return request status'), 'asin': g('ASIN').upper(), 'sku': g('Merchant SKU'),
                    'title': g('Item Name'), 'qty': qty, 'reason_code': g('Return Reason'), 'resolution': g('Resolution'),
                    'carrier': g('Return carrier'), 'tracking_id': ''.join(ch for ch in g('Tracking ID') if ch.isalnum()).upper(),
                    'delivered_on': _day(g('Return delivery date')),
                    'refunded_cents': int(round(float(refund) * 100)) if refund.replace('.', '', 1).isdigit() else None})
    return out


class BuyerReturns:
    names = None   # () -> {user_id: name}, set by the server (users live in auth.db): a 409 names who took it in

    def __init__(self, wms, connections=None):
        self.wms = wms
        self.connections = connections
        wms._conn().executescript(SCHEMA)

    # ---------------------------------------------------------------- reading the marketplace

    def save(self, rows: list) -> dict:
        new = 0
        now = now_utc()
        with self.wms._tx() as c:
            for r in rows:
                new += not c.execute('SELECT 1 FROM buyer_return WHERE channel = ? AND rma_id = ? AND order_item_id = ?',
                                     (r['channel'], r['rma_id'], r['order_item_id'])).fetchone()
                c.execute("""INSERT INTO buyer_return (channel, rma_id, order_item_id, order_id, ordered_on, requested_on, status,
                                       asin, sku, title, qty, reason_code, resolution, carrier, tracking_id, delivered_on,
                                       refunded_cents, first_seen_at, seen_at)
                                   VALUES (:channel, :rma_id, :order_item_id, :order_id, :ordered_on, :requested_on, :status, :asin,
                                       :sku, :title, :qty, :reason_code, :resolution, :carrier, :tracking_id, :delivered_on,
                                       :refunded_cents, :now, :now)
                                   ON CONFLICT(channel, rma_id, order_item_id) DO UPDATE SET status = excluded.status,
                                       qty = excluded.qty, reason_code = excluded.reason_code, resolution = excluded.resolution,
                                       carrier = excluded.carrier, tracking_id = excluded.tracking_id,
                                       delivered_on = excluded.delivered_on, refunded_cents = excluded.refunded_cents,
                                       seen_at = excluded.seen_at""", {**r, 'now': now})
        return {'rows': len(rows), 'new': new}

    def due(self, minutes: int = 60) -> bool:
        st = self.wms._q1("SELECT last_run_at FROM sync_state WHERE channel = 'amazon_returns'")
        if not st or not st['last_run_at']:
            return True
        last = datetime.fromisoformat(st['last_run_at'].replace('Z', '+00:00'))
        return datetime.now(timezone.utc) - last >= timedelta(minutes=minutes)

    def refresh_amazon(self, days: int = 60) -> dict:
        """Amazon's returns report for the last `days` (30 per request, asked together). Never under op_lock."""
        from sales_sync import sp_reports
        end = datetime.now(timezone.utc) - timedelta(minutes=5)
        specs, k = [], 0
        while k < days:
            e = end - timedelta(days=k)
            s = e - timedelta(days=min(30, days - k))
            specs.append({'reportType': 'GET_FLAT_FILE_RETURNS_DATA_BY_RETURN_DATE',
                          'dataStartTime': s.strftime('%Y-%m-%dT%H:%M:%SZ'), 'dataEndTime': e.strftime('%Y-%m-%dT%H:%M:%SZ')})
            k += 30
        err, res = None, {}
        try:
            texts = sp_reports(self.connections, specs, {}, None, 'returns report')
            res = self.save([r for t in texts for r in parse_amazon(t)])
        except Exception as e:   # noqa: BLE001 - kept on sync_state, raised again for the log
            err = str(e)[:300]
            raise
        finally:
            import json
            with self.wms._tx() as c:
                c.execute("""INSERT INTO sync_state (channel, cursor, last_run_at, last_ok_at, last_error, last_result)
                             VALUES ('amazon_returns', NULL, ?, ?, ?, ?)
                             ON CONFLICT(channel) DO UPDATE SET last_run_at = excluded.last_run_at,
                                 last_ok_at = COALESCE(excluded.last_ok_at, sync_state.last_ok_at),
                                 last_error = excluded.last_error, last_result = excluded.last_result""",
                          (now_utc(), None if err else now_utc(), err, json.dumps(res)))
        return res

    # ---------------------------------------------------------------- a label scan

    def ids_in(self, c: str) -> list:
        """Return tracking IDs the cleaned scan text contains, longest first."""
        if len(c) < 8:
            return []
        rows = self.wms._q("""SELECT DISTINCT tracking_id AS t FROM buyer_return
                               WHERE LENGTH(tracking_id) >= 8 AND INSTR(?, tracking_id) > 0""", (c,))
        return sorted((r['t'] for r in rows), key=len, reverse=True)

    def _product(self, r: dict):
        """The item: the sale line's product, else a product with this SKU, else one holding the ASIN."""
        p = self.wms._q1("""SELECT l.product_id AS id FROM sales_line l JOIN sales_order o ON o.id = l.order_id
                             WHERE o.channel = ? AND o.external_id = ? AND l.product_id IS NOT NULL
                               AND (l.asin = ? OR l.sku = ?) ORDER BY l.id LIMIT 1""",
                          (r['channel'], r['order_id'], r['asin'], r['sku']))
        if not p and r['sku']:
            p = self.wms._q1('SELECT id FROM product WHERE sku = ? AND archived = 0', (r['sku'],))
        if not p and r['asin']:
            p = self.wms._q1("""SELECT p.id FROM product p JOIN product_identifier i ON i.product_id = p.id
                                 WHERE p.archived = 0 AND i.kind = 'ASIN' AND UPPER(i.value) = ? ORDER BY p.id LIMIT 1""", (r['asin'],))
            p = p or self.wms._q1('SELECT id FROM product WHERE UPPER(sku) = ? AND archived = 0', (r['asin'],))
        return p['id'] if p else None

    def _shipped_from(self, r: dict, product_id):
        """The box or shelf the sale left from (the ledger's move to CUSTOMER on the sale's shipped action)."""
        if not product_id:
            return None
        m = self.wms._q1("""SELECT m.from_box_id AS box_id, m.from_location_id AS location_id, a.at
                              FROM sales_line l JOIN sales_order o ON o.id = l.order_id
                              JOIN stock_move m ON m.action_id = l.shipped_action_id AND m.product_id = ?
                              JOIN action a ON a.id = m.action_id
                             WHERE o.channel = ? AND o.external_id = ? AND a.reversed_by IS NULL
                             ORDER BY m.id LIMIT 1""", (product_id, r['channel'], r['order_id']))
        if not m:
            return None
        if m['box_id']:
            b = self.wms._q1('SELECT id, name, archived FROM box WHERE id = ?', (m['box_id'],))
            if not b or b['archived']:
                return None
            return {'box_id': b['id'], 'location_id': None, 'name': b['name'] or f"Box {b['id']}", 'shipped_at': m['at']}
        loc = self.wms._q1("SELECT id, code, kind, archived FROM location WHERE id = ?", (m['location_id'],))
        if not loc or loc['kind'] == 'virtual' or loc['archived']:
            return None
        return {'box_id': None, 'location_id': loc['id'], 'name': loc['code'], 'shipped_at': m['at']}

    def _taken(self, return_id: int) -> list:
        return self.wms._q("""SELECT i.qty, i.condition, i.at, i.user_id, i.action_id FROM buyer_return_in i
                                JOIN action a ON a.id = i.action_id
                               WHERE i.return_id = ? AND a.reversed_by IS NULL ORDER BY i.id""", (return_id,))

    def card(self, tracking_id: str) -> dict:
        rows = self.wms._q('SELECT * FROM buyer_return WHERE tracking_id = ? ORDER BY id', (tracking_id.upper(),))
        items = []
        for r in rows:
            sold = self._product(r)
            pid = self.wms.merged_target(sold)   # an item merged since: the units come back as the kept one
            p = self.wms._q1("""SELECT p.title, p.sku, p.image_url,
                                       (SELECT ph.id FROM photo ph WHERE ph.product_id = p.id AND ph.removed = 0 ORDER BY ph.id LIMIT 1) AS photo_id
                                  FROM product p WHERE p.id = ?""", (pid,)) if pid else None
            taken = self._taken(r['id'])
            back = sum(t['qty'] for t in taken)
            items.append({'return_id': r['id'], 'order_id': r['order_id'], 'ordered_on': r['ordered_on'],
                          'requested_on': r['requested_on'], 'title': (p and p['title']) or r['title'], 'asin': r['asin'],
                          'sku': r['sku'], 'qty': r['qty'], 'back': back, 'left': max(0, r['qty'] - back),
                          'taken': taken, 'reason_code': r['reason_code'], 'reason': reason_text(r['reason_code']),
                          'resolution': RESOLUTIONS.get(r['resolution'], r['resolution']), 'status': r['status'],
                          'refunded_cents': r['refunded_cents'], 'product_id': pid,
                          'photo_id': p and p['photo_id'], 'image_url': p and p['image_url'],
                          'shipped_from': self._shipped_from(r, sold)})
        ch = rows[0]['channel'] if rows else 'amazon'
        return {'kind': 'label_return', 'tracking_id': tracking_id.upper(), 'carrier': rows[0]['carrier'] if rows else '',
                'channel': ch, 'channel_name': CHANNELS.get(ch, ch), 'items': items,
                'units': sum(i['qty'] for i in items), 'left': sum(i['left'] for i in items)}

    # ---------------------------------------------------------------- taking it back in

    def take(self, return_id: int, user_id: int, qty: int, condition: str, to: dict = None,
             seen_left: int = None, device: str = '') -> dict:
        """Put returned units back on a shelf (condition good, `to` = a box or shelf) or out as damaged. Called under
        op_lock. seen_left: the units the card showed as left; a different number now is a 409 naming who."""
        r = self.wms._q1('SELECT * FROM buyer_return WHERE id = ?', (int(return_id),))
        if not r:
            raise WmsError('That return is not in the app')
        if condition not in ('good', 'damaged'):
            raise WmsError('Say whether it can be sold again or is damaged')
        qty = int(qty or 0)
        if qty < 1:
            raise WmsError('How many came back?')
        taken = self._taken(r['id'])
        left = r['qty'] - sum(t['qty'] for t in taken)
        if seen_left is not None and int(seen_left) != left:
            who = (self.names() or {}).get(taken[-1]['user_id']) if taken and self.names else None
            raise Conflict(f"{who or 'Someone'} already took {r['qty'] - max(0, left)} of this return back in. {max(0, left)} left.")
        if qty > left:
            raise WmsError(f'Only {max(0, left)} of this return left to take in')
        # Review 11-17: a sale shipped before its item was merged stays on the archived source; the unit comes back
        # as the item it is now.
        pid = self.wms.merged_target(self._product(r))
        if not pid:
            raise WmsError(f"No item in the app has {r['asin'] or r['sku']} yet. Add the item first, then scan the label again.")
        with self.wms._tx() as c:
            if condition == 'good':
                if not to:
                    raise WmsError('Scan the box label it goes in')
                dst = self.wms._holder(c, to)
            else:
                dst = (0, self.wms._virtual_id(c, 'DAMAGED'))
            cust = (0, self.wms._virtual_id(c, 'CUSTOMER'))
            tracking, title = c.execute('SELECT tracking, title FROM product WHERE id = ?', (pid,)).fetchone()
            units = [0] * qty
            if tracking == 'unit':   # a one-off unit comes back as itself: the one at CUSTOMER from this order
                units = [u[0] for u in c.execute("""SELECT s.unit_id FROM stock_level s WHERE s.product_id = ? AND s.box_id = 0
                                                      AND s.location_id = ? AND s.unit_id != 0 AND s.qty > 0 ORDER BY s.unit_id""",
                                                 (pid, cust[1])).fetchall()][:qty]
                if len(units) < qty:
                    raise WmsError('That one-off unit is not marked as shipped in the app')
            where = self.wms._label(c, dst) if condition == 'good' else 'damaged'
            action_id = self.wms._new_action(
                c, user_id, 'return_in',
                f"Buyer return: {qty} x {short_title(title or r['title'] or r['sku'])} {'back in ' + where if condition == 'good' else 'damaged'}",
                f"{CHANNELS.get(r['channel'], r['channel'])} order {r['order_id']}, {reason_text(r['reason_code'])}", device)
            for u in units:
                self.wms._move(c, action_id, pid, u, 1 if u else qty, cust, dst)
            c.execute('INSERT INTO buyer_return_in (return_id, action_id, qty, condition, at, user_id) VALUES (?,?,?,?,?,?)',
                      (r['id'], action_id, qty, condition, now_utc(), user_id))
        return {'action_id': action_id, **self.card(r['tracking_id'])}
