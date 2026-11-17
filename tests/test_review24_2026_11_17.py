import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

from merge import Merge  # noqa: E402
from test_review17_2026_11_17 import srv  # noqa: E402,F401  (real server, two signed-in people)

Merge.enabled = True
S_EAN = '4006381333931'


def test_edit_form_loaded_before_a_merge_undo_puts_the_source_barcode_on_both_items(srv):
    """Sam merges S (EAN 4006381333931, 1 unit) into T. Alex opens T's Edit form: it lists S's EAN, now T's. Sam taps
    Undo on the merge: the EAN goes back to S and S is unarchived. Merge.undo (_take_back) changes T's codes but never
    touches T's edited_at, so Alex's save from the form he loaded before the undo passes server._save_product's stale
    check with 200 and save_product writes S's EAN back onto T. The EAN now sits on 2 live items: a scan of it asks
    which one, and the merge Undo's "the two items are apart again" is not true."""
    server, call = srv
    w = server.wms
    box = w.create_boxes(1, 1)[0]
    s = w.save_product({'sku': 'S-1', 'title': 'Mug (scanned)', 'identifiers': [{'kind': 'EAN', 'value': S_EAN}]})['id']
    t = w.save_product({'sku': 'T-1', 'title': 'Mug', 'identifiers': [{'kind': 'UPC', 'value': '012345678905'}]})['id']
    w.receive(1, s, 1, 900, {'box_id': box['id']}, reference='box page')
    st, r = call('Sam', f'/api/products/{s}/merge', {'target': {'product_id': t}, 'seen': {'on_hand': 1}})
    assert st == 200, r
    st, form = call('Alex', f'/api/products/{t}')                  # Alex's Edit form loads (shows the merged EAN)
    assert st == 200 and any(i['value'] == S_EAN for i in form['identifiers'])
    st, u = call('Sam', f"/api/merges/{r['merge_id']}/undo", {})
    assert st == 200, u
    assert w._q1('SELECT product_id FROM product_identifier WHERE value = ?', (S_EAN,))['product_id'] == s
    body = {'sku': form['sku'], 'title': 'Mug, white', 'tracking': form['tracking'], 'owner_id': form['owner_id'],
            'identifiers': [{'kind': i['kind'], 'value': i['value']} for i in form['identifiers']],
            'edited_at': form['edited_at']}
    st, _ = call('Alex', f'/api/products/{t}', body)
    holders = [x['product_id'] for x in w._q('SELECT product_id FROM product_identifier WHERE value = ? ORDER BY product_id', (S_EAN,))]
    assert st == 409 or holders == [s], f'stale edit saved ({st}) after the merge Undo; the EAN is now on items {holders}'
