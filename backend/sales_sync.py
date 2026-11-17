"""Marketplace order sync: one normalized order shape, one adapter per marketplace.

Every adapter returns orders shaped like:
    {'external_id', 'ordered_at' (YYYY-MM-DD, ET), 'ship_by', 'market_status' (text from the marketplace),
     'state': 'open' | 'shipped' | 'cancelled',   # the marketplace's view
     'fulfilled_by': 'me' | 'amazon',              # who ships it (FBA = amazon)
     'lines': [{'line_key', 'sku', 'asin', 'title', 'qty', 'price_cents' (per unit or None)}]}

`Sales.upsert(channel, orders)` writes them into sales_order / sales_line, keyed on
(channel, external_id) and (order_id, line_key), so a re-sync writes nothing new.

Rules (nothing here moves stock; scanning out on Orders does, and Commerce.auto_ship right after each live sync):
- New order we ship (fulfilled_by 'me') that is still open -> on the pick list (status open).
- New order shipped by Amazon (FBA), or already shipped/cancelled before Boxloft saw it -> recorded
  with that status, never on the pick list.
- Open order the marketplace cancelled, with nothing scanned out -> cancelled.
- Open order the marketplace says shipped, but not scanned out here -> stays open and gets
  `review` set. Commerce.auto_ship (Alex 11-11) then scans it out the way the ship dialog's defaults would;
  when those defaults can't be right, the review stays with the reason, for a person.
- A live sync (not the history backfill) that first sees an order we ship already shipped (placed and shipped
  between two syncs, within 3 days) treats it the same way, so its stock comes off too.
"""

import config
import gzip
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from connections import EBAY_API, SPAPI_NA, ConnectionError_, _err_text, _http
from wms import Wms, WmsError, now_utc

