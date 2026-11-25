"""Alex 11-13: Sam sees at the scan whether Boxloft may sell the item on Amazon, and Listings flags what sits in A boxes
that Amazon won't let us sell. SP-API Listings Restrictions is faked here; tests never touch the network."""
import importlib
import json
import os
import sys
import threading
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import approval as approval_mod  # noqa: E402

APPLY = 'https://sellercentral.amazon.com/hz/approvalrequest/restrictions/approve?asin=B0BRANDED1'
ANSWERS = {   # Amazon's real shapes (live probe 11-13)
    'B0APPROVED': {'restrictions': []},
    'B0BRANDED1': {'restrictions': [{'marketplaceId': 'ATVPDKIKX0DER', 'conditionType': 'new_new', 'reasons': [
        {'reasonCode': 'APPROVAL_REQUIRED', 'message': 'You need approval to list in this brand.',
         'links': [{'resource': APPLY, 'verb': 'GET', 'title': 'Request Approval via Seller Central.', 'type': 'text/html'}]}]}]},
    'B0CLOSED01': {'restrictions': [{'marketplaceId': 'ATVPDKIKX0DER', 'conditionType': 'new_new', 'reasons': [
        {'reasonCode': 'NOT_ELIGIBLE', 'message': 'You are not approved to list this brand and we are currently not accepting applications.', 'links': []}]}]},
    'B0NOPAGE01': {'restrictions': [{'marketplaceId': 'ATVPDKIKX0DER', 'reasons': [
        {'reasonCode': 'ASIN_NOT_FOUND', 'message': 'ASIN does not exist in this marketplace.', 'links': []}]}]},
}


def test_parse_reads_each_reason():
    assert approval_mod.parse(ANSWERS['B0APPROVED'])['state'] == 'ok'
    a = approval_mod.parse(ANSWERS['B0BRANDED1'])
    assert (a['state'], a['code'], a['apply_url']) == ('approval', 'APPROVAL_REQUIRED', APPLY)
    assert approval_mod.parse(ANSWERS['B0CLOSED01'])['state'] == 'not_eligible'
    assert approval_mod.parse(ANSWERS['B0NOPAGE01'])['state'] == 'not_found'
    both = {'restrictions': ANSWERS['B0BRANDED1']['restrictions'] + ANSWERS['B0CLOSED01']['restrictions']}
    assert approval_mod.parse(both)['state'] == 'not_eligible'   # the worst reason wins
    odd = {'restrictions': [{'reasons': [{'reasonCode': 'SOMETHING_NEW', 'message': 'x'}]}]}
    assert approval_mod.parse(odd)['state'] == 'restricted'
    # A link off Seller Central is never offered.
    bad = {'restrictions': [{'reasons': [{'reasonCode': 'APPROVAL_REQUIRED', 'links': [{'resource': 'https://evil.example/x'}]}]}]}
    assert approval_mod.parse(bad)['apply_url'] == ''


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv('BOXLOFT_DATA', str(tmp_path))
    import server
    importlib.reload(server)
    from http.server import ThreadingHTTPServer
    server.auth.create_user('sam', 'Sam')
    uid = next(iter(server.auth.user_names()))
    cookie = 'boxloft_session=' + server.auth.create_session(uid, '127.0.0.1', 'test')
    calls = []

    def fake_http(method, url, headers=None, data=None, timeout=20, raw_text=False):
        asin = url.split('asin=')[1].split('&')[0]
        calls.append({'asin': asin, 'locked': server.wms.op_lock._is_owned(), 'timeout': timeout})
        if asin == 'B0TIMEOUT1':
            return 0, 'network error: timed out'
        return 200, ANSWERS.get(asin, {'restrictions': []})
    monkeypatch.setattr(approval_mod, '_http', fake_http)
    monkeypatch.setattr(server.approval, 'ready', lambda: True)
    monkeypatch.setattr(server.approval, '_token', lambda: 'tok')
    monkeypatch.setattr(server.connections, 'get', lambda ch: {'selling_partner_id': 'A1SELLER'} if ch == 'amazon' else {})
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{httpd.server_address[1]}'

    def call(path, body=None):
        req = urllib.request.Request(base + path, method='POST' if body is not None else 'GET',
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={'Cookie': cookie, 'X-Boxloft': '1', 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b'null')
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'null')
    yield server, call, calls, uid
    httpd.shutdown()


def _product(server, sku, asin, upc):
    return server.wms.save_product({'sku': sku, 'title': f'Item {sku}', 'identifiers': [
        {'kind': 'ASIN', 'value': asin}, {'kind': 'UPC', 'value': upc}]})['id']


