"""Sweep 11-11: suspicions proved and fixed, and the live-data repair. Each failed on a9449e7 and passes now."""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from commerce import Commerce  # noqa: E402
from connections import Connections  # noqa: E402
from sales_sync import Sales  # noqa: E402
from test_sorting import setup  # noqa: E402
from wms import Wms  # noqa: E402


def test_a_u1_stock_move_is_not_a_product_edit(tmp_path):
    w = Wms(str(tmp_path / 'w.db'))
    p = w.save_product({'sku': 'S1', 'title': 'Thing'})
    b = w.create_boxes(1, 1)[0]['id']
    time.sleep(1.1)   # timestamps are whole seconds
    w.receive(1, p['id'], 1, 100, {'box_id': b})
    now = w.get_product(p['id'])
    assert now['edited_at'] == p['edited_at'] and now['updated_at'] != p['updated_at']
    time.sleep(1.1)
    w.save_product({'sku': 'S1', 'title': 'Thing 2'}, p['id'])
    assert w.get_product(p['id'])['edited_at'] != p['edited_at']


def test_b_u1_order_sync_waits_for_a_save_in_progress(tmp_path):
    w = Wms(str(tmp_path / 'w.db'))
    s = Sales(w, Commerce(w), Connections(str(tmp_path)))
    order = {'external_id': 'E-1', 'ordered_at': '2026-11-11', 'ship_by': None, 'state': 'open', 'fulfilled_by': 'me',
             'market_status': '', 'lines': [{'line_key': 'L1', 'sku': 'X', 'asin': '', 'title': 'x', 'qty': 1}]}
    w.op_lock.acquire()
    t = threading.Thread(target=s.upsert, args=('ebay', [order]))
    t.start()
    time.sleep(0.3)
    written_while_held = w._q1('SELECT COUNT(*) n FROM sales_order')['n']
    w.op_lock.release()
    t.join(5)
    assert written_while_held == 0
    assert w._q1('SELECT COUNT(*) n FROM sales_order')['n'] == 1


def test_d_u3_ebay_variation_without_label_keyed_by_specifics():
    from listings import parse_ebay_selling
    xml = """<?xml version="1.0"?><GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack>
<ActiveList><ItemArray><Item><ItemID>222</ItemID><Title>Tee</Title><Quantity>5</Quantity>
<SellingStatus><CurrentPrice currencyID="USD">12.00</CurrentPrice></SellingStatus><Variations>
<Variation><StartPrice currencyID="USD">12.00</StartPrice><Quantity>3</Quantity><VariationSpecifics><NameValueList><Name>Size</Name><Value>L</Value></NameValueList></VariationSpecifics></Variation>
<Variation><StartPrice currencyID="USD">12.00</StartPrice><Quantity>2</Quantity><VariationSpecifics><NameValueList><Name>Size</Name><Value>S</Value></NameValueList></VariationSpecifics></Variation>
</Variations></Item></ItemArray><PaginationResult><TotalNumberOfPages>1</TotalNumberOfPages></PaginationResult></ActiveList>
</GetMyeBaySellingResponse>"""
    rows, _ = parse_ebay_selling(xml)
    assert sorted(r['sku'] for r in rows) == ['ITEM-222/Size=L', 'ITEM-222/Size=S']


def test_fix_sweep_data_repairs_and_reruns_clean(tmp_path):
    from datafix import sweep_fix
    import store_mail as sm
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    sm.StoreOrders(w, Connections(str(tmp_path)))   # makes store_mail
    p = w.save_product({'sku': 'NOCOST', 'title': 'x'})['id']
    so.extra('NOCOST', boxes['A1'], 1, scan_id='e1')
    with w._tx() as c:   # saved the old way: $0 with cost_missing 0
        c.execute('UPDATE receipt SET cost_missing = 0 WHERE product_id = ?', (p,))
        po = c.execute("""INSERT INTO purchase_order (source, external_id, account, ordered_at, imported_at, state, status_text, mail_id)
                          VALUES ('walmart.com', 'W-PU', 'gmail', '2026-03-20', 'x', 'ordered', 'Picked up Sun, Mar 22, 3:19pm', '<m@w>')""").lastrowid
        c.execute("""INSERT INTO store_mail (message_id, at, sent_at, sender, subject, outcome, po_id)
                     VALUES ('<m@w>', 'x', '2026-03-21T01:30:00Z', 'w', 's', 'order', ?)""", (po,))
        c.execute("UPDATE purchase_order SET ordered_at = '2026-03-21' WHERE id = ?", (po,))
    dry = sweep_fix(w)
    assert (len(dry['a4_receipts']), dry['d7_orders'], len(dry['c1_orders'])) == (1, 1, 1)
    assert w._q1('SELECT cost_missing FROM receipt WHERE product_id = ?', (p,))['cost_missing'] == 0   # dry run wrote nothing
    sweep_fix(w, apply=True)
    assert w._q1('SELECT cost_missing FROM receipt WHERE product_id = ?', (p,))['cost_missing'] == 1
    o = w._q1('SELECT ordered_at, state, delivered_on FROM purchase_order WHERE id = ?', (po,))
    assert (o['ordered_at'], o['state'], o['delivered_on']) == ('2026-03-20', 'delivered', '2026-03-22')
    again = sweep_fix(w)
    assert (again['a4_receipts'], again['d7_orders'], again['c1_orders']) == ([], 0, [])
    assert w.check_integrity(record=False)['ok']


def test_audit_cards_share_one_rebuild_and_a_save_shows_at_once(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path, expected=3)
    calls = []
    real = au._rows
    au._rows = lambda *a, **k: calls.append(1) or real(*a, **k)
    rid = au.list()['items'][0]['id']
    for _ in range(80):
        au.item(rid)
    assert len(calls) == 1
    so.scan('012345678905', boxes['A1'], 1, scan_id='c1')   # a save: Found 1
    assert au.item(rid)['accounted'] == 1   # the save dropped the cache
