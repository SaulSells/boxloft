"""A new item is a card, not a form (flow audit 11-12, redesign 2).

Before: a code nothing knew opened New product, a 15-input form 3 screens long, even when the catalog had filled it;
from Home it ended on the product page and receiving started over (4 to 7 steps). Now: one card (catalog photo and
name, the purchase it likely came on, a look-alike to add the barcode to), and one tap or box scan makes the item and
puts 1 in the box. One Undo takes back both.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from test_put_away import order  # noqa: E402
from test_sorting import setup, units_in  # noqa: E402
from wms import WmsError  # noqa: E402

NEW = '0850001234561'


def catalog_knows(cat, code=NEW, asin='B0NEWITEM1', title='Lego Bonsai Tree 10281'):
    cat.db[code] = {'asin': asin, 'title': title, 'brand': 'LEGO', 'image_url': None, 'upcs': [code], 'eans': []}


def test_unknown_code_on_a_box_page_is_one_card_and_one_save(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    catalog_knows(cat)
    assert so.add_check(boxes['A1'], NEW)['kind'] == 'new_product'
    card = so.new_card(NEW)
    assert (card['kind'], card['title'], card['brand'], card['asin']) == ('new_item', 'Lego Bonsai Tree 10281', 'LEGO', 'B0NEWITEM1')
    r = so.put_new(boxes['A1'], 1, scan_id='n1', code=NEW)
    assert (r['kind'], r['made'], r['qty'], r['here']) == ('added', True, 1, 1)
    p = w._q1('SELECT * FROM product WHERE id = ?', (r['product_id'],))
    # Made with the defaults nobody has to type: owner Boxloft, counted by quantity, cost not entered.
    assert (p['title'], p['sku'], p['owner_id'], p['tracking'], p['default_cost_cents']) == \
        ('Lego Bonsai Tree 10281', 'B0NEWITEM1', 1, 'qty', None)
    assert [x['id'] for x in w.lookup(NEW)] == [p['id']]   # the next scan finds it
    # The next scan of the same code is 1 more, no card.
    r2 = so.put(boxes['A1'], 1, scan_id='n2', code=NEW)
    assert (r2['kind'], r2['product_id'], r2['here']) == ('added', p['id'], 2)
    assert w.check_integrity()['ok']


def test_no_catalog_hit_takes_a_typed_name(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    card = so.new_card('0799999000013')
    assert card['title'] == '' and card['lines'] == []
    with pytest.raises(WmsError):
        so.put_new(boxes['A1'], 1, scan_id='x1', code='0799999000013')   # no name, no purchase: nothing made
    assert not w.lookup('0799999000013')
    r = so.put_new(boxes['A1'], 1, scan_id='x2', code='0799999000013', title='Blue glass vase')
    p = w._q1('SELECT title, sku FROM product WHERE id = ?', (r['product_id'],))
    assert p == {'title': 'Blue glass vase', 'sku': '0799999000013'} and units_in(w, boxes['A1']) == 1


def test_undo_takes_back_the_unit_and_the_item(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    catalog_knows(cat)
    r = so.put_new(boxes['A1'], 1, scan_id='u1', code=NEW)
    u = so.undo_add(r['add_id'], 1)
    assert u['unmade'] is True and units_in(w, boxes['A1']) == 0
    assert w._q1('SELECT archived FROM product WHERE id = ?', (r['product_id'],))['archived'] == 1
    assert not [p for p in w.lookup(NEW) if not p['archived']]
    assert so.add_check(boxes['A1'], NEW)['kind'] == 'new_product'   # a rescan makes it fresh
    r2 = so.put_new(boxes['A1'], 1, scan_id='u2', code=NEW)
    assert r2['made'] and r2['product_id'] != r['product_id'] and w.check_integrity()['ok']


def test_undo_keeps_an_item_that_was_used_since(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    catalog_knows(cat)
    r = so.put_new(boxes['A1'], 1, scan_id='k1', code=NEW)
    so.put(boxes['A2'], 1, scan_id='k2', code=NEW, option={'kind': 'new', 'product_id': r['product_id'], 'cost_cents': None})
    u = so.undo_add(r['add_id'], 1)
    assert u['unmade'] is False and units_in(w, boxes['A1']) == 0 and units_in(w, boxes['A2']) == 1
    assert w._q1('SELECT archived FROM product WHERE id = ?', (r['product_id'],))['archived'] == 0


def test_two_people_making_it_at_once_make_one_item(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    catalog_knows(cat)
    a = so.put_new(boxes['A1'], 1, scan_id='p1', code=NEW)
    b = so.put_new(boxes['A2'], 2, scan_id='p2', code=NEW)   # Sam's card was open before Alex saved
    assert a['made'] and not b.get('made') and a['product_id'] == b['product_id']
    assert w._q1('SELECT COUNT(*) AS n FROM product WHERE archived = 0 AND sku LIKE ?', ('B0NEWITEM1%',))['n'] == 1
    assert units_in(w, boxes['A1']) + units_in(w, boxes['A2']) == 2
    # A resent save (lost reply) adds nothing.
    c = so.put_new(boxes['A2'], 2, scan_id='p2', code=NEW)
    assert c['add_id'] == b['add_id'] and units_in(w, boxes['A2']) == 1


def test_picking_the_purchase_makes_the_item_from_it(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    with w._tx() as c:   # store_mail's column (Walmart item id); live has it
        w._add_column(c, 'purchase_line', 'store_item_id', 'TEXT')
    line = order(w, '112-NOBAR', 3, ordered='2026-11-01', delivered='2026-11-10', asin='', cost=1250)
    card = so.new_card('')   # no barcode: the delivered lines nobody scanned in
    assert line in [d['id'] for d in card['delivered']]
    r = so.put_new(boxes['A1'], 1, scan_id='l1', line_id=line)
    assert (r['kind'], r['how'], r['made']) == ('added', 'order', True)
    rc = w._q1('SELECT purchase_line_id, unit_cost_cents FROM receipt')
    assert rc == {'purchase_line_id': line, 'unit_cost_cents': 1250}
    assert w._q1('SELECT title FROM product WHERE id = ?', (r['product_id'],))['title'] == 'Kleenex'


def test_a_pile_on_a_new_item_keeps_the_item(tmp_path):
    """Alex 11-16 (Bratz Yasmin, made twice on live and archived both times): Sam made an item from the card, tapped
    the count and typed how many. put_qty undid the line with the card's Undo, which archives the item it made, so the
    re-save said "That product is not in Boxloft". Now the item stays, and the pile line's Undo still takes it back."""
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    catalog_knows(cat)
    r = so.put_new(boxes['A1'], 1, scan_id='q1', code=NEW)
    q = so.put_qty(r['add_id'], 6, 1)
    assert (q['kind'], q['qty'], q['here'], q['product_id'], q['made']) == ('added', 6, 6, r['product_id'], True)
    assert w._q1('SELECT archived FROM product WHERE id = ?', (r['product_id'],))['archived'] == 0
    assert [p['id'] for p in w.lookup(NEW)] == [r['product_id']]   # the barcode stayed on
    q2 = so.put_qty(q['add_id'], 4, 1)   # change it again
    assert (q2['qty'], q2['here']) == (4, 4) and units_in(w, boxes['A1']) == 4
    assert w.check_integrity()['ok']
    u = so.undo_add(q2['add_id'], 1)   # Undo on the pile: the unit and the item it made both go
    assert u['unmade'] is True and units_in(w, boxes['A1']) == 0
    assert w._q1('SELECT archived FROM product WHERE id = ?', (r['product_id'],))['archived'] == 1


