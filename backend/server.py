"""Boxloft dashboard server: JSON API, photos and the React app.

Python http.server with threads, no framework (same approach as an earlier
dashboard). Caddy sits in front for HTTPS and forwards to 127.0.0.1:PORT.

Run locally:  BOXLOFT_DATA=./data BOXLOFT_DEV=1 python3 backend/server.py
"""

import ai
import config
import csv
import io
import json
import logging
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auth import SESSION_COOKIE, SESSION_DAYS, Auth, AuthError  # noqa: E402
from codes import make_code, parse_scan  # noqa: E402
from commerce import Catalog, Commerce  # noqa: E402
from inbound import Inbound  # noqa: E402
from listings import Listings  # noqa: E402
from ebay_link import EbayLink  # noqa: E402
from audit import Audit  # noqa: E402
from sorting import Sorting  # noqa: E402
from printq import PrintQueue  # noqa: E402
from connections import ConnectionError_, Connections  # noqa: E402
from returns import Returns  # noqa: E402
import refused  # noqa: E402
from labels import Labels  # noqa: E402
from buyer_returns import BuyerReturns  # noqa: E402
from merge import Merge  # noqa: E402
from sales_sync import Sales  # noqa: E402
from store_mail import StoreMailError, StoreOrders  # noqa: E402
from pagesync import PageSync, PageSyncError, prefetch_photos  # noqa: E402
from shipbox import ShipBoxes  # noqa: E402
from approval import RECHECK_INPUT_MAX, Approval  # noqa: E402
from photo_id import PhotoId  # noqa: E402
from wms import IDENTIFIER_KINDS, REMOVE_TARGETS, Conflict, Wms, WmsError, now_utc  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.abspath(os.environ.get('BOXLOFT_DATA', os.path.join(ROOT, 'data')))
STATIC = os.path.abspath(os.environ.get('BOXLOFT_STATIC', os.path.join(ROOT, 'web', 'dist')))
PORT = int(os.environ.get('BOXLOFT_PORT', '5101'))
DEV = os.environ.get('BOXLOFT_DEV') == '1'
ET = config.TZ
MAX_JSON = 2 * 1024 * 1024
MAX_PHOTO = 15 * 1024 * 1024
MAX_PAGES_JSON = 16 * 1024 * 1024   # extension 2.0 step: up to 20 trimmed order pages

os.makedirs(os.path.join(DATA, 'photos'), exist_ok=True)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('boxloft')

auth = Auth(os.path.join(DATA, 'auth.db'))
wms = Wms(os.path.join(DATA, 'wms.db'))
commerce = Commerce(wms)
shipboxes = ShipBoxes(wms, commerce)   # sets commerce.learned_box: pick_list suggests the box an item went out in last time
catalog = Catalog(wms, os.path.join(DATA, 'photos'))
photo_reader = PhotoId(wms, catalog, os.path.join(DATA, 'photos'))   # New product: photo -> name, brand, our buys
returns = Returns(wms, commerce)
inbound = Inbound(wms, returns)
audit = Audit(wms, commerce, catalog, returns)
connections = Connections(DATA, config.PUBLIC_URL)
sales = Sales(wms, commerce, connections)
listings = Listings(wms, commerce, connections)
listings.catalog = catalog   # Amazon listing photos from Keepa (Alex 11-13)
sorting = Sorting(wms, commerce, audit, catalog, listings)
sorting.photos = photo_reader   # Add card photo (Alex 11-17): the photo goes on the item a save made
labels = Labels(wms)
sorting.labels = labels   # a carrier label scanned on a box page shows what is inside (Alex 11-16)
buyer_returns = BuyerReturns(wms, connections)
labels.returns = buyer_returns   # a buyer's return label shows the item and why it came back (Alex 11-16)
buyer_returns.names = lambda: auth.user_names()
ebay_items = EbayLink(wms, commerce, audit, listings, sorting)   # eBay listings become items (Alex 11-12)
printq = PrintQueue(wms)
approval = Approval(wms, connections)   # can we sell this ASIN on Amazon (Alex 11-13)
merges = Merge(wms, commerce, audit)   # "same item as": fold one item into another (Alex 11-17)


def image_loads(url: str) -> bool:
    """A store photo link loads from here (some stores refuse other sites)."""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
                                                                 '(KHTML, like Gecko) Chrome/142.0 Safari/537.36'})
        with urllib.request.urlopen(req, timeout=6) as r:
            return r.status == 200 and (r.headers.get('Content-Type') or '').startswith('image/')
    except Exception:  # noqa: BLE001 - any failure means: don't show it
        return False


store_orders = StoreOrders(wms, connections, image_check=image_loads)
pagesync = PageSync(wms, inbound, store_orders, os.path.join(DATA, 'photos'), shipboxes)
SYNC_CHANNELS = ('amazon', 'ebay', 'gmail')
sync_progress = {}  # channel -> {'running': bool, 'text': str}
sync_lock = threading.Lock()
MAX_CSV = 25 * 1024 * 1024


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _int(v, default=None):
    """A number from a query or body, or default. Past SQLite's 64 bits counts as no number (review 11-25: '-9...9',
    '9_9', a 20-digit ?before= reached SQLite and crashed, some after a save had committed)."""
    try:
        n = int(v)
    except (TypeError, ValueError, OverflowError):
        return default
    return n if -INT64 <= n < INT64 else default


INT64 = 2 ** 63
ROUTES = []
# Two people at once (Alex 11-11): every write request runs under wms.op_lock from its first check to its last
# write, so a check ("1 left", "not shipped yet") can't be passed by two requests at once. Routes that wait on the
# network or start a background job stay outside, so they never hold up the floor.
UNLOCKED = ('/api/auth/', '/api/connections/', '/api/ebay/', '/api/amazon/', '/api/listings/refresh', '/api/photo-id',
            '/api/client-diag')
LOCK_WAIT = 30   # seconds a write waits for the one before it, then says busy


def _prefetch(h, path: str):
    """Network work a locked route would do, done before the lock: a bin scan of a new barcode asks Keepa (cached
    forever), so the scan under the lock reads the cache and the other person's scans never wait on Keepa."""
    if path in ('/api/audit-sort/scan', '/api/audit-sort/extra', '/api/put', '/api/put/new'):
        code = str(h.json_body().get('code') or '').strip()
        if code and not wms.lookup(code) and not labels.is_label(code):   # a carrier label is never a catalog lookup
            try:
                catalog.lookup(code)
            except Exception:  # noqa: BLE001 - the scan itself reports lookup errors
                pass
        try:   # Amazon approval for the item (Alex 11-13): asked here, never under the lock
            approval.ensure(_scan_asins(h.json_body()))
        except Exception:  # noqa: BLE001 - a hint only; the scan goes on without it
            log.warning('approval prefetch\n%s', traceback.format_exc())
    elif path == '/api/inbound/v2/step':
        # Delivery photos on Track pages: their links expire in 3 days, so copy them now, outside the lock.
        raw = h.body_bytes(MAX_PAGES_JSON)
        if b'progress-tracker' in raw and b'us-prod-temp.s3.amazonaws.com' in raw:
            prefetch_photos(h.json_body(MAX_PAGES_JSON), os.path.join(DATA, 'photos'))


def _scan_asins(d: dict) -> list:
    """The ASIN(s) a scan or pick names: its product, its Stock audit row, its listing, or the scanned code."""
    out = approval.product_asins(_int(d.get('product_id'))) + approval.item_asins(_int(d.get('item_id')))
    lid = _int(d.get('listing_id'))
    if lid:
        li = wms._q1('SELECT asin FROM listing WHERE id = ?', (lid,))
        out += [li['asin']] if li and li['asin'] else []
    code = str(d.get('code') or '').strip()
    return out + (approval.code_asins(code) if code else [])


def amazon_note(asins, to: dict = None) -> dict:
    """What a scan card says about selling the item on Amazon (cache only, so safe under the lock). in_a_box: the
    box is an Amazon (A) box. other_box: when Amazon says no, the eBay box filled last, to move it to."""
    n = approval.note(asins)
    try:
        n['in_a_box'] = bool(to) and sorting.target(to).get('market') == 'A'
    except Exception:  # noqa: BLE001 - a hint only
        n['in_a_box'] = False
    n['other_box'] = None
    if n['can_sell'] is False:
        try:
            b = sorting.market_boxes('E')['boxes'][:1]
            n['other_box'] = b[0] if b else None
        except Exception:  # noqa: BLE001
            pass
    return n


def _with_amazon(r, d: dict, to: dict = None):
    """Adds `amazon` to a scan answer that names an item."""
    if isinstance(r, dict) and r.get('kind') not in ('wrong', 'target', 'need_target', 'new_product', 'new_item'):
        pid = r.get('product_id') or (r.get('product') or {}).get('id')
        asins = approval.product_asins(pid) or _scan_asins(d)
        if asins or pid:
            r['amazon'] = amazon_note(asins, to)
    return r


def _et_clock(ts) -> str:
    from audit import _et_time
    return _et_time(ts)


# Saves that replace what a screen showed (a field, a status, a decision). The page sends `seen`: the values it
# showed. If the row holds other values now, the save is refused (409) with who changed it (edit_log), so an
# older screen can't put back what the other person just saved. Ledger moves (receive, move, remove, ship) move
# what is really there and are checked under the lock instead. Audit rows, products, counts and listing marks
# have their own checks (audit.check_seen, _save_product, count, audit_listing_mark).
EDIT_GUARDS = [
    (re.compile(r'^/api/boxes/(\d+)$'), 'box', 'this box'),
    (re.compile(r'^/api/units/(\d+)$'), 'unit', 'this unit'),
    (re.compile(r'^/api/ship-boxes/(\d+)$'), 'ship_box', 'this ship box'),
    (re.compile(r'^/api/returns/(\d+)$'), 'purchase_return', 'this return'),
    (re.compile(r'^/api/returns/(\d+)/pull$'), 'purchase_return', 'this return'),
    (re.compile(r'^/api/inbound/(\d+)/hide$'), 'purchase_order', 'this order'),
    (re.compile(r'^/api/store-orders/(\d+)/approve$'), 'purchase_order', 'this store order'),
    (re.compile(r'^/api/purchases/(\d+)/cancel$'), 'purchase_line', 'this purchase line'),
    (re.compile(r'^/api/orders/(\d+)/status$'), 'sales_order', 'this order'),
    (re.compile(r'^/api/orders/lines/(\d+)/link$'), 'sales_line', 'this order line'),
    (re.compile(r'^/api/ship-confirm/(\d+)$'), 'sales_line', 'the box for this order'),
]
GUARD_SKIP = {'id', 'created_at', 'updated_at', 'imported_at', 'synced_at', 'last_seen_at', 'seen_at'}


def _norm(v):
    if v is None or v == '':
        return ''
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v) if float(v).is_integer() else round(float(v), 4)
    if isinstance(v, str):
        try:
            f = float(v)
            return int(f) if f.is_integer() else round(f, 4)
        except ValueError:
            return v
    return v


def last_edit(table: str, row_id: int, what: str) -> str:
    """'Sam changed this box at 7:40 AM ET. ' from edit_log, or '' when nobody did through a guarded save."""
    e = wms._q1('SELECT user_id, at FROM edit_log WHERE tbl = ? AND row_id = ? ORDER BY id DESC LIMIT 1', (table, row_id))
    verb = 'changed'
    if table == 'sales_order':   # shipping changes the order without an edit save: name who shipped it, if newer
        s = wms._q1("""SELECT a.user_id, a.at FROM sales_line l JOIN action a ON a.id = l.shipped_action_id
                        WHERE l.order_id = ? ORDER BY a.id DESC LIMIT 1""", (row_id,))
        if s and (not e or s['at'] >= e['at']):
            e, verb = s, 'shipped'
    if not e:
        return ''
    who = 'Boxloft (auto)' if e['user_id'] == 0 else auth.user_names().get(e['user_id'], 'Someone')
    return f"{who} {verb} {what} at {_et_clock(e['at'])}. "


