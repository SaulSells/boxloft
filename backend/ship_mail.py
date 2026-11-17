"""Shipping emails -> packages (Alex 11-16: "The Gmail should capture all shipping confirmations with their tracking").

A store's shipping email is its advance ship notice: per package, the carrier, the tracking number and (often) the items
inside. Boxloft reads each one once, ties it to a known order of that store by its order number (the take_cancel
rule), and saves the packages in mail_package, so a scan of the box's carrier label (labels.py) opens the order and its
items, and Arriving shows the order shipped with carrier and tracking.

Code only, no AI:
- A tracking number counts only next to a tracking label ("Fedex tracking number 5241...", "Tracking #:",
  "Track your package:") or as a carrier URL's parameter. A number in a carrier's format must pass that carrier's
  check digit (UPS 1Z, FedEx 12 digits, USPS); a bare 12- or 15-digit number anywhere else is an item id or a UPC.
- Items are placed by their words against the order's own lines (store_mail._words, as cancel_plan does), in the
  part of the email that belongs to that package: Walmart prints a package's items before its number, the others after.
  Lists of items not shipped yet ("Waiting to ship") or not bought ("Recently viewed") end the read.
- A return shipment's email never gives inbound tracking. Order state only moves forward (ordered -> shipped ->
  delivered), and delivered only when the delivered packages cover every unit of every line.
"""
import json
import re
from datetime import date

from store_mail import TOKEN_RE, _words, et_day, mail_text, store_of
from wms import now_utc

SHIP_QUERY = ('subject:(shipped OR shipping OR shipment OR "on its way" OR "on the way" OR tracking OR "out for delivery" '
              'OR delivered OR arriving OR arrive OR arrived OR package OR delayed OR track) '
              '-from:amazon.com -from:ebay.com -category:promotions -category:social')

SCHEMA = """
CREATE TABLE IF NOT EXISTS ship_mail (
    message_id TEXT PRIMARY KEY,      -- each shipping email is read once
    at TEXT NOT NULL,
    sent_at TEXT,
    sender TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL,            -- tracking | no_tracking | no_order | hidden | not_ship | return | skipped_store
    po_id INTEGER,
    result TEXT                       -- what the email said (JSON): state, packages, items
);
CREATE TABLE IF NOT EXISTS mail_package (
    id INTEGER PRIMARY KEY,
    po_id INTEGER NOT NULL REFERENCES purchase_order(id),
    tracking_id TEXT NOT NULL,        -- labels.clean form
    carrier TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,              -- shipped | out_for_delivery | delayed | delivered (never steps back)
    shipped_on TEXT,
    expected_on TEXT,
    delivered_on TEXT,
    message_id TEXT,                  -- the newest email about it
    seen_at TEXT NOT NULL,
    UNIQUE (po_id, tracking_id)
);
CREATE INDEX IF NOT EXISTS mail_package_track ON mail_package(tracking_id);
CREATE TABLE IF NOT EXISTS mail_package_line (
    package_id INTEGER NOT NULL REFERENCES mail_package(id),
    line_id INTEGER NOT NULL REFERENCES purchase_line(id),
    qty INTEGER NOT NULL,
    PRIMARY KEY (package_id, line_id)
);
"""

STATE_RANK = {'shipped': 1, 'delayed': 1, 'out_for_delivery': 2, 'delivered': 3}

# ------------------------------------------------------------------ tracking numbers


def _ups_ok(t: str) -> bool:
    v = [int(c) if c.isdigit() else (ord(c) - 63) % 10 for c in t[2:17]]
    return (10 - (sum(v[0::2]) + 2 * sum(v[1::2])) % 10) % 10 == int(t[17]) if t[17].isdigit() else False


def _fedex12_ok(t: str) -> bool:
    d = [int(c) for c in t]
    return sum(x * (1, 3, 7)[i % 3] for i, x in enumerate(reversed(d[:11]))) % 11 % 10 == d[11]


def _mod10_ok(t: str) -> bool:
    """USPS (and GS1): weights 3, 1 from the right of the body."""
    d = [int(c) for c in t]
    return (10 - sum(x * (3 if i % 2 == 0 else 1) for i, x in enumerate(reversed(d[:-1]))) % 10) % 10 == d[-1]


