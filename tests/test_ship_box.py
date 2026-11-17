"""Ship box (Alex 11-11): Amazon's label size per package from Seller Central, a person confirms the box on a popup
after auto_ship, and the next order of that item suggests the box it went out in last time."""
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, '..', 'backend'))
import pagesync as ps  # noqa: E402
from commerce import Commerce  # noqa: E402
from connections import Connections  # noqa: E402
from sales_sync import Sales  # noqa: E402
from shipbox import ShipBoxes, clean_packages  # noqa: E402
from test_auto_ship import TODAY, order  # noqa: E402
from test_pagesync import FakeChrome, extension_allowlist  # noqa: E402
from test_store_mail import make as make_inbound  # noqa: E402
from wms import Wms, WmsError  # noqa: E402

U, SAM = 1, 2


def raw_pkg(pid='PKG1', l=8, w=6, h=4, unit='IN', asins=(('B0POFT56O2', 1),), lb=1, oz=2.5, label=6.0):
    """One package the way extension/sync.js sellerPackages sends it."""
    return {'id': pid, 'carrier': 'UPS', 'type': 'PACKAGE', 'dims': {'l': l, 'w': w, 'h': h, 'unit': unit},
            'weight': [{'value': lb, 'unit': 'LB'}, {'value': oz, 'unit': 'OZ'}], 'label': label,
            'items': [{'asin': a, 'qty': q} for a, q in asins]}


def make(tmp_path):
    w = Wms(str(tmp_path / 'wms.db'))
    c = Commerce(w)
    s = Sales(w, c, Connections(str(tmp_path)))
    sb = ShipBoxes(w, c)
    loc = w.create_locations(['A-01-01'])[0]['id']
    box = w.create_boxes(1, U, loc)[0]['id']
    pid = w.save_product({'sku': 'B0POFT56O2', 'title': 'Filter', 'pkg_l_mm': 100, 'pkg_w_mm': 80, 'pkg_h_mm': 50})['id']
    w.receive(U, pid, 5, 500, {'box_id': box})
    small = c.save_ship_box({'name': 'Small', 'inner_l_in': 6, 'inner_w_in': 5, 'inner_h_in': 4, 'stock': 5})
    eight = c.save_ship_box({'name': '8x6x4', 'inner_l_in': 8, 'inner_w_in': 6, 'inner_h_in': 4, 'stock': 0})
    return w, c, s, sb, pid, small, eight


def ship_on_amazon(s, c, ext, **kw):
    s.upsert('amazon', [order(ext, qty=1, sku='B0POFT56O2', **kw)], live=True)
    s.upsert('amazon', [order(ext, 'shipped', qty=1, sku='B0POFT56O2', **kw)], live=True)
    return c.auto_ship()


def line_of(w, ext):
    return w._q1("""SELECT l.* FROM sales_line l JOIN sales_order o ON o.id = l.order_id WHERE o.external_id = ?""", (ext,))


def stock(w, box):
    return w._q1('SELECT stock FROM ship_box WHERE id = ?', (box['id'],))['stock']


# ---------------------------------------------------------------- what the extension sends

def test_clean_packages_units_and_only_package_fields():
    got = clean_packages([
        raw_pkg('P-1', 20.32, 15.24, 10.16, 'CM', lb=0, oz=0) | {'buyer': 'Jane', 'address': '1 Main St'},
        raw_pkg('bad id!'), 'junk', {'id': 'P2', 'dims': {'l': 'x'}, 'items': [{'asin': 'nope'}, {'asin': 'B0ABCDEFGH', 'qty': 2}]},
    ])
    assert [p['id'] for p in got] == ['P-1', 'P2']
    assert got[0]['lwh'] == [8.0, 6.0, 4.0] and got[0]['weight_oz'] is None and got[0]['label_cents'] == 600
    assert set(got[0]) == {'id', 'carrier', 'type', 'lwh', 'weight_oz', 'label_cents', 'asins'}
    assert got[1]['lwh'] == [None, None, None] and got[1]['asins'] == [['B0ABCDEFGH', 2]]
    assert clean_packages([raw_pkg()])[0]['weight_oz'] == 18.5   # 1 lb + 2.5 oz


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_extension_sends_package_fields_never_the_buyer(tmp_path):
    js = open(os.path.join(HERE, '..', 'extension', 'sync.js')).read()
    start = js.index('const sellerPackages')
    fn = js[start:js.index('});', start) + 3]
    order_json = {'order': {'BuyerName': 'Jane Buyer', 'ShippingAddress': {'Line1': '1 Main St'}, 'packages': [{
        'PackageId': 'abc123', 'Carrier': 'UPS', 'ShipToAddress': {'Name': 'Jane'},
        'MssDetails': {'PackageType': 'PACKAGE', 'Dimensions': {'Length': 8, 'Width': 6, 'Height': 4, 'Unit': 'IN'},
                       'PackageWeight': [{'Value': 1, 'Unit': 'LB'}, {'Value': 2, 'Unit': 'OZ'}],
                       'MssCharges': {'ShippingLabel': {'Amount': 6.0}}, 'ShipFromAddress': {'Name': 'Alex'}},
        'PackageItems': [{'ASIN': 'B0POFT56O2', 'QuantityShipped': 1, 'Title': 'Filter', 'BuyerCustomizedInfo': 'x'}]}]}}
    script = tmp_path / 'x.js'
    script.write_text(fn + f'\nconsole.log(JSON.stringify(sellerPackages({json.dumps(order_json)}.order)));')
    out = subprocess.run(['node', str(script)], capture_output=True, text=True, check=True).stdout
    assert 'Jane' not in out and 'Main St' not in out and 'Alex' not in out and 'Title' not in out
    [p] = json.loads(out)
    assert clean_packages([p])[0] == {'id': 'abc123', 'carrier': 'UPS', 'type': 'PACKAGE', 'lwh': [8, 6, 4],
                                      'weight_oz': 18.0, 'label_cents': 600, 'asins': [['B0POFT56O2', 1]]}


