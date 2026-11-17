"""11-11 night: the live server had 253 auth.db and 184 wms.db file handles open with 199 threads, and 43 of each
with 3 threads after the timeout fix. Each request thread opened its own SQLite connections (threading.local) and
never closed them. On Python 3.12 a connection is only freed by the cycle collector, so a finished thread's
connections stayed open for minutes, and an idle keep-alive thread held its pair for its whole life. A thread now
hands its connections back after every request, so the open count follows the requests running, not the sockets
or the threads that ever ran."""
import gc
import http.client
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(__file__))
from test_cover_all import srv  # noqa: E402,F401  (fixture)


def _open_conns(folder: str) -> dict:
    """Open sqlite connections in this process per database file under folder (closed ones raise and are skipped)."""
    out = {}
    for o in gc.get_objects():
        if not isinstance(o, sqlite3.Connection):
            continue
        try:
            path = o.execute('PRAGMA database_list').fetchone()[2]
        except sqlite3.ProgrammingError:
            continue
        if path.startswith(folder):
            out[os.path.basename(path)] = out.get(os.path.basename(path), 0) + 1
    return out


def _get(c, path, cookie):
    c.request('GET', path, headers={'Cookie': cookie})
    r = c.getresponse()
    r.read()
    assert r.status == 200


def test_short_connections_leave_no_db_connections_behind(srv):  # noqa: F811
    server, call = srv
    port, cookie = server._test_port, server._test_cookie
    folder = os.path.realpath(server.DATA)
    gc.collect()
    gc.disable()   # what the live process sees between collections
    try:
        for _ in range(40):   # 40 requests, each on its own connection: 40 threads that start and end
            c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
            _get(c, '/api/print-jobs', cookie)
            c.close()
        n = _open_conns(folder)
    finally:
        gc.enable()
    assert n.get('auth.db', 0) <= 8 and n.get('wms.db', 0) <= 8, n


def test_idle_keepalive_connections_hold_no_db_connections(srv):  # noqa: F811
    server, call = srv
    port, cookie = server._test_port, server._test_cookie
    folder = os.path.realpath(server.DATA)
    idle = []
    for _ in range(20):   # 20 browser tabs / Caddy connections, each did one request and now waits
        c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        _get(c, '/api/print-jobs', cookie)
        idle.append(c)
    gc.collect()
    n = _open_conns(folder)
    for c in idle:   # the kept connections still work, on a pooled db connection
        _get(c, '/api/changes', cookie)
        c.close()
    assert n.get('auth.db', 0) <= 8 and n.get('wms.db', 0) <= 8, n


def test_a_request_that_died_mid_write_hands_on_no_transaction(tmp_path):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
    from auth import Auth
    a = Auth(str(tmp_path / 'auth.db'))
    c = a._conn()
    c.execute("INSERT INTO auth_log (at, event) VALUES ('x', 'half done')")   # implicit transaction, no commit
    assert c.in_transaction
    a.release()
    again = a._conn()
    assert again is c and not again.in_transaction   # same pooled connection, rolled back
    assert again.execute("SELECT COUNT(*) FROM auth_log WHERE event = 'half done'").fetchone()[0] == 0
