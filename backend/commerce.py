"""Buying, selling and shipping on top of the ledger in wms.py.

- Catalog: product facts by UPC/EAN/ASIN from Keepa (already paid for; 1 token per lookup,
  cached forever), with UPCitemdb's free tier as the fallback. Items with no barcode are
  created from the purchase line they were bought on, plus Sam's own photo. No AI service.
- Purchases: Amazon order history, the arbitrage tracker export, or any retailer CSV become
  purchase lines. Receiving against a line takes its cost and purchase date, so nobody types them.
- Sales: orders imported from a marketplace CSV or added by hand. Each line shows where the
  stock is and which shipping box fits. Shipping it is a wms.remove() to CUSTOMER.
- Shipping boxes: inner sizes and how many are on the shelf.
"""

import csv
import gzip
import io
import json
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import netguard
from codes import make_code
from wms import (LINE_LEFT_SQL, LINE_PRESOLD_SQL, LINE_PULLED_SQL, LINE_RECEIVED_SQL, LINE_RETURNED_SQL, LINE_SHELF_SQL, Wms,
                 WmsError, now_utc)

SCHEMA = """
-- Shipped with none on the shelves (flow audit 11-12, redesign 3): supplier -> customer in one save, tied to the sale
-- (and the Stock audit row when there is one). One row per save; undone_at is set once by its Undo.
CREATE TABLE IF NOT EXISTS ship_unscanned (
    id INTEGER PRIMARY KEY,
    sale_line_id INTEGER NOT NULL,
    at TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    auto INTEGER NOT NULL DEFAULT 0,
    item_id INTEGER,
    action_ids TEXT NOT NULL,
    undone_at TEXT
);
CREATE TABLE IF NOT EXISTS catalog_cache (
    code TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS purchase_order (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    account TEXT NOT NULL DEFAULT '',
    ordered_at TEXT,
    imported_at TEXT NOT NULL,
    UNIQUE (source, external_id)
);
CREATE TABLE IF NOT EXISTS purchase_line (
    id INTEGER PRIMARY KEY,
    po_id INTEGER NOT NULL REFERENCES purchase_order(id),
    line_key TEXT NOT NULL,
    asin TEXT NOT NULL DEFAULT '',
    upc TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    qty INTEGER NOT NULL,
    unit_cost_cents INTEGER NOT NULL,
    product_id INTEGER REFERENCES product(id),
    cancelled INTEGER NOT NULL DEFAULT 0,
    UNIQUE (po_id, line_key)
);
CREATE INDEX IF NOT EXISTS idx_pl_asin ON purchase_line(asin);
CREATE TABLE IF NOT EXISTS sales_order (
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL,
    external_id TEXT NOT NULL,
    ordered_at TEXT,
    ship_by TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'shipped', 'cancelled')),
    note TEXT NOT NULL DEFAULT '',
    imported_at TEXT NOT NULL,
    UNIQUE (channel, external_id)
);
CREATE TABLE IF NOT EXISTS sales_line (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES sales_order(id),
    line_key TEXT NOT NULL,
    sku TEXT NOT NULL DEFAULT '',
    asin TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    qty INTEGER NOT NULL,
    product_id INTEGER REFERENCES product(id),
    shipped_action_id INTEGER,
    ship_box_id INTEGER,
    box_fit TEXT,
    UNIQUE (order_id, line_key)
);
-- Returns to the seller (one row per purchase line; qty is how many of that line went back).
-- state: requested (return started) -> sent (we shipped it back) -> seller_received -> refunded,
-- or no_refund (gone, no money back) or kept (return cancelled: the units are ours again).
CREATE TABLE IF NOT EXISTS purchase_return (
    id INTEGER PRIMARY KEY,
    line_id INTEGER NOT NULL UNIQUE REFERENCES purchase_line(id),
    qty INTEGER NOT NULL CHECK (qty > 0),
    shelf_qty INTEGER NOT NULL DEFAULT 0,   -- of qty, units that were on our shelves (started from stock)
    state TEXT NOT NULL CHECK (state IN ('requested', 'sent', 'seller_received', 'refunded', 'no_refund', 'kept')),
    reason TEXT NOT NULL DEFAULT '',
    status_text TEXT NOT NULL DEFAULT '',
    expected_cents INTEGER NOT NULL DEFAULT 0,
    refund_cents INTEGER NOT NULL DEFAULT 0,
    opened_on TEXT,
    closed_on TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- Every change to a return, in order (who, what Amazon said): the history and the analytics source.
CREATE TABLE IF NOT EXISTS return_event (
    id INTEGER PRIMARY KEY,
    return_id INTEGER NOT NULL REFERENCES purchase_return(id),
    at TEXT NOT NULL,
    user_id INTEGER,
    source TEXT NOT NULL,
    state TEXT NOT NULL,
    qty INTEGER NOT NULL,
    refund_cents INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_return_event ON return_event(return_id);
CREATE TRIGGER IF NOT EXISTS return_event_no_update BEFORE UPDATE ON return_event
BEGIN SELECT RAISE(ABORT, 'return_event is append-only'); END;
CREATE TRIGGER IF NOT EXISTS return_event_no_delete BEFORE DELETE ON return_event
BEGIN SELECT RAISE(ABORT, 'return_event is append-only'); END;
-- Units that were on our shelves and went back: a wms.remove() to VENDOR. Undo the action to take it back.
CREATE TABLE IF NOT EXISTS return_pull (
    id INTEGER PRIMARY KEY,
    return_id INTEGER NOT NULL REFERENCES purchase_return(id),
    action_id INTEGER NOT NULL REFERENCES action(id),
    qty INTEGER NOT NULL CHECK (qty > 0),
    at TEXT NOT NULL,
    user_id INTEGER
);
CREATE TRIGGER IF NOT EXISTS return_pull_no_update BEFORE UPDATE ON return_pull
BEGIN SELECT RAISE(ABORT, 'return_pull is append-only'); END;
CREATE TRIGGER IF NOT EXISTS return_pull_no_delete BEFORE DELETE ON return_pull
BEGIN SELECT RAISE(ABORT, 'return_pull is append-only'); END;
-- Units of a line sold before Boxloft (wms.LINE_PRESOLD_SQL): empty until Audit puts the real view in (audit.PRESOLD_VIEW).
CREATE VIEW IF NOT EXISTS line_presold AS SELECT NULL AS line_id, 0 AS units WHERE 0;
CREATE TABLE IF NOT EXISTS ship_box (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    inner_l_in REAL NOT NULL,
    inner_w_in REAL NOT NULL,
    inner_h_in REAL NOT NULL,
    stock INTEGER NOT NULL DEFAULT 0,
    cost_cents INTEGER,
    archived INTEGER NOT NULL DEFAULT 0
);
"""

MM_PER_IN = 25.4
BOX_PADDING_IN = 0.5      # room for wrap on each dimension
OPEN_LINE_STALE_DAYS = 14
# A purchase of stock: not hidden (Not inventory), not a store email still waiting for a person, not supplies, and
# not an order the seller cancelled (11-11: 60 cancelled Amazon orders, 294 units, $12,193, still counted as bought;
# their lines keep cancelled = 0 because the order state says it, the same rule as Audit._cancelled).
STOCK_ORDER_SQL = "(o.hidden = 0 AND o.kind != 'supplies' AND COALESCE(o.review, '') = '' AND o.state != 'cancelled')"
# Orders whose lines can be received (sweep B1: cancelled orders were offered on scans and Receive). Store orders still
# in review stay receivable: the item is real and may arrive before a person approves the email.
RECEIVABLE_ORDER_SQL = "(o.hidden = 0 AND o.kind != 'supplies' AND o.state != 'cancelled')"


def _cents(v) -> int:
    s = str(v or '').replace('$', '').replace(',', '').strip()
    if not s or s.lower() in ('not available', 'n/a', '-'):
        return 0
    return int(round(float(s) * 100))


def _int(v, default=0) -> int:
    try:
        return int(float(str(v).replace(',', '').strip()))
    except (TypeError, ValueError):
        return default


def _date(v):
    """Many CSV date styles -> ISO date (YYYY-MM-DD), or None."""
    s = (v or '').strip()
    if not s or s.lower() == 'not available':
        return None
    s = s.replace('Z', '+00:00')
    for fmt in (None, '%m/%d/%Y', '%m/%d/%y', '%Y-%m-%d', '%b %d, %Y', '%B %d, %Y', '%d %b %Y', '%m/%d/%Y %H:%M',
                '%m/%d/%Y %I:%M %p', '%b-%d-%y', '%b-%d-%Y'):
        try:
            d = datetime.fromisoformat(s) if fmt is None else datetime.strptime(s, fmt)
            return d.date().isoformat()
        except ValueError:
            continue
    m = re.match(r'(\d{4}-\d{2}-\d{2})', s)
    return m.group(1) if m else None


def _pick(row: dict, *names):
    """First non-empty value among header names (case and spacing ignored)."""
    norm = {re.sub(r'[^a-z0-9]', '', k.lower()): v for k, v in row.items() if k}
    for n in names:
        v = norm.get(re.sub(r'[^a-z0-9]', '', n.lower()))
        if v not in (None, ''):
            return v.strip()
    return ''


def read_csv(text: str) -> list:
    text = text.lstrip('﻿')
    sample = text[:4000]
    delim = '\t' if sample.count('\t') > sample.count(',') else ','
    return list(csv.DictReader(io.StringIO(text), delimiter=delim))


def fits(item_in: list, box_in: list, pad: float = BOX_PADDING_IN) -> bool:
    """Orthogonal fit: sort both, compare dimension by dimension."""
    a = sorted((x + pad for x in item_in), reverse=True)
    b = sorted(box_in, reverse=True)
    return all(x <= y for x, y in zip(a, b))


