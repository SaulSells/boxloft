"""Review 2026-11-24-235712-6e1a19d8: the OverflowError -> 400 handler answered after a save had committed. JSON numbers
past 64 bits are now refused before the route runs (server.too_big), so a 400 means nothing was written; strings and
query values past 64 bits read as no number (server._int)."""
import os, sys
REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))
from test_amazon_approval import srv  # noqa: F401

BIG = 99999999999999999999


def test_c13_put_saves_then_answers_400_number_too_big(srv):
    """POST /api/put commits the unit (sorting.put), then _with_amazon -> _scan_asins reads listing_id from the body
    after the save; a too-big listing_id raises OverflowError there, so the person sees a refusal ('too big, reload')
    for a save that was written. A rescan with a new scan_id adds a second unit."""
    server, call, calls, uid = srv
    box = server.wms.create_boxes(1, 1, name='A31')[0]
    bid = box['id'] if isinstance(box, dict) else box
    pid = server.wms.save_product({'sku': 'NOASIN', 'title': 'No asin item'})['id']   # no ASIN: _scan_asins runs
    before = server.wms._q1('SELECT COUNT(*) n FROM stock_move')['n']
    st, r = call('/api/put', {'product_id': pid, 'box_id': bid, 'scan_id': 's-1', 'listing_id': BIG})
    after = server.wms._q1('SELECT COUNT(*) n FROM stock_move')['n']
    assert not (st == 400 and after > before), \
        f'refused with {st} {r} but {after - before} stock moves were saved'


def test_c13b_put_new_makes_item_then_answers_400(srv):
    """Same class on the new item card: /api/put/new makes the item and puts 1 in, then _scan_asins reads item_id
    (not used by put_new) and overflows: a 400 'reload and try again' over a saved item + unit."""
    server, call, calls, uid = srv
    box = server.wms.create_boxes(1, 1, name='A31')[0]
    bid = box['id'] if isinstance(box, dict) else box
    n0 = server.wms._q1('SELECT COUNT(*) n FROM product')['n']
    st, r = call('/api/put/new', {'box_id': bid, 'scan_id': 'n-1', 'code': '', 'title': 'Hand made thing',
                                  'item_id': BIG})
    n1 = server.wms._q1('SELECT COUNT(*) n FROM product')['n']
    assert not (st == 400 and n1 > n0), f'refused with {st} {r} but {n1 - n0} items were made'


def test_too_big_numbers_refused_before_anything_is_written(srv):
    """A JSON number past 64 bits, anywhere in a body, is a 400 before the route runs and nothing is written. A
    string or query number past 64 bits reads as no number (_int), never a 500 (review 11-25), and a long digit
    tracking ID under a *_id key goes through (USPS 22 digits)."""
    server, call, calls, uid = srv
    import server as srv_mod
    box = server.wms.create_boxes(1, 1, name='A32')[0]
    bid = box['id'] if isinstance(box, dict) else box
    pid = server.wms.save_product({'sku': 'B1', 'title': 'Big test'})['id']
    n0 = server.wms._q1('SELECT COUNT(*) n FROM stock_move')['n']
    for path, body in (('/api/put', {'product_id': pid, 'box_id': bid, 'scan_id': 'b-1', 'listing_id': BIG}),
                       ('/api/put', {'product_id': pid, 'box_id': bid, 'scan_id': 'b-2', 'listing_id': 1e30}),
                       ('/api/move', {'lines': [{'product_id': BIG, 'qty': 1}], 'from': {'box_id': bid}, 'to': {'box_id': bid}})):
        st, r = call(path, body)
        assert st == 400 and 'too big' in r['error'], (path, st, r)
    assert server.wms._q1('SELECT COUNT(*) n FROM stock_move')['n'] == n0
    for path in (f'/api/purchases/open?product_id={BIG}', f'/api/actions?before=-{BIG}'):
        st, r = call(path, None)
        assert st != 500, (path, st, r)
    assert srv_mod._int(str(BIG)) is None and srv_mod._int('-' + str(BIG), 0) == 0 and srv_mod._int('12') == 12
    assert not srv_mod.too_big({'tracking_id': '9233400472350650946389', 'code': '9' * 34})
    assert srv_mod.too_big({'lines': [{'product_id': BIG}]}) and not srv_mod.too_big({'product_id': 2 ** 63 - 1})
