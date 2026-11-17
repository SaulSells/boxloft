"""Order-page parsers for extension 2.0. The extension only fetches pages; this file reads them.

parse_amazon_detail and parse_amazon_list give the same output as extension 1.5.0's parseDetail
and list-page loop in extension/sync.js (checked on real saved pages, see scripts/eval_pages.py).
One addition: each detail item also carries `package`, the number of the shipment block it sits
under. Amazon splits an order into packages, often several of one ASIN in different states, and
inbound.py keeps each package's units and state apart.
parse_walmart_order is 1.5.0's parseWalmart.

Amazon pages are read with a small DOM built on the stdlib HTMLParser, enough for the few
selectors the parser needs. No third-party HTML library.
"""
import math
import re
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlsplit

VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param',
        'source', 'track', 'wbr', 'keygen'}
# Start tags that close an open <p>, as in the HTML parsing rules.
CLOSES_P = {'address', 'article', 'aside', 'blockquote', 'details', 'div', 'dl', 'fieldset',
            'figcaption', 'figure', 'footer', 'form', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'header',
            'hgroup', 'hr', 'main', 'menu', 'nav', 'ol', 'p', 'pre', 'section', 'table', 'ul'}
P_SCOPE = {'applet', 'caption', 'html', 'table', 'td', 'th', 'marquee', 'object', 'template', 'button'}
SPECIAL = (CLOSES_P - {'address', 'div', 'p'}) | P_SCOPE | {'body', 'tr', 'tbody', 'thead', 'li', 'dd', 'dt',
                                                              'select', 'iframe', 'center', 'dir', 'summary'}
ID_RE = re.compile(r'\b(\d{3}-\d{7}-\d{7})\b')


class Node:
    __slots__ = ('tag', 'attrs', 'children', 'parent', 'pos', 'end')

    def __init__(self, tag, attrs, parent, pos):
        self.tag, self.attrs, self.parent, self.pos = tag, attrs, parent, pos
        self.children = []
        self.end = pos

    def text(self) -> str:
        """textContent, whitespace squeezed and trimmed like the extension's text()."""
        out = []
        stack = [self]
        while stack:
            n = stack.pop()
            if isinstance(n, str):
                out.append(n)
            else:
                stack.extend(reversed(n.children))
        return re.sub(r'\s+', ' ', ''.join(out)).strip()

    def iter(self):
        """Element descendants in document order (not self)."""
        stack = list(reversed([c for c in self.children if isinstance(c, Node)]))
        while stack:
            n = stack.pop()
            yield n
            stack.extend(reversed([c for c in n.children if isinstance(c, Node)]))

    def contains(self, other) -> bool:
        return self.pos < other.pos <= self.end

    def has_class(self, cls) -> bool:
        return cls in (self.attrs.get('class') or '').split()

    def first(self, pred, within=None):
        for n in self.iter():
            if pred(n) and (within is None or _has_ancestor(n, within)):
                return n
        return None


def _has_ancestor(n, pred) -> bool:
    p = n.parent
    while p is not None:
        if p.tag != '#document' and pred(p):
            return True
        p = p.parent
    return False


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.count = 0
        self.root = Node('#document', {}, None, 0)
        self.stack = [self.root]

    def _close_to(self, i):
        for n in self.stack[i:]:
            n.end = self.count
        del self.stack[i:]

    def handle_starttag(self, tag, attrs):
        if tag in CLOSES_P:
            # Close an open <p> unless a scope boundary (table cell, button...) sits above it.
            for i in range(len(self.stack) - 1, 0, -1):
                t = self.stack[i].tag
                if t == 'p':
                    self._close_to(i)
                    break
                if t in P_SCOPE:
                    break
        if tag == 'li':
            # A new <li> closes the open one, looking past div/p/address and inline elements.
            for i in range(len(self.stack) - 1, 0, -1):
                t = self.stack[i].tag
                if t == 'li':
                    self._close_to(i)
                    break
                if t in SPECIAL:
                    break
        self.count += 1
        d = {}
        for k, v in attrs:
            d.setdefault(k, '' if v is None else v)
        n = Node(tag, d, self.stack[-1], self.count)
        self.stack[-1].children.append(n)
        if tag not in VOID:
            self.stack.append(n)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and self.stack[-1].tag == tag:
            self._close_to(len(self.stack) - 1)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                self._close_to(i)
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def dom(html: str) -> Node:
    b = _Builder()
    b.feed(html)
    b.close()
    b._close_to(1)
    b.root.end = b.count
    return b.root


