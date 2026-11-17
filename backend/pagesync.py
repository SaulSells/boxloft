"""Extension 2.0: Boxloft plans the run, the extension fetches, Boxloft parses.

The extension calls POST /api/inbound/v2/step in a loop. Each call carries the pages it just read
(Amazon pages with scripts and styles cut out; for Walmart only the order object from the page's
__NEXT_DATA__) and the `state` this module handed it last time, unchanged. The answer is the next
pages to read, or done.

The extension keeps its own safety rules and this module can't change them: a fixed URL allowlist
(Amazon Your Orders pages, walmart.com/orders/<digits>), a fixed pace, a page cap per run, and a
stop on any sign-in, captcha or robot-check page. A URL outside the allowlist comes back as
`refused` and is skipped.

The walk is the one extension 1.5.0 did by itself (extension/sync.js runAmazon/runWalmart):
1. Your Orders list pages, newest first, year by year. Each page's new and still-open orders get
   their detail page read and synced before the next list page. Stop at the first order older
   than 180 days, or, once the first full run is done, at a page with nothing new.
2. Open orders the list pages didn't reach.
3. Track package pages (extension 2.1+) of packages Amazon really shipped (a shipmentId on the order page's
   Track link): every run while on the way, once more after delivery for the delivery photo. The photo link
   expires in 3 days, so the server copies it into DATA/photos/track before the step takes the lock
   (prefetch_photos). An older extension refuses these URLs; the phase then stops and the summary says so.
4. Seller Central (extension 2.2+, Alex 11-11 ship box): sellercentral.amazon.com/orders-api/order/<id> of the
   FBM orders the order sync just saw go to shipped (backend/shipbox.py ShipBoxes.wanted), never any other
   order. The extension sends only each package's size, weight, label cost, carrier and ASINs, never the buyer.
   An older extension refuses these URLs; the phase then stops and the summary says to reload it.
5. Walmart orders from store emails that still need item prices or a status.
"""
import config
import html as htmllib
import json
import os
import re
import urllib.request
from datetime import date, datetime, timedelta, timezone

import pages
from inbound import today_et
from wms import now_utc

AMZ = 'https://www.amazon.com'
WMT = 'https://www.walmart.com'
SELLER = 'https://sellercentral.amazon.com'
CUTOFF_DAYS = 180
FINAL = ('delivered', 'cancelled', 'returned')
DETAILS_PER_STEP = 10      # one list page's worth
OPEN_PER_STEP = 10
WALMART_PER_STEP = 5
TRACK_PER_STEP = 10
TRACK_PER_RUN = 60
SELLER_PER_STEP = 10
# Alex 11-11 7:30 PM ET: no Seller Central reads. Amazon's seller agreement (Agent Policy, from 2026-03-04) wants
# automated tools to identify themselves; this read used the signed-in session, on the business account. The
# box popup learns the box from people instead. Amazon.com reads: one run every 2 hours, none 10 PM to 6 AM ET.
SELLER_READS = False
RUN_EVERY = timedelta(hours=2)
RUN_SLACK = timedelta(minutes=10)   # the extension wakes every 30 min; a run that ended 1:52 ago may start
QUIET_ET = (22, 6)                  # no run from 22:00 to 05:59 ET
TRACK_DELIVERED_DAYS = 14  # same window as Arriving's "Delivered, not scanned in"
PHOTO_MAX = 3 * 1024 * 1024
MAX_RESULTS = 20           # pages accepted per step

AMZ_LIST_RE = re.compile(r'^https://www\.amazon\.com/your-orders/orders\?timeFilter=year-(\d{4})&startIndex=(\d{1,4})$')
AMZ_DETAIL_RE = re.compile(r'^https://www\.amazon\.com/your-orders/order-details\?orderID=(\d{3}-\d{7}-\d{7})$')
WMT_RE = re.compile(r'^https://www\.walmart\.com/orders/(\d{10,20})$')
# Same shape as extension/sync.js ALLOW: never the preship/cancel-items page the order page also links.
AMZ_TRACK_RE = re.compile(r'^https://www\.amazon\.com/progress-tracker/package\?orderId=(\d{3}-\d{7}-\d{7})'
                          r'&shipmentId=([A-Za-z0-9]{4,24})&packageIndex=(\d{1,2})$')
SELLER_RE = re.compile(r'^https://sellercentral\.amazon\.com/orders-api/order/(\d{3}-\d{7}-\d{7})$')
PHOTO_SRC_RE = re.compile(r'data-src="(https://us-prod-temp\.s3\.amazonaws\.com/imageId-[^"]+)"')