class Catalog:
    """Keepa first (gives the ASIN that links a scan to Amazon purchases), UPCitemdb free tier second."""

    KEEPA = 'https://api.keepa.com'
    UPCITEMDB = 'https://api.upcitemdb.com/prod/trial/lookup'

    def __init__(self, wms: Wms, photo_dir: str):
        self.wms = wms
        self.photo_dir = photo_dir
        self.keepa_key = os.environ.get('KEEPA_API_KEY', '')

    def _keepa(self, path: str, **params) -> dict:
        if not self.keepa_key:
            raise WmsError('Product lookup is not set up (no Keepa key on the server)')
        url = f'{self.KEEPA}/{path}?' + urllib.parse.urlencode(dict(key=self.keepa_key, domain=1, **params))
        try:
            raw = urllib.request.urlopen(url, timeout=25).read()
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise WmsError('Product lookup is out of Keepa tokens for a minute. Try again shortly.')
            raise WmsError(f'Product lookup failed (Keepa {e.code})')
        except urllib.error.URLError:
            raise WmsError('Product lookup could not reach Keepa')
        return json.loads(gzip.decompress(raw) if raw[:2] == b'\x1f\x8b' else raw)

    @staticmethod
    def _norm(p: dict) -> dict:
        img = None
        imgs = p.get('images') or []
        if imgs and isinstance(imgs[0], dict) and imgs[0].get('l'):
            img = 'https://m.media-amazon.com/images/I/' + imgs[0]['l']
        elif p.get('imagesCSV'):
            img = 'https://m.media-amazon.com/images/I/' + p['imagesCSV'].split(',')[0]
        dims = [p.get('packageLength'), p.get('packageWidth'), p.get('packageHeight')]
        ok = all(isinstance(x, int) and x > 0 for x in dims)
        return {
            'asin': p.get('asin'), 'title': p.get('title') or '', 'brand': p.get('brand') or '',
            'model': p.get('model') or p.get('partNumber') or '', 'image_url': img,
            'upcs': p.get('upcList') or [], 'eans': p.get('eanList') or [],
            'pkg_l_mm': dims[0] if ok else None, 'pkg_w_mm': dims[1] if ok else None, 'pkg_h_mm': dims[2] if ok else None,
            'pkg_weight_g': p.get('packageWeight') if (p.get('packageWeight') or 0) > 0 else None,
        }

    def lookup(self, code: str) -> list:
        """Amazon catalog records for a UPC, EAN or ASIN. Cached forever after the first lookup."""
        code = (code or '').strip().upper()
        if not re.fullmatch(r'\d{8,14}|B0[0-9A-Z]{8}|[0-9]{9}[0-9X]', code):
            return []
        row = self.wms._q1('SELECT data FROM catalog_cache WHERE code = ?', (code,))
        if row:
            return json.loads(row['data'])
        is_asin = not code.isdigit()
        out = []
        if self.keepa_key:
            d = self._keepa('product', **({'asin': code} if is_asin else {'code': code}))
            out = [self._norm(p) for p in d.get('products') or [] if p.get('title')]
        if not out and not is_asin:
            out = self._upcitemdb(code)
        with self.wms._tx() as c:
            c.execute('INSERT OR REPLACE INTO catalog_cache (code, fetched_at, data) VALUES (?,?,?)',
                      (code, now_utc(), json.dumps(out)))
            for p in out:  # an ASIN lookup later costs nothing
                if p.get('asin'):
                    c.execute('INSERT OR IGNORE INTO catalog_cache (code, fetched_at, data) VALUES (?,?,?)',
                              (p['asin'], now_utc(), json.dumps([p])))
        return out

    @staticmethod
    def barcode_forms(p: dict) -> set:
        """The barcodes a scanner can read off this product: each UPC as 12 digits and as EAN-13 (leading 0), each EAN."""
        out = set()
        for u in p.get('upcs') or []:
            u = str(u).strip()
            if u.isdigit() and len(u) == 12:
                out |= {u, '0' + u}
        for e in p.get('eans') or []:
            e = str(e).strip()
            if e.isdigit() and len(e) == 13:
                out.add(e)
                if e.startswith('0'):
                    out.add(e[1:])
        return out

    def prewarm(self, asins: list, batch: int = 100, progress=None) -> dict:
        """Look up ASINs ahead of a scanning day, 100 per Keepa call (1 token each), and cache every UPC/EAN they carry,
        so a scanned barcode finds the ASIN we bought with no call and no wait. A barcode already cached with an answer
        keeps it; an empty cached answer is replaced."""
        todo = []
        for a in dict.fromkeys(x.strip().upper() for x in asins if x):
            if re.fullmatch(r'B0[0-9A-Z]{8}|[0-9]{9}[0-9X]', a) and not self.wms._q1(
                    "SELECT 1 AS x FROM catalog_cache WHERE code = ? AND data != '[]'", (a,)):
                todo.append(a)
        got, codes = 0, {}
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            d = self._keepa('product', asin=','.join(chunk))
            recs = [self._norm(p) for p in d.get('products') or [] if p.get('title')]
            got += len(recs)
            with self.wms._tx() as c:
                for p in recs:
                    c.execute('INSERT OR REPLACE INTO catalog_cache (code, fetched_at, data) VALUES (?,?,?)',
                              (p['asin'], now_utc(), json.dumps([p])))
                    for code in self.barcode_forms(p):
                        codes.setdefault(code, []).append(p)
            if progress:
                progress(f'{min(i + batch, len(todo))} of {len(todo)} ASINs looked up')
        written = 0
        with self.wms._tx() as c:
            for code, recs in codes.items():
                have = c.execute('SELECT data FROM catalog_cache WHERE code = ?', (code,)).fetchone()
                if have and have[0] != '[]':
                    continue
                c.execute('INSERT OR REPLACE INTO catalog_cache (code, fetched_at, data) VALUES (?,?,?)',
                          (code, now_utc(), json.dumps(recs)))
                written += 1
        return {'asins': len(todo), 'found': got, 'with_barcode': sum(1 for p in {id(r): r for v in codes.values() for r in v}.values()),
                'barcodes_cached': written}

    def _upcitemdb(self, code: str) -> list:
        """Free tier: no key, about 100 lookups a day. Title, brand and a photo; sizes when listed."""
        req = urllib.request.Request(f'{self.UPCITEMDB}?upc={code}', headers={'Accept': 'application/json'})
        try:
            d = json.load(urllib.request.urlopen(req, timeout=20))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise WmsError('The free barcode lookup is used up for today. Type the product in.')
            return []
        except (urllib.error.URLError, json.JSONDecodeError):
            return []
        out = []
        for it in d.get('items') or []:
            dims = [float(x) for x in re.findall(r'([\d.]+)\s*X', (it.get('dimension') or '') + 'X')[:3]]
            dims_mm = [round(x * MM_PER_IN) for x in dims] if len(dims) == 3 and 'inch' in (it.get('dimension') or '').lower() else []
            imgs = [u for u in it.get('images') or [] if u.startswith('https://')]
            out.append({'asin': it.get('asin') or None, 'title': it.get('title') or '', 'brand': it.get('brand') or '',
                        'model': it.get('model') or '', 'image_url': imgs[0] if imgs else None,
                        'upcs': [it['upc']] if it.get('upc') else [], 'eans': [it['ean']] if it.get('ean') else [],
                        'pkg_l_mm': dims_mm[0] if dims_mm else None, 'pkg_w_mm': dims_mm[1] if dims_mm else None,
                        'pkg_h_mm': dims_mm[2] if dims_mm else None, 'pkg_weight_g': None, 'source': 'upcitemdb'})
        return [x for x in out if x['title']]

    def save_image(self, product_id: int, url: str, user_id: int):
        """Keep a copy of the Amazon main photo with the product, so it shows on the pick list."""
        try:
            data = netguard.fetch_public(url, 8 * 1024 * 1024, timeout=20)   # public https hosts only (SSRF, 11-17)
        except (urllib.error.URLError, OSError, ValueError):
            return None
        if data[:3] != b'\xff\xd8\xff':
            return None  # JPEG only; anything else is skipped
        sub = datetime.now(timezone.utc).strftime('%Y-%m')
        rel = os.path.join(sub, f'{secrets.token_hex(12)}.jpg')
        os.makedirs(os.path.join(self.photo_dir, sub), exist_ok=True)
        with open(os.path.join(self.photo_dir, rel), 'wb') as f:
            f.write(data)
        return self.wms.add_photo(rel, user_id, product_id=product_id, source='amazon')


BOX_DIMS_RE = re.compile(r'(\d+(?:\.\d+)?)\s*(?:\'\'|"|”|in\.?|inch(?:es)?)?\s*[xX×]\s*(\d+(?:\.\d+)?)\s*(?:\'\'|"|”|in\.?)?'
                         r'\s*[xX×]\s*(\d+(?:\.\d+)?)')


def link_ship_boxes(c, po_id: int):
    """Supply order lines that are boxes ("10''x8''x4'' Corrugated Shipping Boxes") point at their ship box size,
    made if it isn't on the list yet. Other supplies (tape, paper) stay unlinked."""
    for lid, title, cost in c.execute('SELECT id, title, unit_cost_cents FROM purchase_line WHERE po_id = ? AND ship_box_id IS NULL',
                                      (po_id,)).fetchall():
        m = BOX_DIMS_RE.search(title or '')
        if not m or not re.search(r'(?i)box|mailer|carton', title):
            continue
        dims = [float(x) for x in m.groups()]
        want = sorted(dims)
        box = next((b for b in c.execute('SELECT id, inner_l_in, inner_w_in, inner_h_in FROM ship_box WHERE archived = 0').fetchall()
                    if sorted(b[1:]) == want), None)
        if box:
            bid = box[0]
        else:
            bid = c.execute("""INSERT INTO ship_box (name, inner_l_in, inner_w_in, inner_h_in, stock, cost_cents, archived)
                               VALUES (?,?,?,?,0,?,0)""", (' x '.join(f'{d:g}' for d in dims), *dims, cost or None)).lastrowid
        c.execute('UPDATE purchase_line SET ship_box_id = ? WHERE id = ?', (bid, lid))


