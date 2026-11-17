"""Split Amazon orders: one order, several packages, often of the same ASIN, each in its own state (Alex 11-10).

#112-7053600-1407237: 2 Square Registers, 1 delivered Nov 5 and 1 arriving today.
#112-1230535-2044023: 12 Tamagotchis in 2 packages of 6, both delivered.
"""
import os
import sys
from datetime import date

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, '..', 'backend'))

import audit as audit_mod  # noqa: E402
import inbound as inbound_mod  # noqa: E402
import pages  # noqa: E402
from audit import Audit  # noqa: E402
from commerce import Commerce  # noqa: E402
from inbound import Inbound  # noqa: E402
from returns import Returns  # noqa: E402
from test_pagesync import detail_html  # noqa: E402
from wms import LINE_LEFT_SQL, Wms  # noqa: E402

SQUARE, TAMA = 'B0BASPSP0B', 'B0W62RLF5P'


@pytest.fixture
def env(tmp_path, monkeypatch):
    day = {'today': date(2026, 11, 10)}
    monkeypatch.setattr(inbound_mod, 'today_et', lambda: day['today'])
    monkeypatch.setattr(audit_mod, 'today_et', lambda: day['today'])
    w = Wms(str(tmp_path / 'w.db'))
    com = Commerce(w)
    rt = Returns(w, com)
    ib = Inbound(w, rt)
    au = Audit(w, com, None, rt)
    # A snapshot whose last buy is before these orders, so delivered-not-scanned rows can join.
    au.import_snapshot([{'asin': 'B000000000', 'title': 'Old', 'expected': 1, 'cost_cents': 100, 'avg_cost_cents': 100,
                         'last_buy': '2026-10-01', 'orders': []}], 'test')
    return w, ib, au, day


def square_page(*statuses, seen='2026-11-10'):
    """Extension 2.0: the server parses the real page shape, one package block per status."""
    h = detail_html('112-7053600-1407237', 'October 20, 2026', '',
                    [(st, SQUARE, 'Square Register (2nd Generation)', 1, '$359.00') for st in statuses],
                    total='$778.62', sub='$718.00')
    return pages.parse_amazon_detail('112-7053600-1407237', h, seen)


def tama_page(*statuses, seen='2026-11-10'):
    """Extension 1.x: items with their package status and no package number."""
    return {'order_id': '112-1230535-2044023', 'from_detail': True, 'ordered_at': 'November 5, 2026', 'seen_at': seen,
            'status_text': statuses[0], 'subtotal_cents': 31104, 'total_cents': 33672,
            'items': [{'asin': TAMA, 'title': 'Tamagotchi Original Celebration Y3K', 'qty': 6, 'price_cents': 2592, 'status': st}
                      for st in statuses]}


def cards(ib, order_id):
    return [c for c in ib.arriving()['orders'] if c['order_id'] == order_id]


def units(c):
    return sum(ln['coming'] for ln in c['lines'])


def test_page_parse_numbers_each_package():
    o = square_page('Delivered November 5 Package was left in a secure location', 'Arriving today')
    assert [(i['asin'], i['package'], i['status'][:12]) for i in o['items']] == [(SQUARE, 1, 'Delivered No'), (SQUARE, 2, 'Arriving tod')]