def _guard(h, path: str):
    """Refuse a save whose `seen` values no longer match the row. Returns (table, row_id) to log after the save."""
    for regex, table, what in EDIT_GUARDS:
        m = regex.match(path)
        if not m:
            continue
        row_id = int(m.group(1))
        seen = h.json_body().get('seen')
        if isinstance(seen, dict):
            row = wms._q1(f'SELECT * FROM {table} WHERE id = ?', (row_id,))
            if row is not None:
                diff = [k for k, v in seen.items() if k in row and k not in GUARD_SKIP and _norm(row[k]) != _norm(v)]
                if diff:
                    raise Conflict(f"{what[0].upper()}{what[1:]} changed since your screen loaded. {last_edit(table, row_id, what)}"
                                   "Nothing was saved: the screen now shows the new version. Check it and save again.")
        return table, row_id
    return None


BODY_LIMITS = {'/api/photos': MAX_PHOTO, '/api/inbound/v2/step': MAX_PAGES_JSON,
               '/api/purchases/import': MAX_CSV, '/api/orders/import': MAX_CSV}


def locked(h, path: str, fn, args: dict):
    # Read the whole body before the lock (red line 11-11): a phone photo on weak signal, or the extension's 16 MB
    # step, must not hold every other save while its bytes trickle in. The route's own read returns this copy.
    h.body_bytes(BODY_LIMITS.get(path, MAX_JSON))
    _prefetch(h, path)
    if not wms.op_lock.acquire(timeout=LOCK_WAIT):
        raise HttpError(503, 'Boxloft is busy with another save. Try again in a moment.')
    try:
        edited = _guard(h, path)
        out = fn(h, **args)
        if edited:
            with wms._tx() as c:
                c.execute('INSERT INTO edit_log (tbl, row_id, user_id, at, path) VALUES (?,?,?,?,?)',
                          (edited[0], edited[1], h.user['id'], now_utc(), path))
        return out
    finally:
        wms.op_lock.release()
# Server-to-server callbacks (eBay) that cannot send the X-Boxloft header; each is public and read-nothing.
WEBHOOKS = {'/api/ebay/account-deletion'}


# Path parts that are row ids: the handlers call int() on them. Anything else (a page that sent 'undefined') is a 400
# with words, never a 500 (review 11-24: POST /api/products/undefined/sell-sku answered 'Server error').
ID_PARAMS = {'aid', 'bid', 'cid', 'eid', 'iid', 'jid', 'kid', 'lid', 'mid', 'oid', 'pid', 'po', 'rid', 'tid', 'uid',
             'photo_id', 'track_id'}


def too_big(v) -> bool:
    """A JSON number SQLite can't hold, anywhere in a body: an int or float past 64 bits. Checked before the route runs
    (review 11-24: routes bound them after a save had committed). Strings are never checked here: tracking IDs are
    long digit strings (USPS 22, Walmart 20, review 11-25); a string a route reads as a number goes through _int."""
    if isinstance(v, bool) or v is None:
        return False
    if isinstance(v, (int, float)):
        return not -INT64 <= v < INT64
    if isinstance(v, dict):
        return any(too_big(x) for x in v.values())
    if isinstance(v, list):
        return any(too_big(x) for x in v)
    return False


def route(method: str, pattern: str, public: bool = False, admin: bool = False):
    regex = re.compile('^' + re.sub(r'<(\w+)>', r'(?P<\1>[^/]+)', pattern) + '$')

    def deco(fn):
        ROUTES.append((method, regex, fn, public, admin))
        return fn
    return deco


def release_db():
    """This thread is done with the databases for now: its connections go back to the pool."""
    auth.release()
    wms.release()


def in_thread(fn, *args):
    """Run fn in a daemon thread that hands its db connections back when it ends."""
    def run():
        try:
            fn(*args)
        finally:
            release_db()
    threading.Thread(target=run, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server_version = 'Boxloft'
    protocol_version = 'HTTP/1.1'
    # 11-11 night: keep-alive connections from Caddy never closed, each holding a thread, a socket and its own wms.db
    # and auth.db connections. At 199 threads the process hit 1,023 of 1,024 open files and requests failed with
    # "unable to open database file" (the Overview never loaded). A connection idle this long is closed.
    timeout = 30

    def handle_one_request(self):
        # The db connections go back to the pool after every request, so a kept-alive socket waiting for its next
        # request holds none (dbpool, 11-11 night: 253 auth.db handles were open at 199 threads).
        try:
            super().handle_one_request()
        finally:
            release_db()

    def log_message(self, fmt, *args):
        if DEV:
            log.info('%s %s', self.address_string(), fmt % args)

    # ------------------------------------------------------------------ helpers

    @property
    def ip(self) -> str:
        # Trust X-Forwarded-For only from the local proxy, and only its LAST value: the hop that proxy wrote.
        # A proxy that appends (nginx's $proxy_add_x_forwarded_for) keeps what the client sent in front, so the
        # first value is whatever an attacker typed, and the sign-in lockout would count it (black hat 11-17).
        fwd = self.headers.get('X-Forwarded-For')
        if fwd and self.client_address[0] in ('127.0.0.1', '::1'):
            return fwd.split(',')[-1].strip() or self.client_address[0]
        return self.client_address[0]

    @property
    def device(self) -> str:
        name = (self.headers.get('X-Device') or '').strip()[:60]
        return name or (self.headers.get('User-Agent') or '')[:60]

    def token(self):
        # Parsed by hand: SimpleCookie drops the whole header when any other cookie is malformed.
        for part in (self.headers.get('Cookie') or '').split(';'):
            name, _, value = part.strip().partition('=')
            if name == SESSION_COOKIE and value:
                return value
        return None

    def body_bytes(self, limit: int) -> bytes:
        if getattr(self, '_body', None) is not None:   # read once: the scan prefetch reads it before the route
            return self._body
        length = _int(self.headers.get('Content-Length'), 0)
        if length > limit:
            raise HttpError(413, 'Upload is too large')
        self._body = self.rfile.read(length) if length else b''
        return self._body

    def _json_peek(self, path: str):
        """The JSON body, read once before the route (as locked() does), or None when it is not JSON."""
        raw = self.body_bytes(BODY_LIMITS.get(path, MAX_JSON))
        if not raw or raw[:1] not in (b'{', b'['):
            return None
        try:
            return json.loads(raw)
        except (ValueError, RecursionError):
            return None

    def json_body(self, limit: int = MAX_JSON) -> dict:
        raw = self.body_bytes(limit)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, RecursionError):
            raise HttpError(400, 'Bad JSON')
        if not isinstance(data, dict):
            raise HttpError(400, 'Expected a JSON object')
        return data

    def send_json(self, data, status: int = 200, headers: dict = None):
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, body: bytes, ctype: str, cache: str = 'no-cache', extra: dict = None):
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', cache)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def redirect(self, location: str):
        self.send_response(302)
        self.send_header('Location', location)
        self.send_header('Content-Length', '0')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()

    def session_cookie(self, token: str, clear: bool = False) -> str:
        # HTTPS-only cookie, except on a plain-HTTP address behind Caddy (http:// site, for example a private network).
        plain = self.headers.get('X-Forwarded-Proto') == 'http' and self.client_address[0] in ('127.0.0.1', '::1')
        secure = '' if DEV or plain else ' Secure;'
        if clear:
            return f'{SESSION_COOKIE}=; Path=/; HttpOnly;{secure} SameSite=Lax; Max-Age=0'
        return f'{SESSION_COOKIE}={token}; Path=/; HttpOnly;{secure} SameSite=Lax; Max-Age={SESSION_DAYS * 86400}'

    # ------------------------------------------------------------------ dispatch

    def do_GET(self):
        self.dispatch('GET')

    def do_HEAD(self):
        self.dispatch('GET')

    def do_POST(self):
        # One handler object serves every request on a kept-alive connection (Caddy reuses them), so the body read
        # for one request must never be seen by the next (11-11: a second save got the first save's body), and a
        # body nobody read (401, 404, a route that ignores it) must not be parsed as the next request line.
        self._body = None
        try:
            self.dispatch('POST')
        finally:
            if self._body is None:
                length = _int(self.headers.get('Content-Length'), 0)
                if length > MAX_PAGES_JSON:
                    self.close_connection = True
                elif length > 0:
                    try:
                        self.rfile.read(length)
                    except OSError:
                        self.close_connection = True
            self._body = None

    def dispatch(self, method: str):
        self.user = None   # set per request below; a kept-alive handler must not name the last request's person
        url = urlparse(self.path)
        path = url.path
        if not path.startswith('/api/'):
            if method != 'GET':
                return self.send_json({'error': 'Not found'}, 404)
            return self.serve_static(path)
        try:
            if method == 'POST' and self.headers.get('X-Boxloft') != '1' and path not in WEBHOOKS:
                # Browsers cannot add this header cross-site without a CORS preflight we never grant.
                raise HttpError(403, 'Missing request header')
            for m, regex, fn, public, admin in ROUTES:
                match = regex.match(path)
                if m != method or not match:
                    continue
                user = auth.validate(self.token())
                if not user and path.startswith('/api/inbound/'):
                    # The Chrome extension signs in with a device token, good for /api/inbound/ only.
                    bearer = (self.headers.get('Authorization') or '').removeprefix('Bearer ').strip()
                    user = auth.validate_token(bearer)
                if not public and not user:
                    raise HttpError(401, 'Please sign in')
                if admin and not user['is_admin']:
                    raise HttpError(403, 'Admins only')
                # At most 18 digits: SQLite's ids are 64-bit (review 11-24: 20 nines gave OverflowError, 5000 digits
                # passed int()'s limit; both were 500s).
                if any(k in ID_PARAMS and not (v.isascii() and v.isdigit() and len(v) <= 18)
                       for k, v in match.groupdict().items()):
                    raise HttpError(400, 'That address has no item number in it. Reload the page and try again.')
                self.user = user
                self.query = {k: v[-1] for k, v in parse_qs(url.query).items()}
                # The extension's page posts are left out: store page data carries long digit ids pages.py reads as text.
                try:
                    big = method == 'POST' and not path.startswith('/api/inbound/') and too_big(self._json_peek(path))
                except RecursionError:
                    raise HttpError(400, 'Bad JSON')
                if big:
                    raise HttpError(400, 'One of the numbers sent is too big. Reload the page and try again.')
                t0 = time.monotonic()
                if method == 'GET' or path.startswith(UNLOCKED):
                    result = fn(self, **match.groupdict())
                else:
                    result = locked(self, path, fn, match.groupdict())
                ms = (time.monotonic() - t0) * 1000
                if ms > 1000:   # Alex 11-12 "Overview slow again": slow requests leave a line to read later
                    log.warning('slow %s %s %.0f ms (%s)', method, path, ms, (user or {}).get('username', '-'))
                if result is not None:
                    self.send_json(result)
                return
            raise HttpError(404, 'Not found')
        except HttpError as e:
            self.send_json({'error': str(e)}, e.status)
        except Conflict as e:
            self._refused(method, path, 409, str(e))
            self.send_json({'error': str(e), 'conflict': True}, 409)
        except (WmsError, AuthError, ConnectionError_) as e:
            self._refused(method, path, 400, str(e))
            self.send_json({'error': str(e)}, 400)
        except Exception:
            log.error('Unhandled error on %s %s\n%s', method, path, traceback.format_exc())
            self.send_json({'error': 'Server error. It has been logged.'}, 500)

    def _refused(self, method: str, path: str, status: int, message: str):
        """Alex 11-16: a refused save left no trace (refused.py). Never raises."""
        try:
            user = (getattr(self, 'user', None) or {}).get('username', '')
            refused.record(DATA, method, path, status, message, user, (self.headers.get('X-Device') or '').strip(),
                           getattr(self, '_body', None))
            log.info('refused %s %s %s (%s): %s', status, method, path, user or '-', message[:200])
        except Exception:  # noqa: BLE001 - a log line must never break the answer
            pass

    def serve_static(self, path: str):
        rel = os.path.normpath(path.lstrip('/')) if path not in ('', '/') else 'index.html'
        full = os.path.abspath(os.path.join(STATIC, rel))
        if not full.startswith(STATIC + os.sep) or not os.path.isfile(full):
            full = os.path.join(STATIC, 'index.html')  # SPA route
        if not os.path.isfile(full):
            return self.send_bytes(b'Boxloft web app is not built yet.', 'text/plain')
        with open(full, 'rb') as f:
            body = f.read()
        if os.path.basename(full) == 'index.html':
            body = body.replace(b'<head>', b'<head>' + config.client_script(), 1)  # window.__APP__ (config.py)
        ctype = mimetypes.guess_type(full)[0] or 'application/octet-stream'
        # Vite puts a content hash in every /assets/ filename, so those bytes never change.
        cache = 'public, max-age=31536000, immutable' if '/assets/' in full else 'no-cache'
        self.send_bytes(body, ctype, cache, {'X-Content-Type-Options': 'nosniff'})


