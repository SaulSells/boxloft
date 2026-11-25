"""Sorting the audit pile into boxes: one scan = one unit.

Alex 11-11 12:20 PM: there are no bins. Boxes sit on shelves, and each box has a name written on it (A31). Each box
gets a printed 4x6 label with that name and a barcode, made the first time someone types its name here. Open shelving
holds items loose in their retail packaging on named shelves (S1), each with its own label.

The person sets the current box (or shelf) by scanning its label or typing its name, then scans each item:

- the code is resolved to Stock audit rows: product identifiers -> ASIN, an ASIN directly, a purchase line's
  UPC, our listing's SKU, and last the catalog (Keepa/UPCitemdb, cached forever) for UPC/EAN -> ASIN;
- exactly one row with units left: Audit.found(1) into the box at once (same receipts as "Found it");
- several rows: the person picks; a fully accounted row asks first; nothing on the list: an extra.

Extras are units nobody expected: received into the box (against an open purchase line of that product when
there is one, so it leaves Arriving; else at the cost typed in) and kept in audit_extra (append-only, one row
per receipt) so they can be listed. Undo reverses the receipt; the row then stops counting.

Every write carries the client's scan_id: a retried request finds the first save instead of adding a unit.
"""
import contextlib
import json
import math
import re
import threading
import time