def js_round(x: float) -> int:
    """Math.round: halves go up."""
    return math.floor(x + 0.5)


def cents(s):
    """1.5.0's cents(): the first $ amount, read the way parseFloat reads it."""
    m = re.search(r'\$\s*([\d.]+)', (s or '').replace(',', ''))
    if not m:
        return None
    f = re.match(r'\d+(?:\.\d*)?|\.\d+', m.group(1))
    return js_round(float(f.group(0)) * 100) if f else None


def _dc(name):
    return lambda n: n.attrs.get('data-component') == name


def parse_amazon_list(html: str) -> dict:
    """Order IDs and order-card count on a Your Orders list page (1.5.0's list loop)."""
    # Amazon's session id (ue_sid = '135-...') has the same shape as an order ID. 1.5.0 read it as
    # an order on every run (a blank detail page each time); 2.0 skips it.
    sid = set(re.findall(r"ue_sid\s*=\s*'(\d{3}-\d{7}-\d{7})'", html))
    ids = []
    for m in ID_RE.finditer(html):
        if m.group(1) in sid:
            continue
        if m.group(1) not in ids and not m.group(1).startswith('000-'):
            ids.append(m.group(1))
    return {'ids': ids, 'cards': len(re.findall(r'class="order-card', html))}


def parse_amazon_detail(order_id: str, html: str, seen_at: str) -> dict:
    """An order-details page -> the order dict extension 1.x posted to /api/inbound/sync."""
    d = dom(html)
    status_els = [n for n in d.iter() if n.attrs.get('data-component') == 'shipmentStatus']
    statuses = [t for t in (n.text() for n in status_els) if t]
    allp = [n for n in d.iter() if n.attrs.get('data-component') == 'purchasedItems']
    top = [e for e in allp if not any(o is not e and o.contains(e) for o in allp)]

    def package_of(pi):
        """(package number, its status text): the shipment block the item sits under, 1 = top. 0 = none."""
        before = [i for i, s in enumerate(status_els) if pi.pos > s.pos]
        return (before[-1] + 1, status_els[before[-1]].text()) if before else (0, '')

    # Each shipment block has 0 or 1 "Track package" link. Its kind says whether Amazon shipped it:
    # shipmentId = a real package (tracking ID on its page), itemId only = not shipped yet, whatever the
    # status line says ("Arriving November 30 - December 8" shows on unshipped orders). Measured 11-11 on
    # 86 real order pages and 20 Track pages (amazon-track.json).
    tracks = {}
    for n in d.iter():
        href = (n.attrs.get('href') or '') if n.tag == 'a' else ''
        if '/progress-tracker/package?' not in href:
            continue
        before = [i for i, s in enumerate(status_els) if n.pos > s.pos]
        if before and before[-1] + 1 not in tracks:
            tracks[before[-1] + 1] = track_link(href)

    items = []
    for pi in top:
        t = pi.first(_dc('itemTitle'))
        a = (t.first(lambda n: n.tag == 'a') if t else None) or \
            pi.first(lambda n: n.tag == 'a' and '/dp/' in (n.attrs.get('href') or ''))
        asin = None
        if a:
            m = re.search(r'/(?:dp|product)/([A-Z0-9]{10})', a.attrs.get('href') or '')
            asin = m.group(1) if m else None
        img_box = pi.first(_dc('itemImage'))
        img = img_box.first(lambda n: n.tag == 'img') if img_box else None
        badge = None
        if img_box:
            bt = img_box.text()
            badge = bt if re.fullmatch(r'\d+', bt) else None
        offscreen = lambda n: n.has_class('a-offscreen')  # noqa: E731
        price = pi.first(offscreen, within=_dc('unitPrice')) or \
            pi.first(offscreen, within=lambda n: n.has_class('a-price'))
        image = None
        if img:
            image = re.sub(r'^//', 'https://', img.attrs.get('data-a-hires') or img.attrs.get('src') or '')
        title = (t.text() if t else '') or ((img.attrs.get('alt') or '') if img else '')
        package, status = package_of(pi)
        it = {'asin': asin, 'title': title, 'qty': int(badge) if badge else 1,
              'price_cents': cents(price.text() if price else ''), 'image': image, 'status': status,
              'package': package}
        if tracks.get(package):
            it['track'] = tracks[package]
        if it['asin'] or it['title']:
            items.append(it)

    summary_el = d.first(_dc('chargeSummary'))
    summary = summary_el.text() if summary_el else ''
    od = d.first(_dc('orderDate'))
    status_text = next((s for s in statuses if not re.match(r'delivered', s, re.I)), None) or \
        (statuses[0] if statuses else None) or \
        ('Cancelled' if re.search('cancel', html[:200000], re.I) and re.search('order was cancel', html, re.I) else '')
    sub = re.search(r'Subtotal:\s*\$[\d,.]+', summary)
    tot = re.search(r'Grand Total:\s*\$[\d,.]+', summary)
    return {
        'order_id': order_id, 'from_detail': True, 'seen_at': seen_at,
        'ordered_at': od.text() if od else '',
        'status_text': status_text,
        'subtotal_cents': cents(sub.group(0) if sub else None),
        'total_cents': cents(tot.group(0) if tot else None),
        'items': items,
    }


