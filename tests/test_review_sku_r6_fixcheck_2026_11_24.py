"""Review 2026-11-24-234650-f7e8aa6a: numbers past 64 bits in a query or body gave 500s (now a 400 for every route),
and erased items' ids came back for new items, which inherited their listing_link and ebay_made rows. A new item
now never takes an erased id, so the two id-reuse proofs assert the id differs instead of c == a."""
import os, sys
REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))
from test_amazon_approval import srv  # noqa: F401

BIG = 99999999999999999999


def test_c23_query_id_past_int64_is_not_500(srv):
    """Router fix caps path ids at 18 digits, but query ids go through _int() straight into SQLite."""
    server, call, calls, uid = srv
    st, r = call(f'/api/purchases/open?product_id={BIG}', None)
    assert st != 500, (st, r)


def test_c23_body_id_past_int64_is_not_500(srv):
    """Same class in a JSON body: /api/move with a 20-digit product_id."""
    server, call, calls, uid = srv
    st, r = call('/api/move', {'lines': [{'product_id': BIG, 'qty': 1}], 'from': {'box_id': BIG}, 'to': {'box_id': 1}})
    assert st != 500, (st, r)


def test_c23_put_body_id_past_int64_is_not_500(srv):
    server, call, calls, uid = srv
    st, r = call('/api/put', {'product_id': BIG, 'box_id': BIG, 'scan_id': 'x1'})
    assert st != 500, (st, r)
