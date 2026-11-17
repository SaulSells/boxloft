"""Alex 11-11 "I want it all covered": every save that replaces what a screen showed sends `seen` (the values the
screen showed). A save from an older screen is refused (409) with who changed it; nothing is written. Runs a real
server on a temp folder with two signed-in users (the HTTP race harness does the same on a live copy)."""
import importlib
import json
import os
import sys
import threading
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv('BOXLOFT_DATA', str(tmp_path))
    import server
    importlib.reload(server)
    from http.server import ThreadingHTTPServer
    server.auth.create_user('alex', 'Alex', 'admin')
    server.auth.create_user('sam', 'Sam')
    ids = {v: k for k, v in server.auth.user_names().items()}
    cookie = {who: 'boxloft_session=' + server.auth.create_session(ids[who], '127.0.0.1', 'test') for who in ('Alex', 'Sam')}
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{httpd.server_address[1]}'
    server._test_port, server._test_cookie = httpd.server_address[1], cookie['Sam']   # raw-connection tests

    def call(who, path, body=None):
        req = urllib.request.Request(base + path, method='POST' if body is not None else 'GET',
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={'Cookie': cookie[who], 'X-Boxloft': '1', 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b'null')
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'null')
    yield server, call
    httpd.shutdown()


def test_box_rename_from_an_old_screen_is_refused_with_who(srv):
    server, call = srv
    box = server.wms.create_boxes(1, 1)[0]
    screen = {'name': box['name'], 'notes': '', 'archived': 0}
    assert call('Sam', f"/api/boxes/{box['id']}", {'name': 'Blue tote', 'seen': screen})[0] == 200
    st, body = call('Alex', f"/api/boxes/{box['id']}", {'name': 'Alex name', 'seen': screen})
    assert st == 409 and 'Sam changed this box at' in body['error'] and 'Nothing was saved' in body['error']
    assert server.wms.get_box(box['id'])['name'] == 'Blue tote'
    fresh = {'name': 'Blue tote', 'notes': '', 'archived': 0}
    assert call('Alex', f"/api/boxes/{box['id']}", {'name': 'Alex name', 'seen': fresh})[0] == 200
    assert call('Alex', f"/api/boxes/{box['id']}", {'name': 'No seen sent'})[0] == 200   # old tabs are not checked


def test_cancel_an_order_that_shipped_since_the_screen_loaded(srv):
    server, call = srv
    w, com = server.wms, server.commerce
    pid = w.save_product({'sku': 'T-1', 'title': 'Thing', 'owner_id': 1})['id']
    box = w.create_boxes(1, 1)[0]['id']
    w.receive(1, pid, 2, 500, {'box_id': box})
    server.sales.upsert('amazon', [{'external_id': 'O-1', 'ordered_at': '2026-11-10', 'state': 'open', 'fulfilled_by': 'me',
                                    'lines': [{'line_key': 'L1', 'sku': 'T-1', 'asin': '', 'title': 'Thing', 'qty': 1, 'price_cents': 999}]}])
    line = w._q1('SELECT l.id, l.order_id FROM sales_line l')
    com.link_line(line['id'], pid)
    assert call('Sam', f"/api/orders/lines/{line['id']}/ship", {'from': {'box_id': box}})[0] == 200
    st, body = call('Alex', f"/api/orders/{line['order_id']}/status", {'status': 'cancelled', 'seen': {'status': 'open'}})
    assert st == 409 and 'Sam shipped this order at' in body['error'] and 'Nothing was saved' in body['error']
    assert w._q1('SELECT status FROM sales_order WHERE id = ?', (line['order_id'],))['status'] == 'shipped'


def test_count_refused_when_stock_moved_since_the_count_started(srv):
    server, call = srv
    w = server.wms
    pid = w.save_product({'sku': 'T-2', 'title': 'Counted', 'owner_id': 1})['id']
    box = w.create_boxes(1, 1)[0]['id']
    w.receive(1, pid, 2, 500, {'box_id': box})
    screen = [{'product_id': pid, 'unit_id': 0, 'qty': 2}]
    sam = {v: k for k, v in server.auth.user_names().items()}['Sam']
    w.receive(sam, pid, 1, 500, {'box_id': box})          # Sam puts one more in while Alex counts
    counted = [{'product_id': pid, 'unit_id': 0, 'qty': 2}]
    st, body = call('Alex', '/api/count', {'holder': {'box_id': box}, 'counted': counted, 'seen': screen})
    assert st == 409 and 'Sam moved stock in or out of it' in body['error']
    assert sum(r['qty'] for r in w.holder_contents({'box_id': box})) == 3
    fresh = [{'product_id': pid, 'unit_id': 0, 'qty': 3}]
    assert call('Alex', '/api/count', {'holder': {'box_id': box}, 'counted': fresh, 'seen': fresh})[0] == 200


def test_numbers_compare_loosely(srv):
    server, _ = srv
    assert server._norm(None) == server._norm('') and server._norm(True) == 1 and server._norm('5') == server._norm(5.0)
    assert server._norm(2.5) == server._norm('2.5') and server._norm('Blue') == 'Blue'


def test_line_cost_from_an_old_screen_is_refused_with_who(srv):
    """Alex 11-17: a buy cost typed on a box page line. Sam types $12.99; Alex's screen still showed not entered."""
    server, call = srv
    w = server.wms
    box = w.create_boxes(1, 1)[0]
    w.save_product({'sku': 'MUG-2', 'title': 'Red mug', 'identifiers': [{'kind': 'UPC', 'value': '0712345678928'}]})
    st, put = call('Sam', '/api/put', {'box_id': box['id'], 'code': '0712345678928', 'scan_id': 's1'})
    assert st == 200 and put['how'] == 'new' and put['cost_cents'] is None
    st, saved = call('Sam', f"/api/put/{put['add_id']}/cost", {'cost_cents': 1299, 'seen': {'cost_cents': None}})
    assert st == 200 and (saved['cost_cents'], saved['units'], saved['avg_cost_cents']) == (1299, 1, 1299)
    st, body = call('Alex', f"/api/put/{put['add_id']}/cost", {'cost_cents': 900, 'seen': {'cost_cents': None}})
    assert st == 409 and 'Sam changed this cost to $12.99' in body['error'] and 'Nothing was saved' in body['error']
    assert server.sorting.line_cost(put['add_id'])['cost_cents'] == 1299
    assert call('Alex', f"/api/put/{put['add_id']}/cost", {'cost_cents': 900, 'seen': {'cost_cents': 1299}})[0] == 200
