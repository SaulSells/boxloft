"""Every way a box page line is made x every later action on it (Alex 11-16).

The Bratz Yasmin bug: each feature had its own tests (Undo takes back an item the card made; a pile spreads over
orders), and no test did one after the other. The count change ran the card's Undo, which archives the item, and the
save after it was refused with "That product is not in Boxloft". This file runs each later action on each kind of
line, so a new kind of line or a new action has to pass all of them. Add a maker or an action here when you add one.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from test_card_listings import HYDRO_UPC, with_listings  # noqa: E402
from test_put_away import kleenex_code, order  # noqa: E402
from test_sorting import UNLISTED_UPC, UPC, units_in  # noqa: E402
from wms import WmsError  # noqa: E402

NEW = '0850001234561'


def _audit(w, so, cat, boxes, lid):
    return so.put(boxes['A1'], 1, scan_id='m', code=UPC), UPC


def _order(w, so, cat, boxes, lid):
    kleenex_code(w)
    order(w, '112-ORD', 4)   # shipped, not delivered: an open line, on no audit row
    return so.put(boxes['A1'], 1, scan_id='m', code=UNLISTED_UPC), UNLISTED_UPC


def _move(w, so, cat, boxes, lid):
    p = w.save_product({'sku': 'MUG-1', 'title': 'Blue mug', 'default_cost_cents': 450,
                        'identifiers': [{'kind': 'UPC', 'value': '0712345678911'}]})
    w.receive(1, p['id'], 3, 450, boxes['A2'])
    return so.put(boxes['A1'], 1, scan_id='m', code='0712345678911'), '0712345678911'


def _extra(w, so, cat, boxes, lid):
    w.save_product({'sku': 'MUG-2', 'title': 'Red mug', 'default_cost_cents': 300,
                    'identifiers': [{'kind': 'UPC', 'value': '0712345678928'}]})
    return so.put(boxes['A1'], 1, scan_id='m', code='0712345678928'), '0712345678928'


def _card(w, so, cat, boxes, lid):
    cat.db[NEW] = {'asin': 'B0NEWITEM1', 'title': 'Lego Bonsai Tree 10281', 'brand': 'LEGO', 'image_url': None,
                   'upcs': [NEW], 'eans': []}
    return so.put_new(boxes['A1'], 1, scan_id='m', code=NEW), NEW


def _card_purchase(w, so, cat, boxes, lid):
    with w._tx() as c:
        w._add_column(c, 'purchase_line', 'store_item_id', 'TEXT')
    line = order(w, '112-CARD', 4, asin='')
    return so.put_new(boxes['A1'], 1, scan_id='m', line_id=line), ''


def _card_listing(w, so, cat, boxes, lid):
    return so.put_new(boxes['A1'], 1, scan_id='m', code=HYDRO_UPC, listing_id=lid['HF-9.21.25-7.45']), HYDRO_UPC


def _from_label(w, so, cat, boxes, lid):
    """Put from a carrier label's card (Alex 11-16): an order line, tied to its package (label_put)."""
    from labels import Labels
    from test_labels import order as lorder, package
    so.labels = Labels(w)
    po, (lego,) = lorder(w, '112-LBL', [('LEGO Bonsai Tree', 'B0LEGO0001', 4)])
    package(w, po, 'S1', '1ZI2QDR00374260031', 'UPS', [(lego, 4)])
    r = so.add(boxes['A1'], {'kind': 'order', 'line_id': lego, 'left': 4, 'label': 'order', 'sub': ''}, 1, 1, scan_id='m')
    so.labels.record_put(r['add_id'], '1ZI2QDR00374260031')
    return {**so._put_result(r, {'kind': 'order', 'line_id': lego, 'left': 4}, 1, []), 'others': []}, ''


MAKERS = {'label': (_from_label, 'order', False),'audit': (_audit, 'audit', False), 'order': (_order, 'order', False), 'move': (_move, 'move', False),
          'extra': (_extra, 'new', False), 'card': (_card, 'new', True), 'card_purchase': (_card_purchase, 'order', True),
          'card_listing': (_card_listing, 'new', True)}


@pytest.fixture(params=sorted(MAKERS))
def line(request, tmp_path):
    w, so, boxes, lid = with_listings(tmp_path)
    make, how, made = MAKERS[request.param]
    r, code = make(w, so, so.catalog, boxes, lid)
    assert (r['kind'], r['how'], bool(r.get('made'))) == ('added', how, made), request.param
    return {'w': w, 'so': so, 'box': boxes['A1'], 'r': r, 'code': code, 'made': made, 'pid': r['product_id'],
            'name': request.param}


def archived(w, pid):
    return w._q1('SELECT archived FROM product WHERE id = ?', (pid,))['archived'] == 1


