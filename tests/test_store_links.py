"""Arriving links each item to the store's own product page when the store gave an ID (Alex 11-13: "Should also have a
link to the retailers product page if we know it")."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from commerce import Commerce  # noqa: E402
from inbound import Inbound, store_url  # noqa: E402
from wms import Wms  # noqa: E402


def test_store_url_only_from_ids_the_store_gave():
    assert store_url('amazon', 'b0xcfza6he') == 'https://www.amazon.com/dp/B0XCFZA6HE'
    assert store_url('amazon', '0316769487') == 'https://www.amazon.com/dp/0316769487'      # a book's ASIN is its ISBN-10
    assert store_url('walmart.com', '', '280663824') == 'https://www.walmart.com/ip/280663824'
    assert store_url('walmart.com', '', '') == ''                                          # not read off the order page yet
    assert store_url('theboxery.com', '', '') == '' and store_url('amazon', '') == ''
    assert store_url('amazon', 'B0XCFZA6HE/../x') == ''


def test_arriving_lines_carry_their_link(tmp_path):
    w = Wms(str(tmp_path / 'w.db'))
    Commerce(w)
    ib = Inbound(w)
    ib.sync([{'order_id': '112-7269728-2303005', 'ordered_at': '2026-10-07', 'status_text': 'Arriving today', 'from_detail': True,
              'subtotal_cents': 4245, 'total_cents': 4245,
              'items': [{'asin': 'B0XCFZA6HE', 'title': 'NECA Gargoyles Ultimate Goliath', 'price_cents': 4245}]}])
    line = ib.arriving()['orders'][0]['lines'][0]
    assert line['url'] == 'https://www.amazon.com/dp/B0XCFZA6HE'
    assert 'source' not in line and 'store_item_id' not in line
