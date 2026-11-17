"""Alex 11-12 (Crocs B0NHJH131B): bought 3 on 2025-06-18, 2 sold and shipped on Amazon, the last one sold today.
Orders showed today's sale, the Stock audit said "1 should be here (3 bought, 2 sold on Amazon)", and the product page
showed nothing but "Not in stock": its purchase line had no product (live: 33 lines on 12 products like that).
Now the line links by its exact code, the product page lists buys, sales and the audit row, and the audit card
shows the open sale."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from test_audit_own_count import SNAP, order, sale  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
from audit import Audit  # noqa: E402
from commerce import Commerce  # noqa: E402
from connections import Connections  # noqa: E402
from inbound import Inbound  # noqa: E402
from returns import Returns  # noqa: E402
from sales_sync import Sales  # noqa: E402
from wms import Wms  # noqa: E402

CROC = 'B0CROCS001'


def setup(tmp_path):
    w = Wms(str(tmp_path / 'w.db'))
    com = Commerce(w)
    Inbound(w)
    au = Audit(w, com, returns=Returns(w, com))
    s = Sales(w, com, Connections(str(tmp_path)))
    with w._tx() as c:
        line_id = order(c, '112-CROC-1', '2025-06-18', 'Acmebuyer', CROC, 3, cents=1418)
    au.import_snapshot([{'asin': CROC, 'title': 'Crocs clog', 'expected': 1, 'avg_cost_cents': 1418, 'cost_cents': 1418,
                         'first_buy': '2025-06-18', 'last_buy': '2025-06-18',
                         'orders': [{'id': '112-CROC-1', 'date': '2025-06-18', 'units': 1}]}], SNAP)
    sale(s, '114-S1', '2025-08-19', CROC)
    sale(s, '113-S2', '2025-12-18', CROC)
    sale(s, '112-TODAY', '2026-11-12', CROC, state='open')
    return w, com, au, s, line_id


def test_crocs_buy_sales_and_audit_tell_one_story(tmp_path):
    w, com, au, s, line_id = setup(tmp_path)
    assert w._q1('SELECT product_id FROM purchase_line WHERE id = ?', (line_id,))['product_id'] is None
    p = w.save_product({'sku': CROC, 'title': 'Crocs clog', 'owner_id': 1, 'identifiers': [{'kind': 'ASIN', 'value': CROC}]})
    with w._tx() as c:
        com.relink(c)
    assert w._q1('SELECT product_id FROM purchase_line WHERE id = ?', (line_id,))['product_id'] == p['id']

    links = com.product_links(p['id'])
    assert links['asins'] == [CROC]
    (buy,) = links['buys']
    assert (buy['order_id'], buy['qty'], buy['presold'], buy['left_to_receive']) == ('112-CROC-1', 3, 2, 1)
    assert [(x['order_id'], x['status']) for x in links['sales']] == [
        ('112-TODAY', 'open'), ('113-S2', 'shipped'), ('114-S1', 'shipped')]

    (row,) = au.list()['items']
    assert (row['expected'], row['bought'], row['amazon_sold']) == (1, 3, 2)   # an open sale is still ours to ship
    au.open_sales([row])
    assert [(o['order_id'], o['qty']) for o in row['open_sales']] == [('112-TODAY', 1)]


def test_open_fba_sale_is_not_ours_to_ship(tmp_path):
    w, com, au, s, line_id = setup(tmp_path)
    s.upsert('amazon', [{'external_id': '112-FBA', 'ordered_at': '2026-11-12', 'state': 'open', 'fulfilled_by': 'amazon',
                         'lines': [{'line_key': 'OI-FBA', 'sku': CROC, 'asin': CROC, 'title': 'x', 'qty': 1, 'price_cents': 999}]}])
    row = {'match_asin': CROC}   # the filter only; how an FBA sale counts on the row is the audit's own rule
    au.open_sales([row])
    assert [o['order_id'] for o in row['open_sales']] == ['112-TODAY']


def test_relink_takes_only_a_code_on_exactly_one_product(tmp_path):
    w = Wms(str(tmp_path / 'w.db'))
    com = Commerce(w)
    with w._tx() as c:
        po = c.execute("""INSERT INTO purchase_order (source, external_id, account, ordered_at, imported_at)
                          VALUES ('walmart.com','W1','gmail','2026-01-01','x')""").lastrowid
        shared = c.execute("INSERT INTO purchase_line (po_id, line_key, asin, upc, title, qty, unit_cost_cents) VALUES (?,?,?,?,?,?,0)",
                           (po, 'a', '', '00012345678905', 'Twin item', 1)).lastrowid
        single = c.execute("INSERT INTO purchase_line (po_id, line_key, asin, upc, title, qty, unit_cost_cents) VALUES (?,?,?,?,?,?,0)",
                           (po, 'b', '', '098765432109', 'One item', 1)).lastrowid
    for sku in ('TWIN-1', 'TWIN-2'):
        w.save_product({'sku': sku, 'title': sku, 'owner_id': 1, 'identifiers': [{'kind': 'UPC', 'value': '012345678905'}]})
    one = w.save_product({'sku': 'ONE', 'title': 'One', 'owner_id': 1, 'identifiers': [{'kind': 'EAN', 'value': '0098765432109'}]})
    with w._tx() as c:
        com.relink(c)
    got = {r['id']: r['product_id'] for r in w._q('SELECT id, product_id FROM purchase_line')}
    assert got == {shared: None, single: one['id']}