def still_whole(x):
    """The item is there, its barcode still finds it, its purchase still points at it, and the ledger adds up."""
    w, pid = x['w'], x['pid']
    assert not archived(w, pid), f"{x['name']}: item archived"
    if x['code']:
        assert pid in [p['id'] for p in w.lookup(x['code']) if not p['archived']], f"{x['name']}: barcode lost"
    assert w.check_integrity()['ok']


def test_count_up_then_down(line):
    so, w = line['so'], line['w']
    q = so.put_qty(line['r']['add_id'], 2, 1, over_ok=True)
    assert q.get('kind') == 'added' and units_in(w, line['box'], line['pid']) == 2, line['name']
    still_whole(line)
    q2 = so.put_qty(q['add_id'], 1, 1)
    assert units_in(w, line['box'], line['pid']) == 1
    still_whole(line)
    if line['name'] == 'label':   # the line still knows its package: 3 of its 4 left to put away
        assert so.labels.match('1ZI2QDR00374260031')['left'] == 3
    # The line's Undo still does what the first line's Undo did: an item the card made goes with it.
    u = so.undo_add(q2['add_id'], 1)
    assert units_in(w, line['box'], line['pid']) == 0 and bool(u.get('unmade')) == line['made']


def test_change_to_another_choice(line):
    so, w, r = line['so'], line['w'], line['r']
    other = next((o for o in r['others'] if o['kind'] != r['how']), None)
    if not other:
        pytest.skip(f"{line['name']}: no other choice offered")
    c = so.put(line['box'], 1, scan_id='c', code=line['code'], product_id=line['pid'], option=other, qty=1,
               over_ok=True, replaces=r['add_id'])
    assert (c['kind'], c['how'], c['replaced']) == ('added', other['kind'], r['add_id']), line['name']
    assert units_in(w, line['box'], line['pid']) == 1
    still_whole(line)
    u = so.undo_add(c['add_id'], 1)
    assert units_in(w, line['box'], line['pid']) == 0 and bool(u.get('unmade')) == line['made']


def test_undo(line):
    so, w = line['so'], line['w']
    u = so.undo_add(line['r']['add_id'], 1)
    assert units_in(w, line['box'], line['pid']) == 0
    assert bool(u.get('unmade')) == line['made'] and archived(w, line['pid']) == line['made'], line['name']
    assert w.check_integrity()['ok']


def test_resend_adds_nothing(line):
    so, w, r = line['so'], line['w'], line['r']
    again = (so.put_new(line['box'], 1, scan_id='m', code=line['code']) if line['made']
             else so.put(line['box'], 1, scan_id='m', code=line['code']))
    assert again['add_id'] == r['add_id'] and units_in(w, line['box'], line['pid']) == 1, line['name']
    still_whole(line)


def test_cost_typed_on_the_line(line):
    """Alex 11-17: "When scanning an item in that isn't in the audit / linked via an order I have no way of adding a buy
    cost." Extra stock takes a cost typed on its line; a pile change keeps it; a Usual unit cost never overwrites it;
    blank puts it back to not entered. Lines on an order or an audit row take the order's cost: refused."""
    so, w, r = line['so'], line['w'], line['r']
    if r['how'] != 'new':
        assert 'cost_cents' not in r
        with pytest.raises(WmsError, match="order's"):
            so.set_cost(r['add_id'], 1299, 1)
        return
    assert 'cost_cents' in r
    s = so.set_cost(r['add_id'], 1299, 1)
    assert (s['cost_cents'], s['units'], s['avg_cost_cents']) == (1299, 1, 1299), line['name']
    assert so.line_cost(r['add_id']) == {'cost_cents': 1299, 'who': 1}
    q = so.put_qty(r['add_id'], 3, 1, over_ok=True)                    # a pile: all 3 at the typed cost
    assert q['cost_cents'] == 1299 and w.get_product(line['pid'])['avg_cost_cents'] == 1299, line['name']
    p = w.get_product(line['pid'])
    w.save_product({'sku': p['sku'], 'title': p['title'], 'default_cost_cents': 500,
                    'identifiers': [{'kind': i['kind'], 'value': i['value']} for i in p['identifiers']]}, line['pid'])
    assert so.line_cost(q['add_id'])['cost_cents'] == 1299              # the usual cost leaves a typed cost alone
    assert so.set_cost(q['add_id'], None, 1)['cost_cents'] is None      # blank: not entered
    p = w.get_product(line['pid'])
    w.save_product({'sku': p['sku'], 'title': p['title'], 'default_cost_cents': 600,
                    'identifiers': [{'kind': i['kind'], 'value': i['value']} for i in p['identifiers']]}, line['pid'])
    assert so.line_cost(q['add_id'])['cost_cents'] == 600               # not entered again: the usual cost fills it
    assert so.extras()['rows'][0]['unit_cost_cents'] == 600             # Stock audit's extras list shows it too
    still_whole(line)
    so.undo_add(q['add_id'], 1)
    with pytest.raises(WmsError, match='undone'):
        so.set_cost(q['add_id'], 100, 1)
    assert w.check_integrity()['ok']