def test_allowlist_refuses_seller_central():
    """Alex 11-11: Seller Central reads are off. The extension (2.3+) refuses every Seller Central URL, and the
    server's own shape check still knows the one URL it once asked for."""
    allow = extension_allowlist()
    url = ps.seller_order_url('113-4270759-3440577')
    assert ps.SELLER_RE.match(url) and not any(r.match(url) for r in allow)
    assert ps.SELLER_READS is False


def test_seller_reads_off_by_default(tmp_path):
    wms, sb, sync = run_setup(tmp_path)
    chrome = SellerChrome({'111-0000012-0000012': [raw_pkg('PKG12')]})
    summary = chrome.run(sync)
    assert summary['ok'] and not summary.get('seller', {}).get('asked')
    assert not wms._q('SELECT * FROM seller_read')


@pytest.fixture
def seller_on(monkeypatch):
    """The switched-off Seller Central phase, kept working in case Alex turns it back on."""
    monkeypatch.setattr(ps, 'SELLER_READS', True)


# ---------------------------------------------------------------- the loop

def test_shipped_order_popup_confirm_then_next_order_uses_that_box(tmp_path):
    w, c, s, sb, pid, small, eight = make(tmp_path)
    assert ship_on_amazon(s, c, '111-0000001-0000001')['lines'] == 1
    assert stock(w, small) == 4 and line_of(w, '111-0000001-0000001')['box_taken'] == 1   # the size-based guess
    assert sb.wanted() == ['111-0000001-0000001']

    # The extension reads Seller Central: Amazon's label says 8 x 6 x 4 (on the list, 0 in stock).
    assert sb.take('111-0000001-0000001', [raw_pkg('PKG1')]) == {'packages': 1, 'sized': 1}
    assert sb.wanted() == []                                     # read once it has a size
    [p] = sb.pending()
    assert p['amazon']['dims'] == '8 x 6 x 4 in' and p['amazon']['box_id'] == eight['id'] and p['suggested'] == eight['id']
    assert p['amazon']['label_cents'] == 600 and p['amazon']['carrier'] == 'UPS'

    # Sam confirms 8x6x4: the Small taken by the guess goes back; 8x6x4 had none to take.
    sb.confirm(SAM, p['id'], eight['id'])
    assert stock(w, small) == 5 and stock(w, eight) == 0
    ln = line_of(w, '111-0000001-0000001')
    assert (ln['ship_box_id'], ln['box_taken'], ln['box_confirmed_by']) == (eight['id'], 0, SAM)
    assert sb.pending() == []

    # Next order of the same item: suggested and shipped in the box it went out in last time.
    s.upsert('amazon', [order('111-0000002-0000002', qty=1, sku='B0POFT56O2')], live=True)
    rec = [o for o in c.pick_list('open') if o['external_id'] == '111-0000002-0000002'][0]['recommendation']
    assert rec['box']['id'] == eight['id'] and rec['learned'] and 'last time' in rec['reason']
    s.upsert('amazon', [order('111-0000002-0000002', 'shipped', qty=1, sku='B0POFT56O2')], live=True)
    c.auto_ship()
    assert line_of(w, '111-0000002-0000002')['ship_box_id'] == eight['id']
    assert w.check_integrity(record=False)['ok']