def names(rows: list) -> list:
    users = auth.user_names()
    for r in rows:
        if 'user_id' in r:
            r['user_name'] = 'Boxloft (auto)' if r['user_id'] == 0 else users.get(r['user_id'], '?')
    return rows


# ---------------------------------------------------------------------- auth routes

@route('GET', '/api/health', public=True)
def health(h):
    return {'ok': True}


@route('POST', '/api/auth/login', public=True)
def login(h):
    data = h.json_body()
    user = auth.authenticate(data.get('username', ''), data.get('password', ''), h.ip)
    token = auth.create_session(user['id'], h.ip, h.headers.get('User-Agent', ''))
    h.send_json({'user': user}, headers={'Set-Cookie': h.session_cookie(token)})


@route('POST', '/api/auth/logout', public=True)
def logout(h):
    auth.logout(h.token())
    h.send_json({'ok': True}, headers={'Set-Cookie': h.session_cookie('', clear=True)})


@route('GET', '/api/auth/me')
def me(h):
    return {'user': h.user}


@route('GET', '/api/auth/setup', public=True)
def setup_info(h):
    user = auth.setup_info(h.query.get('token', ''))
    if not user:
        raise HttpError(404, 'This setup link has expired or was already used. Ask an admin for a new one.')
    return {'user': {'username': user['username'], 'display_name': user['display_name']}}


@route('POST', '/api/auth/setup', public=True)
def setup_complete(h):
    data = h.json_body()
    user = auth.complete_setup(data.get('token', ''), data.get('password', ''), h.ip)
    token = auth.create_session(user['id'], h.ip, h.headers.get('User-Agent', ''))
    h.send_json({'user': user}, headers={'Set-Cookie': h.session_cookie(token)})


@route('POST', '/api/auth/password')
def change_password(h):
    data = h.json_body()
    auth.change_password(h.user['id'], data.get('current', ''), data.get('new', ''), keep_token=h.token())
    return {'ok': True}


@route('GET', '/api/auth/sessions')
def sessions(h):
    return {'sessions': auth.list_sessions(h.user['id'], h.token())}


@route('POST', '/api/auth/sessions/logout-others')
def logout_others(h):
    return {'signed_out': auth.logout_others(h.user['id'], h.token())}


@route('GET', '/api/users', admin=True)
def users_list(h):
    return {'users': auth.list_users()}


@route('POST', '/api/users', admin=True)
def users_create(h):
    d = h.json_body()
    token = auth.create_user(d.get('username', ''), d.get('display_name', ''), d.get('role', 'staff'), d.get('email', ''))
    auth.log('user_created', h.user['id'], d.get('username', ''), h.ip)
    return {'setup_path': f'/setup?token={token}'}


@route('POST', '/api/users/<uid>/reset', admin=True)
def users_reset(h, uid):
    token = auth.reset_setup(int(uid))
    auth.log('user_reset', h.user['id'], uid, h.ip)
    return {'setup_path': f'/setup?token={token}'}


@route('POST', '/api/users/<uid>', admin=True)
def users_update(h, uid):
    d = h.json_body()
    uid = int(uid)
    if uid == h.user['id'] and (d.get('is_active') is False or d.get('role') == 'staff'):
        raise HttpError(400, 'You cannot remove your own admin access')
    if 'is_active' in d:
        auth.set_active(uid, bool(d['is_active']))
    if 'role' in d:
        auth.set_role(uid, d['role'])
    return {'ok': True}


# ---------------------------------------------------------------------- inventory routes

@route('GET', '/api/meta')
def meta(h):
    return {'identifier_kinds': IDENTIFIER_KINDS, 'remove_targets': REMOVE_TARGETS, 'owners': wms.list_owners()}


@route('GET', '/api/summary')
def summary(h):
    s = wms.summary()
    s['recent'] = names(wms.list_actions(limit=12))
    s['mine'] = names(wms.list_actions(limit=5, user_id=h.user['id']))   # staff Home: my last 5 saves, each with Undo
    s['open_orders'] = wms._q1("SELECT COUNT(*) n FROM sales_order WHERE status = 'open'")['n']
    s['purchases'] = commerce.list_purchases(status='open', limit=1)['totals']
    ships = commerce.list_ship_boxes()
    # Never counted (all 0): no "count ship boxes" line that never clears (flow audit 11-12).
    s['low_ship_boxes'] = [b for b in ships if b['stock'] <= 5] if any(b['stock'] > 0 for b in ships) else []
    arr = inbound.arriving()
    # One count with Arriving's headings (inbound.counts): packages, and the orders they belong to.
    k = arr['counts']
    s['arriving'] = {'counts': k,
                     'today': k.get('today', {}).get('orders', 0),
                     'open': len({o['id'] for o in arr['orders'] if o['state'] != 'delivered'}),
                     'delivered_not_in': k.get('delivered', {}).get('orders', 0),
                     'delivered_packages': k.get('delivered', {}).get('packages', 0),
                     'last_sync': arr['last_sync']['at'] if arr['last_sync'] else None}
    s['unaccounted'] = audit.unaccounted(inbound, arr)
    s['ebay'] = ebay_items.gaps() if connections.get('ebay').get('refresh_token') else None
    s['refused'] = refused.today(DATA)   # Alex 11-16: what people were refused today, so a bug shows the same day
    return s


@route('GET', '/api/scan')
def scan(h):
    raw = h.query.get('code', '')
    parsed = parse_scan(raw)
    kind = parsed['kind']
    try:
        if kind == 'box':
            return {'kind': 'box', 'box': wms._box_row(parsed['id'])}
        if kind == 'location':
            loc = wms.get_location(parsed['id'])
            return {'kind': 'location', 'location': {k: loc[k] for k in ('id', 'code', 'name', 'kind', 'scan_code')}}
        if kind == 'unit':
            return {'kind': 'unit', 'unit': wms.get_unit(parsed['id'])}
    except WmsError:
        return {'kind': 'unknown', 'code': parsed.get('code'), 'message': f'{parsed.get("code")} is not in the system'}
    if kind == 'bad_check':
        return {'kind': 'bad_check', 'code': parsed['code'], 'message': 'Misread: the check digit is wrong. Scan again.'}
    products = wms.lookup(parsed['value'])
    if products:
        return {'kind': 'product', 'value': parsed['value'], 'products': products}
    if labels.is_label(parsed['value']):   # a carrier label (Alex 11-16): the Package page logs it and shows what is inside
        return {'kind': 'label', 'value': parsed['value'],
                'message': 'That is a carrier label. Scan it from the header or a box page to see what is inside.'}
    # Not a product yet, but maybe bought: an open order line for it means "receive it", not "make a product".
    found = commerce.open_lines_for_code(parsed['value'])
    if not found and parsed['value'].isdigit():
        # Alex 11-11 (Tikki plush, UPC 043377506911): Amazon orders carry the ASIN, not the barcode, so a UPC found
        # its order only once someone had looked the code up. Ask the catalog here (cached forever; New product asks
        # the same right after, so no extra Keepa token), then look again by the ASINs it gives.
        try:
            hits = catalog.lookup(parsed['value'])
        except Exception:  # noqa: BLE001 - a lookup failure leaves the scan as it was
            log.exception('catalog lookup on scan failed for %s', parsed['value'])
            hits = []
        asins = sorted({(p.get('asin') or '').upper() for p in hits if p.get('asin')})
        products = list({p['id']: p for a in asins for p in wms.lookup(a)}.values())
        if products:
            return {'kind': 'product', 'value': parsed['value'], 'products': products}
        if asins:
            found = commerce.open_lines_for_code(parsed['value'], tuple(asins))
    lines = [{k: ln[k] for k in ('id', 'product_id', 'title', 'order_id', 'left_to_receive', 'package_left', 'why')}
             for ln in commerce.rank_for_receiving(found)]
    return {'kind': 'unknown', 'value': parsed['value'], 'message': f'No product with code {parsed["value"]}', 'lines': lines}


@route('GET', '/api/owners')
def owners(h):
    return {'owners': wms.list_owners()}


@route('POST', '/api/owners', admin=True)
def owner_create(h):
    d = h.json_body()
    return wms.create_owner(d.get('name', ''), bool(d.get('is_client', True)))


@route('GET', '/api/products')
def products(h):
    q = h.query
    rows = wms.list_products(search=q.get('q', '').strip(), owner_id=_int(q.get('owner_id')),
                             include_archived=q.get('archived') == '1', in_stock_only=q.get('in_stock') == '1')
    # Alex 11-12: which marketplaces each item is for sale on (one item can be on several), and a filter by it
    markets = listings.product_markets()
    for r in rows:
        r['markets'] = sorted(markets.get(r['id'], ()))
    want = q.get('market', '')
    if want == 'none':
        rows = [r for r in rows if not r['markets']]
    elif want:
        rows = [r for r in rows if want in r['markets']]
    return {'products': rows}


def _save_product(h, product_id=None):
    d = h.json_body()
    if product_id and (d.get('edited_at') or d.get('updated_at')):
        # Alex 11-11, two people: an edit made on a page that loaded an older copy would put the other person's
        # changes back. The page sends the edited_at it loaded (an older tab: updated_at); a newer edit refuses this
        # one. A stock move or photo since is not an edit (sweep A-u1).
        col = 'edited_at' if d.get('edited_at') else 'updated_at'
        cur = wms._q1(f'SELECT {col} AS seen, updated_at, updated_by FROM product WHERE id = ?', (product_id,))
        if cur and cur['seen'] != d.get(col):
            who = auth.user_names().get(cur['updated_by'], 'Someone') if cur['updated_by'] else 'Someone'
            raise Conflict(f"{who} saved this product at {_et_clock(cur['seen'])}, after your page loaded. "
                           "Nothing was saved: the page now shows their version. Make your change again.")
    p = wms.save_product(d, product_id, h.user['id'])
    with wms._tx() as c:
        c.execute('UPDATE product SET updated_by = ? WHERE id = ?', (h.user['id'], p['id']))
    with wms._tx() as c:   # an order line or listing waiting for this SKU or code links to it now
        commerce.relink(c)
    if not product_id and d.get('photo_token'):
        # Alex 11-11: the photo Sam took to fill in the form becomes the product's photo (before any catalog image).
        if photo_reader.attach(str(d['photo_token']), p['id'], h.user['id'], p['title']):
            p = wms.get_product(p['id'])
    if d.get('image_url') and not any(ph for ph in p['photos']):
        filled = p.get('cost_filled')
        catalog.save_image(p['id'], d['image_url'], h.user['id'])
        p = {**wms.get_product(p['id']), 'cost_filled': filled}
    return p


@route('POST', '/api/products')
def product_create(h):
    return _save_product(h)


@route('GET', '/api/products/<pid>')
def product_get(h, pid):
    p = wms.get_product(int(pid))
    p['sell_sku'] = listings.sell_sku(int(pid))   # Alex 11-24: one tap copies it for Seller Central
    names(p['history'])
    names(p['receipts'])
    return p


@route('POST', '/api/products/<pid>/sell-sku')
def product_sell_sku(h, pid):
    """Copy SKU (Alex 11-24): the item's Seller Central SKU, saved on the first copy so it never changes after."""
    return listings.save_sell_sku(int(pid), h.user['id'])


@route('POST', '/api/products/<pid>')
def product_update(h, pid):
    return _save_product(h, int(pid))


def _product_seen(h, product_id: int, seen):
    """The product page's updated_at: someone saved since, so this delete or archive was decided on old facts."""
    cur = wms._q1('SELECT updated_at, updated_by FROM product WHERE id = ?', (product_id,))
    if not cur:
        raise HttpError(404, 'Product not found')
    if seen and cur['updated_at'] != seen:
        who = auth.user_names().get(cur['updated_by'], 'Someone') if cur['updated_by'] else 'Someone'
        raise Conflict(f"{who} changed this product at {_et_clock(cur['updated_at'])}, after your page loaded. "
                       'Nothing was done: the page now shows their version. Look again, then decide.')


