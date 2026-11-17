"""Store order emails -> purchase orders.

Any store's order confirmation becomes one normalized order. Code finds candidate values
(order-number-like tokens, money amounts, lines that could be item names); TypeSafe's Jev only
picks among those candidates, so it cannot invent a number. Code copies the picked text and
checks the arithmetic. Every order still waits for a person before it counts (Alex 2026-11-10).

Only orders paid with the business card count (Alex: Chase Visa ending 4242). The card digits
come from a regex, never from the model.
"""
import hashlib
import html as htmllib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from wms import LINE_LEFT_SQL, LINE_RECEIVED_SQL, LINE_RETURNED_SQL, now_utc

TYPESAFE_URL = 'https://api.typesafe.ai/v1/systemone'
MODEL = 'jev-latest'
NONE = 'none of these'

MONEY_RE = re.compile(r'-?\$\s?\d[\d,]*(?:\.\d{2})?')
CARD_RE = re.compile(r'(?:ending(?:\s+(?:in|with))?|x|\*{2,}|•+|card|visa|mastercard|amex|discover)[\s:•*x#-]*(\d{4})\b', re.I)
# Item prices: some stores (Maxsold) print them without a $ sign. Percentages are tax rates, not prices.
ITEM_MONEY_RE = re.compile(r'(?<![\d.%])-?\$?\s?\d[\d,]*\.\d{2}\b(?!\s?%)')
TOKEN_RE = re.compile(r'#?\b[A-Z0-9][A-Z0-9-]{4,}\b', re.I)
QTY_RE = re.compile(r'(?:qty|quantity)\s*:?\s*(\d{1,4})|(?:×|\bx)\s?(\d{1,4})\b|\b(\d{1,4})\s?(?:×|x)\b', re.I)
# A store's status or greeting headline where an item name could sit (Walmart: "We're on it! Star Wars Micro Galax... 👍",
# Alex 11-10). Never an item, for any store.
STATUS_RE = re.compile(
    r"(?i)^\W*(?:we(?:'|’)re on it|we(?:'|’)ve got (?:it|your order)|we got (?:it|your order)|we(?:'|’)re (?:getting|packing|preparing)"
    r"|thanks?(?: you)? for (?:your|the|shopping)|thank you\s*[!,.]|good news|great news|hooray|yay\b|woo-?hoo|get excited"
    r"|it(?:'|’)s official|heads up|almost there|get ready|order (?:confirmed|received|placed|update|is in)\b"
    r"|your (?:order|package|items?|delivery|pickup|purchase) (?:is|are|has|have|was|were|will)\b)")
ELLIPSIS_RE = re.compile(r'\s*(?:\.\.\.|…)')


def _norm(s: str) -> str:
    s = re.sub(r'\[img:\s*|\]', '', s or '')
    s = re.sub(r'(?i)^\s*quantity \d+ items?\s+', '', s)
    return ' '.join(re.findall(r'[a-z0-9]+', s.lower()))


def is_status_line(text: str, others=()) -> bool:
    """A status headline, or a cut-off name ("Star Wars Micro Galax...") that repeats a full name in the
    same email or order. Neither is an item of its own."""
    t = re.sub(r'\[img:\s*|\]', '', text or '').strip()
    if STATUS_RE.match(t):
        return True
    m = ELLIPSIS_RE.search(t)
    if not m:
        return False
    head = re.sub(r'^[^!?\n]{1,40}[!?]\s+', '', t[:m.start()])   # a leading "Yay! " before the cut name
    key = _norm(head)
    return len(key) >= 8 and any(len(o) > len(key) + 2 and o.startswith(key) for o in map(_norm, others))


class StoreMailError(Exception):
    pass


# ------------------------------------------------------------------ text

def text_from_html(h: str) -> str:
    """Email HTML -> lines of text. Image alt text is kept: Walmart puts item names only there."""
    t = re.sub(r'(?is)<(style|script|head)[^>]*>.*?</\1>', ' ', h)
    t = re.sub(r'(?is)<img[^>]*?alt="([^"]*)"[^>]*>', lambda m: f' [img: {m.group(1)}] ' if m.group(1).strip() else ' ', t)
    t = re.sub(r'(?i)</(tr|p|div|h\d|li|table)>|<br\s*/?>', '\n', t)
    t = re.sub(r'<[^>]+>', ' ', t)
    t = htmllib.unescape(t)
    return clean_text(t)


NOT_PRODUCT_IMG = re.compile(r'logo|icon|pixel|spacer|banner|status|barcode|facebook|instagram|pinterest|twitter|'
                             r'youtube|tiktok|stars?[_.-]|recs\.|igodigital|/wf/open|/open\?|track|badge|appstore|'
                             r'google_?play|hero|header|footer|divider|color_bar|loyalty|chat|social|arrow|cid:', re.I)


def _words(s: str) -> set:
    return {w for w in re.findall(r'[a-z0-9]+', (s or '').lower()) if len(w) > 1 and w not in ('image', 'of', 'the', 'and', 'with', 'for')}


def item_images(html: str, names: list) -> list:
    """A product photo URL for each item name (None where the email has none).
    1. An image whose alt text is the item's name (Walmart, Woot, Mattel, most Shopify stores).
    2. Else the nearest product-sized image just before the name in the email (Crate & Barrel, Macy's).
    Logos, icons, tracking pixels and recommendation grids are never picked (their URLs can carry the buyer's email)."""
    if not html or not names:
        return [None] * len(names)
    h = re.sub(r'\s+', ' ', htmllib.unescape(re.sub(r'(?is)<(style|script|head)[^>]*>.*?</\1>', ' ', html)))
    imgs = []
    for m in re.finditer(r'(?i)<img\b[^>]*>', h):
        tag = m.group(0)
        attr = lambda k: (re.search(k + r'\s*=\s*["\']([^"\']*)["\']', tag, re.I) or [None, ''])[1]  # noqa: E731
        src, alt = attr('src').strip(), attr('alt').strip()
        size = [int(v.group(0)) for v in (re.match(r'\d+', attr('width')), re.match(r'\d+', attr('height'))) if v]
        if size and min(size) < 60:
            continue
        imgs.append({'pos': m.start(), 'src': re.sub(r'^http://', 'https://', src), 'alt': re.sub(r'(?i)^image of\s+', '', alt)})
    def fits(alt, name):
        want, got = _words(name), _words(alt)
        return len(want & got) / len(want) if want and got else 0.0

    # Images labeled with some item's name belong to that item; position only picks among the rest.
    loose = [im for im in imgs if not any(fits(im['alt'], nm) >= 0.6 for nm in names)]
    out = []
    for name in names:
        want = _words(name)
        best, score = None, 0.0
        for im in imgs:
            got = _words(im['alt'])
            if want and got:
                sc = len(want & got) / len(want)
                if sc > score:
                    best, score = im, sc
        if best and score >= 0.6:
            out.append(best['src'])
            continue
        # By position: find the name in the email, then the closest product image before it.
        key = re.sub(r'\s+', ' ', name).strip()[:28].lower()
        p = h.lower().find(key) if len(key) >= 8 else -1
        before = [im for im in loose if p > 0 and 0 < p - im['pos'] < 4000]
        after = [im for im in loose if p > 0 and 0 < im['pos'] - p < 1500]
        pick = before[-1] if before else (after[0] if after else None)
        out.append(pick['src'] if pick else None)
    return out


def item_links(html: str, names: list, images: list = None) -> list:
    """The email's own link for each item ('' where it has none), Alex 11-13 "grab the link wherever possible": the
    <a> that wraps the item's photo (the one item_images picked), else the <a> whose text or image alt is the item's
    name (60%+ of its words). Raw: it may be a click tracker; resolve_link makes it a clean product page."""
    if not html or not names:
        return [''] * len(names)
    h = re.sub(r'\s+', ' ', htmllib.unescape(re.sub(r'(?is)<(style|script|head)[^>]*>.*?</\1>', ' ', html)))
    anchors = []
    for m in re.finditer(r'(?is)<a\b([^>]*)>(.*?)</a>', h):
        href = (re.search(r'href\s*=\s*["\']([^"\']*)["\']', m.group(1), re.I) or [None, ''])[1].strip()
        if not href.lower().startswith(('https://', 'http://')):
            continue
        inner = m.group(2)
        alts = ' '.join(re.findall(r'alt\s*=\s*["\']([^"\']*)["\']', inner, re.I))
        anchors.append({'href': href, 'words': _words(re.sub(r'<[^>]+>', ' ', inner) + ' ' + alts),
                        'srcs': {re.sub(r'^http://', 'https://', s.strip())
                                 for s in re.findall(r'src\s*=\s*["\']([^"\']*)["\']', inner, re.I)}})
    out = []
    for name, img in zip(names, images or [None] * len(names)):
        hit = next((a for a in anchors if img and img in a['srcs']), None)
        want = _words(name)
        if not hit and want:
            best = max(anchors, key=lambda a: len(want & a['words']), default=None)
            if best and len(want & best['words']) / len(want) >= 0.6:
                hit = best
        out.append(hit['href'] if hit else '')
    return out


# Click trackers stores send their item links through (live 11-13: Mattel trk-creations.../ls/click, Stanley
# cta.narvar.com/f/a/...). Following one is what a tap on the link in the email does; only redirects are read.
TRACKER_RE = re.compile(r'(?i)^(trk|click|clicks|links?|cta|email|e|t|ct|go|track|trk-[a-z-]+)\.|narvar|sendgrid|klaviyo|'
                        r'list-manage|mandrillapp|sparkpost|exct|exacttarget|braze|bnc\.lt|\.myshopify\.com$')
