"""Track package pages (extension 2.1): not shipped from the link kind, tracking ID, carrier, delivery photo.

Real pages are checked by scripts/eval_pages.py (amazon-track.json, 20 Track pages read 11-11); these tests
use small fakes with the same markup."""
import os
import re
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, '..', 'backend'))

import pages  # noqa: E402
import pagesync as ps  # noqa: E402
from test_pagesync import TODAY, FakeChrome, extension_allowlist, list_html, placed  # noqa: E402
from test_store_mail import make  # noqa: E402

PHOTO = ('https://us-prod-temp.s3.amazonaws.com/imageId-76d0d914-8bb4-533a-b5b1-b6e7193851d8?X-Amz-Algorithm=AWS4-HMAC-SHA256'
         '&amp;X-Amz-Expires=259200&amp;X-Amz-Signature=47b1')


def order_html(oid, packages, total='$30.00', sub='$28.00'):
    """packages: [(status, link, asin, title, qty)]; link 'S<id>' = shipmentId, 'I' = itemId only, '' = none."""
    rows = ''
    for i, (st, link, a, t, q) in enumerate(packages):
        href = ''
        if link.startswith('S'):
            href = f'/progress-tracker/package?orderId={oid}&amp;_encoding=UTF8&amp;shipmentId={link[1:]}&amp;packageIndex={i}&amp;vt=YOUR_ORDERS'
        elif link == 'I':
            href = f'/progress-tracker/package?itemId=jkmmmpqqkrnnpp&amp;orderId={oid}&amp;_encoding=UTF8'
        rows += f"""
      <div data-component="shipmentStatus"><span>{st}</span></div>
      {f'<a href="{href}">Track package</a>' if href else ''}
      <a href="/progress-tracker/package/preship/cancel-items?orderID={oid}">Cancel items</a>
      <div data-component="purchasedItems"><div class="a-row">
        <div data-component="itemImage"><img alt="{t}" src="//m.media-amazon.com/{a}.jpg">{f'<span>{q}</span>' if q > 1 else ''}</div>
        <div data-component="itemTitle"><a href="/dp/{a}?ref=x">{t}</a></div>
        <div data-component="unitPrice"><span class="a-price"><span class="a-offscreen">$14.00</span></span></div>
      </div></div>"""
    return f"""<html><body><div data-component="orderDate">{placed(3)}</div>{rows}
      <div data-component="chargeSummary">Item(s) Subtotal: {sub} Grand Total: {total}</div></body></html>"""


def track_html(headline, carrier='', tid='', note='', photo=False, event=None):
    ev = ''
    if event:
        ev = f"""<div class="a-row tracking-event-date-header"><span class="tracking-event-date">Wednesday, November 11</span></div>
          <span class="tracking-event-time">7:02 AM</span><span class="tracking-event-message">{event[0]}</span>
          <span class="tracking-event-location">{event[1]}</span>"""
    img = f'<img alt="" src="grey.gif" class="photo-on-delivery-img a-lazy-loaded" data-src="{PHOTO}"/>' if photo else ''
    return f"""<html><body><section class="pt-card promise-card"><h1 class="pt-promise-main-slot">{headline}</h1>
      {f'<div class="pt-promise-details-slot">{note}</div>' if note else ''}</section>
      <section class="pt-card delivery-card"><div class="pt-delivery-card-wrapper"><div><h3 class="a-spacing-small">{carrier}</h3>
      <div class="pt-delivery-card-trackingId">Tracking ID: {tid}</div></div><div>{img}</div></div></section>
      <div class="a-popover-preload"><div id="tracking-events-container"><div class="a-container">
      <div class="a-row tracking-event-carrier-header"><h2> {carrier} </h2></div>
      <div class="a-row tracking-event-trackingId-text"><h4> Tracking ID: {tid} </h4></div>{ev}</div></div></div></body></html>"""


class TrackChrome(FakeChrome):
    """FakeChrome that also answers Track pages and does the server's photo copy before each step."""

    def __init__(self, lists, details, tracks, photos_dir, allow_track=True):
        super().__init__(lists, details)
        self.tracks, self.photos_dir = tracks, photos_dir
        if not allow_track:
            self.allow = [r for r in self.allow if 'progress' not in r.pattern]
        self.downloads = []

    def fake_download(self, url, photos_dir):
        rel = ps.photo_rel(url)
        assert rel and '&amp;' not in url
        os.makedirs(os.path.join(photos_dir, 'track'), exist_ok=True)
        open(os.path.join(photos_dir, rel), 'wb').write(b'\xff\xd8\xff fake jpeg')
        self.downloads.append(url)
        return True

    def page(self, url):
        if not any(r.match(url) for r in self.allow):
            return {'url': url, 'refused': 'not on the allowlist'}
        m = ps.AMZ_TRACK_RE.match(url)
        if m:
            self.fetched.append(url)
            return {'url': url, 'html': self.tracks[m.group(2)]}
        return super().page(url)

    def run(self, sync, stop_at=None):
        state, results = None, []
        for _ in range(200):
            body = {'state': state, 'results': results, 'stop': {}}
            ps.prefetch_photos(body, self.photos_dir, fetch=self.fake_download)
            r = sync.step(body, 'test', TODAY)
            if r['done']:
                return r['summary']
            state, results = r['state'], [self.page(u) for u in r['fetch']]
        raise AssertionError('run did not finish')


