"""Boxloft inventory: an append-only, double-entry stock ledger.

Every change is an *action* (one thing a person did: receive, move, count, undo)
made of *moves*. A stock move takes a quantity of one product (or one unit) from
one holder to another. A holder is a box or a location. Virtual locations
(VENDOR, CUSTOMER, ADJUST, ...) stand for the world outside the shelves, so
stock never appears or vanishes: receiving is VENDOR -> box, a sale is
box -> CUSTOMER, a count that finds two short is box -> ADJUST.

Rules the database enforces:
- stock_move, box_move and action rows are never updated or deleted
  (triggers), except action.reversed_by, which undo sets once.
- stock_level is a cache of the ledger, written in the same transaction.
  check_integrity() rebuilds it from the ledger and reports any difference.
- Per product, quantities over all holders (virtual ones included) sum to 0.

Mistakes are fixed with undo, which writes the opposite moves and points back
at the original action.
"""

import config
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone

import dbpool
from codes import make_code

VIRTUAL_LOCATIONS = [
    ('VENDOR', 'Received from suppliers'),
    ('CUSTOMER', 'Sold and shipped to customers'),
    ('FBA', 'Sent to Amazon FBA'),
    ('WFS', 'Sent to Walmart WFS'),
    ('CLIENT', 'Returned to a 3PL client'),
    ('ADJUST', 'Count corrections (lost or found)'),
    ('DAMAGED', 'Damaged or thrown out'),
]
# Alex 11-11 12:20 PM: boxes sit on shelves in the warehouse; a new box starts at this place (made once).
HOME_SITE = config.SITE_NAME
# The outside world in history lines (flow audit 11-12: "into CUSTOMER").
PLACE_WORDS = {'CUSTOMER': 'the customer', 'VENDOR': 'the seller', 'ADJUST': 'a count fix', 'DAMAGED': 'damaged',
               'CLIENT': 'the client', 'FBA': 'Amazon FBA', 'WFS': 'Walmart WFS'}


def short_title(t: str, n: int = 48) -> str:
    t = ' '.join((t or '').split())
    return t if len(t) <= n else t[:n - 1].rstrip(' ,-|') + '…'
REMOVE_TARGETS = ['CUSTOMER', 'FBA', 'WFS', 'CLIENT', 'DAMAGED', 'ADJUST']
# Also allowed for wms.remove() by code, not offered on the Remove page: VENDOR = sent back to the seller
# (a purchase return; Returns page "Pull from stock").
REMOVE_TARGETS_INTERNAL = REMOVE_TARGETS + ['VENDOR']
# Purchase-line math (purchase_line and purchase_return live in commerce.py; these only run when a
# line id is given). A return's units either went back before anyone received them, or came off our
# shelves (shelf_qty, said when the return was started from stock; or proven by a pull to VENDOR).
# Left to receive = bought - returned before receiving - received. Negative means more was received
# than the line still holds: returned units still on a shelf that need pulling.
# Units of receipt r that a merge sent back as wrong (receipt_void), while that merge's action stands.
VOIDED_SQL = """COALESCE((SELECT SUM(v.qty) FROM receipt_void v JOIN action va ON va.id = v.action_id
                           WHERE v.receipt_id = r.id AND va.reversed_by IS NULL), 0)"""
LINE_RECEIVED_SQL = """COALESCE((SELECT SUM(r.qty) FROM receipt r JOIN action a ON a.id = r.action_id
                         WHERE r.purchase_line_id = l.id AND a.reversed_by IS NULL), 0)"""
LINE_RETURNED_SQL = """COALESCE((SELECT pr.qty FROM purchase_return pr WHERE pr.line_id = l.id AND pr.state != 'kept'), 0)"""
LINE_PULLED_SQL = """COALESCE((SELECT SUM(rp.qty) FROM return_pull rp JOIN purchase_return pr ON pr.id = rp.return_id
                         JOIN action a ON a.id = rp.action_id WHERE pr.line_id = l.id AND a.reversed_by IS NULL), 0)"""
LINE_SHELF_SQL = f"""COALESCE((SELECT MIN(pr.qty, MAX(pr.shelf_qty, {LINE_PULLED_SQL})) FROM purchase_return pr
                         WHERE pr.line_id = l.id AND pr.state != 'kept'), 0)"""
# Units of the line Amazon sold that nothing here shipped (view line_presold, audit.py PRESOLD_VIEW; Alex 11-11): they
# were shipped from stock nobody scanned in, so they are not still coming. Commerce makes an empty view until Audit
# replaces it with the real one.
LINE_PRESOLD_SQL = """COALESCE((SELECT p.units FROM line_presold p WHERE p.line_id = l.id), 0)"""
LINE_LEFT_SQL = f'(l.qty - {LINE_RETURNED_SQL} + {LINE_SHELF_SQL} - {LINE_RECEIVED_SQL} - {LINE_PRESOLD_SQL})'
BOX_NAME_RE = re.compile(r'^[A-Z]{1,3}-?\d{1,4}$')   # same as sorting.NAME_RE: A31, F7, S2


def clean_box_name(name: str) -> str:
    """'a-31 ' -> 'A31', the way the Box bar and the label write it; other names are kept as typed."""
    n = re.sub(r'\s+', '', (name or '').upper())
    return n.replace('-', '') if BOX_NAME_RE.match(n) else (name or '').strip()


IDENTIFIER_KINDS = ['UPC', 'EAN', 'GTIN', 'ASIN', 'FNSKU', 'EBAY_SKU', 'EBAY_ITEM', 'WALMART_SKU', 'WALMART_ITEM', 'SHOPIFY_SKU', 'MPN', 'OTHER']

SCHEMA = """
CREATE TABLE IF NOT EXISTS owner (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    is_client INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product (
    id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES owner(id),
    sku TEXT UNIQUE NOT NULL COLLATE NOCASE,
    title TEXT NOT NULL,
    tracking TEXT NOT NULL DEFAULT 'qty' CHECK (tracking IN ('qty', 'unit')),
    condition TEXT NOT NULL DEFAULT 'New',
    notes TEXT NOT NULL DEFAULT '',
    default_cost_cents INTEGER,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_identifier (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES product(id),
    kind TEXT NOT NULL,
    value TEXT NOT NULL COLLATE NOCASE,
    UNIQUE (product_id, kind, value)
);
CREATE INDEX IF NOT EXISTS idx_ident_value ON product_identifier(value);
CREATE TABLE IF NOT EXISTS unit (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES product(id),
    condition TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    cost_cents INTEGER,
    receipt_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS photo (
    id INTEGER PRIMARY KEY,
    product_id INTEGER REFERENCES product(id),
    unit_id INTEGER REFERENCES unit(id),
    path TEXT NOT NULL,
    user_id INTEGER,
    removed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_deleted (   -- Alex 11-11: a product erased with no history, kept here as it was
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    sku TEXT NOT NULL,
    title TEXT NOT NULL,
    data TEXT NOT NULL                       -- JSON: the product, its codes, and what pointed at it
);
CREATE TABLE IF NOT EXISTS location (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL COLLATE NOCASE,
    name TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'shelf' CHECK (kind IN ('shelf', 'dock', 'virtual')),
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS box (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'box' CHECK (kind IN ('box', 'tote', 'pallet')),
    location_id INTEGER NOT NULL REFERENCES location(id),
    notes TEXT NOT NULL DEFAULT '',
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS action (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    device TEXT NOT NULL DEFAULT '',
    reverses_action_id INTEGER REFERENCES action(id),
    reversed_by INTEGER REFERENCES action(id)
);
CREATE TABLE IF NOT EXISTS receipt (
    id INTEGER PRIMARY KEY,
    action_id INTEGER NOT NULL REFERENCES action(id),
    product_id INTEGER NOT NULL REFERENCES product(id),
    qty INTEGER NOT NULL CHECK (qty > 0),
    unit_cost_cents INTEGER NOT NULL,
    supplier TEXT NOT NULL DEFAULT '',
    reference TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS stock_move (
    id INTEGER PRIMARY KEY,
    action_id INTEGER NOT NULL REFERENCES action(id),
    product_id INTEGER NOT NULL REFERENCES product(id),
    unit_id INTEGER NOT NULL DEFAULT 0,
    qty INTEGER NOT NULL CHECK (qty > 0),
    from_box_id INTEGER NOT NULL DEFAULT 0,
    from_location_id INTEGER NOT NULL DEFAULT 0,
    to_box_id INTEGER NOT NULL DEFAULT 0,
    to_location_id INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_move_action ON stock_move(action_id);
CREATE INDEX IF NOT EXISTS idx_move_product ON stock_move(product_id);
CREATE TABLE IF NOT EXISTS box_move (
    id INTEGER PRIMARY KEY,
    action_id INTEGER NOT NULL REFERENCES action(id),
    box_id INTEGER NOT NULL REFERENCES box(id),
    from_location_id INTEGER NOT NULL,
    to_location_id INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_boxmove_box ON box_move(box_id);
CREATE TABLE IF NOT EXISTS stock_level (
    product_id INTEGER NOT NULL,
    unit_id INTEGER NOT NULL DEFAULT 0,
    box_id INTEGER NOT NULL DEFAULT 0,
    location_id INTEGER NOT NULL DEFAULT 0,
    qty INTEGER NOT NULL,
    PRIMARY KEY (product_id, unit_id, box_id, location_id)
);
CREATE INDEX IF NOT EXISTS idx_level_box ON stock_level(box_id);
CREATE TABLE IF NOT EXISTS count_line (
    id INTEGER PRIMARY KEY,
    action_id INTEGER NOT NULL REFERENCES action(id),
    box_id INTEGER NOT NULL DEFAULT 0,
    location_id INTEGER NOT NULL DEFAULT 0,
    product_id INTEGER NOT NULL,
    unit_id INTEGER NOT NULL DEFAULT 0,
    expected INTEGER NOT NULL,
    counted INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS integrity_run (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    ok INTEGER NOT NULL,
    details TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS stock_move_no_update BEFORE UPDATE ON stock_move
BEGIN SELECT RAISE(ABORT, 'stock_move is append-only'); END;
CREATE TRIGGER IF NOT EXISTS stock_move_no_delete BEFORE DELETE ON stock_move
BEGIN SELECT RAISE(ABORT, 'stock_move is append-only'); END;
CREATE TRIGGER IF NOT EXISTS box_move_no_update BEFORE UPDATE ON box_move
BEGIN SELECT RAISE(ABORT, 'box_move is append-only'); END;
CREATE TRIGGER IF NOT EXISTS box_move_no_delete BEFORE DELETE ON box_move
BEGIN SELECT RAISE(ABORT, 'box_move is append-only'); END;
CREATE TRIGGER IF NOT EXISTS action_no_delete BEFORE DELETE ON action
BEGIN SELECT RAISE(ABORT, 'action is append-only'); END;
CREATE TRIGGER IF NOT EXISTS action_update_once BEFORE UPDATE ON action
WHEN OLD.reversed_by IS NOT NULL
  OR NEW.at IS NOT OLD.at OR NEW.user_id IS NOT OLD.user_id OR NEW.kind IS NOT OLD.kind
  OR NEW.summary IS NOT OLD.summary OR NEW.reverses_action_id IS NOT OLD.reverses_action_id
BEGIN SELECT RAISE(ABORT, 'action rows only gain reversed_by, once'); END;
"""


class WmsError(Exception):
    """A request the warehouse rules refuse. The message is shown to the user."""


class Conflict(WmsError):
    """The record changed since the person's screen loaded it (another person saved first). HTTP 409: the web app
    shows the message and reloads, and nothing is saved."""


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _dicts(cur) -> list:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _one(cur):
    rows = _dicts(cur)
    return rows[0] if rows else None