def test_a_pile_on_a_new_item_from_its_purchase(tmp_path):
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    with w._tx() as c:
        w._add_column(c, 'purchase_line', 'store_item_id', 'TEXT')
    line = order(w, '112-PILE', 3, ordered='2026-11-01', delivered='2026-11-10', asin='', cost=1250)
    r = so.put_new(boxes['A1'], 1, scan_id='l1', line_id=line)
    q = so.put_qty(r['add_id'], 5, 1, over_ok=True)   # 3 bought: 3 on the order, 2 as extra
    assert (q['qty'], q['how']) == (3, 'order') and q['extra_line']['qty'] == 2
    assert units_in(w, boxes['A1']) == 5
    assert w._q1('SELECT archived FROM product WHERE id = ?', (r['product_id'],))['archived'] == 0
    assert w._q1('SELECT product_id FROM purchase_line WHERE id = ?', (line,))['product_id'] == r['product_id']
    assert w.check_integrity()['ok']


def test_change_on_a_new_item_keeps_the_item(tmp_path):
    """Alex 11-16: the box page's "Not this? Change" undid the first line, then saved again. On a line the new item card
    made (from its purchase), that Undo archived the item and let go of the purchase, so the new save failed. Now one
    request (replaces) takes the units out and saves the new choice; the item and its purchase stay."""
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    with w._tx() as c:
        w._add_column(c, 'purchase_line', 'store_item_id', 'TEXT')
    line = order(w, '112-CHG', 3, ordered='2026-11-01', delivered='2026-11-10', asin='', cost=1250)
    r = so.put_new(boxes['A1'], 1, scan_id='c1', line_id=line)
    new = next(o for o in r['others'] if o['kind'] == 'new')
    q = so.put(boxes['A1'], 1, scan_id='c2', product_id=r['product_id'], option=new, qty=2, replaces=r['add_id'])
    assert (q['kind'], q['how'], q['qty'], q['here'], q['made'], q['replaced']) == ('added', 'new', 2, 2, True, r['add_id'])
    assert w._q1('SELECT archived FROM product WHERE id = ?', (r['product_id'],))['archived'] == 0
    assert w._q1('SELECT product_id FROM purchase_line WHERE id = ?', (line,))['product_id'] == r['product_id']
    assert so.put(boxes['A1'], 1, scan_id='c2', product_id=r['product_id'], option=new, qty=2,
                  replaces=r['add_id'])['add_id'] == q['add_id']   # a resend adds nothing
    assert units_in(w, boxes['A1']) == 2 and w.check_integrity()['ok']
    # Past what was bought: the question comes first and nothing changes, the first line included.
    ask = so.put(boxes['A1'], 1, scan_id='c3', product_id=r['product_id'], option=new, qty=5, replaces=q['add_id'])
    assert ask['kind'] == 'over' and units_in(w, boxes['A1']) == 2
    with pytest.raises(WmsError):   # another box's line
        so.put(boxes['A2'], 1, scan_id='c4', product_id=r['product_id'], option=new, qty=1, replaces=q['add_id'])
    assert so.undo_add(q['add_id'], 1)['unmade'] is True   # its Undo still takes the item back
