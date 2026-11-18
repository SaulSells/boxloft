"""Photo -> product (Alex 11-11 "You can use Gemini ... the cheapest one that'll work").

Sam photographs an item on New product. One photo does three jobs:
1. Gemini reads the box: name, brand, model, barcode digits (structured JSON, temperature 0).
2. The read is matched against what we bought (products, open audit rows, purchase lines not received), so she can
   tap the buy it came from, with its picture, instead of making a duplicate.
3. The photo itself is saved and becomes the product's photo when she taps Create product.

Model (measured 11-11 on Alex's photos):
gemini-3.8-flash with thinkingLevel low named the NECA Trick 'r Treat figure (4.7-5.3 s) and the NECA Predator box
(9.1 s) right and said "no product" for a phone screen. The flash-lite models named the film, not the figure.

The call runs outside wms.op_lock (server.UNLOCKED): it waits on the network for seconds. Every read is logged in
photo_read with what the product was finally saved as, so how often Gemini was right can be counted later."""
import base64
import json
import math
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from wms import LINE_LEFT_SQL, Conflict, WmsError, now_utc

API = 'https://generativelanguage.googleapis.com/v1beta/models'
MODEL = os.environ.get('GEMINI_MODEL', 'gemini-3.8-flash')
TIMEOUT = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS photo_read (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    photo TEXT NOT NULL,                    -- path under DATA/photos (photo-id/YYYY-MM/<hex>.jpg)
    model TEXT NOT NULL DEFAULT '',
    seconds REAL,
    tokens_in INTEGER, tokens_out INTEGER,
    is_product INTEGER,
    title TEXT NOT NULL DEFAULT '',
    brand TEXT NOT NULL DEFAULT '',
    model_no TEXT NOT NULL DEFAULT '',
    barcode TEXT NOT NULL DEFAULT '',
    error TEXT,
    matches TEXT NOT NULL DEFAULT '[]',    -- JSON: the buys shown, best first
    product_id INTEGER,                     -- set when the photo is saved on a product
    saved_title TEXT                        -- what the product was created as (compare with title)
);
"""

PROMPT = ('A warehouse worker photographed one retail product to add it to inventory. Read the packaging. '
          'title: the product name the way a seller would list it on Amazon (brand, line, character or model, size or count). '
          'brand: the maker. model: model or part number if printed, else "". barcode_digits: the UPC/EAN digits if readable, else "". '
          'Use the printed text, logos and package design: if you recognize the maker or product line from its logo or '
          'packaging style, name it. Never invent a model number or barcode. '
          'Ignore phones, screens, hands and background. is_product false if no retail product is visible.')
RESPONSE = {'type': 'object', 'properties': {
    'is_product': {'type': 'boolean'}, 'title': {'type': 'string'}, 'brand': {'type': 'string'},
    'model': {'type': 'string'}, 'barcode_digits': {'type': 'string'},
    'confidence': {'type': 'string', 'enum': ['high', 'medium', 'low']}},
    'required': ['is_product', 'title', 'brand', 'model', 'barcode_digits', 'confidence']}

TOKEN_RE = re.compile(r'^(\d{6})([0-9a-f]{24})$')
STOP = set('the a an and or of for with to in on by from new set pack pcs piece pieces count ct oz inch in x'.split())
MATCH_MIN = 0.6   # share of the read's word weight a buy title must hold to be shown
MAX_MATCHES = 5
RARE_SHARE = 0.003   # a word on at most 0.3% of buy titles (3 of 981 live, at least 1) is an anchor ("apocalypse", "darth")


class PhotoIdError(Exception):
    pass


def gtin_ok(digits: str) -> bool:
    """A UPC-A, EAN-8/13 or GTIN-14 whose check digit adds up. A misread digit fails, so no lookup goes out for it."""
    if not re.fullmatch(r'\d{8}|\d{12,14}', digits or ''):
        return False
    body, check = digits[:-1], int(digits[-1])
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def words(text: str) -> list:
    return [w for w in re.findall(r'[a-z0-9]+', (text or '').lower().replace("'", '')) if w not in STOP and len(w) > 1]


def rank(read: str, cands: list, limit: int = MAX_MATCHES) -> list:
    """Buys whose titles share the read's rare words. TF-IDF style: each word weighs log(N / titles holding it), so
    "predator" or "trick" counts and "neca", "figure", "ultimate" barely do. A word no buy has (Gemini read "Sam",
    no buy says Sam) still counts in the read's total, so a title that matches only the common words scores low."""
    q = set(words(read))
    if not q or not cands:
        return []
    # A product also goes by the buy titles of its ASIN (alts): Sam may have saved it as a short name.
    docs = [[set(words(t)) for t in [c['title'], *c.get('alts', [])]] for c in cands]
    n = len(docs)
    df = {}
    for d in docs:
        for w in set().union(*d):
            df[w] = df.get(w, 0) + 1
    idf = {w: math.log((n + 1) / (df.get(w, 0) + 0.5)) for w in q}
    total = sum(idf.values()) or 1.0
    rare_max = max(1, round(n * RARE_SHARE))
    out = []
    for c, d in zip(cands, docs):
        best = max(d, key=lambda t: sum(idf[w] for w in q & t))
        score = sum(idf[w] for w in q & best) / total
        # Alex 11-11 (Dreamcatcher album): Keepa's long title ("... Monochrome Postcard, Agent Card + Poster") holds
        # many words no buy has, so its share stays low; 2 shared words that only 1-2 buys have still pin it down.
        anchors = sum(1 for w in q & best if df.get(w, 0) <= rare_max)
        if score >= MATCH_MIN or anchors >= 2:
            out.append({**{k: v for k, v in c.items() if k != 'alts'}, 'score': round(score, 3)})
    kind_order = {'product': 0, 'audit': 1, 'line': 2}
    out.sort(key=lambda c: (-c['score'], kind_order[c['kind']], -(c.get('left') or 0)))
    return out[:limit]


