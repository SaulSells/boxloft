import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

import pytest  # noqa: E402

from merge import Merge  # noqa: E402
from test_sorting import ASIN, setup  # noqa: E402
from wms import WmsError  # noqa: E402

Merge.enabled = True


def _row(w):
    return w._q1('SELECT id FROM audit_item WHERE asin = ?', (ASIN,))['id']


def _in_box(w, pid, box):
    return w._q1('SELECT COALESCE(SUM(qty), 0) AS n FROM stock_level WHERE product_id = ? AND box_id = ?',
                 (pid, box['box_id']))['n']


def test_undo_of_extras_a_new_merge_never_rebooked_is_refused(tmp_path):
    """S came in as 3 extras on 2 box-page saves (2, then 1). Its Stock audit row has 1 left, so "Same item as Stock
    audit row" finds 1 and voids that 1 (newest receipt). The older save's 2 units are still plain extra stock, nothing
    counts them anywhere. Sam scanned those 2 by mistake and taps Undo: bec4d16's guard refuses it ("A merge of items
    counted these units on its Stock audit row since") for any extra receipt older than the merge, though receipt_void
    says exactly which units the merge took. The 2 phantom units can't be undone."""
    w, au, so, cat, inbound, b = setup(tmp_path, expected=1)
    m = Merge(w, au.commerce, au)
    s = w.save_product({'sku': 'S-1', 'title': 'Toploader (scanned)'})['id']
    older = w.receive(1, s, 2, 900, b['A1'], reference='box page')['action_id']
    w.receive(1, s, 1, 900, b['A1'], reference='box page')
    row = _row(w)
    m.run(s, {'audit_id': row}, 1, seen={'on_hand': 3})
    assert au.item(row)['accounted'] == 1 and _in_box(w, s, b['A1']) == 3
    voided_on_older = w._q1('SELECT COALESCE(SUM(v.qty), 0) AS n FROM receipt_void v JOIN receipt r ON r.id = v.receipt_id '
                            'WHERE r.action_id = ?', (older,))['n']
    assert voided_on_older == 0          # the merge took its unit from the newer save
    try:
        w.undo(1, older)
    except WmsError as e:
        pytest.fail(f'Undo of 2 extras the merge never touched was refused: {e}')
    # The row keeps its 1 found unit, and that unit is still in the box.
    assert au.item(row)['accounted'] == 1 and _in_box(w, s, b['A1']) == 1


# ---------------------------------------------------------------- a real server, two people
import importlib  # noqa: E402
import json  # noqa: E402
import threading  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402


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


def test_edit_form_of_kept_item_loaded_before_a_merge_wipes_the_merged_barcodes(srv):
    """Alex opens item T's Edit form. Sam merges S (barcode 4006381333931, 1 unit in a box) into T: the barcode now sits
    on T, so the next scan of it opens T. Merge.run only bumps product.updated_at, not edited_at, and the product save's
    stale check (server._save_product) compares edited_at. Alex's save from the older form (title fix, T's codes as
    loaded) goes through with 200 and save_product rewrites T's codes from the form: S's barcode is gone from every
    item. The next scan of it is an unknown code and the new item card makes a third copy of the item."""
    server, call = srv
    w = server.wms
    box = w.create_boxes(1, 1)[0]
    s = w.save_product({'sku': 'S-1', 'title': 'Mug (scanned)', 'identifiers': [{'kind': 'EAN', 'value': '4006381333931'}]})['id']
    t = w.save_product({'sku': 'T-1', 'title': 'Mug', 'identifiers': [{'kind': 'UPC', 'value': '012345678905'}]})['id']
    w.receive(1, s, 1, 900, {'box_id': box['id']}, reference='box page')
    st, form = call('Alex', f'/api/products/{t}')                  # Alex's Edit form loads
    assert st == 200
    st, r = call('Sam', f'/api/products/{s}/merge', {'target': {'product_id': t}, 'seen': {'on_hand': 1}})
    assert st == 200, r
    assert w._q1("SELECT product_id FROM product_identifier WHERE kind = 'EAN' AND value = '4006381333931'")['product_id'] == t
    body = {'sku': form['sku'], 'title': 'Mug, white', 'tracking': form['tracking'], 'owner_id': form['owner_id'],
            'identifiers': [{'kind': i['kind'], 'value': i['value']} for i in form['identifiers']],
            'edited_at': form['edited_at']}
    st, r = call('Alex', f'/api/products/{t}', body)
    on_t = w._q1("SELECT COUNT(*) AS n FROM product_identifier WHERE kind = 'EAN' AND value = '4006381333931'")['n']
    assert st == 409 or on_t == 1, f'stale edit saved ({st}); the merged barcode is now on {on_t} items'
