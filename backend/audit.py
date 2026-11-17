"""Stock audit: count everything bought in the last 24 months that hasn't sold, and box what's found.

The list (audit_item, one row per ASIN) started as a snapshot built from the purchase-to-sale matching in
a spreadsheet (the 10-02 sheet). Since 11-11 each row's starting count comes
from Boxloft's own records (_base: Amazon order lines up to the sheet's last buy, less seller returns, less Amazon
sales up to the sheet's sales day, oldest buy first); the sheet's number stays as `sheet_expected`. The team works
through it:

- Found N in box X: the units are received into the ledger against that ASIN's open purchase lines,
  oldest first, so cost and buy date come from the real orders. Anything past those lines is
  received at the ASIN's average cost. Each receipt is one audit_count row tied to its action, so
  an undone action stops counting (the same rule as receipts).
- Sold elsewhere (eBay, Walmart, Facebook, in person, Amazon...): received and sold in one ledger action
  (VENDOR -> CUSTOMER) against the same order lines, so the order's cost leaves with it, plus a sales
  record with the platform, price and date. Undo reverses both.
- Sent back to the seller: becomes (or claims) a purchase return on the order line (returns.py), so the
  refund owed shows on Returns.
- Not here: the row is closed with a reason (not found, damaged, other). Reopen undoes that.

Delivered, not scanned in: Amazon orders placed after the snapshot's last buy that Amazon shows delivered
more than DELIVERED_DAYS ago, with units nobody received, join the list (expected += those units). A split
order counts per package (inbound.portions): a package delivered 6 days ago joins while its twin still on
the way stays on Arriving.
Store orders from email (approved on Arriving) join the same way once delivered DELIVERED_DAYS ago, or
STORE_DAYS after the order when no delivery news came. Their lines have no ASIN, so each gets its own
row keyed 'LINE-<purchase_line id>' (audit_item.line_id). The snapshot knew only Amazon buys, so no
cutoff applies to them. When a person confirms such a row as one of our listings (Listed panel), the row takes
that listing's ASIN at read time (`match_asin`; the key stays LINE-<id>): its Amazon sales after the buy count,
and its product, photo, Listings link and barcode scans go by that ASIN. Undo the confirm and all of it goes back.
Amazon sales of one ASIN bought in several places go to the oldest buy first (Alex 11-10).

unaccounted() is the closing check: every purchase line with units still expected must sit in the
review queue, on Arriving or on this list. Anything else is a hole, and the Overview shows the count. Units
received outside the audit (Receive page) leave the expected count; units received by the audit count
as found, so the numbers stay put while the team works.

audit_count (found, close) and audit_outcome (sold, returned) are append-only, like the ledger. Each
expected unit ends as one outcome; outcomes() adds them up in units, cost and sale dollars by platform.
"""
import config
import functools
import json
import secrets
import threading
from datetime import date, datetime, timedelta