class Commerce:
    def __init__(self, wms: Wms):
        self.wms = wms
        conn = wms._conn()
        conn.executescript(SCHEMA)
        Wms._add_column(conn, 'sales_line', 'price_cents', 'INTEGER')   # per unit, when known (analytics)
        Wms._add_column(conn, 'purchase_return', 'shelf_qty', 'INTEGER NOT NULL DEFAULT 0')
        Wms._add_column(conn, 'purchase_order', 'kind', "TEXT NOT NULL DEFAULT 'stock'")   # 'supplies': ship boxes etc., not stock
        Wms._add_column(conn, 'purchase_order', 'hidden', 'INTEGER NOT NULL DEFAULT 0')
        Wms._add_column(conn, 'purchase_order', 'review', "TEXT NOT NULL DEFAULT ''")   # store_mail: waiting for a person
        Wms._add_column(conn, 'purchase_order', 'state', "TEXT NOT NULL DEFAULT 'ordered'")   # same as inbound.py
        Wms._add_column(conn, 'purchase_line', 'ship_box_id', 'INTEGER')        # supply line: the box size it fills
        Wms._add_column(conn, 'purchase_line', 'supply_received', 'INTEGER NOT NULL DEFAULT 0')
        Wms._add_column(conn, 'sales_line', 'box_taken', 'INTEGER NOT NULL DEFAULT 0')  # 1 = shipping took it off the box count
        Wms._add_column(conn, 'sales_order', 'closed_at', 'TEXT')   # when it went to shipped here (Shipped tab order, Alex 11-12)
        # Speed sweep 11-12 (Orders 2 s on the droplet): joins and per-row subqueries on these read whole tables.
        conn.execute('CREATE INDEX IF NOT EXISTS idx_pl_po ON purchase_line(po_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_sales_line_order ON sales_line(order_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_return_pull_return ON return_pull(return_id)')
        self.learned_box = None   # ShipBoxes sets it: product_id -> the box that item went out in last time, or None

    # ------------------------------------------------------------------ purchases

    def import_purchases(self, text: str, source_hint: str = '', since: str = None, history_mode: str = 'auto') -> dict:
        """Load a purchase CSV. Formats are told apart by their headers.

        Amazon privacy export (Order History.csv): one row per shipment item; qty and Total Amount
        (tax and shipping included) are summed per order and ASIN. It covers every buy on the account,
        personal ones too, so by default ('auto') it only corrects qty and cost of Amazon orders already
        imported from the tracker; history_mode='add' (or no Amazon orders yet) adds every order since `since`. Tracker export (orders-export.csv):
        COG is the unit cost. Anything else: a header with date, order, item/title, upc or asin, qty, cost."""
        rows = read_csv(text)
        if not rows:
            raise WmsError('That file has no rows')
        heads = {re.sub(r'[^a-z0-9]', '', h.lower()) for h in rows[0].keys() if h}
        lines = {}  # (source, order_id, key) -> dict

        def add(source, order_id, account, date, asin, upc, title, qty, total_cents, unit_cents=None):
            if not order_id or qty <= 0:
                return
            key = asin or upc or re.sub(r'\W+', ' ', title.lower()).strip()[:60]
            if not key:
                return
            k = (source, order_id, key)
            cur = lines.setdefault(k, {'source': source, 'order_id': order_id, 'account': account, 'date': date,
                                        'asin': asin, 'upc': upc, 'title': title, 'qty': 0, 'total': 0})
            cur['qty'] += qty
            cur['total'] += total_cents if unit_cents is None else unit_cents * qty

        if {'asin', 'orderid', 'originalquantity', 'totalamount'} <= heads:
            fmt = 'amazon_history'
            for r in rows:
                if _pick(r, 'Order Status').lower() == 'cancelled':
                    continue
                add('amazon', _pick(r, 'Order ID'), _pick(r, 'Website'), _date(_pick(r, 'Order Date')),
                    _pick(r, 'ASIN').upper(), '', _pick(r, 'Product Name'), _int(_pick(r, 'Original Quantity')),
                    _cents(_pick(r, 'Total Amount')))
        elif {'orderid', 'cog', 'productasin', 'productquantity'} <= heads:
            fmt = 'tracker'
            for r in rows:
                if _pick(r, 'Box Status') in ('Cancelled', 'Returned', 'Refunded'):
                    continue
                add('amazon', _pick(r, 'Order ID'), _pick(r, 'Account Email').split('@')[0], _date(_pick(r, 'Date')),
                    _pick(r, 'Product ASIN').upper(), '', _pick(r, 'Product Title'), _int(_pick(r, 'Product Quantity'), 1),
                    0, unit_cents=_cents(_pick(r, 'COG')))
        else:
            fmt = 'generic'
            for r in rows:
                qty = _int(_pick(r, 'qty', 'quantity', 'units'), 1)
                unit = _pick(r, 'unit cost', 'unit price', 'cost', 'price', 'cog', 'item price')
                total = _pick(r, 'total', 'total amount', 'item total', 'line total')
                add((_pick(r, 'retailer', 'store', 'source', 'vendor', 'supplier', 'website') or source_hint or 'other').lower(),
                    _pick(r, 'order id', 'order number', 'order #', 'order', 'po', 'receipt'), _pick(r, 'account'),
                    _date(_pick(r, 'order date', 'date', 'purchase date')), _pick(r, 'asin').upper(),
                    re.sub(r'\D', '', _pick(r, 'upc', 'ean', 'gtin', 'barcode')),
                    _pick(r, 'title', 'item', 'product name', 'product', 'description', 'name'), qty,
                    _cents(total) if total and not unit else 0, unit_cents=_cents(unit) if unit else None)
        if since:
            lines = {k: v for k, v in lines.items() if (v['date'] or '') >= since}
        if not lines:
            raise WmsError('No purchase lines found. The file needs an order number, an item and a quantity.')
        if fmt == 'amazon_history':
            have_amazon = self.wms._q1("SELECT COUNT(*) n FROM purchase_order WHERE source = 'amazon'")['n']
            if history_mode == 'fix' or (history_mode == 'auto' and have_amazon):
                return self._fix_from_history(lines)
        new_lines = skipped = 0
        spend = 0
        with self.wms._tx() as c:
            for ln in lines.values():
                c.execute("""INSERT OR IGNORE INTO purchase_order (source, external_id, account, ordered_at, imported_at)
                             VALUES (?,?,?,?,?)""", (ln['source'], ln['order_id'], ln['account'], ln['date'], now_utc()))
                po = c.execute('SELECT id FROM purchase_order WHERE source = ? AND external_id = ?',
                               (ln['source'], ln['order_id'])).fetchone()[0]
                key = ln['asin'] or ln['upc'] or re.sub(r'\W+', ' ', ln['title'].lower()).strip()[:60]
                unit = round(ln['total'] / ln['qty']) if ln['qty'] else 0
                cur = c.execute("""INSERT OR IGNORE INTO purchase_line (po_id, line_key, asin, upc, title, qty, unit_cost_cents)
                                   VALUES (?,?,?,?,?,?,?)""", (po, key, ln['asin'], ln['upc'], ln['title'][:300], ln['qty'], unit))
                if cur.rowcount:
                    new_lines += 1
                    spend += unit * ln['qty']
                else:
                    skipped += 1
        return {'format': fmt, 'rows': len(rows), 'new_lines': new_lines, 'already_had': skipped, 'spend_cents': spend}

    def _fix_from_history(self, lines: dict) -> dict:
        """Amazon's own numbers win over the tracker's for orders both files have."""
        fixed = same = 0
        with self.wms._tx() as c:
            for ln in lines.values():
                row = c.execute("""SELECT l.id, l.qty, l.unit_cost_cents FROM purchase_line l
                                      JOIN purchase_order o ON o.id = l.po_id
                                     WHERE o.source = 'amazon' AND o.external_id = ? AND l.asin = ?""",
                                (ln['order_id'], ln['asin'])).fetchone()
                if not row:
                    continue
                unit = round(ln['total'] / ln['qty'])
                if (row[1], row[2]) == (ln['qty'], unit):
                    same += 1
                    continue
                received = c.execute("""SELECT COALESCE(SUM(r.qty), 0) FROM receipt r JOIN action a ON a.id = r.action_id
                                          WHERE r.purchase_line_id = ? AND a.reversed_by IS NULL""", (row[0],)).fetchone()[0]
                c.execute('UPDATE purchase_line SET qty = ?, unit_cost_cents = ? WHERE id = ?',
                          (max(ln['qty'], received), unit, row[0]))
                fixed += 1
        return {'format': 'amazon_history', 'mode': 'fix', 'rows': len(lines), 'corrected': fixed, 'already_right': same,
                'new_lines': 0, 'note': 'Only orders already imported were checked. Personal buys were left out.'}

    def close_before(self, date: str) -> int:
        """Close open lines ordered before a date (bought long ago, sold before Boxloft existed)."""
        if not _date(date):
            raise WmsError('Pick a date')
        rec = self._received_sql()
        with self.wms._tx() as c:
            cur = c.execute(f"""UPDATE purchase_line SET cancelled = 1 WHERE cancelled = 0 AND id IN (
                                  SELECT l.id FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id
                                   WHERE o.ordered_at < ? AND {LINE_LEFT_SQL} > 0)""", (_date(date),))
            return cur.rowcount

    def add_purchase(self, data: dict) -> dict:
        order_id = (data.get('order_id') or '').strip() or f'manual-{secrets.token_hex(4)}'
        qty = _int(data.get('qty'), 0)
        if qty <= 0 or not (data.get('title') or data.get('asin') or data.get('upc')):
            raise WmsError('Enter the item and a quantity')
        unit = int(data.get('unit_cost_cents') or 0)
        text = 'order id,retailer,date,asin,upc,title,qty,unit cost\n'
        buf = io.StringIO()
        csv.writer(buf).writerow([order_id, (data.get('source') or 'other').lower(), data.get('date') or '',
                                  (data.get('asin') or '').upper(), data.get('upc') or '', data.get('title') or '',
                                  qty, f'{unit / 100:.2f}'])
        return self.import_purchases(text + buf.getvalue())

    def _received_sql(self) -> str:
        return LINE_RECEIVED_SQL

    def list_purchases(self, status: str = 'open', q: str = '', limit: int = 300) -> dict:
        # Stock only (Alex 11-11: "12,000 units for $95,000 is under $10 a unit"): orders hidden as Not inventory,
        # store emails nobody approved yet, and supplies (The Boxery's 9,800 ship boxes) are not purchases of stock.
        # Supplies spend is reported on its own line so it stays visible.
        where, params = ['l.cancelled = 0', STOCK_ORDER_SQL], []
        rec = self._received_sql()
        if status == 'open':
            where.append(f'{LINE_LEFT_SQL} > 0')
        elif status == 'received':
            where.append(f'{LINE_LEFT_SQL} <= 0')
        elif status == 'returned':
            where.append(f'{LINE_RETURNED_SQL} > 0')
        for word in q.split()[:8]:   # every word has to match, in any order ("dunkin 128")
            where.append('(l.title LIKE ? OR l.asin = ? OR l.upc = ? OR o.external_id LIKE ?)')
            params += [f'%{word}%', word.upper(), word, f'%{word}%']
        rows = self.wms._q(f"""
            SELECT l.id, l.asin, l.upc, l.title, l.qty, l.unit_cost_cents, l.product_id, {rec} AS received,
                   {LINE_RETURNED_SQL} AS returned, {LINE_LEFT_SQL} AS left_to_receive,
                   (SELECT pr.state FROM purchase_return pr WHERE pr.line_id = l.id) AS return_state,
                   o.source, o.external_id AS order_id, o.account, o.ordered_at
              FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id
             WHERE {' AND '.join(where)}
             ORDER BY o.ordered_at DESC, l.id DESC LIMIT ?""", params + [min(limit, 2000)])
        totals = self.wms._q1(f"""
            SELECT COUNT(*) lines, COALESCE(SUM(l.qty), 0) bought, COALESCE(SUM(l.qty * l.unit_cost_cents), 0) spend,
                   COALESCE(SUM({rec}), 0) received, COALESCE(SUM({LINE_RETURNED_SQL}), 0) returned,
                   COALESCE(SUM({LINE_PRESOLD_SQL}), 0) presold,
                   COALESCE(SUM(CASE WHEN {LINE_LEFT_SQL} > 0 AND o.ordered_at < date('now', '-{OPEN_LINE_STALE_DAYS} days')
                                     THEN {LINE_LEFT_SQL} ELSE 0 END), 0) stale_units
              FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id WHERE l.cancelled = 0 AND {STOCK_ORDER_SQL}""")
        totals['supplies'] = self.wms._q1("""
            SELECT COUNT(DISTINCT o.id) orders, COALESCE(SUM(l.qty), 0) units, COALESCE(SUM(l.qty * l.unit_cost_cents), 0) spend
              FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id
             WHERE l.cancelled = 0 AND o.hidden = 0 AND o.kind = 'supplies'""")
        return {'lines': rows, 'totals': totals, 'stale_days': OPEN_LINE_STALE_DAYS}

    def get_line(self, line_id: int) -> dict:
        rec = self._received_sql()
        row = self.wms._q1(f"""SELECT l.*, {rec} AS received, {LINE_RETURNED_SQL} AS returned, {LINE_LEFT_SQL} AS left_to_receive,
                                      o.source, o.external_id AS order_id, o.account, o.ordered_at, o.state AS order_state
                                 FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id WHERE l.id = ?""", (line_id,))
        if not row:
            raise WmsError('Purchase line not found')
        return row

    def open_lines_for(self, product_id: int, asins: tuple = ()) -> list:
        """Purchase lines that this product can be received against, oldest first. asins: ASINs to count as the
        product's too (merge preview: the ASIN a merge into an audit row would give it)."""
        idents = self.wms._q('SELECT kind, value FROM product_identifier WHERE product_id = ?', (product_id,))
        asins = list(dict.fromkeys([i['value'].upper() for i in idents if i['kind'] == 'ASIN'] + [a.upper() for a in asins if a]))
        upcs = [re.sub(r'^0+', '', i['value']) for i in idents if i['kind'] in ('UPC', 'EAN', 'GTIN')]
        conds, params = ['l.product_id = ?'], [product_id]
        if asins:
            conds.append(f"l.asin IN ({','.join('?' * len(asins))})")
            params += asins
        if upcs:
            conds.append(f"LTRIM(l.upc, '0') IN ({','.join('?' * len(upcs))})")
            params += upcs
        rec = self._received_sql()
        return self.wms._q(f"""
            SELECT l.id, l.asin, l.upc, l.title, l.qty, l.unit_cost_cents, {rec} AS received,
                   {LINE_RETURNED_SQL} AS returned, {LINE_LEFT_SQL} AS left_to_receive,
                   o.source, o.external_id AS order_id, o.account, o.ordered_at
              FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id
             WHERE l.cancelled = 0 AND {RECEIVABLE_ORDER_SQL} AND {LINE_LEFT_SQL} > 0 AND ({' OR '.join(conds)})
               AND (l.product_id IS NULL OR l.product_id = ? OR l.product_id IN (SELECT id FROM product WHERE archived = 1))
             ORDER BY o.ordered_at, l.id""", params + [product_id])

    def bought_tally(self, product_id: int, qty: int = 1):
        """Units bought here against units that came in, for the "past what was bought" ask (Sam 11-12: one Batmobile
        scanned and typed in until on hand said 6, of 3 bought). Bought = the product's lines (by product or ASIN, not
        cancelled) less returns, as LINE_LEFT_SQL counts them. In = on hand + units shipped, sent to FBA/WFS or
        damaged (their virtual places' balance; count fixes stay out, so a Count down never makes every later scan
        ask) + units pulled for a return + Amazon sales of units nobody scanned in (line_presold). None when nothing
        was bought here (eBay-made items, older stock): there is nothing to compare with. `over` = in + qty passes
        bought. The presold view (~75 ms) is read only when it can change the answer."""
        if not product_id:
            return None
        asins = [r['value'].upper() for r in self.wms._q(
            "SELECT value FROM product_identifier WHERE product_id = ? AND kind = 'ASIN'", (int(product_id),))]
        conds, params = ['l.product_id = ?'], [int(product_id)]
        if asins:
            conds.append(f"UPPER(l.asin) IN ({','.join('?' * len(asins))})")
            params += asins
        lines = self.wms._q(f"""SELECT l.id, UPPER(l.asin) AS asin, l.qty - {LINE_RETURNED_SQL} + {LINE_SHELF_SQL} AS n, {LINE_PULLED_SQL} AS pulled
                                  FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id
                                 WHERE l.cancelled = 0 AND {RECEIVABLE_ORDER_SQL} AND ({' OR '.join(conds)})""", params)
        bought = sum(max(0, ln['n']) for ln in lines)
        if bought <= 0:
            return None
        lv_sql = """SELECT COALESCE(SUM(CASE WHEN s.box_id != 0 OR l.kind != 'virtual' THEN s.qty END), 0) AS have,
                           COALESCE(SUM(CASE WHEN s.box_id = 0 AND l.code IN ('CUSTOMER', 'FBA', 'WFS', 'DAMAGED')
                                             THEN s.qty END), 0) AS gone
                      FROM stock_level s LEFT JOIN location l ON l.id = s.location_id WHERE s.product_id = ?"""
        lv = self.wms._q1(lv_sql, (int(product_id),))
        have, gone = lv['have'], lv['gone']
        # Items merged into this one (merge.py): units that left as them count only as far as they came in on buy
        # lines, or as counted units an earlier merge carried into them (review 11-17: a source with no buys that
        # shipped 3 made the target's first real units ask; a chain A -> B -> C lost what B shipped).
        for src in self._merged_into(int(product_id)):
            sv = self.wms._q1(lv_sql, (src,))
            on_lines = self.wms._q1("""SELECT COALESCE(SUM(r.qty), 0) AS n FROM receipt r JOIN action a ON a.id = r.action_id
                                        WHERE r.product_id = ? AND (r.purchase_line_id IS NOT NULL OR r.merged_from IS NOT NULL)
                                          AND a.reversed_by IS NULL""",
                                    (src,))['n']
            # Its buy-line units still in hand went to this item as counted units (receipt.merged_from) and sit in
            # `have` already: only the rest of them can have left (review 11-17: counted twice).
            moved = self.wms._q1("""SELECT COALESCE(SUM(r.qty), 0) AS n FROM receipt r JOIN action a ON a.id = r.action_id
                                     WHERE r.merged_from = ? AND a.reversed_by IS NULL""", (src,))['n']
            # A Count up (ADJUST) is in hand too: units the merge carried over as counted may be those, not bought ones
            # (review 11-17: a found-by-count unit moved over hid a shipped bought one).
            adj = self.wms._q1("""SELECT COALESCE(SUM(s.qty), 0) AS n FROM stock_level s JOIN location l ON l.id = s.location_id
                                   WHERE s.product_id = ? AND s.box_id = 0 AND l.code = 'ADJUST'""", (src,))['n']
            have += max(0, sv['have'])
            # Of the units moved over, at most the count-ups were not bought; the rest were buy-line units. Only buy-line
            # units not moved and not in hand can have shipped (review 11-17: count-ups never stand in for bought ones).
            gone += min(max(0, sv['gone']), max(0, on_lines - max(0, moved - max(0, -adj)) - max(0, sv['have'])))
        # Clipped once, over this item and its merged ones: a buyer return of a source's sale comes back as this item
        # (CUSTOMER -1 here, +1 on the source), so the two cancel (review 11-17: clipped apart, it counted twice).
        gone = max(0, gone) + sum(ln['pulled'] for ln in lines)
        presold = 0
        asins = sorted(set(asins) | {ln['asin'] for ln in lines if ln['asin']})
        if asins and have + gone + qty <= bought and self.wms._q1(f"""
                SELECT 1 AS x FROM sales_line sl JOIN sales_order so ON so.id = sl.order_id
                 WHERE so.channel = 'amazon' AND so.status = 'shipped' AND sl.shipped_action_id IS NULL
                   AND UPPER(sl.asin) IN ({','.join('?' * len(asins))}) LIMIT 1""", asins):
            ids = [ln['id'] for ln in lines]
            presold = self.wms._q1(f"SELECT COALESCE(SUM(units), 0) AS n FROM line_presold WHERE line_id IN ({','.join('?' * len(ids))})",
                                   ids)['n']
        came = have + gone + presold
        return {'bought': bought, 'have': have, 'gone': gone, 'presold': presold, 'in': came, 'room': bought - came,
                'qty': qty, 'over': came + qty > bought}

    def _merged_into(self, product_id: int) -> list:
        """Items folded into this one by merges that stand (and items folded into those), oldest first."""
        if not self.wms._q1("SELECT 1 AS x FROM sqlite_master WHERE type = 'table' AND name = 'product_merge'"):
            return []
        return [r['id'] for r in self.wms._q("""
            WITH RECURSIVE src(id) AS (
                SELECT source_id FROM product_merge WHERE target_id = ? AND undone_at IS NULL AND source_id != target_id
                UNION SELECT m.source_id FROM product_merge m JOIN src ON m.target_id = src.id
                 WHERE m.undone_at IS NULL AND m.source_id != m.target_id)
            SELECT id FROM src WHERE id != ?""", (product_id, product_id))]

    @staticmethod
    def tally_words(t: dict) -> str:
        """Where the units went, in words: '3 in stock, 1 shipped, 1 sold on Amazon before it was scanned in'."""
        parts = [f"{t['have']} in stock"]
        if t['gone']:
            parts.append(f"{t['gone']} shipped or gone")
        if t['presold']:
            parts.append(f"{t['presold']} sold on Amazon before {'it was' if t['presold'] == 1 else 'they were'} scanned in")
        return ', '.join(parts)

    def over_receive(self, product_id: int, qty: int) -> str:
        """A receive with no order line that takes stock past what was bought asks first. '' when it fits, or nothing
        was bought here."""
        if not product_id or qty <= 0:
            return ''
        t = self.bought_tally(product_id, qty)
        if not t or not t['over']:
            return ''
        return (f"You bought {t['bought']} of these: {self.tally_words(t)}. Receiving {qty} more makes {t['in'] + qty}. "
                "If the same one was scanned or typed in twice, use Undo on its history line instead.")

    def rank_for_receiving(self, lines: list) -> list:
        """The one rule for "which order is the item in my hand from" (flow audit 11-12, finding 1: a Beyblade scan
        took a 10-06 order with 12 left while Arriving's card for the same item said 4 from the 11-03 package).
        Order: a package delivered in Arriving's window (oldest delivery first), then one delivered before it, then an
        order nobody read a delivery for (CSV imports, store emails), then one still on the way. Each line gets
        `package_left` (units of that kind still to scan in: delivered units, else all left), `delivered_on` and
        `why`. The scan, the Receive form, Arriving's button and the box page all use this order; a scan is 1 unit.
        Stock audit receipts keep plain oldest-first (open_lines_for): their numbers depend on it."""
        from inbound import DELIVERED_DAYS, portions, today_et   # inbound imports only wms: no cycle
        if not lines:
            return []
        ids = [int(ln['id']) for ln in lines]
        marks = ','.join('?' * len(ids))
        pk, orders = {}, {}
        for p in self.wms._q(f"""SELECT line_id, pkg, qty, state, status_text, expected_from, expected_to, delivered_on
                                   FROM purchase_package WHERE line_id IN ({marks}) ORDER BY pkg""", ids):
            pk.setdefault(p['line_id'], []).append(p)
        for o in self.wms._q(f"""SELECT l.id AS line_id, o.state, o.status_text, o.expected_from, o.expected_to,
                                        o.delivered_on, o.last_seen_at
                                   FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id WHERE l.id IN ({marks})""", ids):
            orders[o['line_id']] = o
        recent = (today_et() - timedelta(days=DELIVERED_DAYS)).isoformat()
        out = []
        for ln in lines:
            o = orders.get(ln['id']) or {}
            left = max(0, ln.get('left_to_receive') or 0)
            parts = portions(ln.get('qty') or left, left, pk.get(ln['id'], []), o) if o else []
            done = [p for p in parts if p['state'] == 'delivered']
            got = sum(p['qty'] for p in done)
            on = min((p['delivered_on'] or '' for p in done), default='') or None
            if got and on and on >= recent:
                rank, why = 0, f'delivered {on}'
            elif got:
                rank, why = 1, f'delivered {on}' if on else 'delivered'
            elif not o.get('last_seen_at') and not pk.get(ln['id']):
                rank, why = 2, 'no delivery news'
            else:
                rank, why = 3, 'not delivered yet'
            out.append({**ln, 'package_left': got if got else left, 'delivered_on': on, 'why': why, 'rank': rank})
        out.sort(key=lambda x: (x['rank'], x['delivered_on'] or '', x.get('ordered_at') or '', x['id']))
        return out

    def open_sales_for(self, product_id) -> list:
        """Orders we ship that want this item and nothing here shipped yet, ship-by first (flow audit 11-12, finding
        9: a Calvin Klein tee sold with ship by 11-13 went into a box and nothing said so). Matches the product, else
        its SKU or ASIN. Read-only."""
        if not product_id:
            return []
        p = self.wms._q1('SELECT sku FROM product WHERE id = ?', (int(product_id),))
        if not p:
            return []
        asins = [r['value'].upper() for r in self.wms._q(
            "SELECT value FROM product_identifier WHERE product_id = ? AND kind = 'ASIN'", (int(product_id),))]
        conds, params = ['l.product_id = ?', 'UPPER(l.sku) = UPPER(?)'], [int(product_id), p['sku']]
        if asins:
            conds.append(f"UPPER(l.asin) IN ({','.join('?' * len(asins))})")
            params += asins
        return self.wms._q(f"""SELECT o.id AS order_id, o.channel, o.external_id, o.ordered_at, o.ship_by, l.id AS line_id, l.qty
                                 FROM sales_line l JOIN sales_order o ON o.id = l.order_id
                                WHERE o.status = 'open' AND l.shipped_action_id IS NULL AND o.fulfilled_by != 'amazon'
                                  AND ({' OR '.join(conds)})
                                ORDER BY COALESCE(o.ship_by, o.ordered_at), l.id""", params)

    def receive_lines_for(self, product_id: int) -> list:
        """Open lines for Receive, the box page and Arriving, in rank_for_receiving's order."""
        return self.rank_for_receiving(self.open_lines_for(product_id))

    def product_links(self, product_id: int) -> dict:
        """Everything tied to one product, for its page (Alex 11-12: the Crocs page showed none of its buy, sales or
        audit row). Buys and sales match the product, else its exact ASIN or UPC, the same codes Receive uses.
        Read-only. `asins` lets the caller find the product's Stock audit rows."""
        idents = self.wms._q('SELECT kind, value FROM product_identifier WHERE product_id = ?', (product_id,))
        asins = sorted({i['value'].upper() for i in idents if i['kind'] == 'ASIN'})
        upcs = sorted({re.sub(r'^0+', '', i['value']) for i in idents if i['kind'] in ('UPC', 'EAN', 'GTIN')} - {''})
        conds, params = ['l.product_id = ?'], [product_id]
        if asins:
            conds.append(f"(l.product_id IS NULL AND UPPER(l.asin) IN ({','.join('?' * len(asins))}))")
            params += asins
        if upcs:
            conds.append(f"(l.product_id IS NULL AND l.upc != '' AND LTRIM(l.upc, '0') IN ({','.join('?' * len(upcs))}))")
            params += upcs
        rec = self._received_sql()
        buys = self.wms._q(f"""
            SELECT l.id, l.qty, l.unit_cost_cents, l.cancelled, {rec} AS received, {LINE_RETURNED_SQL} AS returned,
                   {LINE_PRESOLD_SQL} AS presold, MAX(0, {LINE_LEFT_SQL}) AS left_to_receive,
                   o.source, o.external_id AS order_id, o.ordered_at, o.state, o.hidden, o.kind
              FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id
             WHERE ({' OR '.join(conds)}) AND o.kind != 'supplies'
             ORDER BY o.ordered_at DESC, l.id DESC""", params)
        sconds, sparams = ['l.product_id = ?'], [product_id]
        if asins:
            sconds.append(f"(l.product_id IS NULL AND UPPER(l.asin) IN ({','.join('?' * len(asins))}))")
            sparams += asins
        sales = self.wms._q(f"""
            SELECT l.id, l.qty, l.price_cents, l.sku, l.shipped_action_id IS NOT NULL AS shipped_here,
                   o.channel, o.external_id AS order_id, o.ordered_at, o.status, o.fulfilled_by, o.market_status
              FROM sales_line l JOIN sales_order o ON o.id = l.order_id
             WHERE {' OR '.join(sconds)}
             ORDER BY o.status = 'open' DESC, o.ordered_at DESC, l.id DESC""", sparams)
        return {'asins': asins, 'buys': buys, 'sales': sales}

    def receive_lines(self, user_id: int, product_id: int, to: dict, lines: list, device: str = '',
                      unit_condition: str = '', note: str = '') -> dict:
        """One pile against several order lines (Alex 11-11: a scan that shows 2 orders of the same item takes both).
        lines: [{purchase_line_id, qty}]. Every line's units left are checked before anything is written; each line
        is its own receipt at its order's cost and date. Returns the action ids, oldest order first."""
        todo, seen = [], set()
        for x in lines or []:
            n = int(x.get('qty') or 0)
            if n <= 0:
                continue
            ln = self.get_line(int(x.get('purchase_line_id') or 0))
            if ln['id'] in seen:
                raise WmsError(f"Order {ln['order_id']} is in the list twice")
            seen.add(ln['id'])
            if ln['cancelled'] or ln['order_state'] == 'cancelled':
                raise WmsError(f"Order {ln['order_id']} was cancelled. Nothing was saved.")
            # every check wms.receive makes runs here first, so no line is saved when a later one would be refused
            p = self.wms._q1('SELECT sku FROM product WHERE id = ?', (product_id,)) or {}
            self.wms._check_line_product(self.wms._conn(), product_id, p.get('sku'), ln['product_id'],
                                         (ln['asin'] or '').upper(), (ln['upc'] or '').lstrip('0'), ln['order_id'])
            left = max(0, ln['left_to_receive'])
            if n > left:
                raise WmsError(f"Order {ln['order_id']} has only {left} left to receive. Nothing was saved.")
            todo.append((ln, n))
        if not todo:
            raise WmsError('Pick at least one order and how many')
        todo.sort(key=lambda t: (t[0]['ordered_at'] or '', t[0]['id']))
        actions, units = [], []
        for ln, n in todo:
            r = self.wms.receive(user_id, product_id, n, ln['unit_cost_cents'] or None, to,
                                 supplier=f"{ln['source']}{' (' + ln['account'] + ')' if ln['account'] else ''}",
                                 reference=ln['order_id'] or '', note=note, device=device, unit_condition=unit_condition,
                                 purchase_line_id=ln['id'], purchased_at=ln['ordered_at'])
            actions.append(r['action_id'])
            units += r['units']
        return {'action_ids': actions, 'action_id': actions[0], 'units': units}

    def open_lines_for_code(self, code: str, asins: tuple = ()) -> list:
        """Open purchase lines (units still to receive) for a scanned code that no product has yet: the ASIN itself,
        a line's UPC, the ASINs the catalog cache already knows for that barcode (no network call), or `asins` the
        caller found for it. Oldest first."""
        c = (code or '').strip().upper()
        if not c:
            return []
        asins = {a.upper() for a in asins if a} | ({c} if re.fullmatch(r'B0[0-9A-Z]{8}', c) else set())
        cached = self.wms._q1('SELECT data FROM catalog_cache WHERE code = ?', (c,))
        if cached:
            asins |= {(p.get('asin') or '').upper() for p in json.loads(cached['data']) if p.get('asin')}
        conds, params = [], []
        if asins:
            conds.append(f"UPPER(l.asin) IN ({','.join('?' * len(asins))})")
            params += sorted(asins)
        if c.isdigit() and len(c) >= 8:
            conds.append("(l.upc != '' AND LTRIM(l.upc, '0') = LTRIM(?, '0'))")
            params.append(c)
        if not conds:
            return []
        rec = self._received_sql()
        return self.wms._q(f"""
            SELECT l.id, l.product_id, l.asin, l.title, l.qty, {LINE_LEFT_SQL} AS left_to_receive,
                   o.source, o.external_id AS order_id, o.ordered_at, {rec} AS received
              FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id
             WHERE l.cancelled = 0 AND {RECEIVABLE_ORDER_SQL} AND {LINE_LEFT_SQL} > 0 AND ({' OR '.join(conds)})
             ORDER BY o.ordered_at, l.id""", params)

    def cancel_line(self, line_id: int, cancelled: bool = True):
        with self.wms._tx() as c:
            c.execute('UPDATE purchase_line SET cancelled = ? WHERE id = ?', (1 if cancelled else 0, line_id))

    # ------------------------------------------------------------------ shipping boxes

    def list_ship_boxes(self, include_archived: bool = False) -> list:
        """Ship boxes with `coming`: units on supply orders (The Boxery) not received yet."""
        return self.wms._q(f"""SELECT b.*, COALESCE((SELECT SUM(l.qty - l.supply_received) FROM purchase_line l
                                   JOIN purchase_order o ON o.id = l.po_id
                                  WHERE l.ship_box_id = b.id AND l.cancelled = 0 AND o.hidden = 0 AND o.kind = 'supplies'), 0) AS coming
                                 FROM ship_box b {'' if include_archived else 'WHERE b.archived = 0'}
                                ORDER BY b.inner_l_in * b.inner_w_in * b.inner_h_in""")

    def supplies_incoming(self) -> list:
        """Supply orders with units not received yet, oldest first, for the Ship boxes page."""
        orders = self.wms._q("""SELECT o.id, o.source AS store, o.external_id AS order_id, o.ordered_at, o.state, o.status_text,
                                       o.total_cents, o.mail_id
                                  FROM purchase_order o WHERE o.kind = 'supplies' AND o.hidden = 0
                                   AND EXISTS (SELECT 1 FROM purchase_line l WHERE l.po_id = o.id AND l.cancelled = 0
                                               AND l.qty > l.supply_received)
                                 ORDER BY o.ordered_at""")
        for o in orders:
            o['lines'] = self.wms._q("""SELECT l.id, l.title, l.qty, l.supply_received, l.unit_cost_cents, l.ship_box_id,
                                              b.name AS ship_box_name
                                         FROM purchase_line l LEFT JOIN ship_box b ON b.id = l.ship_box_id
                                        WHERE l.po_id = ? AND l.cancelled = 0 AND l.qty > l.supply_received ORDER BY l.id""", (o['id'],))
        return orders

    def receive_supply(self, line_id: int, qty: int) -> dict:
        """Supplies came in: the box size's on-hand count goes up by qty."""
        qty = _int(qty, 0)
        with self.wms._tx() as c:
            ln = c.execute("""SELECT l.qty, l.supply_received, l.ship_box_id, o.kind FROM purchase_line l
                              JOIN purchase_order o ON o.id = l.po_id WHERE l.id = ?""", (line_id,)).fetchone()
            if not ln or ln[3] != 'supplies':
                raise WmsError('No such supply line')
            left = ln[0] - ln[1]
            if not 1 <= qty <= left:
                raise WmsError(f'Enter 1 to {left}')
            c.execute('UPDATE purchase_line SET supply_received = supply_received + ? WHERE id = ?', (qty, line_id))
            if ln[2]:
                c.execute('UPDATE ship_box SET stock = stock + ? WHERE id = ?', (qty, ln[2]))
        return {'ok': True}

    def save_ship_box(self, data: dict, box_id: int = None) -> dict:
        try:
            dims = [float(data[k]) for k in ('inner_l_in', 'inner_w_in', 'inner_h_in')]
        except (KeyError, TypeError, ValueError):
            raise WmsError('Enter the inside length, width and height in inches')
        if min(dims) <= 0:
            raise WmsError('Sizes must be above 0')
        name = (data.get('name') or '').strip() or ' x '.join(f'{d:g}' for d in dims)
        vals = (name, *dims, _int(data.get('stock'), 0), data.get('cost_cents'), 1 if data.get('archived') else 0)
        with self.wms._tx() as c:
            if box_id:
                c.execute("""UPDATE ship_box SET name=?, inner_l_in=?, inner_w_in=?, inner_h_in=?, stock=?, cost_cents=?,
                             archived=? WHERE id=?""", (*vals, box_id))
            else:
                box_id = c.execute("""INSERT INTO ship_box (name, inner_l_in, inner_w_in, inner_h_in, stock, cost_cents, archived)
                                      VALUES (?,?,?,?,?,?,?)""", vals).lastrowid
        return self.wms._q1('SELECT * FROM ship_box WHERE id = ?', (box_id,))

    def recommend_box(self, items: list) -> dict:
        """items: [(dims_in [l,w,h] or None, qty)]. Smallest box that holds the biggest item and,
        for several items, has 1.3x their summed volume. Boxes with stock come first."""
        known = [(d, q) for d, q in items if d]
        if not known or len(known) < len(items):
            return {'box': None, 'reason': 'Size unknown. Add package dimensions on the product.'}
        biggest = max((d for d, _ in known), key=lambda d: d[0] * d[1] * d[2])
        units = sum(q for _, q in known)
        need = sum((d[0] + BOX_PADDING_IN) * (d[1] + BOX_PADDING_IN) * (d[2] + BOX_PADDING_IN) * q for d, q in known)
        need = need if units == 1 else need * 1.3
        boxes = self.list_ship_boxes()
        if not boxes:
            return {'box': None, 'reason': 'No shipping boxes entered yet (Ship boxes page).'}
        ok = [b for b in boxes if fits(biggest, [b['inner_l_in'], b['inner_w_in'], b['inner_h_in']])
              and b['inner_l_in'] * b['inner_w_in'] * b['inner_h_in'] >= need]
        if not ok:
            return {'box': None, 'reason': 'No box on the list is big enough.'}
        in_stock = [b for b in ok if b['stock'] > 0]
        best = (in_stock or ok)[0]
        # Flow audit 11-12: cartons were never counted (all 0), so "out of stock" showed on every order. Say it only
        # once someone has counted cartons.
        counted = any(b['stock'] > 0 for b in boxes)
        return {'box': best, 'reason': 'Smallest fit is out of stock' if counted and not in_stock else '',
                'item_in': [round(x, 1) for x in biggest]}

    # ------------------------------------------------------------------ sales

    def _match_product(self, c, sku: str, asin: str):
        for value in (sku, asin):
            if not value:
                continue
            row = c.execute("""SELECT p.id FROM product p LEFT JOIN product_identifier i ON i.product_id = p.id
                                WHERE p.archived = 0 AND (p.sku = ? OR i.value = ?) LIMIT 1""", (value, value)).fetchone()
            if row:
                return row[0]
        return None

    @staticmethod
    def product_index(c) -> dict:
        """Lowercased SKU or identifier value -> product id (not archived). A SKU beats an identifier."""
        idx = {}
        for pid, value in c.execute("""SELECT i.product_id, i.value FROM product_identifier i JOIN product p ON p.id = i.product_id
                                        WHERE p.archived = 0 ORDER BY i.product_id DESC""").fetchall():
            idx[value.lower()] = pid
        for pid, sku in c.execute('SELECT id, sku FROM product WHERE archived = 0 ORDER BY id DESC').fetchall():
            idx[sku.lower()] = pid
        return idx

    @staticmethod
    def index_match(idx: dict, sku: str, asin: str):
        """Same order as _match_product: the SKU first, then the ASIN."""
        return idx.get((sku or '').lower() or None) or idx.get((asin or '').lower() or None)

    def relink(self, c) -> int:
        """Listings and sold lines with no product yet: try again. Runs when a product is made or saved and on
        each listings refresh, so a new product shows on Orders and Listings right away. Moves no stock."""
        idx = self.product_index(c)
        has_listing = bool(c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'listing'").fetchone())
        by_sku = {}
        if has_listing:
            for lid, sku, asin, pid, code_type, code in c.execute(
                    'SELECT id, sku, asin, product_id, code_type, code FROM listing').fetchall():
                label = code if code_type == 'SKU' else ''   # an eBay Custom label
                if not pid:
                    pid = self.index_match(idx, sku, asin) or self.index_match(idx, label, '')
                    if pid:
                        c.execute('UPDATE listing SET product_id = ? WHERE id = ?', (pid, lid))
                if pid:
                    by_sku[sku.lower()] = pid
                    if label:
                        by_sku.setdefault(label.lower(), pid)
        n = 0
        for sid, sku, asin in c.execute('SELECT id, sku, asin FROM sales_line WHERE product_id IS NULL').fetchall():
            # a SKU we only know from the listing: follow it to the listing's product
            pid = self.index_match(idx, sku, asin) or by_sku.get((sku or '').lower() or None)
            if pid:
                c.execute('UPDATE sales_line SET product_id = ? WHERE id = ?', (pid, sid))
                n += 1
        return n + self.relink_buys(c)

    @staticmethod
    def relink_buys(c) -> int:
        """Purchase lines with no product: point them at the one product that carries the line's ASIN, else its UPC
        (Alex 11-12: the Crocs B0NHJH131B product page showed none of its buy; live had 33 lines on 12 products
        like that). Only an exact code held by exactly one product counts, never a title. Moves no stock: Receive
        and the audit already match by ASIN, so no count changes."""
        n = 0
        for col in ('asin', 'upc'):
            norm = "UPPER(TRIM(v))" if col == 'asin' else "LTRIM(TRIM(v), '0')"
            rows = c.execute(f"""
                WITH codes AS (SELECT {norm.replace('v', 'i.value')} AS code, i.product_id AS pid
                                 FROM product_identifier i JOIN product p ON p.id = i.product_id AND p.archived = 0
                                WHERE i.kind IN ({"'ASIN'" if col == 'asin' else "'UPC', 'EAN', 'GTIN'"})),
                     one AS (SELECT code, MIN(pid) AS pid FROM codes WHERE code != ''
                              GROUP BY code HAVING COUNT(DISTINCT pid) = 1)
                SELECT l.id, one.pid FROM purchase_line l JOIN one ON one.code = {norm.replace('v', 'l.' + col)}
                 WHERE l.product_id IS NULL AND COALESCE(l.{col}, '') != ''""").fetchall()
            for line_id, pid in rows:
                n += c.execute('UPDATE purchase_line SET product_id = ? WHERE id = ? AND product_id IS NULL',
                               (pid, line_id)).rowcount
        return n

    def import_sales(self, text: str, channel: str) -> dict:
        """Amazon order report (tab file), eBay or Walmart order CSV, or any CSV with order, sku/asin, qty."""
        channel = (channel or '').lower() or 'other'
        rows = read_csv(text)
        if not rows:
            raise WmsError('That file has no rows')
        new_orders = new_lines = 0
        unmatched = 0
        with self.wms._tx() as c:
            for r in rows:
                oid = _pick(r, 'order-id', 'order id', 'order number', 'sales record number', 'purchase order number',
                            'po#', 'po number', 'order #', 'order')
                qty = _int(_pick(r, 'quantity-purchased', 'quantity-to-ship', 'quantity', 'qty'), 1)
                sku = _pick(r, 'sku', 'custom label', 'seller sku', 'item sku')
                asin = _pick(r, 'asin').upper()
                title = _pick(r, 'product-name', 'item title', 'item description', 'product name', 'title', 'item')
                if not oid or qty <= 0 or not (sku or asin or title):
                    continue
                if _pick(r, 'order-status', 'order status', 'status').lower() in ('cancelled', 'canceled', 'shipped'):
                    continue
                cur = c.execute("""INSERT OR IGNORE INTO sales_order (channel, external_id, ordered_at, ship_by, imported_at)
                                   VALUES (?,?,?,?,?)""", (channel, oid, _date(_pick(r, 'purchase-date', 'order date', 'sale date', 'date')),
                                                          _date(_pick(r, 'promise-date', 'ship by date', 'ship-by date', 'latest-ship-date')),
                                                          now_utc()))
                new_orders += cur.rowcount
                so = c.execute('SELECT id FROM sales_order WHERE channel = ? AND external_id = ?', (channel, oid)).fetchone()[0]
                key = _pick(r, 'order-item-id', 'order item id', 'item number', 'line number', 'transaction id') or sku or asin or title[:60]
                pid = self._match_product(c, sku, asin)
                cur = c.execute("""INSERT OR IGNORE INTO sales_line (order_id, line_key, sku, asin, title, qty, product_id)
                                   VALUES (?,?,?,?,?,?,?)""", (so, key, sku, asin, title[:300], qty, pid))
                if cur.rowcount:
                    new_lines += 1
                    unmatched += 0 if pid else 1
        return {'rows': len(rows), 'new_orders': new_orders, 'new_lines': new_lines, 'unmatched': unmatched}

    def add_sale(self, data: dict) -> dict:
        pid = _int(data.get('product_id'), 0)
        qty = _int(data.get('qty'), 1)
        if not pid or qty <= 0:
            raise WmsError('Pick the product and quantity')
        p = self.wms._q1('SELECT sku, title FROM product WHERE id = ?', (pid,))
        if not p:
            raise WmsError('Product not found')
        channel = (data.get('channel') or 'other').lower()
        oid = (data.get('order_id') or '').strip() or f'manual-{secrets.token_hex(4)}'
        with self.wms._tx() as c:
            c.execute("""INSERT OR IGNORE INTO sales_order (channel, external_id, ordered_at, imported_at, note)
                         VALUES (?,?,?,?,?)""", (channel, oid, datetime.now(timezone.utc).date().isoformat(), now_utc(),
                                                 data.get('note') or ''))
            so = c.execute('SELECT id, status FROM sales_order WHERE channel = ? AND external_id = ?', (channel, oid)).fetchone()
            if so[1] != 'open':
                raise WmsError(f'Order {oid} is already {so[1]}')
            # Same SKU again adds to its open line; a line that already shipped stays as shipped (sweep B7), so the
            # new units get their own line.
            key, n = p['sku'], 1
            while c.execute('SELECT 1 FROM sales_line WHERE order_id = ? AND line_key = ? AND shipped_action_id IS NOT NULL',
                            (so[0], key)).fetchone():
                n += 1
                key = f"{p['sku']}#{n}"
            c.execute("""INSERT INTO sales_line (order_id, line_key, sku, title, qty, product_id) VALUES (?,?,?,?,?,?)
                         ON CONFLICT(order_id, line_key) DO UPDATE SET qty = qty + excluded.qty""",
                      (so[0], key, p['sku'], p['title'], qty, pid))
        return {'order_id': so[0]}

    def link_line(self, line_id: int, product_id: int, remember: bool = True):
        with self.wms._tx() as c:
            line = c.execute('SELECT sku, asin, order_id FROM sales_line WHERE id = ?', (line_id,)).fetchone()
            if not line:
                raise WmsError('Order line not found')
            ch = c.execute('SELECT channel FROM sales_order WHERE id = ?', (line[2],)).fetchone()[0]
            c.execute('UPDATE sales_line SET product_id = ? WHERE id = ?', (product_id, line_id))
            if remember and line[0]:
                kind = {'ebay': 'EBAY_SKU', 'walmart': 'WALMART_SKU', 'shopify': 'SHOPIFY_SKU'}.get(ch, 'OTHER')
                c.execute('INSERT OR IGNORE INTO product_identifier (product_id, kind, value) VALUES (?,?,?)',
                          (product_id, kind, line[0]))
            # Other open lines with the same SKU get the same product.
            if line[0]:
                c.execute('UPDATE sales_line SET product_id = ? WHERE product_id IS NULL AND sku = ? AND shipped_action_id IS NULL',
                          (product_id, line[0]))

    def _dims_in(self, product_id: int):
        p = self.wms._q1('SELECT pkg_l_mm, pkg_w_mm, pkg_h_mm FROM product WHERE id = ?', (product_id,))
        if not p or not all(p.values()):
            return None
        return [p['pkg_l_mm'] / MM_PER_IN, p['pkg_w_mm'] / MM_PER_IN, p['pkg_h_mm'] / MM_PER_IN]

    def pick_list(self, status: str = 'open') -> list:
        # To ship: the one due first on top. Shipped and cancelled: newest first (Alex 11-11: 1,839 shipped orders from
        # the history import filled the 300 with 2025 orders, so one shipped today never showed). Orders scanned out
        # here come first by when they were scanned (an old order auto_ship took today is news), then by order date.
        # Alex 11-12: an order closed here with no scan (eBay shipped it before Boxloft held it) sorts by when it
        # closed (closed_at); before, it sat under all 115 scanned orders and looked missing.
        order = 'COALESCE(o.ship_by, o.ordered_at), o.id' if status == 'open' else \
            'shipped_at IS NULL, shipped_at DESC, o.ordered_at DESC, o.id DESC'
        orders = self.wms._q(f"""SELECT o.*, (SELECT MAX(l.shipped_action_id) FROM sales_line l WHERE l.order_id = o.id) AS last_ship,
                                        COALESCE(o.closed_at, (SELECT MAX(a.at) FROM sales_line l JOIN action a ON a.id = l.shipped_action_id
                                                                WHERE l.order_id = o.id)) AS shipped_at
                                   FROM sales_order o WHERE o.status = ? ORDER BY {order} LIMIT 300""", (status,))
        # A line with no item yet shows its listing's photo (Alex 11-12: eBay photos on the order cards)
        pic = ("COALESCE(p.image_url, (SELECT li.image_url FROM listing li WHERE li.channel = ? AND li.sku = l.sku))"
               if self.wms._q1("SELECT 1 AS x FROM pragma_table_info('listing') WHERE name = 'image_url'") else 'p.image_url')
        for o in orders:
            o['lines'] = self.wms._q(f"""
                SELECT l.*, p.sku AS product_sku, p.title AS product_title, p.tracking,
                       {pic} AS image_url,   -- shown until the saved copy of the photo is in (save_image runs off the request)
                       (SELECT ph.id FROM photo ph WHERE ph.product_id = l.product_id AND ph.removed = 0
                         ORDER BY ph.source = 'upload' DESC, ph.id LIMIT 1) AS photo_id,
                       sb.name AS ship_box_name,
                       (SELECT su.id FROM ship_unscanned su WHERE su.sale_line_id = l.id AND su.undone_at IS NULL
                         AND l.shipped_action_id IS NOT NULL ORDER BY su.id DESC LIMIT 1) AS unscanned_id
                  FROM sales_line l LEFT JOIN product p ON p.id = l.product_id
                  LEFT JOIN ship_box sb ON sb.id = l.ship_box_id
                 WHERE l.order_id = ? ORDER BY l.id""", ((o['channel'],) if pic != 'p.image_url' else ()) + (o['id'],))
            for ln in o['lines']:
                ln['where'] = self._where(ln['product_id']) if ln['product_id'] else []
            pending = [(self._dims_in(ln['product_id']) if ln['product_id'] else None, ln['qty'])
                       for ln in o['lines'] if not ln['shipped_action_id']]
            o['recommendation'] = self.recommend_box(pending) if pending else None
            # Ship box (Alex 11-11): one item going out alone gets the box it went out in last time (a person's
            # answer on the confirm popup, else Amazon's label size), so auto_ship and the ship dialog use it.
            todo = [ln for ln in o['lines'] if not ln['shipped_action_id']]
            if self.learned_box and len(todo) == 1 and todo[0]['qty'] == 1 and todo[0]['product_id']:
                got = self.learned_box(todo[0]['product_id'], todo[0]['asin'] or '')
                if got:
                    o['recommendation'] = {'box': got['box'], 'reason': got['reason'], 'learned': True}
        return orders

    def _where(self, product_id: int) -> list:
        """Where to pick from: boxes holding the product, the box with the oldest stock first."""
        rows = self.wms._q("""
            SELECT s.box_id, s.location_id, s.unit_id, s.qty, b.name AS box_name,
                   COALESCE(lb.code, l.code) AS location_code, u.condition AS unit_condition,
                   (SELECT MIN(a.at) FROM stock_move m JOIN action a ON a.id = m.action_id
                     WHERE m.product_id = s.product_id AND m.to_box_id = s.box_id) AS since
              FROM stock_level s
              LEFT JOIN box b ON b.id = s.box_id LEFT JOIN location lb ON lb.id = b.location_id
              LEFT JOIN location l ON l.id = s.location_id LEFT JOIN unit u ON u.id = s.unit_id
             WHERE s.product_id = ? AND s.qty > 0 AND (s.box_id != 0 OR l.kind != 'virtual')
             ORDER BY since, s.box_id""", (product_id,))
        for r in rows:
            r['box_code'] = make_code('box', r['box_id']) if r['box_id'] else None
            r['unit_code'] = make_code('unit', r['unit_id']) if r['unit_id'] else None
        return rows

    def ship_line(self, user_id: int, line_id: int, src: dict, unit_id: int = 0, ship_box_id: int = None,
                  box_fit: str = '', device: str = '') -> dict:
        line = self.wms._q1("""SELECT l.*, o.channel, o.external_id, o.status AS order_status FROM sales_line l
                                JOIN sales_order o ON o.id = l.order_id WHERE l.id = ?""", (line_id,))
        if not line:
            raise WmsError('Order line not found')
        if line['shipped_action_id']:
            raise WmsError('Already shipped')
        if line['order_status'] != 'open':
            raise WmsError(f"Order {line['external_id']} is {line['order_status']}. Nothing was shipped.")
        if not line['product_id']:
            raise WmsError('Link this line to a product first')
        tracking = self.wms._q1('SELECT tracking FROM product WHERE id = ?', (line['product_id'],))['tracking']
        if tracking == 'unit' and not unit_id:
            raise WmsError('Scan or pick which unit you are shipping')
        lines = [{'product_id': line['product_id'], 'unit_id': unit_id, 'qty': 1}] if unit_id else \
            [{'product_id': line['product_id'], 'qty': line['qty']}]
        if unit_id and line['qty'] > 1:
            raise WmsError('Ship multi-unit lines one unit at a time (split coming later)')
        res = self.wms.remove(user_id, src, lines, 'CUSTOMER', note=f'{line["channel"]} {line["external_id"]}', device=device)
        with self.wms._tx() as c:
            done = self.close_line(c, line_id, line['order_id'], res['action_id'], ship_box_id, box_fit)
        return {'action_id': res['action_id'], 'order_done': done}

    @staticmethod
    def close_line(c, line_id: int, order_id: int, action_id: int, ship_box_id: int = None, box_fit: str = '') -> bool:
        """The line is shipped by `action_id`: ship box taken, order shipped when it was its last line. True = order done."""
        # One order goes out in one box (sweep B6: a 2-line order took 2): only the first line shipped takes it.
        had = c.execute('SELECT 1 FROM sales_line WHERE order_id = ? AND box_taken = 1', (order_id,)).fetchone()
        taken = c.execute('UPDATE ship_box SET stock = stock - 1 WHERE id = ? AND stock > 0',
                          (ship_box_id,)).rowcount if ship_box_id and not had else 0
        c.execute('UPDATE sales_line SET shipped_action_id = ?, ship_box_id = ?, box_fit = ?, box_taken = ? WHERE id = ?',
                  (action_id, ship_box_id, box_fit or None, taken, line_id))
        left = c.execute('SELECT COUNT(*) FROM sales_line WHERE order_id = ? AND shipped_action_id IS NULL', (order_id,)).fetchone()[0]
        if not left:
            c.execute("UPDATE sales_order SET status = 'shipped', closed_at = ? WHERE id = ?", (now_utc(), order_id))
        return not left

    MARKED_SHIPPED = 'Marked shipped on the marketplace, not scanned out here'
    MARKET_BOX = {'ebay': r'^E-?\d+$'}   # sorting.MARKETS: a channel whose sales leave only from its letter's boxes
    ship_unscanned = None   # Audit.ship_unscanned, set when the audit starts (it owns the audit rows)
    sold_before_boxloft = None   # EbayLink.sold_before_boxloft(order, line): an eBay sale Boxloft could never have held

    def auto_ship(self, device: str = 'auto') -> dict:
        """Alex 11-11: an order he ships in Seller Central (or eBay) is scanned out here by Boxloft (user 0), so the
        shelf count drops with no tap. It does exactly what "Packed and shipped" does with the ship dialog's defaults:
        the first place on the pick list (oldest stock), the suggested ship box. When that default can't be right (no
        product, none on the shelves, the first place holds too few, a one-off unit with several in stock) the order
        keeps `review` with the reason, for a person to scan out or cancel. Runs under op_lock, after autolink."""
        out = {'orders': 0, 'lines': 0, 'review': 0}
        with self.wms.op_lock:
            for o in self.pick_list('open'):
                if o['fulfilled_by'] != 'me' or not (o['review'] or '').startswith(self.MARKED_SHIPPED[:24]):
                    continue
                why, history = [], 0
                for ln in o['lines']:
                    if ln['shipped_action_id']:
                        continue
                    if not ln['product_id'] and self.sold_before_boxloft and self.sold_before_boxloft(o, ln):
                        history += 1   # Alex 11-12: sold out on eBay before Boxloft read eBay; closes like history
                        continue
                    places = ln.get('where') or []
                    if o['channel'] in self.MARKET_BOX and places:
                        # Fable 11-12 #5: an eBay sale leaves from an E box, never from the Amazon stock in an A box
                        mine = [p for p in places if re.match(self.MARKET_BOX[o['channel']], (p.get('box_name') or '').upper())]
                        if not mine:
                            why.append(f"{ln['title'][:40]}: only in other markets' boxes "
                                       f"({', '.join(p.get('box_name') or p.get('location_code') or '?' for p in places[:3])}); "
                                       'move one to an E box or ship it by hand')
                            continue
                        places = mine
                    w = places[0] if places else None
                    if not ln['product_id']:
                        why.append(f"{ln['title'][:40]}: not linked to a product")
                    elif not w and self.ship_unscanned:
                        # Flow audit 11-12 (redesign 3): never scanned in, so it left from the package. Supplier ->
                        # customer, tied to the Stock audit row or the order it was bought on. With neither, it sold
                        # before it came in (Alex 11-17, outside=True): closed now, so a box filled later keeps its count.
                        box = (o.get('recommendation') or {}).get('box') or {}
                        try:
                            self.ship_unscanned(ln['id'], 0, device, box.get('id'), auto=True, outside=True)
                            out['lines'] += 1
                        except WmsError as e:
                            why.append(f"{ln['title'][:40]}: none on the shelves, and {e}")
                    elif not w:
                        why.append(f"{ln['title'][:40]}: none on the shelves")
                    elif w['qty'] < ln['qty']:
                        why.append(f"{ln['title'][:40]}: {ln['qty']} sold, the first place holds {w['qty']}")
                    elif ln['tracking'] == 'unit' and (ln['qty'] > 1 or len(ln['where']) > 1):
                        why.append(f"{ln['title'][:40]}: one-off units, pick which one")
                    else:
                        src = {'box_id': w['box_id']} if w['box_id'] else {'location_id': w['location_id']}
                        box = (o.get('recommendation') or {}).get('box') or {}
                        try:
                            self.ship_line(0, ln['id'], src, w['unit_id'] or 0, box.get('id'), '', device)
                            out['lines'] += 1
                        except WmsError as e:
                            why.append(f"{ln['title'][:40]}: {e}")
                with self.wms._tx() as c:
                    if why:
                        c.execute('UPDATE sales_order SET review = ? WHERE id = ?',
                                  (f"{self.MARKED_SHIPPED}. Boxloft could not take it: " + '; '.join(why), o['id']))
                        out['review'] += 1
                    else:
                        c.execute("UPDATE sales_order SET review = '' WHERE id = ?", (o['id'],))
                        if history:   # no stock move, as the history import closes an order (its lines keep no action)
                            c.execute("UPDATE sales_order SET status = 'shipped', closed_at = ? WHERE id = ?", (now_utc(), o['id']))
                            out['history'] = out.get('history', 0) + 1
                        out['orders'] += 1
        return out

    def set_order_status(self, order_id: int, status: str):
        if status not in ('open', 'cancelled'):
            raise WmsError('Status must be open or cancelled')
        with self.wms._tx() as c:
            c.execute('UPDATE sales_order SET status = ? WHERE id = ?', (status, order_id))