A, B = '112-0000001-0000001', '112-0000002-0000002'


def setup(tmp_path, a_pkg2='Estimated to arrive by November 14', allow_track=True):
    wms, inbound, so = make(tmp_path)
    photos = str(tmp_path / 'photos')
    sync = ps.PageSync(wms, inbound, so, photos)
    details = {
        A: order_html(A, [('Delivered November 8 Package was left in a secure location', 'S1A2B3C4', 'B000000001', 'Square', 1),
                          (a_pkg2, 'SXY99Z', 'B000000002', 'Tamagotchi', 2)]),
        B: order_html(B, [('Arriving November 30 - December 8', 'I', 'B000000003', 'Halloween game', 1)]),
    }
    tracks = {
        '1A2B3C4': track_html('Delivered November 8', 'Delivery facilitated by Amazon', 'TBA305753140231',
                              'Package was left in a secure location', photo=True, event=('Package delivered to safe place', 'Springfield, US')),
        'XY99Z': track_html('Arriving today', 'Shipped with UPS', '1ZRQ4KQN0353343250', event=('Package is out for delivery.', 'Shelbyville, IL US')),
    }
    chrome = TrackChrome({(TODAY.year, 0): [A, B]}, details, tracks, photos, allow_track)
    return wms, inbound, sync, chrome


def po(wms, oid):
    return wms._q1("SELECT * FROM purchase_order WHERE external_id = ?", (oid,))


# ---------------------------------------------------------------- parsers

def test_link_kind_per_package_and_track_page_fields():
    o = pages.parse_amazon_detail(A, order_html(A, [('Delivered November 8', 'S1A2B3C4', 'B000000001', 'Square', 1),
                                                    ('Arriving Friday', 'I', 'B000000002', 'Tama', 1),
                                                    ('Delivered', '', 'B000000003', 'Old', 1)]), '2026-11-10')
    assert [i.get('track') for i in o['items']] == [{'shipment_id': '1A2B3C4', 'package_index': 0},
                                                    {'item_id': 'jkmmmpqqkrnnpp'}, None]
    t = pages.parse_amazon_track(track_html('Delivered November 8', 'Delivery facilitated by Amazon', 'TBA305753140231',
                                            'Package was left in a secure location', photo=True, event=('Delivered', 'Springfield, US')))
    assert (t['carrier'], t['tracking_id'], t['headline'], t['note']) == (
        'Amazon', 'TBA305753140231', 'Delivered November 8', 'Package was left in a secure location')
    assert t['photo_url'].startswith('https://us-prod-temp.s3.amazonaws.com/imageId-') and '&amp;' not in t['photo_url']
    assert t['last_event'] == {'date': 'Wednesday, November 11', 'time': '7:02 AM', 'text': 'Delivered', 'where': 'Springfield, US'}


def test_photo_links_only_from_amazons_bucket(tmp_path):
    good = PHOTO.replace('&amp;', '&')
    assert ps.photo_rel(good) == 'track/imageId-76d0d914-8bb4-533a-b5b1-b6e7193851d8.jpg'
    for bad in ('http://us-prod-temp.s3.amazonaws.com/imageId-76d0d914-8bb4?X=1', 'https://evil.com/imageId-76d0d914-8bb4?X=1',
                'https://us-prod-temp.s3.amazonaws.com.evil.com/imageId-76d0d914-8bb4?X=1',
                'https://us-prod-temp.s3.amazonaws.com/../../etc/passwd?X=1', 'https://169.254.169.254/latest/meta-data'):
        assert ps.photo_rel(bad) is None
        assert ps._download_photo(bad, str(tmp_path)) is False   # refused before any network call
    # Only Track page results are looked at.
    called = []
    body = {'results': [{'url': 'https://www.amazon.com/your-orders/order-details?orderID=' + A,
                         'html': f'<img data-src="{PHOTO}">'}]}
    assert ps.prefetch_photos(body, str(tmp_path), fetch=lambda u, d: called.append(u)) == 0 and not called