KEEP_QUERY = ('variant',)   # on a product path only a Shopify variant stays; utm_*, _kx tokens and the rest go
# Never kept anywhere: tracking tags and anything that can name the buyer.
DROP_QUERY = re.compile(r'(?i)^(utm_.*|_kx|_ke|fbclid|gclid|msclkid|mc_[ce]id|_hs.*|mkt_tok|ref|ref_|trk.*|cmp|campaign|'
                        r'e|em|email|uid|user.*|customer.*|token|sig|hash|sid|session.*)$')


def clean_link(url: str) -> str:
    """Scheme, host and path. On a product path only KEEP_QUERY keys stay; on the site's home page the query is what
    finds the item (Bookmanager: ?q=h.tviewer&...&qse=<book key>, live 11-13), so it stays less DROP_QUERY keys.
    '' for a home page with nothing left (not an item)."""
    s = urllib.parse.urlsplit(url)
    if s.scheme not in ('http', 'https') or not s.netloc:
        return ''
    home = s.path in ('', '/')
    q = [(k, v) for k, v in urllib.parse.parse_qsl(s.query)
         if not DROP_QUERY.match(k) and (home or k in KEEP_QUERY)]
    if home and not q:
        return ''
    return urllib.parse.urlunsplit(('https', s.netloc.lower(), s.path, urllib.parse.urlencode(q), ''))[:1000]


