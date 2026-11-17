"""Amazon orders arriving, fed by the Boxloft Chrome extension.

The extension reads the Your Orders pages in Alex's signed-in Chrome and posts what it saw:
order id, date, totals, Amazon's raw status line ("Arriving Wednesday", "Now expected by
November 4", "Delivered today") and the items. All interpretation happens here, so a change
in Amazon's wording is fixed on the server without reinstalling anything.

Each order becomes a purchase_order (source 'amazon') with purchase_lines, the same rows the
CSV imports make, so receiving against them works the same way. Status changes are kept in
purchase_event, and an order whose expected date changes jumps to its new day on Arriving.

Returns: an order page shows one status per package. Extension 1.4.0+ sends each item's own package
status, so "3 delivered + 1 Return started" becomes a delivered order with a return of 1 on that line
(returns.py), not a whole order marked returned. Orders with an open return stay on the extension's
re-read list until the refund comes.

Split orders: Amazon often ships one order as several packages, even several packages of the same ASIN
(2 x 6 Tamagotchis, one delivered and one still out). Each package's units and state go in
purchase_package, one row per (package, line), replaced on every detail read. The order is delivered
only when every package is; its expected date is the soonest open package's. Arriving shows one card
per package state (`portions`), so delivered units sit under "Delivered, not scanned in" while the
rest keep their own date, and the stock audit's delivered-not-scanned rows count delivered packages
only. Units no package covers (older extensions, list-page reads, store orders) follow the order.
"""

import config
import json
import re
from datetime import date, datetime, timedelta

