import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import sales_sync  # noqa: E402
from audit import Audit  # noqa: E402
from commerce import Commerce  # noqa: E402
from connections import Connections  # noqa: E402
from inbound import Inbound  # noqa: E402
from sales_sync import Sales, amazon_report_orders, report_windows  # noqa: E402
from wms import Wms, WmsError  # noqa: E402

COLS = ['amazon-order-id', 'merchant-order-id', 'purchase-date', 'last-updated-date', 'order-status', 'fulfillment-channel',
        'sales-channel', 'order-channel', 'ship-service-level', 'product-name', 'sku', 'asin', 'item-status', 'quantity',
        'currency', 'item-price', 'order-item-id']


def rep(*rows):
    lines = ['\t'.join(COLS)]
    for r in rows:
        d = dict(zip(['id', 'date', 'status', 'fc', 'sc', 'asin', 'istatus', 'qty', 'price', 'item'], r))
        lines.append('\t'.join([d['id'], '', d['date'], d['date'], d['status'], d['fc'], d['sc'], '', 'Standard', 'Thing',
                                'SKU-' + d['asin'], d['asin'], d['istatus'], d['qty'], 'USD', d['price'], d['item']]))
    return '\n'.join(lines)


REPORT = rep(
    ('111-1', '2026-09-28T15:00:00+00:00', 'Shipped - Delivered to Buyer', 'Merchant', 'Amazon.com', 'B0AUDIT001', 'Shipped', '2', '37.98', 'OI-1'),
    ('111-2', '2026-09-10T15:00:00+00:00', 'Shipped', 'Merchant', 'Amazon.com', 'B0AUDIT001', 'Shipped', '1', '19.00', 'OI-2'),
    ('111-3', '2025-03-02T15:00:00+00:00', 'Shipped', 'Amazon', 'Amazon.com', 'B0FBA00001', 'Shipped', '4', '440.0', 'OI-3'),
    ('111-4', '2026-10-01T15:00:00+00:00', 'Cancelled', 'Merchant', 'Amazon.com', 'B0AUDIT001', 'Cancelled', '0', '', 'OI-4'),
    ('111-5', '2026-10-02T15:00:00+00:00', 'Shipped', 'Amazon', 'Non-Amazon', 'B0AUDIT001', 'Unshipped', '13', '', 'OI-5'),
    ('111-6', '2026-10-03T15:00:00+00:00', 'Shipped', 'Merchant', 'Amazon.com', 'B0AUDIT001', 'Cancelled', '1', '20.00', 'OI-6'))


def test_report_parser():
    orders = {o['external_id']: o for o in amazon_report_orders('﻿' + REPORT)}
    assert '111-5' not in orders                      # FBA shipping another channel's sale
    o = orders['111-1']
    assert (o['state'], o['fulfilled_by'], o['ordered_at']) == ('shipped', 'me', '2026-09-28')
    assert o['lines'] == [{'line_key': 'OI-1', 'sku': 'SKU-B0AUDIT001', 'asin': 'B0AUDIT001', 'title': 'Thing', 'qty': 2,
                           'price_cents': 1899}]      # item-price is the line total
    assert orders['111-3']['fulfilled_by'] == 'amazon' and orders['111-3']['lines'][0]['price_cents'] == 11000
    assert orders['111-4']['state'] == 'cancelled' and orders['111-4']['lines'] == []
    assert orders['111-6']['lines'] == []             # the item was cancelled


def test_windows_cover_range_in_29_day_pieces():
    w = report_windows(datetime(2025, 2, 19, tzinfo=timezone.utc), datetime(2026, 9, 17, tzinfo=timezone.utc))
    assert len(w) == 20 and w[0][0] == '2025-02-19T00:00:00Z' and w[-1][1] == '2026-09-17T00:00:00Z'
    assert all(a < b for a, b in w) and all(w[i][1] == w[i + 1][0] for i in range(len(w) - 1))