def gemini_read(jpeg: bytes, key: str, model: str = MODEL) -> dict:
    """One generateContent call. Returns {is_product, title, brand, model, barcode_digits, confidence, seconds, tokens}."""
    body = {'contents': [{'parts': [{'inline_data': {'mime_type': 'image/jpeg', 'data': base64.b64encode(jpeg).decode()}},
                                    {'text': PROMPT}]}],
            'generationConfig': {'responseMimeType': 'application/json', 'responseSchema': RESPONSE, 'temperature': 0,
                                 'thinkingConfig': {'thinkingLevel': 'low'}}}
    req = urllib.request.Request(f'{API}/{model}:generateContent', data=json.dumps(body).encode(), method='POST',
                                 headers={'x-goog-api-key': key, 'Content-Type': 'application/json'})
    t0 = time.time()
    for attempt in (1, 2):
        try:
            out = json.load(urllib.request.urlopen(req, timeout=TIMEOUT))
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503) and attempt == 1:   # busy: one more try
                time.sleep(1.5)
                continue
            raise PhotoIdError(f'The photo reader said no (Gemini {e.code}). Type the name, or try again.')
        except (urllib.error.URLError, TimeoutError, OSError):
            raise PhotoIdError('The photo reader did not answer in time. Type the name, or try again.')
    try:
        data = json.loads(out['candidates'][0]['content']['parts'][-1]['text'])
    except (KeyError, IndexError, ValueError):
        raise PhotoIdError('The photo reader gave no answer. Type the name, or try again.')
    u = out.get('usageMetadata') or {}
    return _shape(data, round(time.time() - t0, 1), u.get('promptTokenCount'),
                  (u.get('candidatesTokenCount') or 0) + (u.get('thoughtsTokenCount') or 0), model)


def _shape(data: dict, seconds, tokens_in, tokens_out, model: str) -> dict:
    return {'is_product': bool(data.get('is_product')), 'title': str(data.get('title') or '').strip()[:300],
            'brand': str(data.get('brand') or '').strip()[:120], 'model': str(data.get('model') or '').strip()[:80],
            'barcode_digits': re.sub(r'\D', '', str(data.get('barcode_digits') or ''))[:14],
            'confidence': data.get('confidence') or '', 'seconds': seconds,
            'tokens_in': tokens_in, 'tokens_out': tokens_out, 'ai_model': model}


