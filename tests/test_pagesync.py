"""Extension 2.0: server-side page parsers and the step planner (backend/pages.py, backend/pagesync.py)."""
import os
import re
import subprocess
import sys
from datetime import date, timedelta

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, '..', 'backend'))

import pages  # noqa: E402
import pagesync as ps  # noqa: E402
from test_store_mail import FakeJev, WALMART_HTML, make  # noqa: E402
import store_mail as sm  # noqa: E402

TODAY = date(2026, 11, 10)
CORPUS = os.path.expanduser(os.environ.get('BOXLOFT_PAGE_CORPUS', '~/boxloft-page-corpus'))


def detail_html(oid, placed, status, items, total='$30.00', sub='$28.00'):
    rows = ''.join(f"""
      <div data-component="shipmentStatus"><span>{st}</span></div>
      <div data-component="purchasedItems"><div class="a-row">
        <div data-component="itemImage"><img alt="{t}" src="//m.media-amazon.com/{a}.jpg" data-a-hires="">{f'<span>{q}</span>' if q > 1 else ''}</div>
        <div data-component="itemTitle"><a href="/dp/{a}?ref=x">{t}</a></div>
        <div data-component="unitPrice"><span class="a-price"><span class="a-offscreen">{p}</span></span></div>
      </div></div>""" for (st, a, t, q, p) in items)
    return f"""<html><head><title>Order Details</title><script>var ue_sid = '135-1680104-7989391';</script></head><body>
      <div data-component="orderDate">{placed}</div><p>Order # {oid}<div>{status}</div>{rows}
      <div data-component="chargeSummary">Item(s) Subtotal: {sub} Grand Total: {total}</div></body></html>"""


def list_html(ids):
    cards = ''.join(f'<div class="order-card js-order-card"><span>Order # {i}</span></div>' for i in ids)
    return f"<html><script>var ue_sid = '135-1680104-7989391';</script><body>{cards}</body></html>"


# ---------------------------------------------------------------- parsers

def test_amazon_detail_parse():
    h = detail_html('112-0000001-0000001', 'November 3, 2026', '', [
        ('Delivered November 5', 'B000000001', 'Lego &amp; Bricks', 3, '$12.50'),
        ('Return started', 'B000000002', 'Socks', 1, '$1,003.00')])
    o = pages.parse_amazon_detail('112-0000001-0000001', h, '2026-11-10')
    assert o['ordered_at'] == 'November 3, 2026' and o['status_text'] == 'Return started'
    assert (o['subtotal_cents'], o['total_cents']) == (2800, 3000)
    assert [(i['asin'], i['title'], i['qty'], i['price_cents'], i['status']) for i in o['items']] == [
        ('B000000001', 'Lego & Bricks', 3, 1250, 'Delivered November 5'), ('B000000002', 'Socks', 1, 100300, 'Return started')]
    assert o['items'][0]['image'] == 'https://m.media-amazon.com/B000000001.jpg'


def test_amazon_list_skips_session_id():
    lp = pages.parse_amazon_list(list_html(['112-0000001-0000001', '000-0000000-0000000', '114-0000002-0000002']))
    assert lp == {'ids': ['112-0000001-0000001', '114-0000002-0000002'], 'cards': 3}


def test_cents_reads_like_parsefloat():
    assert pages.cents('$12.34.') == 1234 and pages.cents('$ 1,000') == 100000 and pages.cents('free') is None
    assert pages.cents('$0.125') == 13 and pages.cents('$0.285') == 28   # Math.round: halves up, float error kept


def test_walmart_parse():
    o = {'displayId': '2000145-74623142', 'orderDate': '2026-04-26T13:00:00', 'paymentMethods': [{'description': 'Visa ending in 4242'}],
         'priceDetails': {'subTotal': {'value': 14.95}, 'grandTotal': {'value': 15.94}},
         'groups_2101': [{'status': {'message': {'parts': [{'text': 'Delivered '}, {'text': 'Mon, Apr 27'}]}},
                          'shipment': {'trackingNumber': '901323479428'},
                          'items': [{'quantity': 2, 'productInfo': {'name': 'Concealer', 'usItemId': '58', 'imageInfo': {'thumbnailUrl': 'u'}},
                                     'priceInfo': {'linePrice': {'value': 9.94}}}]},
                         {'status': {'statusType': 'CANCELED'}, 'items': [{'productInfo': {'name': 'Foundation'}}]}]}
    assert pages.parse_walmart_order(o) == {
        'order_id': '2000145-74623142', 'ordered_at': '2026-04-26', 'subtotal_cents': 1495, 'total_cents': 1594, 'cards': ['4242'],
        'items': [{'name': 'Concealer', 'item_id': '58', 'qty': 2, 'line_cents': 994, 'image': 'u', 'status': 'Delivered Mon, Apr 27',
                   'tracking': '901323479428'},
                  {'name': 'Foundation', 'item_id': '', 'qty': 1, 'line_cents': None, 'image': None, 'status': 'CANCELED',
                   'tracking': ''}]}


