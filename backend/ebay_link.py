"""eBay listings become Boxloft items (Alex 11-12, reviewed by Fable before the build).

Sam photographs an item and lists it on eBay from her phone. She never copies a SKU between the two apps: the link
lives here, keyed on eBay's item number (listing.sku = ITEM-<id>, which every eBay sale carries too).

- Every live eBay listing with no product gets one by itself (`make_missing`, after each listings read), unless its
  title scores GUESS_MIN or more against something Boxloft already has. Those wait for a person to say "same item"
  or "new item" (`candidates`, `match`): a title alone never links (the Camco rule).
- A product made from a listing carries the listing's ITEM-<id> as an EBAY_ITEM identifier, so its sales find it after
  the listing ends. A relist gets a new item number; its title matches the old product, so a person links it once.
- Box page strip (`to_box`): live eBay listings that offer more units than the item's E boxes hold. One tap puts the
  missing units in the box (`put`, sorting.put underneath), refused with who when someone filled it meanwhile.
- The backlog: listings first read on eBay's first read day were for sale before Boxloft knew them. Their units sit
  in no box Boxloft knows, so when eBay ships one and nothing here ever held the item, the sale closes by itself as
  supplier -> customer with no cost (`backlog_ship`, used by Audit.ship_unscanned). Anything that ever had a unit in a
  box stays a person's call.
Nothing here edits a stock move. Products are made only through Audit._make_product.
"""
import json
import math
import re
import threading

from listings import ASIN_RE, _words, is_live
from sorting import market_of
from wms import Conflict, WmsError, now_utc

GUESS_MIN = 0.5      # a title this close to something we have waits for a person (Fable 11-12: 110 listings at 0.5-0.75)
# ... and rare shared words must carry half the weight too (Alex 11-13). On live 11-13 the plain score alone offered
# "Marvel Dazzler Action Figure" for a Transformers Optimus Prime: "action figure" is on hundreds of titles. Of 181
# guesses, weighted 0.6+ were 30 (almost all right), 0.5-0.59 27 (mixed), under 0.5 124 (another figure of the same
# brand, or noise).
WEIGHT_MIN = 0.5
STRIP_MAX = 40

SCHEMA = """
CREATE TABLE IF NOT EXISTS ebay_made (
    product_id INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL,
    how TEXT NOT NULL CHECK (how IN ('auto', 'person')),
    user_id INTEGER,
    at TEXT NOT NULL
);
"""


def _score(wa: set, wb: set) -> float:
    """listings.title_score on precomputed word sets."""
    if len(wa) < 3 or len(wb) < 3:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


class Pool(list):
    """The guess pool, with the word weights of the titles it was read with."""
    def __init__(self, items, idf):
        super().__init__(items)
        self.idf = idf


def _idf(titles) -> dict:
    """word -> inverse document frequency over every title Boxloft knows: rare words weigh more."""
    df = {}
    for t in titles:
        for w in _words(t or ''):
            df[w] = df.get(w, 0) + 1
    n = max(1, len(titles))
    return {w: math.log((n + 1) / (k + 1)) + 1 for w, k in df.items()} | {'': math.log(n + 1) + 1}


def _weighted(wa: set, wb: set, idf: dict) -> float:
    """Shared words' weight over the lighter title's weight (an unseen word is as rare as can be)."""
    rare = idf.get('', 1.0)
    sa, sb = sum(idf.get(w, rare) for w in wa), sum(idf.get(w, rare) for w in wb)
    den = min(sa, sb)
    return sum(idf.get(w, rare) for w in wa & wb) / den if den else 0.0


def item_number(sku: str) -> str:
    """ITEM-336808944152 or ITEM-336808944152/RED -> 336808944152."""
    return (sku or '').split('-', 1)[-1].split('/', 1)[0]


