"""Can we sell this ASIN on Amazon? (Alex 11-13: "I'd like Sam to know when she scans an item to box it if we can sell
it or not, also flagging any items in Amazon boxes that we aren't approved for.")

Source of truth: SP-API Listings Restrictions (GET /listings/2021-08-01/restrictions, condition new_new). No
restrictions = approved. Each reason has a reasonCode: APPROVAL_REQUIRED (with an apply link in Seller Central),
NOT_ELIGIBLE (Amazon takes no applications) or ASIN_NOT_FOUND. Rate: 5 a second.

Answers are cached per (ASIN, condition) in `amazon_approval` with checked_at. An approved ASIN is asked again after
7 days, a restricted one after 1 day (Alex may apply and get approved). Amazon is never called under wms.op_lock:
scans ask in server._prefetch (one try, short timeout), the GET routes ask before they answer, and the sync loop
refreshes every ASIN on hand or on the Stock audit list. Under the lock only the cache is read.
Checked on live 11-13: of 111 ASINs in A boxes, 86 approved, 15 need approval, 8 not eligible, 2 not found; every
ASIN with an Active Amazon listing came back approved (40 of 40), none of the 25 restricted has one.
Nothing here moves stock or blocks a save.
"""
import re
import threading
import time

from connections import SPAPI_NA, _err_text, _http
from wms import Wms, WmsError, now_utc

US_MARKETPLACE = 'ATVPDKIKX0DER'
CONDITION = 'new_new'
ASIN_RE = re.compile(r'^B0[A-Z0-9]{8}\Z', re.I)   # \Z: '$' also matched before a trailing newline (11-25)
OK_DAYS, RESTRICTED_DAYS = 7, 1
PACE = 0.22   # seconds between calls in a refresh (Amazon allows 5 a second)
RECHECK_MAX = 200   # ASINs one "Check again" asks (about 1 minute at PACE); the rest wait for the next press
RECHECK_RUNS = 20   # checks kept by run id for phones that lost their answer
RECHECK_INPUT_MAX = 5000   # entries a request may send at all (junk is dropped before RECHECK_MAX counts)

SCHEMA = """
CREATE TABLE IF NOT EXISTS amazon_approval (
    asin TEXT NOT NULL,
    condition TEXT NOT NULL,
    state TEXT NOT NULL,                  -- ok / approval / not_eligible / not_found / restricted (other code)
    code TEXT NOT NULL DEFAULT '',        -- Amazon's reasonCode(s), comma-joined
    message TEXT NOT NULL DEFAULT '',     -- Amazon's words
    apply_url TEXT NOT NULL DEFAULT '',   -- Seller Central's "Request approval" link, when Amazon gives one
    checked_at TEXT NOT NULL,
    PRIMARY KEY (asin, condition)
);
"""

# Worst first: the state a reason code gives, and the words Sam reads.
STATES = {
    'NOT_ELIGIBLE': 'not_eligible',
    'APPROVAL_REQUIRED': 'approval',
    'ASIN_NOT_FOUND': 'not_found',
}
RANK = ['not_eligible', 'approval', 'restricted', 'not_found']
WORDS = {
    'ok': 'OK to sell on Amazon',
    'approval': "Can't sell on Amazon yet: needs approval",
    'not_eligible': "Can't sell on Amazon: brand closed to us",
    'not_found': "Can't sell on Amazon: Amazon has no page for this ASIN",
    'restricted': "Can't sell on Amazon: restricted",
}


def parse(payload: dict) -> dict:
    """Amazon's answer -> {state, code, message, apply_url}."""
    reasons = [x for r in (payload.get('restrictions') or []) for x in (r.get('reasons') or [])]
    if not reasons and not payload.get('restrictions'):
        return {'state': 'ok', 'code': '', 'message': '', 'apply_url': ''}
    states = [STATES.get(x.get('reasonCode') or '', 'restricted') for x in reasons] or ['restricted']
    state = min(states, key=RANK.index)
    url = next((ln.get('resource') or '' for x in reasons for ln in (x.get('links') or [])
                if (ln.get('resource') or '').startswith('https://sellercentral.amazon.com/')), '')
    return {'state': state, 'code': ','.join(sorted({x.get('reasonCode') or '?' for x in reasons})),
            'message': ' '.join(dict.fromkeys(x.get('message') or '' for x in reasons)).strip()[:300], 'apply_url': url}


