"""What is listed for sale right now, and which audit rows it covers.

Source of truth is the marketplace. `Listings.refresh_amazon()` reads two things from SP-API:
- Reports GET_MERCHANT_LISTINGS_ALL_DATA: every SKU on the account (active and inactive), with ASIN,
  quantity, price, status and fulfillment channel (DEFAULT = we ship it, AMAZON_NA = FBA).
- FBA Inventory getInventorySummaries: units at Amazon per SKU (fulfillable, inbound, reserved, unfulfillable).
They land in `listing`, one row per (channel, sku), refreshed in place. A SKU missing from a full report
gets `gone_at`, so a deleted listing drops off without losing its history.

Audit rows get the listings that match them as suggestions (ASIN exact, then the row's product, then UPC,
then title words). A person answers with `listing_mark` rows (append-only):
  confirm  = yes, this audit row is that listing
  not_this = wrong match; never suggest it for this row again
  manual   = listed somewhere Boxloft can't read (eBay, Mercari, ...), with platform, qty, link
  undo     = cancels the mark in `undoes`
Nothing here moves stock.
"""
import csv
import io
import json
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET

from connections import ConnectionError_, _http
from sales_sync import SCHEMA as SYNC_SCHEMA, sp_call, sp_reports
from commerce import RECEIVABLE_ORDER_SQL
from wms import Conflict, Wms, WmsError, now_utc

US_MARKETPLACE = 'ATVPDKIKX0DER'

SCHEMA = """
-- Copy SKU (Alex 11-24): the SKU an item's buy got on its first copy. Append-only: it never changes, one per item and
-- buy text, and no two items share a SKU.
CREATE TABLE IF NOT EXISTS sell_sku (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL,
    base TEXT NOT NULL,                    -- M.D.YY-price of the buy
    sku TEXT NOT NULL UNIQUE,              -- base, or base-2, base-3 ...
    day TEXT NOT NULL,
    cost_cents INTEGER NOT NULL,           -- 0: an estimated SKU with no price (Alex 11-25: always a SKU)
    ref TEXT NOT NULL DEFAULT '',
    user_id INTEGER,
    at TEXT NOT NULL,
    UNIQUE (product_id, base)
);
CREATE TRIGGER IF NOT EXISTS sell_sku_no_update BEFORE UPDATE ON sell_sku BEGIN SELECT RAISE(ABORT, 'sell_sku is append-only'); END;
CREATE TRIGGER IF NOT EXISTS sell_sku_no_delete BEFORE DELETE ON sell_sku BEGIN SELECT RAISE(ABORT, 'sell_sku is append-only'); END;
CREATE TABLE IF NOT EXISTS listing (
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL,                 -- 'amazon' (more marketplaces later)
    sku TEXT NOT NULL,
    asin TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    fulfillment TEXT NOT NULL DEFAULT 'FBM' CHECK (fulfillment IN ('FBM', 'FBA')),
    status TEXT NOT NULL DEFAULT '',       -- Active / Inactive / Incomplete, as the marketplace says
    qty INTEGER NOT NULL DEFAULT 0,        -- FBM: quantity on the listing; FBA: fulfillable at Amazon
    price_cents INTEGER,
    listing_id TEXT NOT NULL DEFAULT '',
    opened_on TEXT,
    condition TEXT NOT NULL DEFAULT '',
    code_type TEXT NOT NULL DEFAULT '',    -- product-id-type: 1 ASIN, 2 ISBN, 3 UPC, 4 EAN
    code TEXT NOT NULL DEFAULT '',
    fba_inbound INTEGER NOT NULL DEFAULT 0,
    fba_reserved INTEGER NOT NULL DEFAULT 0,
    fba_unfulfillable INTEGER NOT NULL DEFAULT 0,
    product_id INTEGER,
    first_seen TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    gone_at TEXT,
    UNIQUE (channel, sku)
);
CREATE INDEX IF NOT EXISTS idx_listing_asin ON listing(asin);
CREATE TABLE IF NOT EXISTS listing_mark (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL,              -- audit_item
    kind TEXT NOT NULL CHECK (kind IN ('confirm', 'not_this', 'manual', 'undo')),
    platform TEXT NOT NULL DEFAULT '',     -- key of PLATFORMS
    listing_id INTEGER,                    -- listing row (confirm / not_this)
    qty INTEGER,
    url TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    undoes INTEGER,
    user_id INTEGER,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_listing_mark_item ON listing_mark(item_id);
CREATE TRIGGER IF NOT EXISTS listing_mark_no_update BEFORE UPDATE ON listing_mark
BEGIN SELECT RAISE(ABORT, 'listing_mark is append-only'); END;
CREATE TRIGGER IF NOT EXISTS listing_mark_no_delete BEFORE DELETE ON listing_mark
BEGIN SELECT RAISE(ABORT, 'listing_mark is append-only'); END;
-- "Find its listing": a person tied a listing to a product that had no code the listing could match.
CREATE TABLE IF NOT EXISTS listing_link (
    id INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    asin TEXT NOT NULL DEFAULT '',
    ident_added INTEGER NOT NULL DEFAULT 0,   -- 1 = this link put the ASIN on the product (undo takes it off)
    sales_lines TEXT NOT NULL DEFAULT '[]',   -- sold lines that followed the link (undo lets the open ones go)
    listings TEXT NOT NULL DEFAULT '[]',      -- other listings relink() pointed at the product (same ASIN; undo lets go)
    user_id INTEGER,
    at TEXT NOT NULL,
    undone_at TEXT,
    undone_by INTEGER
);
CREATE INDEX IF NOT EXISTS idx_listing_link_listing ON listing_link(listing_id);
"""

PLATFORMS = {'amazon_fbm': 'Amazon FBM', 'amazon_fba': 'Amazon FBA', 'ebay': 'eBay', 'walmart': 'Walmart',
             'mercari': 'Mercari', 'whatnot': 'Whatnot', 'facebook': 'Facebook Marketplace', 'tcgplayer': 'TCGplayer',
             'poshmark': 'Poshmark', 'other': 'Other'}

# Alex 11-11: stock sells where its box's letter says (sorting.MARKETS). This is the one map from a box letter to
# the channel whose listings cover it. `key` is the product code that channel's listings match on ('' = SKU only).
# `platforms` are the hand-made mark platforms (PLATFORMS) that count as listed on the channel, with how they ship.
MARKET_CHANNEL = {'A': 'amazon', 'E': 'ebay', 'W': 'walmart'}
CHANNELS = {
    'amazon': {'name': 'Amazon', 'key': 'ASIN', 'platforms': {'amazon_fbm': 'FBM', 'amazon_fba': 'FBA'}},
    'ebay': {'name': 'eBay', 'key': '', 'platforms': {'ebay': 'FBM'}},
    'walmart': {'name': 'Walmart', 'key': '', 'platforms': {'walmart': 'FBM'}},
}
CHANNEL_LETTER = {ch: letter for letter, ch in MARKET_CHANNEL.items()}
ASIN_RE = re.compile(r'^B0[A-Z0-9]{8}\Z', re.I)
SUGGEST_MAX = 5

# item-condition codes in the listings report
CONDITIONS = {'11': 'New', '1': 'Used - Like New', '2': 'Used - Very Good', '3': 'Used - Good', '4': 'Used - Acceptable',
              '5': 'Collectible - Like New', '6': 'Collectible - Very Good', '7': 'Collectible - Good',
              '8': 'Collectible - Acceptable', '10': 'Refurbished'}

STOP = set('the a an and or of for with to in on by from new set pack pcs piece pieces count ct oz inch in x - & + / , .'.split())
TITLE_MIN = 0.75   # share of the shorter title's words found in the other one (checked on live data 11-10)


def _words(title: str) -> set:
    return {w for w in re.findall(r'[a-z0-9]+', (title or '').lower()) if w not in STOP and len(w) > 1}