# (carrier, pattern, check or None). The first pattern that fits decides; a failed check means not a tracking number.
CARRIER_FORMATS = [
    ('UPS', re.compile(r'1Z[0-9A-Z]{16}'), _ups_ok),
    ('Amazon', re.compile(r'TBA\d{12}'), None),
    ('USPS', re.compile(r'9[1-5]\d{20}|9[1-5]\d{24}|9[1-5]\d{28}|9[1-5]\d{32}'), _mod10_ok),
    ('USPS', re.compile(r'[A-Z]{2}\d{9}US'), None),
    ('FedEx', re.compile(r'\d{12}'), _fedex12_ok),
    ('FedEx', re.compile(r'\d{15}'), None),
    ('DHL', re.compile(r'GM\d{16,22}'), None),
    ('LaserShip', re.compile(r'1LS[0-9A-Z]{10,20}'), None),
    ('OnTrac', re.compile(r'[CD]\d{14}'), None),
]
CARRIER_WORDS = {'ups': 'UPS', 'upsn': 'UPS', 'fedex': 'FedEx', 'usps': 'USPS', 'dhl': 'DHL', 'ontrac': 'OnTrac',
                 'lasership': 'LaserShip', 'amazon': 'Amazon', 'walmart': 'Walmart', 'uniuni': 'UniUni', 'veho': 'Veho'}

LABEL_RE = re.compile(
    r'(?i)(?:\b(?P<car>upsn?|fedex|usps|dhl|ontrac|lasership|amazon|walmart|uniuni|veho)\b'
    r'(?:[ \t]+(?:ecommerce|express|ground|smartpost|mail))?[ \t]*)?'
    r'(?:tracking[ \t]*(?:number|no\.?|#|id|code)?|track your (?:package|shipment))[ \t]*(?:is)?[ \t]*[:#]?'
    r'(?:\s|-->|-)*(?P<tok>[A-Z0-9](?:[A-Z0-9]| (?=\d))+)')
URL_PARAM_RE = re.compile(r'(?i)[?&](?:tracknums?|tracknumbers?|trknbr|tlabels|qtc_tlabels1|tracking_?numbers?|trackingid|tracking_id)'
                          r'=([A-Z0-9%, ]{8,60})')
URL_PATH_RE = re.compile(r'(?i)(?:track\.amazon\.com/tracking/|/track(?:ing)?/)(TBA\d{12}|1Z[0-9A-Z]{16})\b')


def carrier_format(t: str):
    """(carrier, check passed) for a cleaned number, or ('', None) when no carrier format fits."""
    for name, rx, check in CARRIER_FORMATS:
        if rx.fullmatch(t):
            return name, (check(t) if check else True)
    return '', None


def _plain(t: str) -> str:
    return re.sub(r'[^0-9A-Z]', '', (t or '').upper())


def tracking_numbers(text: str, html: str = '', not_these=()) -> list:
    """[{tracking_id, carrier, pos}] in the order they first appear. pos = index in text (None: only in the html)."""
    out = {}
    skip = {_plain(x) for x in not_these if x}

    def add(raw, word, pos, labelled):
        t = _plain(raw)
        if len(t) < 8 or len(t) > 34 or t in skip or len(set(t)) < 3 or sum(c.isdigit() for c in t) < 6:
            return
        fmt, ok = carrier_format(t)
        if ok is False:
            return   # a carrier's format with a wrong check digit: an item id or a typo, never a package
        if not fmt and not labelled:
            return   # an unknown format counts only where the email calls it a tracking number
        if t not in out:
            out[t] = {'tracking_id': t, 'carrier': CARRIER_WORDS.get((word or '').lower(), '') or fmt, 'pos': pos}
        elif out[t]['pos'] is None and pos is not None:
            out[t]['pos'] = pos

    for m in LABEL_RE.finditer(text or ''):
        add(m.group('tok').strip(), m.group('car'), m.start(), True)
    for src, is_text in ((text or '', True), (html or '', False)):
        for m in URL_PARAM_RE.finditer(src):
            for part in re.split(r'%2C|,', m.group(1), flags=re.I):
                add(part, '', m.start() if is_text else None, False)
        for m in URL_PATH_RE.finditer(src):
            add(m.group(1), '', m.start() if is_text else None, False)
    return sorted(out.values(), key=lambda x: (x['pos'] is None, x['pos'] or 0))