class Approval:
    def __init__(self, wms: Wms, connections, condition: str = CONDITION):
        self.wms = wms
        self.connections = connections
        self.condition = condition
        self._tok = {}
        self._tok_lock = threading.Lock()
        self._recheck_lock = threading.Lock()   # one "Check again" at a time
        self._tried = {}   # ASIN -> when Check again last asked Amazon, answered or not (fix check 11-24)
        # The newest Check again: progress, then its answer. A phone that lost the connection reads the answer here
        # (Alex 11-24: "It worked but also said load failed" on an 11-item check of 12 s).
        self.recheck_state = {'run': '', 'running': False}
        # The last RECHECK_RUNS checks by run id (review 11-24: another person's check must not hide yours).
        self.recheck_runs = {}
        self.last_error = ""
        self.wms._conn().executescript(SCHEMA)

    # ------------------------------------------------------------------ Amazon

    def ready(self) -> bool:
        if not self.connections:
            return False
        cur = self.connections.get('amazon')
        return bool((cur.get('last_test') or {}).get('ok') and cur.get('selling_partner_id') and cur.get('refresh_token'))

    def _token(self) -> str:
        with self._tok_lock:
            if time.time() - self._tok.get('at', 0) > 3000:
                self._tok = {'token': self.connections.amazon_access_token(), 'at': time.time()}
            return self._tok['token']

    def ask(self, asin: str, timeout: int = 20, tries: int = 3):
        """One ASIN from Amazon, saved to the cache. None when Amazon did not answer (the cache keeps the old one)."""
        asin = (asin or '').upper()
        if not ASIN_RE.match(asin) or not self.ready():
            return None
        sid = self.connections.get('amazon').get('selling_partner_id', '')
        path = (f'/listings/2021-08-01/restrictions?asin={asin}&sellerId={sid}'
                f'&marketplaceIds={US_MARKETPLACE}&conditionType={self.condition}')
        for attempt in range(tries):
            try:
                tok = self._token()
            except Exception:  # noqa: BLE001 - no token = no answer; the scan goes on
                return None
            st, p = _http('GET', SPAPI_NA + path, {'x-amz-access-token': tok, 'Accept': 'application/json',
                                                   'User-Agent': 'Boxloft/1.0 (Language=Python)'}, timeout=timeout)
            if st == 200 and isinstance(p, dict):
                return self._save(asin, parse(p))
            if st == 403:   # the token aged out early: one fresh one
                with self._tok_lock:
                    self._tok = {}
            elif st not in (429, 500, 503):
                self.last_error = f'{st}: {_err_text(p)}'
                return None
            if attempt + 1 < tries:
                time.sleep(1 + 2 * attempt)
        self.last_error = f'{st}: {_err_text(p)}'   # every try failed (403s or busy): keep why (review 11-24)
        return None

    def _save(self, asin: str, a: dict) -> dict:
        row = {'asin': asin, 'condition': self.condition, **a, 'checked_at': now_utc()}
        with self.wms._tx() as c:
            c.execute("""INSERT INTO amazon_approval (asin, condition, state, code, message, apply_url, checked_at)
                         VALUES (:asin, :condition, :state, :code, :message, :apply_url, :checked_at)
                         ON CONFLICT (asin, condition) DO UPDATE SET state = excluded.state, code = excluded.code,
                         message = excluded.message, apply_url = excluded.apply_url, checked_at = excluded.checked_at""", row)
        return row

    # ------------------------------------------------------------------ cache

    def get(self, asin: str):
        return self.wms._q1('SELECT * FROM amazon_approval WHERE asin = ? AND condition = ?',
                            ((asin or '').upper(), self.condition))

    def _stale_sql(self) -> str:
        return (f"(state = 'ok' AND checked_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-{OK_DAYS} days')) OR "
                f"(state != 'ok' AND checked_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-{RESTRICTED_DAYS} days'))")

    def stale(self, asin: str) -> bool:
        row = self.wms._q1(f'SELECT ({self._stale_sql()}) AS old FROM amazon_approval WHERE asin = ? AND condition = ?',
                           ((asin or '').upper(), self.condition))
        return not row or bool(row['old'])

    def ensure(self, asins, timeout: int = 4) -> None:
        """Before a scan (outside the lock): ask Amazon for up to 2 ASINs with no fresh answer, one try each."""
        for a in [a for a in dict.fromkeys((x or '').upper() for x in asins) if ASIN_RE.match(a)][:2]:
            if self.stale(a):
                self.ask(a, timeout=timeout, tries=1)

    # ------------------------------------------------------------------ which ASIN

    def product_asins(self, product_id) -> list:
        if not product_id:
            return []
        out = [r['value'].upper() for r in self.wms._q(
            "SELECT value FROM product_identifier WHERE product_id = ? AND kind = 'ASIN' ORDER BY id", (product_id,))]
        p = self.wms._q1('SELECT sku FROM product WHERE id = ?', (product_id,))
        if p and ASIN_RE.match(p['sku'] or ''):
            out.append(p['sku'].upper())
        return list(dict.fromkeys(a for a in out if ASIN_RE.match(a)))

    def code_asins(self, code: str) -> list:
        """The ASIN(s) a scanned code stands for: the product holding it, else the catalog's cached answer."""
        code = (code or '').strip()
        if not code:
            return []
        if ASIN_RE.match(code):
            return [code.upper()]
        out = []
        for p in self.wms.lookup(code):
            if not p.get('archived'):
                out += self.product_asins(p['id'])
        if not out:
            import json
            row = self.wms._q1('SELECT data FROM catalog_cache WHERE code = ?', (code.upper(),))
            try:
                out = [(h.get('asin') or '').upper() for h in json.loads(row['data'])] if row else []
            except ValueError:
                out = []
        return list(dict.fromkeys(a for a in out if ASIN_RE.match(a)))

    def item_asins(self, item_id) -> list:
        if not item_id:
            return []
        r = self.wms._q1('SELECT asin FROM audit_item WHERE id = ?', (item_id,))
        return [r['asin'].upper()] if r and ASIN_RE.match(r['asin'] or '') else []

    # ------------------------------------------------------------------ what a screen shows

    def note(self, asins) -> dict:
        """The worst cached answer over the item's ASINs, with Sam's words. Cache only (safe under the lock).
        {asin, state, can_sell, text, detail, apply_url, checked_at}; state 'no_asin' / 'unknown' when there's none."""
        asins = [a for a in dict.fromkeys((x or '').upper() for x in asins) if ASIN_RE.match(a)]
        if not asins:
            return {'asin': '', 'state': 'no_asin', 'can_sell': None, 'text': 'No ASIN: Amazon approval not checked',
                    'detail': '', 'apply_url': '', 'checked_at': None}
        rows = [r for r in (self.get(a) for a in asins) if r]
        if not rows:
            return {'asin': asins[0], 'state': 'unknown', 'can_sell': None,
                    'text': 'Amazon approval not checked yet', 'detail': '', 'apply_url': '', 'checked_at': None}
        worst = min(rows, key=lambda r: (RANK + ['ok']).index(r['state']) if r['state'] in RANK + ['ok'] else 0)
        return {'asin': worst['asin'], 'state': worst['state'], 'can_sell': worst['state'] == 'ok',
                'text': WORDS.get(worst['state'], WORDS['restricted']), 'detail': worst['message'],
                'apply_url': worst['apply_url'], 'checked_at': worst['checked_at']}

    # ------------------------------------------------------------------ background refresh

    def wanted(self) -> list:
        """Every ASIN worth knowing: products with stock on a shelf or in a box, Stock audit rows, buys of the last 120 days."""
        out = []
        for r in self.wms._q("""
                SELECT DISTINCT s.product_id FROM stock_level s LEFT JOIN location l ON l.id = s.location_id
                 WHERE s.qty > 0 AND (s.box_id != 0 OR l.kind != 'virtual')"""):
            out += self.product_asins(r['product_id'])
        for sql in ("SELECT DISTINCT asin FROM audit_item WHERE asin != ''",
                    # buys of the last 120 days: what is arriving gets scanned into a box next
                    """SELECT DISTINCT pl.asin FROM purchase_line pl JOIN purchase_order po ON po.id = pl.po_id
                        WHERE pl.asin != '' AND pl.cancelled = 0 AND po.ordered_at >= date('now', '-120 days')"""):
            try:
                out += [r['asin'].upper() for r in self.wms._q(sql)]
            except Exception:  # noqa: BLE001 - a table a fresh database doesn't have yet
                pass
        return [a for a in dict.fromkeys(out) if ASIN_RE.match(a)]

    def refresh(self, progress=None) -> dict:
        """Ask Amazon for every wanted ASIN with no fresh answer, paced to 5 a second, to the end (no cap)."""
        if not self.ready():
            return {'asked': 0, 'skipped': 'Amazon not connected'}
        todo = [a for a in self.wanted() if self.stale(a)]
        asked = failed = 0
        for i, a in enumerate(todo):
            if self.ask(a) is None:
                failed += 1
            asked += 1
            if progress and i % 25 == 0:
                progress(f'Amazon approval: {i + 1} of {len(todo)}')
            time.sleep(PACE)
        return {'asked': asked, 'failed': failed, 'counts': self.counts()}

    def recheck(self, asins, run: str = '') -> dict:
        """Alex 11-24 ("a way to refresh the checks to see if I'm still gated"): ask Amazon now for these ASINs, fresh
        answer or not, paced like refresh. Network only: never call it under the lock. One run at a time.
        {asked, failed, now_ok: [asin], still, left, checked_at}; now_ok = gated before, may sell now. Past RECHECK_MAX
        the oldest answers go first and `left` says how many wait, so the next press reaches them (review 11-24)."""
        valid = [a for a in dict.fromkeys((x or '').upper() for x in asins or []) if ASIN_RE.match(a)]
        if len(valid) > RECHECK_MAX:   # a failed ask counts as asked too, so ASINs Amazon keeps failing can't starve the rest
            valid.sort(key=lambda a: max((self.get(a) or {}).get('checked_at') or '', self._tried.get(a, '')))
        asins = valid[:RECHECK_MAX]
        if not self.ready():
            raise WmsError('Amazon is not connected (Settings, Connections), so Boxloft cannot ask it.')
        if not self._recheck_lock.acquire(blocking=False):
            raise WmsError('Amazon is being asked already. Wait a minute, then reload.')
        state = {'run': run, 'running': True, 'done': 0, 'total': len(asins), 'started_at': now_utc()}
        finished = False
        self._keep_run(state)
        try:
            before = {a: (self.get(a) or {}).get('state') for a in asins}
            failed, now_ok, still = 0, [], 0
            for i, a in enumerate(asins):
                state['done'] = i
                if i:
                    time.sleep(PACE)
                self._tried[a] = now_utc()
                r = self.ask(a, timeout=10, tries=2)
                if r is None:
                    failed += 1
                elif r['state'] == 'ok':
                    if before[a] and before[a] != 'ok':
                        now_ok.append(a)
                else:
                    still += 1
            out = {'asked': len(asins), 'failed': failed, 'now_ok': now_ok, 'still': still,
                   'left': len(valid) - len(asins), 'checked_at': now_utc()}
            self._keep_run({**state, 'running': False, 'done': len(asins), 'result': out})
            finished = True
            return out
        finally:
            if not finished:   # died midway, run id or not (review 11-24: one with no id stayed running)
                self._keep_run({**state, 'running': False, 'error': 'The check stopped. Try again.'})
            self._recheck_lock.release()

    def _keep_run(self, state: dict) -> None:
        self.recheck_state = state
        if state.get('run'):
            self.recheck_runs.pop(state['run'], None)
            self.recheck_runs[state['run']] = state
            while len(self.recheck_runs) > RECHECK_RUNS:
                self.recheck_runs.pop(next(iter(self.recheck_runs)))

    def run_state(self, run: str = '') -> dict:
        """One check by run id ({..., unknown: True} when this server never ran it or restarted since), else the newest
        with the recent ones listed."""
        if run:
            return self.recheck_runs.get(run) or {'run': run, 'running': False, 'unknown': True}
        return {**self.recheck_state, 'recent': list(reversed(self.recheck_runs.values()))}

    def counts(self) -> dict:
        return {r['state']: r['n'] for r in self.wms._q(
            'SELECT state, COUNT(*) AS n FROM amazon_approval WHERE condition = ? GROUP BY state', (self.condition,))}