@pytest.mark.skipif(not os.path.exists(os.path.join(CORPUS, 'amazon.json')), reason='private page corpus not on this machine')
def test_real_pages_match_extension_150():
    """Alex's real pages (private corpus): server parsers == extension 1.5.0's output, raw and trimmed."""
    r = subprocess.run([sys.executable, os.path.join(HERE, '..', 'scripts', 'eval_pages.py'), CORPUS], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-3000:]


# ---------------------------------------------------------------- planner

def extension_allowlist():
    """The ALLOW regexes from extension/sync.js, so planned URLs are checked against the real list."""
    js = open(os.path.join(HERE, '..', 'extension', 'sync.js')).read()
    block = js[js.index('const ALLOW = {'):js.index('};', js.index('const ALLOW = {'))]
    return [re.compile(m.replace('\\/', '/')) for m in re.findall(r'/(\^[^\n]*?\$)/', block)]


class FakeChrome:
    """Plays the extension: runs steps, answers each URL from a fake Amazon/Walmart, checks the allowlist."""

    def __init__(self, amazon_lists, details, walmart=None):
        self.lists, self.details, self.walmart = amazon_lists, details, walmart or {}
        self.allow = extension_allowlist()
        self.fetched = []

    def page(self, url):
        assert any(r.match(url) for r in self.allow), f'not on the extension allowlist: {url}'
        self.fetched.append(url)
        m = ps.AMZ_LIST_RE.match(url)
        if m:
            ids = self.lists.get((int(m.group(1)), int(m.group(2))), [])
            return {'url': url, 'html': list_html(ids)}
        m = ps.AMZ_DETAIL_RE.match(url)
        if m:
            return {'url': url, 'html': self.details[m.group(1)]}
        oid = ps.WMT_RE.match(url).group(1)
        return {'url': url, 'order': self.walmart.get(oid)}

    def run(self, sync, stop_at=None):
        state, results, stop = None, [], {}
        for _ in range(200):
            r = sync.step({'state': state, 'results': results, 'stop': stop}, 'test', TODAY)
            if r['done']:
                return r['summary']
            state, results = r['state'], []
            for u in r['fetch']:
                site = 'walmart' if 'walmart' in u else 'amazon'
                if stop_at and stop_at[0] == site and stop_at[1] in u:
                    stop = {site: 'captcha'}
                    break
                results.append(self.page(u))
        raise AssertionError('run did not finish')


def placed(days_ago):
    d = TODAY - timedelta(days=days_ago)
    return d.strftime('%B ') + str(d.day) + d.strftime(', %Y')


def oid(n):
    return f'112-{n:07d}-0000000'


def world(n_orders):
    """n_orders Amazon orders, one every 7 days back from today, 10 per list page, years split properly."""
    lists, details = {}, {}
    by_year = {}
    for n in range(n_orders):
        d = TODAY - timedelta(days=7 * n)
        by_year.setdefault(d.year, []).append(oid(n))
        details[oid(n)] = detail_html(oid(n), placed(7 * n), '', [
            ('Arriving November 14' if n == 0 else 'Delivered', f'B{n:09d}', f'Item {n}', 1, '$10.00')], '$10.60', '$10.00')
    for y, ids in by_year.items():
        for i in range(0, len(ids), 10):
            lists[(y, i)] = ids[i:i + 10]
    return lists, details


def test_first_run_reads_back_to_cutoff_then_walmart(tmp_path):
    wms, inbound, so = make(tmp_path)
    sync = ps.PageSync(wms, inbound, so)
    lists, details = world(40)            # 40 weeks: past the 180-day cutoff
    ch = FakeChrome(lists, details)
    s = ch.run(sync)
    got = {r['external_id'] for r in wms._q("SELECT external_id FROM purchase_order WHERE source = 'amazon'")}
    assert got == {oid(n) for n in range(26)}            # 25 weeks = 175 days in; week 26 = 182 days is too old
    assert s['ok'] and s['skipped_old'] >= 1 and s['backfill_done'] and s['found_new'] == 26
    assert wms._q1("SELECT cursor FROM sync_state WHERE channel = 'chrome'")['cursor'] == 'backfill_done'
    o = inbound.known()['orders'][oid(0)]
    assert o['state'] == 'shipped'
    line = wms._q1('SELECT unit_cost_cents FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id WHERE o.external_id = ?', (oid(3),))
    assert line['unit_cost_cents'] == 1060   # tax spread like 1.x

    # Second run: one list page (nothing new on it), then the one open order again. No backfill walk.
    ch2 = FakeChrome(lists, details)
    s2 = ch2.run(sync)
    assert ch2.fetched == [ps.amazon_list_url(2026, 0), ps.amazon_detail_url(oid(0))]
    assert s2['found_new'] == 0 and s2['sent'] == 1