# ------------------------------------------------------------------ what kind of email

RETURN_RE = re.compile(r'(?i)\breturn(?:s|ed)?\b|\brefund')
CANCEL_RE = re.compile(r'(?i)\bcancel')
DELIVERED_RE = re.compile(r'(?i)\bdelivered\b|\bhas arrived\b|\bpackage arrived\b|\bitems? arrived\b|\bwas left\b')
OUT_RE = re.compile(r'(?i)out for delivery|should arrive by \d|arrive by \d{1,2}(?::\d\d)?\s*[ap]m|scheduled to arrive by')
DELAYED_RE = re.compile(r'(?i)\bdelayed\b|running late')
SHIPPED_RE = re.compile(r'(?i)\bshipped\b|\bon (?:its|the) way\b|\bshipment\b|\btracking\b|\barriving\b|\bheaded your way\b|'
                        r'\bpackage\b.*\barrive')
STOP_RE = re.compile(r'(?i)^\s*(?:recently viewed|you might also like|recommended for you|you may also like|waiting to ship|'
                     r'customers also|more to explore|see more|shop all|(?:order )?items not (?:part of this shipment|shipped)|'
                     r'not yet shipped|still processing)\b')
MORE_RE = re.compile(r'(?i)more to come|split into multiple|part of your order|part \d+ of \d+|items? from your order have shipped|'
                     r'may be shipped in multiple|^\s*en route\b', re.M)
# The store says the order is complete (Woot: "This package completes your order"): no other box is coming.
COMPLETE_RE = re.compile(r'(?i)this package completes your order|completes your order|your (?:whole|entire) order (?:has been|was) delivered')


def email_state(subject: str, text: str):
    """'delivered' | 'out_for_delivery' | 'delayed' | 'shipped' | None (not a shipping notice)."""
    head = '\n'.join((text or '').splitlines()[:12])
    for src in (subject or '', head):
        for state, rx in (('delivered', DELIVERED_RE), ('out_for_delivery', OUT_RE), ('delayed', DELAYED_RE), ('shipped', SHIPPED_RE)):
            if rx.search(src):
                return state
    return None


def kind_of(subject: str, text: str) -> str:
    """'ship', 'return' (the buyer's return going back: never inbound), or 'not_ship'."""
    head = '\n'.join((text or '').splitlines()[:6])
    if RETURN_RE.search(subject or '') or re.search(r'(?i)\breturn (?:label|package|shipment)|your return\b', head):
        return 'return'
    if CANCEL_RE.search(subject or ''):
        return 'not_ship'   # the cancellation pass reads it
    return 'ship' if email_state(subject, text) else 'not_ship'


# ------------------------------------------------------------------ items in each package

ALT_QTY_RE = re.compile(r'(?i)\[img:\s*quantity (\d{1,4}) items?\s+(.*?)\]?\s*$')
QTY_TAIL_RE = re.compile(r'(?i)\s(?:×|x)\s?(\d{1,4})(?:\s+of\s+\d{1,4})?\s*$')   # "M3GAN Doll × 1 of 2" = 1 in this box
BARE_QTY_RE = re.compile(r'^\s*(\d{1,5})\s*$')
MONEY_LINE_RE = re.compile(r'^\s*\$\s?\d')
QTY_LINE_RE = re.compile(r'(?i)^\s*(?:qty|quantity)\s*:?\s*(\d{1,4})\b')
UNITS_RE = re.compile(r'(?i)^\s*(\d{1,4})\s+items?\s+see all')


def _fits(name: str, want: dict):
    """The one order line whose words this name fits best, or None (no fit, or two fit equally)."""
    got = _words(name)
    if not got or (len(got) < 2 and got not in want.values()):
        return None   # one word fits only a one-word title, whole ("MooMix", never the logo or moo@moomix.example)
    scores = []
    for lid, w in want.items():
        common = len(w & got)
        if len(w) < 3 and common / len(got) < 0.5:
            continue   # a short title needs the name to be mostly that title
        if w and common >= min(3, len(w)) and (common / len(w) >= 0.6 or common / len(got) >= 0.8):
            scores.append((common / len(w) + common / len(got), lid))
    if not scores:
        return None
    scores.sort(key=lambda s: -s[0])
    if len(scores) > 1 and scores[0][0] - scores[1][0] < 0.05:
        return None
    return scores[0][1]