ET = config.TZ
MARKED_SHIPPED = 'Marked shipped on the marketplace, not scanned out here'   # = Commerce.MARKED_SHIPPED
US_MARKETPLACE = 'ATVPDKIKX0DER'
FIRST_SYNC_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_state (
    channel TEXT PRIMARY KEY,
    cursor TEXT,               -- ISO UTC: next run asks for orders changed after this (minus an overlap)
    last_run_at TEXT,
    last_ok_at TEXT,
    last_error TEXT,
    last_result TEXT           -- JSON counts of the last run
);
"""


def _et_date(iso: str):
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace('Z', '+00:00'))
    except ValueError:
        return iso[:10]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET).date().isoformat()


def _cents(amount) -> int | None:
    try:
        return round(float(amount) * 100)
    except (TypeError, ValueError):
        return None


class Sales:
    def __init__(self, wms: Wms, commerce, connections):
        self.wms = wms
        self.commerce = commerce
        self.connections = connections
        conn = wms._conn()
        conn.executescript(SCHEMA)
        Wms._add_column(conn, 'sales_order', 'fulfilled_by', "TEXT NOT NULL DEFAULT 'me'")
        Wms._add_column(conn, 'sales_order', 'market_status', "TEXT NOT NULL DEFAULT ''")
        Wms._add_column(conn, 'sales_order', 'review', "TEXT NOT NULL DEFAULT ''")
        Wms._add_column(conn, 'sales_order', 'synced_at', 'TEXT')

    # ------------------------------------------------------------------ the one write path

    def upsert(self, channel: str, orders: list, live: bool = False) -> dict:
        counts = {'orders': len(orders), 'new_orders': 0, 'new_lines': 0, 'unmatched': 0,
                  'cancelled': 0, 'review': 0, 'fba': 0}
        now = now_utc()
        recent = (datetime.now(ET) - timedelta(days=3)).date().isoformat()
        with self.wms.op_lock, self.wms._tx() as c:   # sweep B-u1: the sync thread waits for a save in progress
            for o in orders:
                ext = str(o.get('external_id') or '').strip()
                if not ext or not o.get('lines'):
                    continue
                by = 'amazon' if o.get('fulfilled_by') == 'amazon' else 'me'
                state = o.get('state') if o.get('state') in ('open', 'shipped', 'cancelled') else 'open'
                row = c.execute('SELECT id, status, review FROM sales_order WHERE channel = ? AND external_id = ?',
                                (channel, ext)).fetchone()
                if row is None:
                    status = 'cancelled' if state == 'cancelled' else ('shipped' if by == 'amazon' or state == 'shipped' else 'open')
                    review = ''
                    if live and status == 'shipped' and by == 'me' and (o.get('ordered_at') or '') >= recent:
                        status, review = 'open', MARKED_SHIPPED   # shipped before we saw it: auto_ship takes the stock off
                        counts['review'] += 1
                    so = c.execute("""INSERT INTO sales_order (channel, external_id, ordered_at, ship_by, status, imported_at,
                                                               fulfilled_by, market_status, synced_at, review)
                                      VALUES (?,?,?,?,?,?,?,?,?,?)""",
                                   (channel, ext, o.get('ordered_at'), o.get('ship_by'), status, now, by,
                                    o.get('market_status', ''), now, review)).lastrowid
                    counts['new_orders'] += 1
                    counts['fba'] += by == 'amazon'
                else:
                    so, status, review = row
                    shipped_here = c.execute('SELECT COUNT(*) FROM sales_line WHERE order_id = ? AND shipped_action_id IS NOT NULL',
                                             (so,)).fetchone()[0]
                    new_status, new_review = status, review
                    # An FBA order is saved as shipped when first seen, even while Pending; Amazon may still cancel
                    # it (sweep C4: it stayed shipped and the audit counted it as a sale).
                    if state == 'cancelled' and not shipped_here and (status == 'open' or (status == 'shipped' and by == 'amazon')):
                        new_status, new_review = 'cancelled', ''
                        counts['cancelled'] += 1
                    elif status == 'open' and state == 'shipped' and by == 'me':
                        new_review = review if (review or '').startswith(MARKED_SHIPPED) else MARKED_SHIPPED
                        counts['review'] += not review
                    c.execute("""UPDATE sales_order SET status = ?, review = ?, market_status = ?, ship_by = COALESCE(?, ship_by),
                                        fulfilled_by = ?, synced_at = ? WHERE id = ?""",
                              (new_status, new_review, o.get('market_status', ''), o.get('ship_by'), by, now, so))
                for ln in o['lines']:
                    key = str(ln.get('line_key') or ln.get('sku') or ln.get('asin') or '')[:120]
                    qty = int(ln.get('qty') or 0)
                    if not key or qty <= 0:
                        continue
                    sku, asin = (ln.get('sku') or '').strip(), (ln.get('asin') or '').strip().upper()
                    pid = self.commerce._match_product(c, sku, asin)
                    cur = c.execute("""INSERT OR IGNORE INTO sales_line (order_id, line_key, sku, asin, title, qty, product_id, price_cents)
                                       VALUES (?,?,?,?,?,?,?,?)""",
                                    (so, key, sku, asin, (ln.get('title') or '')[:300], qty, pid, ln.get('price_cents')))
                    if cur.rowcount:
                        counts['new_lines'] += 1
                        counts['unmatched'] += 0 if pid else 1
                    else:  # price can arrive later (Amazon pending orders); never touch a shipped line's qty
                        c.execute("""UPDATE sales_line SET price_cents = COALESCE(?, price_cents),
                                            qty = CASE WHEN shipped_action_id IS NULL THEN ? ELSE qty END
                                      WHERE order_id = ? AND line_key = ?""", (ln.get('price_cents'), qty, so, key))
        return counts

    # ------------------------------------------------------------------ runs

    def state(self) -> dict:
        return {r['channel']: r for r in self.wms._q('SELECT * FROM sync_state')}

    def run(self, channel: str, progress=None) -> dict:
        fetch = {'amazon': self._amazon_orders, 'ebay': self._ebay_orders}.get(channel)
        if not fetch:
            raise WmsError('No order sync for that connection yet')
        st = self.wms._q1('SELECT cursor FROM sync_state WHERE channel = ?', (channel,)) or {}
        since = st.get('cursor')
        if since:  # 1 hour overlap: marketplaces publish updates a little late
            since = (datetime.fromisoformat(since.replace('Z', '+00:00')) - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        else:
            since = (datetime.now(timezone.utc) - timedelta(days=FIRST_SYNC_DAYS)).strftime('%Y-%m-%dT%H:%M:%SZ')
        started = now_utc()
        try:
            orders = fetch(since, progress)
            result = self.upsert(channel, orders, live=True)
            err = None
        except (ConnectionError_, WmsError) as e:
            result, err = None, str(e)
        with self.wms._tx() as c:
            c.execute("""INSERT INTO sync_state (channel, cursor, last_run_at, last_ok_at, last_error, last_result)
                         VALUES (?,?,?,?,?,?)
                         ON CONFLICT(channel) DO UPDATE SET last_run_at = excluded.last_run_at,
                             cursor = CASE WHEN excluded.last_error IS NULL THEN excluded.cursor ELSE sync_state.cursor END,
                             last_ok_at = COALESCE(excluded.last_ok_at, sync_state.last_ok_at),
                             last_error = excluded.last_error,
                             last_result = COALESCE(excluded.last_result, sync_state.last_result)""",
                      (channel, started if err is None else None, started, started if err is None else None, err,
                       json.dumps(result) if result else None))
        if err:
            raise WmsError(err)
        return result

    # ------------------------------------------------------------------ Amazon SP-API

    def _amazon_get(self, base: str, path: str, token_box: dict):
        """GET with the SP-API rate limits: on 429 wait and retry (up to ~5 min), refresh the token when it ages."""
        for attempt in range(10):
            if time.time() - token_box.get('at', 0) > 3000:
                token_box['token'], token_box['at'] = self.connections.amazon_access_token(), time.time()
            st, p = _http('GET', base + path, {'x-amz-access-token': token_box['token'], 'Accept': 'application/json',
                                               'User-Agent': 'Boxloft/1.0 (Language=Python)'})
            if st == 200:
                return p
            if st in (429, 500, 503):
                time.sleep(min(60, 5 * (attempt + 1)))
                continue
            raise ConnectionError_(f'Amazon SP-API {st}: {_err_text(p)}')
        raise ConnectionError_('Amazon SP-API kept saying "too many requests"; will try again next run')

    def _amazon_orders(self, since: str, progress=None, base: str = SPAPI_NA) -> list:
        tok = {}
        q = {'MarketplaceIds': US_MARKETPLACE, 'LastUpdatedAfter': since, 'MaxResultsPerPage': 100}
        raw = []
        while True:
            p = self._amazon_get(base, '/orders/v0/orders?' + urllib.parse.urlencode(q), tok)
            payload = p.get('payload', {})
            raw += payload.get('Orders', [])
            if progress:
                progress(f'{len(raw)} Amazon orders listed')
            nxt = payload.get('NextToken')
            if not nxt:
                break
            q = {'MarketplaceIds': US_MARKETPLACE, 'NextToken': nxt}
            time.sleep(1)  # getOrders: burst 20, then 1 every 60 s; the 429 retry covers long backfills
        out = []
        for i, o in enumerate(raw):
            items = []
            path = f"/orders/v0/orders/{urllib.parse.quote(o['AmazonOrderId'])}/orderItems"
            while path:
                p = self._amazon_get(base, path, tok).get('payload', {})
                items += p.get('OrderItems', [])
                nt = p.get('NextToken')
                path = f"/orders/v0/orders/{urllib.parse.quote(o['AmazonOrderId'])}/orderItems?NextToken={urllib.parse.quote(nt)}" if nt else None
                time.sleep(2.1)  # getOrderItems: 0.5 per second
            out.append(amazon_order(o, items))
            if progress and i % 10 == 9:
                progress(f'{i + 1} of {len(raw)} Amazon orders read')
        return out

    def backfill_amazon(self, since: str, until: str = None, progress=None) -> dict:
        """Every Amazon order from `since` (YYYY-MM-DD) to now, from the all-orders report in 29-day pieces,
        all asked for at once. Same write path as the sync (keys are Amazon's order-item ids), so running it
        twice, or over days the sync already read, writes nothing twice. Never moves stock."""
        start = datetime.fromisoformat(since).replace(tzinfo=ET).astimezone(timezone.utc)
        end = (datetime.fromisoformat(until).replace(tzinfo=ET).astimezone(timezone.utc) if until
               else datetime.now(timezone.utc) - timedelta(minutes=5))
        if end <= start:
            raise WmsError('Pick a start date before today')
        specs = [{'reportType': 'GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL', 'dataStartTime': a, 'dataEndTime': b}
                 for a, b in report_windows(start, end)]
        texts = sp_reports(self.connections, specs, {}, progress, 'order report')
        orders = {}
        for t in texts:
            for o in amazon_report_orders(t):
                orders[o['external_id']] = o   # pieces meet at the edges: one copy per order
        result = self.upsert('amazon', [o for o in orders.values() if o['lines']])
        result['reports'] = len(specs)
        return result

    # ------------------------------------------------------------------ eBay Sell Fulfillment

    def _ebay_orders(self, since: str, progress=None) -> list:
        token = self.connections.ebay_user_token()
        out, offset = [], 0
        flt = f'lastmodifieddate:[{since[:19]}.000Z..]'
        while True:
            q = urllib.parse.urlencode({'filter': flt, 'limit': 200, 'offset': offset})
            st, p = _http('GET', f'{EBAY_API}/sell/fulfillment/v1/order?{q}',
                          {'Authorization': f'Bearer {token}', 'Accept': 'application/json'})
            if st != 200:
                raise ConnectionError_(f'eBay orders {st}: {_err_text(p)}')
            out += [ebay_order(o) for o in p.get('orders', [])]
            if progress:
                progress(f'{len(out)} of {p.get("total", len(out))} eBay orders read')
            offset += 200
            if offset >= int(p.get('total') or 0):
                return out


def amazon_order(o: dict, items: list) -> dict:
    s = o.get('OrderStatus', '')
    state = 'cancelled' if s in ('Canceled', 'Unfulfillable') else ('shipped' if s == 'Shipped' else 'open')
    lines = []
    for it in items:
        qty = int(it.get('QuantityOrdered') or 0)
        total = _cents((it.get('ItemPrice') or {}).get('Amount'))
        lines.append({'line_key': it.get('OrderItemId'), 'sku': it.get('SellerSKU', ''), 'asin': it.get('ASIN', ''),
                      'title': it.get('Title', ''), 'qty': qty,
                      'price_cents': round(total / qty) if total is not None and qty else None})
    return {'external_id': o.get('AmazonOrderId'), 'ordered_at': _et_date(o.get('PurchaseDate')),
            'ship_by': _et_date(o.get('LatestShipDate')), 'market_status': s, 'state': state,
            'fulfilled_by': 'amazon' if o.get('FulfillmentChannel') == 'AFN' else 'me', 'lines': lines}


def ebay_order(o: dict) -> dict:
    cancel = (o.get('cancelStatus') or {}).get('cancelState', '')
    fs = o.get('orderFulfillmentStatus', '')
    state = 'cancelled' if cancel == 'CANCELED' else ('shipped' if fs == 'FULFILLED' else 'open')
    lines, ship_by = [], None
    for li in o.get('lineItems', []):
        qty = int(li.get('quantity') or 0)
        total = _cents((li.get('lineItemCost') or {}).get('value'))
        sb = (li.get('lineItemFulfillmentInstructions') or {}).get('shipByDate')
        ship_by = min(filter(None, [ship_by, _et_date(sb)]), default=None)
        # No Custom label: the listing's key (listings.ebay_item_key), so the sale follows a listing a person linked.
        sku = li.get('sku') or (f'ITEM-{li["legacyItemId"]}' if li.get('legacyItemId') else '')
        lines.append({'line_key': li.get('lineItemId'), 'sku': sku, 'asin': '',
                      'title': li.get('title', ''), 'qty': qty,
                      'price_cents': round(total / qty) if total is not None and qty else None})
    return {'external_id': o.get('orderId'), 'ordered_at': _et_date(o.get('creationDate')), 'ship_by': ship_by,
            'market_status': f'{fs}{" / " + cancel if cancel and cancel != "NONE_REQUESTED" else ""}',
            'state': state, 'fulfilled_by': 'me', 'lines': lines}


# ---------------------------------------------------------------------- SP-API calls and reports (shared)

def sp_call(connections, method: str, path: str, tok: dict, body: dict = None, base: str = SPAPI_NA):
    """One SP-API call with the rate-limit retry; refreshes the LWA token when it ages."""
    for attempt in range(12):
        if time.time() - tok.get('at', 0) > 3000:
            tok['token'], tok['at'] = connections.amazon_access_token(), time.time()
        h = {'x-amz-access-token': tok['token'], 'Accept': 'application/json', 'User-Agent': 'Boxloft/1.0 (Language=Python)'}
        data = None
        if body is not None:
            h['Content-Type'] = 'application/json'
            data = json.dumps(body).encode()
        st, p = _http(method, base + path, h, data, timeout=60)
        if st in (200, 202):
            return p
        if st in (429, 500, 503):
            time.sleep(min(60, 5 * (attempt + 1)))
            continue
        raise ConnectionError_(f'Amazon SP-API {st}: {_err_text(p)}')
    raise ConnectionError_('Amazon SP-API kept saying "too many requests"; will try again next run')


def sp_reports(connections, specs: list, tok: dict, progress=None, what: str = 'report') -> list:
    """Asks for every report in `specs` at once, then waits for all of them together.
    Amazon builds them in parallel: 20 monthly order reports took 6 minutes this way, 34 one at a time
    (measured 2026-11-10). The wait left is createReport's limit (15 at once, then about 1 a minute).
    specs: [{'reportType', 'dataStartTime'?, 'dataEndTime'?}]. Returns the report texts in the same order."""
    ids = []
    for i, s in enumerate(specs):
        ids.append(sp_call(connections, 'POST', '/reports/2021-06-30/reports', tok,
                           {'marketplaceIds': [US_MARKETPLACE], **s})['reportId'])
        if progress and len(specs) > 1:
            progress(f'asked Amazon for {what} {i + 1} of {len(specs)}')
    out, pending, started = [None] * len(specs), dict(enumerate(ids)), time.time()
    while pending:
        if time.time() - started > 3600:
            raise ConnectionError_(f'Amazon took over an hour to build the {what}; will try again next run')
        time.sleep(10)
        for i, rid in list(pending.items()):
            r = sp_call(connections, 'GET', f'/reports/2021-06-30/reports/{rid}', tok)
            st = r.get('processingStatus')
            if st in ('CANCELLED', 'FATAL'):
                raise ConnectionError_(f'Amazon could not build the {what} ({st})')
            if st != 'DONE':
                continue
            d = sp_call(connections, 'GET', f"/reports/2021-06-30/documents/{r['reportDocumentId']}", tok)
            with urllib.request.urlopen(d['url'], timeout=300) as f:
                raw = f.read()
            if d.get('compressionAlgorithm') == 'GZIP':
                raw = gzip.decompress(raw)
            for enc in ('utf-8', 'cp1252'):
                try:
                    out[i] = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                out[i] = raw.decode('utf-8', 'replace')
            del pending[i]
        if progress:
            progress(f'Amazon built {len(specs) - len(pending)} of {len(specs)} {what}s')
    return out


def report_windows(start: datetime, end: datetime, days: int = 29) -> list:
    """The all-orders report covers at most 30 days per request."""
    out, cur = [], start
    while cur < end:
        nxt = min(cur + timedelta(days=days), end)
        out.append((cur.strftime('%Y-%m-%dT%H:%M:%SZ'), nxt.strftime('%Y-%m-%dT%H:%M:%SZ')))
        cur = nxt
    return out


def amazon_report_orders(text: str) -> list:
    """GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL (tab file) -> normalized orders.
    One row per order item; item-price is the line total. Multi-channel (non-Amazon.com) orders are FBA
    shipping a sale made elsewhere, so they are left out here (that channel records the sale)."""
    import csv
    import io
    orders = {}
    for r in csv.DictReader(io.StringIO(text), delimiter='\t'):
        r = {(k or '').strip().lstrip('﻿'): (v or '').strip() for k, v in r.items() if k}
        oid = r.get('amazon-order-id', '')
        if not oid or r.get('sales-channel', 'Amazon.com') != 'Amazon.com':
            continue
        s = r.get('order-status', '')
        state = 'cancelled' if s == 'Cancelled' else ('shipped' if s.startswith('Shipped') else 'open')
        o = orders.setdefault(oid, {'external_id': oid, 'ordered_at': _et_date(r.get('purchase-date')), 'ship_by': None,
                                    'market_status': s, 'state': state, 'lines': [],
                                    'fulfilled_by': 'amazon' if r.get('fulfillment-channel') == 'Amazon' else 'me'})
        qty = int(float(r.get('quantity') or 0))
        if r.get('item-status') == 'Cancelled' or qty <= 0:
            continue
        total = _cents(r.get('item-price'))
        o['lines'].append({'line_key': r.get('order-item-id') or r.get('sku'), 'sku': r.get('sku', ''),
                           'asin': r.get('asin', ''), 'title': r.get('product-name', ''), 'qty': qty,
                           'price_cents': round(total / qty) if total is not None else None})
    return list(orders.values())