def test_person_beats_amazon_and_other_sizes(tmp_path):
    w, c, s, sb, pid, small, eight = make(tmp_path)
    ship_on_amazon(s, c, '111-0000003-0000003')
    sb.take('111-0000003-0000003', [raw_pkg('PKG3', 12.5, 12.5, 16)])   # not on the box list
    [p] = sb.pending()
    assert p['amazon']['box_id'] is None and p['suggested'] == small['id']   # the auto_ship guess stays preselected
    got = sb.learned(pid)
    assert got['box'] is None and got['dims'] == [12.5, 12.5, 16] and 'not on your box list' in got['reason']
    with pytest.raises(WmsError):
        sb.confirm(SAM, p['id'], other='twelve by ten')
    sb.confirm(SAM, p['id'], other='12.5 x 12.5 x 16')
    assert stock(w, small) == 5                                   # the guess goes back
    assert line_of(w, '111-0000003-0000003')['box_dims'] == '12.5 x 12.5 x 16 in'
    assert 'Packed in 12.5 x 12.5 x 16 in last time' in sb.learned(pid)['reason']
    ship_on_amazon(s, c, '111-0000004-0000004')
    [p] = sb.pending()
    sb.confirm(U, p['id'], own=True)
    assert 'its own packaging' in sb.learned(pid)['reason']      # the newest person answer wins


def test_learning_skips_mixed_packages_and_box_tolerance(tmp_path):
    w, c, s, sb, pid, small, eight = make(tmp_path)
    sb.take('111-0000005-0000005', [raw_pkg('MIX', asins=(('B0POFT56O2', 1), ('B0OTHER001', 1)))])
    sb.take('111-0000006-0000006', [raw_pkg('TWO', asins=(('B0POFT56O2', 2),))])
    assert sb.learned(pid) is None
    assert sb.match_box([4.05, 8.1, 6])['id'] == eight['id']
    assert sb.match_box([8.3, 6, 4]) is None and sb.match_box([None, 6, 4]) is None


