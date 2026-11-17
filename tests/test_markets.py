"""Alex 11-11: Amazon, eBay, Walmart and local stock. A market is the box's letter (A, E, W, L). Picking a market
offers its boxes in use and the box to start filling: the lowest empty one, or a new one numbered after the highest
box Boxloft has for that letter."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
sys.path.insert(0, os.path.dirname(__file__))

from sorting import market_of  # noqa: E402
from wms import WmsError  # noqa: E402
from test_sorting import setup  # noqa: E402


def test_market_of_codes():
    assert [market_of(c) for c in ('A12', 'e3', 'W1', 'L-7', 'B4', 'F3', 'ALEXSHOUSE', 'A1B')] == ['A', 'E', 'W', 'L', None, None, None, None]


def test_next_box_fills_empty_boxes_first_then_makes_the_next_number(tmp_path):
    w, au, so, cat, inbound, bins = setup(tmp_path)        # boxes A1-A3 exist, all empty
    pid = w.save_product({'sku': 'T-1', 'title': 'Thing', 'owner_id': 1})['id']
    w.receive(1, pid, 2, 500, bins['A1'])
    m = so.market_boxes('a')
    assert [b['code'] for b in m['boxes']] == ['A1'] and m['boxes'][0]['units'] == 2
    assert m['next_empty']['code'] == 'A2' and m['next_new'] == 'A4'
    b = so.next_box('A')
    assert b['code'] == 'A2' and b['new'] is False and b['market_name'] == 'Amazon'

    # Walmart has no boxes yet: W1 is made, and its label still has to print.
    b = so.next_box('W')
    assert b['code'] == 'W1' and b['new'] is True and b['market'] == 'W' and b['type'] == 'box'
    assert so.next_box('W')['code'] == 'W1'                   # still empty: the same box, nothing new made
    w.receive(1, pid, 1, 500, {'box_id': b['id']})
    assert so.next_box('W')['code'] == 'W2'                   # W1 holds stock: the next one
    assert so.market_boxes('E')['next_new'] == 'E1'           # other markets untouched
    # A handwritten box entered as it is relabelled (A189) sets the next new number.
    so.make_box('A189', 1)
    assert so.market_boxes('A')['next_new'] == 'A190'


def test_two_people_get_different_next_boxes(tmp_path):
    w, au, so, cat, inbound, bins = setup(tmp_path)
    a = so.next_box('A', user_id=1)
    b = so.next_box('A', user_id=2)
    assert a['code'] == 'A1' and b['code'] == 'A2'
    assert so.next_box('A', user_id=1)['code'] == 'A1'        # the same person gets their own box back


def test_retired_numbers_are_not_reused_and_bad_market_refused(tmp_path):
    w, au, so, cat, inbound, bins = setup(tmp_path)
    so.boxes('L1-L2', create=True)
    pid = w.save_product({'sku': 'T-2', 'title': 'Thing', 'owner_id': 1})['id']
    w.receive(1, pid, 1, 500, {'box_id': so.market_boxes('L')['next_empty']['id']})   # L1 used
    l2 = so.market_boxes('L')['next_empty']
    so.retire(l2['id'], 1)                                   # L2 retired (empty)
    b = so.next_box('L')
    assert b['code'] == 'L3' and b['new'] is True            # L2's number stays retired
    with pytest.raises(WmsError):
        so.market_boxes('Q')