def setup(tmp_path, monkeypatch):
    w = Wms(str(tmp_path / 'w.db'))
    com = Commerce(w)
    Inbound(w)
    s = Sales(w, com, Connections(str(tmp_path)))
    au = Audit(w, com)
    au.import_snapshot([{'asin': 'B0AUDIT001', 'title': 'Thing', 'expected': 5, 'avg_cost_cents': 1000, 'cost_cents': 5000,
                         'first_buy': '2026-01-01', 'last_buy': '2026-08-01', 'orders': []}],
                       'Bought 2024-11-10 on, unsold as of Amazon sales through 2026-09-16 (units.csv built 2026-10-02)')
    monkeypatch.setattr(sales_sync, 'sp_reports', lambda conns, specs, tok, progress=None, what='': [REPORT] + [''] * (len(specs) - 1))
    return w, s, au


def test_backfill_then_every_sale_comes_off(tmp_path, monkeypatch):
    """Alex 11-11 night: no sales date. Every backfilled Amazon sale nothing here shipped comes off the row (oldest buy
    first), so nothing is offered or recorded by hand any more. A sale an older save tied to the row reads the same,
    and undoing that save keeps the sale off the row (a person said it isn't this row's)."""
    w, s, au = setup(tmp_path, monkeypatch)
    with w._tx() as c:
        po = c.execute("""INSERT INTO purchase_order (source, external_id, account, ordered_at, imported_at)
                          VALUES ('amazon', '114-BUY', 'Acmebuyer', '2026-01-01', 'x')""").lastrowid
        c.execute("INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents) VALUES (?, 'k', 'B0AUDIT001', 'Thing', 5, 1000)", (po,))
    r = s.backfill_amazon('2025-02-19', '2026-11-01')
    assert (r['new_orders'], r['new_lines'], r['reports']) == (3, 3, 22)   # 111-4 and 111-6 have no live lines
    assert s.backfill_amazon('2025-02-19', '2026-11-01')['new_lines'] == 0  # same data again: nothing new
    assert w._q1('SELECT COUNT(*) n FROM action')['n'] == 0                 # never moves stock
    it = au.list()['items'][0]
    assert (it['bought'], it['amazon_sold'], it['expected'], it['accounted']) == (5, 3, 2, 0)   # 111-2 (9-10) and 111-1 (9-28)
    assert 'sold_after' not in it and not hasattr(au, 'record_amazon_sales') and not hasattr(au, 'sold_all')

    line = w._q1("SELECT l.id FROM sales_line l JOIN sales_order o ON o.id = l.order_id WHERE o.external_id = '111-1'")['id']
    with pytest.raises(WmsError):
        au.sold(it['id'], 3, '', 1, sale_line_id=line)                      # the order line has 2
    it = au.sold(it['id'], 2, '', 1, sale_line_id=line)                     # how saves were tied before 11-11 night
    assert (it['bought'], it['amazon_sold'], it['expected'], it['accounted'], it['sold']) == (5, 3, 2, 0, 0)
    assert w._q1('SELECT COUNT(*) n FROM sales_line')['n'] == 3             # no second sale record
    sl = w._q1('SELECT * FROM sales_line WHERE id = ?', (line,))
    assert sl['shipped_action_id'] and sl['product_id']
    out = w._q1("SELECT * FROM audit_outcome WHERE kind = 'sold'")
    assert (out['channel'], out['order_ref'], out['sold_on'], out['price_cents']) == ('amazon', '111-1', '2026-09-28', 1899)
    with pytest.raises(WmsError):
        au.sold(it['id'], 1, '', 1, sale_line_id=line)                      # already tied to stock
    assert w.check_integrity()['ok']

    it = au.undo_outcome(it['id'], out['id'], 1)
    assert w._q1('SELECT shipped_action_id FROM sales_line WHERE id = ?', (line,))['shipped_action_id'] is None
    assert w._q1("SELECT status FROM sales_order WHERE external_id = '111-1'")['status'] == 'shipped'   # still a real sale
    assert (it['amazon_sold'], it['expected'], it['sold']) == (1, 4, 0)    # the undo stands: 111-1 is not this row's
    assert w.check_integrity()['ok']