from codes import make_code, parse_scan
from listings import _words, title_score
from wms import HOME_SITE, LINE_LEFT_SQL, VOIDED_SQL, Wms, WmsError, now_utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS box_add_photo (   -- the Add card's photo a save put on its item (Alex 11-17)
    add_id INTEGER PRIMARY KEY,              -- a box page line (box_add.id) saved from it; several lines can share one.
    photo_id INTEGER NOT NULL                -- Undo and Change take it off with its last standing line
);
CREATE TABLE IF NOT EXISTS photo_freed (     -- photos an Undo or a Change took off an item (never a person's removal):
    photo_id INTEGER PRIMARY KEY             -- only these may move to the right item (review 11-17)
);
CREATE TABLE IF NOT EXISTS audit_extra (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    user_id INTEGER,
    code TEXT NOT NULL DEFAULT '',
    product_id INTEGER NOT NULL REFERENCES product(id),
    item_id INTEGER REFERENCES audit_item(id),   -- set when the audit row was already fully accounted for
    qty INTEGER NOT NULL,
    unit_cost_cents INTEGER,                     -- NULL: nobody typed a cost and no order line had one
    location_id INTEGER,
    action_id INTEGER NOT NULL REFERENCES action(id),
    purchase_line_id INTEGER,
    scan_id TEXT UNIQUE,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TRIGGER IF NOT EXISTS audit_extra_no_update BEFORE UPDATE ON audit_extra
BEGIN SELECT RAISE(ABORT, 'audit_extra is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_extra_no_delete BEFORE DELETE ON audit_extra
BEGIN SELECT RAISE(ABORT, 'audit_extra is append-only'); END;
-- A barcode a scan put on a product (red line 11-11): Undo of that scan takes it off again, so one wrong tap on a
-- guess doesn't send every later scan of that barcode to the wrong row.
CREATE TABLE IF NOT EXISTS scan_code (
    count_id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    value TEXT NOT NULL
);
-- Box page adds (Alex 11-11 1:10 PM): one row per Add tap on a box's page, append-only. kind says what the units
-- were: audit (a Stock audit row's unit found), order (received against an open order line), move (from another
-- box or shelf), new (stock on no order and no list: also an audit_extra). scan_id makes a resent tap a no-op.
CREATE TABLE IF NOT EXISTS box_add (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    user_id INTEGER,
    box_id INTEGER,
    location_id INTEGER,
    kind TEXT NOT NULL,
    product_id INTEGER,
    qty INTEGER NOT NULL,
    action_id INTEGER,            -- order, move, new
    item_id INTEGER,              -- audit
    count_id INTEGER,             -- audit: the save's newest audit_count row (undo takes back its whole batch)
    line_id INTEGER,              -- order
    from_box_id INTEGER,          -- move
    from_location_id INTEGER,     -- move
    ident_kind TEXT,              -- a barcode this add put on the product; its undo takes it off
    ident_value TEXT,
    code TEXT NOT NULL DEFAULT '',
    scan_id TEXT UNIQUE
);
-- A new item made by a box scan (flow audit 11-12, redesign 2): its Undo takes back the product too.
CREATE TABLE IF NOT EXISTS put_made (add_id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL);
CREATE TRIGGER IF NOT EXISTS box_add_no_update BEFORE UPDATE ON box_add
BEGIN SELECT RAISE(ABORT, 'box_add is append-only'); END;
CREATE TRIGGER IF NOT EXISTS box_add_no_delete BEFORE DELETE ON box_add
BEGIN SELECT RAISE(ABORT, 'box_add is append-only'); END;
"""

ASIN_RE = re.compile(r'^B0[0-9A-Z]{8}$')
FNSKU_RE = re.compile(r'^X0[0-9A-Z]{8}$')
NAME_RE = re.compile(r'^[A-Z]{1,3}-?\d{1,4}$')     # a box or shelf name as written on it: A31, F7, S2
RANGE_RE = re.compile(r'^([A-Z]{1,3})-?(\d{1,4})-(?:\1-?)?(\d{1,4})$')
GUESS_MIN = 0.5      # share of the shorter title's words in the other: shown as a guess, never recorded alone
MAX_NAMES = 200
LETTER = {'amazon_fbm': 'A', 'amazon_fba': 'A', 'ebay': 'E', 'walmart': 'W'}   # box letter = the channel it sells on
# Words too common to find a look-alike item by (new_card's twin search).
_TWIN_STOP = {'the', 'and', 'for', 'with', 'set', 'pack', 'new', 'from', 'pcs', 'inch'}

# Alex 11-11: a market is the box's letter. An item's market is the box it sits in; moving it to another letter's
# box changes its market. No per-unit tag, so the ledger, counts and moves need nothing new. F boxes: no market yet.
MARKETS = {'A': 'Amazon', 'E': 'eBay', 'W': 'Walmart', 'L': 'Local'}


def market_of(code: str):
    """'W12' -> 'W'; names that are not market boxes (F3, ALEXSHOUSE, DOCK) -> None."""
    m = re.match(r'^([A-Z]+)-?\d+$', (code or '').upper())
    return m.group(1) if m and m.group(1) in MARKETS else None


def clean_name(name: str) -> str:
    """'a-31 ' -> 'A31': the name as it goes on the label."""
    n = re.sub(r'\s+', '', (name or '').upper())
    return n.replace('-', '') if NAME_RE.match(n) else (name or '').strip().upper()


def expand_ranges(text: str) -> list:
    """'A1-A3, E7' -> ['A1', 'A2', 'A3', 'E7']. Single names pass through."""
    out = []
    for part in re.split(r'[,;\s]+', re.sub(r'\s*-\s*', '-', (text or '').upper())):
        part = part.strip()
        if not part:
            continue
        m = RANGE_RE.match(part)
        if m:
            a, b = int(m.group(2)), int(m.group(3))
            if a > b:
                a, b = b, a
            if b - a + 1 > MAX_NAMES:
                raise WmsError(f'{part} is more than {MAX_NAMES} names')
            out += [f'{m.group(1)}{n}' for n in range(a, b + 1)]
        elif NAME_RE.match(part):
            out.append(part.replace('-', ''))
        else:
            raise WmsError(f'"{part}" is not a box name or a range like A1-A9')
    seen, codes = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            codes.append(c)
    if len(codes) > MAX_NAMES:
        raise WmsError(f'At most {MAX_NAMES} at a time')
    return codes


def _ident(code: str):
    """The identifier a scanned code becomes on a new product (None: it becomes the SKU)."""
    c = code.strip().upper()
    if ASIN_RE.match(c):
        return {'kind': 'ASIN', 'value': c}
    if FNSKU_RE.match(c):
        return {'kind': 'FNSKU', 'value': c}
    if c.isdigit():
        return {'kind': {12: 'UPC', 8: 'UPC', 13: 'EAN', 14: 'GTIN'}.get(len(c), 'UPC'), 'value': c}
    return None


def _place_text(site: str, name: str, code: str) -> str:
    """'Main warehouse', 'Main warehouse · Shelf S2', "Alex's house"."""
    if site:
        return site if not name or name == site else f'{site} · {name}'
    return name or code


def cents_in(v) -> int:
    """A cost typed on the box page, in cents: a 400 with words for junk or a number SQLite can't hold (review 11-25:
    a 20-digit cost string was a 500), checked before anything is written."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise WmsError('Type the cost in dollars, like 12.99')
    if n < 0:
        raise WmsError('Cost cannot be negative')
    if n > 100_000_00:
        raise WmsError('Cost must be between $0 and $100,000')
    return n


class Sorting:
    def __init__(self, wms, commerce, audit, catalog=None, listings=None):
        self.wms, self.commerce, self.audit, self.catalog, self.listings = wms, commerce, audit, catalog, listings
        self.photos = None   # PhotoId (server sets it): a photo taken on the Add card goes on the item it saved
        self._lock = threading.RLock()   # one scan write at a time, so a retried scan_id is seen (add() calls found())
        self._handed = {}   # box id -> (user id, time) for Next box
        conn = wms._conn()
        conn.executescript(SCHEMA)
        Wms._add_column(conn, 'audit_extra', 'box_id', 'INTEGER')   # boxes rebuild 11-11: extras go into boxes
        Wms._add_column(conn, 'box_add', 'action_ids', 'TEXT')      # an add over several order lines: every receipt

    # ------------------------------------------------------------------ targets: a box, or a shelf (loose)

    @staticmethod
    def holder(box_id=None, location_id=None) -> dict:
        if box_id:
            return {'box_id': int(box_id)}
        if location_id:
            return {'location_id': int(location_id)}
        return {}

    def _box(self, box_id: int) -> dict:
        b = self.wms._q1("""SELECT b.id, b.name, b.kind, b.archived, b.location_id, l.code AS loc_code, l.site AS loc_site,
                                   l.name AS loc_name
                              FROM box b JOIN location l ON l.id = b.location_id WHERE b.id = ?""", (int(box_id or 0),))
        if not b:
            raise WmsError('That box is not in Boxloft')
        if b['archived']:
            raise WmsError(f"Box {b['name'] or b['id']} was retired. Make it again (type its name) to use it.")
        return b

    def _shelf(self, location_id: int) -> dict:
        loc = self.wms._q1('SELECT id, code, name, site, kind, archived FROM location WHERE id = ?', (int(location_id or 0),))
        if not loc or loc['kind'] == 'virtual':
            raise WmsError('That shelf is not in Boxloft')
        if loc['archived']:
            raise WmsError(f"{loc['code']} is no longer in use. Scan a box label, or type the box name (A31).")
        return loc

    def target(self, to: dict) -> dict:
        """What is in this box or on this shelf now (the ledger's stock_level), biggest first."""
        to = to or {}
        if to.get('box_id'):
            b = self._box(to['box_id'])
            rows = self.wms._contents(box_id=b['id'])
            word = {'tote': 'Tote', 'pallet': 'Pallet'}.get(b['kind'], 'Box')
            head = {'type': 'box', 'id': b['id'], 'code': b['name'].upper() if b['name'] else f"{word.upper()} {b['id']}",
                    'name': b['name'], 'scan_code': make_code('box', b['id']), 'location_id': b['location_id'],
                    'where': _place_text(b['loc_site'], b['loc_name'], b['loc_code'])}
        elif to.get('location_id'):
            loc = self._shelf(to['location_id'])
            rows = self.wms._contents(location_id=loc['id'])
            shelf = loc['site'] and loc['name'] != loc['site'] and loc['kind'] == 'shelf'
            head = {'type': 'shelf', 'id': loc['id'], 'code': loc['code'] if shelf else _place_text(loc['site'], loc['name'], loc['code']),
                    'name': loc['name'], 'scan_code': make_code('location', loc['id']), 'location_id': loc['id'],
                    'where': loc['site'] or loc['name'] or loc['code']}
        else:
            raise WmsError('Scan a box label first, or type the box name (A31)')
        img = {}
        if rows:
            ids = sorted({r['product_id'] for r in rows})
            img = {r['id']: r['image_url'] for r in self.wms._q(
                f"SELECT id, image_url FROM product WHERE id IN ({','.join('?' * len(ids))})", ids)}
        items = {}
        for r in rows:
            g = items.setdefault(r['product_id'], {'product_id': r['product_id'], 'sku': r['sku'], 'title': r['title'],
                                                   'qty': 0, 'photo_id': r['photo_id'], 'image_url': img.get(r['product_id'])})
            g['qty'] += r['qty']
        items = sorted(items.values(), key=lambda g: (-g['qty'], g['title']))
        mk = market_of(head['code']) if head['type'] == 'box' else None
        return {**head, 'units': sum(g['qty'] for g in items), 'items': len(items), 'contents': items,
                'market': mk, 'market_name': MARKETS.get(mk)}

    def _box_by_name(self, name: str, archived: bool = False):
        return self.wms._q1(f"""SELECT id FROM box WHERE REPLACE(UPPER(name), '-', '') = ? AND archived = {1 if archived else 0}
                                 ORDER BY id DESC LIMIT 1""", (clean_name(name).replace('-', ''),))

    def _shelf_by_name(self, name: str):
        return self.wms._q1("""SELECT id FROM location WHERE code = ? AND kind != 'virtual' AND archived = 0 AND site != ''
                                AND name != site""", (clean_name(name),))

    def find(self, name: str) -> dict:
        """A typed name: the box with that name, else the shelf, else what can be made."""
        n = clean_name(name)
        if not n:
            raise WmsError('Type the name written on the box, like A31')
        b = self._box_by_name(n)
        if b:
            return {'kind': 'target', 'target': self.target({'box_id': b['id']})}
        s = self._shelf_by_name(n)
        if s:
            return {'kind': 'target', 'target': self.target({'location_id': s['id']})}
        return {'kind': 'no_target', 'name': n, 'retired': bool(self._box_by_name(n, archived=True))}

    def make_box(self, name: str, user_id: int, location_id: int = None) -> dict:
        """A box with the name written on it, at the warehouse (or the place given). A retired box with that name
        comes back instead of a second one. new=True: its label still has to be printed."""
        n = clean_name(name)
        if not n:
            raise WmsError('Type the name written on the box, like A31')
        if len(n) > 30:
            raise WmsError('Keep the box name short (what is written on it, like A31)')
        have = self._box_by_name(n)
        if have:
            return {**self.target({'box_id': have['id']}), 'new': False}
        old = self._box_by_name(n, archived=True)
        if old:
            self.wms.update_box(old['id'], archived=False)
            return {**self.target({'box_id': old['id']}), 'new': True}
        b = self.wms.create_boxes(1, user_id, location_id or None, name=n)[0]
        return {**self.target({'box_id': b['id']}), 'new': True}

    def make_shelf(self, name: str, site: str = '') -> dict:
        """A named shelf of open shelving (S1) in a building (default the warehouse): items sit on it loose."""
        n = clean_name(name)
        site = (site or '').strip() or HOME_SITE
        if not n or len(n) > 20:
            raise WmsError('Type the shelf name, like S1')
        s = self._shelf_by_name(n)
        if s:
            return {**self.target({'location_id': s['id']}), 'new': False}
        if self.wms._q1('SELECT 1 AS x FROM location WHERE code = ?', (n,)):
            raise WmsError(f'{n} is already used for another place. Pick another shelf name.')
        with self.wms._tx() as c:
            lid = c.execute("INSERT INTO location (code, name, site, kind, created_at) VALUES (?, ?, ?, 'shelf', ?)",
                            (n, f'Shelf {n}', site, now_utc())).lastrowid
        return {**self.target({'location_id': lid}), 'new': True}

    def boxes(self, ranges: str, create: bool, user_id: int = 0) -> dict:
        """Boxes named by the ranges (A23, A31, A60-A66). create=True makes the missing ones at the warehouse.
        Returns every box in the ranges with its scan code, in range order, for printing."""
        names = expand_ranges(ranges)
        if not names:
            raise WmsError('Type box names like A23, A31')
        out, made = [], 0
        for n in names:
            if create:
                t = self.make_box(n, user_id)
                made += 1 if t['new'] else 0
            else:
                b = self._box_by_name(n)
                if not b:
                    continue
                t = self.target({'box_id': b['id']})
            out.append({'id': t['id'], 'code': t['code'], 'scan_code': t['scan_code'], 'where': t['where'], 'new': t.get('new', False)})
        return {'boxes': out, 'created': made, 'existing': len(out) - made, 'missing': len(names) - len(out)}

    def retire(self, box_id: int, user_id: int, move_to: str = '', device: str = '') -> dict:
        """Take a box out of use (damaged, gone). An empty box is retired at once. A box with stock needs move_to
        (another box's name): everything in it moves there first with one ledger move, then it is retired."""
        b = self._box(box_id)
        units = self.wms._q1('SELECT COALESCE(SUM(qty), 0) AS n FROM stock_level WHERE box_id = ? AND qty > 0', (b['id'],))['n']
        moved_to = None
        if units:
            if not (move_to or '').strip():
                raise WmsError(f"Box {b['name'] or b['id']} still holds {units} unit{'' if units == 1 else 's'}. "
                               f"Pick the box they move to.")
            dst = self._box_by_name(move_to)
            if not dst:
                raise WmsError(f'There is no box {clean_name(move_to)} to move them to')
            if dst['id'] == b['id']:
                raise WmsError('Pick a different box to move them to')
            self.wms.move_all(user_id, {'box_id': b['id']}, {'box_id': dst['id']}, note=f"Box {b['name']} retired", device=device)
            moved_to = self.target({'box_id': dst['id']})
        self.wms.update_box(b['id'], archived=True)
        return {'retired': b['name'] or str(b['id']), 'moved_units': units, 'moved_to': moved_to}

    # ------------------------------------------------------------------ markets (Alex 11-11)

    def _market_rows(self, letter: str, archived: bool = False) -> list:
        """Boxes of one market: named letter + number, numbered, with units now."""
        rows = self.wms._q(f"""SELECT b.id, b.name, b.archived,
                                     COALESCE((SELECT SUM(s.qty) FROM stock_level s WHERE s.box_id = b.id AND s.qty > 0), 0) AS units
                                FROM box b WHERE {'1' if archived else 'b.archived = 0'} AND UPPER(b.name) GLOB ?""",
                           (letter + '[0-9]*',))
        out = []
        for r in rows:
            m = re.match(r'^[A-Z]+-?(\d+)$', r['name'].upper())
            if m and market_of(r['name']) == letter:
                out.append({**r, 'code': r['name'].upper(), 'n': int(m.group(1))})
        return sorted(out, key=lambda r: r['n'])

    def market_boxes(self, letter: str) -> dict:
        """One market's boxes in use (most recently filled first), its next empty box, and the name a new one gets."""
        letter = (letter or '').upper()
        if letter not in MARKETS:
            raise WmsError('Pick Amazon, eBay, Walmart or Local')
        rows = self._market_rows(letter)
        last = {r['box']: r['a'] for r in self.wms._q(
            'SELECT to_box_id AS box, MAX(action_id) AS a FROM stock_move WHERE to_box_id != 0 GROUP BY to_box_id')}
        used = sorted([r for r in rows if r['units'] > 0], key=lambda r: (-(last.get(r['id']) or 0), r['n']))
        empty = next((r for r in rows if r['units'] == 0), None)
        top = max([r['n'] for r in self._market_rows(letter, archived=True)], default=0)
        slim = lambda r: {'id': r['id'], 'code': r['code'], 'units': r['units']}   # noqa: E731
        return {'market': letter, 'name': MARKETS[letter], 'boxes': [slim(r) for r in used],
                'next_empty': slim(empty) if empty else None, 'next_new': f'{letter}{top + 1}'}

    def next_box(self, letter: str, user_id: int = 0) -> dict:
        """The box to start filling for a market: its lowest empty box, or (none left) a new one numbered after the
        highest Boxloft has for that letter. new=True means its label still has to be printed."""
        m = self.market_boxes(letter)
        # An empty box handed to the other person in the last 10 minutes is theirs (red line 11-11: two people tapping
        # Next got the same one and filled two containers under one name).
        now = time.time()
        self._handed = {k: v for k, v in self._handed.items() if v[1] > now - 600}
        free = [r for r in self._market_rows(letter) if r['units'] == 0
                and (r['id'] not in self._handed or self._handed[r['id']][0] == user_id)]
        if free:
            pick = min(free, key=lambda r: r['n'])
            self._handed[pick['id']] = (user_id, now)
            return {**self.target({'box_id': pick['id']}), 'new': False}
        made = self.make_box(m['next_new'], user_id)
        self._handed[made['id']] = (user_id, now)
        return made


    # ------------------------------------------------------------------ resolve

    @staticmethod
    def _slim(r: dict) -> dict:
        keep = ('id', 'asin', 'line_id', 'store', 'title', 'brand', 'image_url', 'expected', 'accounted', 'found', 'received',
                'sold', 'sent_back', 'closed', 'status', 'product_id', 'avg_cost_cents')
        return {k: r.get(k) for k in keep}

    def resolve(self, value: str) -> dict:
        """Audit rows for a scanned product code, plus what is known about it (catalog, existing product)."""
        value = (value or '').strip()
        up = value.upper()
        products = [p for p in self.wms.lookup(value) if not p['archived']]
        asins = {i['value'].upper() for p in products for i in p.get('identifiers') or [] if i['kind'] == 'ASIN'}
        line_ids = set()
        if ASIN_RE.match(up) or self.wms._q1('SELECT 1 AS x FROM audit_item WHERE asin = ?', (up,)):
            asins.add(up)
        if up.isdigit() and len(up) >= 8:   # a purchase line that carries this UPC (store emails, CSV imports)
            for ln in self.wms._q("SELECT id, UPPER(asin) AS asin FROM purchase_line WHERE upc != '' AND LTRIM(upc, '0') = LTRIM(?, '0')", (up,)):
                if ln['asin']:
                    asins.add(ln['asin'])
                else:
                    line_ids.add(ln['id'])
        la = self.audit._listing_asin(value)   # our seller SKU
        if la:
            asins.add(la)
        catalog, error = None, ''
        rows = self._rows(products, asins, line_ids)
        if not rows and not products and self.catalog:
            try:
                hits = self.catalog.lookup(value)
            except WmsError as e:
                hits, error = [], str(e)
            for h in hits:
                if h.get('asin'):
                    asins.add(h['asin'].upper())
                    products += [p for p in self.wms.lookup(h['asin'].upper()) if not p['archived'] and p not in products]
            rows = self._rows(products, asins, line_ids)
            have = {r['asin'].upper() for r in rows if r.get('asin')}
            catalog = next((h for h in hits if (h.get('asin') or '').upper() in have), hits[0] if hits else None)
        product = products[0] if products else None
        if product:   # list_products leaves these out
            product = {**product, **self.wms._q1('SELECT image_url, brand FROM product WHERE id = ?', (product['id'],))}
        title = (catalog or {}).get('title') or (product or {}).get('title') or (rows[0]['title'] if rows else '')
        guesses = []
        if not rows and title:   # no row by code (store rows have no ASIN; a UPC can map to another variant's ASIN):
            for r in self.audit._rows():   # a title that fits is offered as a guess, never recorded alone
                if r['accounted'] >= r['expected']:
                    continue
                s = title_score(title, r['title'])
                if s >= GUESS_MIN:
                    guesses.append({**self._slim(r), 'score': round(s, 2)})
            guesses = sorted(guesses, key=lambda g: -g['score'])[:3]
        image = (catalog or {}).get('image_url') or (product or {}).get('image_url') or (rows[0]['image_url'] if rows else None)
        info = {'code': value, 'title': title, 'image_url': image, 'asin': (catalog or {}).get('asin') or (next(iter(asins)) if len(asins) == 1 else None),
                'brand': (catalog or {}).get('brand') or (product or {}).get('brand') or '',
                'product': {k: product[k] for k in ('id', 'sku', 'title')} if product else None,
                'from_catalog': bool(catalog), 'lookup_error': error}
        return {'rows': [self._slim(r) for r in rows], 'guesses': guesses, 'info': info,
                'in_stock': self.commerce._where(product['id']) if product else []}

    def _rows(self, products: list, asins: set, line_ids: set) -> list:
        conds, params = [], []
        if asins:
            conds.append(f"i.asin IN ({','.join('?' * len(asins))})")
            params += sorted(asins)
            if self.audit._has_table('listing_mark'):   # store rows a person matched to a listing of this ASIN
                conds.append(f"i.id IN (SELECT m.item_id FROM listing_mark m JOIN listing li ON li.id = m.listing_id"
                             f" WHERE m.kind = 'confirm' AND UPPER(li.asin) IN ({','.join('?' * len(asins))})"
                             f" AND NOT EXISTS (SELECT 1 FROM listing_mark u WHERE u.kind = 'undo' AND u.undoes = m.id))")
                params += sorted(asins)
        if line_ids:
            conds.append(f"i.line_id IN ({','.join('?' * len(line_ids))})")
            params += sorted(line_ids)
        if products:
            ids = [p['id'] for p in products]
            conds.append(f"i.line_id IN (SELECT id FROM purchase_line WHERE product_id IN ({','.join('?' * len(ids))}))")
            params += ids
        return self.audit._rows(' OR '.join(conds), params) if conds else []

    def _hint(self, rows: list, product_id=None, title='', bin_code='') -> dict:
        """Where the item is listed (Stock audit's own listing matches), and whether the box's letter fits."""
        if not self.listings:
            return {'listed_on': [], 'letter': None, 'mismatch': False}
        items = [dict(r) for r in rows] or [{'id': -1, 'asin': '', 'line_id': None, 'product_id': product_id, 'title': ''}]
        try:
            self.listings.annotate(items)
        except Exception:   # noqa: BLE001 - a hint only; never stop a scan over it
            return {'listed_on': [], 'letter': None, 'mismatch': False}
        labels, letters = [], set()
        for it in items:
            for x in it.get('listed') or []:
                if not x.get('live'):
                    continue
                if x['label'] not in labels:
                    labels.append(x['label'])
                if LETTER.get(x['platform']):
                    letters.add(LETTER[x['platform']])
        letter = next(iter(letters)) if len(letters) == 1 else None
        have = market_of(bin_code)
        mismatch = bool(letter and have and have in LETTER.values() and have != letter)
        return {'listed_on': labels, 'letter': letter, 'mismatch': mismatch}

    # ------------------------------------------------------------------ scan

    def scan(self, code: str, to: dict, user_id: int, device: str = '', scan_id: str = '') -> dict:
        """to: the current box ({'box_id'}) or shelf ({'location_id'}), or {} when none is set yet."""
        to = to or {}
        p = parse_scan(code)
        kind = p['kind']
        if kind in ('box', 'location'):
            try:
                return {'kind': 'target', 'target': self.target({f'{kind}_id': p['id']})}
            except WmsError as e:
                gone = 'retired' in str(e) or 'no longer' in str(e)
                return {'kind': 'wrong', 'message': str(e) if gone else f"Label {p['code']} is not in Boxloft."}
        if kind == 'unit':
            return {'kind': 'wrong', 'message': 'That unit label is already in Boxloft. Scan the item barcode or a box label.'}
        if kind == 'bad_check':
            return {'kind': 'wrong', 'message': 'Misread: the check digit is wrong. Scan again.'}
        value = p['value']
        sid = (scan_id or '')[:40]
        if sid and to:   # a resend after a lost reply gets the first save's answer (sweep A1: it said "Another one?")
            dup = self.wms._q1("SELECT item_id FROM audit_count WHERE batch = ? AND kind = 'found'", (sid,))
            if dup:
                return self.found(dup['item_id'], to, user_id, device, sid, code=value)
            if self.wms._q1('SELECT id FROM audit_extra WHERE scan_id = ?', (sid,)):
                return self.extra(value, to, user_id, device, sid)
        if NAME_RE.match(value.upper()) and not self.wms.lookup(value):
            return self.find(value)   # a box name typed or scanned (A31): set it, or offer to make it
        if self._label(value):   # a carrier label is never an extra item (Alex 11-16)
            return {'kind': 'wrong', 'message': 'That is a carrier label. Scan it on a box page or the header to see what is inside.'}
        here = self.target(to)['code'] if to else ''   # a retired box stops here, before any lookup
        r = self.resolve(value)
        open_rows = [x for x in r['rows'] if x['accounted'] < x['expected']]
        if len(open_rows) == 1 and to:
            return self.found(open_rows[0]['id'], to, user_id, device, scan_id, info=r['info'], code=value)
        r['hint'] = self._hint(open_rows or r['rows'], (r['info']['product'] or {}).get('id'), bin_code=here)
        if len(open_rows) == 1:
            return {'kind': 'need_target', **r}
        if open_rows:
            return {'kind': 'pick', **r, 'rows': open_rows}
        if r['rows']:
            return {'kind': 'full', **r}
        return {'kind': 'not_listed', **r}

    def _here(self, to: dict, product_id: int) -> int:
        h = self.holder(**to)
        return self.wms._q1('SELECT COALESCE(SUM(qty), 0) AS n FROM stock_level WHERE box_id = ? AND location_id = ? AND product_id = ?',
                            (h.get('box_id', 0), h.get('location_id', 0), product_id))['n']

    def found(self, item_id: int, to: dict, user_id: int, device: str = '', scan_id: str = '', info: dict = None,
              code: str = '', qty: int = 1) -> dict:
        """Found qty (default 1: one scan) of an audit row into the box. More than the row has left is refused: the
        rest are extras."""
        t = self.target(to)
        h = self.holder(**to)
        qty = int(qty or 1)
        scan_id = (scan_id or '')[:40] or None
        with self._lock:
            dup = self.wms._q1("SELECT id, item_id FROM audit_count WHERE batch = ? AND kind = 'found'", (scan_id,)) if scan_id else None
            if dup:
                it = self.audit.item(dup['item_id'])
            else:
                it = self.audit.item(int(item_id))
                if it['accounted'] >= it['expected']:
                    raise WmsError(f"All {it['expected']} are already accounted for. Add it as an extra.")
                left = it['expected'] - it['accounted']
                if not 1 <= qty <= left:
                    raise WmsError(f"Only {left} left to find on this row. Add more than that as extras.")
                it = self.audit.found(it['id'], qty, h, user_id, device,
                                      note='Sorted by scan' if qty == 1 else f'Sorted: {qty} counted', batch=scan_id)
                remembered = self._remember_code(it['product_id'], code)
        cid = self.wms._q1("SELECT MAX(id) AS id FROM audit_count WHERE item_id = ? AND kind = 'found'"
                           + (' AND batch = ?' if scan_id else ''), (it['id'],) + ((scan_id,) if scan_id else ()))['id']
        if not dup and remembered and cid:
            with self.wms._tx() as c:
                c.execute('INSERT OR REPLACE INTO scan_code (count_id, product_id, kind, value) VALUES (?,?,?,?)',
                          (cid, it['product_id'], remembered['kind'], remembered['value']))
        saved = self.wms._q1('SELECT COALESCE(SUM(qty), 0) AS n FROM audit_count WHERE item_id = ? AND batch = ?',
                             (it['id'], scan_id))['n'] if scan_id else qty
        t = self.target(h)
        return {'kind': 'found', 'item': self._slim(it), 'count_id': cid, 'qty': saved or qty, 'repeat': bool(dup),
                'here': self._here(h, it['product_id']), 'target': t, 'info': info,
                'elsewhere': self.elsewhere(it['product_id'], h), 'hint': self._hint([it], bin_code=t['code'])}

    def elsewhere(self, product_id, to: dict) -> list:
        """Other boxes and shelves that hold this product now (not this one), most units first. join=True: a box or a
        warehouse shelf the units just found can join with one tap; other places are shown for information."""
        if not product_id:
            return []
        h = self.holder(**(to or {}))
        shelves = {r['id'] for r in self.wms._q("""SELECT id FROM location WHERE kind = 'shelf' AND archived = 0 AND site != ''
                                                    AND name != site""")}
        merged = {}
        for w in self.commerce._where(product_id):
            k = (w['box_id'] or 0, 0 if w['box_id'] else w['location_id'])
            if k == (h.get('box_id', 0), h.get('location_id', 0)):
                continue
            if k in merged:
                merged[k]['qty'] += w['qty']
                continue
            merged[k] = {'location_id': None if w['box_id'] else w['location_id'], 'box_id': w['box_id'] or None,
                         'code': (w['box_name'].upper() if w['box_name'] else f"Box {w['box_id']}") if w['box_id'] else w['location_code'],
                         'qty': w['qty'], 'join': bool(w['box_id']) or w['location_id'] in shelves}
        return sorted(merged.values(), key=lambda o: -o['qty'])[:4]

    def join(self, product_id: int, src: dict, dst: dict, qty: int, user_id: int, device: str = '') -> dict:
        """Move units just sorted into this box over to the box that already holds the same item (one ledger move)."""
        s, d = self.target(src), self.target(dst)
        qty = int(qty or 1)
        r = self.wms.move(user_id, [{'product_id': int(product_id), 'qty': qty}], self.holder(**src), self.holder(**dst),
                          note=f"Audit: put with the same item in {d['code']}", device=device)
        return {'action_id': r['action_id'], 'target': self.target(self.holder(**src)), 'to': d['code'], 'qty': qty,
                'from': s['code']}

    def search(self, q: str, limit: int = 6) -> list:
        """Open audit rows whose name fits the typed words: every word in the title (or ASIN, brand, store) first,
        then title-score guesses for 3+ words."""
        words = [x for x in re.split(r'\s+', (q or '').lower().strip()) if x]
        if not words:
            return []
        hits = []
        for r in self.audit._rows():
            if r['accounted'] >= r['expected']:
                continue
            hay = f"{r['title']} {r.get('asin') or ''} {r.get('brand') or ''} {r.get('store') or ''}".lower()
            if all(x in hay for x in words):
                hits.append((2.0, r))
            elif len(words) >= 3:
                sc = title_score(q, r['title'])
                if sc >= GUESS_MIN:
                    hits.append((sc, r))
        hits.sort(key=lambda h: (-h[0], -(h[1]['expected'] - h[1]['accounted'])))
        return [{**self._slim(r), 'score': round(min(sc, 1.0), 2)} for sc, r in hits[:limit]]

    def _remember_code(self, product_id: int, code: str):
        """Put the scanned barcode on the product (when no other product has it), so Orders and Receive find it by
        that barcode and the next scan needs no catalog call."""
        ident = _ident(code or '')
        if not product_id or not ident or ident['kind'] == 'ASIN':
            return None
        if any(p['id'] != product_id for p in self.wms.lookup(ident['value'])):
            return
        with self.wms._tx() as c:
            n = c.execute('INSERT OR IGNORE INTO product_identifier (product_id, kind, value) VALUES (?,?,?)',
                          (product_id, ident['kind'], ident['value'])).rowcount
        return ident if n else None

    def forget_code(self, count_id: int):
        """Undo of a found scan: take off the barcode that scan put on the product (if it did)."""
        r = self.wms._q1('SELECT product_id, kind, value FROM scan_code WHERE count_id = ?', (int(count_id),))
        if not r:
            return
        with self.wms._tx() as c:
            c.execute('DELETE FROM product_identifier WHERE product_id = ? AND kind = ? AND value = ?',
                      (r['product_id'], r['kind'], r['value']))
            c.execute('DELETE FROM scan_code WHERE count_id = ?', (int(count_id),))

    def extra(self, code: str, to: dict, user_id: int, device: str = '', scan_id: str = '', cost_cents=None,
              title: str = '', item_id: int = None) -> dict:
        """One unit nobody expected (not on the list, or more than its row expected) into the box."""
        self.target(to)
        h = self.holder(**to)
        code = (code or '').strip()
        scan_id = (scan_id or '')[:40] or None
        if cost_cents not in (None, ''):
            cost_cents = cents_in(cost_cents)
        else:
            cost_cents = None
        with self._lock:
            dup = self.wms._q1('SELECT id FROM audit_extra WHERE scan_id = ?', (scan_id,)) if scan_id else None
            if not dup:
                pid, row_cost = self._extra_product(code, title, cost_cents, user_id, item_id)
                lines = self.commerce.open_lines_for(pid)
                line = lines[0] if lines else None
                if line:   # bought on an order that is still open (Arriving, store review): it arrived
                    cost, line_id, ref = line['unit_cost_cents'], line['id'], line['order_id'] or 'audit-extra'
                    note = f'Audit extra: not on the list, matched to order {line["order_id"]}'
                else:
                    cost = cost_cents if cost_cents is not None else row_cost
                    line_id, ref = None, 'audit-extra'
                    note = 'Audit extra: more than the list expected' if item_id else 'Audit extra: not on the list'
                    if cost is None:
                        note += ' (cost not entered)'
                r = self.wms.receive(user_id, pid, 1, cost, h, reference=ref, note=note,
                                     device=device, purchase_line_id=line_id)
                with self.wms._tx() as c:
                    c.execute("""INSERT INTO audit_extra (at, user_id, code, product_id, item_id, qty, unit_cost_cents, location_id,
                                 box_id, action_id, purchase_line_id, scan_id, note) VALUES (?,?,?,?,?,1,?,?,?,?,?,?,?)""",
                              (now_utc(), user_id, code[:80], pid, int(item_id) if item_id else None, cost,
                               h.get('location_id'), h.get('box_id'), r['action_id'], line_id, scan_id, note))
            row = self.wms._q1('SELECT * FROM audit_extra WHERE ' + ('scan_id = ?' if scan_id else 'id = (SELECT MAX(id) FROM audit_extra)'),
                               (scan_id,) if scan_id else ())
        ex = self.extras()
        return {'kind': 'extra', 'extra': next((e for e in ex['rows'] if e['id'] == row['id']), row), 'repeat': bool(dup),
                'target': self.target(h), 'extras': {'units': ex['units'], 'rows': len(ex['rows'])}}

    def _extra_product(self, code: str, title: str, cost_cents, user_id: int, item_id):
        """(product id, cost to use when none is typed) for an extra."""
        if item_id:
            it = self.audit.item(int(item_id))
            return self.audit._product_for(it, user_id), it['avg_cost_cents']
        have = [p for p in self.wms.lookup(code) if not p['archived']] if code else []
        hit = None
        if not have and self.catalog:
            try:
                hit = next(iter(self.catalog.lookup(code)), None)   # cached by the scan a moment ago: free
            except WmsError:
                hit = None
            if hit and hit.get('asin'):
                have = [p for p in self.wms.lookup(hit['asin'].upper()) if not p['archived']]
        if have:
            p = have[0]
            d = self.wms._q1('SELECT default_cost_cents FROM product WHERE id = ?', (p['id'],))
            return p['id'], p.get('avg_cost_cents') or d['default_cost_cents']
        ident = _ident(code)
        if hit:
            pid = self.audit._make_product(hit.get('asin') or '', (title or '').strip() or hit['title'], hit.get('brand') or '',
                                           hit.get('image_url') or '', cost_cents, user_id, lookup=bool(hit.get('asin')),
                                           base=f'AUDIT-{code}', extra_idents=[ident] if ident and ident['kind'] != 'ASIN' else [])
            return pid, None
        if not code:
            raise WmsError('Scan the item first')
        name = (title or '').strip() or f'Item {code} (not on the audit list)'
        if ident and ident['kind'] == 'ASIN':
            return self.audit._make_product(code.upper(), name, '', '', cost_cents, user_id), None
        base = code if not ident else f'AUDIT-{code}'
        pid = self.audit._make_product('', name, '', '', cost_cents, user_id, base=base, extra_idents=[ident] if ident else [])
        return pid, None

    # ------------------------------------------------------------------ extras

    def extras(self) -> dict:
        """Every extra still standing (undone ones drop out), newest first."""
        rows = self.wms._q("""
            SELECT e.id, e.at, e.user_id, e.code, e.product_id, e.item_id, e.qty,
                   NULLIF(COALESCE((SELECT CASE WHEN r.cost_missing THEN -1 ELSE r.unit_cost_cents END FROM receipt r
                              WHERE r.action_id = e.action_id AND r.product_id = e.product_id LIMIT 1), e.unit_cost_cents), -1) AS unit_cost_cents,
                   e.location_id,
                   e.box_id, e.action_id, e.purchase_line_id, e.note, p.sku, p.title, p.image_url,
                   COALESCE(UPPER(NULLIF(b.name, '')), CASE WHEN e.box_id THEN 'Box ' || e.box_id END, l.code) AS bin
              FROM audit_extra e JOIN action a ON a.id = e.action_id AND a.reversed_by IS NULL
              JOIN product p ON p.id = e.product_id LEFT JOIN location l ON l.id = e.location_id
              LEFT JOIN box b ON b.id = e.box_id
             ORDER BY e.id DESC""")
        # Review 11-17: units a merge re-booked on the target (found on its row or its buy lines) voided their extra
        # receipt. They are not extra stock any more, so the list leaves them out.
        voided = {r['action_id']: r['n'] for r in self.wms._q(f"""
            SELECT r.action_id, SUM({VOIDED_SQL}) AS n FROM receipt r
             WHERE EXISTS (SELECT 1 FROM receipt_void rv WHERE rv.receipt_id = r.id) GROUP BY r.action_id""")} if self._has('receipt_void') else {}
        for r in rows:
            r['qty'] -= min(r['qty'], voided.get(r['action_id']) or 0)
        # An item merged into another: its units in hand went over as the target's (a 'merge' receipt per place). A
        # later merge that found them on a Stock audit row voids that receipt, and those units stop being extras
        # (review 11-17: A into B into the row showed the found unit as extra too). The voided units come off the
        # extras of the same box or shelf first (run() carries a place's own extras first), then the newest. Extras
        # shipped before the merge stay listed, as they would without it. Listed as the item they are now.
        live = [{'r': r, 'pid': r['product_id'], 'at': (r['box_id'] or 0, 0 if r['box_id'] else (r['location_id'] or 0))}
                for r in rows if r['qty'] > 0]
        for _ in range(10):
            groups = {}
            for st in live:
                m = self._merged_into(st['pid']) if st['r']['qty'] > 0 else None
                if m:
                    groups.setdefault(m['id'], (m, []))[1].append(st)
            if not groups:
                break
            for m, sts in groups.values():
                pool = self._carried_voided(m)
                for same in (True, False):
                    for st in sts:
                        for e in pool:
                            if same and e['at'] != st['at']:
                                continue
                            d = min(st['r']['qty'], e['n'])
                            st['r']['qty'] -= d
                            e['n'] -= d
                for st in sts:
                    st['pid'] = m['target_id']
        items = {}
        for st in live:
            r, pid = st['r'], st['pid']
            if pid != r['product_id'] and r['qty'] > 0:
                p = items.get(pid) or items.setdefault(pid, self.wms._q1('SELECT sku, title, image_url FROM product WHERE id = ?', (pid,)))
                r.update(product_id=pid, **p)
        rows = [r for r in rows if r['qty'] > 0]
        return {'rows': rows, 'units': sum(r['qty'] for r in rows),
                'cost_cents': sum(r['qty'] * (r['unit_cost_cents'] or 0) for r in rows),
                'no_cost': sum(1 for r in rows if r['unit_cost_cents'] is None)}

    def _has(self, table: str) -> bool:
        return bool(self.wms._q1("SELECT 1 AS x FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)))

    def _merged_into(self, pid):
        """The standing merge (merge.py) that took this item's units, else None."""
        if not pid or not self._has('product_merge'):
            return None
        # A merge into the item's own Stock audit row (source = target) keeps the item: nothing went away.
        return self.wms._q1("""SELECT id, target_id, action_ids FROM product_merge WHERE source_id = ? AND target_id != source_id
                                AND undone_at IS NULL ORDER BY id DESC LIMIT 1""", (int(pid),))

    def _carried_voided(self, m: dict) -> list:
        """Per place, units merge m carried over to its target as they were (extra stock, or counted units such as an
        extra received on a buy line: merged_from) that a later merge re-booked (voided): [{at: (box, location), n}]."""
        acts = [int(a) for a in (m['action_ids'] or '').split(',') if a]
        if not acts:
            return []
        return [{'at': (x['box_id'] or 0, 0 if x['box_id'] else (x['location_id'] or 0)), 'n': x['n']} for x in self.wms._q(f"""
            SELECT {VOIDED_SQL} AS n,
                   (SELECT sm.to_box_id FROM stock_move sm WHERE sm.action_id = r.action_id AND sm.product_id = r.product_id
                     ORDER BY sm.id LIMIT 1) AS box_id,
                   (SELECT sm.to_location_id FROM stock_move sm WHERE sm.action_id = r.action_id AND sm.product_id = r.product_id
                     ORDER BY sm.id LIMIT 1) AS location_id
              FROM receipt r WHERE r.action_id IN ({','.join('?' * len(acts))}) AND r.product_id = ? AND r.reference = 'merge'
             ORDER BY r.id""", (*acts, m['target_id'])) if x['n'] > 0]

    def _check_not_merged(self, row):
        """A line whose item was merged into another: its units are that item's now, so a cost or count change here
        would land on the archived item (review 11-17)."""
        m = self._merged_into(row['product_id']) if row else None
        if m:
            t = self.wms._q1('SELECT title FROM product WHERE id = ?', (m['target_id'],)) or {}
            raise WmsError(f"This item was merged into {(t.get('title') or 'another item')[:60]}. "
                           'Its units are that item now: change them on its page.')

    def undo_extra(self, extra_id: int, user_id: int, device: str = '') -> dict:
        row = self.wms._q1('SELECT e.action_id, e.location_id, e.box_id, a.reversed_by FROM audit_extra e JOIN action a ON a.id = e.action_id WHERE e.id = ?',
                           (int(extra_id),))
        if not row:
            raise WmsError('Nothing to undo')
        if row['reversed_by']:
            raise WmsError('Already undone')
        self.wms.undo(user_id, row['action_id'], note='Audit: undo extra', device=device)
        ex = self.extras()
        try:
            t = self.target(self.holder(row['box_id'], row['location_id']))
        except WmsError:   # the box was retired since
            t = None
        return {'target': t, 'extras': {'units': ex['units'], 'rows': len(ex['rows'])}}

    # ------------------------------------------------------------------ add items on a box's page (Alex 11-11 1:10 PM)
    # "When scanning a box it opens the box panel. From there it should be easy to add items to the box by either
    # searching inventory or scanning a upc. If the item we scan isn't anywhere yet it goes through the create product
    # flow and then asks how many to add to the original box scanned." Each add is recorded as what it is.

    labels = None   # labels.Labels, set by the server

    def _label(self, code: str) -> bool:
        """A carrier label no item carries as its barcode."""
        return bool(self.labels and code and not self.wms.lookup(code) and self.labels.is_label(code))

    def _product_row(self, pid):
        if not pid:
            return None
        p = self.wms._q1("""SELECT id, sku, title, image_url, tracking, default_cost_cents, archived,
                                   (SELECT ph.id FROM photo ph WHERE ph.product_id = product.id AND ph.removed = 0
                                     ORDER BY ph.id LIMIT 1) AS photo_id
                              FROM product WHERE id = ?""", (int(pid),))
        if not p or p['archived']:
            return None
        p['avg_cost_cents'] = self.wms._avg_costs(p['id']).get(p['id'])
        return p

    def add_check(self, to: dict, code: str = '', product_id: int = None, item_id: int = None) -> dict:
        """Read only. What an item scanned or picked on a box's page can go in as, best first:
        audit (a Stock audit row with units left: found, which receives against its open order lines first),
        order (bought on an open order line nobody received), move (sitting in another box or on a shelf),
        new (stock on no order and no list). kind 'new_product': nothing knows the code, make the product first.
        kind 'target': a box or shelf label (open that page)."""
        t = self.target(to) if to else {"code": ""}   # no box yet (look): what it is, where it sits
        code = (code or '').strip()
        info = {}
        if code and not product_id and not item_id:
            p = parse_scan(code)
            if p['kind'] in ('box', 'location'):
                return {'kind': 'target', 'type': p['kind'], 'id': p['id']}
            if p['kind'] == 'unit':
                return {'kind': 'wrong', 'message': 'That is a one-off unit label. Move it on the Move page.'}
            if p['kind'] == 'bad_check':
                return {'kind': 'wrong', 'message': 'Misread: the check digit is wrong. Scan again.'}
            code = p['value']
            if self._label(code):   # a carrier label: the box page shows what is inside (labels.py)
                return {'kind': 'label_code', 'code': code}
        rows = []
        if item_id:
            it = self.audit.item(int(item_id))
            rows, pid = [it], it['product_id']
            info = {'title': it['title'], 'image_url': it['image_url']}
        elif product_id:
            pid = int(product_id)
        elif code:
            r = self.resolve(code)
            rows, pid, info = r['rows'], (r['info']['product'] or {}).get('id'), r['info']
        else:
            raise WmsError('Scan the item, or search for it')
        product = self._product_row(pid)
        pid = product['id'] if product else None
        if product_id and not product:
            raise WmsError('That product is not in Boxloft (archived?)')
        if product_id:
            idents = self.wms._q('SELECT kind, value FROM product_identifier WHERE product_id = ?', (pid,))
            rows = self._rows([product], {i['value'].upper() for i in idents if i['kind'] == 'ASIN'}, set())
        open_rows = [x for x in rows if x['accounted'] < x['expected']]
        opts = []
        for x in open_rows:
            left = x['expected'] - x['accounted']
            opts.append({'kind': 'audit', 'item_id': x['id'], 'left': left,
                         'label': f"On the Stock audit list: {left} of {x['expected']} still to find",
                         'sub': (f"Bought at {x['store']}" if x.get('store') else '') or (x.get('asin') or '')})
        if not open_rows:   # an audit row receives against its own order lines, so lines show only with no row
            lines = self.commerce.rank_for_receiving(
                self.commerce.open_lines_for(pid) if pid else (self.commerce.open_lines_for_code(code) if code else []))
            if lines and not pid and not info.get('title'):   # no product yet: the order line names the item
                ln = self.wms._q1('SELECT title, image_url FROM purchase_line WHERE id = ?', (lines[0]['id'],))
                info = {**info, 'title': ln['title'] or '', 'image_url': info.get('image_url') or ln['image_url']}
            if len(lines) > 1:   # Alex 11-11: one pile, several orders of the same item: take them all at once
                total = sum(max(0, ln['left_to_receive']) for ln in lines)
                opts.append({'kind': 'order', 'line_ids': [ln['id'] for ln in lines], 'left': total,
                             'cost_cents': lines[0].get('unit_cost_cents'),
                             'label': f"Bought on {len(lines)} orders: {total} not received yet",
                             'sub': 'Fills them in delivery order; each keeps its own cost'})
            for ln in lines[:3]:
                opts.append({'kind': 'order', 'line_id': ln['id'], 'left': ln['left_to_receive'],
                             'cost_cents': ln.get('unit_cost_cents'),
                             'label': f"Bought on {ln['source']} order {ln['order_id']}: {ln['left_to_receive']} not received yet",
                             'sub': f"Ordered {(ln.get('ordered_at') or '')[:10]}" + (f", {ln['why']}" if ln.get('why') else '')})
        if product and product['tracking'] != 'unit':
            for e in self.elsewhere(pid, to):
                opts.append({'kind': 'move', 'product_id': pid,
                             'from': {'box_id': e['box_id']} if e['box_id'] else {'location_id': e['location_id']},
                             'left': e['qty'], 'label': f"Move from {e['code']}: {e['qty']} there", 'sub': 'The same units, moved here'})
        if product:
            opts.append({'kind': 'new', 'product_id': pid, 'left': None,
                         'cost_cents': product['avg_cost_cents'] or product['default_cost_cents'],
                         'label': 'More stock, not on an order or the list', 'sub': 'Goes on the Stock audit extras with its cost'})
        title = (product or {}).get('title') or info.get('title') or ''
        if not opts:
            return {'kind': 'new_product', 'code': code, 'title': title, 'image_url': info.get('image_url'),
                    'target': {'code': t['code']}}
        return {'kind': 'options', 'code': code, 'title': title,
                'image_url': (product or {}).get('image_url') or info.get('image_url'),
                'photo_id': (product or {}).get('photo_id'),
                'product': {k: product[k] for k in ('id', 'sku', 'title')} if product else None,
                'here': self._here(to, pid) if pid and to else 0, 'options': opts, 'target': {'code': t['code']}}

    def add(self, to: dict, opt: dict, qty: int, user_id: int, device: str = '', scan_id: str = '', code: str = '',
            cost_cents=None, cost_missing: bool = False) -> dict:
        """Put qty units into the box as the option says (see add_check). cost_missing: a 'new' line with no cost goes
        in as not entered, not at the item's avg or usual cost (put_qty, so the Usual unit cost fill can follow it). Each write is checked under the lock
        against what is really there: units left on the audit row or order line, stock in the other box."""
        t = self.target(to)
        h = self.holder(**to)
        qty = int(qty or 0)
        if not 1 <= qty <= 10000:
            raise WmsError('Enter how many (1 or more)')
        opt = opt or {}
        kind = opt.get('kind')
        if kind not in ('audit', 'order', 'move', 'new'):
            raise WmsError('Pick what this is first')
        code = (code or '').strip()[:80]
        scan_id = (scan_id or '')[:40] or None
        here = f"Box page: put into {t['code']}"
        with self._lock:
            dup = self.wms._q1('SELECT * FROM box_add WHERE scan_id = ?', (scan_id,)) if scan_id else None
            if dup:
                return self._added(dup, True)
            row = {'kind': kind, 'qty': qty, 'code': code}
            if kind == 'audit':
                r = self.found(int(opt.get('item_id') or 0), to, user_id, device, scan_id, code=code, qty=qty)
                row.update(product_id=r['item']['product_id'], item_id=r['item']['id'], count_id=r['count_id'])
            elif kind == 'order':   # one order line, or several (line_ids): the oldest fills first
                ids = [int(x) for x in (opt.get('line_ids') or [opt.get('line_id')]) if x]
                if not ids:
                    raise WmsError('Pick the order first')
                pid = self.audit.product_for_purchase_line(ids[0], user_id)['product_id']
                plan, n = [], qty
                for ln in (self.commerce.get_line(i) for i in ids):   # in the order given: rank_for_receiving's
                    take = min(n, max(0, ln['left_to_receive']))
                    if take:
                        plan.append({'purchase_line_id': ln['id'], 'qty': take})
                        n -= take
                if n > 0:
                    raise WmsError(f'{"That order has" if len(ids) == 1 else "Those orders have"} only {qty - n} left to '
                                   'receive. Add the rest as more stock.')
                r = self.commerce.receive_lines(user_id, pid, h, plan, device, note=here)
                row.update(product_id=pid, action_id=r['action_ids'][0], line_id=ids[0],
                           action_ids=','.join(str(a) for a in r['action_ids']))
            elif kind == 'move':
                src = self.holder(**(opt.get('from') or {}))
                if not src or src == h:
                    raise WmsError('Pick the box or shelf the units come from')
                pid = int(opt.get('product_id') or 0)
                r = self.wms.move(user_id, [{'product_id': pid, 'qty': qty}], src, h, note=here, device=device)
                row.update(product_id=pid, action_id=r['action_id'], from_box_id=src.get('box_id'),
                           from_location_id=src.get('location_id'))
            else:
                p = self._product_row(opt.get('product_id'))
                if not p:
                    raise WmsError('That product is not in Boxloft')
                if cost_cents not in (None, ''):
                    cost_cents = cents_in(cost_cents)
                elif not cost_missing:
                    cost_cents = p['avg_cost_cents'] or p['default_cost_cents']
                else:
                    cost_cents = None
                note = f"Box page extra: not on an order or the audit list, put into {t['code']}"
                if cost_cents is None:
                    note += ' (cost not entered)'
                r = self.wms.receive(user_id, p['id'], qty, cost_cents, h, reference='box-add', note=note, device=device)
                with self.wms._tx() as c:
                    c.execute("""INSERT INTO audit_extra (at, user_id, code, product_id, qty, unit_cost_cents, location_id, box_id,
                                 action_id, scan_id, note) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                              (now_utc(), user_id, code, p['id'], qty, cost_cents, h.get('location_id'), h.get('box_id'),
                               r['action_id'], f'box-{scan_id}' if scan_id else None, note))
                row.update(product_id=p['id'], action_id=r['action_id'])
            if kind != 'audit':   # found() puts its own barcode on (scan_code)
                ident = self._remember_code(row['product_id'], code)
                if ident:
                    row.update(ident_kind=ident['kind'], ident_value=ident['value'])
            row.update(at=now_utc(), user_id=user_id, box_id=h.get('box_id'), location_id=h.get('location_id'), scan_id=scan_id)
            cols = sorted(row)
            with self.wms._tx() as c:
                aid = c.execute(f"INSERT INTO box_add ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                [row[k] for k in cols]).lastrowid
            return self._added(self.wms._q1('SELECT * FROM box_add WHERE id = ?', (aid,)), False)

    def _added(self, row: dict, repeat: bool) -> dict:
        p = self.wms._q1('SELECT title, sku FROM product WHERE id = ?', (row['product_id'],)) or {}
        h = self.holder(row['box_id'], row['location_id'])
        return {'kind': 'added', 'add_id': row['id'], 'how': row['kind'], 'qty': row['qty'], 'repeat': repeat,
                'product_id': row['product_id'], 'title': p.get('title', ''), 'sku': p.get('sku', ''),
                'here': self._here(h, row['product_id']), 'target': {'code': self.target(h)['code']}}

    def undo_add(self, add_id: int, user_id: int, device: str = '') -> dict:
        """The Undo a person taps on a box page line. On a line the new item card made (put_made) it also archives that
        item and lets go of its codes, purchase and listing links. Code that only needs the units out before saving
        again (count change, Change) uses _undo_units: calling this there archived Sam's new items and the save after
        it said "That product is not in Boxloft" (Alex 11-16). tests/test_box_line_matrix.py runs every action on
        every kind of line."""
        row = self.wms._q1('SELECT * FROM box_add WHERE id = ?', (int(add_id),))
        if not row:
            raise WmsError('Nothing to undo')
        made = self.wms._q1('SELECT product_id FROM put_made WHERE add_id = ?', (row['id'],))
        if made:
            unmade = self._undo_made(row, made['product_id'], user_id, device)
            self._detach_photo(row['id'])   # after the units: a refused Undo changes nothing (review 11-17)
            return {'qty': row['qty'], 'here': 0, 'unmade': unmade}
        self._undo_units(row, user_id, device)
        self._detach_photo(row['id'])
        return {'qty': row['qty'], 'here': self._here(self.holder(row['box_id'], row['location_id']), row['product_id'])}

    def _undo_units(self, row: dict, user_id: int, device: str = '') -> None:
        """Take a box_add line's units back out and the barcode it put on. The item stays, even one the line made."""
        with self._lock:
            if row['kind'] == 'audit':
                self.audit.undo_found(row['item_id'], row['count_id'], user_id, device)
                self.forget_code(row['count_id'])
            else:
                ids = [int(a) for a in (row.get('action_ids') or '').split(',') if a] or [row['action_id']]
                self.wms.undo_many(user_id, ids, note='Box page: undo add', device=device)
                if row['ident_kind']:
                    with self.wms._tx() as c:
                        c.execute('DELETE FROM product_identifier WHERE product_id = ? AND kind = ? AND value = ?',
                                  (row['product_id'], row['ident_kind'], row['ident_value']))

    # ------------------------------------------------------------------ put away (flow audit 11-12, redesign 1)
    # Receive, the box page's Add card and the audit bar were 3 screens for one act: "this item goes in this box".
    # put() is that act. The person scans; the server picks the record with one rule and saves 1 unit at once:
    # an audit row with units left, else the order the item most likely came on (commerce.rank_for_receiving), else a
    # move when the item sits in exactly 1 other place, else extra stock. It asks (kind 'choose') only when 2 answers
    # are both likely: 2+ audit rows, 2+ other places, or a one-off unit.

    @staticmethod
    def auto_choice(options: list, tracking: str = ''):
        """(option, '') the scan saves as, or (None, the question) when a person has to pick."""
        audits = [o for o in options if o['kind'] == 'audit']
        if len(audits) == 1:
            return audits[0], ''
        if audits:
            return None, f'This barcode fits {len(audits)} rows on the Stock audit list. Which one is it?'
        orders = [o for o in options if o['kind'] == 'order' and o.get('line_id')]
        if orders:
            return orders[0], ''
        if tracking == 'unit':
            return None, 'A one-off item: each one gets its own label. Pick what this is.'
        moves = [o for o in options if o['kind'] == 'move']
        if len(moves) > 1:
            return None, f'It is in {len(moves)} other places. Moved from one of them, or another unit?'
        if moves:
            return moves[0], ''
        new = next((o for o in options if o['kind'] == 'new'), None)
        return (new, '') if new else (None, 'Pick what this is')

    def _said(self, opt: dict, qty: int, r: dict) -> str:
        """The result line's second half: which record the scan went to, in words with numbers."""
        k = opt['kind']
        left = opt.get('left')
        if k == 'audit':
            rest = max(0, (left or 0) - qty)
            return f"Stock audit: {rest} still to find" if rest else 'Stock audit: all found'
        if k == 'order':
            ln = self.wms._q1("""SELECT o.external_id, o.ordered_at, o.source FROM purchase_line l
                                   JOIN purchase_order o ON o.id = l.po_id WHERE l.id = ?""", (opt.get('line_id') or (opt.get('line_ids') or [0])[0],))
            rest = max(0, (left or 0) - qty)
            when = f"order of {(ln['ordered_at'] or '')[5:10].replace('-', '/')}" if ln else 'order'
            why = (opt.get('sub') or '').split(', ', 1)[1] if ', ' in (opt.get('sub') or '') else ''
            why = re.sub(r'20\d\d-(\d\d)-(\d\d)', r'\1/\2', why)
            return f"{when}{f', {why}' if why else ''}. {rest} still to come" if rest else f"{when}{f', {why}' if why else ''}. All in"
        if k == 'move':
            src = (opt.get('label') or '').split(':')[0].replace('Move from ', '')
            return f"moved from {src}, {max(0, (left or 0) - qty)} left there"
        return '1 more than the orders show. Saved as extra' if qty == 1 else f'{qty} more than the orders show. Saved as extra'

    def put(self, to: dict, user_id: int, device: str = '', scan_id: str = '', code: str = '', product_id: int = None,
            item_id: int = None, qty: int = 1, option: dict = None, over_ok: bool = False, replaces: int = None,
            fresh: bool = False) -> dict:
        """One scan (or a search pick) = one save. `option` set: the person picked it on a 'choose' card.
        `fresh`: the item came from Put away's card, which said what a new unit goes in as (`look`, no moves). The box
        picked there gets that, never a move out of the box that holds it (Alex 11-24: "add more of that item to a new
        box"); the 'choose' card still lists the moves.
        Extra stock that takes the item past what was bought asks first (Alex 11-12 1:55 PM, after Sam's test scans of
        one Batmobile made 6 of 3): kind 'over', nothing saved; Yes sends the same body with that option and over_ok.
        Audit rows and order lines never ask (they stop at what is left), and a move adds no stock.
        `replaces`: the box page's "Not this? Change" (Alex 11-16). The earlier line's units come out and this save goes
        in, in one request, and the item stays, even one the new item card made (its own Undo would archive it). An
        'over' answer changes nothing, the earlier line included."""
        qty = int(qty or 1)
        scan_id = (scan_id or '')[:40]
        if scan_id:   # a resend after a lost reply gets the first save, whatever the rule would say now
            dup = self.wms._q1('SELECT * FROM box_add WHERE scan_id = ?', (scan_id,))
            if dup:
                return self._put_result(self._added(dup, True), {'kind': dup['kind']}, dup['qty'], [])
        chk = self.add_check(to, code, product_id, item_id)
        if chk['kind'] == 'label_code':   # logged as in hand, with the card of what is inside
            return self.labels.scan(code, user_id, device) or chk
        if chk['kind'] != 'options':
            return chk
        if option:
            opt = option
        else:
            tracking = (self._product_row((chk.get('product') or {}).get('id')) or {}).get('tracking', '')
            opt, why = self.auto_choice([o for o in chk['options'] if not (fresh and o['kind'] == 'move')], tracking)
            if not opt:
                return {**chk, 'kind': 'choose', 'question': why,
                        'open_sales': self.commerce.open_sales_for((chk.get('product') or {}).get('id'))}
        old = None
        if replaces:
            old = self.wms._q1('SELECT * FROM box_add WHERE id = ?', (int(replaces),))
            if not old or self.holder(old['box_id'], old['location_id']) != self.holder(**to):
                raise WmsError('That line is not in this box. Reload the page.')
        if opt.get('kind') == 'new' and not over_ok:
            # The line it replaces is still in: its stock units are not more.
            more = qty - (old['qty'] if old and old['kind'] != 'move' and old['product_id'] == opt.get('product_id') else 0)
            ask = self.over_card(opt.get('product_id'), more) if more > 0 else {}
            if ask:
                return {**chk, **ask, 'option': opt, 'open_sales': self.commerce.open_sales_for(opt.get('product_id'))}
        # A Change is one transaction: a new save that add() refuses (the other box emptied, the order cancelled or
        # filled meanwhile) rolls the old line's undo back too (census replay c14 + review round 9, 11-17).
        with self._lock, (self.wms._tx() if old else contextlib.nullcontext()):
            made = None
            old_photo = None
            if old:
                made = self.wms._q1('SELECT product_id FROM put_made WHERE add_id = ?', (old['id'],))
                old_photo = self._photo_of(old['id'])
                self._undo_units(old, user_id, device)
            r = self.add(to, opt, qty, user_id, device, scan_id, chk['code'], opt.get('cost_cents') if opt.get('kind') == 'new' else None)
            if made and not r.get('repeat'):
                with self.wms._tx() as c:
                    c.execute('UPDATE put_made SET add_id = ? WHERE add_id = ?', (r['add_id'], old['id']))
            if old and not r.get('repeat'):   # the old line's photo: back on the same item, off another one
                if r.get('product_id') == old['product_id']:
                    self._carry_photo(old_photo, r['add_id'])
                else:   # freed off the wrong item, it goes on this one: the page need not send the token again (census 11-17)
                    self._detach_photo(old['id'])
                    if old_photo and self.photos:
                        rel = self.wms.photo_path(old_photo)
                        self.photos.claim(rel, r.get('product_id'), user_id, r.get('title') or '', r['add_id'])
            if old and self.labels and not r.get('repeat'):
                with self.wms._tx() as c:
                    self.labels.carry(c, old['id'], r['add_id'])
        others = [o for o in chk['options'] if o != opt and not o.get('line_ids')]
        out = self._put_result(r, opt, qty, others, chk)
        if made:
            out['made'] = True
        if old:
            out['replaced'] = old['id']
        return out

    def over_card(self, product_id: int, more: int) -> dict:
        """The question when `more` extra units pass what was bought (commerce.bought_tally); {} when they fit."""
        t = self.commerce.bought_tally(product_id, more)
        if not t or not t['over']:
            return {}
        after = t['in'] + more
        ask = 'Another one?' if more == 1 else f'Put {more} more in?'
        return {'kind': 'over', 'more': more, 'bought': t['bought'], 'after': after,
                'question': f"That makes {after} of {t['bought']} bought. {ask}",
                'detail': f"Now: {self.commerce.tally_words(t)}. If the same one was scanned twice, tap No."}

    def _put_result(self, r: dict, opt: dict, qty: int, others: list, chk: dict = None) -> dict:
        chk = chk or {}
        said = self._said(opt, qty, r)
        # Alex 11-17: the photo as it is after the save. A save that made the item (audit row, order line, new item
        # card) saves its photo too, and the line kept the scan's "no photo yet" for good.
        p = self._product_row(r.get('product_id')) or {}
        out = {**r, 'kind': 'added', 'said': said, 'option': opt, 'others': others,
               'photo_id': p.get('photo_id') or chk.get('photo_id'), 'image_url': p.get('image_url') or chk.get('image_url'),
               'open_sales': self.commerce.open_sales_for(r.get('product_id'))}
        if r.get('how') == 'new' and r.get('add_id'):   # extra stock: its cost shows on the line, with a box to type it
            out['cost_cents'] = self.line_cost(r['add_id'])['cost_cents']
        return out

    # ------------------------------------------------------------------ the cost of a line (Alex 11-17)

    def _line_receipts(self, row: dict) -> list:
        ids = [int(a) for a in (row.get('action_ids') or '').split(',') if a] or ([row['action_id']] if row['action_id'] else [])
        if not ids:
            return []
        return self.wms._q(f"""SELECT r.id, r.qty, CASE WHEN r.cost_missing THEN NULL ELSE r.unit_cost_cents END AS cost_cents
                                  FROM receipt r JOIN action a ON a.id = r.action_id
                                 WHERE r.action_id IN ({','.join('?' * len(ids))}) AND r.product_id = ? AND a.reversed_by IS NULL
                                   AND r.purchase_line_id IS NULL ORDER BY r.id""", (*ids, row['product_id']))

    def line_cost(self, add_id: int) -> dict:
        """Cost each of a box page line that went in as extra stock (no order, no audit row): {cost_cents, who}. who =
        the user id that set it last (a cost typed on the line, or the item's Usual unit cost), for a 409."""
        row = self.wms._q1('SELECT * FROM box_add WHERE id = ?', (int(add_id),))
        recs = self._line_receipts(row) if row and row['kind'] == 'new' else []
        if not recs:
            return {'cost_cents': None, 'who': None}
        last = self.wms._q1("""SELECT user_id FROM (SELECT user_id, at, id, 0 AS k FROM cost_set WHERE receipt_id = ?
                                UNION ALL SELECT user_id, at, id, 1 FROM cost_fill WHERE receipt_id = ?) ORDER BY at DESC, k, id DESC LIMIT 1""",
                            (recs[0]['id'], recs[0]['id']))
        return {'cost_cents': recs[0]['cost_cents'], 'who': last['user_id'] if last else None}

    def _cost_filled(self, row) -> bool:
        """The line's cost came from the item's Usual unit cost (cost_fill) and nobody typed one since (cost_set)."""
        recs = self._line_receipts(row) if row and row['kind'] == 'new' else []
        if not recs:
            return False
        last = self.wms._q1("""SELECT kind FROM (SELECT 'set' AS kind, at, id FROM cost_set WHERE receipt_id = ?
                                UNION ALL SELECT 'fill', at, id FROM cost_fill WHERE receipt_id = ?)
                               ORDER BY at DESC, kind = 'fill', id DESC LIMIT 1""",
                            (recs[0]['id'], recs[0]['id']))
        return bool(last and last['kind'] == 'fill')

    def set_cost(self, add_id: int, cents, user_id: int) -> dict:
        """Alex 11-17: "When scanning an item in that isn't in the audit / linked via an order I have no way of adding a
        buy cost." The line's units (extra stock) take this cost each; blank = not entered. Units on an order or an
        audit row take the order's cost, so those lines refuse."""
        row = self.wms._q1('SELECT * FROM box_add WHERE id = ?', (int(add_id),))
        if not row:
            raise WmsError('That line is not saved')
        if row['kind'] != 'new':
            raise WmsError("These went in on an order or the Stock audit: their cost is the order's.")
        self._check_not_merged(row)
        recs = self._line_receipts(row)
        if not recs:
            raise WmsError('This line was undone. Nothing was saved.')
        r = self.wms.set_receipt_cost([x['id'] for x in recs], cents, user_id)
        return {'add_id': row['id'], 'cost_cents': r['cents'], 'units': sum(x['qty'] for x in recs),
                'avg_cost_cents': self.wms._avg_costs(row['product_id']).get(row['product_id'])}

    def put_qty(self, add_id: int, qty: int, user_id: int, device: str = '', over_ok: bool = False) -> dict:
        """A pile: the saved line becomes `qty` units. Its save is undone and made again as qty, spread over the item's
        open orders (rank order) when it went in on an order. A pile bigger than its audit row or orders have left
        (Alex 11-12: 30 in hand, 25 left) fills them, and the rest goes in as extra stock on a second line
        (`extra_line`), so nobody scans the rest one by one. Past what was bought it asks first (kind 'over')."""
        row = self.wms._q1('SELECT * FROM box_add WHERE id = ?', (int(add_id),))
        if not row:
            raise WmsError('That line is not saved')
        qty = int(qty or 0)
        if not 1 <= qty <= 10000:
            raise WmsError('Type how many (1 or more)')
        self._check_not_merged(row)
        to = self.holder(row['box_id'], row['location_id'])
        filled = refill = False   # filled: goes in as not entered; refill: the Usual unit cost had filled it
        whole = contextlib.ExitStack()
        with self._lock, whole:
            chk = self.add_check(to, product_id=row['product_id']) if row['product_id'] else {'options': []}
            # The options as they stand once this line is undone: its own units count as left again.
            if row['kind'] == 'audit':
                opt = {'kind': 'audit', 'item_id': row['item_id'],
                       'left': next((o['left'] for o in chk['options'] if o.get('item_id') == row['item_id']), 0) + row['qty']}
            elif row['kind'] == 'order':
                lines = self.commerce.receive_lines_for(row['product_id'])
                opt = {'kind': 'order', 'line_ids': [], 'left': sum(ln['left_to_receive'] for ln in lines) + row['qty']}
            elif row['kind'] == 'move':
                src = {'box_id': row['from_box_id']} if row['from_box_id'] else {'location_id': row['from_location_id']}
                there = next((o['left'] for o in chk['options'] if o['kind'] == 'move' and o['from'] == src), 0)
                opt = {'kind': 'move', 'product_id': row['product_id'], 'from': src, 'left': there + row['qty'],
                       'label': f"Move from {self.target(src)['code']}: {there + row['qty']} there"}
            else:
                # A cost the Usual unit cost filled stays filled: the new receipt goes in as not entered and is filled
                # again below, so it keeps following the Usual unit cost (review 11-17). A typed cost carries over.
                refill = self._cost_filled(row)
                typed = None if refill else self.line_cost(row['id'])['cost_cents']
                filled = typed is None   # not entered, or filled by the Usual unit cost: never the avg cost as if typed
                opt = {'kind': 'new', 'product_id': row['product_id'], 'cost_cents': typed}
            left = opt.get('left')
            rest = qty - left if left is not None and qty > left else 0
            if rest and row['kind'] == 'move':
                raise WmsError(f"Only {left} {'is' if left == 1 else 'are'} in {self.target(opt['from'])['code']}. "
                               'Change how many to that, then scan the others: they go in on their own.')
            if (row['kind'] == 'new' or rest) and qty > row['qty'] and not over_ok:   # more extra stock: past bought asks
                ask = self.over_card(row['product_id'], qty - row['qty'])
                if ask:
                    p = self.wms._q1('SELECT title FROM product WHERE id = ?', (row['product_id'],)) or {}
                    where = 'orders' if row['kind'] == 'order' else 'Stock audit row'
                    return {**ask, 'add_id': row['id'], 'qty': qty, 'title': p.get('title', ''),
                            'detail': ask['detail'] + (f' {left} go on its {where}, {rest} as extra.' if rest else '')}
            # A line the new item card made: its Undo would archive the item too (Alex 11-16, Bratz Yasmin: "That
            # product is not in Boxloft" on every count change). Here only the units come out; the item stays, and
            # the new line takes over put_made, so its Undo still takes the item back.
            made = self.wms._q1('SELECT product_id FROM put_made WHERE add_id = ?', (row['id'],))
            old_photo = self._photo_of(row['id'])
            # From here to the end is one transaction: a refused save puts the old line back (review round 9, 11-17).
            whole.enter_context(self.wms._tx())
            self._undo_units(row, user_id, device)
            if opt['kind'] == 'order':   # its own units are back on their order: rank again, then spread the pile
                opt['line_ids'] = [ln['id'] for ln in self.commerce.receive_lines_for(row['product_id'])]
            r = self.add(to, opt, qty - rest, user_id, device, '', row['code'] or '', opt.get('cost_cents') if opt['kind'] == 'new' else None,
                         cost_missing=filled)
            if refill:   # the Usual unit cost fills it again, so it keeps following later edits (blank stays blank)
                p = self.wms._q1('SELECT default_cost_cents FROM product WHERE id = ?', (row['product_id'],))
                if p and p['default_cost_cents']:
                    mine = [x['id'] for x in self._line_receipts(self.wms._q1('SELECT * FROM box_add WHERE id = ?', (r['add_id'],)))]
                    with self.wms._tx() as c:   # this line's receipts only, never the item's other lines
                        Wms._fill_costs(c, row['product_id'], p['default_cost_cents'], user_id, now_utc(), mine)
            out = self._put_result(r, opt, qty - rest, [])
            self._carry_photo(old_photo, r['add_id'])
            if old_photo:   # the answer was built while the pile's undo had it off (review 11-17)
                out['photo_id'] = old_photo
            if made:
                with self.wms._tx() as c:
                    c.execute('UPDATE put_made SET add_id = ? WHERE add_id = ?', (r['add_id'], row['id']))
                out['made'] = True
            if self.labels:   # put from a label's card: the new line keeps its package
                with self.wms._tx() as c:
                    self.labels.carry(c, row['id'], r['add_id'])
            if rest:   # the rest of the pile: extra stock at the item's cost, a line of its own with its own Undo
                p = self._product_row(row['product_id'])
                cost = (p['avg_cost_cents'] or p['default_cost_cents']) if p else None
                new = {'kind': 'new', 'product_id': row['product_id'], 'cost_cents': cost}
                x = self.add(to, new, rest, user_id, device, '', row['code'] or '', cost)
                out['extra_line'] = self._put_result(x, new, rest, [])
                if old_photo:   # the extra line came from the same save: it holds the photo too (review round 7)
                    with self.wms._tx() as c:
                        c.execute('INSERT OR REPLACE INTO box_add_photo (add_id, photo_id) VALUES (?, ?)', (x['add_id'], old_photo))
                    out['extra_line']['photo_id'] = old_photo
        return out

    def look(self, code: str = '', product_id: int = None) -> dict:
        """An item scanned with no box set: what it is, what it would go in as, and the boxes to offer (the box that
        holds it already, else the last-filled box of the market it is listed on). Read only."""
        if code and not product_id:
            p = parse_scan(code)
            if p['kind'] in ('box', 'location'):
                return {'kind': 'target', 'type': p['kind'], 'id': p['id']}
            if p['kind'] in ('unit', 'bad_check'):
                return {'kind': 'wrong', 'message': 'Scan the item barcode, or a box label.' if p['kind'] == 'unit'
                        else 'Misread: the check digit is wrong. Scan again.'}
            code = p['value']
        chk = self.add_check(None, code, product_id)
        if chk['kind'] != 'options':
            return chk
        pid = (chk.get('product') or {}).get('id')
        # With no box there is nothing to move from yet: say what a new unit would go in as.
        opt, why = self.auto_choice([o for o in chk['options'] if o['kind'] != 'move'],
                                    (self._product_row(pid) or {}).get('tracking', ''))
        if opt and opt['kind'] == 'new':
            held = sum(o['left'] or 0 for o in chk['options'] if o['kind'] == 'move')
            opt = {**opt, 'label': f'Not expected: {held} already here. Another one goes in as extra stock' if held
                   else 'Not on an order or the audit list: goes in as extra stock'}
        boxes = [{'box_id': o['from']['box_id'], 'code': o['label'].split(':')[0].replace('Move from ', ''), 'qty': o['left'],
                  'why': f"holds {o['left']} of it"} for o in chk['options'] if o['kind'] == 'move' and o['from'].get('box_id')]
        letter = None
        if pid and self.listings:
            try:
                letter = self.listings.product_listed(pid).get('suggest')
            except Exception:  # noqa: BLE001 - a hint only
                letter = None
        if letter:
            m = self.market_boxes(letter)
            for b in m['boxes'][:2]:
                if all(b['id'] != x['box_id'] for x in boxes):
                    boxes.append({'box_id': b['id'], 'code': b['code'], 'qty': None, 'why': f"last filled {m['name']} box"})
        return {'kind': 'look', 'code': chk['code'], 'title': chk['title'], 'image_url': chk.get('image_url'),
                'photo_id': chk.get('photo_id'), 'product': chk.get('product'),
                'goes_as': opt and {k: opt.get(k) for k in ('kind', 'label', 'sub', 'left')}, 'question': why,
                'boxes': boxes[:3], 'letter': letter, 'open_sales': self.commerce.open_sales_for(pid)}

    # ------------------------------------------------------------------ a new item is a card (redesign 2)
    # A code nothing knows was a 15-input form 3 screens long, even when the catalog had filled it, and from Home it
    # ended on the product page with receiving to start over. Now: a card with the catalog's photo and name, the
    # purchase it was likely bought on, and a look-alike item to add the barcode to. The box scan (or Yes) is the save:
    # the product is made with defaults (owner Boxloft, normal tracking, cost from the order or "not entered") and 1
    # unit goes in, as put() would. One Undo takes back both.

    def new_card(self, code: str, title_hint: str = '') -> dict:
        """Read only (the catalog answer is cached forever). title_hint: a typed name or a photo's read (Alex 11-17,
        no barcode): with no catalog name, its look-alike items and listings show, so Yes never makes a twin unasked."""
        code = (code or '').strip()
        hits = []
        if self.catalog and code:
            try:
                hits = self.catalog.lookup(code)
            except WmsError:
                hits = []
        hit = hits[0] if hits else {}
        asins = tuple(sorted({(h.get('asin') or '').upper() for h in hits if h.get('asin')}))
        lines = self.commerce.rank_for_receiving(self.commerce.open_lines_for_code(code, asins))[:3] if code else []
        title = hit.get('title') or (title_hint or '').strip()[:200]
        twins = []
        if title:
            # Each of the first 3 words on its own (Alex 11-17): a typed "Hand painted" must still find "Hand-painted".
            seen_ids = set()
            pool = []
            for wd in [x for x in re.findall(r'[A-Za-z0-9]+', title) if len(x) > 2 and x.lower() not in _TWIN_STOP][:3]:
                pool += [p for p in self.wms.list_products(search=wd)[:40] if p['id'] not in seen_ids and not seen_ids.add(p['id'])]
            for p in pool:
                if p.get('archived'):
                    continue
                sc = title_score(title, p['title'])
                if sc >= 0.75:
                    twins.append({'id': p['id'], 'title': p['title'], 'sku': p['sku'], 'photo_id': p.get('photo_id'),
                                  'score': round(sc, 2)})
            twins = sorted(twins, key=lambda t: -t['score'])[:2]
        # An item that already holds the catalog's ASIN is this item, whatever its name (else Yes made a 2nd one).
        same = []
        for a in asins:
            for p in self.wms.lookup(a):
                if not p['archived'] and all(p['id'] != t['id'] for t in same):
                    same.append({'id': p['id'], 'title': p['title'], 'sku': p['sku'], 'photo_id': p.get('photo_id'),
                                 'score': 1.0, 'exact': True})
        twins = (same + [t for t in twins if all(t['id'] != s['id'] for s in same)])[:3]
        offers = self.listing_offers(code, asins, title)
        held = {o['product_id'] for o in offers if o['product_id']}
        twins = [t for t in twins if t['id'] not in held]
        delivered = [] if (title or lines) else self.delivered_unscanned()
        return {'kind': 'new_item', 'code': code, 'title': title, 'brand': hit.get('brand') or '',
                'image_url': hit.get('image_url'), 'asin': (hit.get('asin') or '').upper(),
                'lines': [{k: ln.get(k) for k in ('id', 'title', 'order_id', 'source', 'ordered_at', 'left_to_receive',
                                                   'package_left', 'why', 'unit_cost_cents')} for ln in lines],
                'twins': twins, 'listings': offers, 'delivered': delivered}

    MARKET_NAMES = {'amazon': 'Amazon', 'ebay': 'eBay', 'walmart': 'Walmart'}

    def listing_offers(self, code: str, asins: tuple, title: str, limit: int = 3) -> list:
        """The listings a first scan may be (Alex 11-12 3:20 PM: the Hydro Flask had an Amazon FBM listing and the card
        never said so; the Minecraft armadillo's listing was never linked). Exact: the listing's ASIN is the catalog's
        answer, or its barcode is the scanned one. With no ASIN from the catalog (UPCitemdb answered), live or not, a
        listing whose name scores 0.75+ is offered as "looks alike": a person's tap only, never linked by itself (the
        Alex and the Armadillo plush both score 0.91 against the armadillo's barcode name). Read only."""
        if not self.wms._q1("SELECT 1 AS x FROM sqlite_master WHERE type = 'table' AND name = 'listing'"):
            return []
        digits = code.lstrip('0') if code.isdigit() else ''
        rows = self.wms._q("""SELECT l.*, p.title AS product_title, p.image_url AS product_image, p.archived AS product_archived
                                FROM listing l LEFT JOIN product p ON p.id = l.product_id
                               WHERE l.gone_at IS NULL""")
        out = []
        for li in rows:
            asin = (li['asin'] or '').upper()
            match, score = None, 0.0
            if asin and asin in asins:
                match, score = 'asin', 2.0
            elif digits and (li['code'] or '').isdigit() and li['code'].lstrip('0') == digits:
                match, score = 'barcode', 2.0
            elif not asins and title and li['title']:
                s = title_score(title, li['title'])
                if s >= 0.75:
                    match, score = 'name', s
            if not match:
                continue
            live = li['status'] == 'Active' and (li['qty'] or 0) > 0
            pid = li['product_id'] if li['product_id'] and not li['product_archived'] else None
            out.append({'listing_id': li['id'], 'channel': li['channel'], 'market': self.MARKET_NAMES.get(li['channel'], li['channel']),
                        'sku': li['sku'], 'asin': asin, 'title': li['title'], 'fulfillment': li['fulfillment'],
                        'qty': li['qty'], 'live': live, 'match': match, 'score': round(min(score, 1.0), 2),
                        'image_url': li['image_url'] or (li['product_image'] if pid else None),
                        'product_id': pid, 'product_title': li['product_title'] if pid else None, '_rank': (score, 0.0, live)})
        if sum(o['match'] == 'name' for o in out) > 1:
            # Look-alikes tie often (both plush 0.91). Rare shared words weigh more ("armadillo" is on 1 listing,
            # "minecraft" on many): the armadillo's own listing comes first (live 11-12: 0.913 against Alex 0.870).
            df = {}
            for li in rows:
                for wd in _words(li['title'] or ''):
                    df[wd] = df.get(wd, 0) + 1
            idf = lambda wd: math.log((len(rows) + 1) / (df.get(wd, 0) + 1)) + 1   # noqa: E731
            mine = _words(title)
            for o in out:
                if o['match'] == 'name' and mine:
                    o['_rank'] = (o['_rank'][0], sum(idf(wd) for wd in mine & _words(o['title'])) / sum(idf(wd) for wd in mine),
                                  o['_rank'][2])
            out = sorted(out, key=lambda o: (o['match'] != 'name', o['_rank'][1] if o['match'] == 'name' else 0, o['_rank'][0], o['_rank'][2]),
                         reverse=True)
        else:
            out = sorted(out, key=lambda o: o['_rank'], reverse=True)
        out = out[:limit]
        for o in out:
            o.pop('_rank')
            if not o['image_url'] and o['asin'] and o['match'] == 'name' and self.catalog:
                try:   # a photo to tell look-alikes apart: Amazon listings carry none (1 Keepa token, cached forever)
                    o['image_url'] = next((h.get('image_url') for h in self.catalog.lookup(o['asin'])
                                           if (h.get('asin') or '').upper() == o['asin']), None)
                except WmsError:
                    pass
        return out

    def delivered_unscanned(self, limit: int = 8) -> list:
        """Delivered purchase lines no product holds yet, newest delivery first: the list a no-barcode item is picked
        from."""
        return self.wms._q(f"""
            SELECT l.id, l.title, l.image_url, l.unit_cost_cents, o.external_id AS order_id, o.source, o.ordered_at,
                   COALESCE(o.delivered_on, (SELECT MAX(p.delivered_on) FROM purchase_package p WHERE p.line_id = l.id)) AS delivered_on,
                   {LINE_LEFT_SQL} AS left_to_receive
              FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id
             WHERE l.product_id IS NULL AND l.cancelled = 0 AND o.hidden = 0 AND o.kind != 'supplies' AND o.state != 'cancelled'
               AND (o.state = 'delivered' OR EXISTS (SELECT 1 FROM purchase_package p WHERE p.line_id = l.id AND p.state = 'delivered'))
               AND {LINE_LEFT_SQL} > 0
             ORDER BY delivered_on DESC, l.id DESC LIMIT ?""", (int(limit),))

    def put_new(self, to: dict, user_id: int, device: str = '', scan_id: str = '', code: str = '', title: str = '',
                line_id: int = None, listing_id: int = None, product_id: int = None) -> dict:
        """Make the item and put 1 in the box, as one save. line_id: the purchase the person picked (its name, cost and
        ASIN make the product). listing_id: "it's this listing's item" (Alex 11-12): the item is made from the listing
        and linked to it, or, when the listing has an item already, the barcode goes on that item. product_id: an item
        the card offered (same ASIN or a look-alike): the barcode goes on it. Else the catalog's answer for the code,
        else the typed name."""
        self.target(to)   # a retired box stops here, before anything is made
        scan_id = (scan_id or '')[:40]
        if scan_id:
            dup = self.wms._q1('SELECT * FROM box_add WHERE scan_id = ?', (scan_id,))
            if dup:
                return self._put_result(self._added(dup, True), {'kind': dup['kind']}, dup['qty'], [])
        code = (code or '').strip()[:80]
        li = None
        if listing_id:
            li = self.wms._q1("""SELECT l.*, p.archived AS product_archived FROM listing l
                                   LEFT JOIN product p ON p.id = l.product_id WHERE l.id = ?""", (int(listing_id),))
            if not li or li['gone_at']:
                raise WmsError('That listing is gone. Scan the item again.')
            if li['product_id'] and not li['product_archived']:
                product_id = li['product_id']
        if product_id:
            p = self.wms._q1('SELECT id, archived FROM product WHERE id = ?', (int(product_id),))
            if not p or p['archived']:
                raise WmsError('That item is gone. Scan the item again.')
            return self.put(to, user_id, device, scan_id, code, product_id=p['id'])
        with self._lock:
            have = [p for p in self.wms.lookup(code) if not p['archived']] if code else []
            if have:   # the other person made it a moment ago: this is 1 more of it, never a move of their unit
                pid = have[0]['id']
                chk = self.add_check(to, code, pid)
                opt, _ = self.auto_choice([o for o in chk.get('options', []) if o['kind'] != 'move'],
                                          (self._product_row(pid) or {}).get('tracking', ''))
                return self.put(to, user_id, device, scan_id, code, product_id=pid, option=opt)
            option = None
            if line_id:
                made = self.audit.product_for_purchase_line(int(line_id), user_id)
                pid, new = made['product_id'], made['made']
                option = {'kind': 'order', 'line_id': int(line_id),
                          'left': self.commerce.get_line(int(line_id))['left_to_receive'], 'label': 'order'}
            elif li:
                card = self.new_card(code)
                ident = _ident(code)
                asin = (li['asin'] or '').upper() if li['channel'] == 'amazon' else ''
                # Same ASIN as the catalog: ask Keepa for its sizes and barcodes. A look-alike's ASIN may be a
                # multi-item listing (the armadillo's carries another barcode), so only the scanned code goes on.
                pid = self.audit._make_product(asin, li['title'] or card['title'], card['brand'],
                                               card['image_url'] or li['image_url'] or '', None, user_id,
                                               lookup=bool(asin) and asin == card['asin'], base=asin or code or 'ITEM',
                                               extra_idents=[ident] if ident and ident['kind'] != 'ASIN' else [])
                new = True
                if (self.wms._q1('SELECT product_id FROM listing WHERE id = ?', (li['id'],)) or {}).get('product_id') != pid:
                    if self.listings:   # an eBay listing has no ASIN: link() puts its item ID on the product
                        self.listings.link(pid, li['id'], user_id)
                    else:
                        with self.wms._tx() as c:
                            c.execute('UPDATE listing SET product_id = ? WHERE id = ?', (pid, li['id']))
            else:
                card = self.new_card(code)
                name = (title or '').strip() or card['title']
                if not name:
                    raise WmsError('Type what it is, or pick the purchase it came on')
                ident = _ident(code)
                base = card['asin'] or (code if code else 'ITEM')
                pid = self.audit._make_product(card['asin'], name, card['brand'], card['image_url'] or '', None, user_id,
                                               lookup=bool(card['asin']), base=base,
                                               extra_idents=[ident] if ident and ident['kind'] != 'ASIN' else [])
                new = True
            r = self.put(to, user_id, device, scan_id, code, product_id=pid, option=option)
            if new and r.get('kind') == 'added' and not r.get('repeat'):
                with self.wms._tx() as c:
                    c.execute('INSERT OR IGNORE INTO put_made (add_id, product_id) VALUES (?, ?)', (r['add_id'], pid))
                r['made'] = True
                if r.get('how') == 'new':   # not on an order: say so plainly, not "more than the orders show"
                    r['said'] = 'Not on an order'
                # Say which listing the item is now linked to (the card's Link, or by the ASIN): the Hydro Flask's
                # Yes linked its FBM listing and nothing on screen said so (Alex 11-12).
                linked = self.wms._q("""SELECT channel, fulfillment, sku FROM listing WHERE product_id = ? AND gone_at IS NULL
                                         ORDER BY id LIMIT 2""", (pid,)) if self.wms._q1(
                    "SELECT 1 AS x FROM sqlite_master WHERE type = 'table' AND name = 'listing'") else []
                if linked:
                    l0 = linked[0]
                    kind = self.MARKET_NAMES.get(l0['channel'], l0['channel']) + (f" {l0['fulfillment']}" if l0['channel'] == 'amazon' else '')
                    r['said'] = (f"Linked to your {kind} listing {l0['sku']}"
                                 + (f' and {len(linked) - 1} more' if len(linked) > 1 else '') + '. ' + (r.get('said') or ''))
            return r

    def attach_photo(self, r: dict, token: str, user_id: int) -> dict:
        """A save from the Add card's photo (Alex 11-17: "add items that don't have a barcode. Like take a picture"):
        the photo becomes the item's photo when it has none (a new item, or an old one with no picture). A photo goes
        on one item once (PhotoId.attach), so a resent save adds nothing."""
        if not (token and self.photos and isinstance(r, dict) and r.get('kind') == 'added' and r.get('product_id')):
            return r
        if r.get('repeat'):   # a resent save: the first one attached it, or its line was undone since (review 11-17)
            return r
        ph = self.photos.claim(self.photos.path_for(token), r['product_id'], user_id, r.get('title') or '', r.get('add_id'))
        return {**r, 'photo_id': ph['id']} if ph else r

    def _detach_photo(self, add_id: int) -> None:
        """Undo, or a Change to another item (review 11-17): the photo that line put on its item comes off, so the
        item is as it was and the photo can go on the right item."""
        link = self.wms._q1('SELECT photo_id FROM box_add_photo WHERE add_id = ?', (int(add_id),))
        if not link:
            return
        ph = self.wms._q1('SELECT id, path, product_id, removed FROM photo WHERE id = ?', (link['photo_id'],))
        with self.wms._tx() as c:
            c.execute('DELETE FROM box_add_photo WHERE add_id = ?', (int(add_id),))
            if c.execute('SELECT 1 FROM box_add_photo WHERE photo_id = ?', (link['photo_id'],)).fetchone():
                return   # another standing line of this item was saved from the same photo: it stays
            if ph and not ph['removed']:   # unreachable before (indented under the return), census 11-17
                c.execute('UPDATE photo SET removed = 1 WHERE id = ?', (ph['id'],))
                c.execute('INSERT OR IGNORE INTO photo_freed (photo_id) VALUES (?)', (ph['id'],))
                if c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'photo_read'").fetchone():
                    c.execute('UPDATE photo_read SET product_id = NULL, saved_title = NULL WHERE photo = ? AND product_id = ?',
                          (ph['path'], ph['product_id']))

    def _photo_of(self, add_id: int):
        """The photo a line put on its item, while it is still on it (read before the line's units come out: their
        undo takes it off). A photo a person removed on the item page since is None: never brought back (review 11-17)."""
        return (self.wms._q1("""SELECT bp.photo_id FROM box_add_photo bp JOIN photo ph ON ph.id = bp.photo_id
                                WHERE bp.add_id = ? AND ph.removed = 0""", (int(add_id),)) or {}).get('photo_id')

    def _carry_photo(self, photo_id, new_id: int) -> None:
        """A count change or a Change to the same item makes a new line: the photo the old line's undo took off goes
        back on, tied to the new line, so its Undo still takes it."""
        if not photo_id:
            return
        with self.wms._tx() as c:
            ph = c.execute('SELECT path, product_id FROM photo WHERE id = ?', (photo_id,)).fetchone()
            if not ph:
                return
            c.execute('UPDATE photo SET removed = 0 WHERE id = ?', (photo_id,))
            c.execute('DELETE FROM photo_freed WHERE photo_id = ?', (photo_id,))
            if c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'photo_read'").fetchone():
                c.execute('UPDATE photo_read SET product_id = ? WHERE photo = ? AND product_id IS NULL', (ph[1], ph[0]))
            c.execute('INSERT OR REPLACE INTO box_add_photo (add_id, photo_id) VALUES (?, ?)', (new_id, photo_id))

    def _undo_made(self, row: dict, product_id: int, user_id: int, device: str) -> bool:
        """Undo of a new item's first save: the unit comes out, and the item it made goes too (archived, its codes and
        links let go, so a rescan makes it fresh). If anything else happened to the item since, only the unit comes
        out. True when the item went too."""
        mine = [int(a) for a in (row.get('action_ids') or '').split(',') if a] or ([row['action_id']] if row['action_id'] else [])
        with self._lock:
            if row['kind'] == 'audit':
                self.audit.undo_found(row['item_id'], row['count_id'], user_id, device)
                self.forget_code(row['count_id'])
            else:
                self.wms.undo_many(user_id, mine, note='Put away: undo new item', device=device)
            later = self.wms._q1(f"""SELECT COUNT(*) AS n FROM stock_move m JOIN action a ON a.id = m.action_id
                                      WHERE m.product_id = ? AND a.reversed_by IS NULL AND a.reverses_action_id IS NULL
                                        AND a.id NOT IN ({','.join('?' * len(mine)) or '0'})""", [product_id] + mine)['n']
            # Another item merged into this one since (even with no units) made it the kept item: its codes and buy
            # links are the merge's, so the item stays (review 11-17).
            if later or (self._has('product_merge') and self.wms._q1(
                    'SELECT 1 AS x FROM product_merge WHERE target_id = ? AND source_id != target_id AND undone_at IS NULL',
                    (product_id,))):
                return False
            with self.wms._tx() as c:
                c.execute('UPDATE product SET archived = 1, updated_at = ?, updated_by = ? WHERE id = ?', (now_utc(), user_id, product_id))
                c.execute('DELETE FROM product_identifier WHERE product_id = ?', (product_id,))
                for t in ('purchase_line', 'listing'):
                    c.execute(f'UPDATE {t} SET product_id = NULL WHERE product_id = ?', (product_id,))
                if c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'listing_link'").fetchone():
                    c.execute('UPDATE listing_link SET undone_at = ?, undone_by = ? WHERE product_id = ? AND undone_at IS NULL',
                              (now_utc(), user_id, product_id))   # a card's "link" went with the item
                # a line that shipped keeps its item (Fable 11-12 #8: its ledger move names it)
                c.execute('UPDATE sales_line SET product_id = NULL WHERE product_id = ? AND shipped_action_id IS NULL', (product_id,))
            return True

    def add_search(self, q: str) -> dict:
        """Box page search: products by name, SKU or code, then Stock audit rows with no product yet."""
        q = (q or '').strip()[:120]
        if len(q) < 2:
            return {'products': [], 'rows': []}
        products = [{k: p[k] for k in ('id', 'sku', 'title', 'on_hand', 'photo_id')} for p in self.wms.list_products(search=q)[:8]]
        rows = [r for r in self.search(q, limit=12) if not r.get('product_id')][:6]
        return {'products': products, 'rows': rows}

    # ------------------------------------------------------------------ boxes rebuild (Alex 11-11 12:20 PM)

    def bins_to_boxes(self, apply: bool = False, device: str = 'migrate') -> dict:
        """The 'bins' (locations A1-A199, E1-E100 with no building) were boxes all along. For each bin in use: stock in
        it moves (one ledger move, user 0) into the box with the same name at the warehouse, made if missing; then
        the bin is archived. Empty bins are archived. Re-running finds no bins left and changes nothing."""
        bins = [r for r in self.wms._q("""SELECT id, code FROM location WHERE kind = 'shelf' AND archived = 0 AND site = ''
                                          ORDER BY id""") if NAME_RE.match(r['code'].upper())]
        out = {'bins': len(bins), 'with_stock': 0, 'units': 0, 'boxes_made': 0, 'moved': [], 'archived': 0, 'applied': apply}
        for b in bins:
            units = self.wms._q1('SELECT COALESCE(SUM(qty), 0) AS n FROM stock_level WHERE location_id = ? AND box_id = 0 AND qty > 0',
                                 (b['id'],))['n']
            inner = [x['id'] for x in self.wms._q('SELECT id FROM box WHERE location_id = ? AND archived = 0', (b['id'],))]
            if units:
                out['with_stock'] += 1
                out['units'] += units
            if not apply:
                if units:
                    out['moved'].append({'bin': b['code'], 'units': units, 'box_exists': bool(self._box_by_name(b['code']))})
                continue
            if units:
                had = self._box_by_name(b['code'])
                box = self.make_box(b['code'], 0)
                out['boxes_made'] += 0 if had else 1
                self.wms.move_all(0, {'location_id': b['id']}, {'box_id': box['id']},
                                  note=f"Bins are boxes: bin {b['code']} is box {box['code']}", device=device)
                out['moved'].append({'bin': b['code'], 'units': units, 'box': box['code'], 'box_id': box['id']})
            if inner:   # a box someone put "in" a bin sits at the warehouse now
                self.wms.move_boxes(0, inner, self.wms.home_location(), note=f"Bin {b['code']} is gone", device=device)
            self.wms.rename_location(b['id'], archived=True)
            out['archived'] += 1
        return out
