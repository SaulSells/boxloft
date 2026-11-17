"""Boxloft admin commands.

  python3 scripts/manage.py create-user <username> "<Display Name>" [--admin] [--email x]
  python3 scripts/manage.py reset-user <username>
  python3 scripts/manage.py seed-demo          # sample warehouse for practice or local testing
  python3 scripts/manage.py check              # ledger integrity check
  python3 scripts/manage.py import-returns "Returns Status.csv" "Refund Details.csv"   # Amazon data request
  python3 scripts/manage.py store-cancels --days 400   # store cancellation/refund emails -> cancelled units (read once each)
  python3 scripts/manage.py store-shipping --days 400   # store shipping emails -> tracking numbers + items per package (read once each)
  python3 scripts/manage.py backfill-amazon 2025-02-19   # every Amazon order since that day (reports, ~6 min)
  python3 scripts/manage.py prewarm-audit      # cache the barcodes of open Stock audit ASINs before a scanning day
  python3 scripts/manage.py amazon-approval    # which ASINs Amazon lets you sell (SP-API, read-only)

One-off repairs from the first install (a new database has nothing for them to do; each is a dry run without --apply):
  import-audit snapshot.json   # load the first stock audit list (a JSON list of expected items)
  fill-images, store-links, clean-store-lines, cap-audit-cuts, bins-to-boxes, fix-sweep-data

Set BOXLOFT_DATA to the data folder (default ./data). Setup links print as a path;
put the site's origin in front (http://localhost:5101/setup?token=...).
Write them to a file with --out instead of the terminal when it matters who sees them.
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'backend'))

from auth import Auth  # noqa: E402
from wms import Wms  # noqa: E402

DATA = os.path.abspath(os.environ.get('BOXLOFT_DATA', os.path.join(ROOT, 'data')))
os.makedirs(DATA, exist_ok=True)


def emit(text: str, out: str = None):
    if out:
        with open(out, 'a') as f:
            f.write(text + '\n')
        os.chmod(out, 0o600)
        print(f'written to {out}')
    else:
        print(text)


def demo_blocker(w: Wms):
    """Why the demo can't go in, or None. It needs a warehouse with no products, no boxes and no owner but the
    business itself, so none of its names clash and it never stops half way (review 11-17: a box A1 made first)."""
    if w._q1('SELECT COUNT(*) n FROM product')['n']:
        return 'This warehouse already has products'
    if w._q1('SELECT COUNT(*) n FROM box')['n']:
        return 'This warehouse already has boxes'
    if w._q1('SELECT COUNT(*) n FROM owner')['n'] > 1:
        return 'This warehouse already has client owners'
    return None


def seed_demo(w: Wms):
    why = demo_blocker(w)
    if why:
        sys.exit(f'{why}; seed-demo only runs on an empty one.')
    # Boxes have names written on them; the letter is the market (A Amazon, E eBay, W Walmart, L local).
    client = w.create_owner('Acme Goods (3PL client)', True)
    boxes = [w.create_boxes(1, 1, None, name)[0]['id'] for name in ('A1', 'A2', 'E1', 'L1', 'A3')]
    p1 = w.save_product({'sku': 'DEMO-LEGO-42115', 'title': 'LEGO Technic Lamborghini Sian 42115', 'identifiers': [
        {'kind': 'UPC', 'value': '673419340151'}, {'kind': 'ASIN', 'value': 'B0OTJGA4FG'}, {'kind': 'FNSKU', 'value': 'X002DEMO01'}]})['id']
    p2 = w.save_product({'sku': 'DEMO-JORDAN1-10', 'title': 'Air Jordan 1 Retro High OG, size 10 (pre-owned)', 'tracking': 'unit',
                         'condition': 'Used - Very Good', 'identifiers': [{'kind': 'EBAY_SKU', 'value': 'DEMO-JORDAN1-10'}]})['id']
    p3 = w.save_product({'sku': 'ACME-MAT-BLUE', 'title': 'Acme yoga mat, blue', 'owner_id': client['id'],
                         'identifiers': [{'kind': 'SHOPIFY_SKU', 'value': 'MAT-BLUE'}, {'kind': 'UPC', 'value': '860001234567'}]})['id']
    w.receive(1, p1, 12, 31999, {'box_id': boxes[0]}, supplier='Target', reference='Demo receipt')
    w.receive(1, p1, 6, 28999, {'box_id': boxes[1]}, supplier='Walmart')
    w.receive(1, p2, 3, 8500, {'box_id': boxes[2]}, supplier='Local pickup', unit_condition='Light creasing')
    w.receive(1, p3, 48, 650, {'box_id': boxes[3]}, supplier='Acme Goods')
    w.move(1, [{'product_id': p1, 'qty': 4}], {'box_id': boxes[0]}, {'box_id': boxes[4]})
    w.remove(1, {'box_id': boxes[4]}, [{'product_id': p1, 'qty': 4}], 'FBA', note='FBA15DEMO')
    w.count(1, {'box_id': boxes[3]}, [{'product_id': p3, 'qty': 47}])
    return w.check_integrity()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('create-user')
    c.add_argument('username')
    c.add_argument('display_name')
    c.add_argument('--admin', action='store_true')
    c.add_argument('--email', default='')
    c.add_argument('--out')
    r = sub.add_parser('reset-user')
    r.add_argument('username')
    r.add_argument('--out')
    sub.add_parser('seed-demo', help='sample boxes and items in an empty warehouse')
    sub.add_parser('check', help='ledger integrity check (exit 1 on a problem)')
    ia = sub.add_parser('import-audit', help='one-off: load the first stock audit list from a JSON file')
    ia.add_argument('file')
    ir = sub.add_parser('import-returns')
    ir.add_argument('files', nargs='+')
    cc = sub.add_parser('cap-audit-cuts', help='one-off: cap Stock audit rows whose Amazon order now has fewer units (dry run)')
    cc.add_argument('--apply', action='store_true', help='write the audit_adjust rows')
    sub.add_parser('fill-images', help='one-off: photos for store-email order lines read before photos were kept')
    sub.add_parser('store-links', help="one-off: item links for store-email order lines read before links were kept (Gmail, read-only)")
    sc = sub.add_parser('store-cancels')
    sc.add_argument('--days', type=int, default=400)
    ss = sub.add_parser('store-shipping', help="store shipping emails -> tracking numbers on their orders (read once each)")
    ss.add_argument('--days', type=int, default=400)
    sub.add_parser('clean-store-lines', help='one-off: cancel status headlines saved as items before 11-10')
    bf = sub.add_parser('backfill-amazon')
    bf.add_argument('since')
    bf.add_argument('--until')
    sub.add_parser('prewarm-audit', help="cache the barcodes of every ASIN still open on the Stock audit (Keepa, 1 token each)")
    sub.add_parser('amazon-approval', help='ask Amazon which ASINs on hand, on the audit or bought lately we may sell (SP-API, read-only)')
    bb = sub.add_parser('bins-to-boxes', help='one-off: bins (A12, no building) become boxes of the same name at Main warehouse (dry run)')
    bb.add_argument('--apply', action='store_true', help='move the stock and archive the bins')
    fx = sub.add_parser('fix-sweep-data', help='one-off: repair rows saved wrong before the 11-11 sweep fixes (dry run)')
    fx.add_argument('--apply', action='store_true', help='write the repairs')
    a = ap.parse_args()
    if a.cmd == 'fix-sweep-data':
        from audit import Audit
        from commerce import Commerce
        from datafix import sweep_fix
        from inbound import Inbound
        from returns import Returns
        from sorting import Sorting
        w = Wms(os.path.join(DATA, 'wms.db'))
        com = Commerce(w)
        rt = Returns(w, com)
        Inbound(w, rt)
        au = Audit(w, com, None, rt)
        Sorting(w, com, au)
        out = sweep_fix(w, apply=a.apply)
        out['integrity_ok'] = w.check_integrity(record=False)['ok']
        print(json.dumps(out, indent=1))
        return
    if a.cmd == 'bins-to-boxes':
        from audit import Audit
        from commerce import Commerce
        from sorting import Sorting
        w = Wms(os.path.join(DATA, 'wms.db'))
        com = Commerce(w)
        out = Sorting(w, com, Audit(w, com)).bins_to_boxes(apply=a.apply)
        out['integrity_ok'] = w.check_integrity(record=False)['ok']
        print(json.dumps(out, indent=1))
        return
    if a.cmd == 'amazon-approval':
        from approval import Approval
        from connections import Connections
        ap = Approval(Wms(os.path.join(DATA, 'wms.db')), Connections(DATA))
        print(json.dumps(ap.refresh(progress=lambda t: print(t, flush=True))))
        return
    if a.cmd == 'prewarm-audit':
        from commerce import Catalog
        w = Wms(os.path.join(DATA, 'wms.db'))
        asins = [r['asin'] for r in w._q("""SELECT DISTINCT UPPER(i.asin) AS asin FROM audit_item i WHERE i.asin LIKE 'B0%'""")]
        print(json.dumps(Catalog(w, os.path.join(DATA, 'photos')).prewarm(asins, progress=lambda t: print(t, flush=True))))
        return
    if a.cmd in ('create-user', 'reset-user'):
        auth = Auth(os.path.join(DATA, 'auth.db'))
        if a.cmd == 'create-user':
            token = auth.create_user(a.username, a.display_name, 'admin' if a.admin else 'staff', a.email)
            name = a.username
        else:
            row = auth._conn().execute('SELECT id FROM users WHERE username = ?', (a.username.lower(),)).fetchone()
            if not row:
                sys.exit('No such user')
            token = auth.reset_setup(row[0])
            name = a.username
        emit(f'{name}: /setup?token={token}', a.out)
    elif a.cmd == 'import-audit':
        from audit import Audit
        from commerce import Commerce
        w = Wms(os.path.join(DATA, 'wms.db'))
        snap = json.load(open(a.file))
        print(json.dumps(Audit(w, Commerce(w)).import_snapshot(snap['items'], snap['snapshot'])))
    elif a.cmd == 'cap-audit-cuts':
        from audit import Audit
        from commerce import Commerce
        w = Wms(os.path.join(DATA, 'wms.db'))
        print(json.dumps({'applied': a.apply, 'changes': Audit(w, Commerce(w)).cap_cuts(apply=a.apply)}, indent=1))
    elif a.cmd == 'fill-images':
        from connections import Connections
        from store_mail import StoreOrders
        w = Wms(os.path.join(DATA, 'wms.db'))
        print(json.dumps(StoreOrders(w, Connections(DATA)).fill_images()))
    elif a.cmd == 'store-links':
        from connections import Connections
        from inbound import Inbound
        from store_mail import StoreOrders
        w = Wms(os.path.join(DATA, 'wms.db'))
        Inbound(w)
        print(json.dumps(StoreOrders(w, Connections(DATA)).fill_links()))
    elif a.cmd in ('store-cancels', 'clean-store-lines', 'store-shipping'):
        from connections import Connections
        from store_mail import StoreOrders
        so = StoreOrders(Wms(os.path.join(DATA, 'wms.db')), Connections(DATA))
        if a.cmd == 'store-shipping':
            print(json.dumps(so.read_shipping(a.days, progress=lambda s: print(s, end='\r', file=sys.stderr))))
        else:
            print(json.dumps(so.read_cancellations(a.days) if a.cmd == 'store-cancels' else so.clean_lines()))
    elif a.cmd == 'backfill-amazon':
        from commerce import Commerce
        from connections import Connections
        from listings import Listings
        from sales_sync import Sales
        w = Wms(os.path.join(DATA, 'wms.db'))
        com = Commerce(w)
        r = Sales(w, com, Connections(DATA)).backfill_amazon(a.since, a.until, progress=lambda t: print(t, flush=True))
        li = Listings(w, com)   # outside the transaction: its setup runs a schema script, which commits
        with w._tx() as c:      # products the audit made since: link the old sales to them
            r['sales_lines_linked'] = li._relink(c)
        print(json.dumps(r))
    elif a.cmd == 'import-returns':
        from commerce import Commerce
        from returns import Returns
        w = Wms(os.path.join(DATA, 'wms.db'))
        files = {os.path.basename(f): open(f, encoding='utf-8-sig').read() for f in a.files}
        print(json.dumps(Returns(w, Commerce(w)).import_export(files)))
    else:
        w = Wms(os.path.join(DATA, 'wms.db'))
        if a.cmd == 'seed-demo':
            print(json.dumps(seed_demo(w), indent=1)[:400])
        else:
            res = w.check_integrity()
            print(json.dumps(res, indent=1, default=str)[:2000])
            sys.exit(0 if res['ok'] else 1)


if __name__ == '__main__':
    main()