def _follow(url: str, timeout: float = 15):
    """One request with redirects off: the Location it answers with, or None."""
    class Stop(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    try:
        r = urllib.request.build_opener(Stop).open(
            urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Boxloft)'}), timeout=timeout)
        r.close()
        return None
    except urllib.error.HTTPError as e:
        loc = e.headers.get('Location') if e.code in (301, 302, 303, 307, 308) else None
        return urllib.parse.urljoin(url, loc) if loc else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def resolve_link(url: str, follow=_follow, hops: int = 5) -> str:
    """A clean product URL from an email link: click trackers and *.myshopify.com hosts are followed by their redirects
    (the product page itself is never fetched), then clean_link. A tracker that leads nowhere gives ''."""
    for _ in range(hops):
        if not url or not TRACKER_RE.search(urllib.parse.urlsplit(url).netloc):
            break
        nxt = follow(url)
        if not nxt:
            return clean_link(url) if urllib.parse.urlsplit(url).netloc.lower().endswith('.myshopify.com') else ''
        url = nxt
    else:
        return ''
    return clean_link(url) if url else ''


def clean_text(t: str) -> str:
    t = re.sub(r'\[([^\]\n]{1,300})\]\(\s*(?:https?|mailto):[^)\s]*\)', r'\1', t)  # markdown links -> their text
    t = re.sub(r'https?://\S{60,}', '[link]', t)
    t = re.sub(r'\(\s*\[link\]\s*\)', '', t)
    t = re.sub(r'\[(?!img:)([^\]\n]{1,300})\](?=[\s(]|$)', r'\1', t)  # leftover [link text]
    t = re.sub(r'[͏​-‍­﻿]', '', t)
    lines = []
    for ln in t.splitlines():
        ln = re.sub(r'[ \t\xa0|]+', ' ', ln).strip()
        if ln and not (lines and lines[-1] == ln):
            lines.append(ln)
    return '\n'.join(lines)


def card_last4(text: str) -> list:
    """Every card's last 4 digits the email prints next to wording like 'ending in'."""
    return sorted({m.group(1) for m in CARD_RE.finditer(text)
                   if re.search(r'ending|visa|card|charged|payment|amex|american express|mastercard|discover|\*{2,}\s*\d|\bx\s?-?\d', text[max(0, m.start() - 40):m.end()], re.I)})


def money(s: str):
    if s is None:
        return None
    v = s.replace('$', '').replace(',', '').replace(' ', '')
    try:
        return round(float(v), 2)
    except ValueError:
        return None


def _uniq(xs):
    seen, out = set(), []
    for x in xs:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# ------------------------------------------------------------------ TypeSafe

class Jev:
    def __init__(self, api_key: str, model: str = MODEL, timeout: float = 40):
        if not api_key:
            raise StoreMailError('TypeSafe key missing (TYPESAFE_API_KEY)')
        self.key, self.model, self.timeout = api_key, model, timeout
        self.usage = {'requests': 0, 'input_tokens': 0, 'output_tokens': 0}

    def ask(self, state, questions: dict) -> dict:
        body = json.dumps({'model': self.model, 'state': state, 'questions': questions}).encode()
        for attempt in range(4):
            req = urllib.request.Request(TYPESAFE_URL, data=body, method='POST', headers={
                'Authorization': f'Bearer {self.key}', 'Content-Type': 'application/json'})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    p = json.loads(r.read())
                break
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise StoreMailError(f'TypeSafe {e.code}: {e.read()[:300].decode("utf-8", "replace")}')
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise StoreMailError(f'TypeSafe unreachable: {e}')
        u = p.get('usage') or {}
        self.usage['requests'] += 1
        self.usage['input_tokens'] += int(u.get('input_tokens') or 0)
        self.usage['output_tokens'] += int(u.get('output_tokens') or 0)
        return p.get('answers') or {}


def default_judge():
    """The judge for store emails from the AI keys set (ai.py). TypeSafe's Jev when its key is set."""
    import ai
    try:
        return ai.make_judge()
    except ai.AiError as e:
        raise StoreMailError(str(e))


def _pick(instructions: str, options: list) -> dict:
    return {'type': 'choice', 'instructions': instructions,
            'criteria': {**{o: None for o in options}, NONE: 'None of these is the requested value.'}}


# ------------------------------------------------------------------ extraction

def _item_candidates(lines: list) -> list:
    """Indexes of lines that could be an item name. Recall first: the model rejects the rest."""
    out = []
    for i, ln in enumerate(lines):
        bare = re.sub(r'\[img:\s*|\]', '', ln).strip()
        if len(bare) < 8 or len(bare) > 250:
            continue
        if MONEY_RE.fullmatch(bare) or not re.search(r'[A-Za-z]{3}', bare):
            continue
        if re.match(r'(?i)(subtotal|total|tax|shipping|discount|order (date|number|total)|payment|visa|ending)\b', bare):
            continue
        if re.search(r'\+\s?\d+ (more )?items?\b', bare):
            continue  # a summary like "Pretty Smart Conceal a... + 1 item
        if is_status_line(bare, lines):
            continue  # "We're on it! Star Wars Micro Galax... 👍": the store's headline, not an item
        out.append(i)
    return out[:80]


def extract(text: str, jev: Jev) -> dict:
    """One email's text -> {is_order, order_number, total, items[...], flags[...], confidence}."""
    lines = text.splitlines()
    numbered = '\n'.join(f'L{i}: {ln}' for i, ln in enumerate(lines))
    amounts = _uniq(m.group(0).replace(' ', '') for m in MONEY_RE.finditer(text))[:40]
    tokens = _uniq(t.lstrip('#') for t in TOKEN_RE.findall(text) if re.search(r'\d', t)
                   and not re.fullmatch(r'\d{1,4}', t.lstrip('#')))[:40]
    cand = _item_candidates(lines)

    q = {'is_order': {'type': 'noul', 'instructions':
                      'Is this email the store\'s confirmation or receipt for a purchase the reader made '
                      '(it lists what was bought and what was charged)? Answer no for shipping, "on its way" or delivery '
                      'notices even when they repeat the order details, and for marketing, review requests, '
                      'account notices, or emails about a sale the reader made.'},
         'complete': {'type': 'noul', 'instructions':
                      'Does the email list every item in the order by name? Answer no if it says something like '
                      '"+12 items" or "See all" without listing them, or names fewer products than it says it has. '
                      'An item count that counts units (8 items = quantity 8 of one product) is complete.'}}
    if tokens:
        q['order_number'] = _pick('Which value is this purchase\'s order number (the store\'s id for the order)?', tokens)
    if amounts:
        q['total'] = _pick('Which amount is the order total the buyer was charged (after discounts, with tax and shipping)?', amounts)
    for i in cand:
        q[f'item_{i}'] = {'type': 'noul', 'instructions':
                          f'Is line L{i} the name of a product the buyer bought in this order? Answer no for '
                          'recommendations ("you may also like", "recently viewed"), headings, addresses, '
                          'store names, shipping methods, promotional text, and status headlines or greetings '
                          '("We\'re on it!", "Thanks for your order", a shortened name with "...").'}
    a = jev.ask(numbered, q)

    def choice(name):
        x = a.get(name) or {}
        c = x.get('choice')
        return (None if c in (None, NONE) else c), x.get('confidence')

    is_order = float((a.get('is_order') or {}).get('noul') or 0)
    order_number, on_conf = choice('order_number')
    total_s, total_conf = choice('total')
    item_lines = [i for i in cand if float((a.get(f'item_{i}') or {}).get('noul') or 0) >= 0.5]
    # The same product often shows twice (link text, then plain text); keep the first of a run.
    names, kept = [], []
    for i in item_lines:
        if kept and i == kept[-1] + 1 and not re.search(r'(?i)quantity \d+ item', lines[i]):
            more = re.sub(r'\[img:\s*|\]', '', lines[i]).strip(' []')
            if more not in names[-1]:
                names[-1] = (names[-1] + ' ' + more).strip()
            kept[-1] = i
            continue
        n = re.sub(r'\[img:\s*|\]', '', lines[i]).strip()
        n = re.sub(r'(?i)^quantity \d+ items?\s+', '', n).strip(' []')
        # A price between two equal names means two products (two editions of one book, Alex 11-10), not a repeat.
        priced_between = kept and any(MONEY_RE.search(lines[j]) for j in range(kept[-1] + 1, i))
        if names and not priced_between and (n == names[-1] or (i - kept[-1] <= 2 and (n in names[-1] or names[-1] in n))):
            continue
        names.append(n)
        kept.append(i)

    names = [re.sub(r'\s*[×x]\s?\d{1,4}$', '', n) for n in names]  # 'Tumbler × 1': qty is read separately
    # Image alt text ('Image of Monster High Doll') repeats an item already listed: not an item of its own.
    names = [n for n in names if not (n.lower().startswith('image of ') and n[9:].strip().lower() in {m.lower() for m in names})]
    items = []
    if kept:
        q2 = {}
        bounds = kept[1:] + [min(len(lines), kept[-1] + 15)]
        blocks = []
        for k, (start, end) in enumerate(zip(kept, bounds)):
            block = lines[start:max(end, start + 1)]
            block_text = '\n'.join(block)
            amts = _uniq(m.group(0).replace(' ', '') for m in ITEM_MONEY_RE.finditer(block_text))
            img_q = re.search(r'(?i)quantity (\d+) item', lines[start])
            # Sizes like 10''x8''x4'' are not quantities.
            qty_text = re.sub(r'\d+(?:\.\d+)?\s*(?:\'\'|"|”|in)?\s*[xX×]\s*\d+(?:\.\d+)?(?:\s*(?:\'\'|"|”|in)?\s*[xX×]\s*\d+(?:\.\d+)?)?',
                              ' ', block_text)
            qtys = _uniq([g for m in QTY_RE.finditer(qty_text) for g in m.groups() if g] +
                         ([img_q.group(1)] if img_q else []))
            if not qtys:
                # A QTY column: the count sits alone on its line (The Boxery: name, SKU, 900, $0.54, $486.00).
                # Only before the first money line (sweep D3: Maxsold lot numbers after the price read as qty 18, 62).
                before = []
                for x in block[1:]:
                    if ITEM_MONEY_RE.search(x):
                        break
                    before.append(x)
                qtys = _uniq(x.strip() for x in before if re.fullmatch(r'\s*\d{1,5}\s*', x) and int(x) > 0)
            blocks.append({'name': names[k], 'amounts': amts, 'qtys': qtys, 'text': block_text})
            if amts:
                q2[f'unit_{k}'] = _pick(f'For item {k} ("{names[k][:80]}"), which amount is the price of ONE unit? '
                                        'Answer "none of these" if only a line total for several units is shown.', amts)
                q2[f'line_{k}'] = _pick(f'For item {k} ("{names[k][:80]}"), which amount is the line total '
                                        '(unit price times quantity)?', amts)
            if len(qtys) > 1:
                q2[f'qty_{k}'] = _pick(f'For item {k} ("{names[k][:80]}"), how many units were bought?', qtys)
        a2 = jev.ask({'items': [{'item': k, 'name': b['name'], 'lines': b['text']} for k, b in enumerate(blocks)]}, q2) if q2 else {}
        for k, b in enumerate(blocks):
            def c2(name):
                x = a2.get(name) or {}
                v = x.get('choice')
                return None if v in (None, NONE) else v
            qty = int(c2(f'qty_{k}') or (b['qtys'][0] if len(b['qtys']) == 1 else 1))
            unit, line = money(c2(f'unit_{k}')), money(c2(f'line_{k}'))
            if qty > 1 and unit is not None and line is not None and abs(unit * qty - line) > 0.011:
                # One amount picked as both the unit price and the line total (Walmart prints only the order
                # total near an item: 10 dolls "at $267.52 each", Alex 11-10). The line total is the safer read.
                unit = None
            if unit is None and line is not None and qty:
                unit = round(line / qty, 2)
            if line is None and unit is not None:
                line = round(unit * qty, 2)
            items.append({'name': b['name'], 'qty': qty, 'unit_price': unit, 'line_total': line})

    total = money(total_s)
    if total is not None and len(items) > 1:
        # Walmart prints no item prices; the only amount near an item is the order total. With several
        # products that total belongs to none of them alone.
        for it in items:
            if it['line_total'] == total:
                it['unit_price'] = it['line_total'] = None
    flags = []
    if is_order < 0.5:
        flags.append('not an order confirmation')
    if not order_number:
        flags.append('no order number')
    if total is None:
        flags.append('no total')
    if not items:
        flags.append('no items found')
    if float((a.get('complete') or {}).get('noul') or 0) < 0.5:
        flags.append('email does not list every item')
    if items and any(i['unit_price'] is None for i in items):
        flags.append('some items have no price')
    lines_sum = round(sum(i['line_total'] or 0 for i in items), 2)
    if total and items and all(i['line_total'] is not None for i in items):
        # Total = items + tax + shipping - discounts. Items alone landing far outside that is a misread.
        if not (total * 0.6 <= lines_sum <= total * 1.25):
            flags.append(f'items sum ${lines_sum:.2f} does not fit total ${total:.2f}')
    return {'is_order': round(is_order, 3), 'order_number': order_number, 'order_number_conf': on_conf,
            'total': total, 'total_conf': total_conf, 'items': items, 'items_sum': lines_sum,
            'cards': card_last4(text), 'flags': flags, 'model': jev.model}


# ------------------------------------------------------------------ cancellations

CANCEL_WORD = re.compile(r'(?i)\bcancel+(?:ed|ation|ing)?\b')
REFUND_WORD = re.compile(r'(?i)\brefund')
UNITS_RE = re.compile(r'(?i)\b(\d{1,4})\s+units?\s+(?:refunded|cancel+ed)')
LINE_QTY_RE = re.compile(r'(?i)^\s*(?:qty|quantity)\s*:?\s*(\d{1,4})\b')
WHOLE_RE = re.compile(r"(?i)\b(?:your|this|the) order(?: #?[\w-]+)? (?:has been|was|is|have been) cancel+ed"
                      r"|we(?:'|’)ve cancel+ed your order|\border (?:has been )?cancel+ed\b|cancel+ation (?:of|for) (?:your )?order")
# "Some items in your order have been canceled", "One item in your order...": part of the order, so the whole-order
# wording above doesn't count (sweep D1: every line got cancelled). Such an email goes to a person as unclear.
PARTIAL_RE = re.compile(r"(?i)\b(?:some|one|two|three|four|five|\d+|an?|several|a few)\s+(?:of\s+(?:the|your)\s+)?items?\b"
                        r"[^\n.]{0,30}?\b(?:in|of|from|on)\s+(?:your|this|the)\s+order|\bpart of (?:your|this|the) order")
# Where a cancellation email starts repeating the whole order (Woot: "ORDER DETAILS" lists every item again).
DETAILS_RE = re.compile(r'(?i)^\s*(?:original )?order (?:details|summary|content)\s*$|^\s*items in (?:your|this) order\s*$')


def over_total(items: list, total) -> bool:
    """The read items' prices add up to over 1.25 x the order total (+$1). total in dollars."""
    return bool(total) and all(i.get('unit_price') is not None for i in items) and \
        round(sum((i.get('line_total') or 0) for i in items) * 100) > total * 125 + 100


def keep_once(items: list, total) -> list:
    """Indexes of the items to keep. When the items add up to over the total and dropping exact repeats (same name,
    qty and price) makes them fit, the email listed them twice (a summary and the details: Crate & Barrel, sweep D8).
    Otherwise all are kept: two registrations of one domain product are two real lines."""
    every = list(range(len(items)))
    if not over_total(items, total):
        return every
    seen, keep = set(), []
    for n, i in enumerate(items):
        k = (str(i.get('name') or '').strip().lower(), i.get('qty'), i.get('unit_price'))
        if k not in seen:
            seen.add(k)
            keep.append(n)
    return keep if len(keep) < len(items) and not over_total([items[n] for n in keep], total) else every


def et_day(sent_at):
    """'2026-05-04T01:40:37Z' -> '2026-05-03': the day in Springfield (sweep D7: 46 of 384 store orders were a day late)."""
    from datetime import datetime
    from inbound import ET
    s = str(sent_at or '')
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00')).astimezone(ET).date().isoformat()
    except ValueError:
        return s[:10] or None


def cancel_kind(subject: str, text: str):
    """'cancel', 'refund' or None. The body's first lines count too (some subjects say only "Order update")."""
    head = '\n'.join((text or '').splitlines()[:8])
    if CANCEL_WORD.search(subject or '') or CANCEL_WORD.search(head):
        return 'cancel'
    return 'refund' if REFUND_WORD.search(subject or '') else None


def cancel_plan(kind: str, text: str, titles: dict) -> dict:
    """Which order lines a cancellation email cancels, by code only: each item the email names goes to the one
    order line its words fit best. titles = {line_id: title}. Returns {'lines': {line_id: qty or None (= all left)},
    'whole': bool, 'unclear': [names that fit two lines equally]}. A refund counts only where it names units."""
    lines = (text or '').splitlines()
    for k, ln in enumerate(lines):
        if DETAILS_RE.match(ln):
            lines = lines[:k]
            break
    found, unclear = {}, []
    want = {lid: _words(t) for lid, t in titles.items()}
    for k, ln in enumerate(lines):
        bare = re.sub(r'\[img:\s*|\]', '', ln).strip()
        qm = re.match(r'(?i)quantity (\d+) items?\s+', bare)
        name = bare[qm.end():] if qm else bare
        got = _words(name)
        if len(got) < 3:
            continue
        scores = []
        for lid, w in want.items():
            common = len(w & got)
            if w and common >= 3 and (common / len(w) >= 0.6 or common / len(got) >= 0.8):
                scores.append((common / len(w) + common / len(got), lid))
        if not scores:
            continue
        scores.sort(key=lambda s: -s[0])
        if len(scores) > 1 and scores[0][0] - scores[1][0] < 0.05:
            unclear.append(name[:80])
            continue
        lid = scores[0][1]
        if lid in found:
            continue
        qty = int(qm.group(1)) if qm else None
        if qty is None:
            for nxt in lines[k + 1:k + 4]:
                m = LINE_QTY_RE.match(nxt) or UNITS_RE.search(nxt)
                if m:
                    qty = int(m.group(1))
                    break
        found[lid] = qty
    if kind == 'refund':
        found = {lid: q for lid, q in found.items() if q}
    whole = False
    joined = '\n'.join(lines)
    if not found and not unclear and kind == 'cancel' and WHOLE_RE.search(joined) and not PARTIAL_RE.search(joined):
        found, whole = {lid: None for lid in titles}, True
    return {'lines': found, 'whole': whole, 'unclear': unclear}


CANCEL_QUERY = ('subject:(cancel OR canceled OR cancelled OR cancellation OR refund OR refunded) '
                '-from:amazon.com -from:ebay.com -category:promotions -category:social')
CANCEL_SCHEMA = """
CREATE TABLE IF NOT EXISTS store_cancel (
    message_id TEXT PRIMARY KEY,      -- each cancellation or refund email is read once
    at TEXT NOT NULL,
    sent_at TEXT,
    sender TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL,            -- cancelled | partial | unclear | nothing_left | no_order | not_cancel | skipped_store
    po_id INTEGER,
    result TEXT                       -- what was cancelled (JSON): line ids, qty before, units off
);
"""


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ------------------------------------------------------------------ Gmail -> purchase orders

# Order-ish subjects from any store, minus the channels that have their own sync (Amazon via the
# extension; eBay buys are tracked elsewhere, Alex 2026-11-10).
ORDER_QUERY = ('subject:(order OR receipt OR invoice OR confirmed OR confirmation OR purchase) '
               '-from:amazon.com -from:ebay.com -category:promotions -category:social')
FIRST_DAYS = 365

SCHEMA = """
CREATE TABLE IF NOT EXISTS store_mail (
    message_id TEXT PRIMARY KEY,      -- the email's Message-ID header: each email is read once
    at TEXT NOT NULL,
    sent_at TEXT,
    sender TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL,            -- order | other_card | not_order | no_money | skipped_store | error
    po_id INTEGER,
    result TEXT                       -- extractor output (JSON), kept for checking
);
"""


# Senders that are not stores we buy stock from (services, bills). Alex 11-10: "shipbob, stripe, godaddy
# are not retailers we need to bring into the dash". Seeded once; people add more from Arriving.
SKIP_SEED = ('shipbob.com', 'stripe.com', 'godaddy.com')
# Stores we buy packing supplies from: their orders skip review and feed the Ship boxes page (box counts coming).
SUPPLY_STORES = ('theboxery.com',)
SKIP_SCHEMA = """
CREATE TABLE IF NOT EXISTS store_skip (
    store TEXT PRIMARY KEY,           -- store_of(sender): emails from it are skipped before any AI call
    at TEXT NOT NULL
);
"""


# Senders that mail for many stores; the store's name is then the label in front of them.
RELAYS = ('narvar.com', 'narvarnotify.com', 'shopifyemail.com', 'myshopify.com', 'klaviyomail.com', 'sendgrid.net')


def store_of(sender: str) -> str:
    """'Walmart.com <help@walmart.com>' -> 'walmart.com' (the sender's registered domain)."""
    m = re.search(r'@([\w.-]+)', sender or '')
    host = m.group(1).lower() if m else 'unknown'
    for relay in RELAYS:
        if host.endswith('.' + relay):
            return host[:-len(relay) - 1].split('.')[-1]
    parts = host.split('.')
    return '.'.join(parts[-3:] if len(parts) > 2 and parts[-2] in ('co', 'com') and len(parts[-1]) == 2 else parts[-2:])


def mail_html(msg) -> str:
    for part in msg.walk():
        if part.get_content_type() == 'text/html' and not part.get('Content-Disposition', '').startswith('attachment'):
            try:
                return part.get_payload(decode=True).decode(part.get_content_charset() or 'utf-8', 'replace')
            except Exception:  # noqa: BLE001
                return ''
    return ''


def mail_text(msg) -> str:
    """email.message.Message -> text. HTML wins: it carries Walmart's item names in image alt text."""
    html_part = plain = None
    for part in msg.walk():
        ct = part.get_content_type()
        if part.get_content_maintype() == 'multipart' or part.get('Content-Disposition', '').startswith('attachment'):
            continue
        try:
            body = part.get_payload(decode=True).decode(part.get_content_charset() or 'utf-8', 'replace')
        except Exception:  # noqa: BLE001 - a broken part is skipped, not fatal
            continue
        if ct == 'text/html' and html_part is None:
            html_part = body
        elif ct == 'text/plain' and plain is None:
            plain = body
    return text_from_html(html_part) if html_part else clean_text(plain or '')


class StoreOrders:
    """Order confirmations from any store in Gmail -> purchase_order + purchase_line, held for review."""

    def __init__(self, wms, connections, jev_factory=None, image_check=None):
        self.wms, self.connections = wms, connections
        self.image_check = image_check   # url -> bool: does the photo load from here? (None: keep all; tests)
        self.jev_factory = jev_factory or default_judge   # ai.py: TypeSafe, Anthropic or OpenAI, by key
        conn = wms._conn()
        conn.executescript(SCHEMA)
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'store_skip'").fetchone():
            conn.executescript(SKIP_SCHEMA)
            conn.executemany('INSERT OR IGNORE INTO store_skip (store, at) VALUES (?, ?)', [(d, now_utc()) for d in SKIP_SEED])
            if conn.execute("SELECT 1 FROM pragma_table_info('purchase_order') WHERE name = 'review'").fetchone():
                conn.execute(f"""UPDATE purchase_order SET hidden = 1 WHERE account = 'gmail' AND review != '' AND hidden = 0
                                  AND source IN ({','.join('?' * len(SKIP_SEED))})""", SKIP_SEED)
            conn.commit()
        wms._add_column(conn, 'purchase_order', 'review', "TEXT NOT NULL DEFAULT ''")
        wms._add_column(conn, 'purchase_order', 'kind', "TEXT NOT NULL DEFAULT 'stock'")
        wms._add_column(conn, 'purchase_order', 'mail_id', 'TEXT')
        wms._add_column(conn, 'purchase_order', 'page_read_at', 'TEXT')   # last read of the store's order page
        wms._add_column(conn, 'purchase_line', 'store_item_id', 'TEXT')   # e.g. Walmart's usItemId
        wms._add_column(conn, 'purchase_line', 'store_url', 'TEXT')       # the item's page, from the email's link (resolve_link)
        self.resolve = resolve_link   # tests swap in a fake: no network
        conn.executescript(CANCEL_SCHEMA)

    # ---- one email
    def take(self, message_id: str, sender: str, subject: str, sent_at, text: str, jev, card: str, html: str = '') -> dict:
        """Read one email (once). Returns {'outcome', 'po_id'}."""
        if self.wms._q1('SELECT 1 AS x FROM store_mail WHERE message_id = ?', (message_id,)):
            return {'outcome': 'seen', 'po_id': None}
        outcome, po_id, result = 'not_order', None, None
        if self.wms._q1('SELECT 1 AS x FROM store_skip WHERE store = ?', (store_of(sender),)):
            outcome = 'skipped_store'
        elif not MONEY_RE.search(text):
            outcome = 'no_money'
        elif CANCEL_WORD.search(subject or ''):
            outcome = 'not_order'   # "Canceled: delivery from order #..." is read by the cancellation pass, not the AI
        else:
            try:
                result = extract(text, jev)
            except StoreMailError as e:
                result, outcome = {'error': str(e)}, 'error'
            if result and 'error' not in result:
                if result['is_order'] < 0.5 or not result['order_number'] or result['total'] is None:
                    outcome = 'not_order'
                elif result['cards'] and card and card not in result['cards']:
                    outcome = 'other_card'
                else:
                    outcome = 'order'
                    names = [i['name'] for i in result['items']]
                    images = self._usable(item_images(html, names))
                    links = [self.resolve(u) if u else '' for u in item_links(html, names, images)]   # before the lock
                    with self.wms.op_lock:   # sweep B-u1: the Gmail thread waits for a save in progress
                        po_id = self._save(store_of(sender), message_id, sent_at, result, card, images, links)
        if outcome == 'error':
            return {'outcome': outcome, 'po_id': None, 'error': result['error']}  # not recorded: retried next run
        with self.wms._tx() as c:
            c.execute("""INSERT OR IGNORE INTO store_mail (message_id, at, sent_at, sender, subject, outcome, po_id, result)
                         VALUES (?,?,?,?,?,?,?,?)""", (message_id, now_utc(), sent_at, sender[:200], subject[:300], outcome,
                                                       po_id, json.dumps(result) if result else None))
        return {'outcome': outcome, 'po_id': po_id}

    def _save(self, store: str, message_id: str, sent_at, r: dict, card: str, images: list = None, links: list = None) -> int:
        from inbound import Inbound
        notes = list(r['flags'])
        if not r['cards']:
            notes.append('the email does not show the card')
        total_cents = round(r['total'] * 100)
        images = images or [None] * len(r['items'])
        links = links or [''] * len(r['items'])
        keep = keep_once(r['items'], r['total'])
        if len(keep) < len(r['items']):
            n = len(r['items']) - len(keep)
            notes.append(f"{n} item{'s' if n != 1 else ''} listed twice in the email counted once")
            r = dict(r, items=[r['items'][k] for k in keep])
            images = [images[k] for k in keep]
            links = [links[k] for k in keep]
        if over_total(r['items'], r['total']):
            raw = sum((i.get('line_total') or 0) for i in r['items'])
            notes.append(f"the items add up to ${raw:,.2f} but the order total is ${r['total']:,.2f}: check the quantities")
        review = '; '.join(notes) or 'new store order'
        items = [{'title': i['name'], 'qty': i['qty'], 'image': img,
                  'price_cents': round(i['unit_price'] * 100) if i['unit_price'] is not None else 0} for i, img in zip(r['items'], images)]
        priced = items and all(i['unit_price'] is not None for i in r['items'])
        sub_cents = round(sum(i['line_total'] or 0 for i in r['items']) * 100) if priced else None
        ordered = et_day(sent_at)
        with self.wms._tx() as c:
            row = c.execute('SELECT id FROM purchase_order WHERE source = ? AND external_id = ?', (store, r['order_number'])).fetchone()
            if row:
                return row[0]  # a second confirmation for the same order (resent, edited): keep the first
            po = c.execute("""INSERT INTO purchase_order (source, external_id, account, ordered_at, imported_at, state,
                                                          status_text, last_seen_at, total_cents, review, mail_id)
                              VALUES (?,?,?,?,?,'ordered',?,?,?,?,?)""",
                           (store, r['order_number'], 'gmail', ordered, now_utc(), 'Ordered (store email)', now_utc(),
                            total_cents, review[:300], message_id)).lastrowid
            c.execute("""INSERT INTO purchase_event (po_id, at, state, status_text, expected_from, expected_to)
                         VALUES (?,?,?,?,NULL,NULL)""", (po, now_utc(), 'ordered', f'Read from {store} order email'))
            if items:
                keys = Inbound._upsert_lines(c, po, items, sub_cents, total_cents if sub_cents else None)
                for it, url in zip(items, links):
                    if url and Inbound._line_key(it) in keys:
                        c.execute('UPDATE purchase_line SET store_url = ? WHERE id = ?', (url, keys[Inbound._line_key(it)]))
            if store in SUPPLY_STORES:
                self._as_supplies(c, po)
        return po

    @staticmethod
    def _as_supplies(c, po: int):
        """A packing-supply order: no review, off Arriving and the audit, box lines counted as coming on Ship boxes."""
        from commerce import link_ship_boxes
        c.execute("UPDATE purchase_order SET kind = 'supplies', review = '', status_text = 'Ordered (supplies)' WHERE id = ?", (po,))
        link_ship_boxes(c, po)

    def _usable(self, urls: list) -> list:
        """Drop photo links that don't load from here (The Boxery answers 403 to anyone but its own pages)."""
        if not self.image_check:
            return urls
        return [u if u and self.image_check(u) else None for u in urls]

    def make_supplies(self, store: str, received_before: str, jev=None) -> dict:
        """Turn a store's existing orders into supply orders (one-off, when a store joins SUPPLY_STORES).
        Each email is read again for the right quantities. Orders placed before received_before (YYYY-MM-DD)
        count as already received: their boxes came long ago, and on-hand counts are set by a person."""
        import email
        import imaplib
        from inbound import Inbound
        jev = jev or self.jev_factory()
        cur = self.connections.get('gmail')
        orders = self.wms._q("SELECT id, mail_id, total_cents, ordered_at FROM purchase_order WHERE source = ? AND account = 'gmail'",
                             (store,))
        counts = {'orders': len(orders), 'reread': 0, 'received_before': 0}
        m = imaplib.IMAP4_SSL('imap.gmail.com', timeout=30)
        try:
            m.login(cur['email'], cur['app_password'])
            m.select('"[Gmail]/All Mail"', readonly=True)
            for o in orders:
                items = None
                typ, d = m.uid('SEARCH', None, 'HEADER', 'Message-ID', (o['mail_id'] or '').strip('<>'))
                uids = d[0].split() if typ == 'OK' and d and d[0] else []
                if uids:
                    typ, md = m.uid('FETCH', uids[0], '(BODY.PEEK[])')
                    if typ == 'OK' and md and md[0]:
                        r = extract(mail_text(email.message_from_bytes(md[0][1])), jev)
                        if r.get('items') and all(i['unit_price'] is not None for i in r['items']):
                            items = [{'title': i['name'], 'qty': i['qty'], 'price_cents': round(i['unit_price'] * 100)} for i in r['items']]
                            sub = round(sum(i['line_total'] or 0 for i in r['items']) * 100)
                with self.wms._tx() as c:
                    if items:
                        Inbound._upsert_lines(c, o['id'], items, sub, o['total_cents'])
                        c.execute('UPDATE purchase_line SET image_url = NULL WHERE po_id = ?', (o['id'],))
                        counts['reread'] += 1
                    self._as_supplies(c, o['id'])
                    if (o['ordered_at'] or '') < received_before:
                        c.execute('UPDATE purchase_line SET supply_received = qty WHERE po_id = ?', (o['id'],))
                        counts['received_before'] += 1
        finally:
            try:
                m.logout()
            except Exception:  # noqa: BLE001
                pass
        return counts

    def fill_links(self, progress=None) -> dict:
        """Item links for store-order lines read before links were kept (Alex 11-13): fetch each order's email again by
        Message-ID (read-only), pick each line's link and resolve it. Lines that have one are left alone. Walmart is
        skipped: its emails carry no item links, and its lines link by item number."""
        orders = self.wms._q("""SELECT o.id, o.mail_id FROM purchase_order o WHERE o.account = 'gmail' AND o.hidden = 0
                                   AND o.mail_id IS NOT NULL AND o.source != 'walmart.com' AND EXISTS (SELECT 1 FROM purchase_line l
                                   WHERE l.po_id = o.id AND l.cancelled = 0 AND COALESCE(l.store_url, '') = '')""")
        counts = {'orders': len(orders), 'emails_found': 0, 'lines_linked': 0, 'lines_without': 0}

        def each(o, html):
            lines = self.wms._q("""SELECT id, title, image_url FROM purchase_line WHERE po_id = ? AND cancelled = 0
                                     AND COALESCE(store_url, '') = ''""", (o['id'],))
            raw = item_links(html, [ln['title'] for ln in lines], [ln['image_url'] for ln in lines])
            got = [(ln['id'], self.resolve(u) if u else '') for ln, u in zip(lines, raw)]
            with self.wms._tx() as c:
                for lid, url in got:
                    if url:
                        c.execute("UPDATE purchase_line SET store_url = ? WHERE id = ? AND COALESCE(store_url, '') = ''", (url, lid))
                        counts['lines_linked'] += 1
                    else:
                        counts['lines_without'] += 1
        counts['emails_found'] = self._each_email(orders, each, progress)
        return counts

    def _each_email(self, orders: list, fn, progress=None) -> int:
        """fn(order, html) for each order's email, fetched read-only by Message-ID. Returns how many were found."""
        import email
        import imaplib
        cur = self.connections.get('gmail')
        if not cur.get('email') or not cur.get('app_password'):
            raise StoreMailError('Gmail is not connected')
        found = 0
        m = imaplib.IMAP4_SSL('imap.gmail.com', timeout=30)
        try:
            m.login(cur['email'], cur['app_password'])
            m.select('"[Gmail]/All Mail"', readonly=True)
            for n, o in enumerate(orders, 1):
                if progress:
                    progress(f'{n} of {len(orders)} orders')
                typ, d = m.uid('SEARCH', None, 'HEADER', 'Message-ID', o['mail_id'].strip('<>'))
                uids = d[0].split() if typ == 'OK' and d and d[0] else []
                if not uids:
                    continue
                typ, md = m.uid('FETCH', uids[0], '(BODY.PEEK[])')
                if typ != 'OK' or not md or not md[0]:
                    continue
                found += 1
                fn(o, mail_html(email.message_from_bytes(md[0][1])))
        finally:
            try:
                m.logout()
            except Exception:  # noqa: BLE001
                pass
        return found

    def fill_images(self, progress=None) -> dict:
        """Photos for store-order lines read before photos were kept: fetch each order's email again by
        Message-ID (read-only) and pick each line's photo. Lines that already have one are left alone."""
        import email
        import imaplib
        cur = self.connections.get('gmail')
        if not cur.get('email') or not cur.get('app_password'):
            raise StoreMailError('Gmail is not connected')
        orders = self.wms._q("""SELECT o.id, o.mail_id FROM purchase_order o WHERE o.account = 'gmail' AND o.hidden = 0
                                   AND o.mail_id IS NOT NULL AND EXISTS (SELECT 1 FROM purchase_line l WHERE l.po_id = o.id
                                   AND COALESCE(l.image_url, '') = '')""")
        counts = {'orders': len(orders), 'emails_found': 0, 'lines_filled': 0, 'lines_without': 0}
        m = imaplib.IMAP4_SSL('imap.gmail.com', timeout=30)
        try:
            m.login(cur['email'], cur['app_password'])
            m.select('"[Gmail]/All Mail"', readonly=True)
            for n, o in enumerate(orders, 1):
                if progress:
                    progress(f'{n} of {len(orders)} orders')
                typ, d = m.uid('SEARCH', None, 'HEADER', 'Message-ID', o['mail_id'].strip('<>'))
                uids = d[0].split() if typ == 'OK' and d and d[0] else []
                if not uids:
                    continue
                typ, md = m.uid('FETCH', uids[0], '(BODY.PEEK[])')
                if typ != 'OK' or not md or not md[0]:
                    continue
                counts['emails_found'] += 1
                html = mail_html(email.message_from_bytes(md[0][1]))
                lines = self.wms._q("SELECT id, title FROM purchase_line WHERE po_id = ? AND COALESCE(image_url, '') = ''", (o['id'],))
                with self.wms._tx() as c:
                    for ln, url in zip(lines, self._usable(item_images(html, [ln['title'] for ln in lines]))):
                        if url:
                            c.execute('UPDATE purchase_line SET image_url = ? WHERE id = ?', (url[:1000], ln['id']))
                            counts['lines_filled'] += 1
                        else:
                            counts['lines_without'] += 1
        finally:
            try:
                m.logout()
            except Exception:  # noqa: BLE001
                pass
        return counts

    # ---- review
    def skip_store(self, store: str, skip: bool = True) -> dict:
        """Not a store we buy from: skip its future emails and hide its orders still waiting for review."""
        store = (store or '').strip().lower()
        if not re.fullmatch(r'[a-z0-9.-]{2,100}', store):
            raise StoreMailError('Give a store name like shipbob.com')
        with self.wms._tx() as c:
            if not skip:
                c.execute('DELETE FROM store_skip WHERE store = ?', (store,))
                return {'store': store, 'hidden': 0}
            c.execute('INSERT OR IGNORE INTO store_skip (store, at) VALUES (?, ?)', (store, now_utc()))
            n = c.execute("""UPDATE purchase_order SET hidden = 1 WHERE account = 'gmail' AND source = ?
                             AND review != '' AND hidden = 0""", (store,)).rowcount
        return {'store': store, 'hidden': n}

    def skipped_stores(self) -> list:
        return [r['store'] for r in self.wms._q('SELECT store FROM store_skip ORDER BY store')]

    def to_review(self) -> list:
        rows = self.wms._q("""SELECT id, source AS store, external_id AS order_id, ordered_at, total_cents, review, mail_id
                                FROM purchase_order WHERE account = 'gmail' AND review != '' AND hidden = 0 AND kind != 'supplies'
                                 AND state != 'cancelled' ORDER BY ordered_at DESC""")
        for o in rows:
            o['lines'] = self.wms._q('SELECT id, title, qty, unit_cost_cents, image_url, store_url FROM purchase_line WHERE po_id = ? AND cancelled = 0 ORDER BY id',
                                     (o['id'],))
        return rows

    # ---- lines that are not items
    def clean_lines(self) -> dict:
        """Store-order lines saved before the status filter: a status headline ("We're on it! ...") is cancelled
        unless something was received against it; a bare note like "(On order)" gets the name of the line before it
        (two editions of one book whose second name was taken for a repeat)."""
        rows = self.wms._q(f"""SELECT l.id, l.po_id, l.title, {LINE_RECEIVED_SQL} AS got, o.state FROM purchase_line l
                                 JOIN purchase_order o ON o.id = l.po_id
                                WHERE o.account = 'gmail' AND l.cancelled = 0 ORDER BY l.po_id, l.id""")
        by_po = {}
        for r in rows:
            by_po.setdefault(r['po_id'], []).append(r)
        out = {'status_lines': 0, 'renamed': 0, 'kept_received': 0, 'orders': set(), 'lines': []}
        with self.wms._tx() as c:
            for po, lines in by_po.items():
                prev = None   # the last real item line before this one
                for r in lines:
                    others = [x['title'] for x in lines if x['id'] != r['id']]
                    if is_status_line(r['title'], others):
                        if r['got']:
                            out['kept_received'] += 1
                            continue
                        c.execute('UPDATE purchase_line SET cancelled = 1 WHERE id = ?', (r['id'],))
                        c.execute("""INSERT INTO purchase_event (po_id, at, state, status_text, expected_from, expected_to)
                                     VALUES (?,?,?,?,NULL,NULL)""", (po, now_utc(), r['state'], f'Not an item (store status line): {r["title"]}'[:200]))
                        out['status_lines'] += 1
                        out['orders'].add(po)
                        out['lines'].append(r['id'])
                    elif re.fullmatch(r'\(.{2,60}\)', r['title'].strip()) and prev:
                        name = re.split(r'\s*\(', prev['title'], maxsplit=1)[0].strip()
                        if len(name) >= 3:
                            c.execute('UPDATE purchase_line SET title = ? WHERE id = ?', (f'{name} {r["title"].strip()}', r['id']))
                            out['renamed'] += 1
                            out['lines'].append(r['id'])
                    else:
                        prev = r
            # The headline made a one-item order look like two, so the item got no cost (Walmart prints only the
            # total). Left with one item and nothing received, it gets the total, as a fresh read gives it.
            out['costed'] = 0
            for po in out['orders']:
                live = c.execute(f"""SELECT l.id, l.qty, l.unit_cost_cents, {LINE_RECEIVED_SQL} FROM purchase_line l
                                      WHERE l.po_id = ? AND l.cancelled = 0""", (po,)).fetchall()
                total = c.execute('SELECT total_cents FROM purchase_order WHERE id = ?', (po,)).fetchone()[0]
                if len(live) == 1 and live[0][2] == 0 and not live[0][3] and total and live[0][1]:
                    c.execute('UPDATE purchase_line SET unit_cost_cents = ? WHERE id = ?', (round(total / live[0][1]), live[0][0]))
                    out['costed'] += 1
        out['orders'] = len(out['orders'])
        return out

    # ---- cancellations
    def take_cancel(self, message_id: str, sender: str, subject: str, sent_at, text: str) -> dict:
        """One cancellation or refund email (once): find its order by number, take the cancelled units off."""
        if self.wms._q1('SELECT 1 AS x FROM store_cancel WHERE message_id = ?', (message_id,)):
            return {'outcome': 'seen', 'po_id': None}
        store, kind = store_of(sender), cancel_kind(subject, text)
        outcome, po_id, result = 'no_order', None, None
        if not kind:
            outcome = 'not_cancel'
        elif self.wms._q1('SELECT 1 AS x FROM store_skip WHERE store = ?', (store,)):
            outcome = 'skipped_store'
        else:
            toks = {re.sub(r'[^0-9a-z]', '', t.lower()) for t in TOKEN_RE.findall(f'{subject}\n{text}')}
            match = [o for o in self.wms._q("SELECT id, external_id FROM purchase_order WHERE source = ? AND account = 'gmail'", (store,))
                     if len(re.sub(r'[^0-9a-z]', '', o['external_id'].lower())) >= 5
                     and re.sub(r'[^0-9a-z]', '', o['external_id'].lower()) in toks]
            if len(match) == 1:
                po_id = match[0]['id']
                with self.wms.op_lock:   # sweep B-u1: a cancel never lands between a save's check and its write
                    outcome, result = self._apply_cancel(po_id, kind, text, store)
        with self.wms._tx() as c:
            c.execute("""INSERT OR IGNORE INTO store_cancel (message_id, at, sent_at, sender, subject, outcome, po_id, result)
                         VALUES (?,?,?,?,?,?,?,?)""", (message_id, now_utc(), sent_at, sender[:200], subject[:300], outcome, po_id,
                                                       json.dumps(result) if result else None))
        return {'outcome': outcome, 'po_id': po_id}

    def _apply_cancel(self, po: int, kind: str, text: str, store: str) -> tuple:
        with self.wms._tx() as c:
            lines = [dict(zip(('id', 'title', 'qty', 'left_qty', 'returned'), r)) for r in c.execute(
                f"""SELECT l.id, l.title, l.qty, {LINE_LEFT_SQL}, {LINE_RETURNED_SQL} FROM purchase_line l
                     WHERE l.po_id = ? AND l.cancelled = 0 ORDER BY l.id""", (po,))]
            o = dict(zip(('state', 'review'), c.execute('SELECT state, review FROM purchase_order WHERE id = ?', (po,)).fetchone()))
            plan = cancel_plan(kind, text, {ln['id']: ln['title'] for ln in lines})
            done = []
            for ln in lines:
                if ln['id'] not in plan['lines']:
                    continue
                want = plan['lines'][ln['id']]
                if kind == 'refund' and want is not None:
                    want -= ln['returned']   # sweep D2: the refund pays for units a return already took off
                n = min(ln['left_qty'], want if want is not None else ln['left_qty'])
                if n <= 0:
                    continue
                if n >= ln['qty']:
                    c.execute('UPDATE purchase_line SET cancelled = 1 WHERE id = ?', (ln['id'],))
                else:
                    c.execute('UPDATE purchase_line SET qty = qty - ? WHERE id = ?', (n, ln['id']))
                done.append({'line_id': ln['id'], 'title': ln['title'][:120], 'qty_before': ln['qty'], 'units_off': n})
            live = c.execute('SELECT COUNT(*) FROM purchase_line WHERE po_id = ? AND cancelled = 0', (po,)).fetchone()[0]
            word = 'Refunded' if kind == 'refund' else 'Cancelled'
            if done and not live:
                outcome = 'cancelled'
                c.execute("""UPDATE purchase_order SET state = 'cancelled', status_text = ?, status_changed_at = ?, last_seen_at = ?
                             WHERE id = ?""", (f'{word} by {store} (email)', now_utc(), now_utc(), po))
                c.execute("""INSERT INTO purchase_event (po_id, at, state, status_text, expected_from, expected_to)
                             VALUES (?,?,'cancelled',?,NULL,NULL)""", (po, now_utc(), f'{word} by {store} (email)'))
            elif done:
                outcome = 'partial'
                units = ', '.join(f"{d['units_off']} x {d['title'][:40]}" for d in done)
                c.execute("""INSERT INTO purchase_event (po_id, at, state, status_text, expected_from, expected_to)
                             VALUES (?,?,?,?,NULL,NULL)""", (po, now_utc(), o['state'], f'{word} by {store} (email): {units}'[:200]))
            elif kind == 'refund' and not plan['unclear']:
                return 'refund_no_units', {'kind': kind, 'lines': []}   # a price adjustment or a service refund: no units off
            else:
                outcome = 'nothing_left' if plan['lines'] else 'unclear'
            if plan['unclear'] or outcome == 'unclear':
                # The email names items Boxloft can't place: a person checks the order (it leaves Arriving until then).
                note = f'the store sent a {kind} email: check which items it covers'
                if note not in (o['review'] or ''):
                    c.execute('UPDATE purchase_order SET review = ? WHERE id = ?', ('; '.join(x for x in (o['review'], note) if x)[:300], po))
                if outcome != 'cancelled':
                    outcome = 'unclear'
        return outcome, {'kind': kind, 'lines': done, 'unclear': plan['unclear'], 'whole_by_wording': plan['whole']}

    def _cancel_pass(self, m, days: int, progress=None) -> dict:
        """Cancellation and refund emails from stores we have orders from. Bodies are fetched only for those."""
        import email
        import email.header
        import email.utils
        counts = {}
        stores = {r['source'] for r in self.wms._q("SELECT DISTINCT source FROM purchase_order WHERE account = 'gmail'")}
        typ, d = m.uid('SEARCH', 'X-GM-RAW', f'"{CANCEL_QUERY} newer_than:{days}d"')
        uids = d[0].split() if typ == 'OK' and d and d[0] else []
        for n, uid in enumerate(uids, 1):
            if progress:
                progress(f'cancellations: {n} of {len(uids)} emails')
            typ, hd = m.uid('FETCH', uid, '(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT DATE)])')
            hdr = email.message_from_bytes(hd[0][1]) if typ == 'OK' and hd and hd[0] else None
            if hdr is None:
                continue
            mid = (hdr.get('Message-ID') or f'uid-{uid.decode()}').strip()
            if self.wms._q1('SELECT 1 AS x FROM store_cancel WHERE message_id = ?', (mid,)):
                counts['seen'] = counts.get('seen', 0) + 1
                continue
            subj = str(email.header.make_header(email.header.decode_header(hdr.get('Subject', ''))))
            sent = None
            try:
                sent = email.utils.parsedate_to_datetime(hdr.get('Date')).astimezone(
                    __import__('datetime').timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            except Exception:  # noqa: BLE001
                pass
            text = ''
            if store_of(hdr.get('From', '')) in stores:
                typ, md = m.uid('FETCH', uid, '(BODY.PEEK[])')
                if typ != 'OK' or not md or not md[0]:
                    continue
                text = mail_text(email.message_from_bytes(md[0][1]))
            r = self.take_cancel(mid, hdr.get('From', ''), subj, sent, text)
            counts[r['outcome']] = counts.get(r['outcome'], 0) + 1
        return counts

    def read_cancellations(self, days: int = 400, progress=None) -> dict:
        """One-off or catch-up read of cancellation emails (the regular run reads them since its cursor)."""
        import imaplib
        cur = self.connections.get('gmail')
        if not cur.get('email') or not cur.get('app_password'):
            raise StoreMailError('Gmail is not connected')
        m = imaplib.IMAP4_SSL('imap.gmail.com', timeout=30)
        try:
            m.login(cur['email'], cur['app_password'])
            m.select('"[Gmail]/All Mail"', readonly=True)
            return self._cancel_pass(m, int(days), progress)
        finally:
            try:
                m.logout()
            except Exception:  # noqa: BLE001
                pass

    def _ship_pass(self, m, days: int, progress=None) -> dict:
        """Shipping emails (ship_mail.py): tracking numbers onto known orders of stores we buy from."""
        from ship_mail import ShipMail
        stores = {r['source'] for r in self.wms._q("SELECT DISTINCT source FROM purchase_order WHERE account = 'gmail'")}
        return ShipMail(self.wms).ship_pass(m, int(days), stores, progress)

    def read_shipping(self, days: int = 400, progress=None) -> dict:
        """One-off or catch-up read of shipping emails (the regular run reads them since its cursor)."""
        import imaplib
        cur = self.connections.get('gmail')
        if not cur.get('email') or not cur.get('app_password'):
            raise StoreMailError('Gmail is not connected')
        m = imaplib.IMAP4_SSL('imap.gmail.com', timeout=30)
        try:
            m.login(cur['email'], cur['app_password'])
            m.select('"[Gmail]/All Mail"', readonly=True)
            return self._ship_pass(m, int(days), progress)
        finally:
            try:
                m.logout()
            except Exception:  # noqa: BLE001
                pass

    def approve(self, po_id: int, prices: dict = None):
        """A person checked the order: it joins Arriving. prices = {line_id: unit_cost_cents} fills gaps."""
        with self.wms._tx() as c:
            row = c.execute("SELECT total_cents FROM purchase_order WHERE id = ? AND account = 'gmail'", (po_id,)).fetchone()
            if not row:
                raise StoreMailError('No such store order')
            for lid, cents in (prices or {}).items():
                if int(cents) < 0:
                    raise StoreMailError('A price cannot be negative')
                c.execute('UPDATE purchase_line SET unit_cost_cents = ? WHERE id = ? AND po_id = ?', (int(cents), int(lid), po_id))
            cost = c.execute('SELECT COALESCE(SUM(qty * unit_cost_cents), 0) FROM purchase_line WHERE po_id = ? AND cancelled = 0',
                             (po_id,)).fetchone()[0]
            if row[0] and cost > row[0] * 1.1 + 100:
                # Costs that can't fit the order would inflate stock value and every margin after it.
                raise StoreMailError(f'The items add up to ${cost / 100:,.2f} but the order total is ${row[0] / 100:,.2f}. '
                                     'Fix the price each (it is per unit, not per line), then Approve.')
            c.execute("UPDATE purchase_order SET review = '' WHERE id = ?", (po_id,))
            c.execute("""INSERT INTO purchase_event (po_id, at, state, status_text, expected_from, expected_to)
                         VALUES (?,?,'ordered','Checked by a person',NULL,NULL)""", (po_id, now_utc()))

    # ---- Gmail run
    def run(self, progress=None) -> dict:
        import email
        import email.header
        import email.utils
        import imaplib
        cur = self.connections.get('gmail')
        if not cur.get('email') or not cur.get('app_password'):
            raise StoreMailError('Gmail is not connected')
        card = re.sub(r'\D', '', str(cur.get('card_last4') or ''))[-4:]
        st = self.wms._q1("SELECT cursor FROM sync_state WHERE channel = 'gmail'") or {}
        days = FIRST_DAYS
        if st.get('cursor'):
            age = (time.time() - time.mktime(time.strptime(st['cursor'][:10], '%Y-%m-%d'))) / 86400
            days = max(2, int(age) + 2)
        started = now_utc()
        counts = {'emails': 0, 'order': 0, 'other_card': 0, 'not_order': 0, 'no_money': 0, 'skipped_store': 0, 'seen': 0, 'error': 0}
        err = None
        jev = self.jev_factory()
        m = imaplib.IMAP4_SSL('imap.gmail.com', timeout=30)
        try:
            m.login(cur['email'], cur['app_password'])
            m.select('"[Gmail]/All Mail"', readonly=True)
            typ, d = m.uid('SEARCH', 'X-GM-RAW', f'"{ORDER_QUERY} newer_than:{days}d"')
            uids = d[0].split() if typ == 'OK' and d and d[0] else []
            for n, uid in enumerate(uids, 1):
                if progress:
                    progress(f'{n} of {len(uids)} emails')
                typ, hd = m.uid('FETCH', uid, '(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT DATE)])')
                hdr = email.message_from_bytes(hd[0][1]) if typ == 'OK' and hd and hd[0] else None
                mid = (hdr and hdr.get('Message-ID') or f'uid-{uid.decode()}').strip()
                if self.wms._q1('SELECT 1 AS x FROM store_mail WHERE message_id = ?', (mid,)):
                    counts['seen'] += 1
                    continue
                typ, md = m.uid('FETCH', uid, '(BODY.PEEK[])')
                if typ != 'OK' or not md or not md[0]:
                    continue
                msg = email.message_from_bytes(md[0][1])
                sent = None
                try:
                    sent = email.utils.parsedate_to_datetime(msg.get('Date')).astimezone(
                        __import__('datetime').timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                except Exception:  # noqa: BLE001
                    pass
                subj = str(email.header.make_header(email.header.decode_header(msg.get('Subject', ''))))
                r = self.take(mid, msg.get('From', ''), subj, sent, mail_text(msg), jev, card, mail_html(msg))
                counts['emails'] += 1
                counts[r['outcome']] = counts.get(r['outcome'], 0) + 1
            # After the orders, so a cancellation read in the same run finds its order.
            cancels = self._cancel_pass(m, days, progress)
            counts['cancellations'] = cancels
            counts['cancelled'] = sum(cancels.get(k, 0) for k in ('cancelled', 'partial'))
            ships = self._ship_pass(m, days, progress)   # after the orders too: a shipping email needs its order
            counts['shipping'] = ships
            counts['tracking'] = ships.get('tracking', 0)
        except (imaplib.IMAP4.error, OSError) as e:
            err = f'Gmail: {e}'
        finally:
            try:
                m.logout()
            except Exception:  # noqa: BLE001
                pass
        counts['typesafe_requests'] = jev.usage['requests']
        with self.wms._tx() as c:
            c.execute("""INSERT INTO sync_state (channel, cursor, last_run_at, last_ok_at, last_error, last_result)
                         VALUES ('gmail',?,?,?,?,?)
                         ON CONFLICT(channel) DO UPDATE SET last_run_at = excluded.last_run_at,
                             cursor = CASE WHEN excluded.last_error IS NULL THEN excluded.cursor ELSE sync_state.cursor END,
                             last_ok_at = COALESCE(excluded.last_ok_at, sync_state.last_ok_at),
                             last_error = excluded.last_error, last_result = excluded.last_result""",
                      (started if err is None else None, started, started if err is None else None, err, json.dumps(counts)))
        if err:
            raise StoreMailError(err)
        return counts


# ------------------------------------------------------------------ Walmart order pages (extension 1.5.0+)

WALMART = 'walmart.com'
FINAL_STATES = ('delivered', 'cancelled', 'returned')


def walmart_status(text: str, seen) -> dict:
    """Walmart's group status ('Delivered Mon, Apr 27, 5:24pm', 'Arrives Wed, Nov 4', 'Canceled') -> state + dates."""
    from inbound import _one_date
    t = (text or '').strip()
    low = t.lower()
    when = None
    m = re.search(r'(?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s+)?((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:,?\s+\d{4})?|today|tomorrow|yesterday)', low)
    if 'cancel' in low:
        return {'state': 'cancelled', 'from': None, 'to': None, 'delivered_on': None}
    if 'return' in low or 'refund' in low:
        return {'state': 'returned', 'from': None, 'to': None, 'delivered_on': None}
    if ('deliver' in low and 'out for' not in low and 'arriv' not in low) or 'picked up' in low:
        if m:
            when = _one_date(m.group(1), seen, past=True)
        return {'state': 'delivered', 'from': None, 'to': None, 'delivered_on': (when or seen).isoformat()}
    if m:
        when = _one_date(m.group(1), seen, past=False)
    if 'out for delivery' in low:
        when = seen
    state = 'shipped' if ('ship' in low or 'out for delivery' in low or 'on the way' in low or 'in transit' in low) else 'ordered'
    d = when.isoformat() if when else None
    return {'state': state, 'from': d, 'to': d, 'delivered_on': None}


def _walmart_order_state(groups: list, seen) -> tuple:
    """Several shipment groups -> one order state: the least advanced live group wins."""
    rank = {'ordered': 0, 'shipped': 1, 'delivered': 2, 'returned': 3, 'cancelled': 4}
    sts = [(walmart_status(g, seen), g) for g in groups if g]
    live = [x for x in sts if x[0]['state'] not in ('cancelled', 'returned')] or sts
    if not live:
        return {'state': 'ordered', 'from': None, 'to': None, 'delivered_on': None}, ''
    st, text = min(live, key=lambda x: (rank[x[0]['state']], x[0]['to'] or '9999'))
    if st['state'] == 'delivered':
        st = dict(st, delivered_on=max(x[0]['delivered_on'] for x in live if x[0]['delivered_on']))
    return st, text


def _walmart_wanted(self, limit: int = 300) -> dict:
    """Walmart orders the extension should read: never read, or not finished yet."""
    rows = self.wms._q(f"""SELECT external_id, page_read_at, state FROM purchase_order
                           WHERE source = '{WALMART}' AND account = 'gmail' AND hidden = 0
                             AND (page_read_at IS NULL OR state NOT IN {FINAL_STATES})
                           ORDER BY page_read_at IS NOT NULL, ordered_at DESC LIMIT ?""", (int(limit),))
    return {'orders': [r['external_id'] for r in rows]}


def _rekey_renamed(c, po: int, grouped: list):
    """The email and walmart.com name one item differently ("...Globe with Accessories Baby and Toddler Toys" vs
    "...Globe Child 5-9 Years..."). Lines match by title, so the page read used to add a second line of the same
    units next to the email's (Alex 11-11: LeapFrog globe twice on the audit, 20 + 20). A page item no line matches
    takes over the email line the page doesn't name: the best title match (half the words or more), or the only
    one left on each side when the quantities agree. That line is then updated in place, never doubled."""
    from inbound import Inbound
    from listings import title_score
    lines = c.execute('SELECT id, line_key, title, qty FROM purchase_line WHERE po_id = ? AND cancelled = 0', (po,)).fetchall()
    have = {r[1] for r in lines}
    page = {}
    for g in grouped:
        k = Inbound._line_key(g)
        if k:
            p = page.setdefault(k, {'title': g['title'], 'qty': 0})
            p['qty'] += g['qty']
    new = [(k, p) for k, p in page.items() if k not in have]
    left = [r for r in lines if r[1] not in page]
    for k, p in new:
        if not left:
            break
        best = max(left, key=lambda r: title_score(p['title'], r[2]))
        if title_score(p['title'], best[2]) < 0.5:
            if not (len(new) == 1 and len(left) == 1 and left[0][3] == p['qty']):
                continue
            best = left[0]
        c.execute('UPDATE purchase_line SET line_key = ? WHERE id = ?', (k, best[0]))
        left.remove(best)


def _walmart_pages(self, orders: list) -> dict:
    """Walmart order pages read by the extension -> item prices, item ids, status, card check, tracking numbers."""
    from inbound import Inbound, today_et
    from labels import Labels
    if not isinstance(orders, list) or len(orders) > 300:
        raise StoreMailError('Send a list of up to 300 orders')
    card = re.sub(r'\D', '', str(self.connections.get('gmail').get('card_last4') or ''))[-4:]
    seen = today_et()   # sweep C2: date.today() is UTC on the server, so "Arrives tomorrow" after 7 PM ET was a day late
    counts = {'orders': len(orders), 'updated': 0, 'unknown': 0, 'no_items': 0}
    with self.wms._tx() as c:
        for o in orders:
            oid = str(o.get('order_id') or '').strip()
            row = c.execute("SELECT id, review, state FROM purchase_order WHERE source = ? AND external_id = ?", (WALMART, oid)).fetchone()
            if not row:
                counts['unknown'] += 1
                continue
            po, review, old_state = row
            items = [i for i in (o.get('items') or []) if isinstance(i, dict)]
            if not items:   # nothing read (a page change, not an empty order): change nothing, cancel nothing
                counts['no_items'] += 1
                continue
            live = [i for i in items if 'cancel' not in str(i.get('status') or '').lower()]
            grouped = []
            for i in live:
                qty = max(1, int(i.get('qty') or 1))
                line = i.get('line_cents')
                grouped.append({'title': str(i.get('name') or '')[:300], 'qty': qty, 'image': i.get('image'),
                                'price_cents': round(int(line) / qty) if line is not None else 0, 'item_id': str(i.get('item_id') or '')[:40]})
            sub, total = o.get('subtotal_cents'), o.get('total_cents')
            _rekey_renamed(c, po, grouped)
            keys = Inbound._upsert_lines(c, po, grouped, sub, total) if grouped else {}
            for g in grouped:
                k = Inbound._line_key(g)
                if k in keys and g['item_id']:
                    c.execute('UPDATE purchase_line SET store_item_id = ? WHERE id = ?', (g['item_id'], keys[k]))
            # Each shipment group's tracking number, per line (labels.py: a scan of the box's label shows these).
            Labels.save_store_packages(c, po, [
                {'line_id': keys.get(Inbound._line_key({'title': str(i.get('name') or '')[:300]})),
                 'tracking_id': i.get('tracking'), 'qty': i.get('qty')} for i in live if i.get('tracking')])
            # Lines from the email that the page doesn't list as live (cancelled, or named differently): off,
            # unless something was already received against them.
            for lid, in c.execute('SELECT id FROM purchase_line WHERE po_id = ? AND cancelled = 0', (po,)).fetchall():
                if lid in keys.values():
                    continue
                got = c.execute("""SELECT COALESCE(SUM(r.qty),0) FROM receipt r JOIN action a ON a.id = r.action_id
                                   WHERE r.purchase_line_id = ? AND a.reversed_by IS NULL""", (lid,)).fetchone()[0]
                if not got:
                    c.execute('UPDATE purchase_line SET cancelled = 1 WHERE id = ?', (lid,))
            st, text = _walmart_order_state([str(i.get('status') or '') for i in items], seen)
            cards = [x for x in (o.get('cards') or []) if re.fullmatch(r'\d{4}', str(x))]
            new_review = review
            if review:  # still waiting for a person: say what the page added
                notes = ['prices from the walmart.com order page']
                if card and cards and card not in cards:
                    notes.append(f"walmart.com says it was paid with the card ending {', '.join(cards)}")
                new_review = '; '.join(notes)
            changed = st['state'] != old_state
            c.execute("""UPDATE purchase_order SET state = ?, status_text = ?, expected_from = ?, expected_to = ?, delivered_on = ?,
                             total_cents = COALESCE(?, total_cents), review = ?, page_read_at = ?, last_seen_at = ?
                             {} WHERE id = ?""".format(', status_changed_at = ?' if changed else ''),
                      (st['state'], (text or st['state'])[:200], st['from'], st['to'], st['delivered_on'], total, new_review,
                       now_utc(), now_utc(), *((now_utc(),) if changed else ()), po))
            if changed:
                c.execute("""INSERT INTO purchase_event (po_id, at, state, status_text, expected_from, expected_to)
                             VALUES (?,?,?,?,?,?)""", (po, now_utc(), st['state'], (text or '')[:200], st['from'], st['to']))
            counts['updated'] += 1
    return counts


StoreOrders.walmart_wanted = _walmart_wanted
StoreOrders.walmart_pages = _walmart_pages