@route('GET', '/api/products/<pid>/links')
def product_links(h, pid):
    """Product page (Alex 11-12): its buys, sales and Stock audit rows, so every screen tells the same story."""
    out = commerce.product_links(int(pid))
    asins, line_ids = set(out['asins']), {b['id'] for b in out['buys']}
    keep = ('id', 'asin', 'match_asin', 'line_id', 'expected', 'accounted', 'closed', 'bought', 'amazon_sold',
            'returned_before', 'found', 'received', 'sold', 'sent_back')
    out['audit'] = [{k: r.get(k) for k in keep} for r in audit._all_rows()
                    if (r.get('match_asin') and r['match_asin'] in asins) or (r.get('line_id') in line_ids)]
    return out


@route('GET', '/api/products/<pid>/listed')
def product_listed(h, pid):
    """Receive form: where the product is listed, and what its market's boxes hold (listings.product_listed)."""
    out = listings.product_listed(int(pid))
    asins = approval.product_asins(int(pid))
    approval.ensure(asins)   # a GET: never under the lock
    out['amazon'] = amazon_note(asins)
    return out


@route('GET', '/api/products/<pid>/usage')
def product_usage(h, pid):
    return wms.product_usage(int(pid))


@route('POST', '/api/products/<pid>/delete')
def product_delete(h, pid):
    """Alex 11-11 "a way to delete a product": erased only when it never held stock; otherwise archive it."""
    _product_seen(h, int(pid), h.json_body().get('seen'))
    return wms.delete_product(int(pid), h.user['id'])


@route('POST', '/api/products/<pid>/archive')
def product_archive(h, pid):
    d = h.json_body()
    _product_seen(h, int(pid), d.get('seen'))
    return wms.archive_product(int(pid), h.user['id'], bool(d.get('archived', True)))


@route('GET', '/api/buy-match')
def buy_match(h):
    """Buys that look like a scanned item's catalog title (q + asin) or like a product with no order (product_id)."""
    q = h.query
    return photo_reader.buy_match(q.get('q', '')[:400], q.get('asin', '')[:20], _int(q.get('product_id'), 0))


@route('POST', '/api/products/<pid>/link-buy')
def product_link_buy(h, pid):
    """Alex 11-11: tie a product to the buy it came from (its ASIN, or a store line), and/or keep a scanned code."""
    d = h.json_body()
    # No relink here: Undo must take the link off cleanly. Listings match at read time, and the hourly refresh relinks.
    return photo_reader.link(int(pid), h.user['id'], str(d.get('kind') or ''), _int(d.get('id'), 0), str(d.get('code') or '')[:40])


@route('POST', '/api/products/<pid>/unlink-buy')
def product_unlink_buy(h, pid):
    """Undo of link-buy: takes off only what that link added."""
    d = h.json_body()
    added = d.get('added') if isinstance(d.get('added'), list) else []
    return photo_reader.unlink(int(pid), h.user['id'], added[:5], _int(d.get('line_id'), 0))


@route('GET', '/api/units/<uid>')
def unit_get(h, uid):
    return wms.get_unit(int(uid))


@route('POST', '/api/units/<uid>')
def unit_update(h, uid):
    d = h.json_body()
    wms.update_unit(int(uid), d.get('condition'), d.get('notes'))
    return wms.get_unit(int(uid))


@route('POST', '/api/photos')
def photo_upload(h):
    ctype = (h.headers.get('Content-Type') or '').split(';')[0].strip()
    ext = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp'}.get(ctype)
    if not ext:
        raise HttpError(415, 'Photos must be JPEG, PNG or WebP')
    body = h.body_bytes(MAX_PHOTO)
    magic_ok = (body[:3] == b'\xff\xd8\xff' if ext == 'jpg' else body[:8] == b'\x89PNG\r\n\x1a\n' if ext == 'png'
                else body[:4] == b'RIFF' and body[8:12] == b'WEBP')
    if not magic_ok:
        raise HttpError(415, 'That file is not a valid image')
    sub = datetime.now(ET).strftime('%Y-%m')
    rel = os.path.join(sub, f'{secrets.token_hex(12)}.{ext}')
    full = os.path.join(DATA, 'photos', rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'wb') as f:
        f.write(body)
    try:
        return wms.add_photo(rel, h.user['id'], _int(h.query.get('product_id')), _int(h.query.get('unit_id')))
    except WmsError:
        os.remove(full)
        raise


@route('GET', '/api/photos/<photo_id>')
def photo_get(h, photo_id):
    rel = wms.photo_path(_int(photo_id, 0))
    full = os.path.abspath(os.path.join(DATA, 'photos', rel or ''))
    if not rel or not full.startswith(os.path.join(DATA, 'photos') + os.sep) or not os.path.isfile(full):
        raise HttpError(404, 'Photo not found')
    with open(full, 'rb') as f:
        h.send_bytes(f.read(), mimetypes.guess_type(full)[0] or 'image/jpeg', 'private, max-age=86400')


@route('GET', '/api/track-photos/<track_id>')
def track_photo_get(h, track_id):
    # Amazon's delivery photo for one package (package_track.photo), copied when its Track page was read.
    r = wms._q1('SELECT photo FROM package_track WHERE id = ?', (_int(track_id, 0),)) or {}
    base = os.path.join(DATA, 'photos', 'track')
    full = os.path.abspath(os.path.join(DATA, 'photos', r.get('photo') or ''))
    if not r.get('photo') or not full.startswith(base + os.sep) or not os.path.isfile(full):
        raise HttpError(404, 'Photo not found')
    with open(full, 'rb') as f:
        h.send_bytes(f.read(), 'image/jpeg', 'private, max-age=86400')


@route('POST', '/api/photo-id')
def photo_identify(h):
    """New product, Alex 11-11: a phone photo (JPEG, shrunk on the phone) -> Gemini's name and brand, the buys it may
    be, and a token that makes the photo the product's photo on Create product. In UNLOCKED: Gemini takes 5-10 s."""
    if (h.headers.get('Content-Type') or '').split(';')[0].strip() != 'image/jpeg':
        raise HttpError(415, 'Send the photo as a JPEG')
    return photo_reader.identify(h.body_bytes(MAX_PHOTO), h.user['id'], ai.photo_provider()[1])


@route('GET', '/api/photo-id/<token>')
def photo_identify_get(h, token):
    """The photo New product took, before a product holds it (the form shows it after a match is tapped)."""
    rel = photo_reader.path_for(token)
    if not rel:
        raise HttpError(404, 'Photo not found')
    with open(os.path.join(DATA, 'photos', rel), 'rb') as f:
        h.send_bytes(f.read(), 'image/jpeg', 'private, max-age=86400')


@route('POST', '/api/photo-id/attach')
def photo_identify_attach(h):
    """Tapped a product that already exists and has no photo: the photo becomes its photo."""
    d = h.json_body()
    pid = _int(d.get('product_id'), 0)
    if wms._q1('SELECT 1 AS x FROM photo WHERE product_id = ? AND removed = 0', (pid,)):
        return {'attached': False}   # it has a photo already: keep it
    return {'attached': bool(photo_reader.attach(str(d.get('photo_token') or ''), pid, h.user['id']))}


@route('POST', '/api/photos/<photo_id>/remove')
def photo_remove(h, photo_id):
    wms.remove_photo(int(photo_id))
    return {'ok': True}


@route('GET', '/api/locations')
def locations(h):
    return {'locations': wms.list_locations(include_virtual=h.query.get('virtual') == '1')}


@route('POST', '/api/locations')
def locations_create(h):
    d = h.json_body()
    return {'created': wms.create_locations(d.get('codes') or [], d.get('kind') or 'shelf')}


@route('POST', '/api/places')
def places_create(h):
    d = h.json_body()
    return wms.create_place(d.get('site') or '', d.get('room') or '')


@route('GET', '/api/locations/<lid>')
def location_get(h, lid):
    return wms.get_location(int(lid))


@route('POST', '/api/locations/<lid>')
def location_update(h, lid):
    d = h.json_body()
    wms.rename_location(int(lid), d.get('code'), d.get('name'), d.get('archived'))
    return wms.get_location(int(lid))


@route('GET', '/api/boxes')
def boxes(h):
    q = h.query
    return {'boxes': wms.list_boxes(location_id=_int(q.get('location_id')), search=q.get('q', '').strip(),
                                    include_archived=q.get('archived') == '1')}


@route('POST', '/api/boxes')
def boxes_create(h):
    d = h.json_body()
    return {'boxes': wms.create_boxes(_int(d.get('count'), 1), h.user['id'], _int(d.get('location_id')),
                                      d.get('name') or '', d.get('kind') or 'box')}


@route('GET', '/api/boxes/<bid>')
def box_get(h, bid):
    b = wms.get_box(int(bid))
    names(b['history'])
    names(b['renames'])
    return b


@route('POST', '/api/boxes/<bid>')
def box_update(h, bid):
    d = h.json_body()
    wms.update_box(int(bid), d.get('name'), d.get('notes'), d.get('archived'), user_id=h.user['id'])
    b = wms.get_box(int(bid))
    names(b['history'])
    names(b['renames'])
    return b


@route('POST', '/api/receive')
def receive(h):
    d = h.json_body()
    if not _int(d.get('purchase_line_id')) and not d.get('over_ok'):
        ask = commerce.over_receive(_int(d.get('product_id')), _int(d.get('qty'), 0))
        if ask:
            return {'confirm': ask}   # nothing saved; the form shows it with "Receive anyway"
    return wms.receive(h.user['id'], _int(d.get('product_id')), _int(d.get('qty'), 0), d.get('unit_cost_cents'),
                       d.get('to') or {}, d.get('supplier', ''), d.get('reference', ''), d.get('note', ''), h.device,
                       d.get('unit_condition', ''), _int(d.get('purchase_line_id')), d.get('purchased_at') or None)


@route('POST', '/api/receive-lines')
def receive_lines(h):
    d = h.json_body()
    return commerce.receive_lines(h.user['id'], _int(d.get('product_id'), 0), d.get('to') or {}, d.get('lines') or [],
                                  h.device, str(d.get('unit_condition') or ''), str(d.get('note') or ''))


@route('POST', '/api/move')
def move(h):
    d = h.json_body()
    return wms.move(h.user['id'], d.get('lines') or [], d.get('from') or {}, d.get('to') or {}, d.get('note', ''), h.device)


@route('POST', '/api/move-all')
def move_all(h):
    d = h.json_body()
    return wms.move_all(h.user['id'], d.get('from') or {}, d.get('to') or {}, d.get('note', ''), h.device)


@route('POST', '/api/products/<pid>/gather')
def product_gather(h, pid):
    """Alex 11-13: every pile of one item into one box, from its product page. One action, one Undo."""
    d = h.json_body()
    return wms.gather(h.user['id'], int(pid), d.get('to') or {}, d.get('note', ''), h.device)


@route('GET', '/api/products/<pid>/merge-preview')
def product_merge_preview(h, pid):
    """Alex 11-17, merge items: what "Same item as" would do (units, the Stock audit row they count on)."""
    q = h.query
    return merges.preview(int(pid), {'product_id': _int(q.get('product_id')), 'audit_id': _int(q.get('audit_id'))})


@route('POST', '/api/products/<pid>/merge')
def product_merge(h, pid):
    """One save: this item's codes, open links and units become the target's; this item is archived."""
    d = h.json_body()
    t = d.get('target') or {}
    return merges.run(int(pid), {'product_id': _int(t.get('product_id')), 'audit_id': _int(t.get('audit_id'))},
                      h.user['id'], h.device, d.get('seen'))


@route('POST', '/api/merges/<mid>/undo')
def product_merge_undo(h, mid):
    return merges.undo(int(mid), h.user['id'], h.device)


@route('POST', '/api/move-boxes')
def move_boxes(h):
    d = h.json_body()
    return wms.move_boxes(h.user['id'], [int(x) for x in d.get('box_ids') or []], _int(d.get('location_id')),
                          d.get('note', ''), h.device)