def ai_read(jpeg: bytes, key: str) -> dict:
    """The photo reader set up in ai.py: Gemini (as before), or Anthropic / OpenAI."""
    import ai
    prov, _ = ai.photo_provider()
    if prov not in ('anthropic', 'openai'):
        return gemini_read(jpeg, key)
    try:
        r = ai.chat_photo_read(prov, key, jpeg, PROMPT)
    except ai.AiError as e:
        raise PhotoIdError(f'The photo reader said no ({e}). Type the name, or try again.')
    return _shape(r['data'], r['seconds'], r['tokens_in'], r['tokens_out'],
                  ai.anthropic_model() if prov == 'anthropic' else os.environ.get('BOXLOFT_AI_MODEL', 'openai'))


class PhotoId:
    def __init__(self, wms, catalog, photo_dir: str, reader=None):
        self.wms = wms
        self.catalog = catalog
        self.photo_dir = photo_dir
        self.reader = reader or ai_read   # tests pass a fake: (jpeg, key) -> read dict
        wms._conn().executescript(SCHEMA)

    # ------------------------------------------------------------------ the photo

    def save_photo(self, jpeg: bytes) -> tuple:
        """Keep the photo until a product is created. Returns (token, path under the photo folder)."""
        if jpeg[:3] != b'\xff\xd8\xff':
            raise WmsError('That file is not a JPEG photo')
        month = datetime.now(timezone.utc).strftime('%Y%m')
        hexid = secrets.token_hex(12)
        rel = os.path.join('photo-id', f'{month[:4]}-{month[4:]}', f'{hexid}.jpg')
        full = os.path.join(self.photo_dir, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'wb') as f:
            f.write(jpeg)
        return month + hexid, rel

    def path_for(self, token: str):
        m = TOKEN_RE.match(token or '')
        if not m:
            return None
        rel = os.path.join('photo-id', f'{m.group(1)[:4]}-{m.group(1)[4:]}', f'{m.group(2)}.jpg')
        return rel if os.path.isfile(os.path.join(self.photo_dir, rel)) else None

    def attach(self, token: str, product_id: int, user_id: int, title: str = ''):
        """Create product and the photo-id screen: the photo becomes the product's photo (once; a reused link adds
        nothing). A photo an Undo freed goes on this item too (census 11-17)."""
        return self.claim(self.path_for(token), product_id, user_id, title)

    def claim(self, rel: str, product_id: int, user_id: int, title: str = '', add_id: int = None):
        """The one place a photo-id photo goes on an item (census 11-17: the box page and the unlocked photo-id screen
        each checked, then wrote, so an item got 2 photos). Every check and write is one transaction:
        - the photo is live on this item already: a box page line (add_id) holds it too only when box page lines hold
          it (a person attached it on the photo-id screen: an Undo of the line must not take it off);
        - the item has another live photo, or this one is live on another item, or a person removed it: nothing;
        - an Undo or a Change freed it (photo_freed): it moves here;
        - else it is new: a photo row.
        Returns {'id'} of the photo, or None when nothing went on."""
        if not rel or not product_id:
            return None
        with self.wms._tx() as c:
            lines = c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'box_add_photo'").fetchone()
            rows = c.execute('SELECT id, product_id, removed FROM photo WHERE path = ? ORDER BY id DESC', (rel,)).fetchall()
            # Every row of the file counts (census fix check 11-17: an older race could leave 2 rows of one file).
            row = next((r for r in rows if not r[2] and r[1] == product_id), None) \
                or next((r for r in rows if not r[2]), None) or (rows[0] if rows else None)
            if row and not row[2] and row[1] == product_id:
                if add_id and lines and c.execute('SELECT 1 FROM box_add_photo WHERE photo_id = ?', (row[0],)).fetchone():
                    c.execute('INSERT OR REPLACE INTO box_add_photo (add_id, photo_id) VALUES (?, ?)', (add_id, row[0]))
                return {'id': row[0], 'held': bool(add_id and lines and c.execute(
                    'SELECT 1 FROM box_add_photo WHERE add_id = ? AND photo_id = ?', (add_id, row[0])).fetchone())}
            if c.execute('SELECT 1 FROM photo WHERE product_id = ? AND removed = 0', (product_id,)).fetchone():
                return None
            if not c.execute('SELECT 1 FROM product WHERE id = ?', (product_id,)).fetchone():
                raise WmsError('Product not found')
            if row and not row[2]:
                return None   # live on another item: it stays there
            if rows:
                freed = next((r for r in rows if lines and c.execute('SELECT 1 FROM photo_freed WHERE photo_id = ?', (r[0],)).fetchone()), None)
                if not freed:
                    return None   # a person removed it: it stays off
                row = freed
                c.execute('DELETE FROM photo_freed WHERE photo_id = ?', (row[0],))
                c.execute('UPDATE photo SET product_id = ?, removed = 0 WHERE id = ?', (product_id, row[0]))
                c.execute('UPDATE photo_read SET product_id = ?, saved_title = ? WHERE photo = ?', (product_id, title, rel))
                phid = row[0]
            else:
                phid = c.execute("INSERT INTO photo (product_id, unit_id, path, user_id, created_at, source) "
                                 "VALUES (?, NULL, ?, ?, ?, 'photo-id')", (product_id, rel, user_id, now_utc())).lastrowid
                c.execute('UPDATE photo_read SET product_id = ?, saved_title = ? WHERE photo = ? AND product_id IS NULL',
                          (product_id, title, rel))
            c.execute('UPDATE product SET updated_at = ? WHERE id = ?', (now_utc(), product_id))
            if add_id and lines:
                c.execute('INSERT OR REPLACE INTO box_add_photo (add_id, photo_id) VALUES (?, ?)', (add_id, phid))
            return {'id': phid, 'held': bool(add_id and lines)}

    # ------------------------------------------------------------------ what we bought

    def candidates(self) -> list:
        """Everything Sam could be holding that we know of: products (they come first, so no duplicate is made),
        audit rows whose ASIN has no product yet, and purchase lines with units still coming and no product."""
        out, seen = [], set()
        by_asin = {}   # ASIN -> (buy titles, an image): a product is matched on these too
        for r in self.wms._q("""SELECT asin, title, image_url FROM audit_item WHERE asin LIKE 'B0%' AND title != ''
                                UNION ALL SELECT asin, title, image_url FROM purchase_line WHERE asin != '' AND title != ''"""):
            t, img = by_asin.setdefault(r['asin'].upper(), (set(), [None]))
            t.add(r['title'])
            img[0] = img[0] or r['image_url']
        for p in self.wms._q("""
                SELECT p.id, p.title, (SELECT i.value FROM product_identifier i WHERE i.product_id = p.id AND i.kind = 'ASIN' LIMIT 1) AS asin,
                       (SELECT ph.id FROM photo ph WHERE ph.product_id = p.id AND ph.removed = 0 ORDER BY ph.id LIMIT 1) AS photo_id
                  FROM product p WHERE p.archived = 0 AND p.title != ''"""):
            alts, img = by_asin.get((p['asin'] or '').upper(), (set(), [None]))
            out.append({'kind': 'product', 'id': p['id'], 'product_id': p['id'], 'title': p['title'], 'asin': p['asin'] or '',
                        'photo_id': p['photo_id'], 'image_url': img[0], 'left': None, 'alts': sorted(alts - {p['title']})})
            if p['asin']:
                seen.add(p['asin'].upper())
        for a in self.wms._q("SELECT id, asin, title, image_url, expected FROM audit_item WHERE title != ''"):
            if a['asin'].upper() in seen or a['asin'].upper().startswith('LINE-'):
                continue
            seen.add(a['asin'].upper())
            out.append({'kind': 'audit', 'id': a['id'], 'product_id': None, 'title': a['title'], 'asin': a['asin'],
                        'photo_id': None, 'image_url': a['image_url'], 'left': a['expected']})
        for ln in self.wms._q(f"""
                SELECT l.id, l.asin, l.title, l.image_url, {LINE_LEFT_SQL} AS left_n, o.source, o.external_id
                  FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id
                 WHERE l.cancelled = 0 AND l.product_id IS NULL AND l.title != '' AND o.hidden = 0 AND o.kind = 'stock'
                   AND {LINE_LEFT_SQL} > 0
                 ORDER BY l.id DESC"""):
            key = (ln['asin'] or f"T:{ln['title'].lower()}").upper()
            if key in seen:
                continue
            seen.add(key)
            out.append({'kind': 'line', 'id': ln['id'], 'product_id': None, 'title': ln['title'], 'asin': ln['asin'],
                        'photo_id': None, 'image_url': ln['image_url'], 'left': ln['left_n'],
                        'where': f"{ln['source']} {ln['external_id']}"})
        return out

    # ------------------------------------------------------------------ a product that doesn't know its buy

    def buy_match(self, text: str = '', asin: str = '', product_id: int = 0) -> dict:
        """Alex 11-11: a Dreamcatcher album scanned by an importer's sticker barcode got another listing's ASIN, so
        Receive found no order. Buys whose titles look like `text` (or like the product's title), for the person to
        tap. asin: the catalog's answer for a scan; when we bought that ASIN, nothing is offered (it links already)."""
        asin = (asin or '').strip().upper()
        cands = self.candidates()
        have = set()
        if product_id:
            p = self.wms._q1('SELECT title FROM product WHERE id = ?', (product_id,))
            if not p:
                raise WmsError('Product not found')
            text = text or p['title']
            have = {r['value'].upper() for r in self.wms._q(
                "SELECT value FROM product_identifier WHERE product_id = ? AND kind = 'ASIN'", (product_id,))}
            cands = [c for c in cands if c['kind'] != 'product']
        if asin:
            if self.wms._q1("""SELECT 1 AS x FROM audit_item WHERE asin = ? COLLATE NOCASE
                               UNION ALL SELECT 1 FROM purchase_line WHERE asin = ? COLLATE NOCASE LIMIT 1""", (asin, asin)):
                return {'bought': True, 'matches': []}
            have.add(asin)
        cands = [c for c in cands if (c.get('asin') or '').upper() not in have]
        return {'bought': False, 'matches': rank(text, cands)}

    def link(self, product_id: int, user_id: int, kind: str = '', item_id: int = 0, code: str = '') -> dict:
        """Tie a product to a buy it was not tied to: the buy's ASIN goes on the product (Receive then finds the order
        line, and the audit row counts the receipt), or a store line with no ASIN points at the product. A scanned code
        goes on the product too, so the next scan of that sticker finds it."""
        if not self.wms._q1('SELECT 1 AS x FROM product WHERE id = ?', (product_id,)):
            raise WmsError('Product not found')
        idents = []
        line_id = None
        if kind == 'audit':
            a = self.wms._q1('SELECT asin FROM audit_item WHERE id = ?', (item_id,))
            if not a or not re.fullmatch(r'B0[0-9A-Z]{8}', a['asin'].upper()):
                raise WmsError('That audit row has no ASIN to link')
            idents.append(('ASIN', a['asin'].upper()))
        elif kind == 'line':
            ln = self.wms._q1('SELECT asin, product_id FROM purchase_line WHERE id = ?', (item_id,))
            if not ln:
                raise WmsError('Order line not found')
            if ln['asin']:
                idents.append(('ASIN', ln['asin'].upper()))
            elif ln['product_id'] and ln['product_id'] != product_id:
                raise WmsError('That order line belongs to another product already')
            else:
                line_id = item_id
        elif kind:
            raise WmsError('Pick a buy to link')
        code = (code or '').strip()
        if code:
            k = ('UPC' if re.fullmatch(r'\d{12}', code) else 'EAN' if re.fullmatch(r'\d{13}', code)
                 else 'GTIN' if re.fullmatch(r'\d{14}', code) else 'ASIN' if re.fullmatch(r'B0[0-9A-Z]{8}', code.upper())
                 else 'FNSKU' if re.fullmatch(r'X0[0-9A-Z]{8}', code.upper()) else 'OTHER')
            idents.append((k, code.upper() if k in ('ASIN', 'FNSKU') else code))
        if not idents and not line_id:
            raise WmsError('Nothing to link')
        added, set_line = [], None
        with self.wms._tx() as c:
            for k, v in idents:
                if k == 'ASIN':
                    other = c.execute("""SELECT p.id, p.title FROM product_identifier i JOIN product p ON p.id = i.product_id
                                          WHERE i.kind = 'ASIN' AND i.value = ? AND p.archived = 0 AND p.id != ?""",
                                      (v, product_id)).fetchone()
                    if other:   # sweep D5: two products with one ASIN split its listing, orders and audit row
                        raise Conflict(f'{v} is already on {other[1][:60]}. Nothing was saved: open that product instead.')
                if c.execute('INSERT OR IGNORE INTO product_identifier (product_id, kind, value) VALUES (?,?,?)',
                             (product_id, k, v)).rowcount:
                    added.append({'kind': k, 'value': v})
            if line_id and c.execute('UPDATE purchase_line SET product_id = ? WHERE id = ? AND product_id IS NULL',
                                     (product_id, line_id)).rowcount:
                set_line = line_id
            c.execute('UPDATE product SET updated_at = ?, edited_at = updated_at, updated_by = ? WHERE id = ?', (now_utc(), user_id, product_id))
        return {'linked': [v for _, v in idents] + ([f'line {line_id}'] if line_id else []), 'added': added, 'line_id': set_line}

    def unlink(self, product_id: int, user_id: int, added: list, line_id: int = 0) -> dict:
        """Undo of link: takes off only what that link added (a wrong look-alike tapped by mistake)."""
        with self.wms._tx() as c:
            for a in added or []:
                if isinstance(a, dict):
                    c.execute('DELETE FROM product_identifier WHERE product_id = ? AND kind = ? AND value = ?',
                              (product_id, str(a.get('kind') or ''), str(a.get('value') or '')))
            if line_id:
                c.execute('UPDATE purchase_line SET product_id = NULL WHERE id = ? AND product_id = ?', (line_id, product_id))
            c.execute('UPDATE product SET updated_at = ?, edited_at = updated_at, updated_by = ? WHERE id = ?', (now_utc(), user_id, product_id))
        return {'ok': True}

    # ------------------------------------------------------------------ the whole step

    def identify(self, jpeg: bytes, user_id: int, key: str) -> dict:
        token, rel = self.save_photo(jpeg)
        res = {'photo': token, 'read': None, 'matches': [], 'catalog': None, 'error': None}
        read = None
        if not key:
            res['error'] = 'The photo reader is not set up yet (no AI key on the server). Type the name. The photo still saves.'
        else:
            try:
                read = self.reader(jpeg, key)
            except PhotoIdError as e:
                res['error'] = f'{e} The photo still saves with the product.'
        if read:
            res['read'] = {k: read[k] for k in ('is_product', 'title', 'brand', 'model', 'barcode_digits', 'confidence', 'seconds')}
            if not read['is_product']:
                res['error'] = 'No product in that photo. Take it again closer, with the front of the box filling the picture.'
            else:
                code = read['barcode_digits'] if gtin_ok(read['barcode_digits']) else ''
                res['read']['barcode_digits'] = code
                if code:
                    known = self.wms.lookup(code)
                    if known:
                        p = known[0]
                        res['matches'].append({'kind': 'product', 'id': p['id'], 'product_id': p['id'], 'title': p['title'],
                                               'asin': '', 'photo_id': p.get('photo_id'), 'image_url': None,
                                               'left': None, 'score': 1.0, 'by': 'barcode'})
                    else:
                        try:
                            hits = self.catalog.lookup(code)
                            res['catalog'] = hits[0] if hits else None
                        except WmsError:
                            pass
                text = ' '.join(x for x in (read['brand'], read['title'], read['model']) if x)
                have = {m['id'] for m in res['matches'] if m['kind'] == 'product'}
                res['matches'] += [m for m in rank(text, self.candidates()) if not (m['kind'] == 'product' and m['id'] in have)]
                res['matches'] = res['matches'][:MAX_MATCHES]
        with self.wms._tx() as c:
            c.execute("""INSERT INTO photo_read (created_at, user_id, photo, model, seconds, tokens_in, tokens_out, is_product,
                                                 title, brand, model_no, barcode, error, matches)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (now_utc(), user_id, rel, (read.get('ai_model') or MODEL) if read else '', read and read['seconds'],
                       read and read.get('tokens_in'), read and read.get('tokens_out'),
                       None if not read else int(read['is_product']), (read or {}).get('title', ''),
                       (read or {}).get('brand', ''), (read or {}).get('model', ''),
                       (res['read'] or {}).get('barcode_digits', ''), res['error'],
                       json.dumps([{k: m.get(k) for k in ('kind', 'id', 'title', 'score')} for m in res['matches']])))
        return res