from wms import LINE_LEFT_SQL, LINE_RECEIVED_SQL, LINE_RETURNED_SQL, Wms, WmsError, now_utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS purchase_event (
    id INTEGER PRIMARY KEY,
    po_id INTEGER NOT NULL REFERENCES purchase_order(id),
    at TEXT NOT NULL,
    state TEXT NOT NULL,
    status_text TEXT NOT NULL,
    expected_from TEXT,
    expected_to TEXT
);
CREATE INDEX IF NOT EXISTS idx_pevent_po ON purchase_event(po_id);
CREATE TABLE IF NOT EXISTS personal_asin (
    asin TEXT PRIMARY KEY,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS purchase_package (
    id INTEGER PRIMARY KEY,
    po_id INTEGER NOT NULL REFERENCES purchase_order(id),
    line_id INTEGER NOT NULL REFERENCES purchase_line(id),
    pkg INTEGER NOT NULL,              -- package number on the order page, 1 = top
    qty INTEGER NOT NULL,
    state TEXT NOT NULL,               -- ordered, shipped, delivered, cancelled, returned (units on purchase_return)
    status_text TEXT NOT NULL DEFAULT '',
    expected_from TEXT,
    expected_to TEXT,
    delivered_on TEXT,
    seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ppkg_po ON purchase_package(po_id);
-- What the package's Track page said (extension 2.1+). Keyed on Amazon's shipment id, which stays the same
-- while purchase_package is rewritten on every order-page read. Amazon's view, updated in place.
CREATE TABLE IF NOT EXISTS package_track (
    id INTEGER PRIMARY KEY,
    po_id INTEGER NOT NULL REFERENCES purchase_order(id),
    shipment_id TEXT NOT NULL,
    read_at TEXT NOT NULL,
    reads INTEGER NOT NULL DEFAULT 0,
    headline TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    carrier TEXT NOT NULL DEFAULT '',
    tracking_id TEXT,
    last_event TEXT,                   -- JSON {date, time, text, where}
    stages TEXT,                       -- JSON [{name, done}]
    state TEXT NOT NULL,               -- shipped or delivered (a Track page with a shipment id is at least shipped)
    expected_from TEXT,
    expected_to TEXT,
    delivered_on TEXT,
    photo_seen INTEGER NOT NULL DEFAULT 0,   -- the page offered a delivery photo
    photo TEXT,                              -- its copy under DATA/photos (the link expires in 3 days)
    UNIQUE (po_id, shipment_id)
);
CREATE INDEX IF NOT EXISTS idx_ppkg_line ON purchase_package(line_id);
CREATE TABLE IF NOT EXISTS sync_run (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    device TEXT NOT NULL DEFAULT '',
    orders INTEGER NOT NULL,
    changed INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
"""

MONTHS = {m: i for i, m in enumerate(['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], 1)}
WEEKDAYS = {d: i for i, d in enumerate(['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'])}
FINAL = ('delivered', 'cancelled', 'returned')
DELIVERED_DAYS = 14  # older delivered-but-not-received lines stay on Purchases for reconciling
ET = config.TZ
LIVE = ('ordered', 'shipped', 'delivered')   # package states whose units are still ours to receive


def store_url(source: str, asin: str, item_id: str = '', saved: str = '') -> str:
    """The store's own product page (Alex 11-13: "a link to the retailer's product page if we know it"). Amazon by
    ASIN, Walmart by its item number (the extension reads it off the order page), any other store by the link its
    order email gave for the item (`saved`, store_mail.resolve_link: trackers followed, tokens dropped). Never built
    from a title: a search could open the wrong item."""
    asin = (asin or '').strip().upper()
    if source == 'amazon' and re.fullmatch(r'B0[0-9A-Z]{8}|[0-9]{9}[0-9X]', asin):
        return f'https://www.amazon.com/dp/{asin}'
    if source == 'walmart.com' and re.fullmatch(r'[0-9]{4,15}', (item_id or '').strip()):
        return f'https://www.walmart.com/ip/{item_id.strip()}'
    return saved if (saved or '').startswith('https://') else ''


def today_et() -> date:
    """The warehouse's date. The server runs on UTC, which turns 'today' into tomorrow after 7 PM ET."""
    return datetime.now(ET).date()


def portions(qty: int, left: int, packages: list, order: dict) -> list:
    """Where a line's units still to scan in are: [{state, qty, expected_from, expected_to, delivered_on,
    status_text}]. `left` is wms.LINE_LEFT_SQL; `packages` the line's purchase_package rows; `order` the
    purchase_order row, which units no package covers follow. Scanned-in units are taken from delivered
    packages first (oldest delivery first), then from the open package expected soonest."""
    if left <= 0:
        return []
    covered = sum(p['qty'] for p in packages if p['state'] != 'cancelled')
    parts = [dict(p) for p in packages if p['state'] in LIVE]
    if qty - covered > 0 or not parts:
        parts.append({'state': order['state'] if order['state'] in LIVE else 'ordered', 'qty': max(0, qty - covered) or qty,
                      'expected_from': order.get('expected_from'), 'expected_to': order.get('expected_to'),
                      'delivered_on': order.get('delivered_on'), 'status_text': order.get('status_text') or '', 'pkg': 0})
    open_ = sorted((p for p in parts if p['state'] != 'delivered'), key=lambda p: p.get('expected_to') or '9999', reverse=True)
    done = sorted((p for p in parts if p['state'] == 'delivered'), key=lambda p: p.get('delivered_on') or '', reverse=True)
    out, rest = [], left
    for p in open_ + done:
        take = min(p['qty'], rest)
        rest -= take
        if take > 0:
            out.append({**p, 'qty': take})
    if rest > 0 and out:
        out[0]['qty'] += rest    # more left than the packages hold (a shelf unit pulled back): keep it visible
    merged = {}
    for p in out:
        k = _card_key(p)
        if k not in merged:
            merged[k] = {**{f: p.get(f) for f in ('state', 'expected_from', 'expected_to', 'delivered_on', 'status_text')},
                         'qty': 0, 'pkgs': []}
        merged[k]['qty'] += p['qty']
        if p.get('pkg') and p['pkg'] not in merged[k]['pkgs']:
            merged[k]['pkgs'].append(p['pkg'])   # which order-page packages this card holds (their Track reads)
    return list(merged.values())


def bucket(o: dict, today: str) -> str:
    """Which Arriving section a card goes in (web/src/lib/arriving.ts said the same; the server now decides it so
    Home and Arriving count from one place)."""
    if o['state'] == 'delivered':
        return 'delivered'
    to, frm = o.get('expected_to'), o.get('expected_from')
    if not to:
        return 'nodate'
    if to < today:
        return 'late'
    # A window that has started is "today" only once Amazon shipped it (sweep C5).
    if to == today or (o['state'] == 'shipped' and frm and frm <= today):
        return 'today'
    if (date.fromisoformat(to) - date.fromisoformat(today)).days <= 7:
        return 'week'
    return 'later'


def counts(cards: list) -> dict:
    """Flow audit 11-12 finding 2: Home said "10 delivered, not scanned in" (orders) while Arriving's heading said 12
    (package cards; a split order has a card per package). One count for both: {section: {packages, orders}}."""
    out = {}
    for c in cards:
        s = out.setdefault(c['bucket'], {'packages': 0, 'orders': set()})
        s['packages'] += 1
        s['orders'].add(c['id'])
    return {k: {'packages': v['packages'], 'orders': len(v['orders'])} for k, v in out.items()}


def _card_key(p: dict) -> tuple:
    if p['state'] == 'delivered':
        return ('delivered', p.get('delivered_on') or '')
    return (p['state'], p.get('expected_from') or '', p.get('expected_to') or '')


def _one_date(s: str, seen: date, past: bool):
    """'today', 'Wednesday', 'November 6', 'Nov 6, 2026' -> date. past=True for delivered dates."""
    s = s.strip().lower().rstrip('.')
    if s in ('today', 'now') or s.startswith('today '):
        return seen
    if s == 'tomorrow' or s.startswith('tomorrow '):
        return seen + timedelta(days=1)
    if s == 'yesterday':
        return seen - timedelta(days=1)
    for name, wd in WEEKDAYS.items():
        if s.startswith(name):
            delta = (wd - seen.weekday()) % 7
            return seen - timedelta(days=(seen.weekday() - wd) % 7) if past else seen + timedelta(days=delta)
    m = re.match(r'([a-z]{3})[a-z]*\.?\s+(\d{1,2})(?:,?\s+(\d{4}))?', s)
    if not m or m.group(1) not in MONTHS:
        return None
    month, day = MONTHS[m.group(1)], int(m.group(2))
    year = int(m.group(3)) if m.group(3) else seen.year
    try:
        d = date(year, month, day)
    except ValueError:
        return None
    if not m.group(3):
        # No year on the page: pick the year that puts the date nearest the day we read it.
        if past and d > seen + timedelta(days=1):
            d = date(year - 1, month, day)
        elif not past and d < seen - timedelta(days=60):
            d = date(year + 1, month, day)
    return d


def parse_status(text: str, seen: date) -> dict:
    """Amazon's status line -> state and expected window (ISO dates)."""
    t = re.sub(r'\s+', ' ', (text or '')).strip()
    low = t.lower()
    out = {'state': 'ordered', 'from': None, 'to': None, 'delivered_on': None}
    if not t:
        return out
    if 'cancel' in low:
        out['state'] = 'cancelled'
        return out
    if any(k in low for k in ('return complete', 'return received', 'refunded', 'refund issued', 'return started', 'replacement')):
        out['state'] = 'returned'
        return out
    if low.startswith('delivered') or ' delivered' in low[:20]:
        out['state'] = 'delivered'
        rest = re.sub(r'^.*?delivered\s*', '', low)
        d = _one_date(rest, seen, past=True) if rest else seen
        out['delivered_on'] = (d or seen).isoformat()
        return out
    if any(k in low for k in ('not yet shipped', 'preparing for shipment', 'ordered', 'payment', 'pending')):
        out['state'] = 'ordered'
    elif any(k in low for k in ('arriving', 'expected', 'out for delivery', 'shipped', 'in transit', 'on the way', 'running late', 'delayed')):
        out['state'] = 'shipped'
    # "Estimated to arrive by ..." is shown before an order ships: keep it 'ordered', but take the date.
    estimated = low.startswith('estimated to arrive')
    if 'out for delivery' in low:
        out['from'] = out['to'] = seen.isoformat()
        return out
    body = re.sub(r'\s*previously expected.*$', '', low)  # "Now arriving today Previously expected November 4"
    body = re.sub(r'^(now\s+)?(arriving|expected|expected by|arriving by|estimated to arrive|estimated delivery:?|delivery)\s*(by\s+)?', '', body)
    parts = [p for p in re.split(r'\s+-\s+|\s+to\s+', body) if p]
    dates = [_one_date(p, seen, past=False) for p in parts[:2]]
    dates = [d for d in dates if d]
    if dates:
        out['from'] = dates[0].isoformat()
        out['to'] = dates[-1].isoformat()
        if 'by ' in low and len(dates) == 1:
            out['from'] = None
        if out['state'] == 'ordered' and 'not yet shipped' not in low and not estimated:
            out['state'] = 'shipped'
    return out


def old_blank_is_delivered(st: dict, text: str, ordered_at, seen: date) -> dict:
    """Amazon shows no status at all on orders a few months old (only "Return window closed on ...").
    A blank status on an order older than 45 days means it arrived long ago: delivered, date unknown,
    so it stays out of "on the way" and out of "delivered, not scanned in"."""
    if (text or '').strip() or not ordered_at:
        return st
    try:
        placed = date.fromisoformat(str(ordered_at)[:10])
    except ValueError:
        return st
    if placed < seen - timedelta(days=45):
        return {**st, 'state': 'delivered', 'from': None, 'to': None, 'delivered_on': None}
    return st


def _is_return(text: str) -> bool:
    from returns import amazon_return_state
    return amazon_return_state(text) is not None


class Inbound:
    def __init__(self, wms: Wms, returns=None):
        self.wms = wms
        self.returns = returns   # returns.Returns, for per-package return statuses
        conn = wms._conn()
        conn.executescript(SCHEMA)
        for col, decl in (('state', "TEXT NOT NULL DEFAULT 'ordered'"), ('status_text', "TEXT NOT NULL DEFAULT ''"),
                          ('expected_from', 'TEXT'), ('expected_to', 'TEXT'), ('delivered_on', 'TEXT'),
                          ('status_changed_at', 'TEXT'), ('last_seen_at', 'TEXT'), ('total_cents', 'INTEGER'),
                          ('hidden', 'INTEGER NOT NULL DEFAULT 0'),
                          ('review', "TEXT NOT NULL DEFAULT ''"), ('mail_id', 'TEXT')):  # store-email orders (store_mail.py)
            Wms._add_column(conn, 'purchase_order', col, decl)
        Wms._add_column(conn, 'purchase_line', 'image_url', 'TEXT')
        Wms._add_column(conn, 'purchase_line', 'store_item_id', 'TEXT')   # Walmart's usItemId (store_mail fills it); Arriving links by it
        Wms._add_column(conn, 'purchase_line', 'store_url', 'TEXT')       # the item's page from its order email (store_mail)
        Wms._add_column(conn, 'purchase_package', 'shipment_id', 'TEXT')       # from its Track package link
        Wms._add_column(conn, 'purchase_package', 'package_index', 'INTEGER')
        Wms._add_column(conn, 'purchase_order', 'kind', "TEXT NOT NULL DEFAULT 'stock'")   # 'supplies': ship boxes etc., not stock

    def known(self, since_days: int = 200) -> dict:
        """What the extension needs to decide how far to read: open orders and the oldest one."""
        rows = self.wms._q(f"""SELECT external_id, state, ordered_at, last_seen_at,
                                      (SELECT COUNT(*) FROM purchase_line l WHERE l.po_id = o.id) AS lines
                                 FROM purchase_order o
                                WHERE source = 'amazon' AND (ordered_at >= date('now', '-{int(since_days)} days')
                                      OR (last_seen_at IS NOT NULL AND state NOT IN {FINAL!r}))""")
        # Sweep C3: an open order the extension already reads (a preorder placed in May, arriving 11-23) stays on the
        # re-read list at any age. Imported orders it never saw (815 on live, all before 2026-04) stay off.
        # An open return keeps the order on the re-read list, at any age, until the refund comes. So does an
        # order marked 'returned' by an older extension that has no line return yet (one more read splits it).
        returning = {r['external_id'] for r in self.wms._q("""
            SELECT DISTINCT o.external_id FROM purchase_order o JOIN purchase_line l ON l.po_id = o.id
              LEFT JOIN purchase_return pr ON pr.line_id = l.id
             WHERE o.source = 'amazon' AND (pr.state IN ('requested', 'sent', 'seller_received')
                   OR (o.state = 'returned' AND NOT EXISTS (SELECT 1 FROM purchase_return x JOIN purchase_line y ON y.id = x.line_id
                                                             WHERE y.po_id = o.id)))""")}
        have = {r['external_id'] for r in rows}
        rows += [dict(r, lines=1) for r in self.wms._q("SELECT external_id, state, ordered_at, last_seen_at FROM purchase_order WHERE source = 'amazon'")
                 if r['external_id'] in returning and r['external_id'] not in have]
        for r in rows:
            if r['external_id'] in returning:
                r['state'] = 'returning'
        open_ = [r for r in rows if r['state'] not in FINAL and r['lines']]
        return {'orders': {r['external_id']: {'state': r['state'], 'lines': r['lines']} for r in rows},
                'oldest_open': min((r['ordered_at'] for r in open_ if r['ordered_at']), default=None),
                'cutoff_days': 180}

    def sync(self, orders: list, device: str = '') -> dict:
        """Upsert orders the extension read. Lines are replaced from detail reads only."""
        if not isinstance(orders, list) or len(orders) > 500:
            raise WmsError('Send a list of up to 500 orders')
        changed = new = 0
        today = today_et()
        personal = {r['asin'] for r in self.wms._q('SELECT asin FROM personal_asin')}
        with self.wms._tx() as c:
            for o in orders:
                oid = str(o.get('order_id') or '').strip()
                if not re.fullmatch(r'\d{3}-\d{7}-\d{7}|D\d{2}-\d{7}-\d{7}', oid) or oid.startswith('000-'):
                    continue
                if not o.get('items') and not str(o.get('status_text') or '').strip() and not o.get('ordered_at'):
                    continue  # an order-number lookalike on the list page (placeholder, tracking id): its page had nothing
                seen = today
                if o.get('seen_at'):
                    try:
                        seen = datetime.fromisoformat(str(o['seen_at'])[:10]).date()
                    except ValueError:
                        pass
                ordered_at = o.get('ordered_at')
                if ordered_at and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', ordered_at):
                    d = _one_date(ordered_at, seen, past=True)
                    ordered_at = d.isoformat() if d else None
                items = o.get('items') or []
                # Each item carries its own package status (extension 1.4.0+). The order's status comes from
                # the packages that aren't returns (returns go on the lines below): delivered only when every
                # package is, otherwise the open package expected soonest.
                pk = self._package_states(items, seen)
                if pk:
                    self._merge_tracks(c, oid, pk, today)
                st = self._order_from_packages(pk) if pk else None
                if st:
                    o['status_text'] = st.pop('text')
                else:
                    keep = [p['text'] for p in pk or [] if p['state'] != 'returned']
                    if keep:
                        o['status_text'] = next((t for t in keep if not t.lower().startswith('delivered')), keep[0])
                    st = parse_status(o.get('status_text', ''), seen)
                st = old_blank_is_delivered(st, o.get('status_text', ''), ordered_at, seen)
                row = c.execute("SELECT id, state, expected_from, expected_to, status_text FROM purchase_order WHERE source='amazon' AND external_id=?",
                                (oid,)).fetchone()
                if not row:
                    po = c.execute("""INSERT INTO purchase_order (source, external_id, account, ordered_at, imported_at)
                                      VALUES ('amazon', ?, ?, ?, ?)""", (oid, o.get('account') or '', ordered_at, now_utc())).lastrowid
                    new += 1
                    prev = (None, None, None, None)
                else:
                    po, prev = row[0], row[1:]
                is_change = (st['state'], st['from'], st['to']) != (prev[0], prev[1], prev[2]) or (o.get('status_text') or '')[:200] != (prev[3] or '')
                c.execute("""UPDATE purchase_order SET state=?, status_text=?, expected_from=?, expected_to=?, delivered_on=?,
                             last_seen_at=?, ordered_at=COALESCE(ordered_at, ?), total_cents=COALESCE(?, total_cents)
                             {} WHERE id=?""".format(', status_changed_at=?' if is_change else ''),
                          (st['state'], (o.get('status_text') or '')[:200], st['from'], st['to'], st['delivered_on'], now_utc(),
                           ordered_at, o.get('total_cents'), *((now_utc(),) if is_change else ()), po))
                if is_change:
                    changed += 1
                    c.execute("""INSERT INTO purchase_event (po_id, at, state, status_text, expected_from, expected_to)
                                 VALUES (?,?,?,?,?,?)""", (po, now_utc(), st['state'], (o.get('status_text') or '')[:200], st['from'], st['to']))
                if items and o.get('from_detail'):
                    # A package Amazon cancelled inside an order that still ships: its units were never bought.
                    gone = [p for p in pk or [] if p['state'] == 'cancelled'] if st['state'] != 'cancelled' else []
                    buy = [i for i in items if not any(p['item'] is i for p in gone)]
                    keys = self._upsert_lines(c, po, buy, o.get('subtotal_cents'), o.get('total_cents'))
                    for key in {self._line_key(p['item']) for p in gone} - set(keys):
                        c.execute(f"""UPDATE purchase_line SET cancelled = 1 WHERE po_id = ? AND line_key = ?
                                      AND {LINE_RECEIVED_SQL.replace('l.id', 'purchase_line.id')} = 0""", (po, key))
                    self._save_packages(c, po, pk, seen)
                    if pk and self.returns:
                        self._apply_returns(c, items, keys)
                elif items and not c.execute('SELECT 1 FROM purchase_line WHERE po_id = ?', (po,)).fetchone():
                    # List-page read: titles and ASINs only, cost unknown until the detail read.
                    self._upsert_lines(c, po, items, None, None)
                asins = {i.get('asin') for i in items if i.get('asin')}
                if asins and asins <= personal:
                    c.execute('UPDATE purchase_order SET hidden = 1 WHERE id = ?', (po,))
            c.execute('INSERT INTO sync_run (at, device, orders, changed, note) VALUES (?,?,?,?,?)',
                      (now_utc(), device[:60], len(orders), changed, f'{new} new'))
        return {'orders': len(orders), 'new': new, 'changed': changed}

    @staticmethod
    def _package_states(items: list, seen: date):
        """Items that carry a package status -> [{item, pkg, text, state, from, to, delivered_on}], None if none do.
        Items with a blank status (Amazon shows none on old packages) get no package and follow the order."""
        out, nums = [], {}
        for it in items:
            text = re.sub(r'\s+', ' ', str(it.get('status') or '')).strip()
            if not text:
                continue
            n = it.get('package')
            if not isinstance(n, int) or n <= 0:
                # Extension 1.x sends no package number: one package per distinct status text.
                n = 1000 + nums.setdefault(text, len(nums))
            st = {'state': 'returned', 'from': None, 'to': None, 'delivered_on': None} if _is_return(text) else parse_status(text, seen)
            tr = it.get('track') if isinstance(it.get('track'), dict) else {}
            if st['state'] == 'shipped' and tr.get('item_id') and not tr.get('shipment_id'):
                # "Arriving November 30 - December 8" on a package whose Track link has no shipment id: Amazon
                # hasn't shipped it (11 of 11 such Track pages: no tracking ID, "Shipped: Incomplete"). Keep the date.
                st['state'] = 'ordered'
            elif st['state'] == 'ordered' and tr.get('shipment_id') and (st['from'] or st['to']):
                # Sweep C6: "Estimated to arrive by November 15" on a package with a shipment id is on its way (a
                # shipment id means a real package, as take_track says), so it is shipped, not ordered.
                st['state'] = 'shipped'
            out.append({'item': it, 'pkg': n, 'text': text[:200], **st,
                        'shipment_id': tr.get('shipment_id'), 'package_index': tr.get('package_index')})
        return out or None

    @staticmethod
    def _merge_tracks(c, oid: str, pk: list, today: date):
        """Packages with a Track page read take the further-along state of the order page and the Track page, so a
        package never steps back (Track pages are often fresher: "Estimated to arrive by November 14" on the order
        page, "Arriving today, out for delivery" with UPS on its Track page, 11-11)."""
        rows = {r[0]: r for r in c.execute(
            """SELECT t.shipment_id, t.state, t.expected_from, t.expected_to, t.delivered_on, t.headline, t.read_at
                 FROM package_track t JOIN purchase_order o ON o.id = t.po_id
                WHERE o.source = 'amazon' AND o.external_id = ?""", (oid,)).fetchall()}
        for p in pk:
            t = rows.get(p.get('shipment_id'))
            if not t or p['state'] not in LIVE:
                continue
            rank = {'ordered': 0, 'shipped': 1, 'delivered': 2}
            same_day = (t[6] or '')[:10] >= (today - timedelta(days=1)).isoformat()
            if rank[t[1]] > rank[p['state']] or (t[1] == p['state'] == 'shipped' and same_day and t[3]):
                p.update(state=t[1], **{'from': t[2], 'to': t[3]}, delivered_on=t[4], text=(t[5] or p['text'])[:200])

    def take_track(self, order_id: str, shipment_id: str, page: dict, photo_rel, seen: date) -> bool:
        """Save what a Track page said, then move the package (and its order) on if the page is further along.
        False when the order isn't known here."""
        with self.wms._tx() as c:
            r = c.execute("SELECT id FROM purchase_order WHERE source = 'amazon' AND external_id = ?", (order_id,)).fetchone()
            if not r:
                return False
            po = r[0]
            st = parse_status(page.get('headline') or '', seen)
            if st['state'] not in ('shipped', 'delivered'):
                st['state'] = 'shipped'    # a shipment id means a real package, whatever the headline says
            c.execute("""INSERT INTO package_track (po_id, shipment_id, read_at, reads, headline, note, carrier, tracking_id,
                                                    last_event, stages, state, expected_from, expected_to, delivered_on,
                                                    photo_seen, photo)
                         VALUES (?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT (po_id, shipment_id) DO UPDATE SET read_at = excluded.read_at, reads = reads + 1,
                             headline = excluded.headline, note = excluded.note,
                             carrier = COALESCE(NULLIF(excluded.carrier, ''), carrier),
                             tracking_id = COALESCE(excluded.tracking_id, tracking_id),
                             last_event = COALESCE(excluded.last_event, last_event), stages = excluded.stages,
                             state = excluded.state, expected_from = excluded.expected_from,
                             expected_to = excluded.expected_to, delivered_on = excluded.delivered_on,
                             photo_seen = MAX(photo_seen, excluded.photo_seen), photo = COALESCE(excluded.photo, photo)""",
                      (po, shipment_id, now_utc(), page.get('headline') or '', page.get('note') or '', page.get('carrier') or '',
                       page.get('tracking_id'), json.dumps(page['last_event']) if page.get('last_event') else None,
                       json.dumps(page.get('stages') or []), st['state'], st['from'], st['to'], st['delivered_on'],
                       1 if page.get('photo_url') else 0, photo_rel))
            self._promote(c, po, seen)
        return True

    def _promote(self, c, po: int, seen: date):
        """After a Track read: move this order's package rows on where their Track page is further along, then
        the order itself (same rules as an order-page read: delivered only when every package is)."""
        rows = [dict(zip(('id', 'pkg', 'qty', 'state', 'text', 'from', 'to', 'delivered_on', 'shipment_id'), r)) for r in c.execute(
            """SELECT id, pkg, qty, state, status_text, expected_from, expected_to, delivered_on, shipment_id
                 FROM purchase_package WHERE po_id = ? ORDER BY pkg""", (po,)).fetchall()]
        if not rows:
            return
        oid = c.execute('SELECT external_id FROM purchase_order WHERE id = ?', (po,)).fetchone()[0]
        before = [(r['state'], r['from'], r['to'], r['delivered_on'], r['text']) for r in rows]
        self._merge_tracks(c, oid, rows, today_et())
        if before == [(r['state'], r['from'], r['to'], r['delivered_on'], r['text']) for r in rows]:
            return
        for r in rows:
            c.execute("""UPDATE purchase_package SET state = ?, status_text = ?, expected_from = ?, expected_to = ?, delivered_on = ?
                         WHERE id = ?""", (r['state'], r['text'], r['from'], r['to'], r['delivered_on'], r['id']))
        st = self._order_from_packages([{**r, 'item': {'qty': r['qty']}} for r in rows])
        if not st:
            return
        prev = c.execute('SELECT state, expected_from, expected_to, status_text FROM purchase_order WHERE id = ?', (po,)).fetchone()
        if (st['state'], st['from'], st['to'], st['text'][:200]) == tuple(prev):
            return
        c.execute("""UPDATE purchase_order SET state = ?, status_text = ?, expected_from = ?, expected_to = ?, delivered_on = ?,
                     status_changed_at = ? WHERE id = ?""",
                  (st['state'], st['text'][:200], st['from'], st['to'], st['delivered_on'], now_utc(), po))
        c.execute("""INSERT INTO purchase_event (po_id, at, state, status_text, expected_from, expected_to)
                     VALUES (?,?,?,?,?,?)""", (po, now_utc(), st['state'], st['text'][:200], st['from'], st['to']))

    @staticmethod
    def _order_from_packages(pk: list):
        """The order's state from its live packages. None when every package is a return or cancelled."""
        live = [p for p in pk if p['state'] in LIVE]
        if not live:
            return None
        open_ = [p for p in live if p['state'] != 'delivered']
        if open_:
            main = min(open_, key=lambda p: (p['to'] or '9999', p['state'] != 'ordered'))
            st = {'state': main['state'], 'from': main['from'], 'to': main['to'], 'delivered_on': None, 'text': main['text']}
        else:
            last = max(live, key=lambda p: p['delivered_on'] or '')
            st = {'state': 'delivered', 'from': None, 'to': None, 'delivered_on': last['delivered_on'], 'text': last['text']}
        boxes = {}
        for p in pk:
            b = boxes.setdefault(p['pkg'], {'text': p['text'], 'qty': 0})
            b['qty'] += max(1, int(p['item'].get('qty') or 1))
        if len(boxes) > 1:
            short = lambda t: re.split(r'\s+(?:Your package|Package was|It was|Signed by|Your return)', t)[0][:48]  # noqa: E731
            st['text'] = f'{len(boxes)} packages: ' + ' · '.join(f"{short(b['text'])} ({b['qty']})" for b in boxes.values())
        return st

    def _save_packages(self, c, po: int, pk, seen: date):
        """Replace the order's package rows with this read (none: the order's own state covers its units)."""
        c.execute('DELETE FROM purchase_package WHERE po_id = ?', (po,))
        rows = {}
        for p in pk or []:
            key = self._line_key(p['item'])
            line = c.execute('SELECT id FROM purchase_line WHERE po_id = ? AND line_key = ?', (po, key)).fetchone() if key else None
            if not line:
                continue
            r = rows.setdefault((p['pkg'], line[0]), {**p, 'qty': 0})
            r['qty'] += max(1, int(p['item'].get('qty') or 1))
        for (pkg, line_id), r in rows.items():
            c.execute("""INSERT INTO purchase_package (po_id, line_id, pkg, qty, state, status_text, expected_from, expected_to,
                         delivered_on, seen_at, shipment_id, package_index) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (po, line_id, pkg, r['qty'], r['state'], r['text'], r['from'], r['to'], r['delivered_on'], seen.isoformat(),
                       r.get('shipment_id'), r.get('package_index')))

    @staticmethod
    def _line_key(it: dict) -> str:
        asin = (it.get('asin') or '').upper()
        return asin or re.sub(r'\W+', ' ', (it.get('title') or '')[:300].lower()).strip()[:60]

    def _apply_returns(self, c, items: list, keys: dict):
        """Per line: how many units sit in packages with a return status, and the least advanced state."""
        from returns import RANK, amazon_return_state
        per = {}
        for it in items:
            state = amazon_return_state(str(it.get('status') or ''))
            key = self._line_key(it)
            if not state or key not in keys:
                continue
            g = per.setdefault(key, {'qty': 0, 'state': state, 'text': str(it.get('status'))[:200]})
            g['qty'] += max(1, int(it.get('qty') or 1))
            if RANK.get(state, 9) < RANK.get(g['state'], 9):
                g['state'], g['text'] = state, str(it.get('status'))[:200]
        for key, g in per.items():
            self.returns.apply_amazon(c, keys[key], g['qty'], g['state'], g['text'])

    @staticmethod
    def _upsert_lines(c, po: int, items: list, subtotal_cents, total_cents) -> dict:
        """Lines by ASIN (or title). Returns {line_key: line_id}."""
        grouped = {}
        for it in items:
            asin = (it.get('asin') or '').upper()
            title = (it.get('title') or '')[:300]
            key = Inbound._line_key(it)
            if not key:
                continue
            g = grouped.setdefault(key, {'asin': asin, 'title': title, 'qty': 0, 'price': 0, 'image': it.get('image')})
            g['qty'] += max(1, int(it.get('qty') or 1))
            g['price'] = int(it.get('price_cents') or 0) or g['price']
        # Spread tax and shipping over the items so cost is what was really paid.
        factor = (total_cents / subtotal_cents) if subtotal_cents and total_cents else 1.0
        for key, g in grouped.items():
            unit = round(g['price'] * factor) if g['price'] else 0
            row = c.execute('SELECT id, qty, unit_cost_cents FROM purchase_line WHERE po_id = ? AND line_key = ?', (po, key)).fetchone()
            if row:
                received = c.execute("""SELECT COALESCE(SUM(r.qty),0) FROM receipt r JOIN action a ON a.id = r.action_id
                                        WHERE r.purchase_line_id = ? AND a.reversed_by IS NULL""", (row[0],)).fetchone()[0]
                c.execute('UPDATE purchase_line SET qty = ?, unit_cost_cents = ?, image_url = COALESCE(?, image_url), title = COALESCE(?, title) WHERE id = ?',
                          (max(g['qty'], received), unit or row[2], g['image'], g['title'] or None, row[0]))
            else:
                c.execute("""INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents, image_url)
                             VALUES (?,?,?,?,?,?,?)""", (po, key, g['asin'], g['title'] or '', g['qty'], unit, g['image']))
        ids = {}
        for k in grouped:
            r = c.execute('SELECT id FROM purchase_line WHERE po_id = ? AND line_key = ?', (po, k)).fetchone()
            if r:
                ids[k] = r[0]
        return ids

    def arriving(self, include_hidden: bool = False) -> dict:
        """Orders still to scan in, one card per package state. A split order whose packages differ (1 delivered,
        1 arriving today) gives one card each, with `part`/`parts` and the units that card holds in `coming`."""
        rec = LINE_RECEIVED_SQL
        today = today_et()
        oldest = (today - timedelta(days=DELIVERED_DAYS)).isoformat()
        orders = self.wms._q(f"""
            SELECT o.id, o.external_id AS order_id, o.source, o.ordered_at, o.state, o.status_text, o.expected_from, o.expected_to,
                   o.delivered_on, o.status_changed_at, o.last_seen_at, o.total_cents, o.hidden
              FROM purchase_order o
             WHERE (o.source = 'amazon' OR (o.account = 'gmail' AND o.review = '' AND o.kind != 'supplies'))
               AND o.state != 'cancelled' AND o.state != 'returned'
               AND o.last_seen_at IS NOT NULL
               AND (o.state != 'delivered' OR o.delivered_on >= ?)
               AND ({'1' if include_hidden else 'o.hidden = 0'})
               AND EXISTS (SELECT 1 FROM purchase_line l WHERE l.po_id = o.id AND l.cancelled = 0 AND {LINE_LEFT_SQL} > 0)""", (oldest,))
        # One query for every order's lines: LINE_LEFT_SQL reads the line_presold view, which SQLite builds once per
        # statement (one query per order rebuilt it 78 times: /api/summary took 8 s on the droplet, 11-11 night).
        by_order = {}
        if orders:
            for ln in self.wms._q(f"""SELECT l.po_id, l.id, l.asin, l.title, l.qty, l.unit_cost_cents, l.image_url, l.product_id,
                                             l.store_item_id, l.store_url, o.source,
                                             {rec} AS received, {LINE_RETURNED_SQL} AS returned, {LINE_LEFT_SQL} AS left_to_receive
                                        FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id WHERE l.cancelled = 0
                                         AND l.po_id IN ({','.join(str(int(o['id'])) for o in orders)}) ORDER BY l.id"""):
                ln['url'] = store_url(ln.pop('source'), ln['asin'], ln.pop('store_item_id'), ln.pop('store_url'))
                by_order.setdefault(ln.pop('po_id'), []).append(ln)
        cards = []
        for o in orders:
            cards += self._cards(o, by_order.get(o['id'], []), oldest)
        cards.sort(key=lambda o: (o['expected_to'] or o['delivered_on'] or '9999', o['ordered_at'] or ''))
        for c in cards:
            c['bucket'] = bucket(c, today.isoformat())
        last = self.wms._q1('SELECT at, device, orders, changed, note FROM sync_run ORDER BY id DESC LIMIT 1')
        return {'orders': cards, 'last_sync': last, 'today': today.isoformat(),
                'counts': counts([c for c in cards if not c.get('hidden')])}

    def _cards(self, o: dict, lines: list, oldest: str) -> list:
        pk = {}
        ship_of = {}
        for p in self.wms._q("""SELECT line_id, pkg, qty, state, status_text, expected_from, expected_to, delivered_on, shipment_id
                                  FROM purchase_package WHERE po_id = ? ORDER BY pkg""", (o['id'],)):
            pk.setdefault(p['line_id'], []).append(p)
            if p['shipment_id']:
                ship_of[p['pkg']] = p['shipment_id']
        tracks = {t['shipment_id']: t for t in self.wms._q(
            """SELECT id, shipment_id, read_at, headline, note, carrier, tracking_id, last_event, photo
                 FROM package_track WHERE po_id = ?""", (o['id'],))} if ship_of else {}
        groups = {}
        for ln in lines:
            for part in portions(ln['qty'], ln['left_to_receive'], pk.get(ln['id'], []), o):
                if part['state'] == 'delivered' and (part['delivered_on'] or '') < oldest:
                    continue   # delivered over DELIVERED_DAYS ago: on the stock audit, like a whole delivered order
                g = groups.setdefault(_card_key(part), {'part': part, 'lines': {}, 'pkgs': []})
                g['lines'][ln['id']] = part['qty']
                g['pkgs'] += [n for n in part.get('pkgs') or [] if n not in g['pkgs']]
        if not groups:
            return []
        rank = {'delivered': 0, 'shipped': 1, 'ordered': 2}
        keys = sorted(groups, key=lambda k: (rank.get(k[0], 3), k[1:]))
        out = []
        for i, k in enumerate(keys, 1):
            part = groups[k]['part']
            card = {**o, 'key': f"{o['id']}-{i}" if len(keys) > 1 else str(o['id']), 'part': i, 'parts': len(keys),
                    'state': part['state'], 'expected_from': part['expected_from'], 'expected_to': part['expected_to'],
                    'delivered_on': part['delivered_on'] if part['state'] == 'delivered' else None,
                    'status_text': part['status_text'] or o['status_text'] if len(keys) > 1 else o['status_text']}
            # One card: every line, scanned-in ones too ("2 already in"). Several: each card shows its own units.
            card['lines'] = [{**ln, 'coming': groups[k]['lines'].get(ln['id'], 0)} for ln in lines
                             if len(keys) == 1 or groups[k]['lines'].get(ln['id'])]
            # The Track page of each package on this card: carrier, tracking ID, latest scan, drop-off note, photo.
            card['tracks'] = []
            for n in sorted(groups[k]['pkgs']):
                t = tracks.get(ship_of.get(n))
                if t:
                    card['tracks'].append({'pkg': n, 'carrier': t['carrier'], 'tracking_id': t['tracking_id'],
                                           'note': t['note'], 'headline': t['headline'], 'read_at': t['read_at'],
                                           'last_event': json.loads(t['last_event']) if t['last_event'] else None,
                                           'photo': f"/api/track-photos/{t['id']}" if t['photo'] else None})
            if o['source'] != 'amazon' and not card['tracks']:
                card['tracks'] = self._store_tracks(o['id'])
            out.append(card)
        return out

    def _has(self, table: str) -> bool:
        return bool(self.wms._q1("SELECT 1 AS x FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)))

    def _store_tracks(self, po_id: int) -> list:
        """A store order's packages: Walmart's page (store_package) first, then the store's shipping emails (mail_package)."""
        out, seen = [], set()
        if self._has('store_package'):
            for r in self.wms._q("""SELECT sp.tracking_id, sp.carrier, MAX(sp.seen_at) AS seen_at,
                                           GROUP_CONCAT(sp.qty || ' x ' || SUBSTR(l.title, 1, 40), ', ') AS items
                                      FROM store_package sp JOIN purchase_line l ON l.id = sp.line_id
                                     WHERE sp.po_id = ? GROUP BY sp.tracking_id ORDER BY MIN(sp.id)""", (po_id,)):
                seen.add(r['tracking_id'])
                out.append({'pkg': len(out) + 1, 'carrier': r['carrier'], 'tracking_id': r['tracking_id'], 'note': '',
                            'headline': r['items'] or '', 'read_at': r['seen_at'], 'last_event': None, 'photo': None})
        if self._has('mail_package'):
            from ship_mail import ShipMail
            for t in ShipMail.tracks(self.wms, po_id):
                if t['tracking_id'] not in seen:
                    out.append({**t, 'pkg': len(out) + 1})
        return out

    def hide(self, po_id: int, hidden: bool, remember: bool = True):
        with self.wms._tx() as c:
            c.execute('UPDATE purchase_order SET hidden = ? WHERE id = ?', (1 if hidden else 0, po_id))
            if remember:
                for (asin,) in c.execute("SELECT asin FROM purchase_line WHERE po_id = ? AND asin != ''", (po_id,)).fetchall():
                    if hidden:
                        c.execute('INSERT OR IGNORE INTO personal_asin (asin, at) VALUES (?, ?)', (asin, now_utc()))
                    else:
                        c.execute('DELETE FROM personal_asin WHERE asin = ?', (asin,))

    def history(self, po_id: int) -> list:
        return self.wms._q('SELECT at, state, status_text, expected_from, expected_to FROM purchase_event WHERE po_id = ? ORDER BY id', (po_id,))
