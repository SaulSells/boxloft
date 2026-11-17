"""Checks that had no test of their own (skeptical and black-hat reviews 11-17): each test fails when its check is
taken out. Photo links fetch only public https hosts; a line ships once; check_integrity finds each kind of
damage; the ledger's append-only triggers hold; owner 1 takes the app's name."""
import os
import socket
import sqlite3
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import netguard  # noqa: E402
from commerce import Catalog, Commerce  # noqa: E402
from wms import Wms, WmsError  # noqa: E402

U = 1


@pytest.fixture
def env(tmp_path):
    w = Wms(str(tmp_path / 'wms.db'))
    c = Commerce(w)
    c.catalog = Catalog(w, str(tmp_path / 'photos'))
    return w, c


# ---------------------------------------------------------------- photo links (SSRF)

@pytest.mark.parametrize('addr,public', [
    ('127.0.0.1', False), ('10.1.2.3', False), ('192.168.1.1', False), ('172.16.0.9', False),
    ('169.254.169.254', False), ('100.64.0.1', False), ('0.0.0.0', False), ('::1', False),
    ('fe80::1%en0', False), ('fd00::1', False), ('::ffff:127.0.0.1', False), ('::ffff:10.0.0.1', False),
    ('224.0.0.1', False), ('not an ip', False),
    ('8.8.8.8', True), ('151.101.1.69', True), ('2606:4700::1111', True)])
def test_only_public_addresses_pass(addr, public):
    assert netguard.is_public_ip(addr) is public


@pytest.fixture
def listener():
    """A TCP server on 127.0.0.1 that records every byte a client sends."""
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    s.listen(5)
    got = []

    def serve():
        while True:
            try:
                conn, _ = s.accept()
            except OSError:
                return
            conn.settimeout(1)
            try:
                got.append(conn.recv(4096))
            except OSError:
                got.append(b'')
            conn.close()
    threading.Thread(target=serve, daemon=True).start()
    yield s.getsockname()[1], got
    s.close()


@pytest.mark.parametrize('host', ['127.0.0.1', 'localhost'])
def test_photo_link_to_a_private_host_sends_nothing(env, listener, host):
    w, c = env
    port, got = listener
    pid = w.save_product({'sku': 'P1', 'title': 'Thing'})['id']
    assert c.catalog.save_image(pid, f'https://{host}:{port}/latest/meta-data/', U) is None
    with pytest.raises(Exception, match='not a public address'):
        netguard.fetch_public(f'https://{host}:{port}/x', 100, timeout=3)
    assert all(b == b'' for b in got)          # no TLS hello, no request line: the socket closed before any byte
    assert w.get_product(pid)['photos'] == []


def test_photo_link_must_be_https(env):
    w, c = env
    pid = w.save_product({'sku': 'P2', 'title': 'Thing'})['id']
    for url in ('http://169.254.169.254/', 'file:///etc/passwd', 'ftp://example.com/a.jpg', '', None):
        assert c.catalog.save_image(pid, url, U) is None


def test_redirect_to_plain_http_is_refused():
    h = netguard._HttpsOnlyRedirects()
    import urllib.request
    import urllib.error
    req = urllib.request.Request('https://example.com/a.jpg')
    with pytest.raises(urllib.error.URLError):
        h.redirect_request(req, None, 302, 'Found', {}, 'http://169.254.169.254/')


# ---------------------------------------------------------------- a line ships once

def test_a_line_ships_once_while_its_order_stays_open(env):
    w, c = env
    box = w.create_boxes(1, U)[0]['id']
    pid = w.save_product({'sku': 'MY-SKU', 'title': 'Thing'})['id']
    other = w.save_product({'sku': 'MY-SKU2', 'title': 'Other'})['id']
    w.receive(U, pid, 3, 500, {'box_id': box})
    report = ('order-id\torder-item-id\tpurchase-date\tsku\tproduct-name\tquantity-purchased\n'
              '114-7\t9001\t2026-10-30T12:00:00Z\tMY-SKU\tThing\t1\n'
              '114-7\t9002\t2026-10-30T12:00:00Z\tMY-SKU2\tOther\t1\n')
    c.import_sales(report, 'amazon')
    line = next(l for o in c.pick_list() for l in o['lines'] if l['product_id'] == pid)
    assert not c.ship_line(U, line['id'], {'box_id': box})['order_done']      # the other line keeps the order open
    with pytest.raises(WmsError, match='Already shipped'):
        c.ship_line(U, line['id'], {'box_id': box})
    assert w.get_product(pid)['on_hand'] == 2 and w.get_product(other)['on_hand'] == 0
    assert w.check_integrity(record=False)['ok']


# ---------------------------------------------------------------- check_integrity finds each kind of damage