class EbayLink:
    def __init__(self, wms, commerce, audit, listings, sorting):
        self.wms, self.commerce, self.audit, self.listings, self.sorting = wms, commerce, audit, listings, sorting
        self.wms._conn().executescript(SCHEMA)
        audit.backlog_ship = self.backlog_ship
        commerce.sold_before_boxloft = self.sold_before_boxloft
        self._made_lock = threading.Lock()

    # ------------------------------------------------------------------ read

    def backlog_until(self) -> str:
        """The first eBay listings read: what it saw was listed before Boxloft knew eBay."""
        r = self.wms._q1("SELECT MIN(first_seen) AS t FROM listing WHERE channel = 'ebay'")
        return (r or {}).get('t') or ''

    def _live(self) -> list:
        rows = self.listings._linked(self.wms._q("SELECT * FROM listing WHERE channel = 'ebay' AND gone_at IS NULL"))
        return [r for r in rows if is_live(r)]

    DELIVERED_SQL = """(o.state = 'delivered' OR EXISTS (SELECT 1 FROM purchase_package pk
                                                          WHERE pk.line_id = l.id AND pk.state = 'delivered'))"""

    def _pool(self) -> list:
        """What a listing's title is compared with (Alex 11-12 10:41 PM ET: "items received as well. Anything in the
        audit. Anything currently listed on amazon or Walmart. Anything in a box. Anything marked delivered even if not
        put away yet."). Never another eBay listing (Alex 11-13): a product tied to a live eBay listing is that
        listing's, and an item made from a listing stays out until a unit goes in a box and the listing is gone.
        One candidate per thing; every reason it counts goes in its `why` (two at most).
        - products received (a receipt not undone), with units in a box or on a shelf, or with a delivered buy not
          scanned in yet;
        - every Stock audit row: its product if it has one, else the row;
        - live Amazon and Walmart listings (listings.is_live): their product, else the listing itself (kind 'listing');
        - delivered buys (any store) with no product yet, units not scanned in."""
        from wms import LINE_LEFT_SQL
        live_ebay = "SELECT 1 FROM listing li WHERE li.product_id = p.id AND li.channel = 'ebay' AND li.gone_at IS NULL"
        ent, by_asin = {}, {}   # key -> candidate; ASIN -> key of the candidate that stands for it

        def add(key, kind, id_, title, sku, image_url, why, asin=''):
            e = ent.get(key)
            if not e:
                e = ent[key] = {'kind': kind, 'id': id_, 'title': title, 'sku': sku, 'image_url': image_url,
                                'whys': [], 'words': _words(title)}
            if why and why not in e['whys']:
                e['whys'].append(why)
            if asin:
                by_asin.setdefault(asin.upper(), key)

        prods = {p['id']: p for p in self.wms._q(f"""
                SELECT p.id, p.title, p.sku, p.image_url,
                       (SELECT COALESCE(SUM(s.qty), 0) FROM stock_level s LEFT JOIN location lo ON lo.id = s.location_id
                         WHERE s.product_id = p.id AND s.qty > 0 AND (s.box_id != 0 OR lo.kind != 'virtual')) AS on_hand,
                       EXISTS (SELECT 1 FROM receipt r JOIN action a ON a.id = r.action_id
                                WHERE r.product_id = p.id AND a.reversed_by IS NULL) AS received,
                       EXISTS (SELECT 1 FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id
                                WHERE l.product_id = p.id AND l.cancelled = 0 AND o.hidden = 0 AND o.state != 'cancelled'
                                  AND {self.DELIVERED_SQL} AND {LINE_LEFT_SQL} > 0) AS waiting
                  FROM product p WHERE p.archived = 0 AND NOT EXISTS ({live_ebay})""")}
        asin_pid, pid_asin = {}, {}
        for r in self.wms._q("SELECT product_id, value FROM product_identifier WHERE kind = 'ASIN' ORDER BY id"):
            asin_pid.setdefault((r['value'] or '').upper(), r['product_id'])
            pid_asin.setdefault(r['product_id'], (r['value'] or '').upper())

        def add_product(pid, why):
            p = prods.get(pid)   # None: archived, or another eBay listing's item (never offered)
            if p:
                add(('product', pid), 'product', pid, p['title'], p['sku'], p['image_url'], why, pid_asin.get(pid, ''))

        for p in prods.values():
            if p['on_hand']:
                add_product(p['id'], f"{p['on_hand']} on the shelves")
            elif p['received']:
                add_product(p['id'], 'Received, none on the shelves now')
            elif p['waiting']:
                add_product(p['id'], 'Delivered, not scanned in yet')

        line_pid = {r['id']: r['product_id'] for r in self.wms._q(
            'SELECT l.id, l.product_id FROM purchase_line l JOIN audit_item ai ON ai.line_id = l.id WHERE l.product_id IS NOT NULL')}
        for r in self.wms._q("SELECT id, asin, title, image_url, line_id FROM audit_item WHERE asin != ''"):
            asin = r['asin'].upper()
            pid = asin_pid.get(asin) or line_pid.get(r['line_id'])
            if pid:
                add_product(pid, 'On the Stock audit')
                continue
            add(('audit', r['id']), 'audit', r['id'], r['title'], r['asin'], r['image_url'],
                'On the Stock audit: bought, not found yet', '' if asin.startswith('LINE-') else asin)

        for r in self.wms._q(f"""
                SELECT l.id, l.title, l.asin, l.image_url, o.source, o.external_id,
                       COALESCE(o.delivered_on, (SELECT MAX(pk.delivered_on) FROM purchase_package pk WHERE pk.line_id = l.id)) AS delivered_on
                  FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id
                 WHERE l.product_id IS NULL AND l.cancelled = 0 AND o.hidden = 0 AND COALESCE(o.review, '') = '' AND o.kind != 'supplies'
                   AND o.state != 'cancelled' AND {self.DELIVERED_SQL} AND {LINE_LEFT_SQL} > 0"""):
            asin = (r['asin'] or '').upper()
            if asin and (asin in by_asin or asin in asin_pid):
                continue   # its audit row or its item stands for it
            store = 'Amazon' if r['source'] == 'amazon' else r['source']
            add(('line', r['id']), 'line', r['id'], r['title'], r['external_id'], r['image_url'],
                f"Delivered{' ' + r['delivered_on'][:10] if r['delivered_on'] else ''} ({store}), not scanned in yet", asin)

        for li in self.listings._linked(self.wms._q(
                "SELECT * FROM listing WHERE channel IN ('amazon', 'walmart') AND gone_at IS NULL ORDER BY id")):
            if not is_live(li):
                continue
            where = f"Amazon {li['fulfillment']}" if li['channel'] == 'amazon' else 'Walmart'
            why = f"Listed on {where}, {li['qty']} for sale"
            asin = (li['asin'] or '').upper()
            if li['product_id']:
                add_product(li['product_id'], why)
            elif asin and asin in by_asin:
                add(by_asin[asin], None, None, '', '', None, why)
            else:
                add(('listing', li['id']), 'listing', li['id'], li['title'], li['sku'], li.get('image_url'), why, asin)

        out = []
        for e in ent.values():
            e['why'] = ' · '.join(e.pop('whys')[:2])
            out.append(e)
        titles = [r['title'] for q in ('SELECT title FROM listing', 'SELECT title FROM product', 'SELECT title FROM audit_item',
                                       'SELECT title FROM purchase_line') for r in self.wms._q(q)]
        return Pool(out, _idf(titles))

    @staticmethod
    def _best(title: str, pool: list, n: int = 3) -> list:
        """Guesses for a listing title: the plain score reaches GUESS_MIN and the rare-word weight WEIGHT_MIN; best
        weight first."""
        w = _words(title)
        idf = getattr(pool, 'idf', None)
        hits = []
        for p in pool:
            s = _score(w, p['words'])
            if s < GUESS_MIN:
                continue
            k = _weighted(w, p['words'], idf) if idf else s
            if k >= WEIGHT_MIN:
                hits.append((round(k, 2), p))
        hits = sorted(hits, key=lambda h: -h[0])[:n]
        return [{k: p.get(k) for k in ('kind', 'id', 'title', 'sku', 'image_url', 'why')} | {'score': s} for s, p in hits]

    def _in_e_boxes(self) -> dict:
        """product id -> units in E boxes (the eBay market)."""
        out = {}
        for r in self.wms._q("""SELECT s.product_id, b.name, SUM(s.qty) AS n FROM stock_level s JOIN box b ON b.id = s.box_id
                                 WHERE s.box_id != 0 AND s.qty > 0 GROUP BY s.product_id, b.id"""):
            if market_of(r['name'] or '') == 'E':
                out[r['product_id']] = out.get(r['product_id'], 0) + r['n']
        return out

    def to_match(self) -> dict:
        """Live eBay listings with no product: a person says same item (which) or new item."""
        pool = self._pool()
        rows = [r for r in self._live() if not r['product_id']]
        rows.sort(key=lambda r: (r['opened_on'] or '', r['id']), reverse=True)
        return {'listings': [self._slim(r) | {'candidates': self._best(r['title'], pool)} for r in rows]}

    def to_box(self, q: str = '', limit: int = STRIP_MAX) -> dict:
        """Box page strip: live eBay listings that offer more units than the item's E boxes hold, newest listing first.
        A listing with no product yet comes with its title guesses (a person answers before it goes in)."""
        inbox = self._in_e_boxes()
        words = _words(q)
        rows = []
        for r in self._live():
            short = r['qty'] - inbox.get(r['product_id'], 0) if r['product_id'] else r['qty']
            if short <= 0:
                continue
            if words and not words <= _words(r['title']):
                continue
            rows.append(self._slim(r) | {'short': short, 'in_boxes': inbox.get(r['product_id'], 0) if r['product_id'] else 0})
        rows.sort(key=lambda r: (r['opened_on'] or '', r['listing_row']), reverse=True)
        total = len(rows)
        rows = rows[:limit]
        if any(not r['product_id'] for r in rows):
            pool = self._pool()
            for r in rows:
                if not r['product_id']:
                    r['candidates'] = self._best(r['title'], pool)
        units = sum(r['short'] for r in rows)
        return {'listings': rows, 'total': total, 'units_shown': units}

    @staticmethod
    def _slim(r: dict) -> dict:
        return {'listing_row': r['id'], 'item_id': item_number(r['sku']), 'sku': r['sku'], 'title': r['title'],
                'image_url': r.get('image_url'), 'qty': r['qty'], 'price_cents': r['price_cents'],
                'opened_on': r['opened_on'], 'product_id': r['product_id'],
                'url': f"https://www.ebay.com/itm/{item_number(r['sku'])}"}

    def gaps(self) -> dict:
        """Overview (Fable 11-12): both should be 0. Listings waiting for a person, and eBay orders the marketplace
        shipped that are not closed here."""
        unmatched = sum(1 for r in self._live() if not r['product_id'])
        shipped = self.wms._q1(f"""SELECT COUNT(*) AS n FROM sales_order WHERE channel = 'ebay' AND status = 'open'
                                     AND review LIKE ?""", (self.commerce.MARKED_SHIPPED[:24] + '%',))['n']
        return {'unmatched': unmatched, 'shipped_open': shipped}

    # ------------------------------------------------------------------ write

    def _make(self, li: dict, user_id: int, how: str, save_photo: bool = True) -> int:
        pid = self.audit._make_product('', li['title'], '', li.get('image_url') or '', None, user_id,
                                       base=f"EBAY-{item_number(li['sku'])}",
                                       extra_idents=[{'kind': 'EBAY_ITEM', 'value': li['sku']}], save_photo=save_photo)
        with self.wms._tx() as c:
            c.execute('UPDATE listing SET product_id = ? WHERE id = ? AND product_id IS NULL', (pid, li['id']))
            c.execute('INSERT OR IGNORE INTO ebay_made (product_id, listing_id, how, user_id, at) VALUES (?,?,?,?,?)',
                      (pid, li['id'], how, user_id, now_utc()))
        return pid

    def make_missing(self, user_id: int = 0) -> dict:
        """After a listings read: every live listing with no product and no title guess gets its product. Photos are
        copied one at a time afterwards (the first read makes ~250; one thread each would be 250 downloads at once)."""
        with self._made_lock, self.wms.op_lock:
            pool = self._pool()
            made, waiting = [], 0
            for li in self._live():
                if li['product_id']:
                    continue
                if self._best(li['title'], pool, 1):
                    waiting += 1
                    continue
                pid = self._make(li, user_id, 'auto', save_photo=False)
                made.append((pid, li.get('image_url')))
                # Not added to the pool: an item made from a listing is another listing's, never a guess (Alex 11-13)
        if made and self.audit.catalog:
            def photos(todo=made):
                try:
                    for pid, url in todo:
                        if url:
                            try:
                                self.audit.catalog.save_image(pid, url, user_id)
                            except Exception:  # noqa: BLE001 - the product shows eBay's link until a copy is in
                                pass
                finally:
                    self.wms.release()
            threading.Thread(target=photos, daemon=True).start()
        return {'made': len(made), 'waiting': waiting}

    def match(self, listing_row: int, user_id: int, product_id: int = None, item_id: int = None, new: bool = False,
              user_names: dict = None, line_id: int = None, market_listing: int = None) -> dict:
        """A person answers a listing: same as this product, same as this Stock audit row, same as this delivered buy
        (the item is made from the buy line, as Receive does), same as this Amazon or Walmart listing (the item is
        made from that listing and both listings point at it), or a new item.
        A listing someone already answered is refused with who (409)."""
        names = user_names or {}
        li = self.wms._q1('SELECT * FROM listing WHERE id = ?', (int(listing_row),))
        if not li or li['channel'] != 'ebay' or li['gone_at']:
            raise WmsError('That eBay listing ended; refresh the page')
        li = self.listings._linked([li])[0]
        if li['product_id']:
            m = self.wms._q1('SELECT user_id, at FROM ebay_made WHERE listing_id = ? ORDER BY at DESC LIMIT 1', (li['id'],)) or \
                self.wms._q1('SELECT user_id, at FROM listing_link WHERE listing_id = ? AND undone_at IS NULL ORDER BY id DESC LIMIT 1',
                             (li['id'],))
            who = f"{names.get(m['user_id'], 'Boxloft' if not m['user_id'] else 'Someone')} answered it" if m else 'It was answered'
            raise Conflict(f'{who} already. Nothing was saved: the screen now shows the new version.')
        if new:
            pid = self._make(li, user_id, 'person')
            return {'product_id': pid, 'made': True}
        if item_id:
            it = self.audit.item(int(item_id))
            if not it:
                raise WmsError('That Stock audit row is gone; refresh the page')
            product_id = self.audit._product_for(it, user_id)
        if line_id and not product_id:
            product_id = self.audit.product_for_purchase_line(int(line_id), user_id)['product_id']
        if market_listing and not product_id:
            product_id = self._product_for_listing(int(market_listing), user_id, names)
        if not product_id:
            raise WmsError('Pick the item, or New item')
        out = self.listings.link(int(product_id), li['id'], user_id, None, names)
        return {'product_id': int(product_id), 'made': False, 'link_id': out['link_id']}

    def _product_for_listing(self, listing_row: int, user_id: int, names: dict) -> int:
        """The item for a live Amazon or Walmart listing: the one it already points at, else one made from it (ASIN
        as the SKU, as the audit makes them; the catalog adds barcodes and size), and the listing then points at it."""
        m = self.wms._q1('SELECT * FROM listing WHERE id = ?', (listing_row,))
        if not m or m['channel'] == 'ebay' or m['gone_at']:
            raise WmsError('That listing is gone; refresh the page')
        m = self.listings._linked([m])[0]
        if m['product_id']:
            return m['product_id']
        asin = (m['asin'] or '').upper() if ASIN_RE.match((m['asin'] or '').upper()) else ''
        pid = self.audit._make_product(asin, m['title'], '', m.get('image_url') or '', None, user_id, lookup=bool(asin),
                                       base=f"{m['channel'].upper()}-{m['sku']}")
        if not self.listings._linked([self.wms._q1('SELECT * FROM listing WHERE id = ?', (listing_row,))])[0]['product_id']:
            self.listings.link(pid, listing_row, user_id, None, names)   # no ASIN to find it by: tie it by hand
        return pid

    def put(self, to: dict, listing_row: int, qty: int, user_id: int, device: str = '', scan_id: str = '',
            seen_short: int = None, user_names: dict = None, over_ok: bool = False) -> dict:
        """The strip's tap: qty units of the listing's item into this box (sorting.put picks the record: an audit row
        or order first, else a move from the one other place it sits, else extra stock). `seen_short` = what the strip
        showed; if someone put units in since and fewer are missing now, it is refused with who (Fable 11-12 #4)."""
        names = user_names or {}
        scan_id = (scan_id or '')[:40]
        if scan_id and self.wms._q1('SELECT 1 AS x FROM box_add WHERE scan_id = ?', (scan_id,)):
            return self.sorting.put(to, user_id, device, scan_id, product_id=0)   # a resend: the first save's answer
        li = self.wms._q1('SELECT * FROM listing WHERE id = ?', (int(listing_row),))
        if not li or li['channel'] != 'ebay':
            raise WmsError('That eBay listing is gone; refresh the page')
        li = self.listings._linked([li])[0]
        pid = li['product_id']
        if not pid:
            raise WmsError('Say what this listing is first (same item or new item)')
        qty = int(qty or 0)
        if not 1 <= qty <= 10000:
            raise WmsError('Enter how many (1 or more)')
        with self.sorting._lock:
            short = li['qty'] - self._in_e_boxes().get(pid, 0)
            if seen_short is not None and short < min(int(seen_short), qty):
                last = self.wms._q1('SELECT user_id, at FROM box_add WHERE product_id = ? ORDER BY id DESC LIMIT 1', (pid,))
                who = names.get(last['user_id'], 'Someone') if last else 'Someone'
                from audit import _et_time
                when = f" at {_et_time(last['at'])}" if last else ''
                raise Conflict(f"{who} put this item in a box{when}. {max(0, short)} still missing from the E boxes. "
                               'Nothing was saved: the strip now shows the new count.')
            return self.sorting.put(to, user_id, device, scan_id, product_id=pid, qty=qty, over_ok=over_ok)

    # ------------------------------------------------------------------ the backlog (Audit.ship_unscanned asks)

    def sold_before_boxloft(self, order: dict, line: dict) -> bool:
        """commerce.auto_ship asks about an eBay line with no item: ordered before Boxloft first read eBay's listings,
        and its listing was never live here (it sold out first). Boxloft never held that unit, so the order closes as
        the history import closes one: shipped, no stock move. A listing still live waits for a person instead."""
        until = self.backlog_until()
        if order.get('channel') != 'ebay' or not until or not (order.get('ordered_at') or '') < until[:10]:
            return False
        live = self.wms._q1("SELECT 1 AS x FROM listing WHERE channel = 'ebay' AND sku = ? AND gone_at IS NULL",
                            (line.get('sku') or '',))
        return not live

    def backlog_ship(self, product_id: int) -> bool:
        """True: an item listed on eBay before Boxloft knew eBay, made from that listing, and never in a box or on a
        shelf here. Its sale can close as supplier -> customer with no cost (Fable 11-12: only this case, so a real
        shortage, like 3 listed and 1 boxed, still goes to a person)."""
        until = self.backlog_until()
        if not until:
            return False
        m = self.wms._q1("""SELECT l.first_seen FROM ebay_made e JOIN listing l ON l.id = e.listing_id
                             WHERE e.product_id = ?""", (product_id,))
        if not m or m['first_seen'] > until:
            return False
        held = self.wms._q1("""SELECT 1 AS x FROM stock_move m LEFT JOIN location l ON l.id = m.to_location_id
                                WHERE m.product_id = ? AND (m.to_box_id != 0 OR l.kind != 'virtual') LIMIT 1""", (product_id,))
        return not held