class PageSyncError(Exception):
    pass


def amazon_list_url(y, start):
    return f'{AMZ}/your-orders/orders?timeFilter=year-{int(y)}&startIndex={int(start)}'


def amazon_detail_url(oid):
    return f'{AMZ}/your-orders/order-details?orderID={oid}'


def amazon_track_url(oid, shipment_id, package_index):
    return f'{AMZ}/progress-tracker/package?orderId={oid}&shipmentId={shipment_id}&packageIndex={int(package_index or 0)}'


def seller_order_url(oid):
    return f'{SELLER}/orders-api/order/{oid}'


def photo_rel(url: str):
    """Where a delivery photo is kept under DATA/photos, named by Amazon's image id. None if not a photo link."""
    m = pages.PHOTO_RE.match(url or '')
    return f'track/{m.group(1)}.jpg' if m else None


def _download_photo(url: str, photos_dir: str) -> bool:
    """Copy one delivery photo. Only Amazon's photo bucket (pages.PHOTO_RE), at most 3 MB, JPEG or PNG."""
    rel = photo_rel(url)
    if not rel:
        return False
    full = os.path.join(photos_dir, rel)
    if os.path.exists(full):
        return True
    req = urllib.request.Request(url, headers={'User-Agent': 'Boxloft'})
    with urllib.request.urlopen(req, timeout=10) as r:
        if r.geturl().split('?')[0] != url.split('?')[0]:
            return False   # no redirects off the bucket
        data = r.read(PHOTO_MAX + 1)
    if len(data) > PHOTO_MAX or not (data[:3] == b'\xff\xd8\xff' or data[:8] == b'\x89PNG\r\n\x1a\n'):
        return False
    os.makedirs(os.path.dirname(full), exist_ok=True)
    tmp = full + '.part'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, full)
    return True


def prefetch_photos(body: dict, photos_dir: str, fetch=None) -> int:
    """Before the step takes the lock: copy the delivery photo on each Track page it carries. A failure is
    left for a later run (the page is read again while its package has no photo copy, up to 4 reads)."""
    got = 0
    for r in (body or {}).get('results') or []:
        if not isinstance(r, dict) or not AMZ_TRACK_RE.match(str(r.get('url') or '')):
            continue
        m = PHOTO_SRC_RE.search(str(r.get('html') or ''))
        if not m:
            continue
        try:
            got += 1 if (fetch or _download_photo)(htmllib.unescape(m.group(1)), photos_dir) else 0
        except Exception:  # noqa: BLE001 - network or disk; retried on a later run
            pass
    return got


def walmart_url(oid):
    return f"{WMT}/orders/{re.sub(r'[^0-9]', '', oid)}"