class Wms:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()
        self._idle = []
        self._pool_lock = threading.Lock()
        self._write_lock = threading.Lock()
        # One write request at a time, from its first check to its last write (server.locked). Reentrant, so a locked
        # route can call code that takes it again (record_amazon_sales, autolink).
        self.op_lock = threading.RLock()
        self.write_seq = 0   # +1 on every commit in this process: read caches (Audit._all_rows) key on it
        conn = self._conn()
        conn.executescript(SCHEMA)
        self._migrate(conn)
        self._seed(conn)

    # ------------------------------------------------------------------ plumbing

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            with self._pool_lock:
                conn = self._idle.pop() if self._idle else None
            if conn is None:
                conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
                conn.execute('PRAGMA journal_mode=WAL')
                conn.execute('PRAGMA busy_timeout=5000')
                conn.execute('PRAGMA foreign_keys=ON')
            self._local.conn = conn
        return conn

    def release(self):
        """End of a request: this thread's connection goes back to the idle pool (dbpool.release)."""
        dbpool.release(self._local, self._idle, self._pool_lock)

    def _q(self, sql: str, params=()) -> list:
        return _dicts(self._conn().execute(sql, params))

    def _q1(self, sql: str, params=()):
        return _one(self._conn().execute(sql, params))

    def _tx(self):
        """Context manager: one write transaction under the process write lock. Nested in a _tx of the same thread it
        is a SAVEPOINT: an error inside rolls back only its own writes, as before, and the outer _tx decides for all.
        So a save made of several saves (Not this? Change, a count change: undo the old line, then save the new one)
        wraps them in one _tx and is all or nothing (review 11-17: a refused new save left the old line undone)."""
        wms = self

        class _Tx:
            def __enter__(self):
                self.depth = getattr(wms._local, 'tx_depth', 0)
                if self.depth:   # this thread holds the write lock and an open transaction already
                    self.conn = wms._conn()
                    self.conn.execute(f'SAVEPOINT tx{self.depth}')
                    wms._local.tx_depth = self.depth + 1
                    return self.conn
                wms._write_lock.acquire()
                try:
                    self.conn = wms._conn()
                    self.conn.execute('BEGIN IMMEDIATE')
                except BaseException:   # a failed BEGIN (database busy) must not keep the lock: every save would hang
                    wms._write_lock.release()
                    raise
                wms._local.tx_depth = 1
                return self.conn

            def __exit__(self, exc_type, exc, tb):
                if self.depth:
                    wms._local.tx_depth = self.depth
                    if exc_type is not None:
                        self.conn.execute(f'ROLLBACK TO tx{self.depth}')
                    self.conn.execute(f'RELEASE tx{self.depth}')
                    wms.write_seq += 1   # read caches built inside see the writes so far; a rollback drops them too
                    return False
                try:
                    if exc_type is None:
                        self.conn.execute('COMMIT')
                    else:
                        self.conn.execute('ROLLBACK')
                    wms.write_seq += 1   # a cache filled inside the transaction must not outlive its rollback
                finally:
                    wms._local.tx_depth = 0
                    wms._write_lock.release()
                return False

        return _Tx()

    @staticmethod
    def _add_column(conn, table: str, col: str, decl: str):
        cols = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
        if col not in cols:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {col} {decl}')

    def _migrate(self, conn):
        # v1.1: catalog facts on products, purchase links on receipts, photo source.
        self._add_column(conn, 'location', 'site', "TEXT NOT NULL DEFAULT ''")  # which building; name is the room
        for col, decl in (('brand', "TEXT NOT NULL DEFAULT ''"), ('image_url', 'TEXT'), ('pkg_l_mm', 'INTEGER'),
                          ('pkg_w_mm', 'INTEGER'), ('pkg_h_mm', 'INTEGER'), ('pkg_weight_g', 'INTEGER')):
            self._add_column(conn, 'product', col, decl)
        self._add_column(conn, 'receipt', 'purchase_line_id', 'INTEGER')
        self._add_column(conn, 'receipt', 'purchased_at', 'TEXT')
        # Alex 11-11 "Sam won't always know the price": a receipt with no cost typed is saved at 0 and marked, so the
        # average cost leaves it out and the product page shows the cost as not entered.
        self._add_column(conn, 'receipt', 'cost_missing', 'INTEGER NOT NULL DEFAULT 0')
        self._add_column(conn, 'photo', 'source', "TEXT NOT NULL DEFAULT 'upload'")
        self._add_column(conn, 'product', 'updated_by', 'INTEGER')   # who saved the product page last (conflict message)
        # Sweep A-u1: updated_at also moves on every stock move and photo, so the edit form (keyed on it) reloaded and
        # lost what Alex typed when Sam received the item. edited_at moves only on an edit of the product itself.
        self._add_column(conn, 'product', 'edited_at', 'TEXT')
        conn.execute('UPDATE product SET edited_at = updated_at WHERE edited_at IS NULL')
        # Who saved what on edit screens (server.EDIT_GUARDS), for the "Sam changed this box at ..." message.
        conn.execute("""CREATE TABLE IF NOT EXISTS edit_log (id INTEGER PRIMARY KEY, tbl TEXT NOT NULL, row_id INTEGER NOT NULL,
                        user_id INTEGER, at TEXT NOT NULL, path TEXT NOT NULL DEFAULT '')""")
        conn.execute('CREATE INDEX IF NOT EXISTS edit_log_row ON edit_log (tbl, row_id)')
        # Speed sweep 11-12: received-per-line is a subquery on every purchase line (LINE_LEFT_SQL); without this it
        # read every receipt for each line.
        conn.execute('CREATE INDEX IF NOT EXISTS idx_receipt_line ON receipt(purchase_line_id)')
        # Alex 11-12: Sam renamed box A55 to A8 seconds after scanning into it; its history still said "into A55" and
        # nothing on the page said why. Each rename is kept (append-only) for the box page.
        conn.execute("""CREATE TABLE IF NOT EXISTS box_rename (id INTEGER PRIMARY KEY, box_id INTEGER NOT NULL,
                        old_name TEXT NOT NULL, new_name TEXT NOT NULL, at TEXT NOT NULL, user_id INTEGER)""")
        conn.execute('CREATE INDEX IF NOT EXISTS idx_box_rename_box ON box_rename(box_id)')
        # Alex 11-13 (LEGO Alpine Lodge): units saved with no cost, then a usual cost typed on the item. Each time the
        # usual cost fills (or a blank clears) such a receipt's cost, one row here says so (append-only).
        conn.execute("""CREATE TABLE IF NOT EXISTS cost_fill (id INTEGER PRIMARY KEY, receipt_id INTEGER NOT NULL,
                        cents INTEGER, user_id INTEGER, at TEXT NOT NULL)""")
        conn.execute('CREATE INDEX IF NOT EXISTS idx_cost_fill_receipt ON cost_fill(receipt_id)')
        conn.execute("""CREATE TRIGGER IF NOT EXISTS cost_fill_no_update BEFORE UPDATE ON cost_fill
                        BEGIN SELECT RAISE(ABORT, 'cost_fill is append-only'); END""")
        conn.execute("""CREATE TRIGGER IF NOT EXISTS cost_fill_no_delete BEFORE DELETE ON cost_fill
                        BEGIN SELECT RAISE(ABORT, 'cost_fill is append-only'); END""")
        # Merge items (review 11-17, bug 3): units a merge found were really another buy's, so the receipt they came in
        # on as extra stock was wrong. receipt_void takes that many of the receipt's units out of Avg cost while the
        # action that sent them back (to VENDOR) stands; an undo of that action brings them back. Append-only.
        conn.execute("""CREATE TABLE IF NOT EXISTS receipt_void (id INTEGER PRIMARY KEY, receipt_id INTEGER NOT NULL,
                        qty INTEGER NOT NULL CHECK (qty > 0), action_id INTEGER NOT NULL, at TEXT NOT NULL)""")
        conn.execute('CREATE INDEX IF NOT EXISTS idx_receipt_void_receipt ON receipt_void(receipt_id)')
        conn.execute("""CREATE TRIGGER IF NOT EXISTS receipt_void_no_update BEFORE UPDATE ON receipt_void
                        BEGIN SELECT RAISE(ABORT, 'receipt_void is append-only'); END""")
        conn.execute("""CREATE TRIGGER IF NOT EXISTS receipt_void_no_delete BEFORE DELETE ON receipt_void
                        BEGIN SELECT RAISE(ABORT, 'receipt_void is append-only'); END""")
        # A merge's receipt for units that came in as another item on a buy line or the Stock audit: the item they came
        # in as. Such units are never re-booked by a later merge (they were counted once already).
        self._add_column(conn, 'receipt', 'merged_from', 'INTEGER')
        # Alex 11-17: "When scanning an item in that isn't in the audit / linked via an order I have no way of adding a
        # buy cost." A cost typed on a box page line after the save, for units with no buy line, one row per receipt
        # (append-only). Like a cost typed at receiving, a Usual unit cost never overwrites it; a blank puts the
        # receipt back to "not entered".
        conn.execute("""CREATE TABLE IF NOT EXISTS cost_set (id INTEGER PRIMARY KEY, receipt_id INTEGER NOT NULL, cents INTEGER,
                        old_cents INTEGER, user_id INTEGER, at TEXT NOT NULL)""")
        conn.execute('CREATE INDEX IF NOT EXISTS idx_cost_set_receipt ON cost_set(receipt_id)')
        conn.execute("""CREATE TRIGGER IF NOT EXISTS cost_set_no_update BEFORE UPDATE ON cost_set
                        BEGIN SELECT RAISE(ABORT, 'cost_set is append-only'); END""")
        conn.execute("""CREATE TRIGGER IF NOT EXISTS cost_set_no_delete BEFORE DELETE ON cost_set
                        BEGIN SELECT RAISE(ABORT, 'cost_set is append-only'); END""")

    def _seed(self, conn):
        ts = now_utc()
        # Owner 1 is the business itself, named once when the database is made (config.APP_NAME). A later rename of
        # the app never adds a second house owner.
        conn.execute('INSERT INTO owner (name, is_client, created_at) SELECT ?, 0, ? WHERE NOT EXISTS (SELECT 1 FROM owner)',
                     (config.APP_NAME, ts))
        for code, name in VIRTUAL_LOCATIONS:
            conn.execute("INSERT OR IGNORE INTO location (code, name, kind, created_at) VALUES (?, ?, 'virtual', ?)",
                         (code, name, ts))
        conn.execute("INSERT OR IGNORE INTO location (code, name, kind, created_at) VALUES ('RECV', 'Receiving dock', 'dock', ?)",
                     (ts,))

    def _virtual_id(self, conn, code: str) -> int:
        row = conn.execute("SELECT id FROM location WHERE code = ? AND kind = 'virtual'", (code,)).fetchone()
        if not row:
            raise WmsError(f'Unknown virtual location {code}')
        return row[0]

    # ------------------------------------------------------------------ owners

    def list_owners(self) -> list:
        return self._q('SELECT id, name, is_client FROM owner ORDER BY is_client, name')

    def create_owner(self, name: str, is_client: bool = True) -> dict:
        name = (name or '').strip()
        if not name:
            raise WmsError('Owner name is required')
        with self._tx() as c:
            try:
                cur = c.execute('INSERT INTO owner (name, is_client, created_at) VALUES (?, ?, ?)',
                                (name, 1 if is_client else 0, now_utc()))
            except sqlite3.IntegrityError:
                raise WmsError(f'Owner "{name}" already exists')
        return {'id': cur.lastrowid, 'name': name, 'is_client': int(is_client)}

    # ------------------------------------------------------------------ locations and boxes

    def list_locations(self, include_virtual: bool = False) -> list:
        where = '' if include_virtual else "WHERE l.kind != 'virtual'"
        rows = self._q(f"""
            SELECT l.id, l.code, l.name, l.site, l.kind, l.archived,
                   (SELECT COUNT(*) FROM box b WHERE b.location_id = l.id AND b.archived = 0) AS boxes,
                   (SELECT COALESCE(SUM(s.qty), 0) FROM stock_level s
                      LEFT JOIN box b ON b.id = s.box_id
                     WHERE (s.location_id = l.id OR b.location_id = l.id) AND s.qty > 0) AS units
              FROM location l {where} ORDER BY l.kind = 'virtual', l.code""")
        for r in rows:
            r['scan_code'] = make_code('location', r['id'])
        # Bins in number order (A1, A2 ... A10, not A1, A10, A100): split the code into text and number runs.
        natural = lambda c: [(0, int(t), '') if t.isdigit() else (1, 0, t) for t in re.findall(r'\d+|\D+', (c or '').upper())]  # noqa: E731
        return sorted(rows, key=lambda r: (r['kind'] == 'virtual', natural(r['code'])))

    def create_locations(self, codes: list, kind: str = 'shelf') -> list:
        clean = []
        for code in codes:
            code = (code or '').strip().upper()
            if not code:
                continue
            if code in dict(VIRTUAL_LOCATIONS):
                raise WmsError(f'{code} is reserved')
            clean.append(code)
        if not clean:
            raise WmsError('No location codes given')
        if kind not in ('shelf', 'dock'):
            raise WmsError('Location kind must be shelf or dock')
        created = []
        with self._tx() as c:
            ts = now_utc()
            for code in clean:
                exists = c.execute('SELECT id FROM location WHERE code = ?', (code,)).fetchone()
                if exists:
                    continue
                cur = c.execute('INSERT INTO location (code, kind, created_at) VALUES (?, ?, ?)', (code, kind, ts))
                created.append({'id': cur.lastrowid, 'code': code, 'scan_code': make_code('location', cur.lastrowid)})
        return created

    def create_place(self, site: str, room: str = '') -> dict:
        """A place to keep stock loose: a site (Main warehouse, Alex's house) and, optionally, a room
        or area inside it (Back room). Its code is made from the names."""
        site = (site or '').strip()[:60]
        room = (room or '').strip()[:60]
        if len(site) < 2:
            raise WmsError('Pick or name the site (for example Main warehouse)')
        name = room or site
        with self._tx() as c:
            if c.execute("""SELECT 1 FROM location WHERE lower(site) = lower(?) AND lower(name) = lower(?) AND archived = 0""",
                         (site, name)).fetchone():
                raise WmsError(f'{site}{" · " + room if room else ""} is already a place')
            initials = ''.join(w[0] for w in re.findall(r'[A-Za-z0-9]+', site)).upper()[:4]
            base = (initials + '-' + re.sub(r'[^A-Z0-9]', '', room.upper())[:8]) if room else re.sub(r'[^A-Z0-9]', '', site.upper())[:10]
            base = base or 'PLACE'
            code, n = base, 1
            while c.execute('SELECT 1 FROM location WHERE code = ?', (code,)).fetchone() or code in dict(VIRTUAL_LOCATIONS):
                n += 1
                code = f'{base}{n}'
            cur = c.execute("INSERT INTO location (code, name, site, kind, created_at) VALUES (?, ?, ?, 'shelf', ?)",
                            (code, name, site, now_utc()))
            return {'id': cur.lastrowid, 'code': code, 'name': name, 'site': site, 'scan_code': make_code('location', cur.lastrowid)}

    def rename_location(self, location_id: int, code: str = None, name: str = None, archived: bool = None):
        loc = self._q1('SELECT * FROM location WHERE id = ?', (location_id,))
        if not loc or loc['kind'] == 'virtual':
            raise WmsError('Location not found')
        if archived:
            here = self._q1("""SELECT COUNT(*) n FROM box WHERE location_id = ? AND archived = 0""", (location_id,))['n']
            loose = self._q1('SELECT COALESCE(SUM(qty),0) n FROM stock_level WHERE location_id = ? AND qty > 0', (location_id,))['n']
            if here or loose:
                raise WmsError('Empty this location before archiving it')
        with self._tx() as c:
            if code is not None:
                code = code.strip().upper()
                if not code:
                    raise WmsError('Code cannot be blank')
                try:
                    c.execute('UPDATE location SET code = ? WHERE id = ?', (code, location_id))
                except sqlite3.IntegrityError:
                    raise WmsError(f'Location {code} already exists')
            if name is not None:
                c.execute('UPDATE location SET name = ? WHERE id = ?', (name.strip(), location_id))
            if archived is not None:
                c.execute('UPDATE location SET archived = ? WHERE id = ?', (1 if archived else 0, location_id))

    def get_location(self, location_id: int) -> dict:
        loc = self._q1('SELECT id, code, name, site, kind, archived FROM location WHERE id = ?', (location_id,))
        if not loc:
            raise WmsError('Location not found')
        loc['scan_code'] = make_code('location', loc['id'])
        loc['boxes'] = self.list_boxes(location_id=location_id)
        loc['loose'] = self._contents(location_id=location_id)
        return loc

    def home_location(self) -> int:
        """The place "Main warehouse" itself (not a room in it): where boxes sit on the shelves. Made once."""
        r = self._q1("""SELECT id FROM location WHERE site = ? AND name = ? AND kind = 'shelf' AND archived = 0
                         ORDER BY id LIMIT 1""", (HOME_SITE, HOME_SITE))
        return r['id'] if r else self.create_place(HOME_SITE)['id']

    @staticmethod
    def _check_box_name(c, name: str, box_id: int = 0):
        """Box names (the name written on it, A31) are unique among boxes in use, ignoring case and a dash
        (sweep A6: the Boxes page saved "A-31" and the Box bar then made a second box "A31")."""
        if name and c.execute("""SELECT 1 FROM box WHERE REPLACE(UPPER(name), '-', '') = REPLACE(UPPER(?), '-', '')
                                    AND archived = 0 AND id != ?""", (name, box_id)).fetchone():
            raise WmsError(f'There is already a box named {clean_box_name(name)}')

    def create_boxes(self, count: int, user_id: int, location_id: int = None, name: str = '', kind: str = 'box') -> list:
        if not 1 <= int(count) <= 500:
            raise WmsError('Create between 1 and 500 boxes at a time')
        if kind not in ('box', 'tote', 'pallet'):
            raise WmsError('Kind must be box, tote or pallet')
        name = clean_box_name(name)
        if name and int(count) > 1:
            raise WmsError('Each box has its own name: make named boxes one at a time')
        if location_id is None:
            location_id = self.home_location()
        with self._tx() as c:
            self._check_box_name(c, name)
            loc = c.execute('SELECT kind FROM location WHERE id = ? AND archived = 0', (location_id,)).fetchone()
            if not loc or loc[0] == 'virtual':
                raise WmsError('Pick a real location for new boxes')
            ts = now_utc()
            ids = []
            for _ in range(int(count)):
                cur = c.execute('INSERT INTO box (name, kind, location_id, created_at) VALUES (?, ?, ?, ?)',
                                (name, kind, location_id, ts))
                ids.append(cur.lastrowid)
        return [self._box_row(i) for i in ids]

    def _box_row(self, box_id: int) -> dict:
        rows = self.list_boxes(box_ids=[box_id])
        if not rows:
            raise WmsError('Box not found')
        return rows[0]

    def list_boxes(self, location_id: int = None, box_ids: list = None, include_archived: bool = False,
                   search: str = '') -> list:
        where, params = [], []
        if location_id is not None:
            where.append('b.location_id = ?')
            params.append(location_id)
        if box_ids:
            where.append(f"b.id IN ({','.join('?' * len(box_ids))})")
            params.extend(box_ids)
        if not include_archived and not box_ids:
            where.append('b.archived = 0')
        if search:
            where.append('(b.name LIKE ? OR b.notes LIKE ? OR CAST(b.id AS TEXT) = ?)')
            params.extend([f'%{search}%', f'%{search}%', search.lstrip('0')])
        sql = f"""
            SELECT b.id, b.name, b.kind, b.notes, b.archived, b.created_at,
                   b.location_id, l.code AS location_code, l.site AS location_site, l.name AS location_name,
                   COALESCE((SELECT SUM(qty) FROM stock_level s WHERE s.box_id = b.id AND s.qty > 0), 0) AS units,
                   COALESCE((SELECT COUNT(DISTINCT product_id) FROM stock_level s WHERE s.box_id = b.id AND s.qty > 0), 0) AS skus,
                   (SELECT MAX(a.at) FROM action a JOIN stock_move m ON m.action_id = a.id
                     WHERE m.from_box_id = b.id OR m.to_box_id = b.id) AS last_activity,
                   (SELECT MAX(a.at) FROM count_line cl JOIN action a ON a.id = cl.action_id
                     WHERE cl.box_id = b.id AND a.reversed_by IS NULL) AS last_counted
              FROM box b JOIN location l ON l.id = b.location_id
              {'WHERE ' + ' AND '.join(where) if where else ''}
             ORDER BY b.id DESC"""
        rows = self._q(sql, params)
        for r in rows:
            r['scan_code'] = make_code('box', r['id'])
        return rows

    def get_box(self, box_id: int) -> dict:
        box = self._box_row(box_id)
        box['contents'] = self._contents(box_id=box_id)
        box['history'] = self.list_actions(box_id=box_id, limit=50)
        box['renames'] = self._q('SELECT old_name, new_name, at, user_id FROM box_rename WHERE box_id = ? ORDER BY id DESC',
                                 (box_id,))
        # A line saved before a rename names the box as it was then: say what it is called now ("into A55 (now A8)").
        for h in box['history']:
            for r in box['renames']:
                if h.get('at') and h['at'] < r['at'] and box['name'] and r['old_name'] != box['name']:
                    h['summary'], n = re.subn(rf'(?<![\w-]){re.escape(r["old_name"])}(?![\w-])',
                                              f'{r["old_name"]} (now {box["name"]})', h['summary'] or '', count=1)
                    if n:
                        break
        return box

    def update_box(self, box_id: int, name: str = None, notes: str = None, archived: bool = None, user_id: int = None):
        self._box_row(box_id)
        if archived:
            n = self._q1('SELECT COALESCE(SUM(qty),0) n FROM stock_level WHERE box_id = ? AND qty > 0', (box_id,))['n']
            if n:
                raise WmsError('Empty the box before archiving it')
        with self._tx() as c:
            cur = c.execute('SELECT name, archived FROM box WHERE id = ?', (box_id,)).fetchone()
            if name is not None:
                name = clean_box_name(name)
                self._check_box_name(c, name, box_id)
                c.execute('UPDATE box SET name = ? WHERE id = ?', (name, box_id))
                if (cur[0] or '') and name != cur[0]:
                    c.execute('INSERT INTO box_rename (box_id, old_name, new_name, at, user_id) VALUES (?, ?, ?, ?, ?)',
                              (box_id, cur[0], name, now_utc(), user_id))
            elif archived is False and cur[1]:
                self._check_box_name(c, cur[0], box_id)   # brought back: its name must still be free
            if notes is not None:
                c.execute('UPDATE box SET notes = ? WHERE id = ?', (notes.strip(), box_id))
            if archived is not None:
                c.execute('UPDATE box SET archived = ? WHERE id = ?', (1 if archived else 0, box_id))

    def _contents(self, box_id: int = 0, location_id: int = 0) -> list:
        rows = self._q("""
            SELECT s.product_id, s.unit_id, s.qty, p.sku, p.title, p.tracking, p.condition,
                   o.name AS owner, u.condition AS unit_condition, u.cost_cents AS unit_cost_cents,
                   (SELECT ph.id FROM photo ph WHERE ph.removed = 0 AND
                       ((s.unit_id != 0 AND ph.unit_id = s.unit_id) OR ph.product_id = s.product_id)
                     ORDER BY (ph.unit_id IS NOT NULL) DESC, ph.id LIMIT 1) AS photo_id
              FROM stock_level s
              JOIN product p ON p.id = s.product_id
              JOIN owner o ON o.id = p.owner_id
              LEFT JOIN unit u ON u.id = s.unit_id
             WHERE s.box_id = ? AND s.location_id = ? AND s.qty > 0
             ORDER BY p.title, s.unit_id""", (box_id, location_id))
        for r in rows:
            r['unit_code'] = make_code('unit', r['unit_id']) if r['unit_id'] else None
        return rows

    def holder_contents(self, holder: dict) -> list:
        """What a box, or a shelf's loose items, holds now (the rows the Count screen shows)."""
        box_id, location_id = int(holder.get('box_id') or 0), int(holder.get('location_id') or 0)
        return self._contents(box_id=box_id) if box_id else self._contents(location_id=location_id)

    def last_holder_move(self, holder: dict):
        """{user_id, at} of the newest stock move into or out of a box or a shelf's loose items, or None."""
        box_id, location_id = int(holder.get('box_id') or 0), int(holder.get('location_id') or 0)
        if box_id:
            where, params = '(m.from_box_id = ? OR m.to_box_id = ?)', (box_id, box_id)
        else:
            where = '((m.from_location_id = ? AND m.from_box_id = 0) OR (m.to_location_id = ? AND m.to_box_id = 0))'
            params = (location_id, location_id)
        return self._q1(f"""SELECT a.user_id, a.at FROM stock_move m JOIN action a ON a.id = m.action_id
                             WHERE {where} ORDER BY a.id DESC LIMIT 1""", params)

    # ------------------------------------------------------------------ products

    def list_products(self, search: str = '', owner_id: int = None, include_archived: bool = False,
                      in_stock_only: bool = False, ids: list = None, limit: int = 2000) -> list:
        """limit: the Products page's cap. lookup (by ids), summary and the export pass None (sweep A7: products past
        2,000 could not be found by a scan, and the Overview undercounted)."""
        where, params = [], []
        if ids is not None:
            if not ids:
                return []
            where.append(f"p.id IN ({','.join('?' * len(ids))})")
            params.extend(int(i) for i in ids)
        if not include_archived:
            where.append('p.archived = 0')
        if owner_id:
            where.append('p.owner_id = ?')
            params.append(owner_id)
        if search:
            like = f'%{search}%'
            where.append("""(p.sku LIKE ? OR p.title LIKE ? OR p.notes LIKE ? OR EXISTS
                (SELECT 1 FROM product_identifier i WHERE i.product_id = p.id AND i.value LIKE ?))""")
            params.extend([like, like, like, like])
        rows = self._q(f"""
            WITH onhand AS (
                SELECT s.product_id, SUM(s.qty) qty, COUNT(DISTINCT CASE WHEN s.box_id THEN s.box_id END) boxes
                  FROM stock_level s
                  LEFT JOIN location l ON l.id = s.location_id
                 WHERE s.qty > 0 AND (s.box_id != 0 OR l.kind != 'virtual')
                 GROUP BY s.product_id)
            SELECT p.id, p.sku, p.title, p.tracking, p.condition, p.owner_id, o.name AS owner, p.archived,
                   p.updated_at, COALESCE(oh.qty, 0) AS on_hand, COALESCE(oh.boxes, 0) AS boxes,
                   (SELECT ph.id FROM photo ph WHERE ph.product_id = p.id AND ph.removed = 0 ORDER BY ph.id LIMIT 1) AS photo_id,
                   (SELECT GROUP_CONCAT(i.kind || ':' || i.value, '|') FROM product_identifier i WHERE i.product_id = p.id) AS idents
              FROM product p JOIN owner o ON o.id = p.owner_id
              LEFT JOIN onhand oh ON oh.product_id = p.id
             {'WHERE ' + ' AND '.join(where) if where else ''}
             ORDER BY p.updated_at DESC
             {'LIMIT ' + str(int(limit)) if limit else ''}""", params)
        costs = self._avg_costs()
        unit_vals = {r['product_id']: r['v'] for r in self._q("""
            SELECT s.product_id, SUM(COALESCE(u.cost_cents, 0)) v FROM stock_level s
              JOIN unit u ON u.id = s.unit_id
              LEFT JOIN location l ON l.id = s.location_id
             WHERE s.qty > 0 AND s.unit_id != 0 AND (s.box_id != 0 OR l.kind != 'virtual')
             GROUP BY s.product_id""")}
        out = []
        for r in rows:
            if in_stock_only and not r['on_hand']:
                continue
            r['identifiers'] = [dict(zip(('kind', 'value'), x.split(':', 1))) for x in (r.pop('idents') or '').split('|') if x]
            if r['tracking'] == 'unit':
                r['avg_cost_cents'] = round(unit_vals.get(r['id'], 0) / r['on_hand']) if r['on_hand'] else None
                r['value_cents'] = unit_vals.get(r['id'], 0)
            else:
                r['avg_cost_cents'] = costs.get(r['id'])
                r['value_cents'] = (costs.get(r['id']) or 0) * r['on_hand']
            out.append(r)
        return out

    def _avg_costs(self, product_id: int = None) -> dict:
        """Weighted average cost per product over receipts that were not undone."""
        sql = f"""SELECT r.product_id, CAST(ROUND(1.0 * SUM((r.qty - {VOIDED_SQL}) * r.unit_cost_cents)
                                                 / NULLIF(SUM(r.qty - {VOIDED_SQL}), 0)) AS INTEGER) avg
                   FROM receipt r JOIN action a ON a.id = r.action_id
                  WHERE a.reversed_by IS NULL AND r.cost_missing = 0 {{}} GROUP BY r.product_id"""
        if product_id:
            rows = self._q(sql.format('AND r.product_id = ?'), (product_id,))
        else:
            rows = self._q(sql.format(''))
        return {r['product_id']: r['avg'] for r in rows if r['avg'] is not None}

    def get_product(self, product_id: int) -> dict:
        p = self._q1("""SELECT p.*, o.name AS owner FROM product p JOIN owner o ON o.id = p.owner_id
                         WHERE p.id = ?""", (product_id,))
        if not p:
            raise WmsError('Product not found')
        p['identifiers'] = self._q('SELECT id, kind, value FROM product_identifier WHERE product_id = ? ORDER BY kind',
                                   (product_id,))
        p['photos'] = self._q("""SELECT id, unit_id, created_at FROM photo
                                  WHERE product_id = ? AND removed = 0 ORDER BY id""", (product_id,))
        p['avg_cost_cents'] = self._avg_costs(product_id).get(product_id)
        p['where'] = self._q("""
            SELECT s.box_id, s.location_id, s.unit_id, s.qty,
                   COALESCE(lb.code, l.code) AS location_code, b.name AS box_name, l.kind AS location_kind,
                   COALESCE(lb.site, l.site) AS location_site, COALESCE(lb.name, l.name) AS location_name,
                   u.condition AS unit_condition, u.cost_cents AS unit_cost_cents
              FROM stock_level s
              LEFT JOIN box b ON b.id = s.box_id
              LEFT JOIN location lb ON lb.id = b.location_id
              LEFT JOIN location l ON l.id = s.location_id
              LEFT JOIN unit u ON u.id = s.unit_id
             WHERE s.product_id = ? AND s.qty > 0 AND (s.box_id != 0 OR l.kind != 'virtual')
             ORDER BY location_code, s.box_id""", (product_id,))
        for w in p['where']:
            w['box_code'] = make_code('box', w['box_id']) if w['box_id'] else None
            w['unit_code'] = make_code('unit', w['unit_id']) if w['unit_id'] else None
        p['on_hand'] = sum(w['qty'] for w in p['where'])
        # qty = units still booked on the receipt: a merge that re-booked some (receipt_void) took them off it.
        p['receipts'] = self._q(f"""
            SELECT r.id, r.qty - {VOIDED_SQL} AS qty, {VOIDED_SQL} AS voided, CASE WHEN r.cost_missing THEN NULL ELSE r.unit_cost_cents END AS unit_cost_cents,
                   r.supplier, r.reference, r.purchased_at, r.purchase_line_id,
                   a.at, a.reversed_by, a.user_id
              FROM receipt r JOIN action a ON a.id = r.action_id
             WHERE r.product_id = ? ORDER BY r.id DESC LIMIT 100""", (product_id,))
        p['history'] = self.list_actions(product_id=product_id, limit=50)
        return p

    def _clean_identifiers(self, identifiers) -> list:
        out = []
        for ident in identifiers or []:
            kind = (ident.get('kind') or '').strip().upper()
            value = (ident.get('value') or '').strip()
            if not value:
                continue
            if kind not in IDENTIFIER_KINDS:
                raise WmsError(f'Unknown identifier type {kind}')
            out.append((kind, value))
        return out

    def save_product(self, data: dict, product_id: int = None, user_id: int = 0) -> dict:
        sku = (data.get('sku') or '').strip()
        title = (data.get('title') or '').strip()
        if not sku or not title:
            raise WmsError('SKU and title are required')
        tracking = data.get('tracking') or 'qty'
        if tracking not in ('qty', 'unit'):
            raise WmsError('Tracking must be qty or unit')
        owner_id = int(data.get('owner_id') or 1)
        idents = self._clean_identifiers(data.get('identifiers'))
        ts = now_utc()
        with self._tx() as c:
            if not c.execute('SELECT 1 FROM owner WHERE id = ?', (owner_id,)).fetchone():
                raise WmsError('Owner not found')
            try:
                if product_id is None:
                    cur = c.execute("""INSERT INTO product (owner_id, sku, title, tracking, condition, notes,
                                       default_cost_cents, created_at, updated_at, edited_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                                    (owner_id, sku, title, tracking, data.get('condition') or 'New',
                                     data.get('notes') or '', data.get('default_cost_cents'), ts, ts, ts))
                    product_id = cur.lastrowid
                else:
                    old = c.execute('SELECT tracking FROM product WHERE id = ?', (product_id,)).fetchone()
                    if not old:
                        raise WmsError('Product not found')
                    if old[0] != tracking and c.execute('SELECT 1 FROM stock_move WHERE product_id = ? LIMIT 1',
                                                        (product_id,)).fetchone():
                        raise WmsError('Tracking cannot change once stock has moved')
                    # archived only changes when the request names it: the edit form doesn't, so an edit of an
                    # archived product keeps it archived (archive_product is the way in and out).
                    c.execute("""UPDATE product SET owner_id=?, sku=?, title=?, tracking=?, condition=?, notes=?,
                                 default_cost_cents=?, archived=COALESCE(?, archived), updated_at=?, edited_at=? WHERE id=?""",
                              (owner_id, sku, title, tracking, data.get('condition') or 'New', data.get('notes') or '',
                               data.get('default_cost_cents'),
                               (1 if data['archived'] else 0) if 'archived' in data else None, ts, ts, product_id))
            except sqlite3.IntegrityError:
                raise WmsError(f'SKU {sku} is already used by another product')
            for col in ('brand', 'image_url', 'pkg_l_mm', 'pkg_w_mm', 'pkg_h_mm', 'pkg_weight_g'):
                if col in data:
                    c.execute(f'UPDATE product SET {col} = ? WHERE id = ?', (data[col] if data[col] != '' else None
                                                                              if col != 'brand' else '', product_id))
            c.execute('DELETE FROM product_identifier WHERE product_id = ?', (product_id,))
            for kind, value in idents:
                c.execute('INSERT OR IGNORE INTO product_identifier (product_id, kind, value) VALUES (?,?,?)',
                          (product_id, kind, value))
            filled = self._fill_costs(c, product_id, data.get('default_cost_cents'), user_id, ts)
        p = self.get_product(product_id)
        p['cost_filled'] = filled
        return p

    @staticmethod
    def _fill_costs(c, product_id: int, cents, user_id: int, ts: str, receipt_ids: list = None) -> dict:
        """Alex 11-13: "I edited this buy cost but the inventory page isn't changing". Units saved with no cost
        (receipt.cost_missing) take the item's usual cost, so Avg cost and Value count them. A receipt filled this way
        follows later edits of the usual cost, and a blank puts it back to "not entered". A cost a person typed at
        receiving is never touched. receipt_ids: only these (a box page pile change refills just its own line).
        Returns {receipts, units, cents}."""
        try:
            cents = int(cents) if cents not in (None, '') else None
        except (TypeError, ValueError):
            cents = None
        cents = cents if cents and cents > 0 else None
        rows = c.execute(f"""
            SELECT r.id, r.qty - {VOIDED_SQL} AS qty, r.unit_cost_cents, r.cost_missing,
                   (SELECT f.cents FROM cost_fill f WHERE f.receipt_id = r.id ORDER BY f.id DESC LIMIT 1) AS fill_cents,
                   EXISTS (SELECT 1 FROM cost_fill f WHERE f.receipt_id = r.id) AS was_filled,
                   EXISTS (SELECT 1 FROM cost_set s WHERE s.receipt_id = r.id AND s.cents IS NOT NULL AND s.id = (
                             SELECT MAX(s2.id) FROM cost_set s2 WHERE s2.receipt_id = r.id)
                             AND s.at >= COALESCE((SELECT MAX(f.at) FROM cost_fill f WHERE f.receipt_id = r.id), '')) AS typed
              FROM receipt r JOIN action a ON a.id = r.action_id
             WHERE r.product_id = ? AND a.reversed_by IS NULL""", (product_id,)).fetchall()
        if receipt_ids is not None:
            keep = set(receipt_ids)
            rows = [x for x in rows if x[0] in keep]
        n = units = 0
        for rid, qty, cost, missing, fill_cents, was_filled, typed in rows:
            # qty = units still booked (less what a standing merge re-booked). A fully voided receipt is still filled,
            # so it carries the cost if the merge is undone (review 11-17), but counts no units.
            # A cost a person typed (at receiving, or later on the box page: cost_set) is never ours.
            ours = not typed and (missing or (was_filled and fill_cents is not None and cost == fill_cents))
            if not ours or (cents is None and missing) or (cents is not None and not missing and cost == cents):
                continue
            if cents is None:
                c.execute('UPDATE receipt SET unit_cost_cents = 0, cost_missing = 1 WHERE id = ?', (rid,))
                c.execute('UPDATE unit SET cost_cents = NULL WHERE receipt_id = ?', (rid,))
            else:
                c.execute('UPDATE receipt SET unit_cost_cents = ?, cost_missing = 0 WHERE id = ?', (cents, rid))
                c.execute('UPDATE unit SET cost_cents = ? WHERE receipt_id = ?', (cents, rid))
            c.execute('INSERT INTO cost_fill (receipt_id, cents, user_id, at) VALUES (?,?,?,?)', (rid, cents, user_id, ts))
            n += 1 if qty > 0 else 0
            units += max(0, qty)
        return {'receipts': n, 'units': units, 'cents': cents}

    def set_receipt_cost(self, receipt_ids: list, cents, user_id: int) -> dict:
        """A cost typed after the save for receipts with no buy line (a buy line's cost is the order's). cents None =
        "not entered" again. One cost_set row per receipt that changes. Returns {receipts, units, cents}."""
        if cents in (None, ''):
            cents = None
        else:
            try:
                cents = int(cents)
            except (TypeError, ValueError):
                raise WmsError('Type the cost in dollars, like 12.99')
            if not 0 <= cents <= 100_000_00:
                raise WmsError('Cost must be between $0 and $100,000')
        ts = now_utc()
        n = units = 0
        with self._tx() as c:
            for rid in receipt_ids:
                r = c.execute("""SELECT r.qty, r.unit_cost_cents, r.cost_missing, r.purchase_line_id, a.reversed_by
                                     FROM receipt r JOIN action a ON a.id = r.action_id WHERE r.id = ?""", (rid,)).fetchone()
                if not r or r[4]:
                    raise WmsError('Those units were taken back out (Undo). Nothing was saved.')
                if r[3]:
                    raise WmsError("These came in on an order: their cost is the order's.")
                old = None if r[2] else r[1]
                # The same cost the Usual unit cost filled in is still a person's choice from now on (review 11-17:
                # skipped, the next usual cost edit overwrote it). Timestamps are to the second and ids come from two
                # tables, so a typed cost wins a same-second tie, as in _fill_costs.
                filled = (c.execute("""SELECT kind FROM (SELECT 'set' AS kind, at, id FROM cost_set WHERE receipt_id = ?
                                       UNION ALL SELECT 'fill', at, id FROM cost_fill WHERE receipt_id = ?)
                                       ORDER BY at DESC, kind = 'fill', id DESC LIMIT 1""", (rid, rid)).fetchone() or [''])[0] == 'fill'
                if old == cents and not (filled and cents is not None):
                    continue
                if cents is None:
                    c.execute('UPDATE receipt SET unit_cost_cents = 0, cost_missing = 1 WHERE id = ?', (rid,))
                else:
                    c.execute('UPDATE receipt SET unit_cost_cents = ?, cost_missing = 0 WHERE id = ?', (cents, rid))
                c.execute('UPDATE unit SET cost_cents = ? WHERE receipt_id = ?', (cents, rid))
                c.execute('INSERT INTO cost_set (receipt_id, cents, old_cents, user_id, at) VALUES (?,?,?,?,?)',
                          (rid, cents, old, user_id, ts))
                n += 1
                units += r[0]
        return {'receipts': n, 'units': units, 'cents': cents}

    # ------------------------------------------------------------------ delete / archive (Alex 11-11)
    # The ledger is append-only, so a product that ever held stock can't be erased: it is archived (hidden from
    # Inventory, search and the photo matches; its history stays). One that never held stock (made by mistake, a
    # duplicate) is deleted for good, and whatever pointed at it goes back to unlinked.

    HISTORY = (('stock_move', 'stock moves'), ('receipt', 'receipts'), ('unit', 'one-off units'),
               ('box_add', 'box adds'), ('audit_extra', 'audit extras'), ('count_line', 'counts'))
    POINTERS = (('purchase_line', 'order lines'), ('listing', 'listings'), ('sales_line', 'sales'))

    def _tables(self) -> set:
        return {r['name'] for r in self._q("SELECT name FROM sqlite_master WHERE type = 'table'")}

    def product_usage(self, product_id: int) -> dict:
        """What stops a delete (history), what stops an archive (stock on hand), and what a delete would unlink."""
        p = self._q1('SELECT id, sku, title, archived, updated_at FROM product WHERE id = ?', (product_id,))
        if not p:
            raise WmsError('Product not found')
        tables = self._tables()
        count = lambda t: self._q1(f'SELECT COUNT(*) AS n FROM {t} WHERE product_id = ?', (product_id,))['n']  # noqa: E731
        history = {label: n for t, label in self.HISTORY if t in tables and (n := count(t))}
        pointers = {label: n for t, label in self.POINTERS if t in tables and (n := count(t))}
        places = self._q("""
            SELECT s.qty, s.box_id, COALESCE(NULLIF(b.name, ''), CASE WHEN s.box_id THEN 'Box ' || s.box_id END, l.name) AS place
              FROM stock_level s LEFT JOIN box b ON b.id = s.box_id LEFT JOIN location l ON l.id = s.location_id
             WHERE s.product_id = ? AND s.qty > 0 AND (s.box_id != 0 OR l.kind != 'virtual')
             ORDER BY s.qty DESC""", (product_id,))
        return {**p, 'history': history, 'pointers': pointers, 'on_hand': sum(x['qty'] for x in places),
                'places': [{'place': x['place'], 'qty': x['qty']} for x in places], 'can_delete': not history}

    def delete_product(self, product_id: int, user_id: int) -> dict:
        u = self.product_usage(product_id)
        if u['history']:
            what = ', '.join(f'{n} {k}' for k, n in u['history'].items())
            raise WmsError(f'This product has history ({what}), so it can\'t be erased. Archive it instead: it leaves '
                           'Inventory and searches, and its history stays.')
        snap = self.get_product(product_id)
        tables = self._tables()
        with self._tx() as c:
            # Checked again inside the write: a receipt saved a moment ago must win over the delete.
            if c.execute('SELECT 1 FROM stock_move WHERE product_id = ? LIMIT 1', (product_id,)).fetchone():
                raise WmsError('Stock just moved on this product, so it can\'t be erased now. Archive it instead.')
            for t in ('purchase_line', 'listing', 'sales_line', 'photo_read'):
                if t in tables:
                    c.execute(f'UPDATE {t} SET product_id = NULL WHERE product_id = ?', (product_id,))
            if 'photo_freed' in tables:   # an erased item's photo-id photo may go on the right item (census 11-17)
                c.execute("""INSERT OR IGNORE INTO photo_freed (photo_id) SELECT id FROM photo
                              WHERE product_id = ? AND removed = 0 AND source = 'photo-id'""", (product_id,))
            c.execute('UPDATE photo SET product_id = NULL, removed = 1 WHERE product_id = ?', (product_id,))
            if 'scan_code' in tables:
                c.execute('DELETE FROM scan_code WHERE product_id = ?', (product_id,))
            c.execute('DELETE FROM product_identifier WHERE product_id = ?', (product_id,))
            c.execute('DELETE FROM stock_level WHERE product_id = ? AND qty = 0', (product_id,))
            c.execute('INSERT INTO product_deleted (at, user_id, product_id, sku, title, data) VALUES (?,?,?,?,?,?)',
                      (now_utc(), user_id, product_id, snap['sku'], snap['title'],
                       json.dumps({'product': {k: snap.get(k) for k in ('sku', 'title', 'brand', 'owner_id', 'tracking',
                                                                         'condition', 'notes', 'default_cost_cents')},
                                   'identifiers': snap['identifiers'], 'unlinked': u['pointers']}, default=str)))
            c.execute('DELETE FROM product WHERE id = ?', (product_id,))
        return {'deleted': product_id, 'title': snap['title'], 'unlinked': u['pointers']}

    def archive_product(self, product_id: int, user_id: int, archived: bool = True) -> dict:
        u = self.product_usage(product_id)
        if archived and u['on_hand']:
            where = ', '.join(f"{x['qty']} in {x['place']}" for x in u['places'][:4])
            raise WmsError(f"{u['on_hand']} still on hand ({where}). Ship, remove or move them first, then archive it: "
                           'archived stock would drop out of sight.')
        with self._tx() as c:
            c.execute('UPDATE product SET archived = ?, updated_at = ?, edited_at = updated_at, updated_by = ? WHERE id = ?',
                      (1 if archived else 0, now_utc(), user_id, product_id))
        return self.get_product(product_id)

    def lookup(self, value: str) -> list:
        """Products whose SKU or any identifier equals the scanned value."""
        value = (value or '').strip()
        if not value:
            return []
        rows = self._q("""
            SELECT DISTINCT p.id FROM product p
              LEFT JOIN product_identifier i ON i.product_id = p.id
             WHERE p.sku = ? OR i.value = ?""", (value, value))
        if not rows and value.isdigit() and len(value) in (12, 13, 14):
            # A UPC-A scanned as EAN-13 gains a leading 0; GTIN-14 may carry two.
            stripped = value.lstrip('0')
            rows = self._q("""SELECT DISTINCT product_id AS id FROM product_identifier
                               WHERE LTRIM(value, '0') = ? AND kind IN ('UPC','EAN','GTIN')""", (stripped,))
        return self.list_products(include_archived=True, ids=[r['id'] for r in rows], limit=None)

    # ------------------------------------------------------------------ photos

    def add_photo(self, path: str, user_id: int, product_id: int = None, unit_id: int = None, source: str = 'upload') -> dict:
        """An uploaded or catalog photo. A photo-id photo goes on through PhotoId.claim (one check-and-write)."""
        if not product_id and not unit_id:
            raise WmsError('A photo needs a product or a unit')
        with self._tx() as c:
            if unit_id:
                row = c.execute('SELECT product_id FROM unit WHERE id = ?', (unit_id,)).fetchone()
                if not row:
                    raise WmsError('Unit not found')
                product_id = row[0]
            elif not c.execute('SELECT 1 FROM product WHERE id = ?', (product_id,)).fetchone():
                raise WmsError('Product not found')
            cur = c.execute('INSERT INTO photo (product_id, unit_id, path, user_id, created_at, source) VALUES (?,?,?,?,?,?)',
                            (product_id, unit_id, path, user_id, now_utc(), source))
            c.execute('UPDATE product SET updated_at = ? WHERE id = ?', (now_utc(), product_id))
        return {'id': cur.lastrowid, 'product_id': product_id, 'unit_id': unit_id}

    def photo_path(self, photo_id: int):
        row = self._q1('SELECT path FROM photo WHERE id = ?', (photo_id,))
        return row['path'] if row else None

    def remove_photo(self, photo_id: int):
        with self._tx() as c:
            c.execute('UPDATE photo SET removed = 1 WHERE id = ?', (photo_id,))

    # ------------------------------------------------------------------ units

    def get_unit(self, unit_id: int) -> dict:
        u = self._q1("""SELECT u.*, p.sku, p.title, p.owner_id FROM unit u JOIN product p ON p.id = u.product_id
                         WHERE u.id = ?""", (unit_id,))
        if not u:
            raise WmsError('Unit not found')
        u['scan_code'] = make_code('unit', unit_id)
        u['photos'] = self._q('SELECT id FROM photo WHERE unit_id = ? AND removed = 0 ORDER BY id', (unit_id,))
        u['holder'] = self._q1("""
            SELECT s.box_id, s.location_id, COALESCE(lb.code, l.code) AS location_code, l.kind AS location_kind
              FROM stock_level s LEFT JOIN box b ON b.id = s.box_id
              LEFT JOIN location lb ON lb.id = b.location_id LEFT JOIN location l ON l.id = s.location_id
             WHERE s.unit_id = ? AND s.qty > 0""", (unit_id,))
        return u

    def update_unit(self, unit_id: int, condition: str = None, notes: str = None):
        with self._tx() as c:
            if not c.execute('SELECT 1 FROM unit WHERE id = ?', (unit_id,)).fetchone():
                raise WmsError('Unit not found')
            if condition is not None:
                c.execute('UPDATE unit SET condition = ? WHERE id = ?', (condition.strip(), unit_id))
            if notes is not None:
                c.execute('UPDATE unit SET notes = ? WHERE id = ?', (notes.strip(), unit_id))

    # ------------------------------------------------------------------ ledger core

    def _holder(self, c, holder: dict, allow_virtual: bool = False) -> tuple:
        """Normalise {'box_id': n} / {'location_id': n} into (box_id, location_id)."""
        box_id = int(holder.get('box_id') or 0)
        location_id = int(holder.get('location_id') or 0)
        if bool(box_id) == bool(location_id):
            raise WmsError('Pick exactly one box or one location')
        if box_id:
            row = c.execute('SELECT archived FROM box WHERE id = ?', (box_id,)).fetchone()
            if not row:
                raise WmsError(f'Box {box_id} not found')
            if row[0]:
                raise WmsError(f'Box {box_id} is archived')
            return box_id, 0
        row = c.execute('SELECT kind, archived, code FROM location WHERE id = ?', (location_id,)).fetchone()
        if not row:
            raise WmsError('Location not found')
        if row[1]:
            raise WmsError(f'Location {row[2]} is archived')
        if row[0] == 'virtual' and not allow_virtual:
            raise WmsError(f'{row[2]} is not a place on the shelves')
        return 0, location_id

    def _is_virtual(self, c, location_id: int) -> bool:
        if not location_id:
            return False
        return c.execute('SELECT kind FROM location WHERE id = ?', (location_id,)).fetchone()[0] == 'virtual'

    def _new_action(self, c, user_id: int, kind: str, summary: str, note: str = '', device: str = '',
                    reverses: int = None) -> int:
        cur = c.execute("""INSERT INTO action (at, user_id, kind, summary, note, device, reverses_action_id)
                           VALUES (?,?,?,?,?,?,?)""", (now_utc(), user_id, kind, summary, note or '', device or '', reverses))
        return cur.lastrowid

    def _move(self, c, action_id: int, product_id: int, unit_id: int, qty: int,
              src: tuple, dst: tuple):
        """Write one stock move and update the cache. src/dst are (box_id, location_id)."""
        if qty <= 0:
            raise WmsError('Quantity must be at least 1')
        if src == dst:
            raise WmsError('Source and destination are the same')
        if unit_id and qty != 1:
            raise WmsError('A unit moves one at a time')
        have = c.execute("""SELECT qty FROM stock_level WHERE product_id=? AND unit_id=? AND box_id=? AND location_id=?""",
                         (product_id, unit_id, src[0], src[1])).fetchone()
        have = have[0] if have else 0
        if not self._is_virtual(c, src[1]) and have < qty:
            title = self._item(c, product_id)
            raise WmsError(f'Only {have} of {title} in {self._label(c, src)}, cannot take {qty}')
        if unit_id and self._is_virtual(c, src[1]):
            # A unit can only leave a virtual holder if it is there (undo of a sale, for example).
            if have < 1:
                raise WmsError(f'Unit {unit_id} is not in that place')
        c.execute("""INSERT INTO stock_move (action_id, product_id, unit_id, qty, from_box_id, from_location_id,
                     to_box_id, to_location_id) VALUES (?,?,?,?,?,?,?,?)""",
                  (action_id, product_id, unit_id, qty, src[0], src[1], dst[0], dst[1]))
        for (b, l), delta in ((src, -qty), (dst, qty)):
            c.execute("""INSERT INTO stock_level (product_id, unit_id, box_id, location_id, qty) VALUES (?,?,?,?,?)
                         ON CONFLICT(product_id, unit_id, box_id, location_id) DO UPDATE SET qty = qty + excluded.qty""",
                      (product_id, unit_id, b, l, delta))
        c.execute('DELETE FROM stock_level WHERE qty = 0')
        c.execute('UPDATE product SET updated_at = ? WHERE id = ?', (now_utc(), product_id))

    def _move_box(self, c, action_id: int, box_id: int, to_location_id: int):
        row = c.execute('SELECT location_id, archived FROM box WHERE id = ?', (box_id,)).fetchone()
        if not row:
            raise WmsError('Box not found')
        if row[1]:
            raise WmsError(f'Box {box_id} is archived')
        if row[0] == to_location_id:
            return False
        self._check_open(c, 0, to_location_id)
        c.execute('INSERT INTO box_move (action_id, box_id, from_location_id, to_location_id) VALUES (?,?,?,?)',
                  (action_id, box_id, row[0], to_location_id))
        c.execute('UPDATE box SET location_id = ? WHERE id = ?', (to_location_id, box_id))
        return True

    def _label(self, c, holder: tuple) -> str:
        """A place as people say it (flow audit 11-12, finding 3: history read "into Box 8" for A36): the box's
        written name, a shelf's code, or the outside world in words."""
        if holder[0]:
            r = c.execute('SELECT name FROM box WHERE id = ?', (holder[0],)).fetchone()
            return (r[0] if r and r[0] else '') or f'Box {holder[0]}'
        code = c.execute('SELECT code FROM location WHERE id = ?', (holder[1],)).fetchone()[0]
        return PLACE_WORDS.get(code, code)

    @staticmethod
    def _item(c, product_id: int) -> str:
        r = c.execute('SELECT title, sku FROM product WHERE id = ?', (product_id,)).fetchone()
        return short_title(r[0] or r[1]) if r else '?'

    def pretty_actions(self, rows: list) -> list:
        """Action summaries written before 11-12 named boxes by id (Box 8) and items by SKU, and a sale straight from
        the seller read "Received 1 x B0EJC0N2LN into CUSTOMER". Summaries are append-only, so they are reworded at
        read time; new ones are written this way."""
        if not rows:
            return rows
        boxes = {r['id']: r['name'] for r in self._q("SELECT id, name FROM box WHERE name != ''")}
        skus = {}
        need = {m.group(1) for r in rows for m in [re.match(r'(?:Undo: )?Received \d+ x (\S+) into ', r['summary'] or '')] if m}
        if need:
            q = ','.join('?' * len(need))
            skus = {r['sku']: short_title(r['title'] or r['sku']) for r in self._q(f'SELECT sku, title FROM product WHERE sku IN ({q})', sorted(need))}
        for r in rows:
            t = r['summary'] or ''
            t = re.sub(r'\bBox (\d+)\b', lambda m: boxes.get(int(m.group(1)), m.group(0)), t)
            t = re.sub(r'^(Undo: )?Received (\d+) x (\S+) into CUSTOMER$',
                       lambda m: f"{m.group(1) or ''}Sold {m.group(2)} x {skus.get(m.group(3), m.group(3))}, never on a shelf", t)
            t = re.sub(r'^(Undo: )?Received (\d+) x (\S+) into ',
                       lambda m: f"{m.group(1) or ''}Received {m.group(2)} x {skus.get(m.group(3), m.group(3))} into ", t)
            t = re.sub(r'^Moved (\d+) items? ', lambda m: f"Moved {m.group(1)} unit{'s' if m.group(1) != '1' else ''} ", t)
            for code, word in PLACE_WORDS.items():
                t = re.sub(rf'(\b(?:to|into|of|from) ){code}\b', rf'\g<1>{word}', t)   # places only, never a title
            r['summary'] = t
        return rows

    # ------------------------------------------------------------------ actions people take

    @staticmethod
    def _check_line_product(c, product_id: int, sku: str, line_pid, asin: str, upc: str, order: str):
        """A purchase line is received only as its own product (sweep B5/A8: a $99.99 line of another ASIN set a
        product's avg cost and was linked to it). The line's product, else its ASIN or UPC on the product; a line with
        neither code (store email items) takes any product, as Receive's search does."""
        if line_pid and line_pid != product_id and not c.execute(
                'SELECT 1 FROM product WHERE id = ? AND archived = 0', (line_pid,)).fetchone():
            line_pid = None   # its product was deleted: the line is free again
        if line_pid:
            if line_pid != product_id:
                raise WmsError(f'That line on order {order} belongs to another product')
            return
        if not asin and not upc:
            return
        codes = {(r[0], r[1].upper()) for r in c.execute(
            'SELECT kind, value FROM product_identifier WHERE product_id = ?', (product_id,))}
        if asin and (('ASIN', asin) in codes or (sku or '').upper() == asin):
            return
        if upc and any(k in ('UPC', 'EAN', 'GTIN') and v.lstrip('0') == upc for k, v in codes):
            return
        raise WmsError(f'That line on order {order} is for {asin or upc}, which is not on this product')

    def receive(self, user_id: int, product_id: int, qty: int, unit_cost_cents: int, to: dict,
                supplier: str = '', reference: str = '', note: str = '', device: str = '',
                unit_condition: str = '', purchase_line_id: int = None, purchased_at: str = None,
                allow_virtual: bool = False, merged_from: int = None) -> dict:
        """merged_from: a merge's receipt for units that came in as that item on a buy line or the audit (merge.py).
        allow_virtual: receive straight into a virtual location (the audit's "sold elsewhere": bought and
        already gone, so VENDOR -> CUSTOMER in one action, with the receipt carrying the order's cost)."""
        qty = int(qty)
        if not 1 <= qty <= 100000:
            raise WmsError('Quantity must be between 1 and 100,000')
        cost_missing = unit_cost_cents in (None, '')
        if not cost_missing and int(unit_cost_cents) < 0:
            raise WmsError('Cost cannot be negative')
        unit_cost_cents = 0 if cost_missing else int(unit_cost_cents)
        with self._tx() as c:
            p = c.execute('SELECT sku, tracking FROM product WHERE id = ? AND archived = 0', (product_id,)).fetchone()
            if not p:
                raise WmsError('Product not found')
            dst = self._holder(c, to, allow_virtual=allow_virtual)
            if p[1] == 'unit' and qty > 200:
                raise WmsError('Receive at most 200 one-off units at a time')
            vendor = (0, self._virtual_id(c, 'VENDOR'))
            to_customer = not dst[0] and c.execute("SELECT code FROM location WHERE id = ?", (dst[1],)).fetchone()[0] == 'CUSTOMER'
            action_id = self._new_action(c, user_id, 'receive',
                                         f'Sold {qty} x {self._item(c, product_id)}, never on a shelf' if to_customer else
                                         f'Received {qty} x {self._item(c, product_id)} into {self._label(c, dst)}',
                                         note, device)
            if purchase_line_id:
                line = c.execute(f"""SELECT {LINE_LEFT_SQL}, o.ordered_at, {LINE_RETURNED_SQL}, l.cancelled, o.state,
                                            l.product_id, UPPER(COALESCE(l.asin, '')), LTRIM(COALESCE(l.upc, ''), '0'),
                                            l.unit_cost_cents, o.external_id
                                       FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id WHERE l.id = ?""",
                                 (purchase_line_id,)).fetchone()
                if not line:
                    raise WmsError('Purchase line not found')
                if line[3] or line[4] == 'cancelled':
                    raise WmsError(f'Order {line[9]} was cancelled. Receive it without the order.')
                self._check_line_product(c, product_id, p[0], line[5], line[6], line[7], line[9])
                # A line with no price (store emails often list none) is "cost not entered", never a real $0
                # (sweep B8: a $0 receipt pulled the average cost down).
                if not cost_missing and unit_cost_cents == 0 and not line[8]:
                    cost_missing = True
                if qty > max(0, line[0]):
                    sent = f' ({line[2]} went back to the seller)' if line[2] else ''
                    raise WmsError(f'That order line has only {max(0, line[0])} left to receive{sent}. '
                                   'Receive the extra without the order.')
                purchased_at = purchased_at or line[1]
                c.execute("""UPDATE purchase_line SET product_id = ? WHERE id = ? AND (product_id IS NULL
                                OR product_id NOT IN (SELECT id FROM product WHERE archived = 0))""", (product_id, purchase_line_id))
            cur = c.execute("""INSERT INTO receipt (action_id, product_id, qty, unit_cost_cents, supplier, reference,
                                  purchase_line_id, purchased_at, cost_missing, merged_from) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                            (action_id, product_id, qty, unit_cost_cents, supplier or '', reference or '',
                             purchase_line_id, purchased_at, 1 if cost_missing else 0, merged_from))
            receipt_id = cur.lastrowid
            unit_ids = []
            if p[1] == 'unit':
                for _ in range(qty):
                    u = c.execute("""INSERT INTO unit (product_id, condition, cost_cents, receipt_id, created_at)
                                     VALUES (?,?,?,?,?)""", (product_id, unit_condition or '', None if cost_missing else unit_cost_cents,
                                                             receipt_id, now_utc()))
                    # A new unit enters the world at VENDOR, then moves to the shelf.
                    c.execute('INSERT INTO stock_level (product_id, unit_id, box_id, location_id, qty) VALUES (?,?,?,?,0)',
                              (product_id, u.lastrowid, vendor[0], vendor[1]))
                    c.execute("""INSERT INTO stock_move (action_id, product_id, unit_id, qty, from_box_id, from_location_id,
                                 to_box_id, to_location_id) VALUES (?,?,?,1,?,?,?,?)""",
                              (action_id, product_id, u.lastrowid, vendor[0], vendor[1], dst[0], dst[1]))
                    c.execute('UPDATE stock_level SET qty = -1 WHERE product_id=? AND unit_id=? AND box_id=? AND location_id=?',
                              (product_id, u.lastrowid, vendor[0], vendor[1]))
                    c.execute('INSERT INTO stock_level (product_id, unit_id, box_id, location_id, qty) VALUES (?,?,?,?,1)',
                              (product_id, u.lastrowid, dst[0], dst[1]))
                    unit_ids.append(u.lastrowid)
            else:
                self._move(c, action_id, product_id, 0, qty, vendor, dst)
        return {'action_id': action_id, 'receipt_id': receipt_id,
                'units': [{'id': i, 'scan_code': make_code('unit', i)} for i in unit_ids]}

    def move(self, user_id: int, lines: list, src: dict, dst: dict, note: str = '', device: str = '') -> dict:
        """Move some contents from one holder to another. lines: [{product_id, unit_id?, qty}]."""
        # A line the page sent with qty 0 (picked, then unpicked) moves nothing (red line 11-11: it moved 1).
        lines = [x for x in lines or [] if x.get('qty') in (None, '') or int(x['qty']) > 0]
        if not lines:
            raise WmsError('Nothing selected to move')
        with self._tx() as c:
            s = self._holder(c, src)
            d = self._holder(c, dst)
            total = sum(int(x.get('qty') or 1) for x in lines)
            action_id = self._new_action(c, user_id, 'move',
                                         f'Moved {total} unit{"s" if total != 1 else ""} {self._label(c, s)} -> {self._label(c, d)}',
                                         note, device)
            for line in lines:
                self._move(c, action_id, int(line['product_id']), int(line.get('unit_id') or 0),
                           int(line.get('qty') or 1), s, d)
        return {'action_id': action_id}

    def move_all(self, user_id: int, src: dict, dst: dict, note: str = '', device: str = '') -> dict:
        """Empty one holder into another (box into box, loose shelf stock into a box)."""
        with self._tx() as c:
            s = self._holder(c, src)
            rows = c.execute('SELECT product_id, unit_id, qty FROM stock_level WHERE box_id=? AND location_id=? AND qty > 0',
                             s).fetchall()
        if not rows:
            raise WmsError('That place is already empty')
        return self.move(user_id, [{'product_id': r[0], 'unit_id': r[1], 'qty': r[2]} for r in rows], src, dst,
                         note=note, device=device)

    def gather(self, user_id: int, product_id: int, dst: dict, note: str = '', device: str = '') -> dict:
        """Put every unit of one product that is on the shelves into one box or place (Alex 11-13: one SKU sits in
        several boxes). One action, so one Undo puts every pile back. It moves what is there now, under the lock."""
        with self._tx() as c:
            d = self._holder(c, dst)
            rows = [r for r in c.execute(
                """SELECT s.unit_id, s.qty, s.box_id, s.location_id FROM stock_level s
                     LEFT JOIN location l ON l.id = s.location_id
                    WHERE s.product_id = ? AND s.qty > 0 AND (s.box_id != 0 OR l.kind != 'virtual')
                    ORDER BY s.box_id, s.location_id, s.unit_id""", (int(product_id),)).fetchall()
                    if (r[2], r[3]) != d]
            if not rows:
                raise WmsError(f'Nothing to move: all of it is already in {self._label(c, d)}')
            total = sum(r[1] for r in rows)
            places = len({(r[2], r[3]) for r in rows})
            action_id = self._new_action(
                c, user_id, 'move',
                f'Moved {total} unit{"s" if total != 1 else ""} of {self._item(c, int(product_id))} from '
                f'{places} place{"s" if places != 1 else ""} -> {self._label(c, d)}', note, device)
            for unit_id, qty, box_id, location_id in rows:
                self._move(c, action_id, int(product_id), unit_id, qty, (box_id, location_id), d)
        return {'action_id': action_id, 'moved': total, 'places': places}

    def move_boxes(self, user_id: int, box_ids: list, location_id: int, note: str = '', device: str = '') -> dict:
        """Put whole boxes (with everything in them) at a location."""
        if not box_ids:
            raise WmsError('No boxes given')
        with self._tx() as c:
            _, loc = self._holder(c, {'location_id': location_id})
            code = c.execute('SELECT code FROM location WHERE id = ?', (loc,)).fetchone()[0]
            names = ', '.join(str(b) for b in box_ids[:5]) + ('...' if len(box_ids) > 5 else '')
            action_id = self._new_action(c, user_id, 'box_move',
                                         f'Put {len(box_ids)} box{"es" if len(box_ids) != 1 else ""} ({names}) at {code}',
                                         note, device)
            moved = sum(1 for b in box_ids if self._move_box(c, action_id, int(b), loc))
            if not moved:
                raise WmsError(f'{"That box is" if len(box_ids) == 1 else "Those boxes are"} already at {code}')
        return {'action_id': action_id, 'moved': moved}

    def remove(self, user_id: int, src: dict, lines: list, target: str, note: str = '', device: str = '') -> dict:
        """Take stock off the shelves: sold, sent to FBA/WFS, damaged, returned to client."""
        if target not in REMOVE_TARGETS_INTERNAL:
            raise WmsError('Pick where the stock went')
        # A line the page sent with qty 0 (picked, then unpicked) moves nothing (red line 11-11: it moved 1).
        lines = [x for x in lines or [] if x.get('qty') in (None, '') or int(x['qty']) > 0]
        if not lines:
            raise WmsError('Nothing selected')
        with self._tx() as c:
            s = self._holder(c, src)
            d = (0, self._virtual_id(c, target))
            total = sum(int(x.get('qty') or 1) for x in lines)
            action_id = self._new_action(c, user_id, 'remove', f'{total} out of {self._label(c, s)} to {PLACE_WORDS.get(target, target)}', note, device)
            for line in lines:
                self._move(c, action_id, int(line['product_id']), int(line.get('unit_id') or 0),
                           int(line.get('qty') or 1), s, d)
        return {'action_id': action_id}

    def count(self, user_id: int, holder: dict, counted: list, note: str = '', device: str = '') -> dict:
        """Blind count of one box or location. counted: [{product_id, unit_id?, qty}] for everything found.

        Anything expected but not listed counts as 0. Differences post as moves to or
        from ADJUST in one action, so the whole count can be undone."""
        with self._tx() as c:
            h = self._holder(c, holder)
            expected = {(r[0], r[1]): r[2] for r in c.execute(
                'SELECT product_id, unit_id, qty FROM stock_level WHERE box_id=? AND location_id=? AND qty > 0', h)}
            found = {}
            for line in counted:
                key = (int(line['product_id']), int(line.get('unit_id') or 0))
                q = int(line.get('qty') or 0)
                if q < 0:
                    raise WmsError('Counts cannot be negative')
                if key[1] and q > 1:
                    raise WmsError('A unit counts as 0 or 1')
                found[key] = found.get(key, 0) + q
            adjust = (0, self._virtual_id(c, 'ADJUST'))
            diffs = []
            keys = set(expected) | set(found)
            net = sum(found.get(k, 0) - expected.get(k, 0) for k in keys)
            action_id = self._new_action(c, user_id, 'count',
                                         f'Counted {self._label(c, h)}: ' +
                                         ('matched' if all(found.get(k, 0) == expected.get(k, 0) for k in keys)
                                          else f'net {net:+d}'), note, device)
            for key in sorted(keys):
                exp, got = expected.get(key, 0), found.get(key, 0)
                c.execute("""INSERT INTO count_line (action_id, box_id, location_id, product_id, unit_id, expected, counted)
                             VALUES (?,?,?,?,?,?,?)""", (action_id, h[0], h[1], key[0], key[1], exp, got))
                if got > exp:
                    if key[1]:
                        # A unit found here must come from wherever the ledger thinks it is.
                        prev = c.execute('SELECT box_id, location_id FROM stock_level WHERE unit_id = ? AND qty > 0',
                                         (key[1],)).fetchone()
                        self._move(c, action_id, key[0], key[1], 1, (prev[0], prev[1]) if prev else adjust, h)
                    else:
                        self._move(c, action_id, key[0], 0, got - exp, adjust, h)
                elif exp > got:
                    self._move(c, action_id, key[0], key[1], exp - got, h, adjust)
                if got != exp:
                    diffs.append({'product_id': key[0], 'unit_id': key[1], 'expected': exp, 'counted': got})
        return {'action_id': action_id, 'differences': diffs}

    def merged_target(self, product_id):
        """The item a standing merge (merge.py) folded this one into, followed down a chain; the item itself when none.
        A merge into the item's own Stock audit row keeps the item."""
        pid = product_id
        if not pid or not self._q1("SELECT 1 AS x FROM sqlite_master WHERE type = 'table' AND name = 'product_merge'"):
            return pid
        for _ in range(20):
            m = self._q1("""SELECT target_id FROM product_merge WHERE source_id = ? AND target_id != source_id
                             AND undone_at IS NULL ORDER BY id DESC LIMIT 1""", (pid,))
            if not m:
                break
            pid = m['target_id']
        return pid

    def undo(self, user_id: int, action_id: int, note: str = '', device: str = '') -> dict:
        """Reverse an action with opposite moves. The original stays in the history."""
        with self._tx() as c:
            return {'action_id': self._undo(c, user_id, action_id, note, device)}

    def undo_many(self, user_id: int, action_ids: list, note: str = '', device: str = '', merge_ok: bool = False) -> dict:
        """Undo several actions as one save, newest first: all of them or none (sweep A5: one failing left the rest
        half undone, and every retry said "already undone"). Ones already undone are skipped, so an add a failed
        older undo left half done can still finish."""
        with self._tx() as c:
            todo = [a for a in action_ids if not (c.execute('SELECT reversed_by FROM action WHERE id = ?', (a,)).fetchone() or [0])[0]]
            if not todo:
                raise WmsError('That action was already undone')
            return {'action_ids': [self._undo(c, user_id, a, note, device, merge_ok) for a in reversed(todo)]}

    def _check_open(self, c, box_id: int, location_id: int):
        """Stock never goes into a retired box or an archived place (sweep A2: an undo put 29 units in archived A24,
        where no screen showed them and every move out was refused)."""
        if box_id:
            row = c.execute('SELECT archived, name FROM box WHERE id = ?', (box_id,)).fetchone()
            if row and row[0]:
                raise WmsError(f'Box {row[1] or box_id} is retired. Bring it back first, or move the stock by hand.')
        elif location_id:
            row = c.execute('SELECT archived, code, kind FROM location WHERE id = ?', (location_id,)).fetchone()
            if row and row[0] and row[2] != 'virtual':
                raise WmsError(f'{row[1]} is archived. Bring it back first, or move the stock by hand.')

    def _undo(self, c, user_id: int, action_id: int, note: str, device: str, merge_ok: bool = False) -> int:
        a = c.execute('SELECT kind, summary, reversed_by, reverses_action_id FROM action WHERE id = ?',
                      (action_id,)).fetchone()
        if not a:
            raise WmsError('Action not found')
        if a[2]:
            raise WmsError('That action was already undone')
        if a[3]:
            raise WmsError('This is an undo. Redo the original action instead.')
        if c.execute(f"""SELECT 1 FROM receipt r WHERE r.action_id = ? AND {VOIDED_SQL} > 0""", (action_id,)).fetchone():
            raise WmsError('A merge of items took these units over already. Undo the merge first (History).')
        # Review 11-17: a merge's actions go back all together or not at all (Merge.undo). History's Undo of one of
        # them (say the source's units out of a box) brought the source back while the target kept its copy.
        if not merge_ok and c.execute("""SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'product_merge'""").fetchone() \
                and c.execute("""SELECT 1 FROM product_merge WHERE undone_at IS NULL
                                   AND ',' || action_ids || ',' LIKE '%,' || ? || ',%'""", (action_id,)).fetchone():
            raise WmsError('This was part of a merge of items. Undo it in History: that takes the whole merge back.')
        # An older action of an item merged into another since: undone, its units would come back as the archived item,
        # hidden from Inventory and search (review 11-17).
        if not merge_ok and c.execute("""SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'product_merge'""").fetchone():
            m = c.execute("""SELECT p.title FROM stock_move sm JOIN product_merge pm ON pm.source_id = sm.product_id
                               AND pm.target_id != pm.source_id AND pm.undone_at IS NULL
                               JOIN product p ON p.id = pm.target_id
                              WHERE sm.action_id = ? ORDER BY pm.id DESC LIMIT 1""", (action_id,)).fetchone()
            if m:
                raise WmsError(f'This item was merged into {(m[0] or "another item")[:60]} since. Undo that merge first '
                               '(History), or fix the count on the kept item.')
            # A merge into the item's own Stock audit row re-booked its extras as found. One saved before receipt_void
            # existed (live merge 1, item 620 -> row 297) left no void, so guard its older extra receipts here too
            # (review 11-17: undone, the box lost units the row still counts as found).
            has_counts = c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'audit_count'").fetchone()
            merges = c.execute("""SELECT pm.action_ids FROM receipt r JOIN product_merge pm ON pm.source_id = r.product_id
                                    AND pm.target_id = pm.source_id AND pm.undone_at IS NULL
                                   WHERE r.action_id = ? AND r.purchase_line_id IS NULL AND r.merged_from IS NULL
                                     AND NOT EXISTS (SELECT 1 FROM audit_count ac WHERE ac.action_id = r.action_id)""",
                               (action_id,)).fetchall() if has_counts else []
            for (ids,) in merges:
                acts = [int(a) for a in (ids or '').split(',') if a]
                q = ','.join('?' * len(acts))
                # Only a merge saved before receipt_void: it found units (audit count or buy line) and voided nothing.
                # A newer merge voids what it re-booked, and the check above guards exactly those receipts.
                old_style = acts and c.execute(f"""SELECT 1 WHERE EXISTS (SELECT 1 FROM audit_count WHERE action_id IN ({q})
                                                        UNION ALL SELECT 1 FROM receipt WHERE action_id IN ({q})
                                                                         AND purchase_line_id IS NOT NULL)
                                                    AND NOT EXISTS (SELECT 1 FROM receipt_void WHERE action_id IN ({q}))""",
                                               (*acts, *acts, *acts)).fetchone()
                if old_style and action_id < min(acts):
                    raise WmsError('A merge of items counted these units on its Stock audit row since. '
                                   'Undo that merge first (History).')
        new_id = self._new_action(c, user_id, 'undo', f'Undo: {a[1]}', note, device, reverses=action_id)
        for m in reversed(c.execute("""SELECT product_id, unit_id, qty, from_box_id, from_location_id, to_box_id,
                                       to_location_id FROM stock_move WHERE action_id = ? ORDER BY id""",
                                    (action_id,)).fetchall()):
            dst_box = m[5]
            if dst_box and c.execute('SELECT archived FROM box WHERE id = ?', (dst_box,)).fetchone()[0]:
                raise WmsError(f'Box {dst_box} is archived. Restore it first.')
            self._check_open(c, m[3], m[4])
            self._move(c, new_id, m[0], m[1], m[2], (m[5], m[6]), (m[3], m[4]))
        for bm in reversed(c.execute('SELECT box_id, from_location_id, to_location_id FROM box_move WHERE action_id = ? ORDER BY id',
                                     (action_id,)).fetchall()):
            cur_loc = c.execute('SELECT location_id FROM box WHERE id = ?', (bm[0],)).fetchone()[0]
            if cur_loc != bm[2]:
                raise WmsError(f'Box {bm[0]} has moved again since. Move it by hand.')
            self._move_box(c, new_id, bm[0], bm[1])
        c.execute('UPDATE action SET reversed_by = ? WHERE id = ?', (new_id, action_id))
        # The Add card's photo (Alex 11-17): a photo a box page save put on its item comes off with that save, in this
        # same transaction, whichever Undo runs (the line's or History's). Review 11-17: it was a separate write.
        if c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'box_add_photo'").fetchone():
            # A line on a Stock audit row keeps its save as count_id (the batch's newest audit_count row), not the
            # action: one of the batch's actions undone takes the photo off too (review round 5, 11-17).
            audit = c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'audit_count'").fetchone()
            for add_id, photo_id in c.execute("""SELECT bp.add_id, bp.photo_id FROM box_add_photo bp JOIN box_add b ON b.id = bp.add_id
                                                 WHERE b.action_id = ? OR ',' || COALESCE(b.action_ids, '') || ',' LIKE ?"""
                                              + (""" OR (b.kind = 'audit' AND b.count_id IN (
                                                     SELECT c2.id FROM audit_count c1 JOIN audit_count c2
                                                         ON c2.item_id = c1.item_id AND (c2.batch = c1.batch OR c2.id = c1.id)
                                                      WHERE c1.action_id = ?))""" if audit else ''),
                                              (action_id, f'%,{action_id},%') + ((action_id,) if audit else ())).fetchall():
                c.execute('DELETE FROM box_add_photo WHERE add_id = ?', (add_id,))
                if c.execute('SELECT 1 FROM box_add_photo WHERE photo_id = ?', (photo_id,)).fetchone():
                    continue   # another standing line saved from this photo still holds it (review round 6)
                ph = c.execute('SELECT path, product_id FROM photo WHERE id = ? AND removed = 0', (photo_id,)).fetchone()
                if ph:
                    c.execute('UPDATE photo SET removed = 1 WHERE id = ?', (photo_id,))
                    c.execute('INSERT OR IGNORE INTO photo_freed (photo_id) VALUES (?)', (photo_id,))   # may go on the right item
                    if c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'photo_read'").fetchone():
                        c.execute('UPDATE photo_read SET product_id = NULL, saved_title = NULL WHERE photo = ? AND product_id = ?', ph)
        return new_id

    # ------------------------------------------------------------------ reading the ledger

    def list_actions(self, limit: int = 100, before_id: int = None, box_id: int = None, product_id: int = None,
                     location_id: int = None, user_id: int = None, kind: str = None) -> list:
        where, params = [], []
        if before_id:
            where.append('a.id < ?')
            params.append(before_id)
        if user_id:
            where.append('a.user_id = ?')
            params.append(user_id)
        if kind:
            where.append('a.kind = ?')
            params.append(kind)
        if box_id:
            where.append("""(EXISTS (SELECT 1 FROM stock_move m WHERE m.action_id = a.id AND (m.from_box_id = ? OR m.to_box_id = ?))
                         OR EXISTS (SELECT 1 FROM box_move bm WHERE bm.action_id = a.id AND bm.box_id = ?))""")
            params.extend([box_id, box_id, box_id])
        if product_id:
            where.append('EXISTS (SELECT 1 FROM stock_move m WHERE m.action_id = a.id AND m.product_id = ?)')
            params.append(product_id)
        if location_id:
            where.append("""(EXISTS (SELECT 1 FROM stock_move m WHERE m.action_id = a.id AND (m.from_location_id = ? OR m.to_location_id = ?))
                         OR EXISTS (SELECT 1 FROM box_move bm WHERE bm.action_id = a.id AND (bm.from_location_id = ? OR bm.to_location_id = ?)))""")
            params.extend([location_id] * 4)
        params.append(min(int(limit), 500))
        return self.pretty_actions(self._q(f"""SELECT a.id, a.at, a.user_id, a.kind, a.summary, a.note, a.reversed_by, a.reverses_action_id
                             FROM action a {'WHERE ' + ' AND '.join(where) if where else ''}
                            ORDER BY a.id DESC LIMIT ?""", params))

    def get_action(self, action_id: int) -> dict:
        a = self._q1('SELECT * FROM action WHERE id = ?', (action_id,))
        if not a:
            raise WmsError('Action not found')
        a['moves'] = self._q("""
            SELECT m.*, p.sku, p.title, fl.code AS from_location_code, tl.code AS to_location_code
              FROM stock_move m JOIN product p ON p.id = m.product_id
              LEFT JOIN location fl ON fl.id = m.from_location_id
              LEFT JOIN location tl ON tl.id = m.to_location_id
             WHERE m.action_id = ? ORDER BY m.id""", (action_id,))
        for m in a['moves']:
            m['from_box_code'] = make_code('box', m['from_box_id']) if m['from_box_id'] else None
            m['to_box_code'] = make_code('box', m['to_box_id']) if m['to_box_id'] else None
        a['box_moves'] = self._q("""
            SELECT bm.box_id, fl.code AS from_code, tl.code AS to_code FROM box_move bm
              JOIN location fl ON fl.id = bm.from_location_id JOIN location tl ON tl.id = bm.to_location_id
             WHERE bm.action_id = ?""", (action_id,))
        a['count_lines'] = self._q("""SELECT cl.*, p.sku, p.title FROM count_line cl JOIN product p ON p.id = cl.product_id
                                       WHERE cl.action_id = ?""", (action_id,))
        return a

    def summary(self) -> dict:
        products = self.list_products(limit=None)
        real = [p for p in products if p['on_hand'] > 0]
        boxes = self._q1('SELECT COUNT(*) n FROM box WHERE archived = 0')['n']
        empty_boxes = self._q1("""SELECT COUNT(*) n FROM box b WHERE archived = 0 AND NOT EXISTS
                                   (SELECT 1 FROM stock_level s WHERE s.box_id = b.id AND s.qty > 0)""")['n']
        last_count = self._q("""SELECT b.id, MAX(a.at) at FROM box b
                                  LEFT JOIN count_line cl ON cl.box_id = b.id
                                  LEFT JOIN action a ON a.id = cl.action_id AND a.reversed_by IS NULL
                                 WHERE b.archived = 0 AND EXISTS (SELECT 1 FROM stock_level s WHERE s.box_id = b.id AND s.qty > 0)
                                 GROUP BY b.id""")
        never_counted = sum(1 for r in last_count if not r['at'])
        by_owner = {}
        for p in real:
            o = by_owner.setdefault(p['owner'], {'owner': p['owner'], 'skus': 0, 'units': 0, 'value_cents': 0})
            o['skus'] += 1
            o['units'] += p['on_hand']
            o['value_cents'] += p['value_cents'] or 0
        missing_cost = sum(1 for p in real if p['avg_cost_cents'] is None)
        no_photo = sum(1 for p in real if not p['photo_id'])
        no_location_stock = self._q1("""SELECT COALESCE(SUM(s.qty),0) n FROM stock_level s JOIN location l ON l.id = s.location_id
                                         WHERE s.box_id = 0 AND l.kind = 'dock' AND s.qty > 0""")['n']
        return {
            'skus_in_stock': len(real),
            'units': sum(p['on_hand'] for p in real),
            'value_cents': sum(p['value_cents'] or 0 for p in real),
            'boxes': boxes,
            'empty_boxes': empty_boxes,
            'boxes_never_counted': never_counted,
            'locations': self._q1("SELECT COUNT(*) n FROM location WHERE kind != 'virtual' AND archived = 0")['n'],
            'by_owner': sorted(by_owner.values(), key=lambda o: -o['value_cents']),
            'needs_attention': {'missing_cost': missing_cost, 'no_photo': no_photo,
                                'loose_on_dock': no_location_stock},
            'integrity': self.last_integrity(),
        }

    # ------------------------------------------------------------------ the outside check

    def check_integrity(self, record: bool = True) -> dict:
        """Rebuild stock levels from the ledger and compare with the cache."""
        problems = []
        rebuilt = {}
        for row in self._conn().execute("""SELECT product_id, unit_id, from_box_id, from_location_id, to_box_id,
                                           to_location_id, qty FROM stock_move"""):
            pid, uid, fb, fl, tb, tl, q = row
            rebuilt[(pid, uid, fb, fl)] = rebuilt.get((pid, uid, fb, fl), 0) - q
            rebuilt[(pid, uid, tb, tl)] = rebuilt.get((pid, uid, tb, tl), 0) + q
        rebuilt = {k: v for k, v in rebuilt.items() if v}
        cached = {(r[0], r[1], r[2], r[3]): r[4] for r in self._conn().execute(
            'SELECT product_id, unit_id, box_id, location_id, qty FROM stock_level')}
        for key in set(rebuilt) | set(cached):
            if rebuilt.get(key, 0) != cached.get(key, 0):
                problems.append({'type': 'cache_mismatch', 'key': key, 'ledger': rebuilt.get(key, 0),
                                 'cache': cached.get(key, 0)})
        sums = {}
        for (pid, *_), q in cached.items():
            sums[pid] = sums.get(pid, 0) + q
        for pid, total in sums.items():
            if total:
                problems.append({'type': 'nonzero_sum', 'product_id': pid, 'sum': total})
        virtual = {r[0] for r in self._conn().execute("SELECT id FROM location WHERE kind = 'virtual'")}
        for (pid, uid, b, l), q in cached.items():
            if q < 0 and not (b == 0 and l in virtual):
                problems.append({'type': 'negative', 'product_id': pid, 'unit_id': uid, 'box_id': b,
                                 'location_id': l, 'qty': q})
        unit_places = {}
        for (pid, uid, b, l), q in cached.items():
            if uid and q > 0:
                unit_places[uid] = unit_places.get(uid, 0) + q
        for uid, n in unit_places.items():
            if n != 1:
                problems.append({'type': 'unit_in_many_places', 'unit_id': uid, 'count': n})
        for box_id, loc in self._conn().execute('SELECT id, location_id FROM box'):
            last = self._conn().execute('SELECT to_location_id FROM box_move WHERE box_id = ? ORDER BY id DESC LIMIT 1',
                                        (box_id,)).fetchone()
            if last and last[0] != loc:
                problems.append({'type': 'box_location_mismatch', 'box_id': box_id, 'ledger': last[0], 'box': loc})
        result = {'at': now_utc(), 'ok': not problems, 'moves_checked': self._q1('SELECT COUNT(*) n FROM stock_move')['n'],
                  'problems': problems[:50], 'problem_count': len(problems)}
        if record:
            with self._tx() as c:
                c.execute('INSERT INTO integrity_run (at, ok, details) VALUES (?,?,?)',
                          (result['at'], 1 if result['ok'] else 0, json.dumps(result, default=str)))
        return result

    def last_integrity(self):
        row = self._q1('SELECT details FROM integrity_run ORDER BY id DESC LIMIT 1')
        return json.loads(row['details']) if row else None

    # ------------------------------------------------------------------ exports (no lock-in)

    def export_rows(self, which: str) -> tuple:
        if which == 'inventory':
            rows = self._q("""
                SELECT o.name AS owner, p.sku, p.title, p.condition, s.qty,
                       CASE WHEN s.unit_id THEN 'SU' || printf('%07d', s.unit_id) ELSE '' END AS unit,
                       u.cost_cents AS unit_cost_cents,
                       CASE WHEN s.box_id THEN s.box_id ELSE '' END AS box_id,
                       COALESCE(lb.code, l.code) AS location
                  FROM stock_level s JOIN product p ON p.id = s.product_id JOIN owner o ON o.id = p.owner_id
                  LEFT JOIN unit u ON u.id = s.unit_id
                  LEFT JOIN box b ON b.id = s.box_id LEFT JOIN location lb ON lb.id = b.location_id
                  LEFT JOIN location l ON l.id = s.location_id
                 WHERE s.qty > 0 AND (s.box_id != 0 OR l.kind != 'virtual')
                 ORDER BY location, s.box_id, p.sku""")
            for r in rows:
                if r['box_id']:
                    r['box_id'] = make_code('box', r['box_id'])
            return ['owner', 'sku', 'title', 'condition', 'qty', 'unit', 'unit_cost_cents', 'box_id', 'location'], rows
        if which == 'products':
            rows = self.list_products(include_archived=True, limit=None)
            for r in rows:
                r['identifiers'] = '; '.join(f"{i['kind']}={i['value']}" for i in r['identifiers'])
            return ['id', 'owner', 'sku', 'title', 'tracking', 'condition', 'on_hand', 'avg_cost_cents',
                    'value_cents', 'identifiers', 'archived'], rows
        if which == 'ledger':
            rows = self._q("""
                SELECT a.id AS action_id, a.at, a.user_id, a.kind, a.summary, a.reversed_by, m.id AS move_id,
                       p.sku, m.unit_id, m.qty, m.from_box_id, fl.code AS from_location, m.to_box_id, tl.code AS to_location
                  FROM stock_move m JOIN action a ON a.id = m.action_id JOIN product p ON p.id = m.product_id
                  LEFT JOIN location fl ON fl.id = m.from_location_id LEFT JOIN location tl ON tl.id = m.to_location_id
                 ORDER BY m.id""")
            return ['action_id', 'at', 'user_id', 'kind', 'summary', 'reversed_by', 'move_id', 'sku', 'unit_id', 'qty',
                    'from_box_id', 'from_location', 'to_box_id', 'to_location'], rows
        raise WmsError('Unknown export')

    def backup(self, dest_path: str):
        """Consistent copy of the database (safe while the server runs)."""
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        dst = sqlite3.connect(dest_path)
        with dst:
            self._conn().backup(dst)
        dst.close()
