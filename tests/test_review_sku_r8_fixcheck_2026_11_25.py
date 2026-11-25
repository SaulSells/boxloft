import os, sys
REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))
import pytest
from test_amazon_approval import srv  # noqa: F401

BIG = '9' * 20


def _setup(server):
    box = server.wms.create_boxes(1, 1, name='A31')[0]
    bid = box['id'] if isinstance(box, dict) else box
    pid = server.wms.save_product({'sku': 'NOASIN', 'title': 'No asin item'})['id']
    return bid, pid


def _moves(server):
    return server.wms._q1('SELECT COUNT(*) n FROM stock_move')['n']


@pytest.mark.parametrize('lid', ['-' + BIG, ' ' + BIG, '+' + BIG, '9_' + BIG, '٩' * 20])
def test_listing_id_string_int_accepts_skips_too_big_then_saves_and_500s(srv, lid):
    """too_big only flags ascii isdigit strings; Python int() also takes a sign, spaces, underscores and non-ascii
    digits. /api/put saves the unit, then _scan_asins binds the huge listing_id: OverflowError -> 500 (the 400
    handler is gone), over a committed save. The retry with a new scan_id doubles it (the bug the fix meant to end)."""
    server, call, calls, uid = srv
    bid, pid = _setup(server)
    n0 = _moves(server)
    st, r = call('/api/put', {'product_id': pid, 'box_id': bid, 'scan_id': 's-1', 'listing_id': lid})
    assert not (st != 200 and _moves(server) > n0), f'answered {st} {r} but {_moves(server) - n0} moves saved'


def test_qty_as_long_digit_string_is_a_500(srv):
    """A non-id key (qty, replaces, refund_cents, seen_left ...) as a 20-digit string passes too_big; _int() makes a
    number past 64 bits and SQLite raises OverflowError, which now answers 'Server error' (500) instead of a 400."""
    server, call, calls, uid = srv
    bid, pid = _setup(server)
    st, r = call('/api/put', {'product_id': pid, 'box_id': bid, 'scan_id': 's-2', 'replaces': BIG})
    assert st != 500, (st, r)


def test_query_before_signed_number_is_a_500(srv):
    """The query check has the same hole: History's ?before= (a key not ending in id) gets past too_big, _int()
    makes a number past 64 bits, and the read answers 500 instead of a 400 with words."""
    server, call, calls, uid = srv
    for q in ('before=' + BIG, 'before=-' + BIG):
        st, r = call('/api/actions?' + q, None)
        assert st != 500, (q, st, r)