class PageSync:
    def __init__(self, wms, inbound, store_orders, photos_dir=None, shipboxes=None):
        self.wms, self.inbound, self.store_orders = wms, inbound, store_orders
        self.shipboxes = shipboxes
        self.photos_dir = photos_dir
        wms._conn().executescript("""CREATE TABLE IF NOT EXISTS sync_state (
            channel TEXT PRIMARY KEY, cursor TEXT, last_run_at TEXT, last_ok_at TEXT, last_error TEXT, last_result TEXT)""")

    # ---------------------------------------------------------------- state
    def _backfill_done(self) -> bool:
        r = self.wms._q1("SELECT cursor FROM sync_state WHERE channel = 'chrome'") or {}
        if r.get('cursor') == 'backfill_done':
            return True
        # Extension 1.x keeps this flag in Chrome. A house computer that ran 1.x has filled the orders already.
        return bool(self.wms._q1("SELECT 1 AS x FROM purchase_order WHERE source = 'amazon' AND last_seen_at IS NOT NULL LIMIT 1"))

    def _new_state(self, today: date) -> dict:
        cutoff = today - timedelta(days=CUTOFF_DAYS)
        return {'v': 2, 'phase': 'amazon_list', 'y': today.year, 'start': 0, 'cutoff': cutoff.isoformat(),
                'seen_at': today.isoformat(), 'backfill_done': self._backfill_done(), 'done': [],
                'unseen': None, 'past_cutoff': False, 'amazon': {'pages': 0, 'found_new': 0, 'checked': 0, 'sent': 0,
                                                                 'skipped_old': 0, 'error': None},
                'walmart': None, 'wm': {'asked': 0, 'read': 0, 'sent': 0, 'missing': 0, 'error': None},
                'track': None, 'tr': {'asked': 0, 'read': 0, 'photos': 0, 'error': None},
                'seller': None, 'sc': {'asked': 0, 'read': 0, 'sizes': 0, 'error': None}}

    def not_now(self, now: datetime = None) -> str:
        """Why a new run must not start now ('' = go): quiet hours in ET, or the last run ended under 2 hours ago."""
        now = now or datetime.now(timezone.utc)
        et = now.astimezone(config.TZ)
        if et.hour >= QUIET_ET[0] or et.hour < QUIET_ET[1]:
            return f'Quiet hours: no Amazon reads from 10 PM to 6 AM {config.TZ_LABEL}.'
        last = (self.wms._q1("SELECT last_run_at FROM sync_state WHERE channel = 'chrome'") or {}).get('last_run_at')
        if last:
            at = datetime.fromisoformat(last.replace('Z', '+00:00'))
            if now - at < RUN_EVERY - RUN_SLACK:
                nxt = (at + RUN_EVERY - RUN_SLACK).astimezone(config.TZ)
                return f"Runs every 2 hours. Next run after {nxt.strftime('%-I:%M %p')} {config.TZ_LABEL}."
        return ''

    # ---------------------------------------------------------------- step
    def step(self, body: dict, device: str = '', today: date = None) -> dict:
        today = today or today_et()   # the read date in ET: "Arriving today" read at 8 PM ET is still today
        state = body.get('state')
        if state is None:
            state = self._new_state(today)
        elif not isinstance(state, dict) or state.get('v') != 2:
            raise PageSyncError('Unknown run state; start a new run')
        results = body.get('results') or []
        if not isinstance(results, list) or len(results) > MAX_RESULTS:
            raise PageSyncError(f'Send a list of up to {MAX_RESULTS} pages')
        stop = body.get('stop') or {}

        known = self.inbound.known()['orders']
        self._take_results(state, results, known, device)
        known = self.inbound.known()['orders']

        if stop.get('amazon') and state['phase'].startswith('amazon'):
            state['amazon']['error'] = str(stop['amazon'])[:200]
            state['phase'] = 'seller'   # Seller Central is its own sign-in: still read it
        if stop.get('seller') and state['phase'] == 'seller':
            if 'sc' in state:
                state['sc']['error'] = str(stop['seller'])[:200]
            state['phase'] = 'walmart'
        if stop.get('walmart') and state['phase'] == 'walmart':
            state['wm']['error'] = str(stop['walmart'])[:200]
            state['phase'] = 'done'

        fetch = self._plan(state, known)
        done = state['phase'] == 'done'
        summary = self._summary(state)
        if done:
            self._record(state, summary)
        return {'fetch': fetch, 'state': state, 'done': done, 'summary': summary}

    def _take_results(self, state, results, known, device):
        orders, walmart = [], []
        cutoff = date.fromisoformat(state['cutoff'])
        a = state['amazon']
        for r in results:
            if not isinstance(r, dict):
                continue
            url = str(r.get('url') or '')
            if r.get('refused') or r.get('error'):
                # A list page that didn't come back ends the Amazon part (asking again would loop);
                # a detail page that didn't is skipped, as in 1.x.
                if AMZ_LIST_RE.match(url) and state['phase'].startswith('amazon'):
                    a['error'] = f"list page not read: {str(r.get('refused') or r.get('error'))[:120]}"
                    state['phase'] = 'seller'
                if AMZ_TRACK_RE.match(url) and r.get('refused') and 'tr' in state:
                    # Extension 2.0 has no Track pages on its allowlist: stop asking this run.
                    state['tr']['error'] = ('The extension refused Track pages: reload it in chrome://extensions (2.1.0)'
                                            if 'allowlist' in str(r['refused']) else str(r['refused'])[:120])
                    state['track'] = []
                if SELLER_RE.match(url) and r.get('refused') and 'sc' in state:
                    state['sc']['error'] = ('The extension refused Seller Central: reload it in chrome://extensions (2.2.0)'
                                            if 'allowlist' in str(r['refused']) else str(r['refused'])[:120])
                    state['seller'] = []
                continue
            m = SELLER_RE.match(url)
            if m and 'sc' in state:
                if self.shipboxes and isinstance(r.get('packages'), list):
                    got = self.shipboxes.take(m.group(1), r['packages'][:20])
                    state['sc']['read'] += 1
                    state['sc']['sizes'] += got['sized']
                continue
            m = AMZ_TRACK_RE.match(url)
            if m and 'tr' in state:
                tp = pages.parse_amazon_track(str(r.get('html') or ''))
                rel = photo_rel(tp['photo_url']) if tp['photo_url'] else None
                if rel and not (self.photos_dir and os.path.exists(os.path.join(self.photos_dir, rel))):
                    rel = None
                if self.inbound.take_track(m.group(1), m.group(2), tp, rel, date.fromisoformat(state['seen_at'])):
                    state['tr']['read'] += 1
                    state['tr']['photos'] += 1 if rel else 0
                continue
            m = AMZ_LIST_RE.match(url)
            if m:
                a['pages'] += 1
                lp = pages.parse_amazon_list(str(r.get('html') or ''))
                state['list'] = {'ids': lp['ids'], 'cards': lp['cards']}
                continue
            m = AMZ_DETAIL_RE.match(url)
            if m:
                oid = m.group(1)
                o = pages.parse_amazon_detail(oid, str(r.get('html') or ''), state['seen_at'])
                a['checked'] += 1
                placed = _parse_js_date(o['ordered_at'])
                if placed and placed < cutoff and oid not in known:
                    a['skipped_old'] += 1
                    state['past_cutoff'] = True
                    continue
                if oid not in known:
                    a['found_new'] += 1
                orders.append(o)
                continue
            if WMT_RE.match(url):
                o = r.get('order')
                if isinstance(o, dict) and o:
                    walmart.append(pages.parse_walmart_order(o))
                    state['wm']['read'] += 1
                else:
                    state['wm']['missing'] += 1
        if orders:
            self.inbound.sync(orders, device)
            a['sent'] += len(orders)
        if walmart:
            self.store_orders.walmart_pages(walmart)
            state['wm']['sent'] += len(walmart)

    def _plan(self, state, known) -> list:
        """Next URLs to read. Advances state['phase'] until it has something to fetch or is done."""
        open_ = {oid for oid, o in known.items() if o.get('state') not in FINAL}
        cutoff_year = date.fromisoformat(state['cutoff']).year
        for _ in range(50):   # phase changes per step; each pass either returns or moves on
            ph = state['phase']
            if ph == 'amazon_list':
                lp = state.pop('list', None)
                if lp is not None:
                    if not lp['cards']:
                        state['y'] -= 1
                        state['start'] = 0
                        if state['y'] < cutoff_year:
                            state['phase'] = 'amazon_open'
                            continue
                        return [amazon_list_url(state['y'], state['start'])]
                    if not lp['ids']:
                        state['amazon']['error'] = 'Amazon changed the orders page (no order IDs found)'
                        state['phase'] = 'seller'
                        continue
                    state['unseen'] = len([i for i in lp['ids'] if i not in known])
                    todo = [i for i in lp['ids'] if i not in state['done'] and (i not in known or i in open_)]
                    state['done'] += todo
                    state['queue'] = todo
                    state['phase'] = 'amazon_details'
                    continue
                return [amazon_list_url(state['y'], state['start'])]
            if ph == 'amazon_details':
                q = state.get('queue') or []
                if q:
                    state['queue'] = q[DETAILS_PER_STEP:]
                    return [amazon_detail_url(i) for i in q[:DETAILS_PER_STEP]]
                state['phase'] = 'amazon_after_details'
                continue
            if ph == 'amazon_after_details':
                if state['past_cutoff'] or (state['backfill_done'] and not state['unseen']):
                    state['phase'] = 'amazon_open'
                    continue
                state['start'] += 10
                if state['start'] >= 2000:
                    state['y'] -= 1
                    state['start'] = 0
                    if state['y'] < cutoff_year:
                        state['phase'] = 'amazon_open'
                        continue
                state['phase'] = 'amazon_list'
                return [amazon_list_url(state['y'], state['start'])]
            if ph == 'amazon_open':
                rest = sorted(i for i in open_ if i not in state['done'])[:OPEN_PER_STEP]
                if rest:
                    state['done'] += rest
                    return [amazon_detail_url(i) for i in rest]
                if not state['amazon']['error']:
                    state['backfill_done'] = True
                state['phase'] = 'amazon_track'
                continue
            if ph == 'amazon_track':
                if 'tr' not in state:   # a run that started before this phase existed
                    state['phase'] = 'seller'
                    continue
                if state.get('track') is None:
                    state['track'] = [list(t) for t in self.track_wanted(date.fromisoformat(state['seen_at']))[:TRACK_PER_RUN]]
                    state['tr']['asked'] = len(state['track'])
                batch, state['track'] = state['track'][:TRACK_PER_STEP], state['track'][TRACK_PER_STEP:]
                if batch:
                    return [amazon_track_url(*t) for t in batch]
                state['phase'] = 'seller'
                continue
            if ph == 'seller':
                if 'sc' not in state or not self.shipboxes or not SELLER_READS:   # old run, no ship boxes, or off
                    state['phase'] = 'walmart'
                    continue
                if state.get('seller') is None:
                    state['seller'] = list(self.shipboxes.wanted())
                    state['sc']['asked'] = len(state['seller'])
                batch, state['seller'] = state['seller'][:SELLER_PER_STEP], state['seller'][SELLER_PER_STEP:]
                if batch:
                    return [seller_order_url(i) for i in batch]
                state['phase'] = 'walmart'
                continue
            if ph == 'walmart':
                if state['walmart'] is None:
                    state['walmart'] = list(self.store_orders.walmart_wanted().get('orders') or [])
                    state['wm']['asked'] = len(state['walmart'])
                batch, state['walmart'] = state['walmart'][:WALMART_PER_STEP], state['walmart'][WALMART_PER_STEP:]
                if batch:
                    return [walmart_url(i) for i in batch]
                state['phase'] = 'done'
                continue
            return []
        raise PageSyncError('run planner did not settle')

    def track_wanted(self, today: date) -> list:
        """[(order id, shipment id, package index)] whose Track page to read: packages on the way, and packages
        delivered in the last 14 days until a Track read saw them delivered (and copied the photo: 4 reads at most)."""
        since = (today - timedelta(days=TRACK_DELIVERED_DAYS)).isoformat()
        rows = self.wms._q("""
            SELECT o.external_id, p.shipment_id, MIN(p.package_index) AS package_index, MAX(p.state = 'delivered') AS done
              FROM purchase_package p JOIN purchase_order o ON o.id = p.po_id
              LEFT JOIN package_track t ON t.po_id = p.po_id AND t.shipment_id = p.shipment_id
             WHERE o.source = 'amazon' AND o.hidden = 0 AND p.shipment_id IS NOT NULL
               AND (p.state IN ('ordered', 'shipped')
                    OR (p.state = 'delivered' AND p.delivered_on >= ?
                        AND (t.id IS NULL OR t.state != 'delivered'
                             OR (t.photo IS NULL AND t.photo_seen = 1 AND t.reads < 4))))
             GROUP BY o.external_id, p.shipment_id
             ORDER BY done DESC, o.external_id""", (since,))
        return [(r['external_id'], r['shipment_id'], r['package_index'] or 0) for r in rows]

    @staticmethod
    def _summary(state) -> dict:
        a = state['amazon']
        out = {'ok': not a['error'], 'pages': a['pages'], 'found_new': a['found_new'], 'checked': a['checked'],
               'sent': a['sent'], 'skipped_old': a['skipped_old'], 'backfill_done': state['backfill_done'],
               'step': state['phase'].replace('_', ' '),
               'walmart': {k: v for k, v in state['wm'].items() if v is not None}}
        if state.get('tr'):
            out['track'] = {k: v for k, v in state['tr'].items() if v is not None}
        if state.get('sc'):
            out['seller'] = {k: v for k, v in state['sc'].items() if v is not None}
        if a['error']:
            out['error'] = a['error']
        return out

    def _record(self, state, summary):
        with self.wms._tx() as c:
            c.execute("""INSERT INTO sync_state (channel, cursor, last_run_at, last_ok_at, last_error, last_result)
                         VALUES ('chrome', ?, ?, ?, ?, ?)
                         ON CONFLICT(channel) DO UPDATE SET
                             cursor = COALESCE(excluded.cursor, sync_state.cursor),
                             last_run_at = excluded.last_run_at,
                             last_ok_at = COALESCE(excluded.last_ok_at, sync_state.last_ok_at),
                             last_error = excluded.last_error, last_result = excluded.last_result""",
                      ('backfill_done' if state['backfill_done'] else None, now_utc(),
                       None if summary.get('error') else now_utc(), summary.get('error'), json.dumps(summary)))


MONTHS = {m: i for i, m in enumerate(['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], 1)}


def _parse_js_date(s: str):
    """The order date the way 1.5.0's `new Date(o.ordered_at)` read it ('November 3, 2026'); None if unreadable."""
    m = re.search(r'([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})', s or '')
    if m and m.group(1).lower() in MONTHS:
        try:
            return date(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2)))
        except ValueError:
            return None
    try:
        return datetime.fromisoformat((s or '')[:10]).date()
    except ValueError:
        return None