def item_mentions(text: str, titles: dict) -> list:
    """[(pos, line_id, qty or None)] for each place the email names one of the order's lines."""
    want = {lid: _words(t) for lid, t in titles.items()}
    out, pos = [], 0
    lines = (text or '').split('\n')
    for k, ln in enumerate(lines):
        here = pos
        pos += len(ln) + 1
        if STOP_RE.match(ln):
            break
        if '...' in ln or '…' in ln:
            continue   # a cut-off headline ("LEGO Star Wars Kylo Re... +2 items") names nothing for sure
        qty = None
        m = ALT_QTY_RE.search(ln)
        if m:
            qty, name = int(m.group(1)), m.group(2)
        else:
            name = re.sub(r'\[img:[^\]]*\]', '', ln).strip()
            if not name and '[img:' in ln:
                continue   # a bare image: its alt text repeats the name on the next line
            t = QTY_TAIL_RE.search(name)
            if t:
                qty, name = int(t.group(1)), name[:t.start()]
        lid = _fits(name, want)
        if lid is None:
            continue
        if qty is None:
            for j in range(k + 1, min(k + 6, len(lines))):
                nxt = lines[j]
                if STOP_RE.match(nxt) or _fits(re.sub(r'\[img:[^\]]*\]', '', nxt), want) is not None:
                    break
                q = QTY_LINE_RE.match(nxt)
                if q:
                    qty = int(q.group(1))
                    break
                b = BARE_QTY_RE.match(nxt)
                if b and j + 1 < len(lines) and MONEY_LINE_RE.match(lines[j + 1]):
                    qty = int(b.group(1))   # a table: name, item number, qty, price (The Boxery)
                    break
        out.append((here, lid, qty))
    return out


def _cut(text: str) -> str:
    """The email up to the first list of items that are not in a package."""
    pos = 0
    for ln in (text or '').split('\n'):
        if STOP_RE.match(ln):
            return text[:pos]
        pos += len(ln) + 1
    return text or ''


def parse(subject: str, text: str, html: str, titles: dict, qtys: dict = None, order_number: str = '') -> dict:
    """One shipping email -> {kind, state, packages: [{tracking_id, carrier, items: {line_id: qty}}], loose: {line_id: qty},
    expected_on, more}. titles/qtys: the order's live lines {line_id: title} / {line_id: qty}."""
    kind = kind_of(subject, text)
    res = {'kind': kind, 'state': None, 'packages': [], 'loose': {}, 'expected_on': None, 'more': False, 'complete': False}
    if kind != 'ship':
        return res
    res['state'] = email_state(subject, text)
    res['complete'] = bool(COMPLETE_RE.search(f'{subject}\n{text}'))
    res['more'] = not res['complete'] and bool(MORE_RE.search(f'{subject}\n{text}'))
    body = _cut(text)
    upcs = re.findall(r'(?i)\b(?:upc|item#?|sku)\s*:?\s*([0-9A-Z-]{6,})', body)
    trk = tracking_numbers(body, html, not_these=[order_number, *upcs])
    ment = item_mentions(body, titles)
    # Several mentions of one line in one package's part: the one with a quantity wins.
    pkgs = [{'tracking_id': t['tracking_id'], 'carrier': t['carrier'], 'items': {}} for t in trk]
    placed = [t for t in trk if t['pos'] is not None]

    def put(target: dict, lid, qty):
        if lid not in target or (target[lid] is None and qty is not None):
            target[lid] = qty

    if not pkgs:
        for _, lid, qty in ment:
            put(res['loose'], lid, qty)
    elif len(pkgs) == 1 or not placed:
        for _, lid, qty in ment:
            put(pkgs[0]['items'], lid, qty)
    else:
        before = bool(ment) and ment[0][0] < placed[0]['pos']   # Walmart: a package's items come before its number
        by_id = {p['tracking_id']: p for p in pkgs}
        for pos, lid, qty in ment:
            if before:
                t = next((t for t in placed if t['pos'] > pos), None)
            else:
                t = next((t for t in reversed(placed) if t['pos'] < pos), None)
            if t:
                put(by_id[t['tracking_id']]['items'], lid, qty)
    # One package, one line: the line is in it. One package holding as many units as the whole order: every line is.
    m = [UNITS_RE.match(ln) for ln in body.split('\n')]
    units = [int(x.group(1)) for x in m if x]
    if len(pkgs) == 1 and qtys:
        items = pkgs[0]['items']
        if len(qtys) == 1:
            lid = next(iter(qtys))
            items[lid] = items.get(lid) or (units[0] if len(units) == 1 else None)
        elif len(units) == 1 and units[0] == sum(qtys.values()) and not res['more']:
            for lid, q in qtys.items():
                items[lid] = q
    for p in pkgs:
        for lid in p['items']:
            if p['items'][lid] is None and qtys and len(pkgs) == 1:
                p['items'][lid] = qtys.get(lid)
    res['packages'] = pkgs
    res['expected_on'] = _expected(body)
    return res