from inbound import portions, today_et
from listings import SCHEMA as LISTING_SCHEMA
from wms import LINE_LEFT_SQL, LINE_RECEIVED_SQL, LINE_RETURNED_SQL, LINE_SHELF_SQL, Conflict, Wms, WmsError, now_utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_item (
    id INTEGER PRIMARY KEY,
    asin TEXT UNIQUE NOT NULL COLLATE NOCASE,
    title TEXT NOT NULL DEFAULT '',
    brand TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    image_url TEXT,
    expected INTEGER NOT NULL,
    exp_listed INTEGER NOT NULL DEFAULT 0,
    exp_unlisted INTEGER NOT NULL DEFAULT 0,
    exp_problem INTEGER NOT NULL DEFAULT 0,
    exp_transit INTEGER NOT NULL DEFAULT 0,
    cost_cents INTEGER NOT NULL DEFAULT 0,
    avg_cost_cents INTEGER NOT NULL DEFAULT 0,
    first_buy TEXT,
    last_buy TEXT,
    orders TEXT NOT NULL DEFAULT '[]',
    snapshot TEXT NOT NULL DEFAULT '',
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_count (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES audit_item(id),
    at TEXT NOT NULL,
    user_id INTEGER,
    kind TEXT NOT NULL CHECK (kind IN ('found', 'close', 'reopen')),
    qty INTEGER NOT NULL DEFAULT 0,
    box_id INTEGER,
    location_id INTEGER,
    action_id INTEGER REFERENCES action(id),
    reason TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_count_item ON audit_count(item_id);
CREATE TRIGGER IF NOT EXISTS audit_count_no_update BEFORE UPDATE ON audit_count
BEGIN SELECT RAISE(ABORT, 'audit_count is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_count_no_delete BEFORE DELETE ON audit_count
BEGIN SELECT RAISE(ABORT, 'audit_count is append-only'); END;
-- Where the units went when they aren't on our shelves. kind 'undo' cancels the row in `undoes`.
CREATE TABLE IF NOT EXISTS audit_outcome (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES audit_item(id),
    at TEXT NOT NULL,
    user_id INTEGER,
    kind TEXT NOT NULL CHECK (kind IN ('sold', 'returned', 'undo')),
    qty INTEGER NOT NULL DEFAULT 0,
    cost_cents INTEGER NOT NULL DEFAULT 0,
    channel TEXT NOT NULL DEFAULT '',
    price_cents INTEGER,
    sold_on TEXT,
    order_ref TEXT NOT NULL DEFAULT '',
    line_id INTEGER,
    action_id INTEGER REFERENCES action(id),
    sale_line_id INTEGER,
    return_added INTEGER NOT NULL DEFAULT 0,
    return_state TEXT,
    reason TEXT NOT NULL DEFAULT '',
    undoes INTEGER,
    note TEXT NOT NULL DEFAULT '',
    batch TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_outcome_item ON audit_outcome(item_id);
CREATE TRIGGER IF NOT EXISTS audit_outcome_no_update BEFORE UPDATE ON audit_outcome
BEGIN SELECT RAISE(ABORT, 'audit_outcome is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_outcome_no_delete BEFORE DELETE ON audit_outcome
BEGIN SELECT RAISE(ABORT, 'audit_outcome is append-only'); END;
-- Recorded changes to a snapshot row's expected count, one per order: Amazon cut the order's quantity after
-- the CSV was built (18 ordered, 17 cancelled). Applied when the list is read, so the snapshot stays as built
-- and a re-import keeps the change. Append-only; a row with the opposite units takes one back.
CREATE TABLE IF NOT EXISTS audit_adjust (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES audit_item(id),
    at TEXT NOT NULL,
    user_id INTEGER,
    order_ref TEXT NOT NULL,
    units INTEGER NOT NULL,          -- change to expected: -17 = 17 fewer
    snap_units INTEGER NOT NULL,     -- the order's units in the snapshot
    live_units INTEGER NOT NULL,     -- the order's units on Amazon when recorded
    reason TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_adjust_item ON audit_adjust(item_id);
CREATE TRIGGER IF NOT EXISTS audit_adjust_no_update BEFORE UPDATE ON audit_adjust
BEGIN SELECT RAISE(ABORT, 'audit_adjust is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_adjust_no_delete BEFORE DELETE ON audit_adjust
BEGIN SELECT RAISE(ABORT, 'audit_adjust is append-only'); END;
-- One row per counted unit group that still stands (undone ones drop out): the analytics source.
CREATE VIEW IF NOT EXISTS audit_disposition AS
  SELECT ac.item_id, ac.at, ac.user_id, 'in_stock' AS outcome, ac.qty,
         ac.qty * COALESCE((SELECT NULLIF(r.unit_cost_cents * (1 - r.cost_missing), 0) FROM receipt r WHERE r.action_id = ac.action_id LIMIT 1), 0) AS cost_cents,
         '' AS channel, NULL AS price_cents, NULL AS sold_on, ac.box_id, ac.location_id, ac.note
    FROM audit_count ac JOIN action a ON a.id = ac.action_id
   WHERE ac.kind = 'found' AND a.reversed_by IS NULL
  UNION ALL
  SELECT ao.item_id, ao.at, ao.user_id, 'sold_elsewhere', ao.qty, ao.cost_cents, ao.channel, ao.price_cents, ao.sold_on,
         NULL, NULL, ao.note
    FROM audit_outcome ao JOIN action a ON a.id = ao.action_id
   WHERE ao.kind = 'sold' AND a.reversed_by IS NULL
  UNION ALL
  SELECT ao.item_id, ao.at, ao.user_id, 'sent_back', ao.qty, ao.cost_cents, '', NULL, NULL, NULL, NULL, ao.note
    FROM audit_outcome ao
   WHERE ao.kind = 'returned' AND NOT EXISTS (SELECT 1 FROM audit_outcome u WHERE u.kind = 'undo' AND u.undoes = ao.id);
"""

DELIVERED_DAYS = 5
STORE_DAYS = 21    # store order with no delivery news: expected this many days after it was placed
DELIVERED_SNAPSHOT = 'Delivered, not scanned in'
# The ASIN of the listing a person confirmed as this audit row (`i` = audit_item), NULL when none or undone.
MARK_ASIN_SQL = """(SELECT UPPER(li.asin) FROM listing_mark m JOIN listing li ON li.id = m.listing_id
                     WHERE m.item_id = i.id AND m.kind = 'confirm' AND li.asin != ''
                       AND NOT EXISTS (SELECT 1 FROM listing_mark u WHERE u.kind = 'undo' AND u.undoes = m.id)
                     ORDER BY m.id DESC LIMIT 1)"""   # "... unsold as of Amazon sales through 2026-09-16 ..."
AUDIT_SALE_NOTE = 'Stock audit: sold before it was in Boxloft'
# Receipts the audit made itself (Found it, Sold elsewhere): they count as found or sold on the row, not as received.
AUDIT_ACTIONS_SQL = """SELECT action_id FROM audit_count WHERE action_id IS NOT NULL
                       UNION SELECT action_id FROM audit_outcome WHERE action_id IS NOT NULL"""
# Units on line `l` that Sent back on an audit row already counts (accounted), so expected must not also drop them.
RETURN_CLAIMED_SQL = """COALESCE((SELECT SUM(ao.qty) FROM audit_outcome ao WHERE ao.line_id = l.id AND ao.kind = 'returned'
                          AND NOT EXISTS (SELECT 1 FROM audit_outcome u WHERE u.kind = 'undo' AND u.undoes = ao.id)), 0)"""
# Units of line `l` the audit should still look for: bought, less units returned to the seller before they reached
# our shelves (a return from the shelf was received first, so its receipt already counts), less Sent back claims.
LINE_EXPECTED_SQL = f"""(l.qty - MAX(0, {LINE_RETURNED_SQL} - {LINE_SHELF_SQL} - {RETURN_CLAIMED_SQL}))"""
# The one definition of "sold, nothing here shipped it" per purchase line (Alex 11-11 night: no sales date any more,
# the Amazon API has every sale). Every Amazon sale that Amazon shipped, that nothing in Boxloft shipped and that no
# audit save was ever tied to (a person's undo stands) takes one unit of its ASIN, oldest buy first (FIFO), and only a
# unit that had reached us by the sale day: a sale with no such unit was stock from before 2025-02 and takes nothing.
# Lots (`gross` = units that reached us, `since` = from when):
#  - Amazon stock lines up to the sheet's last buy, from the day they were ordered (most have no delivery record);
#  - later Amazon lines, only units Amazon delivered, from the delivery day (a box on its way never sold);
#  - approved store-email lines (by their ASIN or the listing a person confirmed on their audit row), from delivery,
#    or from the order day once STORE_DAYS passed with no delivery news.
# Greedy FIFO in one pass: with G_k = gross units arrived by sale k's day and Q_k = units sold through sale k, the
# sales took min(Q_n, min_k(G_k + Q_n - Q_k)) units, never more than the units nobody received (a unit scanned in was
# here, so it didn't sell). Receive books a scan against the oldest open line, so the arrival check runs on gross units
# per ASIN, and the units taken are then spread over the lines' units not received (`avail`), oldest first.
# Read by _base and _delivered (the audit's starting counts) and by wms.LINE_LEFT_SQL (left to receive everywhere:
# Purchases, Receive, Orders "coming", Returns).
_LOT_GROSS = f"(l.qty - {LINE_RETURNED_SQL} + {LINE_SHELF_SQL})"
PRESOLD_VIEW = f"""CREATE VIEW line_presold AS
WITH sheet AS (
  SELECT COALESCE(MAX(last_buy), '') AS cutoff FROM audit_item WHERE snapshot != '{DELIVERED_SNAPSHOT}'),
amz AS (
  SELECT l.id, UPPER(l.asin) AS asin, l.qty, o.ordered_at, o.state, o.delivered_on, {_LOT_GROSS} AS gross,
         {LINE_RECEIVED_SQL} AS got, o.ordered_at <= sheet.cutoff AS early,
         COALESCE((SELECT SUM(p.qty) FROM purchase_package p WHERE p.line_id = l.id AND p.state != 'cancelled'), 0) AS covered,
         COALESCE((SELECT SUM(p.qty) FROM purchase_package p WHERE p.line_id = l.id
                    AND p.state NOT IN ('delivered', 'cancelled')), 0) AS open_,
         (SELECT MAX(p.delivered_on) FROM purchase_package p WHERE p.line_id = l.id AND p.state = 'delivered') AS pk_on
    FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id CROSS JOIN sheet
   WHERE o.source = 'amazon' AND o.hidden = 0 AND l.cancelled = 0 AND l.asin != '' AND o.state != 'cancelled'
     AND o.kind != 'supplies' AND COALESCE(o.review, '') = '' AND COALESCE(o.ordered_at, '') != '' AND sheet.cutoff != ''),
lots0 AS (
  SELECT id, asin, ordered_at, substr(ordered_at, 1, 10) AS since, MAX(0, gross) AS gross, got FROM amz WHERE early
  UNION ALL
  SELECT id, asin, ordered_at, substr(COALESCE(pk_on, delivered_on, ordered_at), 1, 10),
         MAX(0, gross - open_ - CASE WHEN state = 'delivered' THEN 0 ELSE MAX(0, qty - covered) END), got
    FROM amz WHERE NOT early AND (state = 'delivered' OR pk_on IS NOT NULL)
  UNION ALL
  SELECT l.id, COALESCE(NULLIF(UPPER(l.asin), ''), (SELECT {MARK_ASIN_SQL} FROM audit_item i WHERE i.line_id = l.id LIMIT 1)),
         o.ordered_at, substr(CASE WHEN o.state = 'delivered' THEN COALESCE(o.delivered_on, o.ordered_at) ELSE o.ordered_at END, 1, 10),
         MAX(0, {_LOT_GROSS}), {LINE_RECEIVED_SQL}
    FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id
   WHERE o.account = 'gmail' AND o.review = '' AND o.hidden = 0 AND l.cancelled = 0 AND o.kind != 'supplies'
     AND o.state NOT IN ('cancelled', 'returned') AND COALESCE(o.ordered_at, '') != ''
     AND (o.state = 'delivered' OR o.ordered_at <= date('now', '-{STORE_DAYS} days'))),
lots AS (
  SELECT id, asin, ordered_at, since, gross, MAX(0, gross - got) AS avail FROM lots0
   WHERE gross > 0 AND asin IS NOT NULL AND asin != ''),
run AS (
  SELECT id, asin, avail,
         SUM(avail) OVER (PARTITION BY asin ORDER BY since, ordered_at, id ROWS UNBOUNDED PRECEDING) - avail AS before
    FROM lots WHERE avail > 0),
sales AS (
  SELECT UPPER(sl.asin) AS asin, substr(so.ordered_at, 1, 10) AS day,
         SUM(sl.qty) OVER (PARTITION BY UPPER(sl.asin) ORDER BY substr(so.ordered_at, 1, 10), sl.id ROWS UNBOUNDED PRECEDING) AS qk,
         SUM(sl.qty) OVER (PARTITION BY UPPER(sl.asin)) AS qn
    FROM sales_line sl JOIN sales_order so ON so.id = sl.order_id
   WHERE so.channel = 'amazon' AND so.status = 'shipped' AND sl.shipped_action_id IS NULL AND sl.asin != ''
     AND NOT EXISTS (SELECT 1 FROM audit_outcome ao WHERE ao.sale_line_id = sl.id)
     AND UPPER(sl.asin) IN (SELECT asin FROM run)),
took AS (
  SELECT asin, MIN(MAX(qn), MIN(COALESCE((SELECT SUM(x.gross) FROM lots x WHERE x.asin = s.asin AND x.since <= s.day), 0) + qn - qk),
                   (SELECT SUM(r.avail) FROM run r WHERE r.asin = s.asin)) AS n
    FROM sales s GROUP BY asin)
SELECT run.id AS line_id, MIN(run.avail, took.n - run.before) AS units
  FROM run JOIN took ON took.asin = run.asin
 WHERE took.n > run.before"""
LINE_RECEIVED_ELSEWHERE_SQL = f"""COALESCE((SELECT SUM(r.qty) FROM receipt r JOIN action a ON a.id = r.action_id
                       WHERE r.purchase_line_id = l.id AND a.reversed_by IS NULL AND r.action_id NOT IN ({AUDIT_ACTIONS_SQL})), 0)"""

CHANNELS = {'ebay': 'eBay', 'walmart': 'Walmart', 'amazon': 'Amazon', 'facebook': 'Facebook Marketplace',
            'mercari': 'Mercari', 'whatnot': 'Whatnot', 'tcgplayer': 'TCGplayer', 'local': 'In person', 'other': 'Other'}

REASONS = {'not_found': 'Not found', 'sold': 'Sold already (no details)', 'damaged': 'Damaged or thrown out', 'other': 'Other'}

# Found units: receipts whose action wasn't undone.
FOUND_SQL = """COALESCE((SELECT SUM(ac.qty) FROM audit_count ac JOIN action a ON a.id = ac.action_id
                         WHERE ac.item_id = i.id AND ac.kind = 'found' AND a.reversed_by IS NULL), 0)"""
SOLD_SQL = """COALESCE((SELECT SUM(ao.qty) FROM audit_outcome ao JOIN action a ON a.id = ao.action_id
                        WHERE ao.item_id = i.id AND ao.kind = 'sold' AND a.reversed_by IS NULL), 0)"""
SENT_BACK_SQL = """COALESCE((SELECT SUM(ao.qty) FROM audit_outcome ao
                             WHERE ao.item_id = i.id AND ao.kind = 'returned'
                               AND NOT EXISTS (SELECT 1 FROM audit_outcome u WHERE u.kind = 'undo' AND u.undoes = ao.id)), 0)"""
# Amazon sales tied to this row by an audit save (Record as sold, or Boxloft's auto-record before 11-11 night). They are
# sold on Amazon like the ones line_presold takes off, so the card counts them there and not as accounted.
AMAZON_SAVED_SQL = """COALESCE((SELECT SUM(ao.{col}) FROM audit_outcome ao JOIN action a ON a.id = ao.action_id
                                WHERE ao.item_id = i.id AND ao.kind = 'sold' AND a.reversed_by IS NULL
                                  AND ao.channel = 'amazon' AND ao.sale_line_id IS NOT NULL), 0)"""
# Closed: the latest close/reopen row is a close.
CLOSE_SQL = """(SELECT ac.kind || '|' || ac.reason || '|' || ac.note FROM audit_count ac
                 WHERE ac.item_id = i.id AND ac.kind IN ('close', 'reopen') ORDER BY ac.id DESC LIMIT 1)"""


def _status(found: int, expected: int, closed: bool) -> str:
    if found >= expected:
        return 'found'
    if closed:
        return 'missing' if found == 0 else 'short'
    return 'partial' if found else 'todo'


def _et_time(ts) -> str:
    """'2026-11-11T05:41:00Z' -> '12:41 AM ET' (today) or 'Nov 10, 11:58 PM ET'."""
    try:
        t = datetime.fromisoformat(str(ts).replace('Z', '+00:00')).astimezone(config.TZ)
    except (TypeError, ValueError):
        return 'just now'
    clock = t.strftime('%I:%M %p').lstrip('0') + ' ' + config.TZ_LABEL
    return clock if t.date() == datetime.now(config.TZ).date() else f"{t.strftime('%b')} {t.day}, {clock}"


def _op_locked(fn):
    """Background writers (the sync loop, a page load) take the same lock as write requests (server.locked), so a
    sale recorded by Boxloft and one recorded by a person can't both pass the "not tied yet" check."""
    @functools.wraps(fn)
    def run(self, *args, **kwargs):
        with self.wms.op_lock:
            return fn(self, *args, **kwargs)
    return run


class Audit:
    def __init__(self, wms: Wms, commerce, catalog=None, returns=None):
        self._rows_lock = threading.Lock()   # one rebuild at a time; the others wait and read its result
        self.wms = wms
        self.commerce = commerce
        self.catalog = catalog
        self.returns = returns
        self._link_lock = threading.Lock()   # one autolink pass at a time, so two page loads never make two products
        self.backlog_ship = None   # EbayLink.backlog_ship: an item listed on eBay before Boxloft, never in a box here
        conn = wms._conn()
        conn.executescript(SCHEMA)
        conn.executescript(LISTING_SCHEMA)   # line_presold reads listing marks (store rows); Listings may start later
        Wms._add_column(conn, 'audit_count', 'batch', 'TEXT')  # one save = one batch, even when split across orders
        Wms._add_column(conn, 'purchase_order', 'kind', "TEXT NOT NULL DEFAULT 'stock'")   # 'supplies': ship boxes etc., not stock
        Wms._add_column(conn, 'audit_item', 'line_id', 'INTEGER')  # store-order rows: the one purchase line they stand for
        commerce.ship_unscanned = self.ship_unscanned   # auto_ship ships a never-scanned item through the audit row
        Wms._add_column(conn, 'sales_order', 'fulfilled_by', "TEXT NOT NULL DEFAULT 'me'")  # also made by Sales
        Wms._add_column(conn, 'audit_item', 'store', "TEXT NOT NULL DEFAULT ''")
        # Speed sweep 11-12: line_presold and the audit rows look these up once per row.
        conn.execute('CREATE INDEX IF NOT EXISTS idx_audit_item_line ON audit_item(line_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_audit_item_asin ON audit_item(asin)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_audit_outcome_sale ON audit_outcome(sale_line_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_audit_count_action ON audit_count(action_id)')
        have = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'view' AND name = 'line_presold'").fetchone()
        if not have or have[0] != PRESOLD_VIEW:   # replaces Commerce's empty one; only when the text changed
            with wms._tx() as c:
                c.execute('DROP VIEW IF EXISTS line_presold')
                c.execute(PRESOLD_VIEW)

    # ------------------------------------------------------------------ snapshot

    def import_snapshot(self, items: list, snapshot: str) -> dict:
        """Upsert the expected list by ASIN. Counts are never touched, so a re-import is safe."""
        new = updated = 0
        ts = now_utc()
        with self.wms._tx() as c:
            for it in items:
                asin = str(it.get('asin') or '').strip().upper()
                if not asin or int(it.get('expected') or 0) <= 0:
                    continue
                vals = (str(it.get('title') or '')[:300], str(it.get('brand') or '')[:80], str(it.get('category') or '')[:80],
                        it.get('image_url') or None, int(it['expected']), int(it.get('exp_listed') or 0),
                        int(it.get('exp_unlisted') or 0), int(it.get('exp_problem') or 0), int(it.get('exp_transit') or 0),
                        int(it.get('cost_cents') or 0), int(it.get('avg_cost_cents') or 0), it.get('first_buy'),
                        it.get('last_buy'), json.dumps(it.get('orders') or [])[:20000], snapshot, ts)
                if c.execute('SELECT 1 FROM audit_item WHERE asin = ?', (asin,)).fetchone():
                    c.execute("""UPDATE audit_item SET title=?, brand=?, category=?, image_url=?, expected=?, exp_listed=?,
                                 exp_unlisted=?, exp_problem=?, exp_transit=?, cost_cents=?, avg_cost_cents=?, first_buy=?,
                                 last_buy=?, orders=?, snapshot=?, imported_at=? WHERE asin=?""", vals + (asin,))
                    updated += 1
                else:
                    c.execute("""INSERT INTO audit_item (title, brand, category, image_url, expected, exp_listed, exp_unlisted,
                                 exp_problem, exp_transit, cost_cents, avg_cost_cents, first_buy, last_buy, orders, snapshot,
                                 imported_at, asin) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", vals + (asin,))
                    new += 1
        tot = self.wms._q('SELECT COUNT(*) AS n, SUM(expected) AS units, SUM(cost_cents) AS cents FROM audit_item')[0]
        return {'new': new, 'updated': updated, 'items': tot['n'], 'units': tot['units'], 'cost_cents': tot['cents']}

    # ------------------------------------------------------------------ read

    def _delivered(self) -> dict:
        """ASIN -> units expected from Amazon orders delivered over DELIVERED_DAYS ago and not scanned in."""
        # No snapshot: no Amazon rows (there's nothing to add them to yet). Store rows never need one.
        cutoff, pairs = self._sheet()[:2]
        due = (today_et() - timedelta(days=DELIVERED_DAYS)).isoformat()
        lines = self.wms._q(f"""
            SELECT l.id, l.qty, l.asin, l.title, l.image_url, l.unit_cost_cents, o.external_id AS order_id, o.ordered_at,
                   o.state, o.status_text, o.expected_from, o.expected_to, o.delivered_on,
                   {LINE_EXPECTED_SQL} - {LINE_RECEIVED_ELSEWHERE_SQL} AS units,
                   COALESCE((SELECT p.units FROM line_presold p WHERE p.line_id = l.id), 0) AS presold
              FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id
             WHERE o.source = 'amazon' AND o.hidden = 0 AND l.cancelled = 0 AND l.asin != '' AND o.ordered_at > ?
               AND o.kind != 'supplies' AND COALESCE(o.review, '') = ''   -- ship boxes wait on the Ship boxes page
               AND (o.state = 'delivered' OR (o.state NOT IN ('cancelled', 'returned')
                    AND EXISTS (SELECT 1 FROM purchase_package p WHERE p.line_id = l.id AND p.state = 'delivered')))
             ORDER BY o.ordered_at""", (cutoff,))
        pk = {}
        if lines:
            for p in self.wms._q(f"""SELECT line_id, qty, state, status_text, expected_from, expected_to, delivered_on
                                      FROM purchase_package WHERE line_id IN ({','.join(str(int(x['id'])) for x in lines)})"""):
                pk.setdefault(p['line_id'], []).append(p)
        rows = []
        for ln in lines:
            # Only the units in packages Amazon delivered over DELIVERED_DAYS ago (all of them for a delivered order).
            # Units Amazon sold that nothing here shipped (line_presold) come off the delivered packages first.
            order = {**ln, 'delivered_on': ln['delivered_on'] or (ln['ordered_at'] if ln['state'] == 'delivered' else None)}
            got = [p for p in portions(ln['qty'], ln['units'] - ln['presold'], pk.get(ln['id'], []), order)
                   if p['state'] == 'delivered' and (p['delivered_on'] or '') <= due]
            # A sheet with no last-buy date can list later orders: those units are its own (never counted twice).
            if got and (ln['order_id'], (ln['asin'] or '').upper()) not in pairs:
                rows.append({**ln, 'line_id': ln['id'], 'units': sum(p['qty'] for p in got),
                             'delivered_on': max(p['delivered_on'] for p in got)})
        for st in self._store_lines():
            st['units'] -= st['presold']
            rows.append(st)
        out = {}
        gone = {}
        for r in rows:
            key = r['asin'].upper() if r['asin'] else f"LINE-{r['line_id']}"
            if r['units'] <= 0:   # every unit sold: counts as sold on the row of that ASIN, if it has one
                gone[key] = gone.get(key, 0) + r['presold']
                continue
            g = out.setdefault(key, {'units': 0, 'cost_cents': 0, 'orders': [], 'title': r['title'], 'presold': 0,
                                     'image_url': r['image_url'], 'unit_cost_cents': r['unit_cost_cents'],
                                     'first': r['ordered_at'], 'last': r['ordered_at'],
                                     'line_id': None if r['asin'] else r['line_id'], 'store': r.get('store') or ''})
            g['presold'] += r['presold']
            g['units'] += r['units']
            g['cost_cents'] += r['units'] * r['unit_cost_cents']
            g['last'] = r['ordered_at']
            g['orders'].append({'id': r['order_id'], 'date': r['ordered_at'], 'units': r['units'],
                                'delivered_on': r['delivered_on'], 'delivered': bool(r['delivered_on']),
                                **({'store': r['store']} if r.get('store') else {})})
        for key, n in gone.items():
            if key in out:
                out[key]['presold'] += n
        return out

    def _sheet(self):
        """The snapshot sheet's reach: (cutoff, pairs, judged). cutoff = its last buy ('9999' with no sheet: there is
        nothing to add Amazon rows to yet); pairs = the (order id, ASIN) it lists; judged = the accounts of the tracker
        exports it was built from. A tracker line that is not on the sheet was judged sold out by it. A sheet row with
        no order list (none on live data; older imports and tests) doesn't say which buys it stands for, so every
        buy of its ASIN counts as judged: pairs then holds (None, ASIN)."""
        snap = self.wms._q("SELECT asin, orders, last_buy FROM audit_item WHERE snapshot != ?", (DELIVERED_SNAPSHOT,))
        if not snap:
            return '9999', set(), set()
        pairs = {(o.get('id'), r['asin'].upper()) for r in snap for o in json.loads(r['orders'] or '[]')}
        orders = {o for o, _ in pairs}
        pairs |= {(None, r['asin'].upper()) for r in snap if not json.loads(r['orders'] or '[]')}
        judged = {x['account'] for x in self.wms._q("SELECT external_id, account FROM purchase_order WHERE source = 'amazon'")
                  if x['external_id'] in orders} - {'', 'Amazon.com'}
        return max((r['last_buy'] or '') for r in snap), pairs, judged

    def _base(self) -> dict:
        """Alex 11-11 9:30 PM ("Ensure no other quick math or mistakes are buried in here"): the starting count of every
        ASIN bought on Amazon up to the sheet's last buy comes from Boxloft's own records, not the 10-02 sheet.

        Per ASIN: every Amazon stock line ordered up to the cutoff (not cancelled, not hidden), each at Amazon's real
        quantity less units returned to the seller (LINE_EXPECTED_SQL), then every Amazon sale nothing here shipped
        takes those units oldest buy first (line_presold: FIFO, no sales date since Alex 11-11 night). What is left,
        order by order, is the row's expected units; receipts made on Receive against any of those lines are `received`.

        This replaces the sheet's count and its patches (cancelled orders, audit_adjust cuts, lines the sheet never saw),
        and fixes what the sheet got wrong: tracker quantities of 1 on orders Amazon charged for 5 or 3, seller returns
        never taken off, and units received against an order the sheet had marked sold.

        Returns ASIN -> {units, cost_cents, orders [{id, date, units}], received, received_cents, first, last, title,
        image_url, unit_cost_cents, presold (units Amazon sold), returned (units sent back before reaching us)}."""
        cutoff = self._sheet()[0]
        if cutoff == '9999':
            return {}
        lines = self.wms._q(f"""
            SELECT l.id, UPPER(l.asin) AS asin, l.qty, l.title, l.image_url, l.unit_cost_cents, o.external_id AS order_id,
                   o.ordered_at, {LINE_EXPECTED_SQL} AS units, {LINE_RECEIVED_ELSEWHERE_SQL} AS received,
                   COALESCE((SELECT SUM(r.qty * r.unit_cost_cents) FROM receipt r JOIN action a ON a.id = r.action_id
                              WHERE r.purchase_line_id = l.id AND a.reversed_by IS NULL
                                AND r.action_id NOT IN ({AUDIT_ACTIONS_SQL})), 0) AS received_cents,
                   COALESCE(p.units, 0) AS presold
              FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id LEFT JOIN line_presold p ON p.line_id = l.id
             WHERE o.source = 'amazon' AND o.hidden = 0 AND l.cancelled = 0 AND l.asin != '' AND o.state != 'cancelled'
               AND o.kind != 'supplies' AND COALESCE(o.review, '') = '' AND COALESCE(o.ordered_at, '') != ''
               AND o.ordered_at <= ?
             ORDER BY o.ordered_at, l.id""", (cutoff,))
        out = {}
        for ln in lines:
            g = out.setdefault(ln['asin'], {'units': 0, 'cost_cents': 0, 'orders': [], 'received': 0, 'received_cents': 0,
                                            'first': ln['ordered_at'], 'last': ln['ordered_at'], 'title': ln['title'],
                                            'image_url': ln['image_url'], 'unit_cost_cents': ln['unit_cost_cents'],
                                            'presold': 0, 'returned': 0, '_by_order': {}})
            g['received'] += ln['received']
            g['received_cents'] += ln['received_cents']
            g['image_url'] = g['image_url'] or ln['image_url']
            g['presold'] += min(ln['presold'], max(0, ln['units']))
            g['returned'] += ln['qty'] - ln['units']
            g['last'] = ln['ordered_at']
            left = max(0, ln['units'] - ln['presold'])   # the units of this line the sales didn't take (line_presold)
            if left:
                g['units'] += left
                g['cost_cents'] += left * (ln['unit_cost_cents'] or 0)
                o = g['_by_order'].setdefault(ln['order_id'], {'id': ln['order_id'], 'date': ln['ordered_at'], 'units': 0})
                o['units'] += left
        for asin, g in out.items():
            g['orders'] = list(g.pop('_by_order').values())
            if g['orders']:
                g['first'], g['last'] = g['orders'][0]['date'], g['orders'][-1]['date']
                g['unit_cost_cents'] = round(g['cost_cents'] / g['units'])
        return out

    def _store_lines(self) -> list:
        """Approved store-email order lines that should have arrived by now and weren't scanned in.
        Units received by the audit itself still count here (they show as found), like the Amazon rows."""
        return self.wms._q(f"""
            SELECT l.id AS line_id, UPPER(l.asin) AS asin, l.title, l.image_url, l.unit_cost_cents, o.external_id AS order_id,
                   o.ordered_at, o.source AS store, CASE WHEN o.state = 'delivered' THEN COALESCE(o.delivered_on, o.ordered_at) END AS delivered_on,
                   {LINE_EXPECTED_SQL} - {LINE_RECEIVED_ELSEWHERE_SQL} AS units,
                   COALESCE((SELECT p.units FROM line_presold p WHERE p.line_id = l.id), 0) AS presold
              FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id
             WHERE o.account = 'gmail' AND o.review = '' AND o.hidden = 0 AND l.cancelled = 0 AND o.kind != 'supplies'
               AND o.state NOT IN ('cancelled', 'returned')
               AND ((o.state = 'delivered' AND COALESCE(o.delivered_on, o.ordered_at) <= date('now', '-{DELIVERED_DAYS} days'))
                    OR (o.state != 'delivered' AND o.ordered_at <= date('now', '-{STORE_DAYS} days')))
             ORDER BY o.ordered_at""")

    def unaccounted(self, inbound, arriving: dict = None) -> dict:
        """Purchase lines with units still expected that sit in no list: not in the store review queue, not on
        Arriving, not on this audit. Should be 0. Scope: store-email orders, and Amazon orders after the snapshot's
        last buy. Older Amazon lines are judged by _base (their units left after Amazon's sales are on the list, one row
        per ASIN, made by _sync_delivered), so none of them can fall through."""
        cutoff = self._sheet()[0]
        cutoff = '' if cutoff == '9999' else cutoff
        lines = self.wms._q(f"""
            SELECT l.id, UPPER(l.asin) AS asin, l.title, o.source, o.external_id AS order_id, o.ordered_at, o.state,
                   o.account, o.review, {LINE_LEFT_SQL} AS units, l.unit_cost_cents
              FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id
             WHERE l.cancelled = 0 AND o.hidden = 0 AND o.state != 'cancelled' AND {LINE_LEFT_SQL} > 0
               AND o.kind != 'supplies'   -- supplies wait on the Ship boxes page until received
               AND (o.account = 'gmail' OR o.source = 'amazon')""")
        lines = [ln for ln in lines if ln['account'] == 'gmail' or (ln['ordered_at'] or '') > cutoff]
        # `arriving`: inbound.arriving() the caller already read (the Overview needs it too).
        on_arriving = {ln['id'] for o in (arriving or inbound.arriving())['orders'] for ln in o['lines']}
        listed = {k: {o['id'] for o in g['orders']} for k, g in self._delivered().items()}
        holes = []
        for ln in lines:
            if ln['account'] == 'gmail' and ln['review']:
                continue
            if ln['id'] in on_arriving:
                continue
            key = ln['asin'] if ln['asin'] else f"LINE-{ln['id']}"
            if ln['order_id'] in listed.get(key, ()):
                continue
            holes.append(ln)
        return {'lines': len(holes), 'units': sum(h['units'] for h in holes),
                'cost_cents': sum(h['units'] * (h['unit_cost_cents'] or 0) for h in holes),
                'items': [{k: h[k] for k in ('id', 'source', 'order_id', 'ordered_at', 'state', 'title', 'units')} for h in holes[:20]]}

    def _sync_delivered(self, delivered: dict, base: dict = None):
        """Give each delivered-not-scanned ASIN, and each ASIN whose own count (_base) has units left, an audit row
        (counts need one)."""
        have = {r['asin'].upper() for r in self.wms._q('SELECT asin FROM audit_item')}
        new = [(a, g) for a, g in delivered.items() if a not in have and g['units'] > 0]
        new += [(a, g) for a, g in (base or {}).items() if g['units'] > 0 and a not in have and a not in delivered]
        if not new:
            return
        with self.wms._tx() as c:
            for asin, g in new:
                c.execute("""INSERT OR IGNORE INTO audit_item (asin, title, image_url, expected, avg_cost_cents, first_buy, last_buy,
                             orders, snapshot, imported_at, line_id, store) VALUES (?,?,?,0,?,?,?,'[]',?,?,?,?)""",
                          (asin, (g['title'] or '')[:300], g['image_url'], g['unit_cost_cents'], g['first'], g['last'],
                           DELIVERED_SNAPSHOT, now_utc(), g.get('line_id'), g.get('store') or ''))

    def _cancelled(self) -> set:
        """(order id, ASIN) pairs Boxloft knows were cancelled: the Amazon sync saw the order cancelled, or a
        person closed the line. The snapshot came from a spreadsheet that never knew."""
        return {(r['external_id'], r['asin'].upper()) for r in self.wms._q("""
            SELECT o.external_id, l.asin FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id
             WHERE (o.state = 'cancelled' OR l.cancelled = 1) AND l.asin != ''""")}

    def cap_cuts(self, apply: bool = False, user_id: int = 0,
                 reason: str = 'Amazon cut the order quantity after the CSV was built') -> list:
        """Snapshot orders where Amazon now shows fewer units (not cancelled outright, which _cancelled covers):
        caps each at the live count with one audit_adjust row per order. Never raises a count. A dry run unless
        `apply`. Safe to run again: an order already capped at its live count gets no second row.
        Since 11-11 a row whose ASIN has Amazon lines takes its count from those lines (_base, Amazon's live
        quantities), so cuts only matter for a sheet row with no lines; on live data there are none."""
        base = self._base()
        cancelled = self._cancelled()
        live = {(r['external_id'], r['asin']): r['qty'] for r in self.wms._q("""
            SELECT o.external_id, UPPER(l.asin) AS asin, SUM(l.qty) AS qty
              FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id
             WHERE o.source = 'amazon' AND o.state != 'cancelled' AND l.cancelled = 0 AND l.asin != ''
             GROUP BY 1, 2""")}
        done = {(a['item_id'], a['order_ref']): -a['units'] for a in self.wms._q(
            'SELECT item_id, order_ref, SUM(units) AS units FROM audit_adjust GROUP BY 1, 2')}
        before = {r['id']: r['expected'] for r in self._rows()}
        out = []
        for it in self.wms._q('SELECT id, asin, title, orders FROM audit_item WHERE snapshot != ? AND line_id IS NULL',
                              (DELIVERED_SNAPSHOT,)):
            asin = it['asin'].upper()
            if asin in base:
                continue
            for o in json.loads(it['orders'] or '[]'):
                key, snap = (o.get('id'), asin), int(o.get('units') or 0)
                if key in cancelled or key not in live:
                    continue
                cut = snap - live[key] - done.get((it['id'], o.get('id')), 0)
                if cut > 0:
                    out.append({'item_id': it['id'], 'asin': asin, 'title': it['title'], 'order': o.get('id'),
                                'snapshot_units': snap, 'live_units': live[key], 'units': -cut,
                                'expected_before': before.get(it['id'])})
        if apply and out:
            ts = now_utc()
            with self.wms._tx() as c:
                for x in out:
                    c.execute("""INSERT INTO audit_adjust (item_id, at, user_id, order_ref, units, snap_units, live_units, reason)
                                 VALUES (?,?,?,?,?,?,?,?)""",
                              (x['item_id'], ts, user_id, x['order'], x['units'], x['snapshot_units'], x['live_units'], reason))
            after = {r['id']: r['expected'] for r in self._rows()}
            for x in out:
                x['expected_after'] = after.get(x['item_id'], 0)
        return out

    def _received_elsewhere(self) -> dict:
        """(ASIN, order id) -> [units, cost] checked in on Receive (not by the audit) against that order's lines.
        A snapshot row's units that came in this way are on our shelves: they count as found on the row,
        so the same units are never both in stock and still expected."""
        out = {}
        for x in self.wms._q("""
                SELECT UPPER(l.asin) AS asin, o.external_id AS order_id, SUM(r.qty) AS units,
                       SUM(r.qty * r.unit_cost_cents) AS cents
                  FROM receipt r JOIN action a ON a.id = r.action_id AND a.reversed_by IS NULL
                  JOIN purchase_line l ON l.id = r.purchase_line_id JOIN purchase_order o ON o.id = l.po_id
                 WHERE l.asin != '' AND r.action_id NOT IN (
                       SELECT action_id FROM audit_count WHERE action_id IS NOT NULL
                       UNION SELECT action_id FROM audit_outcome WHERE action_id IS NOT NULL)
                 GROUP BY 1, 2"""):
            out[(x['asin'], x['order_id'])] = [x['units'], x['cents'] or 0]
        return out

    def _mark_asin_sql(self) -> str:
        return MARK_ASIN_SQL if self._has_table('listing_mark') and self._has_table('listing') else 'NULL'

    def _rows(self, where: str = '1', params=()) -> list:
        """The audit rows. Each one reads bought - returned - sold on Amazon = expected (should be here), and
        accounted = found + received on Receive + sold elsewhere + sent back."""
        mark = self._mark_asin_sql()
        received = self._received_elsewhere()
        delivered = self._delivered()
        base = self._base()
        self._sync_delivered(delivered, base)
        cancelled = self._cancelled()
        amazon = {o['external_id']: o for o in self.wms._q("""
            SELECT external_id, state, status_text, delivered_on, last_seen_at FROM purchase_order WHERE source = 'amazon'""")}
        cuts = {}
        for a in self.wms._q('SELECT item_id, order_ref, SUM(units) AS units FROM audit_adjust GROUP BY 1, 2'):
            cuts.setdefault(a['item_id'], {})[a['order_ref']] = -a['units']
        rows = self.wms._q(f"""
            SELECT i.*, {FOUND_SQL} AS found, {SOLD_SQL} AS sold, {SENT_BACK_SQL} AS sent_back, {CLOSE_SQL} AS last_close,
                   (SELECT COALESCE(SUM(pr.qty - pr.shelf_qty), 0) FROM purchase_return pr JOIN purchase_line l ON l.id = pr.line_id
                     WHERE (l.asin = i.asin OR l.id = i.line_id) AND pr.state != 'kept') AS seller_returns,
                   COALESCE((SELECT p.id FROM product p JOIN product_identifier pi ON pi.product_id = p.id
                              WHERE pi.kind = 'ASIN' AND pi.value = i.asin AND p.archived = 0 LIMIT 1),
                            (SELECT l.product_id FROM purchase_line l WHERE l.id = i.line_id),
                            (SELECT p.id FROM product p JOIN product_identifier pi ON pi.product_id = p.id
                              WHERE pi.kind = 'ASIN' AND pi.value = {mark} AND p.archived = 0 LIMIT 1)) AS product_id,
                   {mark} AS mark_asin, {AMAZON_SAVED_SQL.format(col='qty')} AS amazon_saved,
                   {AMAZON_SAVED_SQL.format(col='cost_cents')} AS amazon_saved_cents
              FROM audit_item i WHERE {where}""", params)
        for r in rows:
            mark_asin = r.pop('mark_asin')
            r['match_asin'] = ((mark_asin or '') if r.get('line_id') else r['asin']).upper()
            # The 10-02 sheet's own number stays as history beside Boxloft's count.
            r['sheet_expected'] = r['expected'] if r['snapshot'] != DELIVERED_SNAPSHOT and not r.get('line_id') else None
            b = None if r.get('line_id') else base.get(r['asin'].upper())
            if b is not None:
                self._own_count(r, b, amazon)
            else:
                self._sheet_count(r, received, cancelled, cuts, amazon)
            orders = r.pop('_orders')
            d = delivered.get(r['asin'].upper())
            r['exp_delivered'] = d['units'] if d else 0
            if d:
                r['image_url'] = r['image_url'] or d['image_url']
                r['expected'] += d['units']
                r['cost_cents'] += d['cost_cents']
                if not r['avg_cost_cents'] or r['snapshot'] == DELIVERED_SNAPSHOT:
                    # Rows made from orders follow the order lines' cost now (a price fixed later shows here).
                    r['avg_cost_cents'] = round(d['cost_cents'] / d['units']) if d['units'] else d['unit_cost_cents']
                r['last_buy'] = max(r['last_buy'] or '', d['last'])
                r['first_buy'] = r['first_buy'] or d['first']
                if r['count'] == 'own' and r['expected']:
                    r['avg_cost_cents'] = round(r['cost_cents'] / r['expected'])
            # Bought - returned - sold on Amazon = expected. Amazon sales an audit save is tied to count as sold on
            # Amazon here, not as accounted: the same kind of sale, whoever matched it (Alex 11-11 night).
            saved, saved_cents = r.pop('amazon_saved'), r.pop('amazon_saved_cents')
            r['sold'] -= saved
            r['expected'] = max(0, r['expected'] - saved)
            r['cost_cents'] = max(0, r['cost_cents'] - saved_cents)
            r['amazon_sold'] = (b['presold'] if b else 0) + (d['presold'] if d else 0) + saved
            r['returned_before'] = b['returned'] if b else 0
            r['bought'] = r['expected'] + r['returned_before'] + r['amazon_sold']
            closed = bool(r['last_close'] and r['last_close'].startswith('close|'))
            parts = (r.pop('last_close') or '||').split('|', 2) + ['', '']
            r['closed'] = closed
            r['close_reason'] = REASONS.get(parts[1], parts[1]) if closed else ''
            r['close_note'] = parts[2] if closed else ''
            r['accounted'] = r['found'] + r['received'] + r['sold'] + r['sent_back']
            # On Returns, not yet in these numbers: only a sheet row with no lines (counted ones take returns off already).
            r['seller_returns'] = max(0, r['seller_returns'] - r['sent_back']) if r['count'] == 'sheet' else 0
            r['status'] = _status(r['accounted'], r['expected'], closed)
            r['_presold'] = r['amazon_sold'] - saved
            r['orders'] = orders + (d['orders'] if d else [])
            # Orders whose Receive-page units count on this row as received (merge.py keeps its extras off them).
            r['own_orders'] = [o.get('id') for o in orders if o.get('id') and not o.get('cancelled')]
        # A row stays once a save was made on it (a sale tied to it, found, closed); one with nothing to look for and
        # no save has nothing to check.
        rows = [r for r in rows if r['expected'] > 0 or r['accounted'] > 0 or r['closed'] or r['amazon_sold'] > r['_presold']]
        for r in rows:
            r.pop('_presold')
        return rows

    def _own_count(self, r: dict, b: dict, amazon: dict):
        """The row's starting count from Boxloft's records (_base): every Amazon sale nothing here shipped is off."""
        r['count'] = 'own'
        r['expected'] = b['units']
        r['cost_cents'] = b['cost_cents']
        r['avg_cost_cents'] = b['unit_cost_cents'] or r['avg_cost_cents']
        r['first_buy'], r['last_buy'] = b['first'], b['last']
        r['image_url'] = r['image_url'] or b['image_url']
        r['exp_cancelled'] = r['exp_cut'] = 0
        r['received'], r['received_cents'] = b['received'], b['received_cents']
        r['_orders'] = [dict(o) for o in b['orders']]
        r['where'] = self._where(r['_orders'], amazon)

    def _sheet_count(self, r: dict, received: dict, cancelled: set, cuts: dict, amazon: dict):
        """A row with no Amazon lines up to the cutoff keeps the 10-02 sheet's number (with its known patches:
        cancelled orders, recorded quantity cuts, Receive-page units against its listed orders). Rows made from
        orders (delivered-not-scanned, store emails) start at 0 here and get their units from _delivered.
        On live data every sheet row has its lines, so this path serves those made-from-orders rows."""
        made = r['snapshot'] == DELIVERED_SNAPSHOT or bool(r.get('line_id'))
        r['count'] = 'orders' if made else 'sheet'
        orders = [] if made else json.loads(r['orders'] or '[]')
        r['exp_cancelled'] = 0
        for o in orders:
            if (o.get('id'), r['asin'].upper()) in cancelled:
                o['cancelled'] = True
                r['exp_cancelled'] += int(o.get('units') or 0)
        r['exp_cut'] = 0
        for o in orders:   # recorded quantity cuts (audit_adjust) on orders that weren't cancelled outright
            cut = (cuts.get(r['id']) or {}).get(o.get('id'), 0)
            if cut > 0 and not o.get('cancelled'):
                cut = min(cut, int(o.get('units') or 0))
                o['units_snapshot'] = int(o.get('units') or 0)
                o['units'] = o['units_snapshot'] - cut
                o['cut'] = cut
                r['exp_cut'] += cut
        r['where'] = self._where(orders, amazon) if not made else []
        if r['exp_cancelled'] or r['exp_cut']:
            n = min(r['exp_cancelled'] + r['exp_cut'], r['expected'])
            r['expected'] -= n
            r['cost_cents'] = max(0, r['cost_cents'] - n * r['avg_cost_cents'])
        r['received'] = r['received_cents'] = 0
        for o in orders:   # rows made from orders already leave Receive-page units out of `expected`
            if not o.get('cancelled'):
                got = received.get((r['asin'].upper(), o.get('id')))
                if got:
                    r['received'] += got[0]
                    r['received_cents'] += got[1]
        r['_orders'] = orders

    @staticmethod
    def _where(orders: list, amazon: dict) -> list:
        """Where a snapshot row's units stand by Amazon's live order status (the extension), one entry per kind:
        delivered (with Amazon's delivery dates), on_way (with the day Amazon was last read), returned, or
        no_record (bought before Boxloft read order status: the order dates). Replaces the CSV's labels
        ("never listed", "never marked received"), which were the old tracker's view on 9-18 (Alex 11-10).
        Cancelled orders are left out (exp_cancelled says those); cut orders count their live units.
        Also tags each order with `amazon` (state) and `amazon_on` (delivery day) for the orders list."""
        out = {}
        for o in orders:
            if o.get('cancelled'):
                continue
            a = amazon.get(o.get('id')) or {}
            st = a.get('state') or ''
            if st == 'delivered':
                kind, day = 'delivered', a.get('delivered_on') or ''
            elif st == 'returned':
                kind, day = 'returned', (a.get('last_seen_at') or '')[:10]
            elif st in ('shipped', 'ordered') and a.get('status_text'):
                kind, day = 'on_way', (a.get('last_seen_at') or '')[:10]
            else:
                kind, day = 'no_record', o.get('date') or ''
            o['amazon'], o['amazon_on'] = (st if kind != 'no_record' else ''), (a.get('delivered_on') or '')
            w = out.setdefault(kind, {'kind': kind, 'units': 0, 'from': day, 'to': day})
            w['units'] += int(o.get('units') or 0)
            if day:
                w['from'] = min(w['from'] or day, day)
                w['to'] = max(w['to'] or day, day)
        order = ('delivered', 'on_way', 'returned', 'no_record')
        return sorted((w for w in out.values() if w['units']), key=lambda w: order.index(w['kind']))

    def list(self) -> dict:
        items = self._all_rows()
        items.sort(key=lambda r: -r['cost_cents'])
        s = {'items': len(items), 'units': 0, 'cost_cents': 0, 'found_units': 0, 'found_cents': 0, 'checked': 0,
             'sold_units': 0, 'sent_back_units': 0, 'accounted_units': 0, 'bought_units': 0, 'amazon_sold_units': 0,
             'returned_before_units': 0,
             'by_status': {k: 0 for k in ('todo', 'partial', 'found', 'short', 'missing')}}
        for r in items:
            s['units'] += r['expected']
            s['cost_cents'] += r['cost_cents']
            f = min(r['found'] + r['received'], r['expected'])
            s['found_units'] += r['found'] + r['received']
            s['found_cents'] += f * r['avg_cost_cents']
            s['sold_units'] += r['sold']
            s['sent_back_units'] += r['sent_back']
            s['accounted_units'] += min(r['accounted'], r['expected'])
            s['by_status'][r['status']] += 1
            s['checked'] += r['status'] in ('found', 'short', 'missing')
            s['bought_units'] += r['bought']
            s['amazon_sold_units'] += r['amazon_sold']
            s['returned_before_units'] += r['returned_before']
        snap = self.wms._q('SELECT snapshot FROM audit_item WHERE snapshot != ? ORDER BY id DESC LIMIT 1', (DELIVERED_SNAPSHOT,))
        s['snapshot'] = snap[0]['snapshot'] if snap else ''
        cutoff = self._sheet()[0]
        s['counted'] = {'buys_through': cutoff,
                        'rows': sum(1 for r in items if r['count'] == 'own'),
                        'differ': sum(1 for r in items if r['count'] == 'own' and r['sheet_expected'] is not None
                                      and r['sheet_expected'] != r['expected'] - r['exp_delivered'])} if cutoff != '9999' else None
        return {'items': items, 'summary': s}

    ROWS_TTL = 60   # seconds: a save from another process (manage.py) shows within a minute

    def _all_rows(self, item_id: int = None) -> list:
        """Every row, built once per save (11-11 night: the Stock audit page asks for ~80 cards at once and each
        rebuilt all 775 rows, 0.25 s each on the droplet, so the Overview behind them never loaded on the phone).
        The cache is dropped by any commit in this process (wms.write_seq) and after ROWS_TTL. Callers get copies."""
        import copy
        import time
        with self._rows_lock:
            c = getattr(self, '_rows_cache', None)
            key = (self.wms.write_seq, today_et())   # rows also depend on the day (delivered 5+ days ago)
            if not c or c[0] != key or time.time() - c[1] > self.ROWS_TTL:
                rows = self._rows()
                c = self._rows_cache = ((self.wms.write_seq, today_et()), time.time(), rows)
            if item_id is not None:   # one card: copy only its row (copying all 775 per card took 1.3 s on the droplet)
                return [copy.deepcopy(x) for x in c[2] if x['id'] == int(item_id)]
            return copy.deepcopy(c[2])

    def open_sales(self, items: list) -> None:
        """Adds `open_sales` to each row: orders for its ASIN that are open and not shipped from here (Alex 11-12: the
        Crocs card said "1 should be here (3 bought, 2 sold on Amazon)" while Orders showed today's sale of that 1).
        An open sale isn't in `amazon_sold` until it ships, because the unit is still ours to find and ship. Read-only."""
        want = {r['match_asin'] for r in items if r.get('match_asin')}
        by = {}
        if want:
            for s in self.wms._q("""
                    SELECT UPPER(COALESCE(NULLIF(l.asin, ''), (SELECT i.value FROM product_identifier i
                             WHERE i.product_id = l.product_id AND i.kind = 'ASIN' LIMIT 1), '')) AS asin,
                           l.id AS line_id, l.qty, o.channel, o.external_id AS order_id, o.ordered_at
                      FROM sales_line l JOIN sales_order o ON o.id = l.order_id
                     WHERE o.status = 'open' AND l.shipped_action_id IS NULL AND o.fulfilled_by != 'amazon'
                     ORDER BY o.ordered_at, l.id"""):
                if s['asin'] in want:
                    by.setdefault(s.pop('asin'), []).append(s)
        for r in items:
            r['open_sales'] = by.get(r.get('match_asin') or '', [])

    def item(self, item_id: int) -> dict:
        rows = self._all_rows(item_id)
        if not rows:
            raise WmsError('Audit item not found')
        r = rows[0]
        r['history'] = self.history(item_id)
        r['seller_return_lines'] = self.wms._q("""
            SELECT o.external_id AS order_id, pr.qty, pr.state, pr.reason FROM purchase_return pr
              JOIN purchase_line l ON l.id = pr.line_id JOIN purchase_order o ON o.id = l.po_id
             WHERE (l.asin = ? OR l.id = ?) AND pr.state != 'kept' ORDER BY o.ordered_at""", (r['asin'], r['line_id'] or -1))
        return r

    def histories(self, ids: list) -> dict:
        """Saves on many rows in one request: the Stock audit page asks for the cards it shows (Alex 11-12 1:05 PM:
        one request per card, 40 at a time and again on every refresh, queued Inventory and Orders behind them)."""
        return {i: self.history(i) for i in ids}

    def history(self, item_id: int) -> list:
        """Each save on the row (found, sold, sent back, closed), newest first, one line per save."""
        rows = self.wms._q("""
            SELECT ac.id, ac.at, ac.kind, ac.qty, ac.box_id, ac.location_id, ac.action_id, ac.reason, ac.note, ac.batch,
                   (SELECT CASE WHEN l.site != '' AND l.name != '' AND l.name != l.site THEN l.site || ' · ' || l.name
                           ELSE COALESCE(NULLIF(l.name, ''), NULLIF(l.site, ''), l.code) END
                      FROM location l WHERE l.id = ac.location_id) AS location_name,
                   a.reversed_by IS NOT NULL AS undone, ac.user_id
              FROM audit_count ac LEFT JOIN action a ON a.id = ac.action_id
             WHERE ac.item_id = ? ORDER BY ac.id DESC""", (item_id,))
        # One line per save: a save split across several orders is one batch.
        hist, seen = [], {}
        for h in rows:
            h['reason'] = REASONS.get(h['reason'], h['reason'])
            key = h['batch'] or f"id{h['id']}"
            if h['kind'] == 'found' and key in seen:
                g = seen[key]
                g['qty'] += h['qty']
                g['undone'] = g['undone'] and h['undone']
                g['id'] = h['id']
                continue
            seen[key] = h
            hist.append(h)
        outs = self.wms._q("""
            SELECT ao.id, ao.at, ao.kind, ao.qty, ao.channel, ao.price_cents, ao.sold_on, ao.order_ref, ao.reason,
                   ao.return_state, ao.note, ao.batch, ao.user_id,
                   CASE WHEN ao.kind = 'sold' THEN (SELECT a.reversed_by IS NOT NULL FROM action a WHERE a.id = ao.action_id)
                        ELSE EXISTS (SELECT 1 FROM audit_outcome u WHERE u.kind = 'undo' AND u.undoes = ao.id) END AS undone
              FROM audit_outcome ao WHERE ao.item_id = ? AND ao.kind != 'undo' ORDER BY ao.id DESC""", (item_id,))
        seen = {}
        for h in outs:
            key = h['batch'] or f"o{h['id']}"
            if key in seen:
                g = seen[key]
                g['qty'] += h['qty']
                g['undone'] = g['undone'] and h['undone']
                g['id'] = h['id']
                continue
            h['channel_label'] = CHANNELS.get(h['channel'], h['channel'])
            h['outcome_id'] = h['id']
            seen[key] = h
            hist.append(h)
        for h in hist:
            if 'outcome_id' in h:
                h['outcome_id'] = h['id']
        hist.sort(key=lambda h: h['at'], reverse=True)
        return hist

    # ------------------------------------------------------------------ write

    def _product_for(self, it: dict, user_id: int) -> int:
        """The Boxloft product for this ASIN, made from the audit row if there isn't one yet."""
        if it['product_id']:
            return it['product_id']
        if it.get('line_id'):
            return self._product_for_line(it, user_id)
        return self._make_product(it['asin'], it['title'], it['brand'], it['image_url'], it['avg_cost_cents'], user_id)

    def _make_product(self, asin: str, title: str, brand: str, image_url: str, cost_cents, user_id: int,
                      lookup: bool = False, base: str = '', extra_idents: list = None, save_photo: bool = True) -> int:
        """One new Boxloft product for an ASIN (SKU = the ASIN). The one place the audit, Orders and Receive make
        products. lookup=True also asks the catalog (Keepa, cached) for the size, brand, photo and barcodes, so the
        ship-box suggestion and a later barcode scan work. `base` names the SKU when there is no ASIN."""
        asin = (asin or '').strip().upper()
        idents = [{'kind': 'ASIN', 'value': asin}] if asin else []
        extra = {}
        if lookup and asin and self.catalog and self.catalog.keepa_key:
            try:
                hit = next((p for p in self.catalog.lookup(asin) if (p.get('asin') or '').upper() == asin), None)
            except WmsError:
                hit = None   # no tokens or no network: the product is still made, the size can be typed later
            if hit:
                title, brand, image_url = title or hit['title'], brand or hit['brand'], image_url or hit['image_url']
                idents += [{'kind': 'UPC', 'value': u} for u in (hit.get('upcs') or [])[:5]]
                idents += [{'kind': 'EAN', 'value': u} for u in (hit.get('eans') or [])[:5]]
                extra = {k: hit[k] for k in ('pkg_l_mm', 'pkg_w_mm', 'pkg_h_mm', 'pkg_weight_g') if hit.get(k)}
        for x in extra_idents or []:   # the code that was scanned, so the next scan finds this product
            if (x['kind'], x['value']) not in {(i['kind'], i['value']) for i in idents}:
                idents.append(x)
        base = asin or base
        sku, n = base, 1
        while self.wms._q('SELECT 1 FROM product WHERE sku = ?', (sku,)):
            n += 1
            sku = f'{base}-{n}'
        p = self.wms.save_product({'sku': sku, 'title': title or sku, 'brand': brand or '', 'image_url': image_url or '',
                                   'default_cost_cents': cost_cents, 'identifiers': idents, 'owner_id': 1, **extra})
        with self.wms._tx() as c:   # the open order and the listing for this item now point at it
            self.commerce.relink(c)
        if self.catalog and image_url and save_photo:
            # Keep a copy of the Amazon photo with the product (pick list, Inventory). Off the request path.
            def save(pid=p['id']):
                try:
                    self.catalog.save_image(pid, image_url, user_id)
                finally:
                    self.wms.release()   # dbpool: a finished thread's connection is closed or reused, not left open
            threading.Thread(target=save, daemon=True).start()
        return p['id']

    def product_for_purchase_line(self, line_id: int, user_id: int) -> dict:
        """Receive something that has no product yet: the product is made from the purchase line (name, ASIN or
        UPC, cost, photo), or found if one with that code exists. Nobody fills in a form."""
        ln = self.commerce.get_line(line_id)
        pid, made = ln['product_id'], False
        if pid and not self.wms._q1('SELECT 1 AS x FROM product WHERE id = ? AND archived = 0', (pid,)):
            pid = None
        asin = (ln['asin'] or '').strip().upper()
        if not pid:
            code = asin or (ln.get('upc') or '').strip()
            have = [p for p in self.wms.lookup(code) if not p['archived']] if code else []
            if have:
                pid = have[0]['id']
            elif asin:
                row = self.wms._q1('SELECT title, brand, image_url FROM audit_item WHERE asin = ?', (asin,)) or {}
                pid = self._make_product(asin, ln['title'] or row.get('title'), row.get('brand'),
                                         ln.get('image_url') or row.get('image_url'), ln['unit_cost_cents'], user_id, lookup=True)
                made = True
            else:
                pid = self._product_for_line({'line_id': line_id}, user_id)
                with self.wms._tx() as c:
                    self.commerce.relink(c)
                made = True
            if not asin:   # ASIN lines find their product by the ASIN; others need the link
                with self.wms._tx() as c:
                    c.execute('UPDATE purchase_line SET product_id = ? WHERE id = ?', (pid, line_id))
        p = self.wms._q1('SELECT id, sku, title FROM product WHERE id = ?', (pid,))
        return {'product_id': pid, 'made': made, 'title': p['title'], 'sku': p['sku']}

    def _has_table(self, name: str) -> bool:
        return bool(self.wms._q1("SELECT 1 AS x FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)))

    def _listing_asin(self, sku: str) -> str:
        """The ASIN on our listing with this seller SKU ('' when there is none)."""
        if not sku or not self._has_table('listing'):
            return ''
        row = self.wms._q1("""SELECT UPPER(asin) AS asin FROM listing WHERE sku = ? AND asin != ''
                               ORDER BY gone_at IS NOT NULL, id DESC LIMIT 1""", (sku,))
        return row['asin'] if row else ''

    def _marked_row(self, sku: str):
        """The audit row a person confirmed as the listing with this SKU (Stock audit, Listed panel), if any."""
        if not sku or not self._has_table('listing_mark'):
            return None
        row = self.wms._q1("""SELECT m.item_id FROM listing_mark m JOIN listing li ON li.id = m.listing_id
                               WHERE m.kind = 'confirm' AND li.sku = ?
                                 AND NOT EXISTS (SELECT 1 FROM listing_mark u WHERE u.kind = 'undo' AND u.undoes = m.id)
                               ORDER BY m.id DESC LIMIT 1""", (sku,))
        return row['item_id'] if row else None

    def product_for_sale_line(self, line_id: int, user_id: int) -> dict:
        """An order line with no product: link it to the product with its SKU or ASIN, made now if there is none
        (from the audit row for that ASIN when there is one). A line with no ASIN takes the one on our listing with
        its SKU. link_line remembers the seller SKU."""
        ln = self.wms._q1('SELECT id, sku, asin, title, product_id, shipped_action_id FROM sales_line WHERE id = ?', (line_id,))
        if not ln:
            raise WmsError('Order line not found')
        if ln['product_id']:
            return {'product_id': ln['product_id'], 'made': False}
        asin = (ln['asin'] or '').strip().upper() or self._listing_asin(ln['sku'])
        pid, made = self.commerce._match_product(self.wms._conn(), ln['sku'], asin), False
        if not pid:
            row = (self.wms._q1('SELECT title, brand, image_url, avg_cost_cents FROM audit_item WHERE asin = ?', (asin,)) if asin else None) or {}
            cost = row.get('avg_cost_cents') or (self.wms._q1(
                """SELECT CAST(ROUND(SUM(l.qty * l.unit_cost_cents) * 1.0 / SUM(l.qty)) AS INTEGER) AS c FROM purchase_line l
                    WHERE l.asin = ? AND l.cancelled = 0 AND l.qty > 0""", (asin,)) or {}).get('c') if asin else None
            image = row.get('image_url') or ((self.wms._q1(
                "SELECT image_url FROM purchase_line WHERE asin = ? AND image_url IS NOT NULL AND image_url != '' LIMIT 1",
                (asin,)) or {}).get('image_url') if asin else None)
            # The audit row's title first: it is the name Sam saw on the Stock audit, so both screens say the same.
            pid = self._make_product(asin, row.get('title') or ln['title'] or '', row.get('brand'), image, cost,
                                     user_id, lookup=True, base=ln['sku'] or f"SALE-{ln['id']}")
            made = True
        self.commerce.link_line(line_id, pid)
        return {'product_id': pid, 'made': made}

    @_op_locked
    def autolink_sales(self, user_id: int = None) -> int:
        """Open order lines with no product link themselves when the match is exact, so nobody taps "Add it":
        - the same ASIN as a Stock audit row or a purchase line (the line's ASIN, or the ASIN on our listing with the
          line's SKU): linked to that ASIN's product, made from the audit row if there is none yet;
        - a SKU whose listing a person confirmed as an audit row: linked to that row's product.
        Moves no stock. Runs when Orders loads and after each order or listings sync. Returns the lines linked."""
        if not self._link_lock.acquire(blocking=False):
            return 0
        try:
            lines = self.wms._q("""SELECT l.id, l.sku, UPPER(l.asin) AS asin FROM sales_line l JOIN sales_order o ON o.id = l.order_id
                                    WHERE o.status = 'open' AND l.shipped_action_id IS NULL AND l.product_id IS NULL ORDER BY l.id""")
            n = 0
            for ln in lines:
                if self.wms._q1('SELECT product_id FROM sales_line WHERE id = ?', (ln['id'],))['product_id']:
                    n += 1   # linked just now with an earlier line that has the same SKU (link_line)
                    continue
                asin = ln['asin'] or self._listing_asin(ln['sku'])
                if asin and (self.wms._q1('SELECT 1 AS x FROM audit_item WHERE asin = ?', (asin,)) or self.wms._q1(
                        'SELECT 1 AS x FROM purchase_line WHERE asin = ? AND cancelled = 0 LIMIT 1', (asin,))):
                    try:
                        self.product_for_sale_line(ln['id'], user_id)
                    except WmsError:
                        continue   # the line keeps its "Add it" buttons
                    n += 1
                    continue
                item_id = self._marked_row(ln['sku'])
                if item_id:
                    try:
                        pid = self._product_for(self.item(item_id), user_id)
                    except WmsError:
                        continue
                    self.commerce.link_line(ln['id'], pid)
                    n += 1
            return n
        finally:
            self._link_lock.release()

    def order_hints(self, orders: list) -> list:
        """For Orders: each line that can't be picked yet learns where its units may be. `audit` = this item's
        open stock-audit row ({id, asin, left, expected, accounted}); `coming` = units on purchases not received yet.
        Speed sweep 11-12: the audit rows are read once for all lines (each _rows call rebuilds the whole audit, about
        a quarter second on the droplet, and it ran once per line)."""
        todo = []
        for o in orders:
            for ln in o['lines']:
                ln['audit'], ln['coming'] = None, 0
                if ln['shipped_action_id'] or ln.get('where'):
                    continue
                asin = (ln['asin'] or '').strip().upper()
                if not asin and ln['product_id']:
                    row = self.wms._q1("SELECT value FROM product_identifier WHERE product_id = ? AND kind = 'ASIN'", (ln['product_id'],))
                    asin = row['value'].upper() if row else ''
                todo.append((ln, asin))
        by_asin = self._rows_by('asin', sorted({a for _, a in todo if a}))
        # a store-order row (no ASIN): the row whose purchase line made this product
        store_of = {}
        for ln, asin in todo:
            if not by_asin.get(asin) and ln['product_id'] and ln['product_id'] not in store_of:
                st = self.wms._q1("""SELECT i.id FROM audit_item i JOIN purchase_line l ON l.id = i.line_id
                                      WHERE l.product_id = ? ORDER BY i.id LIMIT 1""", (ln['product_id'],))
                store_of[ln['product_id']] = st['id'] if st else None
        by_id = self._rows_by('id', sorted({i for i in store_of.values() if i}))
        coming = {}
        for ln, asin in todo:
            rows = by_asin.get(asin, []) if asin else []
            if not rows and store_of.get(ln['product_id']):
                rows = by_id.get(store_of[ln['product_id']], [])
            if rows and not rows[0]['closed'] and rows[0]['expected'] > rows[0]['accounted']:
                r = rows[0]
                # id: the order card marks it found with the Stock audit's own call (POST /api/audit/<id>/found)
                ln['audit'] = {'id': r['id'], 'asin': r['asin'], 'left': r['expected'] - r['accounted'],
                               'expected': r['expected'], 'accounted': r['accounted']}
            if ln['product_id']:
                if ln['product_id'] not in coming:
                    coming[ln['product_id']] = sum(x['left_to_receive'] for x in self.commerce.open_lines_for(ln['product_id']))
                ln['coming'] = coming[ln['product_id']]
        return orders

    def _rows_by(self, col: str, values: list) -> dict:
        """Audit rows for many values of i.<col> in one _rows call, grouped by that value, lowest row id first."""
        if not values:
            return {}
        out = {}
        rows = self._rows(f"i.{col} IN ({','.join('?' * len(values))})", tuple(values))
        for r in sorted(rows, key=lambda r: r['id']):
            out.setdefault(r[col], []).append(r)
        return out

    def _product_for_line(self, it: dict, user_id: int = 0) -> int:
        """A store-order line with no product yet: make one from the line (title, store item id) and link the line.
        When a person matched its audit row to a listing, the product is that ASIN's (found, or made with it)."""
        ln = self.wms._q1("""SELECT l.id, l.title, l.image_url, l.unit_cost_cents, l.store_item_id, l.upc, o.source
                               FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id WHERE l.id = ?""", (it['line_id'],))
        if not ln:
            raise WmsError('The order line for this row is gone')
        idents = []
        if ln.get('store_item_id') and ln['source'] == 'walmart.com':
            idents.append({'kind': 'WALMART_ITEM', 'value': ln['store_item_id']})
        if ln.get('upc'):
            idents.append({'kind': 'UPC', 'value': ln['upc']})
        asin = it.get('match_asin')
        if asin is None:
            row = self.wms._q1(f'SELECT {self._mark_asin_sql()} AS a FROM audit_item i WHERE i.line_id = ?', (ln['id'],))
            asin = (row or {}).get('a') or ''
        if asin:
            have = [p for p in self.wms.lookup(asin) if not p['archived']]
            pid = have[0]['id'] if have else self._make_product(
                asin, ln['title'], '', ln['image_url'] or '', ln['unit_cost_cents'] or 0, user_id, lookup=True, extra_idents=idents)
            with self.wms._tx() as c:
                c.execute('UPDATE purchase_line SET product_id = ? WHERE id = ? AND product_id IS NULL', (pid, ln['id']))
            return pid
        base = (ln['source'].split('.')[0][:10].upper() or 'STORE') + '-' + (ln.get('store_item_id') or f"L{ln['id']}")
        sku, n = base, 1
        while self.wms._q('SELECT 1 FROM product WHERE sku = ?', (sku,)):
            n += 1
            sku = f'{base}-{n}'
        p = self.wms.save_product({'sku': sku, 'title': ln['title'] or sku, 'image_url': ln['image_url'] or '',
                                   'default_cost_cents': ln['unit_cost_cents'] or 0, 'identifiers': idents, 'owner_id': 1})
        with self.wms._tx() as c:
            c.execute('UPDATE purchase_line SET product_id = ? WHERE id = ? AND product_id IS NULL', (p['id'], ln['id']))
        return p['id']

    def found(self, item_id: int, qty: int, to: dict, user_id: int, device: str = '', note: str = '',
              batch: str = None) -> dict:
        """batch: the save's id (the sorting screen passes its scan id, so a retried scan is found, not doubled)."""
        qty = int(qty or 0)
        if not 1 <= qty <= 10000:
            raise WmsError('Enter how many you found (1 or more)')
        it = self.item(item_id)
        pid = self._product_for(it, user_id)
        box_id, location_id = int(to.get('box_id') or 0) or None, int(to.get('location_id') or 0) or None
        actions = []
        # Against the real orders first, so cost and buy date are the order's.
        plan, left = self._receive_plan(pid, it, qty)
        for line, take in plan:
            r = self.wms.receive(user_id, pid, take, line['unit_cost_cents'], to, reference=line['order_id'] or '',
                                 note=f'Audit: {note}'.strip(': ') or 'Audit', device=device, purchase_line_id=line['id'])
            actions.append((r['action_id'], take))
        if left > 0:
            r = self.wms.receive(user_id, pid, left, it['avg_cost_cents'], to, reference='audit',
                                 note=(f'Audit, no open order line: {note}' if note else 'Audit, no open order line'),
                                 device=device)
            actions.append((r['action_id'], left))
        batch = batch or secrets.token_hex(6)
        with self.wms._tx() as c:
            for action_id, n in actions:
                c.execute("""INSERT INTO audit_count (item_id, at, user_id, kind, qty, box_id, location_id, action_id, note, batch)
                             VALUES (?,?,?,?,?,?,?,?,?,?)""", (item_id, now_utc(), user_id, 'found', n, box_id, location_id,
                                                                action_id, note or '', batch))
        return self.item(item_id)

    @staticmethod
    def row_lines_first(lines: list, it: dict) -> list:
        """The row's own order lines (its ASIN, or its store line) first, then the rest, each oldest first: found units
        are the row's, so they come in on its orders (review 11-17: oldest of any kind put them on an older Walmart
        line, and the row's own Amazon line was left open for something else to fill)."""
        asin = (it.get('match_asin') or it.get('asin') or '').upper()
        mine = lambda ln: ln['id'] == it.get('line_id') or (asin and (ln.get('asin') or '').upper() == asin)
        return [ln for ln in lines if mine(ln)] + [ln for ln in lines if not mine(ln)]

    def _receive_plan(self, pid: int, it: dict, qty: int):
        """([(open purchase line, units)], units left with no line) to receive `qty` of the row `it` against, oldest
        line first. Left to receive already leaves out units Amazon sold before Boxloft (line_presold), so cost and
        buy date are those of the units really left (bought 3 in June and 4 in July, 2 sold: found 5 = 1 from June and
        4 from July)."""
        plan, left = [], qty
        for ln in self.row_lines_first(self.commerce.open_lines_for(pid), it):
            take = min(left, max(0, ln['left_to_receive']))
            if take > 0:
                plan.append((ln, take))
                left -= take
        return plan, left

    def close(self, item_id: int, reason: str, user_id: int, note: str = '', sales: str = '', device: str = '') -> dict:
        """Mark the units left as missing. Amazon sales are off `expected` already (line_presold), so there is nothing
        to record first; `sales` is accepted from older screens and ignored."""
        if reason not in REASONS:
            raise WmsError('Pick a reason')
        self.item(item_id)
        with self.wms._tx() as c:
            c.execute("INSERT INTO audit_count (item_id, at, user_id, kind, reason, note) VALUES (?,?,?,'close',?,?)",
                      (item_id, now_utc(), user_id, reason, (note or '')[:500]))
        return self.item(item_id)

    def reopen(self, item_id: int, user_id: int) -> dict:
        self.item(item_id)
        with self.wms._tx() as c:
            c.execute("INSERT INTO audit_count (item_id, at, user_id, kind) VALUES (?,?,?,'reopen')", (item_id, now_utc(), user_id))
        return self.item(item_id)

    def undo_found(self, item_id: int, count_id: int, user_id: int, device: str = '') -> dict:
        """Undo one save: every receipt in its batch that isn't undone yet."""
        row = self.wms._q("SELECT batch, action_id FROM audit_count WHERE id = ? AND item_id = ? AND kind = 'found'", (count_id, item_id))
        if not row:
            raise WmsError('Nothing to undo')
        if row[0]['batch']:
            acts = self.wms._q("""SELECT ac.action_id FROM audit_count ac JOIN action a ON a.id = ac.action_id
                                   WHERE ac.item_id = ? AND ac.batch = ? AND a.reversed_by IS NULL""", (item_id, row[0]['batch']))
        else:
            acts = [row[0]] if row[0]['action_id'] else []
        if not acts:
            raise WmsError('Already undone')
        for a in acts:
            self.wms.undo(user_id, a['action_id'], note='Audit: undo', device=device)
        return self.item(item_id)

    # ------------------------------------------------------------------ where the rest went

    def _check_room(self, it: dict, qty: int):
        if not 1 <= qty <= 10000:
            raise WmsError('Enter how many (1 or more)')
        room = it['expected'] - it['accounted']
        if qty > max(room, 0):
            raise WmsError(f'Only {max(room, 0)} of {it["expected"]} are left to account for. '
                           'If you really have more, use Found it for the extra.')

    def sold(self, item_id: int, qty: int, channel: str, user_id: int, price_cents=None, sold_on: str = '',
             order_ref: str = '', note: str = '', device: str = '', sale_line_id: int = None) -> dict:
        """Received and already sold somewhere else: VENDOR -> CUSTOMER at the order's cost, plus a sale record.
        With sale_line_id (a synced marketplace sale nothing shipped), that line is the sale record: its channel,
        date, order and price are used and it is marked shipped by this audit action."""
        self._sold(self.item(item_id), int(qty or 0), channel, user_id, price_cents, sold_on, order_ref, note, device, sale_line_id)
        return self.item(item_id)

    # ------------------------------------------------------------------ ship what was never scanned in (redesign 3)
    # Flow audit 11-12: 3 of 3 open orders had 0 on the shelves, so shipping meant Found it (say a box), save, then the
    # ship dialog: 4 steps and a made-up box move ("Received 1 into A36", "1 out of A36", same minute). The item is
    # leaving, so no box is asked: one action, supplier -> customer at the oldest order line's cost, tied to the sale and
    # to the Stock audit row (its amazon_sold goes up by the units, as an audit "sold" save tied to a sale always did).

    def ship_unscanned(self, sale_line_id: int, user_id: int, device: str = '', ship_box_id: int = None,
                       auto: bool = False, outside: bool = False, cost_cents=None, bought_on: str = '') -> dict:
        """outside (Alex 11-17, College Football 27, bought in a store and sold on Amazon before it was received: "Since
        it's sold it doesn't need to go into a box"): it sold before it came in. The units no buy covers go supplier ->
        customer at the typed unit cost, or "cost not entered" (the item's Usual unit cost fills it later); `bought_on` is
        the order it was bought on, if a person knows it. auto_ship uses it too (Alex 11-17, "bought 50, sold 10, I only
        have 40 to box"): a sale the marketplace shipped while Boxloft held none can't have left a box filled later, so
        it closes now and the box gets what is really there. Waiting instead let the next sync pull the sold units out
        of the box Sam just filled (40 boxed, 30 shown)."""
        ln =self.wms._q1("""SELECT l.*, o.channel, o.external_id, o.status AS order_status, o.fulfilled_by
                               FROM sales_line l JOIN sales_order o ON o.id = l.order_id WHERE l.id = ?""", (int(sale_line_id),))
        if not ln:
            raise WmsError('Order line not found')
        if ln['shipped_action_id']:
            raise WmsError('Already shipped')
        if ln['order_status'] != 'open':
            raise WmsError(f"Order {ln['external_id']} is {ln['order_status']}. Nothing was shipped.")
        if ln['fulfilled_by'] == 'amazon':
            raise WmsError('Amazon ships this one (FBA)')
        pid = ln['product_id']
        if not pid:
            raise WmsError('Link this line to a product first')
        on_shelf = self.commerce._where(pid)
        if on_shelf:
            raise WmsError(f"{sum(w['qty'] for w in on_shelf)} on the shelves: ship it from there")
        qty = int(ln['qty'])
        probe = {'lines': [{**ln, 'where': []}]}
        self.order_hints([probe])
        hint = probe['lines'][0]['audit']
        done = []
        item_id = None
        if hint and hint['left'] >= qty:
            it = self.item(hint['id'])
            before = {o['id'] for o in self.wms._q("SELECT id FROM audit_outcome WHERE sale_line_id = ?", (ln['id'],))}
            self._sold(it, qty, ln['channel'], user_id, sale_line_id=ln['id'], device=device, open_ok=True,
                       note='shipped, never scanned in')
            done = [r['action_id'] for r in self.wms._q("SELECT id, action_id FROM audit_outcome WHERE sale_line_id = ? ORDER BY id",
                                                         (ln['id'],)) if r['id'] not in before]
            item_id = it['id']
        else:
            lines = self.commerce.rank_for_receiving(self.commerce.open_lines_for(pid))
            plan, left = [], qty
            for pl in lines:
                take = min(left, max(0, pl['left_to_receive']))
                if take:
                    plan.append((pl, take))
                    left -= take
            backlog = bool(left == qty and self.backlog_ship and self.backlog_ship(pid))
            if not left and outside:
                raise WmsError('Boxloft already knows where this came from: use Packed and shipped')
            if hint and outside:
                raise WmsError('The Stock audit expects this item: use Packed and shipped or It\'s in a box')
            if left and not backlog and not outside:
                raise WmsError('nothing says it is here: no Stock audit row or order with units left. Receive it first, '
                               'or ship it from its box')
            if left and not backlog and auto and self._ever_held(pid):
                # Fable 11-12: an item that sat in a box here and is at 0 now is a missing unit; a person checks it.
                raise WmsError('it was in a box here before, so a person checks it: "Sold before it came in" if this '
                               'one never reached a box, else find it')
            if outside:
                try:
                    cost_cents = None if cost_cents in (None, '') else int(cost_cents)
                except (TypeError, ValueError):
                    raise WmsError('Cost must be a number')
                if cost_cents is not None and not 0 < cost_cents <= 10_000_000:
                    raise WmsError('Enter a cost above $0, or leave it blank')
            customer = {'location_id': self.wms._q1("SELECT id FROM location WHERE code = 'CUSTOMER'")['id']}
            tag = f"Shipped {ln['channel']} {ln['external_id']}, never scanned in"
            if backlog:   # Alex 11-12: listed on eBay before Boxloft; no buy or box here, so no cost and no shelf change
                r = self.wms.receive(user_id, pid, qty, None, customer, reference=ln['external_id'] or '',
                                     note=f"{tag}: listed on eBay before Boxloft, cost not entered", device=device,
                                     allow_virtual=True)
                done.append(r['action_id'])
            for pl, take in ([] if backlog else plan):
                r = self.wms.receive(user_id, pid, take, pl['unit_cost_cents'], customer, reference=pl['order_id'] or '',
                                     note=tag, device=device, purchase_line_id=pl['id'], allow_virtual=True)
                done.append(r['action_id'])
            if outside and left and not backlog:
                bought_on = (bought_on or '').strip()[:60]
                r = self.wms.receive(user_id, pid, left, cost_cents, customer, reference=bought_on or ln['external_id'] or '',
                                     note=f"{tag}: sold before it came in, " + (f"bought on {bought_on}" if bought_on else
                                          'buy not in Boxloft') + ('' if cost_cents else ', cost not entered')
                                          + (f" (Boxloft held none when {ln['channel']} shipped it)" if auto else ''),
                                     device=device, allow_virtual=True)
                done.append(r['action_id'])
            with self.wms._tx() as c:
                if not c.execute('UPDATE sales_line SET shipped_action_id = ? WHERE id = ? AND shipped_action_id IS NULL',
                                 (done[0], ln['id'])).rowcount:
                    raise WmsError('That line was just shipped by someone else; refresh')
        with self.wms._tx() as c:
            order_done = self.commerce.close_line(c, ln['id'], ln['order_id'], done[0], ship_box_id, '')
            uid = c.execute("""INSERT INTO ship_unscanned (sale_line_id, at, user_id, auto, item_id, action_ids)
                               VALUES (?,?,?,?,?,?)""", (ln['id'], now_utc(), user_id, int(auto), item_id,
                                                        ','.join(str(a) for a in done))).lastrowid
        return {'action_id': done[0], 'order_done': order_done, 'unscanned_id': uid, 'item_id': item_id, 'qty': qty}

    def _ever_held(self, product_id: int) -> bool:
        """True: a unit of this item was in a box or on a shelf here at some time (undone moves count too)."""
        return bool(self.wms._q1("""SELECT 1 AS x FROM stock_move m LEFT JOIN location l ON l.id = m.to_location_id
                                     WHERE m.product_id = ? AND (m.to_box_id != 0 OR l.kind != 'virtual') LIMIT 1""",
                                 (product_id,)))

    def undo_unscanned(self, sale_line_id: int, user_id: int, device: str = '') -> dict:
        """Undo a ship_unscanned save: the units go back to the supplier side, the sale is untied. A person's ship
        reopens the order (and gives the ship box back); one the marketplace shipped (auto) stays shipped, and like any
        undone audit save tied to a sale, the sale then stays off every row (the row expects the unit again)."""
        row = self.wms._q1('SELECT * FROM ship_unscanned WHERE sale_line_id = ? AND undone_at IS NULL ORDER BY id DESC',
                           (int(sale_line_id),))
        if not row:
            raise WmsError('Nothing to undo')
        acts = [int(a) for a in row['action_ids'].split(',') if a]
        if row['item_id']:
            out = self.wms._q1('SELECT id FROM audit_outcome WHERE sale_line_id = ? AND action_id = ?', (row['sale_line_id'], acts[0]))
            self.undo_outcome(row['item_id'], out['id'], user_id, device)
        else:
            self.wms.undo_many(user_id, acts, note='Undo: shipped, never scanned in', device=device)
        with self.wms._tx() as c:
            ln = c.execute('SELECT order_id, ship_box_id, box_taken FROM sales_line WHERE id = ?', (row['sale_line_id'],)).fetchone()
            c.execute('UPDATE sales_line SET shipped_action_id = NULL, box_taken = 0 WHERE id = ?', (row['sale_line_id'],))
            if not row['auto']:
                if ln[2] and ln[1]:
                    c.execute('UPDATE ship_box SET stock = stock + 1 WHERE id = ?', (ln[1],))
                c.execute("UPDATE sales_order SET status = 'open' WHERE id = ?", (ln[0],))
            c.execute('UPDATE ship_unscanned SET undone_at = ? WHERE id = ?', (now_utc(), row['id']))
        return {'ok': True, 'reopened': not row['auto']}

    def check_seen(self, item_id: int, seen, user_names: dict = None) -> None:
        """Alex 11-11, two people on one row: a save made from a screen that showed other numbers is refused, with
        who changed the row and what it shows now, so nobody saves "found 2" on a row the other person just
        finished. `seen` is {accounted, expected, closed} as the screen showed them; None skips the check (old
        clients, scripts). Called under the op lock, right before the save."""
        if not isinstance(seen, dict) or 'accounted' not in seen:
            return
        it = self.item(item_id)
        now = (int(it['accounted']), int(it['expected']), bool(it['closed']))
        try:
            was = (int(seen['accounted']), int(seen.get('expected', it['expected'])), bool(seen.get('closed', it['closed'])))
        except (TypeError, ValueError):
            return
        if now == was:
            return
        last = (it.get('history') or [None])[0]
        who = ''
        if last:
            uid = last.get('user_id')
            name = 'Boxloft (auto)' if uid == 0 else (user_names or {}).get(uid, 'Someone')
            qty = last.get('qty') or 0
            did = {'found': f'found {qty}', 'sold': f'recorded {qty} sold', 'sent_back': f'recorded {qty} sent back',
                   'close': 'closed it as missing', 'reopen': 'reopened it', 'missing': 'closed it as missing'
                   }.get(last.get('kind'), 'changed it')
            if last.get('undone'):
                did = f'undid a save ({did})'
            who = f"{name} {did} at {_et_time(last.get('at'))}. "
        state = 'It is closed now.' if it['closed'] else f"Now {max(0, it['expected'] - it['accounted'])} of {it['expected']} left."
        raise Conflict(f'This item changed since your screen loaded. {who}{state} Nothing was saved: check and save again.')

    def _sold(self, it: dict, qty: int, channel: str, user_id: int, price_cents=None, sold_on: str = '',
              order_ref: str = '', note: str = '', device: str = '', sale_line_id: int = None, open_ok: bool = False):
        """One sold save on the row `it` (as read by item()); keeps it['accounted'] and it['product_id'] current
        so sold_all can call it again without reading the row each time. open_ok: the sale may still be open (it is
        being shipped from the package now, ship_unscanned)."""
        item_id = it['id']
        sale = None
        if sale_line_id:
            sale = self.wms._q1("""SELECT l.id, l.qty, l.price_cents, l.shipped_action_id, o.channel, o.external_id, o.ordered_at, o.status
                                     FROM sales_line l JOIN sales_order o ON o.id = l.order_id WHERE l.id = ?""", (int(sale_line_id),))
            if not sale or sale['status'] not in (('open', 'shipped') if open_ok else ('shipped',)):
                raise WmsError('That sale is not a shipped order')
            if sale['shipped_action_id']:
                raise WmsError('That sale is already tied to stock')
            if not 1 <= qty <= sale['qty']:
                raise WmsError(f"That order line has {sale['qty']}")
            channel, price_cents, sold_on, order_ref = sale['channel'], sale['price_cents'], sale['ordered_at'] or '', sale['external_id']
        channel = (channel or '').strip().lower()
        if channel not in CHANNELS:
            raise WmsError('Pick where it sold')
        if price_cents not in (None, ''):
            price_cents = int(price_cents)
            if price_cents < 0:
                raise WmsError('Price cannot be negative')
        else:
            price_cents = None
        sold_on = (sold_on or '').strip()[:10] or None
        self._check_room(it, qty)
        pid = it['product_id'] = self._product_for(it, user_id)
        customer =self.wms._q1("SELECT id FROM location WHERE code = 'CUSTOMER'")['id']
        to = {'location_id': customer}
        where = CHANNELS[channel]
        tag = f'Audit: sold on {where}' + (f' ({note})' if note else '')
        done = []
        plan, left = self._receive_plan(pid, it, qty)
        for line, take in plan:
            r = self.wms.receive(user_id, pid, take, line['unit_cost_cents'], to, reference=line['order_id'] or '',
                                 note=tag, device=device, purchase_line_id=line['id'], allow_virtual=True)
            done.append((r['action_id'], take, take * line['unit_cost_cents'], line['id']))
        if left > 0:
            r = self.wms.receive(user_id, pid, left, it['avg_cost_cents'], to, reference='audit', note=tag + ', no open order line',
                                 device=device, allow_virtual=True)
            done.append((r['action_id'], left, left * it['avg_cost_cents'], None))
        batch = secrets.token_hex(6)
        p = self.wms._q1('SELECT sku, title FROM product WHERE id = ?', (pid,))
        with self.wms._tx() as c:
            if sale:
                cur = c.execute("""UPDATE sales_line SET shipped_action_id = ?, product_id = COALESCE(product_id, ?)
                                     WHERE id = ? AND shipped_action_id IS NULL""", (done[0][0], pid, sale['id']))
                if not cur.rowcount:
                    raise WmsError('That sale was just tied to stock by someone else; refresh')
                sl = sale['id']
            else:
                ref = (order_ref or '').strip()[:80] or f'audit-{batch}'
                c.execute("""INSERT OR IGNORE INTO sales_order (channel, external_id, ordered_at, status, note, imported_at)
                             VALUES (?,?,?,'shipped',?,?)""", (channel, ref, sold_on or date.today().isoformat(), AUDIT_SALE_NOTE, now_utc()))
                so = c.execute('SELECT id FROM sales_order WHERE channel = ? AND external_id = ?', (channel, ref)).fetchone()[0]
                sl = c.execute("""INSERT INTO sales_line (order_id, line_key, sku, asin, title, qty, product_id, shipped_action_id, price_cents)
                                  VALUES (?,?,?,?,?,?,?,?,?)""", (so, f"{it['asin']}-{batch}", p['sku'], '' if it.get('line_id') else it['asin'],
                                                                  p['title'], qty, pid, done[0][0], price_cents)).lastrowid
            for action_id, n, cost, line_id in done:
                c.execute("""INSERT INTO audit_outcome (item_id, at, user_id, kind, qty, cost_cents, channel, price_cents, sold_on,
                             order_ref, line_id, action_id, sale_line_id, note, batch) VALUES (?,?,?,'sold',?,?,?,?,?,?,?,?,?,?,?)""",
                          (item_id, now_utc(), user_id, n, cost, channel, price_cents, sold_on, (order_ref or '')[:80], line_id,
                           action_id, sl, (note or '')[:300], batch))
        it['accounted'] += qty

    def sent_back(self, item_id: int, qty: int, user_id: int, state: str = 'requested', reason: str = '',
                  refund_cents=None, note: str = '') -> dict:
        """Sent back to the seller. Claims returns Amazon already shows on this ASIN's lines first, then adds
        returns on lines that still have units left to receive (oldest order first)."""
        from returns import STATES
        qty = int(qty or 0)
        if state not in STATES or state == 'kept':
            raise WmsError('Pick where the return stands')
        if not self.returns:
            raise WmsError('Returns are not set up')
        it = self.item(item_id)
        self._check_room(it, qty)
        lines = self.wms._q(f"""
            SELECT l.id, l.unit_cost_cents, {LINE_LEFT_SQL} AS left_to_receive, o.external_id AS order_id,
                   COALESCE((SELECT pr.qty - pr.shelf_qty FROM purchase_return pr WHERE pr.line_id = l.id AND pr.state != 'kept'), 0) AS ret,
                   COALESCE((SELECT SUM(ao.qty) FROM audit_outcome ao WHERE ao.line_id = l.id AND ao.kind = 'returned'
                              AND NOT EXISTS (SELECT 1 FROM audit_outcome u WHERE u.kind = 'undo' AND u.undoes = ao.id)), 0) AS claimed
              FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id
             WHERE ((l.asin = ? AND l.asin != '') OR l.id = ?) AND l.cancelled = 0 ORDER BY o.ordered_at, l.id""",
            (it['asin'], it.get('line_id') or -1))
        plan, left = [], qty
        for ln in lines:   # returns already on record (Amazon page or data request), not yet claimed by the audit
            take = min(left, ln['ret'] - ln['claimed'])
            if take > 0:
                plan.append((ln, take, 0))
                left -= take
        for ln in lines:   # new returns on lines with units still expected
            take = min(left, ln['left_to_receive'])
            if take > 0:
                plan.append((ln, take, take))
                left -= take
        if left > 0:
            raise WmsError(f'Only {qty - left} can be matched to an order line. A return needs the order it was bought on: '
                           'add the purchase first, or mark the rest missing.')
        batch = secrets.token_hex(6)
        with self.wms._tx() as c:
            for ln, n, added in plan:
                if added:
                    self.returns._save(c, ln['id'], 'audit', user_id, add_qty=added, state=state, reason=reason or None,
                                       refund_cents=refund_cents, note=note or None, text=f'Stock audit: {added} sent back')
                elif state:
                    self.returns._save(c, ln['id'], 'audit', user_id, state=state, advance=True, reason=reason or None,
                                       refund_cents=refund_cents)
                c.execute("""INSERT INTO audit_outcome (item_id, at, user_id, kind, qty, cost_cents, order_ref, line_id,
                             return_added, return_state, reason, note, batch) VALUES (?,?,?,'returned',?,?,?,?,?,?,?,?,?)""",
                          (item_id, now_utc(), user_id, n, n * ln['unit_cost_cents'], ln['order_id'], ln['id'], added, state,
                           (reason or '')[:120], (note or '')[:300], batch))
        return self.item(item_id)

    def undo_outcome(self, item_id: int, outcome_id: int, user_id: int, device: str = '') -> dict:
        """Undo one sold / sent-back save (its whole batch)."""
        row = self.wms._q1("SELECT kind, batch FROM audit_outcome WHERE id = ? AND item_id = ? AND kind IN ('sold', 'returned')",
                           (outcome_id, item_id))
        if not row:
            raise WmsError('Nothing to undo')
        rows = self.wms._q("SELECT * FROM audit_outcome WHERE item_id = ? AND kind = ? AND (batch = ? OR id = ?)",
                           (item_id, row['kind'], row['batch'], outcome_id))
        if row['kind'] == 'sold':
            acts = [r['action_id'] for r in rows if self.wms._q1('SELECT reversed_by FROM action WHERE id = ?', (r['action_id'],))['reversed_by'] is None]
            if not acts:
                raise WmsError('Already undone')
            for a in acts:
                self.wms.undo(user_id, a, note='Audit: undo sold', device=device)
            with self.wms._tx() as c:
                for so in {r['sale_line_id'] for r in rows if r['sale_line_id']}:
                    oid, note = c.execute('SELECT l.order_id, o.note FROM sales_line l JOIN sales_order o ON o.id = l.order_id '
                                          'WHERE l.id = ?', (so,)).fetchone()
                    if note != AUDIT_SALE_NOTE:   # a real marketplace sale: it stays sold, just not tied to stock again
                        c.execute('UPDATE sales_line SET shipped_action_id = NULL WHERE id = ?', (so,))
                        continue
                    live = c.execute("""SELECT COUNT(*) FROM sales_line l JOIN action a ON a.id = l.shipped_action_id
                                        WHERE l.order_id = ? AND a.reversed_by IS NULL""", (oid,)).fetchone()[0]
                    if not live:
                        c.execute("UPDATE sales_order SET status = 'cancelled' WHERE id = ?", (oid,))
            return self.item(item_id)
        live = [r for r in rows if not self.wms._q1("SELECT 1 AS x FROM audit_outcome WHERE kind = 'undo' AND undoes = ?", (r['id'],))]
        if not live:
            raise WmsError('Already undone')
        with self.wms._tx() as c:
            for r in live:
                if r['return_added'] and r['line_id']:
                    cur = c.execute('SELECT qty FROM purchase_return WHERE line_id = ?', (r['line_id'],)).fetchone()
                    if cur:
                        rest = cur[0] - r['return_added']
                        if rest > 0:
                            self.returns._save(c, r['line_id'], 'audit', user_id, qty=rest, text='Stock audit: undo sent back')
                        else:
                            self.returns._save(c, r['line_id'], 'audit', user_id, state='kept', text='Stock audit: undo sent back')
                c.execute("""INSERT INTO audit_outcome (item_id, at, user_id, kind, qty, undoes, batch)
                             VALUES (?,?,?,'undo',?,?,?)""", (item_id, now_utc(), user_id, r['qty'], r['id'], r['batch']))
        return self.item(item_id)

    def outcomes(self) -> dict:
        """Where the audited units went: units and cost by outcome, and sales by platform (the analytics view)."""
        rows = self.wms._q("""SELECT outcome, channel, SUM(qty) AS units, SUM(cost_cents) AS cost_cents,
                                     SUM(CASE WHEN price_cents IS NOT NULL THEN qty * price_cents END) AS sales_cents,
                                     SUM(CASE WHEN price_cents IS NOT NULL THEN qty END) AS priced_units,
                                     SUM(CASE WHEN price_cents IS NOT NULL THEN cost_cents END) AS priced_cost_cents
                                FROM audit_disposition GROUP BY outcome, channel ORDER BY outcome, units DESC""")
        for r in rows:
            r['channel_label'] = CHANNELS.get(r['channel'], r['channel'])
        closed = {}
        items = self._rows()
        rec_units = sum(it['received'] for it in items)
        if rec_units:   # checked in on Receive against a row's orders: on our shelves too
            shelf = next((r for r in rows if r['outcome'] == 'in_stock'), None)
            if not shelf:
                shelf = {'outcome': 'in_stock', 'channel': '', 'channel_label': '', 'units': 0, 'cost_cents': 0,
                         'sales_cents': None, 'priced_units': None, 'priced_cost_cents': None}
                rows.insert(0, shelf)
            shelf['units'] += rec_units
            shelf['cost_cents'] += sum(it['received_cents'] for it in items)
        for it in items:
            if it['closed'] and it['expected'] > it['accounted']:
                k = it['close_reason'] or 'Missing'
                g = closed.setdefault(k, {'units': 0, 'cost_cents': 0})
                n = it['expected'] - it['accounted']
                g['units'] += n
                g['cost_cents'] += n * it['avg_cost_cents']
        return {'outcomes': rows, 'closed': closed, 'channels': CHANNELS}