@route('POST', '/api/remove')
def remove(h):
    d = h.json_body()
    if d.get('target') not in REMOVE_TARGETS:   # VENDOR goes through a return (Returns page), so it has a record
        raise WmsError('Pick where the stock went')
    return wms.remove(h.user['id'], d.get('from') or {}, d.get('lines') or [], d.get('target', ''), d.get('note', ''), h.device)


@route('POST', '/api/count')
def count(h):
    d = h.json_body()
    if isinstance(d.get('seen'), list):
        # Alex 11-11: the count screen showed these contents. If the other person moved stock in or out since,
        # this count would write their units off: refuse, say who, and the screen reloads the box.
        now = {(r['product_id'], r['unit_id'] or 0): r['qty'] for r in wms.holder_contents(d.get('holder') or {})}
        was = {(_int(r.get('product_id'), 0), _int(r.get('unit_id'), 0)): _int(r.get('qty'), 0) for r in d['seen'] if isinstance(r, dict)}
        if now != was:
            last = wms.last_holder_move(d.get('holder') or {})
            who = ''
            if last:
                name = 'Boxloft (auto)' if last['user_id'] == 0 else auth.user_names().get(last['user_id'], 'Someone')
                who = f"{name} moved stock in or out of it at {_et_clock(last['at'])}. "
            raise Conflict(f'This spot changed since your count started. {who}Nothing was saved: '
                           'the list now shows what is there. Count it again.')
    return wms.count(h.user['id'], d.get('holder') or {}, d.get('counted') or [], d.get('note', ''), h.device)


@route('POST', '/api/undo')
def undo(h):
    d = h.json_body()
    # An action that shipped an order line: undoing only the stock left the line "shipped" and the ship box gone
    # (red line 11-11). The audit card's own undo clears the line too.
    if wms._q1('SELECT 1 AS x FROM sales_line WHERE shipped_action_id = ?', (_int(d.get('action_id')),)):
        raise WmsError('This shipped an order line. If it was "Record as sold" on a Stock audit card, undo it there; '
                       'otherwise ask an admin.')
    mid = merges.of_action(_int(d.get('action_id')))
    if mid:   # one action of a merge: the whole merge goes back, as its own Undo does
        return {**merges.undo(mid, h.user['id'], h.device), 'merge_id': mid,
                'message': 'Undone: the whole merge of items. The two items are apart again.'}
    return wms.undo(h.user['id'], _int(d.get('action_id')), d.get('note', ''), h.device)


@route('GET', '/api/actions')
def actions(h):
    q = h.query
    return {'actions': names(wms.list_actions(limit=_int(q.get('limit'), 100), before_id=_int(q.get('before')),
                                              box_id=_int(q.get('box_id')), product_id=_int(q.get('product_id')),
                                              location_id=_int(q.get('location_id')), user_id=_int(q.get('user_id')),
                                              kind=q.get('kind') or None))}


@route('GET', '/api/actions/<aid>')
def action_get(h, aid):
    a = wms.get_action(int(aid))
    names([a])
    return a


@route('GET', '/api/integrity')
def integrity(h):
    return wms.last_integrity() or {}


@route('POST', '/api/integrity/run')
def integrity_run(h):
    return wms.check_integrity()


@route('GET', '/api/export/<which>')
def export(h, which):
    which = which.removesuffix('.csv')
    cols, rows = wms.export_rows(which)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction='ignore')
    w.writeheader()
    w.writerows(rows)
    stamp = datetime.now(ET).strftime('%Y-%m-%d')
    h.send_bytes(buf.getvalue().encode(), 'text/csv; charset=utf-8', 'no-store',
                 {'Content-Disposition': f'attachment; filename="boxloft-{which}-{stamp}.csv"'})


@route('GET', '/api/code/<kind>/<eid>')
def code_for(h, kind, eid):
    if kind not in ('box', 'location', 'unit'):
        raise HttpError(404, 'Unknown kind')
    return {'code': make_code(kind, int(eid))}


# ---------------------------------------------------------------------- catalog, purchases, orders, ship boxes