def test_new_order_is_read_and_page_walk_stops(tmp_path):
    wms, inbound, so = make(tmp_path)
    sync = ps.PageSync(wms, inbound, so)
    lists, details = world(12)
    FakeChrome(lists, details).run(sync)
    new = '113-9999999-0000000'
    details[new] = detail_html(new, placed(0), '', [('Not yet shipped', 'B999999999', 'New thing', 2, '$5.00')], '$10.00', '$10.00')
    lists[(2026, 0)] = [new] + lists[(2026, 0)][:9]
    ch = FakeChrome(lists, details)
    s = ch.run(sync)
    assert s['found_new'] == 1
    assert ps.amazon_list_url(2026, 10) in ch.fetched          # page 1 had something new, so page 2 is read
    assert ps.amazon_list_url(2026, 20) not in ch.fetched      # page 2 had nothing new: stop
    assert wms._q1("SELECT state FROM purchase_order WHERE external_id = ?", (new,))['state'] == 'ordered'


def test_walmart_orders_from_email_get_page_data(tmp_path):
    wms, inbound, so = make(tmp_path)
    so.connections.save('gmail', {'email': 'a@b.c', 'app_password': 'x', 'card_last4': '4242'})
    jev = FakeJev('2000145-74623142', '$15.94', {'Concealer': ('$15.94', '$15.94', None), 'Foundation': (None, None, None)})
    po = so.take('<w@wm>', 'Walmart.com <help@walmart.com>', 's', '2026-04-26T13:00:00Z', sm.text_from_html(WALMART_HTML), jev, '4242')['po_id']
    order = {'displayId': '2000145-74623142', 'orderDate': '2026-04-26', 'paymentMethods': [{'description': 'Visa 4242'}],
             'priceDetails': {'subTotal': {'value': 14.95}, 'grandTotal': {'value': 15.94}},
             'groups_2101': [{'status': {'statusType': 'Delivered Mon, Apr 27'}, 'items': [
                 {'productInfo': {'name': 'Pretty Concealer', 'usItemId': '58'}, 'priceInfo': {'linePrice': {'value': 4.97}}}]},
                 {'status': {'statusType': 'Canceled'}, 'items': [
                     {'productInfo': {'name': 'Pretty Foundation', 'usItemId': '56'}, 'priceInfo': {'linePrice': {'value': 9.98}}}]}]}
    ch = FakeChrome({}, {}, {'200014574623142': order})
    s = ch.run(sync := ps.PageSync(wms, inbound, so))
    assert ch.fetched[-1] == 'https://www.walmart.com/orders/200014574623142'
    assert s['walmart'] == {'asked': 1, 'read': 1, 'sent': 1, 'missing': 0}
    assert wms._q1('SELECT review FROM purchase_order WHERE id = ?', (po,))['review'] == 'prices from the walmart.com order page'
    assert so.walmart_wanted() == {'orders': []}
    assert sync.step({}, 'test', TODAY)['fetch'] == [ps.amazon_list_url(2026, 0)]   # a fresh run starts on Amazon


def test_amazon_stop_skips_to_walmart_and_walmart_stop_ends(tmp_path):
    wms, inbound, so = make(tmp_path)
    sync = ps.PageSync(wms, inbound, so)
    lists, details = world(12)
    s = FakeChrome(lists, details).run(sync, stop_at=('amazon', 'order-details'))
    assert not s['ok'] and s['error'] == 'captcha' and s['found_new'] == 0
    assert wms._q1("SELECT cursor FROM sync_state WHERE channel = 'chrome'")['cursor'] is None   # backfill not done


def test_bad_state_and_too_many_results_refused(tmp_path):
    wms, inbound, so = make(tmp_path)
    sync = ps.PageSync(wms, inbound, so)
    with pytest.raises(ps.PageSyncError):
        sync.step({'state': {'v': 1}}, '', TODAY)
    with pytest.raises(ps.PageSyncError):
        sync.step({'results': [{}] * 21}, '', TODAY)


def test_refused_and_failed_pages_are_skipped(tmp_path):
    wms, inbound, so = make(tmp_path)
    sync = ps.PageSync(wms, inbound, so)
    r = sync.step({}, '', TODAY)
    r = sync.step({'state': r['state'], 'results': [{'url': r['fetch'][0], 'refused': 'not on the allowlist'}]}, '', TODAY)
    assert r['done'] and r['summary']['error'] == 'list page not read: not on the allowlist'