def _boxes(server, uid):
    a = server.sorting.make_box('A1', uid)
    e = server.sorting.make_box('E1', uid)
    return a, e


def test_scan_into_an_a_box_says_amazon_needs_approval_and_still_saves(srv):
    server, call, calls, uid = srv
    a, e = _boxes(server, uid)
    pid = _product(server, 'BRANDED', 'B0BRANDED1', '012345678905')
    filler = _product(server, 'FILL', 'B0APPROVED', '036000291452')
    server.wms.receive(uid, filler, 1, 100, {'box_id': e['id']})   # an eBay box in use: where to move it
    st, r = call('/api/put', {'box_id': a['id'], 'scan_id': 's1', 'code': '012345678905'})
    assert st == 200 and r['kind'] == 'added' and r['here'] == 1   # Sam's call: never blocked
    am = r['amazon']
    assert (am['state'], am['can_sell'], am['in_a_box'], am['apply_url']) == ('approval', False, True, APPLY)
    assert am['other_box']['code'] == 'E1'
    assert calls and not any(c['locked'] for c in calls)   # Amazon is asked before the lock, never under it
    assert calls[0]['timeout'] <= 5
    # The answer is cached: a second scan asks Amazon nothing.
    n = len(calls)
    st, r2 = call('/api/put', {'box_id': a['id'], 'scan_id': 's2', 'code': '012345678905'})
    assert r2['amazon']['state'] == 'approval' and len(calls) == n
    # An approved item in the same box says so, with no box to move it to.
    st, ok = call('/api/put', {'box_id': a['id'], 'scan_id': 's3', 'product_id': filler})
    assert ok['amazon']['can_sell'] is True and ok['amazon']['other_box'] is None


def test_an_e_box_scan_is_not_an_a_box_and_amazon_down_still_saves(srv):
    server, call, calls, uid = srv
    a, e = _boxes(server, uid)
    pid = _product(server, 'CLOSED', 'B0CLOSED01', '012345678912')
    st, r = call('/api/put', {'box_id': e['id'], 'scan_id': 'e1', 'code': '012345678912'})
    assert r['kind'] == 'added' and r['amazon']['state'] == 'not_eligible' and r['amazon']['in_a_box'] is False
    slow = _product(server, 'SLOW', 'B0TIMEOUT1', '012345678929')
    st, r = call('/api/put', {'box_id': a['id'], 'scan_id': 'a1', 'product_id': slow})
    assert st == 200 and r['kind'] == 'added' and r['amazon']['state'] == 'unknown' and r['amazon']['can_sell'] is None
    assert pid


def test_receive_form_and_listings_flag_a_box_items_amazon_wont_take(srv, monkeypatch):
    monkeypatch.setattr(approval_mod, 'PACE', 0)
    server, call, calls, uid = srv
    a, e = _boxes(server, uid)
    good = _product(server, 'GOOD', 'B0APPROVED', '036000291452')
    brand = _product(server, 'BRANDED', 'B0BRANDED1', '012345678905')
    closed = _product(server, 'CLOSED', 'B0CLOSED01', '012345678912')
    for pid, n in ((good, 2), (brand, 3), (closed, 1)):
        server.wms.receive(uid, pid, n, 100, {'box_id': a['id']})
    server.wms.receive(uid, closed, 4, 100, {'box_id': e['id']})   # in an eBay box: fine, not flagged
    st, s = call('/api/listings/stock?channel=amazon')   # the page reads the cache only: not asked yet = unknown
    assert {r['approval']['state'] for r in s['rows']} <= {'unknown', 'approval'} and not any(c['locked'] for c in calls)
    server.approval.refresh()   # the sync loop's job
    st, li = call(f'/api/products/{brand}/listed')
    assert li['amazon']['state'] == 'approval' and li['amazon']['apply_url'] == APPLY
    st, s = call('/api/listings/stock?channel=amazon')
    by = {r['sku']: r for r in s['rows']}
    assert by['BRANDED']['approval']['state'] == 'approval' and by['CLOSED']['approval']['state'] == 'not_eligible'
    assert by['GOOD']['approval']['can_sell'] is True
    assert s['summary']['not_approved'] == {'products': 2, 'units': 4}   # A-box units only (CLOSED has 4 more in E1)