def test_allowlist_matches_server_shape():
    allow = extension_allowlist()
    url = ps.amazon_track_url(A, 'SXY99Z', 1)
    assert any(r.match(url) for r in allow) and ps.AMZ_TRACK_RE.match(url)
    for bad in (f'https://www.amazon.com/progress-tracker/package/preship/cancel-items?orderID={A}',
                f'https://www.amazon.com/progress-tracker/package?orderId={A}&itemId=jkmmmpqqkrnnpp',
                url + '&x=1', url.replace('packageIndex=1', 'packageIndex=1&ref_=x'),
                f'https://www.amazon.com/progress-tracker/package?orderId={A}&shipmentId=a/b&packageIndex=0'):
        assert not any(r.match(bad) for r in allow), bad
        assert not ps.AMZ_TRACK_RE.match(bad), bad


# ---------------------------------------------------------------- whole runs

def test_item_only_link_is_not_shipped(tmp_path):
    wms, inbound, sync, chrome = setup(tmp_path)
    chrome.run(sync)
    b = po(wms, B)
    assert b['state'] == 'ordered' and (b['expected_from'], b['expected_to']) == ('2026-11-30', '2026-12-08')
    assert not any(B in u for u in chrome.fetched if 'progress-tracker' in u)   # no shipment: no Track page


def test_run_reads_track_pages_copies_photo_and_moves_package_on(tmp_path):
    wms, inbound, sync, chrome = setup(tmp_path)
    s = chrome.run(sync)
    assert s['track'] == {'asked': 2, 'read': 2, 'photos': 1}
    assert sorted(u.split('shipmentId=')[1] for u in chrome.fetched if 'progress-tracker' in u) == [
        '1A2B3C4&packageIndex=0', 'XY99Z&packageIndex=1']
    assert len(chrome.downloads) == 1 and os.path.isfile(tmp_path / 'photos' / 'track' / 'imageId-76d0d914-8bb4-533a-b5b1-b6e7193851d8.jpg')
    # The order page said "Estimated to arrive by November 14" (not shipped); its Track page: UPS, arriving today.
    pk = wms._q("SELECT pkg, state, expected_to, shipment_id FROM purchase_package WHERE po_id = ? ORDER BY pkg", (po(wms, A)['id'],))
    assert [(p['state'], p['expected_to'], p['shipment_id']) for p in pk] == [
        ('delivered', None, '1A2B3C4'), ('shipped', TODAY.isoformat(), 'XY99Z')]
    a = po(wms, A)
    assert a['state'] == 'shipped' and a['expected_to'] == TODAY.isoformat()
    cards = {c['state']: c for c in inbound.arriving()['orders'] if c['order_id'] == A}
    d, s_ = cards['delivered']['tracks'], cards['shipped']['tracks']
    assert d[0]['tracking_id'] == 'TBA305753140231' and d[0]['carrier'] == 'Amazon' and d[0]['photo'].startswith('/api/track-photos/')
    assert d[0]['note'] == 'Package was left in a secure location'
    assert s_[0]['carrier'] == 'UPS' and s_[0]['photo'] is None and s_[0]['last_event']['where'] == 'Shelbyville, IL US'

    # Next run: the delivered package (photo copied) is not read again; the one on the way is. The order page
    # still lags ("Estimated to arrive"), and the package doesn't step back.
    chrome.fetched.clear()
    chrome.run(sync)
    tracked = [u for u in chrome.fetched if 'progress-tracker' in u]
    assert len(tracked) == 1 and 'XY99Z' in tracked[0]
    assert po(wms, A)['state'] == 'shipped'

    # It arrives: the Track page says delivered before the order page does.
    chrome.tracks['XY99Z'] = track_html('Delivered today', 'Shipped with UPS', '1ZRQ4KQN0353343250', 'Left at front door', photo=True)
    chrome.run(sync)
    a = po(wms, A)
    assert a['state'] == 'delivered' and a['delivered_on'] == TODAY.isoformat()
    events = wms._q('SELECT state FROM purchase_event WHERE po_id = ? ORDER BY id', (a['id'],))
    assert [e['state'] for e in events][-1] == 'delivered'


def test_old_extension_refuses_track_pages_and_run_still_finishes(tmp_path):
    wms, inbound, sync, chrome = setup(tmp_path, allow_track=False)
    s = chrome.run(sync)
    assert 'reload it in chrome://extensions' in s['track']['error'] and s['track']['read'] == 0
    assert s['ok'] and po(wms, B)['state'] == 'ordered'
    assert sync.wms._q1("SELECT last_error FROM sync_state WHERE channel = 'chrome'")['last_error'] is None


def test_hidden_orders_and_old_deliveries_are_not_tracked(tmp_path):
    wms, inbound, sync, chrome = setup(tmp_path, a_pkg2='Delivered October 1')
    chrome.run(sync)   # reads both of A's packages: pkg 2 delivered 40 days ago is outside the window
    fetched = [u for u in chrome.fetched if 'progress-tracker' in u]
    assert len(fetched) == 1 and '1A2B3C4' in fetched[0]
    inbound.hide(po(wms, A)['id'], True, remember=False)
    assert sync.track_wanted(TODAY) == []