SHIPMENT_RE = re.compile(r'[A-Za-z0-9]{4,24}')
PHOTO_RE = re.compile(r'^https://us-prod-temp\.s3\.amazonaws\.com/(imageId-[A-Za-z0-9-]{8,80})\?[^\s"<>]+$')


def track_link(href: str) -> dict:
    """A Track package link -> {'shipment_id', 'package_index'} (shipped) or {'item_id'} (not shipped yet)."""
    q = dict(parse_qsl(urlsplit(href).query))
    sid, idx = q.get('shipmentId') or '', q.get('packageIndex') or '0'
    if SHIPMENT_RE.fullmatch(sid) and idx.isdigit() and len(idx) <= 2:
        return {'shipment_id': sid, 'package_index': int(idx)}
    if q.get('itemId'):
        return {'item_id': q['itemId'][:40]}
    return {}


def _cls(name):
    return lambda n: n.has_class(name)


def parse_amazon_track(html: str) -> dict:
    """A Track package page (/progress-tracker/package) -> what Arriving shows for that package.

    Two layouts seen 11-11: the usual one (headline h1.pt-promise-main-slot, delivery card, milestones) and
    a carrier-delay one ("Shipped with UPS" header, an apology line with the new date). Both carry the
    "See all updates" block (tracking-events-container): carrier, tracking ID and dated events with places,
    so carrier and tracking ID come from there first."""
    d = dom(html)
    ev = d.first(lambda n: n.attrs.get('id') == 'tracking-events-container')
    scope = ev or d

    def text_of(pred, within=None):
        n = (within or d).first(pred)
        return n.text() if n else ''

    carrier = text_of(_cls('tracking-event-carrier-header'), ev) if ev else ''
    if not carrier:
        card = d.first(_cls('pt-delivery-card-wrapper'))
        carrier = text_of(lambda n: n.tag == 'h3', card) if card else ''
    tid_text = text_of(_cls('tracking-event-trackingId-text'), scope) or text_of(_cls('pt-delivery-card-trackingId')) \
        or text_of(_cls('carrierRelatedInfo-trackingId-text'))
    m = re.search(r'Tracking ID:?\s*([A-Za-z0-9]{6,40})', tid_text)
    headline = text_of(_cls('pt-promise-main-slot'))
    if not headline:
        # Carrier-delay layout: "We're sorry for the delay. ... delivered on or before November 15."
        lex = d.first(_cls('lexicalExceptionMessage-container'))
        lt = lex.text() if lex else ''
        mm = re.search(r'(?:on or before|by)\s+([A-Z][a-z]+ \d{1,2})', lt)
        headline = f'Now expected by {mm.group(1)} (running late)' if mm else lt
    note = text_of(_cls('pt-promise-details-slot'))
    stages = []
    for n in d.iter():
        if n.has_class('pt-status-milestone'):
            lab = n.first(_cls('pt-status-milestone-label'))
            if lab:
                stages.append({'name': lab.text(), 'done': n.attrs.get('data-reached') == 'true'})
    last = None
    if ev:
        day = ev.first(_cls('tracking-event-date'))
        msg = ev.first(_cls('tracking-event-message'))
        if msg:
            tm = ev.first(_cls('tracking-event-time'))
            where = ev.first(_cls('tracking-event-location'))
            last = {'date': day.text() if day else '', 'time': tm.text() if tm else '', 'text': msg.text(),
                    'where': where.text() if where else ''}
    photo = None
    for n in d.iter():
        if n.tag == 'img' and (n.has_class('photo-on-delivery-img') or n.has_class('photo-on-delivery-img-thumb')):
            u = n.attrs.get('data-src') or n.attrs.get('src') or ''
            if PHOTO_RE.match(u):
                photo = u
                if n.has_class('photo-on-delivery-img'):
                    break
    carrier = re.sub(r'^(Shipped with|Delivery facilitated by|Delivered by)\s+', '', carrier, flags=re.I)
    return {'headline': headline[:200], 'note': note[:200], 'carrier': carrier[:60],
            'tracking_id': m.group(1) if m else None, 'stages': stages, 'last_event': last, 'photo_url': photo}