EXPECT_RE = re.compile(r'(?i)\b(?:arrives?|arriving|estimated to arrive on|arrive (?:on|by)|should arrive by|delivery by|arrives by)'
                       r'\s*:?\s*(?:mon|tue|wed|thu|fri|sat|sun)?[a-z]*\.?,?\s*'
                       r'((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:,?\s+\d{4})?)')


def _expected(text: str):
    m = EXPECT_RE.search(text or '')
    return m.group(1) if m else None


# ------------------------------------------------------------------ saving


def _day(raw, sent_day: str):
    from inbound import _one_date
    if not raw:
        return None
    try:
        seen = date.fromisoformat(sent_day)
    except (TypeError, ValueError):
        return None
    d = _one_date(raw.replace('.', ''), seen, past=False)
    return d.isoformat() if d else None


class ShipMail:
    """Shipping emails of stores Boxloft has Gmail orders from -> mail_package (+ items), order state forward only."""

    def __init__(self, wms):
        self.wms = wms
        wms._conn().executescript(SCHEMA)

    def find_order(self, store: str, subject: str, text: str):
        """The one Gmail order of this store whose number the email prints (take_cancel's rule), else None."""
        toks = {re.sub(r'[^0-9a-z]', '', t.lower()) for t in TOKEN_RE.findall(f'{subject}\n{text}')}
        match = [o for o in self.wms._q("SELECT id, external_id, hidden FROM purchase_order WHERE source = ? AND account = 'gmail'", (store,))
                 if len(re.sub(r'[^0-9a-z]', '', o['external_id'].lower())) >= 5
                 and re.sub(r'[^0-9a-z]', '', o['external_id'].lower()) in toks]
        return match[0] if len(match) == 1 else None

    def take(self, message_id: str, sender: str, subject: str, sent_at, text: str, html: str = '') -> dict:
        """Read one shipping email (once). Returns {'outcome', 'po_id', 'tracking'}."""
        if self.wms._q1('SELECT 1 AS x FROM ship_mail WHERE message_id = ?', (message_id,)):
            return {'outcome': 'seen', 'po_id': None, 'tracking': []}
        store = store_of(sender)
        po, res, outcome = None, None, None
        if self.wms._q1('SELECT 1 AS x FROM store_skip WHERE store = ?', (store,)):
            outcome = 'skipped_store'
        else:
            kind = kind_of(subject, text)
            if kind != 'ship':
                outcome = kind
            else:
                o = self.find_order(store, subject, text)
                if not o:
                    res = parse(subject, text, html, {}, {})
                    outcome = 'no_order'
                elif o['hidden']:
                    po, outcome = o['id'], 'hidden'
                else:
                    po = o['id']
        tracking = []
        if outcome is None:
            with self.wms.op_lock:   # never between another save's check and its write
                res, outcome = self._save(po, message_id, subject, sent_at, text, html, store)
            tracking = [p['tracking_id'] for p in res['packages']]
        with self.wms._tx() as c:
            c.execute("""INSERT OR IGNORE INTO ship_mail (message_id, at, sent_at, sender, subject, outcome, po_id, result)
                         VALUES (?,?,?,?,?,?,?,?)""", (message_id, now_utc(), sent_at, (sender or '')[:200], (subject or '')[:300],
                                                       outcome, po, json.dumps(res) if res else None))
        return {'outcome': outcome, 'po_id': po, 'tracking': tracking}

    def _save(self, po: int, message_id: str, subject: str, sent_at, text: str, html: str, store: str) -> tuple:
        day = et_day(sent_at) or now_utc()[:10]
        with self.wms._tx() as c:
            o = dict(zip(('external_id', 'state', 'delivered_on'),
                         c.execute('SELECT external_id, state, delivered_on FROM purchase_order WHERE id = ?', (po,)).fetchone()))
            lines = c.execute('SELECT id, title, qty FROM purchase_line WHERE po_id = ? AND cancelled = 0', (po,)).fetchall()
            line_qty = {r[0]: r[2] for r in lines}
            res = parse(subject, text, html, {r[0]: r[1] for r in lines}, line_qty, o['external_id'])
            state = res['state']
            expected = _day(res['expected_on'], day)
            for p in res['packages']:
                row = c.execute('SELECT id, state FROM mail_package WHERE po_id = ? AND tracking_id = ?', (po, p['tracking_id'])).fetchone()
                if row:
                    pid, old = row
                    new = state if STATE_RANK.get(state, 0) >= STATE_RANK.get(old, 0) else old
                    c.execute("""UPDATE mail_package SET state = ?, carrier = CASE WHEN carrier = '' THEN ? ELSE carrier END,
                                     expected_on = COALESCE(?, expected_on), message_id = ?, seen_at = ?,
                                     delivered_on = CASE WHEN ? = 'delivered' THEN COALESCE(delivered_on, ?) ELSE delivered_on END
                                 WHERE id = ?""", (new, p['carrier'], expected, message_id, now_utc(), new, day, pid))
                else:
                    pid = c.execute("""INSERT INTO mail_package (po_id, tracking_id, carrier, state, shipped_on, expected_on, delivered_on,
                                                                 message_id, seen_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                                    (po, p['tracking_id'], p['carrier'], state, day, expected,
                                     day if state == 'delivered' else None, message_id, now_utc())).lastrowid
                for lid, qty in p['items'].items():
                    qty = min(int(qty or 0), line_qty.get(lid, 0))   # a box never holds more of a line than was bought
                    if qty:
                        c.execute("""INSERT INTO mail_package_line (package_id, line_id, qty) VALUES (?,?,?)
                                     ON CONFLICT(package_id, line_id) DO UPDATE SET qty = excluded.qty""", (pid, lid, int(qty)))
            outcome = 'tracking' if res['packages'] else 'no_tracking'
            self._order_state(c, po, o, state, res, expected, day, store)
        return res, outcome

    def _order_state(self, c, po: int, o: dict, state: str, res: dict, expected, day: str, store: str):
        """Move the order forward only: ordered -> shipped -> delivered (when the delivered packages hold every unit)."""
        if o['state'] not in ('ordered', 'shipped'):
            return
        lines = {r[0]: r[1] for r in c.execute('SELECT id, qty FROM purchase_line WHERE po_id = ? AND cancelled = 0', (po,))}
        pk = c.execute('SELECT id, tracking_id, carrier, state FROM mail_package WHERE po_id = ?', (po,)).fetchall()
        got = {}
        for pid, _, _, st in pk:
            if st == 'delivered':
                for lid, q in c.execute('SELECT line_id, qty FROM mail_package_line WHERE package_id = ?', (pid,)):
                    got[lid] = got.get(lid, 0) + q
        if state == 'delivered' and not res['packages'] and not res['more']:
            for lid, q in res['loose'].items():   # a delivered email with no number that names every unit (Stanley)
                got[lid] = got.get(lid, 0) + (q or 0)
        whole = bool(lines) and all(got.get(lid, 0) >= q for lid, q in lines.items())
        whole = whole or (state == 'delivered' and res['complete'])
        all_done = all(st == 'delivered' for _, _, _, st in pk)
        trk = ', '.join(f'{car} {t}'.strip() for _, t, car, _ in pk[:3])
        if state == 'delivered' and whole and (all_done or res['complete']):
            new, text = 'delivered', f'Delivered (email from {store})'
        elif state in ('shipped', 'out_for_delivery', 'delayed', 'delivered') and o['state'] == 'ordered':
            new, text = 'shipped', f"Shipped{': ' + trk if trk else ''} (email from {store})"
        elif o['state'] == 'shipped' and expected and state in ('shipped', 'out_for_delivery', 'delayed'):
            c.execute('UPDATE purchase_order SET expected_from = ?, expected_to = ?, last_seen_at = ? WHERE id = ?',
                      (expected, expected, now_utc(), po))
            return
        else:
            return
        if new == o['state']:
            return
        c.execute("""UPDATE purchase_order SET state = ?, status_text = ?, status_changed_at = ?, last_seen_at = ?,
                         expected_from = COALESCE(?, expected_from), expected_to = COALESCE(?, expected_to),
                         delivered_on = CASE WHEN ? = 'delivered' THEN ? ELSE delivered_on END WHERE id = ?""",
                  (new, text[:200], now_utc(), now_utc(), expected, expected, new, day, po))
        c.execute("""INSERT INTO purchase_event (po_id, at, state, status_text, expected_from, expected_to) VALUES (?,?,?,?,?,?)""",
                  (po, now_utc(), new, text[:200], expected, expected))

    # ---- what other screens read

    @staticmethod
    def tracks(wms, po_id: int) -> list:
        """Arriving: one row per package an email named (carrier, tracking, state, items)."""
        out = []
        for n, p in enumerate(wms._q("""SELECT id, tracking_id, carrier, state, shipped_on, expected_on, delivered_on, seen_at
                                           FROM mail_package WHERE po_id = ? ORDER BY id""", (po_id,)), 1):
            items = wms._q("""SELECT pl.qty, l.title FROM mail_package_line pl JOIN purchase_line l ON l.id = pl.line_id
                               WHERE pl.package_id = ? ORDER BY l.id""", (p['id'],))
            word = {'delivered': 'Delivered', 'out_for_delivery': 'Out for delivery', 'delayed': 'Delayed'}.get(p['state'], 'Shipped')
            when = p['delivered_on'] if p['state'] == 'delivered' else p['shipped_on']
            try:
                when = date.fromisoformat(when).strftime('%b %-d') if when else ''
            except ValueError:
                pass
            note = f"{word} {when}".strip() + ' (store email)'
            out.append({'pkg': n, 'carrier': p['carrier'], 'tracking_id': p['tracking_id'], 'note': note,
                        'headline': ', '.join(f"{i['qty']} x {i['title'][:40]}" for i in items), 'read_at': p['seen_at'],
                        'last_event': None, 'photo': None})
        return out

    # ---- Gmail pass

    def ship_pass(self, m, days: int, stores: set, progress=None) -> dict:
        """Shipping emails on an open IMAP connection. Bodies are fetched only from stores Boxloft has orders from."""
        import email
        import email.header
        import email.utils
        counts = {}
        q = (SHIP_QUERY + f' newer_than:{int(days)}d').replace('"', '\\"')
        typ, d = m.uid('SEARCH', 'X-GM-RAW', f'"{q}"')
        uids = d[0].split() if typ == 'OK' and d and d[0] else []
        for n, uid in enumerate(uids, 1):
            if progress:
                progress(f'shipping emails: {n} of {len(uids)}')
            typ, hd = m.uid('FETCH', uid, '(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT DATE)])')
            hdr = email.message_from_bytes(hd[0][1]) if typ == 'OK' and hd and hd[0] else None
            if hdr is None:
                continue
            mid = (hdr.get('Message-ID') or f'uid-{uid.decode()}').strip()
            if self.wms._q1('SELECT 1 AS x FROM ship_mail WHERE message_id = ?', (mid,)):
                counts['seen'] = counts.get('seen', 0) + 1
                continue
            if store_of(hdr.get('From', '')) not in stores:
                counts['other_sender'] = counts.get('other_sender', 0) + 1
                continue   # not recorded: an order from that store may come later
            subj = str(email.header.make_header(email.header.decode_header(hdr.get('Subject', ''))))
            sent = None
            try:
                sent = email.utils.parsedate_to_datetime(hdr.get('Date')).astimezone(
                    __import__('datetime').timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            except Exception:  # noqa: BLE001
                pass
            typ, md = m.uid('FETCH', uid, '(BODY.PEEK[])')
            if typ != 'OK' or not md or not md[0]:
                continue
            msg = email.message_from_bytes(md[0][1])
            from store_mail import mail_html
            r = self.take(mid, hdr.get('From', ''), subj, sent, mail_text(msg), mail_html(msg))
            counts[r['outcome']] = counts.get(r['outcome'], 0) + 1
        return counts
