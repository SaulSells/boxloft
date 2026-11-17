"""Which box each order shipped in (Alex 11-11): from Amazon's label, confirmed by a person, learned per item.

Alex ships orders by buying the label in Seller Central. Seller Central's own order data
(sellercentral.amazon.com/orders-api/order/<id>, read by the extension 2.2+ in his signed-in Chrome) has each
package's box size, weight, label cost and the ASINs in it (`packages[].MssDetails`). Measured 11-11: 28 of the
last 30 FBM orders had it; the other 2 had labels bought elsewhere. The extension sends only those package
fields, never the buyer's name or address. Alex 11-11 11:49 AM ET: read only orders the order sync just saw go
to shipped (the ones auto_ship handles), never a history backfill or any other order.

- amazon_package: one row per Amazon package (PackageId), what the label said.
- After auto_ship scans an order out, the person packing confirms the box on a popup (pending / confirm);
  Amazon's label size is chosen already when it is in. A person's answer wins over Amazon's.
- learned(product): the box last used for that item, a person's answer first, else the newest Amazon label of
  a package that held only that item (from these reads only). pick_list suggests it, so auto_ship and the ship dialog use it too.
"""
import json
import re
from datetime import datetime, timedelta, timezone

from wms import Wms, WmsError, now_utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS amazon_package (
    package_id TEXT PRIMARY KEY,
    order_ext TEXT NOT NULL,
    carrier TEXT NOT NULL DEFAULT '',
    package_type TEXT NOT NULL DEFAULT '',
    l_in REAL, w_in REAL, h_in REAL,
    weight_oz REAL,
    label_cents INTEGER,
    asins TEXT NOT NULL DEFAULT '[]',     -- JSON [[asin, qty], ...]
    read_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_amazon_package_order ON amazon_package(order_ext);
CREATE TABLE IF NOT EXISTS seller_read (    -- Seller Central order reads: so an order with no label data isn't read forever
    order_ext TEXT PRIMARY KEY,
    tries INTEGER NOT NULL DEFAULT 0,
    packages INTEGER NOT NULL DEFAULT 0,
    last_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shipbox_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""
CONFIRM_DAYS = 3            # the popup asks about orders shipped in the last 3 days; Seller Central reads the same window
MAX_TRIES = 3               # an order whose label Amazon doesn't have (bought elsewhere) is read 3 times at most
OZ = {'OZ': 1.0, 'LB': 16.0, 'G': 1 / 28.3495, 'KG': 1000 / 28.3495}
CM_PER_IN = 2.54


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def clean_packages(raw) -> list:
    """The package fields the extension sends (sync.js sellerPackages), checked and in inches/ounces."""
    out = []
    for p in raw if isinstance(raw, list) else []:
        if not isinstance(p, dict) or not re.fullmatch(r'[A-Za-z0-9-]{1,40}', str(p.get('id') or '')):
            continue
        d = p.get('dims') if isinstance(p.get('dims'), dict) else {}
        lwh = [_num(d.get(k)) for k in ('l', 'w', 'h')]
        unit = str(d.get('unit') or 'IN').upper()
        if all(lwh) and unit in ('IN', 'CM'):
            lwh = [round(x / CM_PER_IN, 2) if unit == 'CM' else x for x in lwh]
        else:
            lwh = [None, None, None]
        oz = 0.0
        for w in p.get('weight') if isinstance(p.get('weight'), list) else []:
            if isinstance(w, dict) and _num(w.get('value')) is not None and str(w.get('unit') or '').upper() in OZ:
                oz += _num(w['value']) * OZ[str(w['unit']).upper()]
        label = _num(p.get('label'))
        asins = [[str(i.get('asin'))[:10], int(i.get('qty') or 1)] for i in p.get('items') or []
                 if isinstance(i, dict) and re.fullmatch(r'[A-Z0-9]{10}', str(i.get('asin') or ''))]
        out.append({'id': str(p['id']), 'carrier': str(p.get('carrier') or '')[:30], 'type': str(p.get('type') or '')[:30],
                    'lwh': lwh, 'weight_oz': round(oz, 2) if oz else None,
                    'label_cents': round(label * 100) if label is not None else None, 'asins': asins})
    return out


def dims_text(lwh) -> str:
    return ' x '.join(f'{x:g}' for x in lwh) + ' in' if lwh and all(lwh) else ''


class ShipBoxes:
    def __init__(self, wms: Wms, commerce):
        self.wms, self.commerce = wms, commerce
        conn = wms._conn()
        conn.executescript(SCHEMA)
        for col, decl in (('box_confirmed_at', 'TEXT'), ('box_confirmed_by', 'INTEGER'),
                          ('box_dims', 'TEXT')):   # a size not on the ship-box list ("12.5 x 12.5 x 16 in"), or 'own'
            Wms._add_column(conn, 'sales_line', col, decl)
        # The popup asks only about orders scanned out after it went live: auto_ship had already taken 46 lines
        # (11-11 11:37 AM to the ship-box deploy), mostly packed days before; nobody can say which box now.
        conn.execute("INSERT OR IGNORE INTO shipbox_meta (k, v) VALUES ('popup_since', ?)", (now_utc(),))
        conn.commit()
        commerce.learned_box = self.learned   # pick_list asks this first

    # ------------------------------------------------------------------ Seller Central reads
    def wanted(self) -> list:
        """Order ids whose Seller Central data to read (Alex 11-11: only these): Amazon FBM orders Boxloft scanned
        out by itself (auto_ship) in the last 3 days, and ones still waiting with "Marked shipped on the
        marketplace" (auto_ship couldn't take them yet). Skipped once a read found a box size, or after 3 reads."""
        since = (datetime.now(timezone.utc) - timedelta(days=CONFIRM_DAYS)).strftime('%Y-%m-%dT%H:%M:%SZ')
        rows = self.wms._q("""
            SELECT o.external_id FROM sales_order o LEFT JOIN seller_read r ON r.order_ext = o.external_id
             WHERE o.channel = 'amazon' AND o.fulfilled_by = 'me' AND o.external_id GLOB '[0-9][0-9][0-9]-[0-9]*-[0-9]*'
               AND (r.order_ext IS NULL OR (r.packages = 0 AND r.tries < ?))
               AND ((o.status = 'open' AND o.review LIKE ?)
                    OR EXISTS (SELECT 1 FROM sales_line l JOIN action a ON a.id = l.shipped_action_id
                                WHERE l.order_id = o.id AND a.device = 'auto' AND a.at >= ?))
             ORDER BY o.ordered_at DESC, o.id DESC""", (MAX_TRIES, self.commerce.MARKED_SHIPPED[:24] + '%', since))
        return [r['external_id'] for r in rows]

    def take(self, order_ext: str, packages: list) -> dict:
        pk = clean_packages(packages)
        with self.wms._tx() as c:
            for p in pk:
                c.execute("""INSERT INTO amazon_package (package_id, order_ext, carrier, package_type, l_in, w_in, h_in,
                                                         weight_oz, label_cents, asins, read_at)
                             VALUES (?,?,?,?,?,?,?,?,?,?,?)
                             ON CONFLICT (package_id) DO UPDATE SET carrier = excluded.carrier, package_type = excluded.package_type,
                                 l_in = excluded.l_in, w_in = excluded.w_in, h_in = excluded.h_in, weight_oz = excluded.weight_oz,
                                 label_cents = excluded.label_cents, asins = excluded.asins, read_at = excluded.read_at""",
                          (p['id'], order_ext, p['carrier'], p['type'], *p['lwh'], p['weight_oz'], p['label_cents'],
                           json.dumps(p['asins']), now_utc()))
            have = sum(1 for p in pk if all(p['lwh']))
            c.execute("""INSERT INTO seller_read (order_ext, tries, packages, last_at) VALUES (?, 1, ?, ?)
                         ON CONFLICT (order_ext) DO UPDATE SET tries = tries + 1, packages = MAX(packages, excluded.packages),
                             last_at = excluded.last_at""", (order_ext, have, now_utc()))
        return {'packages': len(pk), 'sized': have}

    # ------------------------------------------------------------------ boxes
    def match_box(self, lwh):
        """The ship box with these inside sizes (any order, 0.1 in either way), or None."""
        if not lwh or not all(lwh):
            return None
        want = sorted(lwh)
        for b in self.commerce.list_ship_boxes():
            have = sorted([b['inner_l_in'], b['inner_w_in'], b['inner_h_in']])
            if all(abs(a - x) <= 0.1 for a, x in zip(want, have)):
                return b
        return None

    def _label_for(self, order_ext: str, asin: str):
        """The Amazon package of this order that held this ASIN, with a size."""
        for r in self.wms._q("""SELECT * FROM amazon_package WHERE order_ext = ? AND l_in IS NOT NULL ORDER BY package_id""",
                             (order_ext,)):
            if not asin or any(a == asin for a, _ in json.loads(r['asins'])):
                return r
        return None

    def learned(self, product_id: int, asin: str = ''):
        """The box this item went out in last time: {'box', 'dims', 'reason'} or None. A person's answer first."""
        p = self.wms._q1('SELECT sku FROM product WHERE id = ?', (product_id,)) if product_id else None
        if not p:
            return None
        asins = {asin} if asin else set()
        asins |= {r['value'].upper() for r in self.wms._q("SELECT value FROM product_identifier WHERE product_id = ? AND kind = 'ASIN'",
                                                           (product_id,))}
        if re.fullmatch(r'[A-Z0-9]{10}', (p['sku'] or '').upper()):
            asins.add(p['sku'].upper())
        person = self.wms._q1("""
            SELECT l.ship_box_id, l.box_dims, l.box_confirmed_at FROM sales_line l
             WHERE l.product_id = ? AND l.box_confirmed_at IS NOT NULL AND l.qty = 1
               AND (SELECT COUNT(*) FROM sales_line x WHERE x.order_id = l.order_id) = 1
             ORDER BY l.box_confirmed_at DESC, l.id DESC LIMIT 1""", (product_id,))
        if person and (person['ship_box_id'] or person['box_dims']):
            box = self.wms._q1('SELECT * FROM ship_box WHERE id = ? AND archived = 0', (person['ship_box_id'],)) \
                if person['ship_box_id'] else None
            what = box['name'] if box else ('its own packaging' if person['box_dims'] == 'own' else person['box_dims'])
            return {'box': box, 'dims': None, 'reason': f"Packed in {what} last time (confirmed {person['box_confirmed_at'][5:10]})"}
        best = None
        for a in sorted(asins):   # a package that held 1 of this item and nothing else (take() writes asins this way)
            r = self.wms._q1("""SELECT p.*, COALESCE(o.ordered_at, '') AS ordered_at FROM amazon_package p
                                  LEFT JOIN sales_order o ON o.channel = 'amazon' AND o.external_id = p.order_ext
                                 WHERE p.l_in IS NOT NULL AND p.asins = ?
                                 ORDER BY ordered_at DESC, p.read_at DESC LIMIT 1""", (json.dumps([[a, 1]]),))
            if r and (not best or r['ordered_at'] > best[1]):
                best = (r, r['ordered_at'])
        if not best:
            return None
        r, when = best
        lwh = [r['l_in'], r['w_in'], r['h_in']]
        box = self.match_box(lwh)
        label = f"Amazon label {when[5:7]}-{when[8:10]}" if len(when) >= 10 else 'Amazon label'
        if box:
            return {'box': box, 'dims': lwh, 'reason': f'Same box as last time ({label}: {dims_text(lwh)})'}
        return {'box': None, 'dims': lwh, 'reason': f'Last time: {dims_text(lwh)} ({label}), not on your box list'}

    # ------------------------------------------------------------------ the popup
    def pending(self) -> list:
        """Lines Boxloft scanned out (auto_ship) in the last 3 days, after the popup went live, whose box nobody
        confirmed yet."""
        since = (datetime.now(timezone.utc) - timedelta(days=CONFIRM_DAYS)).strftime('%Y-%m-%dT%H:%M:%SZ')
        live = self.wms._q1("SELECT v FROM shipbox_meta WHERE k = 'popup_since'")
        since = max(since, live['v']) if live else since
        rows = self.wms._q("""
            SELECT l.id, l.title, l.asin, l.qty, l.product_id, l.ship_box_id, l.box_dims, l.box_confirmed_at,
                   o.channel, o.external_id, a.at AS shipped_at,
                   (SELECT ph.id FROM photo ph WHERE ph.product_id = l.product_id AND ph.removed = 0
                     ORDER BY ph.source = 'upload' DESC, ph.id LIMIT 1) AS photo_id, p.image_url
              FROM sales_line l JOIN sales_order o ON o.id = l.order_id JOIN action a ON a.id = l.shipped_action_id
              LEFT JOIN product p ON p.id = l.product_id
             WHERE a.device = 'auto' AND a.at >= ? AND l.box_confirmed_at IS NULL AND a.reversed_by IS NULL
             ORDER BY a.at""", (since,))
        for r in rows:
            lab = self._label_for(r['external_id'], r['asin']) if r['channel'] == 'amazon' else None
            r['amazon'] = None
            if lab:
                lwh = [lab['l_in'], lab['w_in'], lab['h_in']]
                box = self.match_box(lwh)
                r['amazon'] = {'dims': dims_text(lwh), 'box_id': box['id'] if box else None, 'carrier': lab['carrier'],
                               'weight_oz': lab['weight_oz'], 'label_cents': lab['label_cents']}
            r['suggested'] = (r['amazon'] or {}).get('box_id') or r['ship_box_id']
        return rows

    def confirm(self, user_id: int, line_id: int, ship_box_id=None, other: str = '', own: bool = False) -> dict:
        ln = self.wms._q1('SELECT id, ship_box_id, box_taken, shipped_action_id FROM sales_line WHERE id = ?', (line_id,))
        if not ln or not ln['shipped_action_id']:
            raise WmsError('That order line is not shipped')
        box = None
        if ship_box_id:
            box = self.wms._q1('SELECT * FROM ship_box WHERE id = ?', (int(ship_box_id),))
            if not box:
                raise WmsError('Pick a box from the list')
        dims = 'own' if own else None
        if not box and not own:
            m = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*[x×*]\s*(\d+(?:\.\d+)?)\s*[x×*]\s*(\d+(?:\.\d+)?)\s*(?:in|")?\s*', other or '', re.I)
            if not m:
                raise WmsError('Pick a box, or type the size as L x W x H in inches')
            dims = dims_text([float(m.group(i)) for i in (1, 2, 3)])
        with self.wms._tx() as c:
            new, taken = (box['id'] if box else None), ln['box_taken']
            if ln['ship_box_id'] != new:
                # auto_ship took 1 of the suggested box off the count (only if there was one): give it back, take the real one
                if ln['ship_box_id'] and taken:
                    c.execute('UPDATE ship_box SET stock = stock + 1 WHERE id = ?', (ln['ship_box_id'],))
                taken = c.execute('UPDATE ship_box SET stock = stock - 1 WHERE id = ? AND stock > 0', (new,)).rowcount if new else 0
            c.execute("""UPDATE sales_line SET ship_box_id = ?, box_taken = ?, box_dims = ?, box_confirmed_at = ?,
                                box_confirmed_by = ? WHERE id = ?""", (new, taken, dims, now_utc(), user_id, line_id))
        return {'ok': True}