def _text_body(h) -> str:
    raw = h.body_bytes(MAX_CSV)
    for enc in ('utf-8-sig', 'cp1252'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise HttpError(400, 'Could not read that file as text')


@route('GET', '/api/catalog/lookup')
def catalog_lookup(h):
    code = h.query.get('code', '')
    return {'code': code, 'products': catalog.lookup(code)}


@route('GET', '/api/purchases')
def purchases(h):
    q = h.query
    return commerce.list_purchases(status=q.get('status', 'open'), q=q.get('q', '').strip(), limit=_int(q.get('limit'), 300))


@route('GET', '/api/purchases/open')
def purchases_open(h):
    return {'lines': commerce.receive_lines_for(_int(h.query.get('product_id'), 0))}


@route('POST', '/api/purchases/line/<lid>/product')
def purchase_line_product(h, lid):
    return audit.product_for_purchase_line(int(lid), h.user['id'])


@route('GET', '/api/purchases/line/<lid>')
def purchase_line(h, lid):
    return {**commerce.get_line(int(lid)), 'return': returns.for_line(int(lid))}


# ---------------------------------------------------------------- returns to the seller

@route('GET', '/api/returns')
def returns_list(h):
    return returns.list(status=h.query.get('status', 'open'), q=h.query.get('q', '').strip())


@route('POST', '/api/returns/import', admin=True)
def returns_import(h):
    return returns.import_export((h.json_body().get('files') or {}))


@route('GET', '/api/returns/for-product/<pid>')
def returns_for_product(h, pid):
    return {'lines': returns.lines_for_product(int(pid))}


@route('GET', '/api/returns/<rid>')
def returns_get(h, rid):
    return returns.get(int(rid))


@route('POST', '/api/returns')
def returns_create(h):
    d = h.json_body()
    return returns.create(_int(d.get('line_id'), 0), _int(d.get('qty'), 0), h.user['id'], d.get('state') or 'requested',
                          (d.get('reason') or '')[:120], (d.get('note') or '')[:500], _int(d.get('refund_cents')),
                          on_shelf=bool(d.get('on_shelf')))


@route('POST', '/api/returns/<rid>')
def returns_update(h, rid):
    d = h.json_body()
    return returns.update(int(rid), h.user['id'], state=d.get('state'), qty=_int(d.get('qty')),
                          refund_cents=_int(d.get('refund_cents')), reason=d.get('reason'), note=d.get('note'))


@route('POST', '/api/returns/<rid>/pull')
def returns_pull(h, rid):
    d = h.json_body()
    return returns.pull(int(rid), _int(d.get('qty'), 0), d.get('from') or {}, h.user['id'], h.device)


@route('POST', '/api/purchases/import')
def purchases_import(h):
    q = h.query
    return commerce.import_purchases(_text_body(h), q.get('source', ''), q.get('since') or None, q.get('mode', 'auto'))


@route('POST', '/api/purchases/close-before')
def purchases_close_before(h):
    return {'closed': commerce.close_before(h.json_body().get('date', ''))}


@route('POST', '/api/purchases')
def purchase_add(h):
    return commerce.add_purchase(h.json_body())


@route('POST', '/api/purchases/<lid>/cancel')
def purchase_cancel(h, lid):
    commerce.cancel_line(int(lid), bool(h.json_body().get('cancelled', True)))
    return {'ok': True}


@route('GET', '/api/ship-boxes')
def ship_boxes(h):
    return {'boxes': commerce.list_ship_boxes(include_archived=h.query.get('archived') == '1')}


@route('POST', '/api/ship-boxes')
def ship_box_create(h):
    return commerce.save_ship_box(h.json_body())


@route('POST', '/api/ship-boxes/<bid>')
def ship_box_update(h, bid):
    return commerce.save_ship_box(h.json_body(), int(bid))


@route('GET', '/api/supplies/incoming')
def supplies_incoming(h):
    return {'orders': commerce.supplies_incoming()}


@route('POST', '/api/supplies/<lid>/receive')
def supplies_receive(h, lid):
    return commerce.receive_supply(int(lid), (h.json_body() or {}).get('qty'))


def autolink_sales():
    """Open order lines that are surely an audited or bought ASIN get their product (audit.autolink_sales).
    Never fails the caller: a line it can't link keeps its buttons on Orders."""
    try:
        n = audit.autolink_sales()
        if n:
            log.info('autolink: %d order lines linked', n)
    except Exception:  # noqa: BLE001
        log.error('autolink\n%s', traceback.format_exc())


@route('GET', '/api/orders')
def orders(h):
    status = h.query.get('status', 'open')
    if status == 'open':
        autolink_sales()
    out = commerce.pick_list(status)
    return {'orders': audit.order_hints(out) if status == 'open' else out}


@route('POST', '/api/orders/import')
def orders_import(h):
    r = commerce.import_sales(_text_body(h), h.query.get('channel', ''))
    autolink_sales()
    return r


@route('POST', '/api/orders')
def order_add(h):
    return commerce.add_sale(h.json_body())


@route('POST', '/api/orders/<oid>/status')
def order_status(h, oid):
    commerce.set_order_status(int(oid), h.json_body().get('status', ''))
    return {'ok': True}


@route('POST', '/api/orders/lines/<lid>/link')
def order_line_link(h, lid):
    d = h.json_body()
    commerce.link_line(int(lid), _int(d.get('product_id'), 0), bool(d.get('remember', True)))
    return {'ok': True}


@route('POST', '/api/orders/lines/<lid>/make-product')
def order_line_make_product(h, lid):
    return audit.product_for_sale_line(int(lid), h.user['id'])


@route('POST', '/api/orders/lines/<lid>/ship')
def order_line_ship(h, lid):
    d = h.json_body()
    return commerce.ship_line(h.user['id'], int(lid), d.get('from') or {}, _int(d.get('unit_id'), 0),
                              _int(d.get('ship_box_id')), d.get('box_fit', ''), h.device)


@route('POST', '/api/orders/lines/<lid>/ship-unscanned')
def order_line_ship_unscanned(h, lid):
    # Flow audit 11-12 (redesign 3): 0 on the shelves, the item never scanned in. One save, no box asked.
    # outside (Alex 11-17): a person says it sold before it came in; bought_on (order #) and cost_cents optional.
    d = h.json_body()
    return audit.ship_unscanned(int(lid), h.user['id'], h.device, _int(d.get('ship_box_id')),
                                outside=bool(d.get('outside')), cost_cents=d.get('cost_cents'),
                                bought_on=str(d.get('bought_on') or ''))


@route('POST', '/api/orders/lines/<lid>/unship-unscanned')
def order_line_unship_unscanned(h, lid):
    return audit.undo_unscanned(int(lid), h.user['id'], h.device)


@route('GET', '/api/ship-confirm')
def ship_confirm_list(h):
    # Orders Boxloft scanned out by itself (shipped in Seller Central): which box did they go out in?
    return {'lines': shipboxes.pending(), 'boxes': commerce.list_ship_boxes()}


@route('POST', '/api/ship-confirm/<lid>')
def ship_confirm(h, lid):
    d = h.json_body()
    return shipboxes.confirm(h.user['id'], int(lid), _int(d.get('ship_box_id')), str(d.get('other') or '')[:40],
                             bool(d.get('own')))


@route('GET', '/api/inbound/known')
def inbound_known(h):
    return inbound.known()


@route('POST', '/api/inbound/sync')
def inbound_sync(h):
    d = h.json_body()
    return inbound.sync(d.get('orders') or [], (d.get('device') or h.device)[:60])


@route('GET', '/api/inbound/arriving')
def inbound_arriving(h):
    r = inbound.arriving(include_hidden=h.query.get('hidden') == '1')
    seen = labels.last_scans({o['id'] for o in r.get('orders') or []})   # Alex 11-16: its box label was scanned here
    who = auth.user_names() if seen else {}
    for o in r.get('orders') or []:
        if o['id'] in seen:
            o['label_scanned'] = {'at': seen[o['id']]['at'], 'by': who.get(seen[o['id']]['user_id'], '')}
    return r


# Carrier labels (Alex 11-16, labels.py): scan the label on a box that came in, see what is inside, put it away.

@route('POST', '/api/labels/scan')
def labels_scan(h):
    d = h.json_body()
    r = labels.scan(str(d.get('code') or ''), h.user['id'], h.device)
    return _label_names(r) if r else {'kind': 'not_label'}


@route('GET', '/api/labels/card')
def labels_card(h):
    """Read only: the card again after a put (the scan itself was logged by /api/labels/scan or /api/put)."""
    t = h.query.get('tracking_id', '')[:60]
    return _label_names(labels.card(t) if t else (labels.match(h.query.get('code', '')[:200]) or {'kind': 'not_label'}))


@route('POST', '/api/labels/pick')
def labels_pick(h):
    d = h.json_body()
    return _label_names(labels.pick(str(d.get('code') or ''), str(d.get('tracking_id') or ''), h.user['id'], h.device))


@route('POST', '/api/labels/put')
def labels_put(h):
    """One item of the package into the box: a receipt on that order line (sorting.add, kind order), a box page line
    with its own Undo and count."""
    d = h.json_body()
    line = commerce.get_line(_int(d.get('line_id'), 0))
    opt = {'kind': 'order', 'line_id': line['id'], 'left': line['left_to_receive'], 'label': 'order', 'sub': ''}
    r = sorting.add(_to(d), opt, _int(d.get('qty'), 1) or 1, h.user['id'], h.device, str(d.get('scan_id') or ''), '')
    if d.get('tracking_id') and not r.get('repeat'):
        labels.record_put(r['add_id'], str(d['tracking_id']))
    return sorting._put_result(r, opt, r['qty'], [])


def _label_names(r: dict) -> dict:
    """user ids -> names on a label card (and on each choice of an ambiguous one)."""
    who = auth.user_names()
    for c in [r] + list(r.get('choices') or []):
        for s in c.get('scanned_before') or []:
            s['by'] = who.get(s.pop('user_id', None), '')
    for i in r.get('items') or []:   # a buyer return: who took units back in
        for t in i.get('taken') or []:
            t['by'] = who.get(t.pop('user_id', None), '')
    return r


# Buyer returns (Alex 11-16, buyer_returns.py): the return label's card puts the units back in a box or out as damaged.

@route('POST', '/api/buyer-returns/<rid>/take')
def buyer_return_take(h, rid):
    d = h.json_body()
    to = _to(d) if (d.get('box_id') or d.get('location_id')) else None
    return _label_names(buyer_returns.take(int(rid), h.user['id'], _int(d.get('qty'), 1) or 1, str(d.get('condition') or ''),
                                           to, _int(d.get('seen_left')) if d.get('seen_left') is not None else None, h.device))


@route('POST', '/api/inbound/<po>/hide')
def inbound_hide(h, po):
    d = h.json_body()
    inbound.hide(int(po), bool(d.get('hidden', True)), bool(d.get('remember', True)))
    return {'ok': True}


@route('GET', '/api/inbound/walmart/wanted')
def inbound_walmart_wanted(h):
    return store_orders.walmart_wanted()


@route('POST', '/api/inbound/walmart')
def inbound_walmart(h):
    d = h.json_body() or {}
    try:
        return store_orders.walmart_pages(d.get('orders') or [])
    except StoreMailError as e:
        raise HttpError(400, str(e))


@route('POST', '/api/inbound/v2/step')
def inbound_v2_step(h):
    # Extension 2.0: the pages it just read in, the next pages to read out (backend/pagesync.py).
    d = h.json_body(MAX_PAGES_JSON)
    if d.get('state') is None:   # a new run: Alex 11-11, every 2 hours, none 10 PM to 6 AM ET (pagesync.not_now)
        why = pagesync.not_now()
        if why:
            return {'fetch': [], 'state': None, 'done': True, 'summary': {'ok': True, 'skipped': why, 'step': 'done'}}
    try:
        return pagesync.step(d, (d.get('device') or h.device or '')[:60])
    except (PageSyncError, StoreMailError, WmsError) as e:
        raise HttpError(400, str(e))


@route('GET', '/api/store-orders/review')
def store_orders_review(h):
    return {'orders': store_orders.to_review()}


@route('GET', '/api/store-orders/skipped')
def store_orders_skipped(h):
    return {'stores': store_orders.skipped_stores()}


@route('POST', '/api/store-orders/skip')
def store_orders_skip(h):
    d = h.json_body() or {}
    try:
        return store_orders.skip_store(str(d.get('store') or ''), bool(d.get('skip', True)))
    except StoreMailError as e:
        raise HttpError(400, str(e))


@route('POST', '/api/store-orders/<po>/approve')
def store_orders_approve(h, po):
    d = h.json_body() or {}
    try:
        store_orders.approve(int(po), d.get('prices') or {})
    except StoreMailError as e:
        raise HttpError(400, str(e))
    return {'ok': True}


@route('GET', '/api/inbound/<po>/history')
def inbound_history(h, po):
    return {'events': inbound.history(int(po))}


# ---------------------------------------------------------------------- audit sorting (scan items into boxes)

def _to(d: dict, prefix: str = '') -> dict:
    """The box ({'box_id'}) or shelf ({'location_id'}) a request names, or {} for none."""
    return sorting.holder(_int(d.get(prefix + 'box_id')), _int(d.get(prefix + 'location_id')))


@route('POST', '/api/audit-sort/boxes')
def audit_sort_boxes(h):
    d = h.json_body()
    return sorting.boxes(str(d.get('ranges') or ''), bool(d.get('create')), h.user['id'])


@route('POST', '/api/audit-sort/box/<bid>/retire')
def audit_sort_retire(h, bid):
    d = h.json_body()
    return sorting.retire(int(bid), h.user['id'], str(d.get('move_to') or ''), h.device)


@route('POST', '/api/targets/find')
def target_find(h):
    return sorting.find(str(h.json_body().get('name') or ''))


@route('POST', '/api/targets/make-box')
def target_make_box(h):
    d = h.json_body()
    return sorting.make_box(str(d.get('name') or ''), h.user['id'], _int(d.get('location_id')))


@route('POST', '/api/targets/make-shelf')
def target_make_shelf(h):
    d = h.json_body()
    return sorting.make_shelf(str(d.get('name') or ''), str(d.get('site') or ''))


@route('GET', '/api/markets/<letter>/boxes')
def market_boxes(h, letter):
    return sorting.market_boxes(letter)


@route('POST', '/api/markets/<letter>/next-box')
def market_next_box(h, letter):
    return sorting.next_box(letter, h.user['id'])


@route('GET', '/api/audit-sort/target')
def audit_sort_target(h):
    return sorting.target(_to(h.query))


@route('POST', '/api/audit-sort/scan')
def audit_sort_scan(h):
    d = h.json_body()
    return _with_amazon(sorting.scan(str(d.get('code') or ''), _to(d), h.user['id'], h.device, str(d.get('scan_id') or '')), d, _to(d))


@route('POST', '/api/audit-sort/found')
def audit_sort_found(h):
    d = h.json_body()
    return sorting.found(_int(d.get('item_id'), 0), _to(d), h.user['id'], h.device, str(d.get('scan_id') or ''),
                         code=str(d.get('code') or ''), qty=_int(d.get('qty'), 1) or 1)


@route('POST', '/api/audit-sort/join')
def audit_sort_join(h):
    d = h.json_body()
    return sorting.join(_int(d.get('product_id'), 0), _to(d, 'from_'), _to(d, 'to_'), _int(d.get('qty'), 1) or 1,
                        h.user['id'], h.device)


@route('GET', '/api/audit-sort/search')
def audit_sort_search(h):
    return {'rows': sorting.search(h.query.get('q', '')[:120])}


@route('POST', '/api/audit-sort/extra')
def audit_sort_extra(h):
    d = h.json_body()
    return _with_amazon(sorting.extra(str(d.get('code') or ''), _to(d), h.user['id'], h.device, str(d.get('scan_id') or ''),
                                      _int(d.get('cost_cents')), str(d.get('title') or '')[:200], _int(d.get('item_id'))), d, _to(d))


@route('GET', '/api/audit-sort/extras')
def audit_sort_extras(h):
    out = sorting.extras()
    names(out['rows'])
    return out


@route('POST', '/api/audit-sort/extras/<eid>/undo')
def audit_sort_extra_undo(h, eid):
    return sorting.undo_extra(int(eid), h.user['id'], h.device)


# Add items on a box's page (Alex 11-11 1:10 PM): scan or search, pick what it is, how many, Add.

@route('GET', '/api/box-add/check')
def box_add_check(h):
    q = h.query
    return sorting.add_check(_to(q), q.get('code', '')[:120], _int(q.get('product_id')), _int(q.get('item_id')))


@route('GET', '/api/box-add/search')
def box_add_search(h):
    return sorting.add_search(h.query.get('q', ''))


@route('POST', '/api/box-add')
def box_add(h):
    d = h.json_body()
    return sorting.add(_to(d), d.get('option') or {}, _int(d.get('qty'), 0), h.user['id'], h.device,
                       str(d.get('scan_id') or ''), str(d.get('code') or ''), _int(d.get('cost_cents')))


# Put away (flow audit 11-12, redesign 1): one scan = one save, the server picks the record (sorting.put).

@route('POST', '/api/put')
def put_away(h):
    d = h.json_body()
    r = sorting.put(_to(d), h.user['id'], h.device, str(d.get('scan_id') or ''), str(d.get('code') or '')[:120],
                    _int(d.get('product_id')), _int(d.get('item_id')), _int(d.get('qty'), 1) or 1,
                    d.get('option') if isinstance(d.get('option'), dict) else None, bool(d.get('over_ok')),
                    _int(d.get('replaces')), fresh=bool(d.get('fresh')))
    if str(r.get('kind', '')).startswith('label'):   # a carrier label (Alex 11-16): what is inside
        return _label_names(r)
    r = sorting.attach_photo(r, str(d.get('photo_token') or ''), h.user['id'])   # a pick from the Add card's photo
    return _with_amazon(r, d, _to(d))


# A new item is a card (flow audit 11-12, redesign 2): the card, then one save makes the item and puts 1 in the box.

@route('GET', '/api/put/new-card')
def put_new_card(h):
    r = sorting.new_card(h.query.get('code', '')[:120], h.query.get('title', '')[:200])
    asins = [r['asin']] if r.get('asin') else []
    asins += [li.get('asin') for li in r.get('listings') or [] if li.get('asin')]
    approval.ensure(asins)   # a GET: never under the lock
    if asins:
        r['amazon'] = amazon_note(asins, _to(h.query))
    return r


@route('POST', '/api/put/new')
def put_new(h):
    d = h.json_body()
    r = sorting.put_new(_to(d), h.user['id'], h.device, str(d.get('scan_id') or ''), str(d.get('code') or '')[:120],
                        str(d.get('title') or '')[:200], _int(d.get('line_id')), _int(d.get('listing_id')),
                        _int(d.get('product_id')))
    # Alex 11-17, an item with no barcode: the Add card's photo becomes the new item's photo.
    return _with_amazon(sorting.attach_photo(r, str(d.get('photo_token') or ''), h.user['id']), d, _to(d))


@route('POST', '/api/put/<aid>/qty')
def put_away_qty(h, aid):
    d = h.json_body()
    return sorting.put_qty(int(aid), _int(d.get('qty'), 0), h.user['id'], h.device, bool(d.get('over_ok')))


@route('POST', '/api/put/<aid>/cost')
def put_away_cost(h, aid):
    """Alex 11-17: a buy cost for a box page line that went in as extra stock. seen = {cost_cents} the line showed."""
    d = h.json_body()
    seen = d.get('seen')
    if isinstance(seen, dict) and 'cost_cents' in seen:
        cur = sorting.line_cost(int(aid))
        if _norm(cur['cost_cents']) != _norm(seen['cost_cents']):
            who = 'Boxloft (auto)' if cur['who'] == 0 else auth.user_names().get(cur['who'], 'Someone')
            now = 'not entered' if cur['cost_cents'] is None else f"${cur['cost_cents'] / 100:,.2f}"
            raise Conflict(f'{who} changed this cost to {now} after your screen loaded. Nothing was saved.')
    return sorting.set_cost(int(aid), d.get('cost_cents'), h.user['id'])


@route('GET', '/api/put/look')
def put_away_look(h):
    q = {'code': h.query.get('code', '')[:120], 'product_id': h.query.get('product_id')}
    r = sorting.look(q['code'], _int(q['product_id']))
    if isinstance(r, dict) and r.get('kind') == 'look':
        approval.ensure(approval.product_asins((r.get('product') or {}).get('id')) or _scan_asins(q))
    r = _with_amazon(r, q)
    if isinstance(r, dict) and (r.get('amazon') or {}).get('can_sell') is False:
        # Amazon said no (Alex 11-13): an A box is never the box offered first; the eBay box Amazon's no names is.
        from sorting import market_of
        boxes = [b for b in r.get('boxes') or [] if market_of(b['code']) != 'A']
        e = r['amazon'].get('other_box')
        if e and all(b['box_id'] != e['id'] for b in boxes):
            boxes.append({'box_id': e['id'], 'code': e['code'], 'qty': None, 'why': "Amazon won't take it: last filled eBay box"})
        r['boxes'] = boxes[:3]
        r['letter'] = 'E' if r.get('letter') in (None, 'A') else r['letter']
    return r


@route('POST', '/api/box-add/<aid>/undo')
def box_add_undo(h, aid):
    return sorting.undo_add(int(aid), h.user['id'], h.device)


# ---------------------------------------------------------------------- label print queue (Alex 11-11)
# Phones send labels here; the computer with the printer prints them (printq.py).

def _job_names(jobs: list) -> list:
    users = auth.user_names()
    for j in jobs:
        j['user_name'] = users.get(j['user_id'], '?')
        j['printed_by_name'] = users.get(j['printed_by']) if j.get('printed_by') else None
    return jobs


CLIENT_DIAG_MAX = 1024 * 1024   # bytes; the probe stops writing past this


@route('POST', '/api/client-diag')
def client_diag(h):
    """Alex 11-12: the home-screen web app reports its viewport and the taps at the top of the screen
    (web/src/lib/tapProbe.ts), because no iPhone can run here. One JSON line per report, 1 MB at most. No lock:
    it writes nothing in the databases."""
    d = h.json_body(limit=64 * 1024)
    path = os.path.join(DATA, 'client-diag.jsonl')
    try:
        if os.path.getsize(path) >= CLIENT_DIAG_MAX:
            return {'ok': False, 'full': True}
    except OSError:
        pass
    line = json.dumps({'at': now_utc(), 'user': h.user['username'], 'device': (h.headers.get('X-Device') or '').strip(),
                       **{k: v for k, v in d.items() if k not in ('at', 'user', 'device')}})
    with _diag_lock, open(path, 'a') as f:
        f.write(line[:16000] + '\n')
    return {'ok': True}


_diag_lock = threading.Lock()


@route('GET', '/api/print-jobs')
def print_jobs(h):
    out = printq.list()
    _job_names(out['waiting'] + out['done'])
    return out


@route('POST', '/api/print-jobs')
def print_job_add(h):
    d = h.json_body(limit=2 * 1024 * 1024)
    return _job_names([printq.add(h.user['id'], d.get('labels'), (h.headers.get('X-Device') or '').strip(), d.get('title') or '')])[0]


@route('POST', '/api/print-jobs/claim')
def print_jobs_claim(h):
    return {'jobs': _job_names(printq.claim(h.json_body().get('ids'), h.user['id'], (h.headers.get('X-Device') or '').strip()))}


@route('POST', '/api/print-jobs/<jid>/remove')
def print_job_remove(h, jid):
    return printq.remove(int(jid), h.user['id'])


@route('POST', '/api/print-jobs/<jid>/back')
def print_job_back(h, jid):
    return printq.back(int(jid))


@route('GET', '/api/changes')
def changes(h):
    """A token that changes whenever anyone saves stock moves, audit counts or outcomes, listing marks, products,
    purchases or returns, plus who made the newest stock move. The web app checks it every 10 s (LiveSync) and
    reloads when someone else changed something."""
    parts = []
    for table, col in (('action', 'id'), ('audit_count', 'id'), ('audit_outcome', 'id'), ('audit_extra', 'id'),
                       ('audit_adjust', 'id'), ('listing_mark', 'id'), ('product', 'updated_at'),
                       ('purchase_event', 'id'), ('return_event', 'id'), ('sales_line', 'shipped_action_id'),
                       ('sales_line', 'box_confirmed_at')):
        try:
            parts.append(str(wms._q1(f'SELECT MAX({col}) AS m FROM {table}')['m'] or 0))
        except Exception:  # noqa: BLE001 - a table a module hasn't made yet
            parts.append('-')
    last = wms._q1('SELECT user_id FROM action ORDER BY id DESC LIMIT 1')
    return {'token': '.'.join(parts), 'by': last['user_id'] if last else None, 'build': web_build()}


_build = {'mtime': None, 'name': ''}


def web_build() -> str:
    """The app's main script name in the index.html served now (index-<hash>.js). A phone that loaded an older one
    reloads (LiveSync). Alex 11-12: a home-screen app kept the old Stock audit after a fix and flooded the server."""
    try:
        p = os.path.join(STATIC, 'index.html')
        m = os.path.getmtime(p)
        if m != _build['mtime']:
            hit = re.search(r'/assets/(index-[\w-]+\.js)', open(p, encoding='utf-8').read())
            _build.update(mtime=m, name=hit.group(1) if hit else '')
        return _build['name']
    except OSError:
        return ''


@route('GET', '/api/audit')
def audit_list(h):
    out = audit.list()
    out['summary'].update(listings.annotate(out['items']))
    audit.open_sales(out['items'])
    return out


@route('GET', '/api/audit/outcomes')
def audit_outcomes(h):
    return audit.outcomes()


@route('GET', '/api/audit/history')
def audit_histories(h):
    """Saves for the cards the Stock audit page shows, in one request (ids=1,2,3; 200 at most)."""
    ids = [int(x) for x in h.query.get('ids', '').split(',') if x.strip().isdigit()][:200]
    return {'histories': audit.histories(ids)}


@route('POST', '/api/audit/<iid>/sold')
def audit_sold(h, iid):
    d = h.json_body()
    audit.check_seen(int(iid), d.get('seen'), auth.user_names())
    return audit.sold(int(iid), _int(d.get('qty'), 0), d.get('channel') or '', h.user['id'], _int(d.get('price_cents')),
                      d.get('sold_on') or '', d.get('order_ref') or '', (d.get('note') or '')[:300], h.device,
                      _int(d.get('sale_line_id')))


@route('POST', '/api/audit/<iid>/sent-back')
def audit_sent_back(h, iid):
    d = h.json_body()
    audit.check_seen(int(iid), d.get('seen'), auth.user_names())
    return audit.sent_back(int(iid), _int(d.get('qty'), 0), h.user['id'], d.get('state') or 'requested',
                           (d.get('reason') or '')[:120], _int(d.get('refund_cents')), (d.get('note') or '')[:300])


@route('POST', '/api/audit/<iid>/undo-outcome/<oid>')
def audit_undo_outcome(h, iid, oid):
    return audit.undo_outcome(int(iid), int(oid), h.user['id'], h.device)


@route('GET', '/api/audit/<iid>')
def audit_item(h, iid):
    it = audit.item(int(iid))
    listings.annotate([it])
    audit.open_sales([it])
    return it


def _check_marks(h, iid: int):
    """Listing decisions: the card sends `seen_mark_seq`, the newest decision on the row when it loaded
    (listings.annotate). If someone confirmed, rejected, marked or undid a match on this row since, this decision is
    refused with who did it. Old clients that send nothing are not checked."""
    seen = h.json_body().get('seen_mark_seq')
    if seen is None:
        return
    last = wms._q1('SELECT id, user_id, at FROM listing_mark WHERE item_id = ? ORDER BY id DESC LIMIT 1', (iid,))
    if (last['id'] if last else 0) != _int(seen, 0):
        name = 'Boxloft (auto)' if last['user_id'] == 0 else auth.user_names().get(last['user_id'], 'Someone')
        raise Conflict(f"This item changed since your screen loaded. {name} changed its listing match at "
                       f"{_et_clock(last['at'])}. Nothing was saved: check the listing and choose again.")


@route('POST', '/api/audit/<iid>/listing')
def audit_listing_mark(h, iid):
    body = h.json_body()
    _check_marks(h, int(iid))
    # A store row matched to a listing: its Amazon sales come off its count at once (line_presold reads the mark).
    return listings.mark(int(iid), body, h.user['id'])


@route('POST', '/api/audit/<iid>/listing/<mid>/undo')
def audit_listing_undo(h, iid, mid):
    _check_marks(h, int(iid))
    return listings.undo_mark(int(iid), int(mid), h.user['id'])


# ---------------------------------------------------------------------- listings (what is for sale now)

@route('GET', '/api/listings')
def listings_list(h):
    out = listings.list(include_gone=h.query.get('gone') == '1')
    out['sync'] = {**(out['sync'] or {}), **(sync_progress.get('amazon_listings') or {})}
    out['syncs'] = {ch: {**(st or {}), **(sync_progress.get(f'{ch}_listings') or {}), 'connected': _listing_ready(ch)}
                    for ch, st in out['syncs'].items()}
    return out


@route('GET', '/api/listings/stock')
def listings_stock(h):
    """Stock in a market's boxes against that market's listings: not listed, inactive, no ASIN, listed short."""
    out = listings.stock_vs_listed(h.query.get('channel') or 'amazon')
    if out['channel'] == 'amazon':   # Alex 11-13: flag what sits in A boxes that Amazon won't let us sell
        bad = {'products': 0, 'units': 0}
        for r in out['rows']:
            r['approval'] = approval.note([r['asin']]) if r['asin'] else None
            r['sell_sku'] = listings.sell_sku(r['product_id'])
            if r['approval'] and r['approval']['can_sell'] is False:
                bad['products'] += 1
                bad['units'] += r['on_hand']
        out['summary']['not_approved'] = bad
    return out


@route('POST', '/api/amazon/approval/recheck')
def amazon_recheck(h):
    """Check again (Alex 11-24): ask Amazon now whether we may sell these ASINs. Under /api/amazon/, so outside the
    lock (it waits on Amazon). No `asins`: every A-box item on the "Amazon won't let us sell" list."""
    asins = h.json_body().get('asins')
    if asins is None:
        asins = [r['asin'] for r in listings.stock_vs_listed('amazon')['rows']
                 if r['asin'] and approval.note([r['asin']])['can_sell'] is False]
    elif not isinstance(asins, list):   # review 11-24: one ASIN as a string once rechecked the whole list
        raise WmsError('asins must be a list of ASINs.')
    return approval.recheck([str(a)[:20] for a in asins[:RECHECK_INPUT_MAX]], str(h.json_body().get('run') or '')[:40])


@route('GET', '/api/amazon/approval/recheck')
def amazon_recheck_state(h):
    """The newest Check again: {run, running, done, total, result | error}. A phone that lost the POST's connection
    reads its answer here by its `run` id (Alex 11-24, "Load failed" while the check had worked)."""
    return approval.run_state(str(h.query.get('run') or '')[:40])


# eBay listings become items (Alex 11-12). Not under /api/ebay/: that prefix is UNLOCKED (consent), these save.

def _ebay_fresh():
    """Opening the box page's eBay strip reads eBay again when the last read is 2+ minutes old (in the background)."""
    if _listing_ready('ebay') and listings.due(2, 'ebay') and not (sync_progress.get('ebay_listings') or {}).get('running'):
        in_thread(run_listings, 'ebay')


@route('GET', '/api/ebay-items/to-box')
def ebay_to_box(h):
    _ebay_fresh()
    out = ebay_items.to_box(h.query.get('q', '')[:120])
    out['reading'] = bool((sync_progress.get('ebay_listings') or {}).get('running'))
    return out


@route('GET', '/api/ebay-items/to-match')
def ebay_to_match(h):
    return ebay_items.to_match()


@route('POST', '/api/ebay-items/<lid>/match')
def ebay_match(h, lid):
    d = h.json_body()
    out = ebay_items.match(int(lid), h.user['id'], _int(d.get('product_id')), _int(d.get('item_id')), bool(d.get('new')),
                           auth.user_names(), _int(d.get('line_id')), _int(d.get('listing_id')))
    autolink_sales()
    return out


@route('POST', '/api/ebay-items/<lid>/put')
def ebay_put(h, lid):
    d = h.json_body()
    return ebay_items.put(_to(d), int(lid), _int(d.get('qty'), 0) or 0, h.user['id'], h.device, str(d.get('scan_id') or ''),
                          _int(d.get('seen_short')), auth.user_names(), bool(d.get('over_ok')))


@route('POST', '/api/listings/link')
def listings_link(h):
    """Find its listing: a person ties a listing to a product that has no ASIN. `seen` = {updated_at, asin} of the
    product as the screen showed it; a newer save, or a listing someone else already linked, is refused with 409."""
    d = h.json_body()
    out = listings.link(_int(d.get('product_id')) or 0, _int(d.get('listing_id')) or 0, h.user['id'], d.get('seen'),
                        auth.user_names())
    autolink_sales()   # an open order of that ASIN can find the product now
    return out


@route('POST', '/api/listings/link/<kid>/undo')
def listings_unlink(h, kid):
    return listings.unlink(int(kid), h.user['id'])


LISTING_CHANNELS = ('amazon', 'ebay')


def _listing_ready(ch: str) -> bool:
    """A channel's listings can be read: its last connection test passed (eBay: and the seller approved)."""
    cur = connections.get(ch)
    return bool((cur.get('last_test') or {}).get('ok')) and (ch != 'ebay' or bool(cur.get('refresh_token')))


def run_listings(channel: str = 'amazon'):
    """One listings refresh of a channel to completion, with live progress. One at a time per channel."""
    key = f'{channel}_listings'
    with sync_lock:
        if (sync_progress.get(key) or {}).get('running'):
            return False
        sync_progress[key] = {'running': True, 'text': 'starting'}

    def progress(text):
        sync_progress[key] = {'running': True, 'text': text}
    try:
        log.info('%s listings refresh: %s', channel, listings.run(progress, channel))
        if channel == 'ebay':   # a new listing with no title guess gets its item now (Alex 11-12)
            log.info('ebay items: %s', ebay_items.make_missing())
        autolink_sales()   # a new listing can name the ASIN an open order's SKU stands for
    except Exception as e:  # noqa: BLE001 - stored in sync_state and shown on the page
        log.warning('%s listings refresh failed: %s', channel, e)
    finally:
        sync_progress[key] = {'running': False, 'text': ''}
    return True


@route('POST', '/api/listings/refresh')
def listings_refresh(h):
    ch = (h.query.get('channel') or (h.json_body() or {}).get('channel') or 'amazon').lower()
    if ch not in LISTING_CHANNELS:
        raise HttpError(400, 'Boxloft reads Amazon and eBay listings')
    if not _listing_ready(ch):
        raise HttpError(400, f'Connect {"eBay" if ch == "ebay" else "Amazon"} first (Settings, Connections)')
    in_thread(run_listings, ch)
    return {'started': True}


@route('POST', '/api/audit/<iid>/found')
def audit_found(h, iid):
    d = h.json_body()
    audit.check_seen(int(iid), d.get('seen'), auth.user_names())
    return audit.found(int(iid), _int(d.get('qty'), 0), d.get('to') or {}, h.user['id'], h.device, (d.get('note') or '')[:300])


@route('POST', '/api/audit/<iid>/close')
def audit_close(h, iid):
    d = h.json_body()
    audit.check_seen(int(iid), d.get('seen'), auth.user_names())
    return audit.close(int(iid), d.get('reason') or '', h.user['id'], d.get('note') or '', d.get('sales') or '', h.device)


@route('POST', '/api/audit/<iid>/reopen')
def audit_reopen(h, iid):
    audit.check_seen(int(iid), h.json_body().get('seen'), auth.user_names())
    return audit.reopen(int(iid), h.user['id'])


@route('POST', '/api/audit/<iid>/undo/<cid>')
def audit_undo(h, iid, cid):
    out = audit.undo_found(int(iid), int(cid), h.user['id'], h.device)
    sorting.forget_code(int(cid))   # a barcode this scan put on the product comes off too
    return out


@route('GET', '/api/tokens', admin=True)
def tokens(h):
    return {'tokens': auth.list_tokens()}


@route('POST', '/api/tokens', admin=True)
def token_create(h):
    return {'token': auth.create_token(h.user['id'], h.json_body().get('name', 'Chrome extension'))}


@route('POST', '/api/tokens/<tid>/revoke', admin=True)
def token_revoke(h, tid):
    auth.revoke_token(int(tid))
    return {'ok': True}


# ---------------------------------------------------------------------- connections (marketplaces, Gmail)

@route('GET', '/api/connections', admin=True)
def connections_list(h):
    out = connections.summary()
    state = sales.state()
    for ch in SYNC_CHANNELS:
        out[ch]['sync'] = {**(state.get(ch) or {}), **(sync_progress.get(ch) or {})}
    return {'connections': out}


def run_sync(channel: str):
    """One order sync to completion, with live progress for the Connections card. One at a time per channel."""
    with sync_lock:
        if (sync_progress.get(channel) or {}).get('running'):
            return False
        sync_progress[channel] = {'running': True, 'text': 'starting'}

    def progress(text):
        sync_progress[channel] = {'running': True, 'text': text}
    try:
        r = store_orders.run(progress) if channel == 'gmail' else sales.run(channel, progress)
        sync_progress[channel] = {'running': False, 'text': ''}
        log.info('order sync %s: %s', channel, r)
        if channel != 'gmail':
            autolink_sales()   # new orders link to audited or bought items now, not when someone opens Orders
            # Shipped in Seller Central / eBay: scan it out here too, as the ship dialog's defaults would (Alex 11-11).
            log.info('auto ship %s: %s', channel, commerce.auto_ship())
        # Amazon sales nothing here shipped come off the Stock audit by themselves (audit.PRESOLD_VIEW, Alex 11-11).
    except Exception as e:  # noqa: BLE001 - the error is stored in sync_state and shown on the card
        sync_progress[channel] = {'running': False, 'text': ''}
        log.warning('order sync %s failed: %s', channel, e)
    return True


@route('POST', '/api/connections/<ch>/sync', admin=True)
def connections_sync(h, ch):
    if ch not in SYNC_CHANNELS:
        raise HttpError(400, 'No order sync for that connection yet')
    in_thread(run_sync, ch)
    return {'started': True}


def sync_loop():
    """Every 15 minutes: order sync for each marketplace whose last connection test passed. The first run waits 2
    minutes after a start, so the phones reloading right after a deploy don't wait behind it (Alex 11-12)."""
    time.sleep(120)
    while True:
        for ch in SYNC_CHANNELS:
            try:
                last = connections.get(ch).get('last_test') or {}
                if last.get('ok'):
                    run_sync(ch)
            except Exception:
                log.error('sync loop %s\n%s', ch, traceback.format_exc())
        for ch in LISTING_CHANNELS:   # what is listed: once an hour (Amazon's report takes a few minutes to build)
            try:
                if _listing_ready(ch) and listings.due(15 if ch == 'ebay' else 60, ch):   # eBay: Sam lists all day
                    run_listings(ch)
            except Exception:
                log.error('listings loop %s\n%s', ch, traceback.format_exc())
        try:   # buyer returns: Amazon's returns report once an hour, so a return label scans as soon as Amazon has it
            if _listing_ready('amazon') and buyer_returns.due(60):
                log.info('amazon buyer returns: %s', buyer_returns.refresh_amazon())
        except Exception:
            log.error('buyer returns loop\n%s', traceback.format_exc())
        try:   # Amazon approval for every ASIN on hand or on the audit list: only stale answers are asked again
            if approval.ready():
                log.info('amazon approval: %s', approval.refresh())
        except Exception:
            log.error('approval loop\n%s', traceback.format_exc())
        time.sleep(900)


@route('POST', '/api/connections/<ch>', admin=True)
def connections_save(h, ch):
    return connections.save(ch, h.json_body())


@route('POST', '/api/connections/<ch>/clear', admin=True)
def connections_clear(h, ch):
    return connections.clear(ch)


@route('POST', '/api/connections/<ch>/test', admin=True)
def connections_test(h, ch):
    return connections.test(ch)


@route('POST', '/api/ebay/connect', admin=True)
def ebay_connect(h):
    return {'url': connections.ebay_consent_url()}


@route('GET', '/api/ebay/callback', admin=True)
def ebay_callback(h):
    # eBay sends the browser here after consent (the RuName's accept URL). Admin session cookie rides along (SameSite=Lax).
    try:
        user = connections.ebay_callback(h.query.get('code', ''), h.query.get('state', ''))
        h.redirect('/settings?ebay=connected' + (f'&user={user}' if user else ''))
    except ConnectionError_ as e:
        from urllib.parse import quote
        h.redirect('/settings?ebay_error=' + quote(str(e)[:200]))


@route('POST', '/api/amazon/connect', admin=True)
def amazon_connect(h):
    return {'url': connections.amazon_consent_url()}


@route('GET', '/api/amazon/login', admin=True)
def amazon_login(h):
    # Seller Central sends the browser here after Confirm (the app's OAuth Login URI); we bounce straight back.
    try:
        h.redirect(connections.amazon_login(h.query.get('amazon_callback_uri', ''), h.query.get('amazon_state', '')))
    except ConnectionError_ as e:
        from urllib.parse import quote
        h.redirect('/settings?amazon_error=' + quote(str(e)[:200]))


@route('GET', '/api/amazon/callback', admin=True)
def amazon_callback(h):
    from urllib.parse import quote
    try:
        sp = connections.amazon_callback(h.query.get('spapi_oauth_code', ''), h.query.get('state', ''),
                                         h.query.get('selling_partner_id', ''))
        result = connections.test('amazon')
        h.redirect('/settings?amazon=connected' + (f'&seller={quote(sp)}' if sp else '')
                   + ('' if result['ok'] else '&amazon_error=' + quote(result['detail'][:200])))
    except ConnectionError_ as e:
        h.redirect('/settings?amazon_error=' + quote(str(e)[:200]))


@route('GET', '/api/ebay/account-deletion', public=True)
def ebay_deletion_check(h):
    code = h.query.get('challenge_code', '')
    if not code:
        raise HttpError(400, 'challenge_code missing')
    return {'challengeResponse': connections.ebay_challenge(code)}


@route('POST', '/api/ebay/account-deletion', public=True)
def ebay_deletion_notice(h):
    try:
        payload = json.loads(h.body_bytes(64 * 1024) or b'{}')
    except ValueError:
        payload = {}
    connections.ebay_deletion_notice(payload if isinstance(payload, dict) else {})
    h.send_response(204)
    h.send_header('Content-Length', '0')
    h.end_headers()


# ---------------------------------------------------------------------- nightly jobs

def nightly_loop():
    """03:15 ET: rebuild-and-compare integrity check, then a database backup (30 kept)."""
    last_day = None
    while True:
        try:
            now = datetime.now(ET)
            if now.hour == 3 and now.minute >= 15 and last_day != now.date():
                last_day = now.date()
                with wms.op_lock:   # the rebuild reads the ledger while no write request runs
                    result = wms.check_integrity()
                log.info('integrity: ok=%s problems=%s', result['ok'], result['problem_count'])
                stamp = now.strftime('%Y-%m-%d')
                bdir = os.path.join(DATA, 'backups')
                wms.backup(os.path.join(bdir, f'wms-{stamp}.db'))
                auth_dst = os.path.join(bdir, f'auth-{stamp}.db')
                import sqlite3
                src = sqlite3.connect(auth.db_path)
                dst = sqlite3.connect(auth_dst)
                src.backup(dst)
                src.close()
                dst.close()
                for old in sorted(f for f in os.listdir(bdir) if f.endswith('.db'))[:-60]:
                    os.remove(os.path.join(bdir, old))
                auth.cleanup()
        except Exception:
            log.error('nightly job failed\n%s', traceback.format_exc())
        time.sleep(60)


def main():
    if wms.last_integrity() is None:
        wms.check_integrity()
    threading.Thread(target=nightly_loop, daemon=True).start()
    threading.Thread(target=sync_loop, daemon=True).start()
    host = '0.0.0.0' if DEV else '127.0.0.1'
    httpd = ThreadingHTTPServer((host, PORT), Handler)
    httpd.daemon_threads = True
    log.info('Boxloft on http://%s:%s (data %s)', host, PORT, DATA)
    httpd.serve_forever()


if __name__ == '__main__':
    main()