def _kinds(w):
    r = w.check_integrity(record=False)
    assert not r['ok']
    return {p['type'] for p in r['problems']}


def _stocked(w, tracking='qty'):
    b1, b2 = [b['id'] for b in w.create_boxes(2, U)]
    pid = w.save_product({'sku': 'S-' + tracking, 'title': 'Thing', 'tracking': tracking})['id']
    w.receive(U, pid, 1 if tracking == 'unit' else 3, 500, {'box_id': b1})
    assert w.check_integrity(record=False)['ok']
    return pid, b1, b2


def test_integrity_finds_a_cache_that_disagrees_with_the_ledger(env):
    w, _ = env
    pid, b1, _ = _stocked(w)
    with w._tx() as c:
        c.execute('UPDATE stock_level SET qty = 2 WHERE product_id = ? AND box_id = ?', (pid, b1))
    assert {'cache_mismatch', 'nonzero_sum'} <= _kinds(w)


def test_integrity_finds_negative_stock_in_a_box(env):
    w, _ = env
    pid, b1, b2 = _stocked(w)
    with w._tx() as c:
        c.execute('INSERT INTO stock_level (product_id, unit_id, box_id, location_id, qty) VALUES (?, 0, ?, 0, -1)', (pid, b2))
        c.execute('UPDATE stock_level SET qty = qty + 1 WHERE product_id = ? AND box_id = ?', (pid, b1))
    assert 'negative' in _kinds(w)


def test_integrity_finds_a_unit_in_two_places(env):
    w, _ = env
    pid, b1, b2 = _stocked(w, 'unit')
    uid = w._q1('SELECT unit_id FROM stock_level WHERE product_id = ? AND box_id = ?', (pid, b1))['unit_id']
    with w._tx() as c:
        c.execute('INSERT INTO stock_level (product_id, unit_id, box_id, location_id, qty) VALUES (?, ?, ?, 0, 1)', (pid, uid, b2))
    assert 'unit_in_many_places' in _kinds(w)


def test_integrity_finds_a_box_whose_place_disagrees_with_its_moves(env):
    w, _ = env
    loc1, loc2 = [l['id'] for l in w.create_locations(['S-1', 'S-2'])]
    box = w.create_boxes(1, U, loc1)[0]['id']
    w.move_boxes(U, [box], loc2)
    assert w.check_integrity(record=False)['ok']
    with w._tx() as c:
        c.execute('UPDATE box SET location_id = ? WHERE id = ?', (loc1, box))
    assert 'box_location_mismatch' in _kinds(w)


# ---------------------------------------------------------------- append-only triggers

@pytest.mark.parametrize('sql', [
    'DELETE FROM stock_move', 'UPDATE stock_move SET qty = qty + 1',
    'DELETE FROM box_move', 'UPDATE box_move SET to_location_id = from_location_id',
    'DELETE FROM action', "UPDATE action SET summary = 'edited'", 'UPDATE action SET user_id = 99'])
def test_ledger_rows_cannot_be_changed(env, sql):
    w, _ = env
    loc1, loc2 = [l['id'] for l in w.create_locations(['S-1', 'S-2'])]
    box = w.create_boxes(1, U, loc1)[0]['id']
    w.move_boxes(U, [box], loc2)
    pid = w.save_product({'sku': 'T', 'title': 'Thing'})['id']
    w.receive(U, pid, 2, 100, {'box_id': box})
    with pytest.raises(sqlite3.DatabaseError, match='append-only|only gain reversed_by'):
        with w._tx() as c:
            c.execute(sql)


def test_action_reversed_by_is_set_once(env):
    w, _ = env
    box = w.create_boxes(1, U)[0]['id']
    pid = w.save_product({'sku': 'T', 'title': 'Thing'})['id']
    r = w.receive(U, pid, 2, 100, {'box_id': box})
    w.undo(U, r['action_id'])
    with pytest.raises(sqlite3.DatabaseError, match='only gain reversed_by'):
        with w._tx() as c:
            c.execute('UPDATE action SET reversed_by = NULL WHERE id = ?', (r['action_id'],))


# ---------------------------------------------------------------- owner 1 takes the app's name

def test_owner_one_is_named_after_the_app_once(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, 'APP_NAME', 'Boxco')
    w = Wms(str(tmp_path / 'wms.db'))
    assert [r['name'] for r in w._q('SELECT name FROM owner ORDER BY id')] == ['Boxco']
    monkeypatch.setattr(config, 'APP_NAME', 'Renamed later')
    w2 = Wms(str(tmp_path / 'wms.db'))
    assert [r['name'] for r in w2._q('SELECT name FROM owner ORDER BY id')] == ['Boxco']