def test_refresh_asks_only_stale_answers(srv, monkeypatch):
    server, call, calls, uid = srv
    a, e = _boxes(server, uid)
    good = _product(server, 'GOOD', 'B0APPROVED', '036000291452')
    brand = _product(server, 'BRANDED', 'B0BRANDED1', '012345678905')
    for pid in (good, brand):
        server.wms.receive(uid, pid, 1, 100, {'box_id': a['id']})
    monkeypatch.setattr(approval_mod, 'PACE', 0)
    out = server.approval.refresh()
    assert out['asked'] == 2 and out['counts'] == {'ok': 1, 'approval': 1}
    assert server.approval.refresh()['asked'] == 0   # both fresh
    with server.wms._tx() as c:   # 2 days later: the restricted one is asked again (Alex may have applied), not the approved one
        c.execute("UPDATE amazon_approval SET checked_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-2 days')")
    calls.clear()
    assert server.approval.refresh()['asked'] == 1 and [c['asin'] for c in calls] == ['B0BRANDED1']


def test_check_again_asks_amazon_now_for_the_gated_list(srv, monkeypatch):
    """Alex 11-24: "I need a way to refresh the checks to see if I'm still gated". Check again asks Amazon now,
    fresh answer or not, only for the A-box items Amazon won't let us sell, outside the lock."""
    monkeypatch.setattr(approval_mod, 'PACE', 0)
    server, call, calls, uid = srv
    a, e = _boxes(server, uid)
    good = _product(server, 'GOOD', 'B0APPROVED', '036000291452')
    brand = _product(server, 'BRANDED', 'B0BRANDED1', '012345678905')
    closed = _product(server, 'CLOSED', 'B0CLOSED01', '012345678912')
    for pid in (good, brand, closed):
        server.wms.receive(uid, pid, 2, 100, {'box_id': a['id']})
    server.approval.refresh()
    assert server.approval.refresh()['asked'] == 0   # all fresh: the daily loop would ask nothing today
    monkeypatch.setitem(ANSWERS, 'B0BRANDED1', {'restrictions': []})   # Alex got approved in Seller Central
    calls.clear()
    st, r = call('/api/amazon/approval/recheck', {})
    assert st == 200 and sorted(c['asin'] for c in calls) == ['B0BRANDED1', 'B0CLOSED01']
    assert not any(c['locked'] for c in calls)
    assert (r['asked'], r['failed'], r['now_ok'], r['still']) == (2, 0, ['B0BRANDED1'], 1)
    st, s = call('/api/listings/stock?channel=amazon')
    assert s['summary']['not_approved'] == {'products': 1, 'units': 2}
    # One row's own Check again: that ASIN only, even though its answer is fresh. Junk is dropped.
    calls.clear()
    st, r = call('/api/amazon/approval/recheck', {'asins': ['b0closed01', 'not-an-asin']})
    assert st == 200 and [c['asin'] for c in calls] == ['B0CLOSED01'] and (r['asked'], r['still']) == (1, 1)
    # A second press while one runs is refused, and asks Amazon nothing.
    calls.clear()
    with server.approval._recheck_lock:
        st, r = call('/api/amazon/approval/recheck', {})
    assert st == 400 and 'already' in r['error'] and calls == []


def test_a_phone_that_lost_the_connection_reads_the_answer_by_run(srv, monkeypatch):
    """Alex 11-24: "It worked but also said load failed". The check finished on the server while the phone lost the
    POST's connection; the app reads the answer back by its run id."""
    monkeypatch.setattr(approval_mod, 'PACE', 0)
    server, call, calls, uid = srv
    a, e = _boxes(server, uid)
    server.wms.receive(uid, _product(server, 'BRANDED', 'B0BRANDED1', '012345678905'), 1, 100, {'box_id': a['id']})
    server.approval.refresh()
    st, s = call('/api/amazon/approval/recheck')
    assert st == 200 and s == {'run': '', 'running': False, 'recent': []}   # no check yet
    st, r = call('/api/amazon/approval/recheck', {'run': 'r1'})
    st, s = call('/api/amazon/approval/recheck')
    assert (s['run'], s['running'], s['done'], s['total']) == ('r1', False, 1, 1) and s['result'] == r
    # A second press while it runs is refused and leaves the running check's state alone.
    with server.approval._recheck_lock:
        st, _ = call('/api/amazon/approval/recheck', {'run': 'r2'})
    assert st == 400 and call('/api/amazon/approval/recheck')[1]['run'] == 'r1'
    # Another person's check after it still leaves r1's answer readable by its id; an unknown id says so.
    call('/api/amazon/approval/recheck', {'run': 'n1'})
    assert call('/api/amazon/approval/recheck?run=r1')[1]['result'] == r
    assert call('/api/amazon/approval/recheck?run=zz')[1] == {'run': 'zz', 'running': False, 'unknown': True}