def parse_walmart_order(o: dict) -> dict:
    """__NEXT_DATA__ order object from walmart.com/orders/<id> -> what 1.5.0 posted to /api/inbound/walmart."""
    def c(v):
        return js_round(v * 100) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    def g_(d, *ks):
        for k in ks:
            d = d.get(k) if isinstance(d, dict) else None
        return d if d is not None else {}

    def status(g):
        parts = g_(g, 'status', 'message', 'parts') or []
        s = ''.join(str(p.get('text') or '') for p in parts if isinstance(p, dict))
        return s or g_(g, 'status').get('statusType') or g.get('fulfillmentText') or ''

    items = []
    for g in (o.get('groups_2101') or o.get('groups') or []):
        for i in (g.get('items') or []):
            pi = i.get('productInfo') or {}
            items.append({
                'name': pi.get('name') or '',
                'item_id': pi.get('usItemId') or '',
                'qty': i.get('quantity') or 1,
                'line_cents': c(g_(i, 'priceInfo', 'linePrice').get('value')),
                'image': (pi.get('imageInfo') or {}).get('thumbnailUrl') or None,
                'status': status(g),
                # One group = one package (Alex 11-16: scan the carrier label, see what is inside).
                'tracking': str(g_(g, 'shipment').get('trackingNumber') or '')[:60],
            })
    cards = []
    for p in (o.get('paymentMethods') or []):
        m = re.search(r'(\d{4})\s*$', p.get('description') or '')
        if m:
            cards.append(m.group(1))
    pd = o.get('priceDetails') or {}
    return {'order_id': o.get('displayId'), 'ordered_at': (o.get('orderDate') or '')[:10], 'items': items,
            'subtotal_cents': c(g_(pd, 'subTotal').get('value')), 'total_cents': c(g_(pd, 'grandTotal').get('value')),
            'cards': cards}