def test_wanted_is_only_orders_shipped_on_amazon_just_now(tmp_path):
    w, c, s, sb, pid, small, eight = make(tmp_path)
    ship_on_amazon(s, c, '111-0000007-0000007')
    # A person ships one with the dialog: not read (they picked the box). FBA and history orders: not read.
    s.upsert('amazon', [order('111-0000008-0000008', qty=1, sku='B0POFT56O2')], live=True)
    c.ship_line(U, line_of(w, '111-0000008-0000008')['id'], {'box_id': 1}, ship_box_id=small['id'])
    s.upsert('amazon', [order('111-0000009-0000009', 'shipped', qty=1) | {'fulfilled_by': 'amazon'}], live=True)
    s.upsert('amazon', [order('111-0000010-0000010', 'shipped', qty=1, days_ago=40)])
    # Marked shipped on Amazon but none on the shelves: waits with review, and its label is read.
    s.upsert('amazon', [order('111-0000011-0000011', qty=1, sku='NOPE')], live=True)
    s.upsert('amazon', [order('111-0000011-0000011', 'shipped', qty=1, sku='NOPE')], live=True)
    c.auto_ship()
    assert sorted(sb.wanted()) == ['111-0000007-0000007', '111-0000011-0000011']
    # A label bought elsewhere (no size): asked 3 times, then left alone.
    for _ in range(3):
        sb.take('111-0000011-0000011', [{'id': 'X1', 'dims': {}}])
    assert sb.wanted() == ['111-0000007-0000007']
    # An auto-shipped order older than 3 days is out of the window (and off the popup).
    old = (datetime.now(timezone.utc) - timedelta(days=4)).strftime('%Y-%m-%dT%H:%M:%SZ')
    with w._tx() as cx:
        trig = [r[0] for r in cx.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'action'")]
        for t in trig:
            cx.execute(f'DROP TRIGGER {t}')
        cx.execute('UPDATE action SET at = ? WHERE device = ?', (old, 'auto'))
    assert sb.wanted() == [] and sb.pending() == []


# ---------------------------------------------------------------- the extension run

class SellerChrome(FakeChrome):
    def __init__(self, seller, allow_seller=True):
        super().__init__({}, {})
        self.seller = seller
        if allow_seller:   # plays extension 2.2, the last one that read Seller Central (2.3 refuses it)
            self.allow = self.allow + [ps.SELLER_RE]

    def page(self, url):
        if not any(r.match(url) for r in self.allow):
            return {'url': url, 'refused': 'not on the allowlist'}
        m = ps.SELLER_RE.match(url)
        if m:
            self.fetched.append(url)
            return {'url': url, 'packages': self.seller.get(m.group(1), [])}
        return super().page(url)


def run_setup(tmp_path):
    wms, inbound, so = make_inbound(tmp_path)
    c = Commerce(wms)
    s = Sales(wms, c, Connections(str(tmp_path)))
    sb = ShipBoxes(wms, c)
    loc = wms.create_locations(['A-01-01'])[0]['id']
    box = wms.create_boxes(1, U, loc)[0]['id']
    pid = wms.save_product({'sku': 'B0POFT56O2', 'title': 'Filter'})['id']
    wms.receive(U, pid, 5, 500, {'box_id': box})
    ship_on_amazon(s, c, '111-0000012-0000012')
    return wms, sb, ps.PageSync(wms, inbound, so, str(tmp_path / 'photos'), sb)


def test_run_reads_seller_central_for_just_shipped_orders(tmp_path, seller_on):
    wms, sb, sync = run_setup(tmp_path)
    chrome = SellerChrome({'111-0000012-0000012': [raw_pkg('PKG12')]})
    summary = chrome.run(sync)
    assert summary['seller'] == {'asked': 1, 'read': 1, 'sizes': 1}
    assert [u for u in chrome.fetched if 'sellercentral' in u] == ['https://sellercentral.amazon.com/orders-api/order/111-0000012-0000012']
    assert sb.pending()[0]['amazon']['dims'] == '8 x 6 x 4 in'
    chrome.fetched.clear()
    assert chrome.run(sync)['seller'] == {'asked': 0, 'read': 0, 'sizes': 0}   # never read again
    assert not [u for u in chrome.fetched if 'sellercentral' in u]


def test_old_extension_refuses_seller_central_and_run_still_finishes(tmp_path, seller_on):
    wms, sb, sync = run_setup(tmp_path)
    summary = SellerChrome({}, allow_seller=False).run(sync)
    assert '2.2.0' in summary['seller']['error'] and summary['ok']
    assert sb.wanted() == ['111-0000012-0000012']                   # still wanted for the reloaded extension


def test_signed_out_of_seller_central_stops_only_that_phase(tmp_path, seller_on):
    wms, sb, sync = run_setup(tmp_path)
    state, results = None, []
    for _ in range(50):
        r = sync.step({'state': state, 'results': results, 'stop': {}}, 'test', TODAY)
        if any('sellercentral' in u for u in r['fetch']):
            break
        state, results = r['state'], [FakeChrome({}, {}).page(u) for u in r['fetch']]
    r = sync.step({'state': r['state'], 'results': [], 'stop': {'seller': 'Signed out of Seller Central'}}, 'test', TODAY)
    assert r['done'] and r['summary']['seller']['error'] == 'Signed out of Seller Central'


def test_popup_skips_lines_auto_shipped_before_it_went_live(tmp_path):
    w = Wms(str(tmp_path / 'wms.db'))
    c = Commerce(w)
    s = Sales(w, c, Connections(str(tmp_path)))
    loc = w.create_locations(['A-01-01'])[0]['id']
    box = w.create_boxes(1, U, loc)[0]['id']
    pid = w.save_product({'sku': 'B0POFT56O2', 'title': 'Filter'})['id']
    w.receive(U, pid, 5, 500, {'box_id': box})
    ship_on_amazon(s, c, '111-0000013-0000013')               # auto_ship ran before ship box was deployed
    time.sleep(1.1)
    sb = ShipBoxes(w, c)
    assert sb.pending() == []                                  # nobody is asked about the backlog
    assert sb.wanted() == ['111-0000013-0000013']              # but its Amazon label is still read and learned
    ship_on_amazon(s, c, '111-0000014-0000014')
    assert [p['external_id'] for p in sb.pending()] == ['111-0000014-0000014']


# ---------------------------------------------------------------- schedule (Alex 11-11 7:30 PM ET)

def _utc(et_hour, minute=0):
    from zoneinfo import ZoneInfo
    return datetime(2026, 11, 12, et_hour, minute, tzinfo=ZoneInfo('America/New_York')).astimezone(timezone.utc)


def test_no_run_between_10pm_and_6am_et(tmp_path):
    wms, sb, sync = run_setup(tmp_path)
    for h in (22, 23, 0, 3, 5):
        assert 'Quiet hours' in sync.not_now(_utc(h, 30))
    assert sync.not_now(_utc(6, 0)) == '' and sync.not_now(_utc(21, 59)) == ''


def test_one_run_every_two_hours(tmp_path):
    wms, sb, sync = run_setup(tmp_path)
    last = _utc(9, 3)
    with wms._tx() as c:
        c.execute("""INSERT INTO sync_state (channel, last_run_at) VALUES ('chrome', ?)
                     ON CONFLICT(channel) DO UPDATE SET last_run_at = excluded.last_run_at""",
                  (last.strftime('%Y-%m-%dT%H:%M:%SZ'),))
    assert 'Next run after 10:53 AM ET' in sync.not_now(_utc(10, 30))
    assert sync.not_now(_utc(11, 0)) == ''        # the 30-minute wake-up near the 2-hour mark runs
