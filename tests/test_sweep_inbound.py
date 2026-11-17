"""Sweep 11-11 group C (inbound, sales sync, Walmart pages, Arriving): each test failed on a9449e7 and passes now."""
import os
import shutil
import subprocess
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import inbound as inbound_mod  # noqa: E402
import store_mail as sm  # noqa: E402
from commerce import Commerce  # noqa: E402
from connections import Connections  # noqa: E402
from inbound import Inbound  # noqa: E402
from sales_sync import Sales, amazon_order  # noqa: E402
from test_store_mail import WALMART_HTML, FakeJev, make  # noqa: E402
from wms import Wms  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), '..')


def test_c1_walmart_pickup_is_delivered_in_the_past():
    st = sm.walmart_status('Picked up Sun, Mar 22, 3:19pm', date(2026, 11, 11))
    assert (st['state'], st['delivered_on']) == ('delivered', '2026-03-22')


def _walmart_po(tmp_path):
    wms, inbound, so = make(tmp_path)
    so.connections.save('gmail', {'email': 'a@b.c', 'app_password': 'x', 'card_last4': '4242'})
    jev = FakeJev('2000145-74623142', '$15.94', {'Concealer': ('$15.94', '$15.94', None), 'Foundation': (None, None, None)})
    so.take('<w@wm>', 'Walmart.com <help@walmart.com>', 's', '2026-04-26T13:00:00Z', sm.text_from_html(WALMART_HTML), jev, '4242')
    return wms, so


def test_c2_walmart_page_dates_use_the_warehouse_day(tmp_path, monkeypatch):
    wms, so = _walmart_po(tmp_path)
    monkeypatch.setattr(inbound_mod, 'today_et', lambda: date(2026, 1, 5))   # far from any real day: date.today() can't match
    page = {'order_id': '2000145-74623142', 'subtotal_cents': 1495, 'total_cents': 1594, 'cards': ['4242'], 'items': [
        {'name': 'Pretty Concealer 198 Light', 'item_id': '1', 'qty': 1, 'line_cents': 497, 'status': 'Arrives tomorrow'}]}
    so.walmart_pages([page])
    assert wms._q1("SELECT expected_to FROM purchase_order WHERE external_id = '2000145-74623142'")['expected_to'] == '2026-01-06'


def test_walmart_page_with_no_items_cancels_nothing(tmp_path):
    wms, so = _walmart_po(tmp_path)
    before = wms._q('SELECT id, cancelled FROM purchase_line ORDER BY id')
    out = so.walmart_pages([{'order_id': '2000145-74623142', 'total_cents': 1594, 'products': [{'name': 'renamed key'}]}])
    assert out['no_items'] == 1
    assert wms._q('SELECT id, cancelled FROM purchase_line ORDER BY id') == before


def test_c3_old_open_order_the_extension_reads_stays_known(tmp_path):
    w = Wms(str(tmp_path / 'w.db'))
    Commerce(w)
    ib = Inbound(w)
    with w._tx() as c:
        po = c.execute("""INSERT INTO purchase_order (source, external_id, ordered_at, imported_at, state, last_seen_at)
                          VALUES ('amazon', '114-2920836-0577689', date('now', '-250 days'), 'x', 'ordered', 'x')""").lastrowid
        c.execute("INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents) VALUES (?, 'k', 'B0PRE00001', 'Preorder', 1, 100)", (po,))
        c.execute("""INSERT INTO purchase_order (source, external_id, ordered_at, imported_at, state)
                     VALUES ('amazon', '114-OLD-IMPORT', date('now', '-250 days'), 'x', 'ordered')""")
    k = ib.known()['orders']
    assert '114-2920836-0577689' in k and '114-OLD-IMPORT' not in k


def test_c4_fba_order_cancelled_after_pending(tmp_path):
    w = Wms(str(tmp_path / 'w.db'))
    s = Sales(w, Commerce(w), Connections(str(tmp_path)))
    item = [{'OrderItemId': '111', 'SellerSKU': 'SKU1', 'ASIN': 'B0POFT56O2', 'Title': 'Thing', 'QuantityOrdered': 1}]
    base = {'AmazonOrderId': '111-0000000-0000001', 'PurchaseDate': '2026-11-11T15:00:00Z', 'FulfillmentChannel': 'AFN'}
    s.upsert('amazon', [amazon_order({**base, 'OrderStatus': 'Pending'}, item)], live=True)
    s.upsert('amazon', [amazon_order({**base, 'OrderStatus': 'Canceled'}, [{**item[0], 'QuantityOrdered': 0}])], live=True)
    assert w._q1('SELECT status FROM sales_order')['status'] == 'cancelled'


def test_c6_package_with_shipment_id_and_estimate_is_shipped():
    items = [{'asin': 'B0X', 'qty': 1, 'package': 2, 'status': 'Estimated to arrive by November 15',
              'track': {'shipment_id': 'NxS97XZXt', 'package_index': 0}}]
    [p] = Inbound._package_states(items, date(2026, 11, 10))
    assert (p['state'], p['to']) == ('shipped', '2026-11-15')
    # with only an item id Amazon has not shipped it: stays ordered
    items[0]['track'] = {'item_id': 'abc'}
    assert Inbound._package_states(items, date(2026, 11, 10))[0]['state'] == 'ordered'


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_c5_unshipped_window_is_not_arriving_today(tmp_path):
    src = open(os.path.join(ROOT, 'web', 'src', 'lib', 'arriving.ts')).read()
    script = tmp_path / 'check.ts'
    script.write_text(src + """
const t = '2026-11-12';
console.log(JSON.stringify([
  bucket({ state: 'ordered', expected_from: '2026-11-12', expected_to: '2026-11-24' }, t),
  bucket({ state: 'shipped', expected_from: '2026-11-12', expected_to: '2026-11-24' }, t),
  bucket({ state: 'ordered', expected_from: '2026-11-10', expected_to: '2026-11-12' }, t),
]));
""")
    out = subprocess.run(['node', '--experimental-strip-types', '--no-warnings', str(script)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == '["later","today","today"]'
