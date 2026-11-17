"""Alex 11-11 "We also need a way to delete an inventory / product". The ledger is append-only: a product that never
held stock is erased; one with history is archived, and only once nothing is on hand."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from test_cover_all import srv  # noqa: E402,F401  (fixture)


def test_a_product_made_by_mistake_is_erased_and_its_links_freed(srv):  # noqa: F811
    server, call = srv
    w = server.wms
    st, p = call('Sam', '/api/products', {'sku': 'DUP-1', 'title': 'Duplicate by mistake', 'owner_id': 1,
                                          'identifiers': [{'kind': 'UPC', 'value': '036000291452'}]})
    with w._tx() as c:
        c.execute("INSERT INTO purchase_order (source, external_id, imported_at) VALUES ('amazon', 'A-1', '2026-11-01')")
        po = c.execute('SELECT last_insert_rowid()').fetchone()[0]
        c.execute("INSERT INTO purchase_line (po_id, line_key, title, qty, unit_cost_cents, product_id) VALUES (?, 'a', 'X', 1, 100, ?)",
                  (po, p['id']))
    st, u = call('Sam', f"/api/products/{p['id']}/usage")
    assert u['can_delete'] and u['history'] == {} and u['pointers'] == {'order lines': 1}
    st, r = call('Sam', f"/api/products/{p['id']}/delete", {'seen': p['updated_at']})
    assert st == 200 and r['deleted'] == p['id']
    assert call('Sam', f"/api/products/{p['id']}")[0] == 400 or not w._q1('SELECT 1 AS x FROM product WHERE id = ?', (p['id'],))
    assert w._q1('SELECT product_id FROM purchase_line WHERE po_id = ?', (po,))['product_id'] is None   # free to link again
    assert w.lookup('036000291452') == []
    gone = w._q1('SELECT * FROM product_deleted')
    assert gone['sku'] == 'DUP-1' and '036000291452' in gone['data']
    # The SKU can be used again.
    assert call('Sam', '/api/products', {'sku': 'DUP-1', 'title': 'Right one', 'owner_id': 1})[0] == 200


def test_a_product_with_history_is_archived_only_when_nothing_is_on_hand(srv):  # noqa: F811
    server, call = srv
    w = server.wms
    st, p = call('Sam', '/api/products', {'sku': 'H-1', 'title': 'Has history', 'owner_id': 1})
    box = w.create_boxes(1, 1)[0]
    w.receive(1, p['id'], 2, 500, {'box_id': box['id']})
    st, r = call('Sam', f"/api/products/{p['id']}/delete", {})
    assert st == 400 and "can't be erased" in r['error'] and w._q1('SELECT 1 AS x FROM product WHERE id = ?', (p['id'],))
    st, r = call('Sam', f"/api/products/{p['id']}/archive", {'archived': True})
    assert st == 400 and '2 still on hand' in r['error']
    w.remove(1, {'box_id': box['id']}, [{'product_id': p['id'], 'qty': 2}], 'CUSTOMER')
    cur = call('Sam', f"/api/products/{p['id']}")[1]
    st, a = call('Sam', f"/api/products/{p['id']}/archive", {'archived': True, 'seen': cur['updated_at']})
    assert st == 200 and a['archived'] == 1
    assert all(x['id'] != p['id'] for x in call('Sam', '/api/products')[1]['products'])
    assert any(x['id'] == p['id'] for x in call('Sam', '/api/products?archived=1')[1]['products'])
    # Editing an archived product keeps it archived (the form never sends `archived`).
    st, e = call('Sam', f"/api/products/{p['id']}", {'sku': 'H-1', 'title': 'Has history, renamed', 'owner_id': 1})
    assert st == 200 and e['archived'] == 1
    # The history is untouched, and it comes back with one tap.
    assert w.check_integrity()['ok']
    st, b = call('Sam', f"/api/products/{p['id']}/archive", {'archived': False})
    assert st == 200 and b['archived'] == 0


def test_delete_from_an_old_screen_is_refused_with_who(srv):  # noqa: F811
    server, call = srv
    st, p = call('Sam', '/api/products', {'sku': 'S-1', 'title': 'Shared', 'owner_id': 1})
    __import__('time').sleep(1.1)   # updated_at is to the second
    st, _ = call('Alex', f"/api/products/{p['id']}", {'sku': 'S-1', 'title': 'Alex renamed it', 'owner_id': 1,
                                                       'updated_at': p['updated_at']})
    st, r = call('Sam', f"/api/products/{p['id']}/delete", {'seen': p['updated_at']})
    assert st == 409 and 'Alex changed this product' in r['error']
    assert server.wms._q1('SELECT title FROM product WHERE id = ?', (p['id'],))['title'] == 'Alex renamed it'
