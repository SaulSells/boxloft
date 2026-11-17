"""Server page parsers vs extension 1.5.0 on real saved pages.

Usage: python3 scripts/eval_pages.py <dir>   (dir holds amazon.json and walmart.json, saved from
Alex's Chrome with the 1.5.0 parser's output beside each raw page; private, never in git)

Each Amazon page is checked twice: raw, and after trim_amazon() (what extension 2.0 sends).
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'backend'))
import pages  # noqa: E402


def trim_amazon(html: str) -> str:
    """Python copy of extension 2.0's trim (sync.js trimPage): drop script/style/svg/noscript bodies."""
    return re.sub(r'<(script|style|svg|noscript)\b[^>]*>[\s\S]*?</\1\s*>', '', html, flags=re.I)


def diff(a, b, path=''):
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            yield from diff(a.get(k, '<missing>'), b.get(k, '<missing>'), f'{path}.{k}')
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            yield f'{path}: {len(a)} items vs {len(b)}'
        for i, (x, y) in enumerate(zip(a, b)):
            yield from diff(x, y, f'{path}[{i}]')
    elif a != b:
        yield f'{path}: {a!r} != {b!r}'


def main(d):
    bad = 0
    n = 0
    a = json.load(open(os.path.join(d, 'amazon.json')))
    for lp in a['lists']:
        for label, html in (('raw', lp['html']), ('trim', trim_amazon(lp['html']))):
            got = pages.parse_amazon_list(html)
            n += 1
            # 1.5.0 also took Amazon's session id (ue_sid, in a script) for an order ID; 2.0 doesn't.
            want = [i for i in lp['ids_150'] if not re.search(r"ue_sid\s*=\s*'" + i, lp['html'])]
            sid_skipped = len(lp['ids_150']) - len(want)
            if got['ids'] != want or got['cards'] != lp['cards_150']:
                bad += 1
                print('LIST', label, lp['path'], got, lp['ids_150'], lp['cards_150'])
    t0 = time.time()
    sizes = []
    kinds = {'raw': [], 'trim': []}
    for dp in a['details']:
        for label, html in (('raw', dp['html']), ('trim', trim_amazon(dp['html']))):
            if label == 'trim':
                sizes.append((len(dp['html']), len(html)))
            got = pages.parse_amazon_detail(dp['id'], html, dp['parsed_150']['seen_at'])
            for it in got['items']:
                it.pop('package', None)   # 2.0 adds the package number; 1.5.0 had none
                tr = it.pop('track', None)   # 2.1 adds the Track link kind
                if tr is not None:
                    kinds[label].append((dp['id'], 'ship' if tr.get('shipment_id') else 'item', it['status']))
            n += 1
            ds = list(diff(dp['parsed_150'], got))
            if ds:
                bad += 1
                print('DETAIL', label, dp['id'], *ds[:6], sep='\n  ')
    print(f'amazon: {len(a["details"])} detail + {len(a["lists"])} list pages, {time.time() - t0:.1f}s; '
          f'trim keeps {sum(t for _, t in sizes) / max(1, sum(r for r, _ in sizes)):.0%} of bytes')
    wp = os.path.join(d, 'walmart.json')
    wm_tracks = 0
    if os.path.exists(wp):
        for p in json.load(open(wp))['pages']:
            if not p.get('order'):
                print('WALMART no order', p['id'], p.get('status'))
                continue
            n += 1
            got = pages.parse_walmart_order(p['order'])
            # Tracking (Alex 11-16) came after 1.5.0: graded against the page's own shipment numbers, then left out.
            want = sorted(str((g.get('shipment') or {}).get('trackingNumber') or '') for g in
                          (p['order'].get('groups_2101') or p['order'].get('groups') or []) for _ in (g.get('items') or []))
            have = sorted(i.pop('tracking', '') for i in got['items'])
            wm_tracks += sum(1 for t in have if t)
            if have != want:
                bad += 1
                print('WALMART tracking', p['id'], have, want)
            ds = list(diff(p['parsed_150'], got))
            if ds:
                bad += 1
                print('WALMART', p['id'], *ds[:6], sep='\n  ')
    # Track link kind on the saved order pages: a package Amazon says is delivered always has a shipment id.
    for label, ks in kinds.items():
        n += 1
        wrong = [k for k in ks if k[1] == 'item' and k[2].lower().startswith('delivered')]
        if wrong:
            bad += 1
            print('TRACK KIND', label, wrong[:5])
    print(f"order pages: {sum(k[1] == 'ship' for k in kinds['trim'])} shipment links, "
          f"{sum(k[1] == 'item' for k in kinds['trim'])} item-only links")
    bad, n = track_pages(d, bad, n)
    print(f'{n - bad} of {n} checks equal; walmart items with a tracking number: {wm_tracks}')
    return 1 if bad else 0


def track_pages(d, bad, n):
    """Track pages (amazon-track.json) vs what Chrome's DOM showed when they were read (amazon-track-truth.json),
    and each page's order page: its link kind for that package must match."""
    p, tp = os.path.join(d, 'amazon-track.json'), os.path.join(d, 'amazon-track-truth.json')
    if not (os.path.exists(p) and os.path.exists(tp)):
        return bad, n
    a, truth = json.load(open(p)), json.load(open(tp))['rows']
    details = {x['id']: pages.parse_amazon_detail(x['id'], trim_amazon(x['html']), '2026-11-11') for x in a['details']}
    for page, (oid7, kind, tid, photos, h1) in zip(a['tracks'], truth):
        for label, html in (('raw', page['html']), ('trim', trim_amazon(page['html']))):
            n += 1
            got = pages.parse_amazon_track(html)
            sid = (re.search(r'shipmentId=([A-Za-z0-9]+)', page['path']) or [None, None])[1]
            errs = []
            if not page['order'].endswith(oid7) or (('ship' if sid else 'item') != kind):
                errs.append(f'kind {kind}')
            want_tid = None if tid == '-' else (tid[:3], int(tid[3:]))
            got_tid = (got['tracking_id'][:3], len(got['tracking_id'])) if got['tracking_id'] else None
            if got_tid != want_tid:
                errs.append(f"tracking {got['tracking_id']} want {tid}")
            if bool(got['photo_url']) != (photos.split('/')[1] != '0'):
                errs.append(f"photo {bool(got['photo_url'])} want {photos}")
            if not h1.startswith('Shipped with') and got['headline'] != h1:
                errs.append(f"headline {got['headline']!r} want {h1!r}")
            if kind == 'ship' and not got['carrier']:
                errs.append('no carrier')
            # The order page links this package with the same kind.
            o = details.get(page['order'])
            links = [i.get('track') or {} for i in (o or {}).get('items', [])]
            if sid and not any(t.get('shipment_id') == sid for t in links):
                errs.append('order page has no link to this shipment')
            if errs:
                bad += 1
                print('TRACK', label, page['order'], *errs, sep='\n  ')
    print(f'track: {len(a["tracks"])} Track pages, {len(a["details"])} order pages')
    return bad, n


if __name__ == '__main__':
    sys.exit(main(sys.argv[1]))
