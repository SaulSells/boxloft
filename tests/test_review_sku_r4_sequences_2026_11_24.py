"""Review 2026-11-24-232446-b5e2eb5e (fix check of the Copy SKU redesign): a path part that should be a row id but is
not a number ('undefined' from a page with no product_id) answered 500 'Server error'. The router now refuses it
with a 400 and words, for every route whose handler calls int() on the part (server.ID_PARAMS)."""
import os, sys
REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))
from test_amazon_approval import srv  # noqa: E402,F401


def test_c18_c30_non_numeric_pid_on_copy_sku_is_400_not_500(srv):
    """POST /api/products/undefined/sell-sku (a CopySku with no product_id) or /abc/: int(pid) raises ValueError
    in product_sell_sku, so the person gets 'Server error' 500 instead of a 400 with words."""
    server, call, calls, uid = srv
    for pid in ('undefined', 'abc'):
        st, r = call(f'/api/products/{pid}/sell-sku', {})
        assert st == 400, (pid, st, r)


def test_every_id_route_refuses_a_non_number_with_400(srv):
    server, call, calls, uid = srv
    for path, body in (('/api/products/undefined', None), ('/api/products/abc/listed', None),
                       ('/api/boxes/undefined', None), ('/api/boxes/%C2%B2', None), ('/api/boxes/x', {})):
        st, r = call(path, body)
        assert st == 400 and 'number' in r['error'], (path, st, r)
    st, r = call('/api/products/999999/sell-sku', {})   # a real number still reaches the handler
    assert st == 400 and 'number' not in r['error'], (st, r)
