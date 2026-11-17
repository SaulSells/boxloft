"""Carrier labels: scan the label on a box that came in and see what is inside (Alex 11-16).

The label is the package's licence plate, as a 3PL dock uses it. Boxloft already holds the tracking ID of each
package: Amazon's Track pages (package_track, keyed on order + shipment) with the units per package
(purchase_package), and Walmart's order page, one shipment group per package (store_package, filled by
store_mail.walmart_pages).
Store shipping emails (ship_mail.py) add a third source: mail_package, one row per order and tracking number, with
the items the email names in mail_package_line. A Walmart page read wins over an email for the same package.
A scan:
- matches when the scanned text contains a known tracking ID. Label barcodes carry more than the ID: USPS IMpb is
  420 + ZIP (+4) + the ID, FedEx's 34-digit barcode ends with it; UPS 1Z and Amazon TBA are the ID itself. The
  longest ID that fits wins; two different IDs of the same length both fitting is held for a person (never guessed).
- is logged in label_scan (who, when, the raw text, what it matched): "in hand" is its own fact, apart from the
  carrier's "delivered", and the raw text is how real labels get checked against the stored IDs.
- never goes to the catalog: a carrier format is known on sight (looks_like), so no Keepa token is spent on it.
"""
import re

from wms import LINE_LEFT_SQL, WmsError, now_utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS store_package (
    id INTEGER PRIMARY KEY,
    po_id INTEGER NOT NULL REFERENCES purchase_order(id),
    line_id INTEGER NOT NULL REFERENCES purchase_line(id),
    tracking_id TEXT NOT NULL,
    carrier TEXT NOT NULL DEFAULT '',
    qty INTEGER NOT NULL,
    seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS store_package_track ON store_package(tracking_id);
CREATE TABLE IF NOT EXISTS label_scan (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    device TEXT NOT NULL DEFAULT '',
    raw TEXT NOT NULL,
    clean TEXT NOT NULL,
    result TEXT NOT NULL,              -- matched, ambiguous, unknown (a carrier format no package has), return (a buyer return)
    tracking_id TEXT,
    po_ids TEXT NOT NULL DEFAULT ''    -- comma list
);
CREATE INDEX IF NOT EXISTS label_scan_track ON label_scan(tracking_id);
CREATE TABLE IF NOT EXISTS label_put (    -- a box page line put from a label's card: which package its units came from
    add_id INTEGER PRIMARY KEY,           -- box_add.id (a count change or Change moves it to the new line)
    tracking_id TEXT NOT NULL
);
"""

# Formats known on sight (on the cleaned text). A 12-digit FedEx or a 20-digit Walmart number looks like a product
# code or nothing in particular, so those count only when a package holds that ID.
FORMATS = [
    ('UPS', re.compile(r'^1Z[0-9A-Z]{16}$')),
    ('Amazon', re.compile(r'^TBA\d{12}$')),
    ('USPS', re.compile(r'^420\d{5}(?:\d{4})?9[1-5]\d{18,32}$')),   # IMpb: 420 + ZIP (+4) + ID
    ('USPS', re.compile(r'^9[1-5]\d{20}(?:\d{4}|\d{8})?$')),        # the ID alone: 22, 26 or 30 digits
    ('FedEx', re.compile(r'^\d{34}$')),
    ('DHL', re.compile(r'^GM\d{16,22}$')),
    ('LaserShip', re.compile(r'^1LS[0-9A-Z]{10,20}$')),
]


def _ean8_ok(c: str) -> bool:
    d = [int(x) for x in c]
    return (10 - (3 * (d[0] + d[2] + d[4] + d[6]) + d[1] + d[3] + d[5]) % 10) % 10 == d[7]


def is_routing(raw: str) -> bool:
    """The short ZIP barcode on a UPS or FedEx label (420 + ZIP), not the tracking one. A real EAN-8 starting
    with 420 has a valid check digit; a ZIP code passes that 1 time in 10, so those 1 in 10 go on as an item."""
    c = clean(raw)
    return bool(re.fullmatch(r'420\d{5}', c)) and not _ean8_ok(c)


def clean(raw: str) -> str:
    """Upper case, letters and digits only: drops the GS (FNC1) separator, ']C1' symbology prefix and spaces."""
    t = (raw or '').strip()
    if t.startswith(']C1') or t.startswith(']c1'):
        t = t[3:]
    return re.sub(r'[^0-9A-Za-z]', '', t).upper()


def looks_like(raw: str) -> str:
    """The carrier a code's format names, else ''."""
    c = clean(raw)
    return next((name for name, rx in FORMATS if rx.match(c)), '')


def carrier_of(tracking_id: str) -> str:
    c = clean(tracking_id)
    if re.fullmatch(r'\d{12}|\d{15}', c):
        return 'FedEx'
    return looks_like(c)


class Labels:
    returns = None   # buyer_returns.BuyerReturns, set by the server: a buyer's return label opens its return card

    def __init__(self, wms):
        from ship_mail import SCHEMA as SHIP_SCHEMA
        self.wms = wms
        wms._conn().executescript(SCHEMA)
        wms._conn().executescript(SHIP_SCHEMA)   # store shipping emails' packages

    # ---------------------------------------------------------------- what a code is

    def _ids(self, c: str) -> list:
        """Known tracking IDs the cleaned scan contains, longest first."""
        if len(c) < 8:
            return []
        rows = self.wms._q("""SELECT DISTINCT UPPER(tracking_id) AS t FROM package_track
                                WHERE tracking_id IS NOT NULL AND LENGTH(tracking_id) >= 8 AND INSTR(?, UPPER(tracking_id)) > 0
                              UNION
                              SELECT DISTINCT UPPER(tracking_id) FROM store_package
                                WHERE LENGTH(tracking_id) >= 8 AND INSTR(?, UPPER(tracking_id)) > 0
                              UNION
                              SELECT DISTINCT UPPER(tracking_id) FROM mail_package
                                WHERE LENGTH(tracking_id) >= 8 AND INSTR(?, UPPER(tracking_id)) > 0""", (c, c, c))
        return sorted((r['t'] for r in rows), key=len, reverse=True)

    def match(self, raw: str) -> dict:
        """Read only. {'kind': 'label', ...package card} | {'kind': 'label_ambiguous', 'choices'} |
        {'kind': 'label_unknown', 'carrier'} | {} (not a label)."""
        c = clean(raw)
        ret = self.returns.ids_in(c) if self.returns else []
        ids = self._ids(c)
        if ret and (not ids or len(ret[0]) >= len(ids[0])):   # a buyer sent it back (Alex 11-16): the return card
            return self.returns.card(ret[0])
        if ids:
            same = [t for t in ids if len(t) == len(ids[0])]
            if len(same) > 1:   # codex blast-radius: a wrong contents link is worse than a question
                return {'kind': 'label_ambiguous', 'code': c, 'choices': [self.card(t) for t in same]}
            return self.card(ids[0])
        carrier = looks_like(c)
        if carrier:
            return {'kind': 'label_unknown', 'code': c, 'carrier': carrier}
        if is_routing(c):
            return {'kind': 'label_unknown', 'code': c, 'carrier': '', 'routing': True}
        return {}

    def is_label(self, raw: str) -> bool:
        return bool(looks_like(raw) or is_routing(raw) or self._ids(clean(raw))
                    or (self.returns and self.returns.ids_in(clean(raw))))

    # ---------------------------------------------------------------- the card

    def _lines(self, where: str, args: tuple) -> list:
        return self.wms._q(f"""SELECT l.id AS line_id, l.po_id, l.title, l.image_url, l.asin, l.product_id, l.qty AS line_qty,
                                      l.unit_cost_cents, {LINE_LEFT_SQL} AS left_to_receive,
                                      p.title AS product_title, p.sku,
                                      (SELECT ph.id FROM photo ph WHERE ph.product_id = l.product_id AND ph.removed = 0
                                        ORDER BY ph.id LIMIT 1) AS photo_id
                                 FROM purchase_line l LEFT JOIN product p ON p.id = l.product_id AND p.archived = 0
                                WHERE l.cancelled = 0 AND {where}""", args)

    def card(self, tracking_id: str) -> dict:
        """Everything in the package with this tracking ID, order by order (one box can hold several orders)."""
        t = tracking_id.upper()
        orders = {}
        for tr in self.wms._q("""SELECT id, po_id, shipment_id, carrier, state, delivered_on, photo FROM package_track
                                  WHERE UPPER(tracking_id) = ?""", (t,)):
            pk = self.wms._q("""SELECT line_id, SUM(qty) AS qty FROM purchase_package
                                 WHERE po_id = ? AND shipment_id = ? AND state != 'cancelled' GROUP BY line_id""",
                             (tr['po_id'], tr['shipment_id']))
            in_pkg = {r['line_id']: r['qty'] for r in pk}
            if not in_pkg:   # no package rows for this shipment: an order with one package holds every line
                n = self.wms._q1("SELECT COUNT(DISTINCT shipment_id) AS n FROM package_track WHERE po_id = ?", (tr['po_id'],))['n']
                if n == 1:
                    in_pkg = {ln['line_id']: ln['line_qty'] for ln in self._lines('l.po_id = ?', (tr['po_id'],))}
            o = orders.setdefault(tr['po_id'], {'carrier': tr['carrier'], 'state': tr['state'], 'delivered_on': tr['delivered_on'],
                                                'photo_track_id': tr['id'] if tr['photo'] else None, 'in_pkg': {}})
            for k, v in in_pkg.items():
                o['in_pkg'][k] = o['in_pkg'].get(k, 0) + v
        for sp in self.wms._q('SELECT po_id, line_id, carrier, SUM(qty) AS qty FROM store_package WHERE UPPER(tracking_id) = ? '
                              'GROUP BY po_id, line_id', (t,)):
            o = orders.setdefault(sp['po_id'], {'carrier': sp['carrier'], 'state': None, 'delivered_on': None,
                                                'photo_track_id': None, 'in_pkg': {}})
            o['in_pkg'][sp['line_id']] = o['in_pkg'].get(sp['line_id'], 0) + sp['qty']
        paged = {r['po_id'] for r in self.wms._q('SELECT DISTINCT po_id FROM store_package WHERE UPPER(tracking_id) = ?', (t,))}
        for mp in self.wms._q('SELECT id, po_id, carrier, state, delivered_on FROM mail_package WHERE UPPER(tracking_id) = ?', (t,)):
            if mp['po_id'] in paged:
                continue   # Walmart's order page already said what this package holds
            o = orders.setdefault(mp['po_id'], {'carrier': mp['carrier'], 'state': mp['state'] if mp['state'] == 'delivered' else 'shipped',
                                                'delivered_on': mp['delivered_on'], 'photo_track_id': None, 'in_pkg': {}})
            for r in self.wms._q('SELECT line_id, qty FROM mail_package_line WHERE package_id = ?', (mp['id'],)):
                o['in_pkg'][r['line_id']] = o['in_pkg'].get(r['line_id'], 0) + r['qty']
        out = []
        for po_id, o in orders.items():
            po = self.wms._q1("""SELECT id, source, external_id, ordered_at, state, delivered_on FROM purchase_order
                                  WHERE id = ?""", (po_id,))
            if not po:
                continue
            ids = list(o['in_pkg'])
            lines = self._lines(f"l.id IN ({','.join('?' * len(ids))})", tuple(ids)) if ids else []
            items = []
            for ln in lines:
                n = o['in_pkg'][ln['line_id']]
                left = self._left_by_package(ln['line_id'], ln['left_to_receive']).get(t, min(n, max(0, ln['left_to_receive'])))
                items.append({'line_id': ln['line_id'], 'title': ln['product_title'] or ln['title'], 'sku': ln['sku'],
                              'image_url': ln['image_url'], 'photo_id': ln['photo_id'], 'asin': ln['asin'],
                              'product_id': ln['product_id'] if ln['product_title'] else None,
                              'in_package': n, 'left': max(0, left),
                              'cost_cents': ln['unit_cost_cents']})
            items.sort(key=lambda i: (-i['left'], i['title'] or ''))
            out.append({'po_id': po['id'], 'source': po['source'], 'order_id': po['external_id'],
                        'ordered_at': (po['ordered_at'] or '')[:10], 'state': o['state'] or po['state'],
                        'delivered_on': o['delivered_on'] or po['delivered_on'],
                        'photo_url': f"/api/track-photos/{o['photo_track_id']}" if o['photo_track_id'] else None,
                        'items': items})
        out.sort(key=lambda x: x['ordered_at'])
        carrier = next((o['carrier'] for o in orders.values() if o['carrier']), '') or carrier_of(t)
        seen = self.wms._q("""SELECT s.at, s.user_id FROM label_scan s WHERE s.tracking_id = ? ORDER BY s.id LIMIT 1""", (t,))   # the first scan: was it opened before?
        return {'kind': 'label', 'tracking_id': t, 'carrier': carrier, 'orders': out,
                'units': sum(i['in_package'] for o in out for i in o['items']),
                'left': sum(i['left'] for o in out for i in o['items']), 'scanned_before': seen}

    def _packages_of_line(self, line_id: int) -> list:
        """Every package a line's units travel in: [{key (tracking ID, else S:shipment), qty, delivered_on}]."""
        out = {}
        for r in self.wms._q("""SELECT p.shipment_id, p.qty, p.delivered_on, UPPER(t.tracking_id) AS tracking_id
                                  FROM purchase_package p LEFT JOIN package_track t ON t.po_id = p.po_id AND t.shipment_id = p.shipment_id
                                 WHERE p.line_id = ? AND p.state != 'cancelled' AND p.shipment_id IS NOT NULL""", (line_id,)):
            k = r['tracking_id'] or f"S:{r['shipment_id']}"
            x = out.setdefault(k, {'key': k, 'qty': 0, 'delivered_on': r['delivered_on']})
            x['qty'] += r['qty']
        for r in self.wms._q('SELECT tracking_id, qty FROM store_package WHERE line_id = ?', (line_id,)):
            x = out.setdefault(r['tracking_id'], {'key': r['tracking_id'], 'qty': 0, 'delivered_on': None})
            x['qty'] += r['qty']
        paged = set(out)
        for r in self.wms._q("""SELECT p.tracking_id, p.delivered_on, pl.qty FROM mail_package_line pl
                                  JOIN mail_package p ON p.id = pl.package_id WHERE pl.line_id = ?""", (line_id,)):
            if r['tracking_id'] in paged:
                continue   # the page read and the email name the same package: count it once
            x = out.setdefault(r['tracking_id'], {'key': r['tracking_id'], 'qty': 0, 'delivered_on': r['delivered_on']})
            x['qty'] += r['qty']
        return list(out.values())

    def _put_from(self, line_id: int) -> dict:
        """tracking ID -> units of this line put away from that label's card, not undone."""
        if not self.wms._q1("SELECT 1 AS x FROM sqlite_master WHERE type = 'table' AND name = 'box_add'"):
            return {}   # no box page yet (scripts, tests): nothing was put from a card
        rows = self.wms._q("""SELECT lp.tracking_id, SUM(b.qty) AS n FROM label_put lp JOIN box_add b ON b.id = lp.add_id
                                 JOIN action a ON a.id = b.action_id
                                WHERE b.line_id = ? AND b.kind = 'order' AND a.reversed_by IS NULL
                                GROUP BY lp.tracking_id""", (line_id,))
        return {r['tracking_id']: r['n'] for r in rows}

    def _left_by_package(self, line_id: int, line_left: int) -> dict:
        """tracking ID -> units of the line still to put away from that package. Units put from a label's card come
        off that package. Every other unit the line no longer waits for (scanned in without the label, sold, sent
        back) comes off the delivered packages first, oldest delivery first, as inbound.portions takes them."""
        pk = self._packages_of_line(line_id)
        if not pk:
            return {}
        done = self._put_from(line_id)
        other = max(0, sum(p['qty'] for p in pk) - max(0, line_left) - sum(done.values()))
        out = {}
        for p in sorted(pk, key=lambda p: (p['delivered_on'] is None, p['delivered_on'] or '', p['key'])):
            rest = max(0, p['qty'] - done.get(p['key'], 0))
            take = min(rest, other)
            other -= take
            out[p['key']] = rest - take
        return out

    def record_put(self, add_id: int, tracking_id: str):
        with self.wms._tx() as c:
            c.execute('INSERT OR REPLACE INTO label_put (add_id, tracking_id) VALUES (?, ?)', (int(add_id), clean(tracking_id)))

    @staticmethod
    def carry(c, old_add_id: int, new_add_id: int):
        """A count change or Change made a new line in place of the old one: the package link moves with it."""
        c.execute('UPDATE label_put SET add_id = ? WHERE add_id = ?', (new_add_id, old_add_id))

    # ---------------------------------------------------------------- a scan

    def scan(self, raw: str, user_id: int, device: str = '') -> dict:
        """Log the scan (in hand) and answer what it is. Not a label: {} and nothing is logged."""
        raw = (raw or '').strip()[:200]
        r = self.match(raw)
        if not r:
            return {}
        if r['kind'] == 'label':
            result, tid, pos = 'matched', r['tracking_id'], ','.join(str(o['po_id']) for o in r['orders'])
        elif r['kind'] == 'label_return':
            result, tid, pos = 'return', r['tracking_id'], ''
        elif r['kind'] == 'label_ambiguous':
            result, tid, pos = 'ambiguous', None, ''
        else:
            result, tid, pos = 'unknown', None, ''
        with self.wms._tx() as c:
            c.execute("""INSERT INTO label_scan (at, user_id, device, raw, clean, result, tracking_id, po_ids)
                         VALUES (?,?,?,?,?,?,?,?)""", (now_utc(), user_id, (device or '')[:40], raw, clean(raw), result, tid, pos))
        return r

    def pick(self, raw: str, tracking_id: str, user_id: int, device: str = '') -> dict:
        """A person picked one package on an ambiguous scan: that answer is logged and its card returned."""
        r = self.match(raw)
        if r.get('kind') != 'label_ambiguous' or tracking_id.upper() not in [ch['tracking_id'] for ch in r['choices']]:
            raise WmsError('That package is not one this label fits. Scan the label again.')
        card = self.card(tracking_id)
        with self.wms._tx() as c:
            c.execute("""INSERT INTO label_scan (at, user_id, device, raw, clean, result, tracking_id, po_ids)
                         VALUES (?,?,?,?,?,'picked',?,?)""", (now_utc(), user_id, (device or '')[:40], raw[:200], clean(raw),
                                                              card['tracking_id'], ','.join(str(o['po_id']) for o in card['orders'])))
        return card

    def last_scans(self, po_ids: list) -> dict:
        """po_id -> the newest label scan of a package on that order {at, user_id} (Arriving: 'Label scanned')."""
        out = {}
        for r in self.wms._q("SELECT at, user_id, po_ids FROM label_scan WHERE result IN ('matched', 'picked') ORDER BY id"):
            for p in (r['po_ids'] or '').split(','):
                if p and int(p) in po_ids:
                    out[int(p)] = {'at': r['at'], 'user_id': r['user_id']}
        return out

    # ---------------------------------------------------------------- Walmart packages

    @staticmethod
    def save_store_packages(c, po_id: int, rows: list):
        """Walmart's page read: [{line_id, tracking_id, qty}] replaces what this order had (a page read is the truth)."""
        c.execute(SCHEMA.split(';')[0])   # store_mail may run before Labels made its tables (tests, scripts)
        c.execute('DELETE FROM store_package WHERE po_id = ?', (po_id,))
        for r in rows:
            t = clean(r.get('tracking_id') or '')
            if len(t) >= 8 and r.get('line_id'):
                c.execute('INSERT INTO store_package (po_id, line_id, tracking_id, carrier, qty, seen_at) VALUES (?,?,?,?,?,?)',
                          (po_id, int(r['line_id']), t, r.get('carrier') or carrier_of(t), max(1, int(r.get('qty') or 1)), now_utc()))