def title_score(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    if len(wa) < 3 or len(wb) < 3:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


def is_live(li: dict) -> bool:
    """For sale right now: the marketplace says Active, has units, and the listing wasn't deleted.
    The one definition behind the audit's "For sale now" tab, its banner and the Listings page."""
    return li.get('status') == 'Active' and (li.get('qty') or 0) > 0 and not li.get('gone_at')


def _cents(v):
    try:
        return round(float(v) * 100) if str(v).strip() != '' else None
    except ValueError:
        return None


def _int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def parse_merchant_listings(text: str) -> list:
    """GET_MERCHANT_LISTINGS_ALL_DATA (tab separated, header row) -> listing dicts."""
    out = []
    for r in csv.DictReader(io.StringIO(text), delimiter='\t'):
        r = {(k or '').strip().lstrip('\ufeff').lower(): (v or '').strip() for k, v in r.items() if k}
        sku = r.get('seller-sku', '')
        if not sku:
            continue
        fc = r.get('fulfillment-channel', 'DEFAULT').upper()
        out.append({'sku': sku, 'asin': (r.get('asin1') or '').upper(), 'title': r.get('item-name', '')[:300],
                    'fulfillment': 'FBM' if fc in ('', 'DEFAULT') else 'FBA', 'status': r.get('status', ''),
                    'qty': _int(r.get('quantity')), 'price_cents': _cents(r.get('price')),
                    'listing_id': r.get('listing-id', ''), 'opened_on': (r.get('open-date') or '')[:10] or None,
                    'condition': CONDITIONS.get(r.get('item-condition', ''), r.get('item-condition', '')), 'code_type': r.get('product-id-type', ''),
                    'code': r.get('product-id', '')})
    return out


# eBay (Alex 11-11): the Trading API's GetMyeBaySelling is the read that sees every listing. The Sell Inventory API
# only sees listings made through that same API, so anything listed in Seller Hub or the app would be missing.
# A listing is keyed on eBay's item ID (sku = ITEM-<item id>), which stays put when the Custom label is edited.
# The Custom label goes in `code` (code_type 'SKU'): when it is a Boxloft SKU or a product code, the listing and
# its sales find the product with no tap. A sale whose listing has no Custom label carries the same ITEM-<item id>
# (sales_sync.ebay_order), so it follows the listing once a person links that listing to a product.
EBAY_NS = {'e': 'urn:ebay:apis:eBLBaseComponents'}
EBAY_TRADING = 'https://api.ebay.com/ws/api.dll'
EBAY_PAGE = 200


def ebay_item_key(item_id: str, variation: str = '') -> str:
    return f'ITEM-{item_id}' + (f'/{variation}' if variation else '')


def ebay_selling_request(page: int) -> bytes:
    return (f'<?xml version="1.0" encoding="utf-8"?><GetMyeBaySellingRequest xmlns="{EBAY_NS["e"]}">'
            f'<ActiveList><Include>true</Include><Pagination><EntriesPerPage>{EBAY_PAGE}</EntriesPerPage>'
            f'<PageNumber>{page}</PageNumber></Pagination></ActiveList><DetailLevel>ReturnAll</DetailLevel>'
            '</GetMyeBaySellingRequest>').encode()


def parse_ebay_selling(text: str) -> tuple:
    """One GetMyeBaySelling page -> (listing dicts, total pages). Raises ConnectionError_ on an eBay error.
    A listing with variations gives one row per variation (its own SKU, quantity and price)."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise ConnectionError_(f'eBay listings: not XML: {str(text)[:200]}')

    def t(el, path):
        return (el.findtext(path, default='', namespaces=EBAY_NS) or '').strip()

    if t(root, 'e:Ack') == 'Failure':
        msg = '; '.join(filter(None, (t(e, 'e:LongMessage') or t(e, 'e:ShortMessage') for e in root.findall('e:Errors', EBAY_NS))))
        raise ConnectionError_(f'eBay listings: {msg[:300] or "Failure"}')
    act = root.find('e:ActiveList', EBAY_NS)
    if act is None:
        return [], 0
    pages = _int(t(act, 'e:PaginationResult/e:TotalNumberOfPages'))
    out = []
    for it in act.findall('e:ItemArray/e:Item', EBAY_NS):
        item_id = t(it, 'e:ItemID')
        if not item_id:
            continue
        pic = t(it, 'e:PictureDetails/e:GalleryURL') or t(it, 'e:PictureDetails/e:PictureURL')
        base = {'asin': '', 'title': t(it, 'e:Title')[:300], 'fulfillment': 'FBM', 'status': 'Active',
                'listing_id': item_id, 'opened_on': t(it, 'e:ListingDetails/e:StartTime')[:10] or None,
                'condition': t(it, 'e:ConditionDisplayName'),
                # the gallery link is a 140 px thumbnail; s-l500 is the same photo at 500 px (Alex 11-12: photos)
                'image_url': re.sub(r'/s-l\d+\.(jpg|webp|png)$', r'/s-l500.\1', pic) if pic.startswith('https://') else ''}
        price = _cents(t(it, 'e:SellingStatus/e:CurrentPrice') or t(it, 'e:BuyItNowPrice') or t(it, 'e:StartPrice'))
        variations = it.findall('e:Variations/e:Variation', EBAY_NS)
        if not variations:
            avail = t(it, 'e:QuantityAvailable')
            qty = _int(avail) if avail else _int(t(it, 'e:Quantity')) - _int(t(it, 'e:SellingStatus/e:QuantitySold'))
            label = t(it, 'e:SKU')
            out.append({**base, 'sku': ebay_item_key(item_id), 'qty': max(0, qty), 'price_cents': price,
                        'code_type': 'SKU' if label else '', 'code': label})
            continue
        for n, v in enumerate(variations, 1):
            label = t(v, 'e:SKU')
            if not label:   # sweep D-u3: a position changes when a variation is added; its specifics don't
                spec = ';'.join(sorted(f"{t(x, 'e:Name')}={t(x, 'e:Value')}"
                                       for x in v.findall('e:VariationSpecifics/e:NameValueList', EBAY_NS)))
                n = spec[:80] or n
            qty = _int(t(v, 'e:Quantity')) - _int(t(v, 'e:SellingStatus/e:QuantitySold'))
            out.append({**base, 'sku': ebay_item_key(item_id, label or str(n)), 'qty': max(0, qty),
                        'price_cents': _cents(t(v, 'e:StartPrice')) or price,
                        'code_type': 'SKU' if label else '', 'code': label})
    return out, pages


def ebay_item_request(item_id: str) -> bytes:
    return (f'<?xml version="1.0" encoding="utf-8"?><GetItemRequest xmlns="{EBAY_NS["e"]}">'
            f'<ItemID>{re.sub(r"[^0-9]", "", item_id)}</ItemID></GetItemRequest>').encode()


def parse_ebay_item(text: str) -> dict:
    """GetItem -> {title, image_url} ({} on an eBay error: the item is too old or not ours)."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {}
    it = root.find('e:Item', EBAY_NS)
    if it is None:
        return {}
    t = lambda p: (it.findtext(p, default='', namespaces=EBAY_NS) or '').strip()  # noqa: E731
    pic = t('e:PictureDetails/e:GalleryURL') or t('e:PictureDetails/e:PictureURL')
    return {'title': t('e:Title')[:300],
            'image_url': re.sub(r'/s-l\d+\.(jpg|webp|png)$', r'/s-l500.\1', pic) if pic.startswith('https://') else ''}


def sku_text(day: str, cents: int) -> str:
    """Alex 11-24: a Seller Central SKU from the buy, "date it was purchased - price paid": 2026-10-12 and 1015 cents
    = "10.12.26-10.15" (no leading zeros, as his 11.5.26 SKUs)."""
    return f"{date_text(day)}-{cents // 100}.{cents % 100:02d}"


def date_text(day: str) -> str:
    """2026-10-12 -> "10.12.26"."""
    y, m, d = (int(x) for x in day[:10].split('-'))
    return f"{m}.{d}.{y % 100:02d}"


# A receipt whose cost the Usual unit cost filled in (newest cost event a cost_fill; a typed cost_set wins a tie) is no
# price paid (review 11-25: an estimate saved the usual cost as 'price: a buy' over a real buy line's price).
NOT_FILLED = """NOT EXISTS (SELECT 1 FROM cost_fill f WHERE f.receipt_id = r.id
                          AND NOT EXISTS (SELECT 1 FROM cost_set cs WHERE cs.receipt_id = r.id AND cs.at >= f.at))"""


class Listings:
    # A buy a SKU may come from: a live receipt with a cost and a purchase date, or a buy line with a price on an order
    # that is inventory and not cancelled (review 11-24: cancelled and Not inventory orders gave SKUs).
    _SKU_RECEIPTS = f"""
        SELECT r.product_id, substr(r.purchased_at, 1, 10) AS day, r.unit_cost_cents AS cents, r.reference, a.at AS seen
          FROM receipt r JOIN action a ON a.id = r.action_id
         WHERE a.reversed_by IS NULL AND a.reverses_action_id IS NULL AND r.cost_missing = 0 AND r.unit_cost_cents > 0
           AND {NOT_FILLED} AND r.purchased_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'"""
    _SKU_LINES = f"""
        SELECT l.product_id, substr(o.ordered_at, 1, 10) AS day, l.unit_cost_cents AS cents, o.external_id AS reference,
               o.imported_at AS seen
          FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id
         WHERE l.cancelled = 0 AND COALESCE(l.unit_cost_cents, 0) > 0 AND {RECEIVABLE_ORDER_SQL} AND o.review = ''
           AND o.ordered_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'"""

    def sell_sku(self, product_id: int):
        """The SKU to paste into Seller Central (Alex 11-24, "the date it was purchased - price paid"): the newest buy
        with both, as M.D.YY-price; with none, one made from what there is (`_sku_estimate`, Alex 11-25: always a
        SKU). None only for an item that does not exist. {product_id, base, sku, saved, day, cost_cents, from,
        estimated}.
        The first Copy saves it (`save_sell_sku`, table sell_sku): a saved SKU never changes and no other item can
        take it (review 11-24, 3 rounds: any SKU worked out fresh moved when buys were relinked, approved, cancelled
        or hidden). Before that it is a preview: items with a buy on that day at that price take turns, first known
        buy first (ties by item id), each taking the first of base, base-2, ... that is not saved by another item, not
        taken by an earlier item and not held by an Amazon listing of another ASIN (its own listing may)."""
        r = self._sku_buy(product_id)
        if not r:
            return self._sku_estimate(product_id)
        base = sku_text(r['day'], r['cents'])
        out = {'product_id': product_id, 'base': base, 'day': r['day'], 'cost_cents': r['cents'],
               'from': r['reference'] or '', 'estimated': False}
        kept = self.wms._q1('SELECT sku FROM sell_sku WHERE product_id = ? AND base = ?', (product_id, base))
        if kept:
            return {**out, 'sku': kept['sku'], 'saved': True}
        saved = {x['sku']: x['product_id'] for x in self.wms._q(
            "SELECT sku, product_id FROM sell_sku WHERE sku = ? OR sku LIKE ? ESCAPE '\\'",
            (base, base.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '-%'))}
        first = {x['product_id']: x['seen'] or '' for x in self.wms._q(
            f"""SELECT product_id, MIN(seen) AS seen FROM ({self._SKU_RECEIPTS} UNION ALL {self._SKU_LINES})
                 WHERE day = ? AND cents = ? AND product_id IS NOT NULL GROUP BY product_id""", (r['day'], r['cents']))}
        used = set()
        for q in sorted(set(first) | {product_id}, key=lambda q: (first.get(q, '~'), q)):
            own = self.wms._q1('SELECT sku FROM sell_sku WHERE product_id = ? AND base = ?', (q, base))
            if own:
                used.add(own['sku'])
                continue
            mine = {x['value'].upper() for x in self.wms._q(
                "SELECT value FROM product_identifier WHERE product_id = ? AND kind = 'ASIN'", (q,))}
            n = 1
            while True:   # no cap: a few hundred listings on one text still end (review 11-24: -199 was taken)
                sku = base if n == 1 else f'{base}-{n}'
                taken = {(x['asin'] or '').upper() for x in self.wms._q(
                    "SELECT asin FROM listing WHERE channel = 'amazon' AND sku = ?", (sku,))}
                if sku not in used and saved.get(sku, q) == q and (not taken or (mine and taken <= mine)):
                    break
                n += 1
            used.add(sku)
            if q == product_id:
                return {**out, 'sku': sku, 'saved': False}
        return None

    # Rows that hold the made-up SKU of an item with no dated, priced buy end in "-S<item id>".
    def _sku_estimate(self, product_id: int):
        """Alex 11-25: "if we don't have data we use what we can. If we have none then we need to create a sku,
        regardless always have a copy button." An item with no buy that has both a date and a price gets
        M.D.YY[-price]-S<item id>. The date is its newest buy's date, else the day it came into Boxloft (its first
        receipt), else the day the item was made (ET). The price is its newest priced buy, else its Usual unit cost,
        else left out. The item id makes it unique with no turns; a listing of another ASIN holding it moves it on to
        -2, -3. Once copied, the newest saved one shows while the item still has no dated, priced buy."""
        p = self.wms._q1('SELECT id, created_at, default_cost_cents FROM product WHERE id = ?', (product_id,))
        if not p:
            return None
        tag = f'-S{product_id}'
        kept = self.wms._q1("""SELECT base, sku, day, cost_cents, ref FROM sell_sku WHERE product_id = ?
                                 AND substr(base, -?) = ? ORDER BY id DESC LIMIT 1""", (product_id, len(tag), tag))
        if kept:
            return {'product_id': product_id, 'base': kept['base'], 'sku': kept['sku'], 'saved': True,
                    'day': kept['day'], 'cost_cents': kept['cost_cents'] or None, 'from': kept['ref'], 'estimated': True}
        from store_mail import et_day
        live = "a.reversed_by IS NULL AND a.reverses_action_id IS NULL"
        dated = "GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'"
        lines = f"""FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id
                    WHERE l.product_id = ? AND l.cancelled = 0 AND {RECEIVABLE_ORDER_SQL} AND o.review = ''"""
        day = self.wms._q1(f"""SELECT day, how FROM (
                   SELECT substr(r.purchased_at, 1, 10) AS day, 'bought' AS how, r.id AS k FROM receipt r
                     JOIN action a ON a.id = r.action_id WHERE r.product_id = ? AND {live} AND r.purchased_at {dated}
                   UNION ALL SELECT substr(o.ordered_at, 1, 10), 'bought', -l.id {lines} AND o.ordered_at {dated})
                 ORDER BY day DESC, k DESC LIMIT 1""", (product_id, product_id))
        if not day:
            first = self.wms._q1(f"""SELECT MIN(a.at) AS at FROM receipt r JOIN action a ON a.id = r.action_id
                                      WHERE r.product_id = ? AND {live}""", (product_id,))
            day = ({'day': et_day(first['at']), 'how': 'came in'} if first and first['at']
                   else {'day': et_day(p['created_at']), 'how': 'item made'})
        if not re.fullmatch(r'\d{4}-\d\d-\d\d', str(day['day'] or '')):   # an odd stored time: today (review 11-25)
            from inbound import today_et
            day = {'day': today_et().isoformat(), 'how': 'today'}
        price = self.wms._q1(f"""SELECT cents, how FROM (
                   SELECT r.unit_cost_cents AS cents, 'a buy' AS how, 1 AS src, r.id AS k FROM receipt r
                     JOIN action a ON a.id = r.action_id
                    WHERE r.product_id = ? AND {live} AND r.cost_missing = 0 AND r.unit_cost_cents > 0 AND {NOT_FILLED}
                   UNION ALL SELECT l.unit_cost_cents, 'a buy', 2, l.id {lines} AND COALESCE(l.unit_cost_cents, 0) > 0)
                 ORDER BY src, k DESC LIMIT 1""", (product_id, product_id))
        if not price and (p['default_cost_cents'] or 0) > 0:
            price = {'cents': p['default_cost_cents'], 'how': 'usual unit cost'}
        cents = price['cents'] if price else None
        base = (sku_text(day['day'], cents) if cents else date_text(day['day'])) + tag
        n = 1
        while True:
            sku = base if n == 1 else f'{base}-{n}'
            other = self.wms._q1('SELECT 1 FROM sell_sku WHERE sku = ? AND product_id != ?', (sku, product_id))
            taken = {(x['asin'] or '').upper() for x in self.wms._q(
                "SELECT asin FROM listing WHERE channel = 'amazon' AND sku = ?", (sku,))}
            mine = {x['value'].upper() for x in self.wms._q(
                "SELECT value FROM product_identifier WHERE product_id = ? AND kind = 'ASIN'", (product_id,))}
            if not other and (not taken or (mine and taken <= mine)):
                break
            n += 1
        note = f"date: {day['how']}" + (f", price: {price['how']}" if price else ', no price on file')
        return {'product_id': product_id, 'base': base, 'sku': sku, 'saved': False, 'day': day['day'],
                'cost_cents': cents, 'from': note, 'estimated': True}

    def save_sell_sku(self, product_id: int, user_id: int) -> dict:
        """Copy SKU (under the lock): the SKU the preview gives now, saved for good for this item and buy."""
        s = self.sell_sku(product_id)
        if not s:
            raise WmsError('Item not found.')
        if not s['saved']:
            with self.wms._tx() as c:
                c.execute("""INSERT INTO sell_sku (product_id, base, sku, day, cost_cents, ref, user_id, at)
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                          (product_id, s['base'], s['sku'], s['day'], s['cost_cents'] or 0, s['from'], user_id, now_utc()))
            s = {**s, 'saved': True}
        return s

    def _sku_buy(self, product_id: int):
        """The newest buy with a date and a price: a live receipt first, else a buy line. {day, cents, reference}."""
        return (self.wms._q1(f"SELECT * FROM ({self._SKU_RECEIPTS} AND r.product_id = ? ORDER BY day DESC, r.id DESC LIMIT 1)", (product_id,))
                or self.wms._q1(f"SELECT * FROM ({self._SKU_LINES} AND l.product_id = ? ORDER BY day DESC, l.id DESC LIMIT 1)", (product_id,)))

    def __init__(self, wms: Wms, commerce, connections=None):
        self.wms = wms
        self.commerce = commerce
        self.connections = connections
        self.wms._conn().executescript(SCHEMA + SYNC_SCHEMA)
        Wms._add_column(self.wms._conn(), 'listing_link', 'listings', "TEXT NOT NULL DEFAULT '[]'")
        Wms._add_column(self.wms._conn(), 'listing', 'image_url', 'TEXT')   # eBay's photo (Amazon rows: none)
        Wms._add_column(self.wms._conn(), 'listing_link', 'ident_kind', "TEXT NOT NULL DEFAULT 'ASIN'")

    # ------------------------------------------------------------------ write: refresh from the marketplace

    def upsert(self, channel: str, rows: list, fba: dict = None, full: bool = True) -> dict:
        """rows from parse_merchant_listings; fba = sku -> {fulfillable, inbound, reserved, unfulfillable}.
        full=True means rows is every listing on the account, so anything missing is gone."""
        fba = fba or {}
        now = now_utc()
        counts = {'listings': len(rows), 'new': 0, 'gone': 0, 'fbm_active': 0, 'fba_active': 0}
        seen = set()
        with self.wms._tx() as c:
            for r in rows:
                seen.add(r['sku'])
                f = fba.get(r['sku'])
                qty = r['qty']
                inbound = reserved = unful = 0
                if r['fulfillment'] == 'FBA':
                    qty = f['fulfillable'] if f else 0
                    inbound, reserved, unful = (f['inbound'], f['reserved'], f['unfulfillable']) if f else (0, 0, 0)
                pid = (self.commerce._match_product(c, r['sku'], r['asin'])
                       or (self.commerce._match_product(c, r['code'], '') if r.get('code_type') == 'SKU' and r['code'] else None))
                counts['new'] += not c.execute('SELECT 1 FROM listing WHERE channel = ? AND sku = ?', (channel, r['sku'])).fetchone()
                c.execute("""
                    INSERT INTO listing (channel, sku, asin, title, fulfillment, status, qty, price_cents, listing_id, opened_on,
                                         condition, code_type, code, fba_inbound, fba_reserved, fba_unfulfillable, product_id,
                                         first_seen, seen_at, image_url)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(channel, sku) DO UPDATE SET asin=excluded.asin, title=excluded.title,
                        fulfillment=excluded.fulfillment, status=excluded.status, qty=excluded.qty,
                        price_cents=excluded.price_cents, listing_id=excluded.listing_id, opened_on=excluded.opened_on,
                        condition=excluded.condition, code_type=excluded.code_type, code=excluded.code,
                        fba_inbound=excluded.fba_inbound, fba_reserved=excluded.fba_reserved,
                        fba_unfulfillable=excluded.fba_unfulfillable,
                        product_id=COALESCE(excluded.product_id, listing.product_id), seen_at=excluded.seen_at, gone_at=NULL,
                        image_url=COALESCE(excluded.image_url, listing.image_url)""",
                    (channel, r['sku'], r['asin'], r['title'], r['fulfillment'], r['status'], qty, r['price_cents'],
                     r['listing_id'], r['opened_on'], r['condition'], r['code_type'], r['code'], inbound, reserved, unful,
                     pid, now, now, r.get('image_url') or None))
                if r['status'].lower() == 'active' and qty > 0:
                    counts['fba_active' if r['fulfillment'] == 'FBA' else 'fbm_active'] += 1
            if full:
                for (lid, sku) in c.execute('SELECT id, sku FROM listing WHERE channel = ? AND gone_at IS NULL', (channel,)).fetchall():
                    if sku not in seen:
                        c.execute('UPDATE listing SET gone_at = ? WHERE id = ?', (now, lid))
                        counts['gone'] += 1
            counts['sales_lines_linked'] = self._relink(c)
        return counts

    def _relink(self, c) -> int:
        """Listings and sold lines with no product yet: try again (the audit makes products as it finds them)."""
        return self.commerce.relink(c)

    def _amazon_call(self, method: str, path: str, tok: dict, body: dict = None):
        return sp_call(self.connections, method, path, tok, body)

    def _amazon_fba(self, tok: dict) -> dict:
        out, nt = {}, None
        while True:
            q = {'granularityType': 'Marketplace', 'granularityId': US_MARKETPLACE, 'marketplaceIds': US_MARKETPLACE,
                 'details': 'true'}
            if nt:
                q['nextToken'] = nt
            p = self._amazon_call('GET', '/fba/inventory/v1/summaries?' + urllib.parse.urlencode(q), tok)
            for s in (p.get('payload') or {}).get('inventorySummaries', []):
                d = s.get('inventoryDetails') or {}
                out[s.get('sellerSku', '')] = {
                    'fulfillable': _int(d.get('fulfillableQuantity')),
                    'inbound': sum(_int(d.get(k)) for k in ('inboundWorkingQuantity', 'inboundShippedQuantity', 'inboundReceivingQuantity')),
                    'reserved': _int((d.get('reservedQuantity') or {}).get('totalReservedQuantity')),
                    'unfulfillable': _int((d.get('unfulfillableQuantity') or {}).get('totalUnfulfillableQuantity'))}
            nt = (p.get('pagination') or {}).get('nextToken')
            if not nt:
                return out
            time.sleep(0.6)  # 2 per second

    def refresh_amazon(self, progress=None) -> dict:
        tok = {}
        if progress:
            progress('asking Amazon for every listing')
        rows = parse_merchant_listings(sp_reports(self.connections, [{'reportType': 'GET_MERCHANT_LISTINGS_ALL_DATA'}], tok,
                                                  progress, 'listings report')[0])
        if progress:
            progress(f'{len(rows)} listings read; reading FBA stock')
        fba = self._amazon_fba(tok)
        out = self.upsert('amazon', rows, fba)
        out['photos'] = self.fill_photos(progress)
        return out

    catalog = None   # server sets it (commerce.Catalog)

    def fill_photos(self, progress=None) -> dict:
        """Amazon's listings report has no photo (Alex 11-13: order cards for items not in Boxloft showed none). The
        app has no Catalog or Listings Items role (both 403 on 11-13), so the photo comes from Keepa by ASIN: the
        cache first (no token), then 100 ASINs a call, 1 token each, cached forever. A listing keeps the photo it has."""
        todo = {r['asin'].upper() for r in self.wms._q("""SELECT DISTINCT asin FROM listing WHERE channel = 'amazon'
                    AND gone_at IS NULL AND asin != '' AND COALESCE(image_url, '') = ''""")}
        if not todo:
            return {'missing': 0}

        def cached():
            got = {}
            for a in todo:
                r = self.wms._q1("SELECT data FROM catalog_cache WHERE code = ? AND data != '[]'", (a,))
                for p in json.loads(r['data']) if r else []:
                    if (p.get('asin') or '').upper() == a and (p.get('image_url') or '').startswith('https://'):
                        got[a] = p['image_url']
                        break
            return got
        have = cached()
        looked, err = 0, ''
        need = sorted(todo - set(have))
        if need and self.catalog and self.catalog.keepa_key:
            if progress:
                progress(f'reading {len(need)} listing photos from Keepa')
            try:
                looked = self.catalog.prewarm(need)['found']
            except WmsError as e:   # out of tokens or no network: the next hourly read tries again
                err = str(e)[:120]
            have = cached()
        with self.wms._tx() as c:
            for a, url in have.items():
                c.execute("""UPDATE listing SET image_url = ? WHERE channel = 'amazon' AND UPPER(asin) = ?
                               AND COALESCE(image_url, '') = ''""", (url, a))
        return {'missing': len(todo), 'filled': len(have), 'keepa_found': looked, **({'error': err} if err else {})}

    def _ebay_page(self, token: str, page: int) -> str:
        st, text = _http('POST', EBAY_TRADING, {
            'X-EBAY-API-SITEID': '0', 'X-EBAY-API-COMPATIBILITY-LEVEL': '1271', 'X-EBAY-API-CALL-NAME': 'GetMyeBaySelling',
            'X-EBAY-API-IAF-TOKEN': token, 'Content-Type': 'text/xml'}, ebay_selling_request(page), timeout=60, raw_text=True)
        if st != 200:
            raise ConnectionError_(f'eBay listings {st}: {str(text)[:200]}')
        return text

    def refresh_ebay(self, progress=None) -> dict:
        """Every active eBay listing (GetMyeBaySelling ActiveList, 200 a page). A listing that ended or sold out
        is missing from the list, so it gets gone_at like a deleted Amazon SKU."""
        token = self.connections.ebay_user_token()
        rows, page, pages = [], 1, 1
        while page <= pages:
            got, pages = parse_ebay_selling(self._ebay_page(token, page))
            rows += got
            if progress:
                progress(f'{len(rows)} eBay listings read (page {page} of {max(pages, 1)})')
            page += 1
            if page <= pages:
                time.sleep(0.5)
        out = self.upsert('ebay', rows)
        out['ended_read'] = self._ebay_ended(token)
        return out

    def _ebay_ended(self, token: str, limit: int = 60) -> int:
        """eBay sales whose listing Boxloft never saw (it sold out before a read): GetItem once each, for the title and
        photo on the order card (Alex 11-12). Saved as an ended listing row, so it is read once."""
        todo = self.wms._q("""SELECT DISTINCT l.sku FROM sales_line l JOIN sales_order o ON o.id = l.order_id
                               WHERE o.channel = 'ebay' AND l.sku LIKE 'ITEM-%'
                                 AND NOT EXISTS (SELECT 1 FROM listing li WHERE li.channel = 'ebay' AND li.sku = l.sku)
                               ORDER BY o.ordered_at DESC LIMIT ?""", (limit,))
        now = now_utc()
        n = 0
        for r in todo:
            item_id = r['sku'][5:].split('/', 1)[0]
            st, text = _http('POST', EBAY_TRADING, {
                'X-EBAY-API-SITEID': '0', 'X-EBAY-API-COMPATIBILITY-LEVEL': '1271', 'X-EBAY-API-CALL-NAME': 'GetItem',
                'X-EBAY-API-IAF-TOKEN': token, 'Content-Type': 'text/xml'}, ebay_item_request(item_id), timeout=30, raw_text=True)
            got = parse_ebay_item(text) if st == 200 else {}
            with self.wms._tx() as c:
                c.execute("""INSERT OR IGNORE INTO listing (channel, sku, title, fulfillment, status, qty, listing_id, first_seen,
                                                            seen_at, gone_at, image_url)
                             VALUES ('ebay', ?, ?, 'FBM', 'Ended', 0, ?, ?, ?, ?, ?)""",
                          (r['sku'], got.get('title', ''), item_id, now, now, now, got.get('image_url') or None))
            n += 1
            time.sleep(0.2)
        return n

    def run(self, progress=None, channel: str = 'amazon') -> dict:
        """One full refresh, recorded in sync_state as '<channel>_listings' (shown on the Listings page)."""
        started = now_utc()
        fn = {'amazon': self.refresh_amazon, 'ebay': self.refresh_ebay}[channel]
        try:
            result, err = fn(progress), None
        except (ConnectionError_, WmsError, OSError, ValueError) as e:
            result, err = None, str(e)[:300]
        with self.wms._tx() as c:
            c.execute("""INSERT INTO sync_state (channel, cursor, last_run_at, last_ok_at, last_error, last_result)
                         VALUES (?, NULL, ?, ?, ?, ?)
                         ON CONFLICT(channel) DO UPDATE SET last_run_at = excluded.last_run_at,
                             last_ok_at = COALESCE(excluded.last_ok_at, sync_state.last_ok_at),
                             last_error = excluded.last_error,
                             last_result = COALESCE(excluded.last_result, sync_state.last_result)""",
                      (f'{channel}_listings', started, started if err is None else None, err,
                       json.dumps(result) if result else None))
        if err:
            raise WmsError(err)
        return result

    def due(self, minutes: int = 60, channel: str = 'amazon') -> bool:
        st = self.wms._q1('SELECT last_run_at FROM sync_state WHERE channel = ?', (f'{channel}_listings',))
        if not st or not st['last_run_at']:
            return True
        from datetime import datetime, timedelta, timezone
        last = datetime.fromisoformat(st['last_run_at'].replace('Z', '+00:00'))
        return datetime.now(timezone.utc) - last >= timedelta(minutes=minutes)

    # ------------------------------------------------------------------ read

    def _linked(self, rows: list) -> list:
        """Sets each listing's product_id as of now. The stored link is only written on a refresh or when a
        product is saved, so a listing with none is matched here by SKU, then ASIN, like _match_product."""
        if any(not r['product_id'] for r in rows):
            idx = self.commerce.product_index(self.wms._conn())
            for r in rows:
                r['product_id'] = (r['product_id'] or self.commerce.index_match(idx, r['sku'], r['asin'])
                                   or (self.commerce.index_match(idx, r['code'], '') if r.get('code_type') == 'SKU' else None))
        return rows

    def list(self, include_gone: bool = False) -> dict:
        rows = self._linked(self.wms._q(f"""
            SELECT li.*, ai.id AS audit_id,
                   COALESCE(NULLIF(ai.image_url, ''),   -- store rows made before their photo was read keep it on the line
                            (SELECT pl.image_url FROM purchase_line pl WHERE pl.id = ai.line_id),
                            NULLIF(li.image_url, '')) AS image_url,   -- eBay's photo, or Keepa's for Amazon (fill_photos)
                   (SELECT COUNT(*) FROM listing_mark m WHERE m.listing_id = li.id AND m.kind = 'confirm'
                       AND NOT EXISTS (SELECT 1 FROM listing_mark u WHERE u.kind = 'undo' AND u.undoes = m.id)) AS confirmed
              FROM listing li LEFT JOIN audit_item ai ON ai.id = COALESCE(
                   (SELECT x.id FROM audit_item x WHERE x.asin = li.asin AND li.asin != ''),
                   (SELECT m.item_id FROM listing_mark m WHERE m.listing_id = li.id AND m.kind = 'confirm'   -- a store row matched by a person
                      AND NOT EXISTS (SELECT 1 FROM listing_mark u WHERE u.kind = 'undo' AND u.undoes = m.id) ORDER BY m.id DESC LIMIT 1))
             {'' if include_gone else 'WHERE li.gone_at IS NULL'}
             ORDER BY (li.status = 'Active' AND li.qty > 0) DESC, li.qty DESC, li.title"""))
        on_hand = {r['product_id']: r['qty'] for r in self.wms._q("""
            SELECT s.product_id, SUM(s.qty) qty FROM stock_level s LEFT JOIN location l ON l.id = s.location_id
             WHERE s.qty > 0 AND (s.box_id != 0 OR l.kind != 'virtual') GROUP BY s.product_id""")}
        market = {ch: self.market_stock(letter) for letter, ch in MARKET_CHANNEL.items()}
        for r in rows:
            r['on_hand'] = on_hand.get(r['product_id'], 0)
            # on_hand_market: the units in this channel's own boxes (A boxes for Amazon). on_hand is every box.
            r['on_hand_market'] = (market.get(r['channel'], {}).get(r['product_id']) or {}).get('on_hand', 0)
            r['live'] = is_live(r)
        manual = self.wms._q("""
            SELECT m.*, ai.title, ai.asin, ai.image_url FROM listing_mark m JOIN audit_item ai ON ai.id = m.item_id
             WHERE m.kind = 'manual' AND NOT EXISTS (SELECT 1 FROM listing_mark u WHERE u.kind = 'undo' AND u.undoes = m.id)
             ORDER BY m.id DESC""")
        for m in manual:
            m['platform_label'] = PLATFORMS.get(m['platform'], m['platform'])
        st = self.wms._q1("SELECT * FROM sync_state WHERE channel = 'amazon_listings'")
        syncs = {ch: self.wms._q1('SELECT * FROM sync_state WHERE channel = ?', (f'{ch}_listings',)) for ch in ('amazon', 'ebay')}
        live = [r for r in rows if not r['gone_at'] and r['channel'] == 'amazon']   # sweep D6: FBM/FBA stats are Amazon's
        summary = {
            'listings': len(live),
            'active_fbm': sum(1 for r in live if r['fulfillment'] == 'FBM' and r['status'] == 'Active' and r['qty'] > 0),
            'active_fba': sum(1 for r in live if r['fulfillment'] == 'FBA' and r['qty'] > 0),
            'units_fbm': sum(r['qty'] for r in live if r['fulfillment'] == 'FBM' and r['status'] == 'Active'),
            'units_fba': sum(r['qty'] for r in live if r['fulfillment'] == 'FBA'),
            'units_fba_inbound': sum(r['fba_inbound'] for r in live),
            # FBM listings offering more than Boxloft has on a shelf: either still in the audit or an oversell risk
            'fbm_over': sum(1 for r in live if r['fulfillment'] == 'FBM' and r['status'] == 'Active' and r['qty'] > r['on_hand']),
            'manual': len(manual),
        }
        return {'listings': rows, 'manual': manual, 'summary': summary, 'platforms': PLATFORMS, 'sync': st, 'syncs': syncs}

    # ------------------------------------------------------------------ read: stock in a market's boxes against its listings

    def market_stock(self, letter: str) -> dict:
        """product_id -> {on_hand, boxes: [{name, qty, box_id}]} for stock in that market's boxes (and old bins)."""
        from sorting import market_of   # sorting imports this module
        out = {}
        for r in self.wms._q("""
                SELECT s.product_id, s.box_id, COALESCE(b.name, l.code) AS holder, SUM(s.qty) AS qty
                  FROM stock_level s LEFT JOIN box b ON b.id = s.box_id AND s.box_id != 0
                  LEFT JOIN location l ON l.id = s.location_id AND s.box_id = 0
                 WHERE s.qty > 0 AND (s.box_id != 0 OR l.kind != 'virtual')
                 GROUP BY s.product_id, s.box_id, holder"""):
            if market_of(r['holder']) != letter:
                continue
            e = out.setdefault(r['product_id'], {'on_hand': 0, 'boxes': []})
            e['on_hand'] += r['qty']
            e['boxes'].append({'name': r['holder'].upper(), 'qty': r['qty'], 'box_id': r['box_id'] or None})
        for e in out.values():
            e['boxes'].sort(key=lambda b: (-b['qty'], b['name']))
        return out

    def _item_products(self, idx: dict) -> dict:
        """audit_item id -> product id, for rows a person marked: by the row's ASIN, else its purchase line."""
        out = {}
        for r in self.wms._q("""SELECT ai.id, ai.asin, pl.product_id FROM audit_item ai LEFT JOIN purchase_line pl ON pl.id = ai.line_id
                                 WHERE ai.id IN (SELECT item_id FROM listing_mark)"""):
            pid = idx.get((r['asin'] or '').lower()) or r['product_id']
            if pid:
                out[r['id']] = pid
        return out

    def _channel_match(self, channel: str):
        """The one place a channel's listings meet products: (listings not deleted, product id -> its listings,
        listings with no product, product id -> hand-made marks for the channel's platforms). A listing gets its
        product by SKU, then ASIN (_linked), else from the audit row a person confirmed it on."""
        cfg = CHANNELS[channel]
        idx = self.commerce.product_index(self.wms._conn())
        lis = self._linked(self.wms._q('SELECT * FROM listing WHERE channel = ? AND gone_at IS NULL', (channel,)))
        by_pid, loose = {}, []
        for li in lis:
            (by_pid.setdefault(li['product_id'], []) if li['product_id'] else loose).append(li)
        marks = self._marks()
        manual = {}   # product id -> [{platform, label, qty, fulfillment}]
        if marks:
            items = self._item_products(idx)
            by_id = {li['id']: li for li in lis}
            for item_id, ms in marks.items():
                pid = items.get(item_id)
                if not pid:
                    continue
                for m in ms:
                    if m['kind'] == 'manual' and m['platform'] in cfg['platforms']:
                        manual.setdefault(pid, []).append({'platform': m['platform'], 'label': PLATFORMS[m['platform']],
                                                           'qty': m['qty'] or 0, 'fulfillment': cfg['platforms'][m['platform']]})
                    elif m['kind'] == 'confirm' and m['listing_id'] in by_id:   # a store row matched by a person
                        li = by_id[m['listing_id']]
                        if not li['product_id']:
                            li['product_id'] = pid
                            by_pid.setdefault(pid, []).append(li)
                            loose = [x for x in loose if x['id'] != li['id']]
        return lis, by_pid, loose, manual

    def product_markets(self) -> dict:
        """product id -> the channels it is for sale on now (Alex 11-12, Inventory's marketplace filter): a live listing
        (is_live) matched the one way listings meet products (_channel_match), or a hand-made mark for that channel's
        platforms. An item listed on Amazon and eBay is on both."""
        out = {}
        for ch in CHANNELS:
            _, by_pid, _, manual = self._channel_match(ch)
            for pid, lis in by_pid.items():
                if any(is_live(li) for li in lis):
                    out.setdefault(pid, set()).add(ch)
            for pid, marks in manual.items():
                if any((m.get('qty') or 0) > 0 for m in marks):
                    out.setdefault(pid, set()).add(ch)
        return out

    @staticmethod
    def _for_sale(mine: list, hand: list) -> tuple:
        """(units for sale that we ship, units for sale at the marketplace's warehouse) by is_live, plus marks."""
        fbm = sum(li['qty'] for li in mine if li['fulfillment'] == 'FBM' and is_live(li)) + sum(m['qty'] for m in hand if m['fulfillment'] == 'FBM')
        fba = sum(li['qty'] for li in mine if li['fulfillment'] == 'FBA' and is_live(li)) + sum(m['qty'] for m in hand if m['fulfillment'] == 'FBA')
        return fbm, fba

    def product_listed(self, product_id: int) -> dict:
        """For the Receive form (Alex 11-11): where this product is listed, next to what its market's boxes hold.
        One entry per channel whose listings Boxloft reads, or that has a listing or mark for the product:
          state: live (a listing we ship is for sale now), fba (only the marketplace's own stock is for sale),
                 inactive (a listing exists, none for sale now), none (not listed).
          fills: live, and the market's boxes hold fewer than the listing offers: these units are what it sells.
        `suggest` is the box letter of the first channel that is live. Same matching as stock_vs_listed. Read-only."""
        out = []
        for channel, cfg in CHANNELS.items():
            lis, by_pid, _, manual = self._channel_match(channel)
            mine, hand = by_pid.get(product_id, []), manual.get(product_id, [])
            if not mine and not hand and not (lis or channel == 'amazon'):
                continue   # a channel Boxloft can't read yet, and nobody marked it
            letter = CHANNEL_LETTER[channel]
            fbm, fba = self._for_sale(mine, hand)
            here = (self.market_stock(letter).get(product_id) or {}).get('on_hand', 0)
            state = 'live' if fbm > 0 else 'fba' if fba > 0 else 'inactive' if mine else 'none'
            out.append({'channel': channel, 'name': cfg['name'], 'letter': letter, 'state': state, 'listed': fbm,
                        'listed_fba': fba, 'in_market': here, 'fills': state == 'live' and here < fbm,
                        'listings': [{'id': li['id'], 'sku': li['sku'], 'asin': li['asin'], 'fulfillment': li['fulfillment'],
                                      'status': li['status'], 'qty': li['qty'], 'live': is_live(li)} for li in mine]})
        return {'channels': out, 'suggest': next((c['letter'] for c in out if c['state'] == 'live'), None)}

    def stock_vs_listed(self, channel: str) -> dict:
        """Every product with stock in the channel's boxes (A boxes for Amazon), next to what is for sale there.
        One reason per product:
          listed     = the listings we ship ourselves offer at least what is in the boxes
          short      = listed, but fewer than we have (`unlisted` = the difference)
          inactive   = it has a listing, but none we ship is for sale now (inactive, at 0, or only FBA is live)
          not_listed = no listing at all
          no_asin    = the product has no code a listing could match (Amazon: no ASIN); `suggestions` holds up to 5
                       listings with no product whose title is close. A person taps one; nothing links by title alone.
        Read-only. "For sale now" is is_live; hand-made marks for the channel's platforms count as listed."""
        channel = (channel or '').lower()
        if channel not in CHANNELS:
            raise WmsError('Pick Amazon, eBay or Walmart')
        cfg, letter = CHANNELS[channel], CHANNEL_LETTER[channel]
        stock = self.market_stock(letter)
        lis, by_pid, loose, manual = self._channel_match(channel)
        prods = {}
        if stock:
            marks_q = ','.join('?' * len(stock))
            for r in self.wms._q(f"""
                    SELECT p.id, p.sku, p.title, p.updated_at, p.image_url,
                           (SELECT ph.id FROM photo ph WHERE ph.product_id = p.id AND ph.removed = 0 ORDER BY ph.id LIMIT 1) AS photo_id
                      FROM product p WHERE p.id IN ({marks_q})""", tuple(stock)):
                prods[r['id']] = {**r, 'codes': []}
            for r in self.wms._q(f'SELECT product_id, kind, value FROM product_identifier WHERE product_id IN ({marks_q})', tuple(stock)):
                if r['product_id'] in prods:
                    prods[r['product_id']]['codes'].append((r['kind'], r['value']))
        loose_words = [(li, _words(li['title'])) for li in loose if li['title']]
        rows = []
        for pid, st in stock.items():
            p = prods.get(pid)
            if not p:
                continue
            mine = by_pid.get(pid, [])
            hand = manual.get(pid, [])
            fbm, fba = self._for_sale(mine, hand)
            asin = next((v for k, v in p['codes'] if k == 'ASIN'), '') or (p['sku'] if ASIN_RE.match(p['sku']) else '')
            if fbm >= st['on_hand']:
                reason = 'listed'
            elif fbm > 0:
                reason = 'short'
            elif mine or hand:
                reason = 'inactive'
            elif cfg['key'] == 'ASIN' and not asin:
                reason = 'no_asin'
            else:
                reason = 'not_listed'
            row = {'product_id': pid, 'sku': p['sku'], 'title': p['title'], 'asin': asin.upper(), 'updated_at': p['updated_at'],
                   'photo_id': p['photo_id'], 'image_url': p['image_url'], 'on_hand': st['on_hand'], 'boxes': st['boxes'],
                   'listed': fbm, 'listed_fba': fba, 'unlisted': max(0, st['on_hand'] - fbm), 'reason': reason,
                   'listings': [{'id': li['id'], 'sku': li['sku'], 'asin': li['asin'], 'fulfillment': li['fulfillment'],
                                 'status': li['status'], 'qty': li['qty'], 'live': is_live(li)} for li in mine],
                   'manual': hand, 'suggestions': []}
            if reason == 'no_asin' or (reason == 'not_listed' and not cfg['key']):
                wp = _words(p['title'])
                scored = []
                if len(wp) >= 3:
                    for li, wl in loose_words:
                        if len(wl) >= 3:
                            sc = len(wp & wl) / min(len(wp), len(wl))
                            if sc >= TITLE_MIN:
                                scored.append((sc, li))
                scored.sort(key=lambda x: (-x[0], -is_live(x[1]), x[1]['id']))
                row['suggestions'] = [{'listing_id': li['id'], 'sku': li['sku'], 'asin': li['asin'], 'title': li['title'],
                                       'fulfillment': li['fulfillment'], 'status': li['status'], 'qty': li['qty'],
                                       'price_cents': li['price_cents'], 'live': is_live(li), 'score': round(sc * 100)}
                                      for sc, li in scored[:SUGGEST_MAX]]
            rows.append(row)
        order = {'not_listed': 0, 'inactive': 1, 'no_asin': 2, 'short': 3, 'listed': 4}
        rows.sort(key=lambda r: (order[r['reason']], -r['unlisted'], r['title'].lower()))
        summary = {k: {'products': 0, 'units': 0, 'unlisted': 0} for k in order}
        for r in rows:
            g = summary[r['reason']]
            g['products'] += 1
            g['units'] += r['on_hand']
            g['unlisted'] += r['unlisted']
        summary['all'] = {'products': len(rows), 'units': sum(r['on_hand'] for r in rows),
                          'unlisted': sum(r['unlisted'] for r in rows)}
        nothing = [r for r in rows if r['reason'] in ('not_listed', 'inactive', 'no_asin')]
        summary['none_for_sale'] = {'products': len(nothing), 'units': sum(r['on_hand'] for r in nothing)}
        return {'channel': channel, 'name': cfg['name'], 'letter': letter, 'synced': bool(lis) or channel == 'amazon',
                'rows': rows, 'summary': summary,
                'channels': [{'key': ch, 'name': CHANNELS[ch]['name'], 'letter': CHANNEL_LETTER[ch]} for ch in CHANNELS]}

    # ------------------------------------------------------------------ write: find its listing

    def link(self, product_id: int, listing_id: int, user_id: int, seen=None, user_names: dict = None) -> dict:
        """A person says "this listing is that product". Puts the listing's ASIN on the product, points the listing at
        it, and lets waiting sold lines follow (relink). `seen` = {updated_at, asin} of the product as the screen showed it;
        a product someone saved since, or a listing someone linked since, is refused (Conflict). Moves no stock."""
        from audit import _et_time
        names = user_names or {}
        p = self.wms._q1('SELECT id, title, archived, updated_at, updated_by FROM product WHERE id = ?', (product_id,))
        li = self.wms._q1('SELECT * FROM listing WHERE id = ?', (listing_id,))
        if not p or p['archived']:
            raise WmsError('That product is gone; refresh the page')
        if not li or li['gone_at']:
            raise WmsError('That listing is gone; refresh the page')
        # The screen's updated_at, and the ASIN it showed (timestamps are whole seconds: two saves can share one).
        has = (self.wms._q1("SELECT value FROM product_identifier WHERE product_id = ? AND kind = 'ASIN' ORDER BY id LIMIT 1",
                            (product_id,)) or {}).get('value', '')
        if isinstance(seen, dict) and ((seen.get('updated_at') and seen['updated_at'] != p['updated_at'])
                                       or ('asin' in seen and (seen['asin'] or '').upper() != has.upper())):
            who = names.get(p['updated_by'], 'Someone') if p['updated_by'] else 'Someone'
            raise Conflict(f"{who} changed this product at {_et_time(p['updated_at'])}, after your screen loaded. "
                           'Nothing was saved: the screen now shows the new version. Check it and choose again.')
        idx = self.commerce.product_index(self.wms._conn())
        other = li['product_id'] or self.commerce.index_match(idx, li['sku'], li['asin'])
        if other:
            last = self.wms._q1('SELECT user_id, at FROM listing_link WHERE listing_id = ? AND undone_at IS NULL ORDER BY id DESC LIMIT 1',
                                (listing_id,))
            who = f"{names.get(last['user_id'], 'Someone')} linked it at {_et_time(last['at'])}. " if last else ''
            t = (self.wms._q1('SELECT title FROM product WHERE id = ?', (other,)) or {}).get('title', 'another product')
            raise Conflict(f'That listing already belongs to {"this product" if other == product_id else t}. {who}'
                           'Nothing was saved: the screen now shows the new version.')
        asin = (li['asin'] or '').upper()
        kind = 'ASIN'
        if not asin and li['channel'] == 'ebay':   # Fable 11-12 #3: sales after the listing ends still find the product
            asin, kind = li['sku'], 'EBAY_ITEM'
        now = now_utc()
        with self.wms._tx() as c:
            added = 0
            if asin:
                added = c.execute('INSERT OR IGNORE INTO product_identifier (product_id, kind, value) VALUES (?, ?, ?)',
                                  (product_id, kind, asin)).rowcount
            c.execute('UPDATE listing SET product_id = ? WHERE id = ?', (product_id, listing_id))
            waiting = {r[0] for r in c.execute('SELECT id FROM sales_line WHERE product_id IS NULL').fetchall()}
            free = {r[0] for r in c.execute('SELECT id FROM listing WHERE product_id IS NULL').fetchall()}
            self.commerce.relink(c)
            got = [r[0] for r in c.execute('SELECT id FROM sales_line WHERE product_id = ?', (product_id,)).fetchall() if r[0] in waiting]
            pulled = [r[0] for r in c.execute('SELECT id FROM listing WHERE product_id = ?', (product_id,)).fetchall()
                      if r[0] in free and r[0] != listing_id]
            c.execute('UPDATE product SET updated_at = ?, edited_at = updated_at, updated_by = ? WHERE id = ?', (now, user_id, product_id))
            link_id = c.execute("""INSERT INTO listing_link (listing_id, product_id, asin, ident_added, sales_lines, listings,
                                                            user_id, at, ident_kind) VALUES (?,?,?,?,?,?,?,?,?)""",
                                (listing_id, product_id, asin, added, json.dumps(got), json.dumps(pulled), user_id, now, kind)).lastrowid
        return {'link_id': link_id, 'product_id': product_id, 'listing_id': listing_id, 'asin': asin if kind == 'ASIN' else '',
                'sales_lines_linked': len(got), 'updated_at': now}

    def unlink(self, link_id: int, user_id: int) -> dict:
        """Undo of link(): the ASIN that link put on the product comes off, the listing has no product again, and
        sold lines that followed it and haven't shipped let go."""
        k = self.wms._q1('SELECT * FROM listing_link WHERE id = ?', (link_id,))
        if not k:
            raise WmsError('Nothing to undo')
        if k['undone_at']:
            raise WmsError('Already undone')
        now = now_utc()
        with self.wms._tx() as c:
            for lid in [k['listing_id']] + json.loads(k.get('listings') or '[]'):
                c.execute('UPDATE listing SET product_id = NULL WHERE id = ? AND product_id = ?', (lid, k['product_id']))
            if k['ident_added'] and k['asin']:
                c.execute('DELETE FROM product_identifier WHERE product_id = ? AND kind = ? AND value = ?',
                          (k['product_id'], k.get('ident_kind') or 'ASIN', k['asin']))
            for sid in json.loads(k['sales_lines'] or '[]'):
                c.execute('UPDATE sales_line SET product_id = NULL WHERE id = ? AND product_id = ? AND shipped_action_id IS NULL',
                          (sid, k['product_id']))
            c.execute('UPDATE product SET updated_at = ?, edited_at = updated_at, updated_by = ? WHERE id = ?', (now, user_id, k['product_id']))
            c.execute('UPDATE listing_link SET undone_at = ?, undone_by = ? WHERE id = ?', (now, user_id, link_id))
        return {'ok': True, 'updated_at': now}

    def _marks(self) -> dict:
        """item_id -> live marks (undone ones dropped)."""
        out = {}
        for m in self.wms._q("""SELECT m.* FROM listing_mark m WHERE m.kind != 'undo'
                                 AND NOT EXISTS (SELECT 1 FROM listing_mark u WHERE u.kind = 'undo' AND u.undoes = m.id)
                                 ORDER BY m.id"""):
            out.setdefault(m['item_id'], []).append(m)
        return out

    def annotate(self, items: list) -> dict:
        """Adds `listed` to each audit row: confirmed and manual marks first, then suggestions.
        Each entry: {state: confirmed|manual|suggested, platform, label, qty, sku, asin, status, price_cents,
                     listing_id, mark_id, match, title}. Returns counts for the summary."""
        live = self._linked(self.wms._q('SELECT * FROM listing WHERE gone_at IS NULL'))
        by_asin, by_pid, by_code = {}, {}, {}
        for li in live:
            if li['asin']:
                by_asin.setdefault(li['asin'].upper(), []).append(li)
            if li['product_id']:
                by_pid.setdefault(li['product_id'], []).append(li)
            if li['code'] and li['code_type'] in ('3', '4', 'UPC', 'EAN'):
                by_code.setdefault(li['code'].lstrip('0'), []).append(li)
        upc = {r['id']: (r['upc'] or '').lstrip('0') for r in self.wms._q(
            "SELECT id, upc FROM purchase_line WHERE upc IS NOT NULL AND upc != ''")} if self._has_col('purchase_line', 'upc') else {}
        titled = [(li, _words(li['title'])) for li in live if li['title']]
        marks = self._marks()
        # mark_seq: id of the newest person decision on the row (confirm, not this, manual, undo). The card sends it
        # back with its next decision, so a choice made from an older screen is refused (server._check_marks).
        seqs = {r['item_id']: r['n'] for r in self.wms._q('SELECT item_id, MAX(id) AS n FROM listing_mark GROUP BY item_id')}
        # rows_for_sale: audit rows with a listing that is live now (is_live) or a hand-made mark.
        # rows_for_sale_confirmed: those a person confirmed or marked. The rest are matches nobody checked yet.
        counts = {'rows_for_sale': 0, 'rows_for_sale_confirmed': 0, 'units_listed': 0}
        for it in items:
            ms = marks.get(it['id'], [])
            rejected = {m['listing_id'] for m in ms if m['kind'] == 'not_this'}
            confirmed = {m['listing_id']: m for m in ms if m['kind'] == 'confirm'}
            out = []
            for m in ms:
                if m['kind'] == 'manual':
                    out.append({'state': 'manual', 'platform': m['platform'], 'label': PLATFORMS.get(m['platform'], m['platform']),
                                'qty': m['qty'], 'url': m['url'], 'note': m['note'], 'mark_id': m['id'], 'at': m['at'],
                                'live': True})
            cands = {}
            for li, how in ([(x, 'ASIN') for x in by_asin.get((it.get('asin') or '').upper(), [])] +
                            [(x, 'product') for x in by_pid.get(it.get('product_id'), [])] +
                            [(x, 'UPC') for x in by_code.get(upc.get(it.get('line_id'), '') or '-', [])]):
                cands.setdefault(li['id'], (li, how))
            if not cands and it.get('line_id') and it.get('title'):
                # Store-email rows have no ASIN: best title match. Rows with an ASIN never use titles
                # (a different ASIN is a different product: Gears 5 vs Tony Hawk both say "Standard Edition Xbox").
                wi = _words(it['title'])
                best, score = None, 0.0
                if len(wi) >= 3:
                    for li, wl in titled:
                        if len(wl) < 3:
                            continue
                        s = len(wi & wl) / min(len(wi), len(wl))
                        if s > score:
                            best, score = li, s
                if best and score >= TITLE_MIN:
                    cands[best['id']] = (best, f'title words {round(score * 100)}%')
            for lid, m in confirmed.items():
                li = next((x for x in live if x['id'] == lid), None) or self.wms._q1('SELECT * FROM listing WHERE id = ?', (lid,))
                if li:
                    out.append(self._entry(li, 'confirmed', 'person', m['id']))
            for lid, (li, how) in cands.items():
                if lid in confirmed or lid in rejected:
                    continue
                out.append(self._entry(li, 'suggested', how, None))
            it['listed'] = out
            it['mark_seq'] = seqs.get(it['id'], 0)
            now = [x for x in out if x['live']]
            sure = [x for x in now if x['state'] in ('confirmed', 'manual')]
            counts['rows_for_sale'] += bool(now)
            counts['rows_for_sale_confirmed'] += bool(sure)
            counts['units_listed'] += sum(x['qty'] or 0 for x in sure)
        return counts

    def _has_col(self, table: str, col: str) -> bool:
        return any(r['name'] == col for r in self.wms._q(f'PRAGMA table_info({table})'))

    @staticmethod
    def _platform(li: dict) -> str:
        """amazon_fbm / amazon_fba for Amazon listings; the channel (ebay, walmart) for the rest (sweep D6)."""
        if (li.get('channel') or 'amazon') != 'amazon':
            return li['channel'] if li['channel'] in PLATFORMS else 'other'
        return 'amazon_fba' if li['fulfillment'] == 'FBA' else 'amazon_fbm'

    @staticmethod
    def _entry(li: dict, state: str, how: str, mark_id) -> dict:
        platform = Listings._platform(li)
        return {'state': state, 'platform': platform, 'label': PLATFORMS[platform], 'qty': li['qty'], 'sku': li['sku'],
                'asin': li['asin'], 'status': li['status'] if not li.get('gone_at') else 'Deleted', 'price_cents': li['price_cents'],
                'listing_id': li['id'], 'mark_id': mark_id, 'match': how, 'title': li['title'],
                'fba_inbound': li['fba_inbound'], 'live': is_live(li)}

    # ------------------------------------------------------------------ write: people

    def mark(self, item_id: int, data: dict, user_id: int) -> dict:
        kind = data.get('kind')
        if kind not in ('confirm', 'not_this', 'manual'):
            raise WmsError('Pick confirm, not this, or listed on')
        if not self.wms._q1('SELECT 1 FROM audit_item WHERE id = ?', (item_id,)):
            raise WmsError('Audit item not found')
        platform, lid, qty = '', None, None
        if kind in ('confirm', 'not_this'):
            lid = _int(data.get('listing_id'), 0)
            li = self.wms._q1('SELECT * FROM listing WHERE id = ?', (lid,))
            if not li:
                raise WmsError('That listing is gone; refresh the page')
            platform = self._platform(li)
            dup = self.wms._q1("""SELECT id FROM listing_mark m WHERE item_id = ? AND listing_id = ? AND kind = ?
                                   AND NOT EXISTS (SELECT 1 FROM listing_mark u WHERE u.kind = 'undo' AND u.undoes = m.id)""",
                               (item_id, lid, kind))
            if dup:
                return {'id': dup['id']}
        else:
            platform = data.get('platform') or ''
            if platform not in PLATFORMS:
                raise WmsError('Pick where it is listed')
            qty = _int(data.get('qty'), 0)
            if not 1 <= qty <= 100000:
                raise WmsError('Enter how many are listed (1 or more)')
        url = (data.get('url') or '').strip()[:500]
        if url and not url.lower().startswith(('http://', 'https://')):
            raise WmsError('The link should start with https://')
        with self.wms._tx() as c:
            mid = c.execute("""INSERT INTO listing_mark (item_id, kind, platform, listing_id, qty, url, note, user_id, at)
                               VALUES (?,?,?,?,?,?,?,?,?)""",
                            (item_id, kind, platform, lid, qty, url, (data.get('note') or '').strip()[:300], user_id,
                             now_utc())).lastrowid
        return {'id': mid}

    def undo_mark(self, item_id: int, mark_id: int, user_id: int) -> dict:
        m = self.wms._q1("SELECT * FROM listing_mark WHERE id = ? AND item_id = ? AND kind != 'undo'", (mark_id, item_id))
        if not m:
            raise WmsError('Nothing to undo')
        if self.wms._q1("SELECT 1 FROM listing_mark WHERE kind = 'undo' AND undoes = ?", (mark_id,)):
            raise WmsError('Already undone')
        with self.wms._tx() as c:
            c.execute("INSERT INTO listing_mark (item_id, kind, platform, listing_id, undoes, user_id, at) VALUES (?,?,?,?,?,?,?)",
                      (item_id, 'undo', m['platform'], m['listing_id'], mark_id, user_id, now_utc()))
        return {'ok': True}
