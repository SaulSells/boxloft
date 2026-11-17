import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from commerce import Commerce  # noqa: E402
from inbound import Inbound, old_blank_is_delivered, parse_status  # noqa: E402
from wms import Wms  # noqa: E402

SEEN = date(2026, 10, 30)  # a Friday


def test_parse_status_wording():
    p = lambda t: parse_status(t, SEEN)
    assert p('Arriving November 6') == {'state': 'shipped', 'from': '2026-11-06', 'to': '2026-11-06', 'delivered_on': None}
    assert p('Arriving Wednesday')['to'] == '2026-11-04'
    assert p('Arriving Friday')['to'] == '2026-10-30'
    assert p('Arriving tomorrow')['to'] == '2026-10-31'
    assert p('Now expected by November 4') == {'state': 'shipped', 'from': None, 'to': '2026-11-04', 'delivered_on': None}
    r = p('Arriving November 12 - November 24')
    assert (r['from'], r['to']) == ('2026-11-12', '2026-11-24')
    assert p('Now arriving November 9 - November 12')['from'] == '2026-11-09'
    assert p('Arriving January 3')['to'] == '2027-01-03'
    assert p('Delivered today')['delivered_on'] == '2026-10-30'
    assert p('Delivered October 3')['delivered_on'] == '2026-10-03'
    assert p('Delivered Tuesday')['delivered_on'] == '2026-10-27'
    assert p('Delivered December 20')['delivered_on'] == '2025-12-20'
    assert p('Not yet shipped')['state'] == 'ordered'
    assert p('Cancelled')['state'] == 'cancelled'
    assert p('Return complete')['state'] == 'returned'
    assert p('Out for delivery')['to'] == '2026-10-30'
    # Wordings seen on the first live sync, 2026-11-09
    assert p('Return received A refund will appear on your original payment method in 2-4 business days.')['state'] == 'returned'
    # Old orders: Amazon shows a blank status
    blank = parse_status('', SEEN)
    assert old_blank_is_delivered(blank, '', '2026-07-10', SEEN)['state'] == 'delivered'
    assert old_blank_is_delivered(blank, '', '2026-10-20', SEEN)['state'] == 'ordered'
    assert p('Arriving today 1:45 PM - 3:45 PM') == {'state': 'shipped', 'from': '2026-10-30', 'to': '2026-10-30', 'delivered_on': None}
    assert p('Now arriving today Previously expected November 4')['to'] == '2026-10-30'
    assert p('Arriving tomorrow 7 AM - 11 AM')['to'] == '2026-10-31'
    assert p('Arriving by November 24') == {'state': 'shipped', 'from': None, 'to': '2026-11-24', 'delivered_on': None}
    assert p('Estimated to arrive by November 12') == {'state': 'ordered', 'from': None, 'to': '2026-11-12', 'delivered_on': None}


def test_sync_upserts_and_tracks_changes(tmp_path):
    w = Wms(str(tmp_path / 'w.db'))
    Commerce(w)
    ib = Inbound(w)
    base = {'order_id': '112-5426135-6525665', 'ordered_at': 'October 30, 2026', 'seen_at': '2026-10-30'}
    r = ib.sync([{**base, 'status_text': 'Not yet shipped', 'items': [{'asin': 'B03DHUJF1J', 'title': 'Folgers'}]}])
    assert (r['new'], r['changed']) == (1, 1)
    ib.sync([{**base, 'status_text': 'Not yet shipped', 'from_detail': True, 'subtotal_cents': 20715, 'total_cents': 20715,
              'items': [{'asin': 'B03DHUJF1J', 'title': 'Folgers', 'price_cents': 6905, 'image': 'https://m.media-amazon.com/x.jpg'}] * 3}])
    a = ib.arriving()['orders'][0]
    assert (a['state'], a['lines'][0]['qty'], a['lines'][0]['unit_cost_cents']) == ('ordered', 3, 6905)
    r = ib.sync([{**base, 'status_text': 'Arriving Monday'}])
    assert r['changed'] == 1
    a = ib.arriving()['orders'][0]
    assert (a['state'], a['expected_to']) == ('shipped', '2026-11-02')
    assert len(ib.history(a['id'])) == 2
    assert ib.sync([{**base, 'status_text': 'Arriving Monday'}])['changed'] == 0
    ib.hide(a['id'], True)
    assert ib.arriving()['orders'] == []
    # The next order with the same ASIN is hidden by itself.
    ib.sync([{'order_id': '112-0000000-0000001', 'ordered_at': '2026-10-31', 'status_text': 'Arriving Monday',
              'items': [{'asin': 'B03DHUJF1J', 'title': 'Folgers'}]}])
    assert ib.arriving()['orders'] == []


def test_tax_and_shipping_spread_into_unit_cost(tmp_path):
    w = Wms(str(tmp_path / 'w.db'))
    Commerce(w)
    ib = Inbound(w)
    ib.sync([{'order_id': '112-1111111-1111111', 'ordered_at': '2026-10-01', 'status_text': 'Arriving tomorrow', 'from_detail': True,
              'subtotal_cents': 10000, 'total_cents': 10663,
              'items': [{'asin': 'B0AAAAAAAA', 'title': 'A', 'price_cents': 5000}, {'asin': 'B0AAAAAAAA', 'title': 'A', 'price_cents': 5000}]}])
    line = ib.arriving()['orders'][0]['lines'][0]
    assert (line['qty'], line['unit_cost_cents']) == (2, 5332)


def test_arriving_skips_csv_only_and_old_deliveries(tmp_path):
    w = Wms(str(tmp_path / 'w.db'))
    c = Commerce(w)
    ib = Inbound(w)
    c.import_purchases('Order ID,Date,Account Email,Address,Total,COG,Box Status,Shipment Status,Product ASIN,Product Title,Product Quantity\n'
                       '111-0000000-0000001,02/01/2025,a@x.com,,10,10,Delivered,,B0AAAAAAAA,Old thing,1\n')
    ib.sync([{'order_id': '112-0000000-0000002', 'ordered_at': '2025-01-02', 'status_text': 'Delivered January 5, 2025', 'from_detail': True,
              'subtotal_cents': 100, 'total_cents': 100, 'items': [{'asin': 'B0BBBBBBBB', 'title': 'Long gone', 'price_cents': 100}]}])
    assert ib.arriving()['orders'] == []


def test_sync_skips_order_number_lookalikes(tmp_path):
    w = Wms(str(tmp_path / 'w.db'))
    Commerce(w)
    ib = Inbound(w)
    r = ib.sync([{'order_id': '000-0000000-8675309', 'status_text': 'Arriving tomorrow', 'items': [{'title': 'x'}]},
                 {'order_id': '135-1680104-7989391', 'status_text': '', 'items': []}])
    assert r['new'] == 0
    assert not w._q("SELECT 1 FROM purchase_order")