def test_split_order_same_asin_one_delivered_one_on_the_way(env):
    w, ib, au, day = env
    ib.sync([square_page('Delivered November 5 Package was left in a secure location', 'Arriving today')])
    po = w._q1("SELECT * FROM purchase_order WHERE external_id = '112-7053600-1407237'")
    assert (po['state'], po['expected_to'], po['delivered_on']) == ('shipped', '2026-11-10', None)   # not delivered until both are
    assert po['status_text'] == '2 packages: Delivered November 5 (1) · Arriving today (1)'
    line = w._q1(f'SELECT l.id, l.qty, {LINE_LEFT_SQL} AS left_ FROM purchase_line l')
    assert (line['qty'], line['left_']) == (2, 2)                                              # one line, both units still to scan
    assert [(p['pkg'], p['qty'], p['state']) for p in w._q('SELECT * FROM purchase_package ORDER BY pkg')] == [(1, 1, 'delivered'), (2, 1, 'shipped')]
    got = cards(ib, '112-7053600-1407237')
    assert [(c['state'], c['delivered_on'] or c['expected_to'], units(c), c['part'], c['parts'], c['status_text'][:12]) for c in got] == [
        ('delivered', '2026-11-05', 1, 1, 2, 'Delivered No'), ('shipped', '2026-11-10', 1, 2, 2, 'Arriving tod')]
    assert {c['key'] for c in got} == {f"{po['id']}-1", f"{po['id']}-2"}
    # The evening after: still 1 on the way, its own day has passed. Only that unit is late; the delivered one
    # is on the stock audit (delivered 5+ days ago, not scanned in).
    day['today'] = date(2026, 11, 11)
    late = [c for c in cards(ib, '112-7053600-1407237') if c['state'] != 'delivered' and c['expected_to'] < '2026-11-11']
    assert [units(c) for c in late] == [1]
    row = next(r for r in au.list()['items'] if r['asin'] == SQUARE)
    assert (row['expected'], row['exp_delivered']) == (1, 1)
    assert au.unaccounted(ib)['lines'] == 0
    # Sam scans one in: it came from the delivered package, the other is still coming.
    pid = w.save_product({'sku': 'SQ', 'title': 'Square', 'identifiers': [{'kind': 'ASIN', 'value': SQUARE}], 'owner_id': 1})['id']
    box = w.create_boxes(1, 1)[0]['id']
    w.receive(1, pid, 1, 38931, {'box_id': box}, purchase_line_id=line['id'])
    got = cards(ib, '112-7053600-1407237')
    assert [(c['state'], units(c), c['parts']) for c in got] == [('shipped', 1, 1)]
    assert not any(r['asin'] == SQUARE and r['exp_delivered'] for r in au.list()['items'])
    # The second package arrives: the whole order is delivered now.
    ib.sync([square_page('Delivered November 5 Package was left in a secure location', 'Delivered today', seen='2026-11-11')])
    po = w._q1("SELECT * FROM purchase_order WHERE external_id = '112-7053600-1407237'")
    assert (po['state'], po['delivered_on']) == ('delivered', '2026-11-11')
    got = cards(ib, '112-7053600-1407237')
    assert [(c['state'], c['delivered_on'], units(c)) for c in got] == [('delivered', '2026-11-11', 1)]
    assert au.unaccounted(ib)['lines'] == 0
    assert w.check_integrity()['ok']


def test_split_order_fully_delivered(env):
    w, ib, au, day = env
    ib.sync([tama_page('Now arriving today 3:30 PM - 5:30 PM', 'Delivered November 6 Package was left in a secure location')])
    po = w._q1("SELECT * FROM purchase_order WHERE external_id = '112-1230535-2044023'")
    assert po['state'] == 'shipped'
    assert sorted((c['state'], units(c)) for c in cards(ib, '112-1230535-2044023')) == [('delivered', 6), ('shipped', 6)]
    # Later that day Amazon says both are delivered.
    ib.sync([tama_page('Delivered today', 'Delivered November 6 Package was left in a secure location')])
    po = w._q1("SELECT * FROM purchase_order WHERE external_id = '112-1230535-2044023'")
    assert (po['state'], po['delivered_on']) == ('delivered', '2026-11-10')   # the last package's day
    assert ib.known()['orders']['112-1230535-2044023']['state'] == 'delivered'   # off the re-read list
    got = cards(ib, '112-1230535-2044023')
    assert all(c['state'] == 'delivered' for c in got) and sum(units(c) for c in got) == 12
    assert not [c for c in ib.arriving()['orders'] if c['order_id'] == '112-1230535-2044023' and c['state'] != 'delivered']
    # Two days on: the package delivered Nov 6 is 5+ days old and joins the audit; today's package doesn't yet.
    day['today'] = date(2026, 11, 12)
    row = next(r for r in au.list()['items'] if r['asin'] == TAMA)
    assert (row['exp_delivered'], row['orders'][0]['delivered_on']) == (6, '2026-11-06')
    day['today'] = date(2026, 11, 15)
    row = next(r for r in au.list()['items'] if r['asin'] == TAMA)
    assert row['exp_delivered'] == 12
    assert au.unaccounted(ib)['lines'] == 0


def test_cancelled_package_inside_a_shipping_order(env):
    w, ib, au, day = env
    o = tama_page('Arriving tomorrow', 'Cancelled Your order was cancelled.')
    ib.sync([o])
    po = w._q1("SELECT * FROM purchase_order WHERE external_id = '112-1230535-2044023'")
    assert (po['state'], po['expected_to']) == ('shipped', '2026-11-11')
    assert w._q1('SELECT qty FROM purchase_line')['qty'] == 6                  # the cancelled 6 were never bought
    assert [units(c) for c in cards(ib, '112-1230535-2044023')] == [6]


def test_older_extension_without_package_status_keeps_order_state(env):
    w, ib, au, day = env
    o = tama_page('x', 'x')
    for it in o['items']:
        it.pop('status')
    o['status_text'] = 'Arriving tomorrow'
    ib.sync([o])
    assert not w._q('SELECT 1 FROM purchase_package')
    [c] = cards(ib, '112-1230535-2044023')
    assert (c['state'], c['expected_to'], units(c), c['parts'], c['key']) == ('shipped', '2026-11-11', 12, 1, str(c['id']))


def test_today_is_eastern_time():
    from datetime import datetime, timezone
    assert inbound_mod.today_et() == datetime.now(timezone.utc).astimezone(inbound_mod.ET).date()
