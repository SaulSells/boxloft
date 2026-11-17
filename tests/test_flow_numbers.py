"""Flow audit 11-12, findings 1-3: screens that showed different numbers for the same thing.

1. A Beyblade scan took the 10-06 order (12 left, delivered 10-26) and filled in 12, while Arriving's card for the
   same item said 4 from the 11-03 package. Scan, Receive and Arriving now share one rule (commerce.rank_for_receiving).
2. Home said "10 delivered, not scanned in" (orders) and Arriving "(12)" (package cards). One count: inbound.counts.
3. History said "into Box 8" for A36, named items by SKU, and "Received 1 x ... into CUSTOMER" for a sale.
"""
import os
import sys
from datetime import date

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, '..', 'backend'))

import inbound as inbound_mod  # noqa: E402
from commerce import Commerce  # noqa: E402
from inbound import Inbound  # noqa: E402
from returns import Returns  # noqa: E402
from wms import Wms  # noqa: E402

ASIN = 'B07P3W4R90'


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(inbound_mod, 'today_et', lambda: date(2026, 11, 12))
    w = Wms(str(tmp_path / 'w.db'))
    com = Commerce(w)
    ib = Inbound(w, Returns(w, com))
    return w, com, ib


def order(w, ext, ordered, qty, delivered=None, packages=None, seen=True, asin=ASIN):
    """An Amazon order read by the extension (seen) with one line; packages = [(qty, delivered_on)]."""
    with w._tx() as c:
        po = c.execute("""INSERT INTO purchase_order (source, external_id, ordered_at, imported_at, state, delivered_on,
                          last_seen_at, status_text) VALUES ('amazon', ?, ?, '2026-11-01', ?, ?, ?, ?)""",
                       (ext, ordered, 'delivered' if delivered else 'shipped', delivered, '2026-11-12' if seen else None,
                        f'Delivered {delivered}' if delivered else 'Arriving Friday')).lastrowid
        ln = c.execute("""INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents)
                          VALUES (?, ?, ?, 'Beyblade X Brace Triceratops Dual Pack', ?, 1500)""", (po, ext, asin, qty)).lastrowid
        for i, (n, d) in enumerate(packages or [], 1):
            c.execute("""INSERT INTO purchase_package (po_id, line_id, pkg, qty, state, delivered_on, seen_at)
                         VALUES (?, ?, ?, ?, 'delivered', ?, '2026-11-12')""", (po, ln, i, n, d))
    return ln


def product(w):
    return w.save_product({'sku': ASIN, 'title': 'Beyblade X Brace Triceratops Dual Pack',
                           'identifiers': [{'kind': 'ASIN', 'value': ASIN}]})['id']


def test_scan_and_arriving_pick_the_same_order_and_quantity(env):
    w, com, ib = env
    old = order(w, '112-8704568-9160222', '2026-10-06', 12, delivered='2026-10-26')   # 17 days ago: off Arriving
    new = order(w, '112-4200646-9512413', '2026-10-23', 4, delivered='2026-11-03')
    pid = product(w)
    ranked = com.receive_lines_for(pid)
    assert [ln['id'] for ln in ranked] == [new, old]
    first = ranked[0]
    assert (first['package_left'], first['delivered_on']) == (4, '2026-11-03')
    # Arriving shows the same line with the same units.
    card = [c for c in ib.arriving()['orders'] if c['bucket'] == 'delivered']
    assert [(ln['id'], ln['coming']) for c in card for ln in c['lines']] == [(new, 4)]
    # Before: oldest order first, all 12.
    assert com.open_lines_for(pid)[0]['id'] == old


def test_rank_order_recent_then_older_then_unread_then_on_the_way(env):
    w, com, _ = env
    way = order(w, 'W', '2026-11-10', 1)
    unread = order(w, 'U', '2026-01-02', 1, seen=False)
    older = order(w, 'O', '2026-09-01', 1, delivered='2026-09-05')
    recent = order(w, 'R', '2026-11-01', 1, delivered='2026-11-08')
    pid = product(w)
    assert [ln['id'] for ln in com.receive_lines_for(pid)] == [recent, older, unread, way]


def test_scan_of_unknown_code_lists_lines_in_the_same_order(env):
    w, com, _ = env
    old = order(w, 'A', '2026-10-06', 12, delivered='2026-10-26')
    new = order(w, 'B', '2026-10-23', 4, delivered='2026-11-03')
    ranked = com.rank_for_receiving(com.open_lines_for_code(ASIN))
    assert [ln['id'] for ln in ranked] == [new, old]


def test_home_and_arriving_count_packages_and_orders_the_same(env):
    w, com, ib = env
    order(w, 'S1', '2026-11-01', 2, delivered='2026-11-10', packages=[(1, '2026-11-08'), (1, '2026-11-10')])
    order(w, 'S2', '2026-11-02', 1, delivered='2026-11-09')
    arr = ib.arriving()
    cards = [c for c in arr['orders'] if c['bucket'] == 'delivered']
    assert len(cards) == 3                                     # the split order shows a card per delivery day
    assert arr['counts']['delivered'] == {'packages': 3, 'orders': 2}
    assert arr['counts']['delivered']['packages'] == len(cards)
    assert arr['counts']['delivered']['orders'] == len({c['id'] for c in cards})


def test_history_names_boxes_items_and_sales(env):
    w, com, _ = env
    pid = product(w)
    box = w.create_boxes(1, 1, name='A36')[0]
    unnamed = w.create_boxes(1, 1)[0]
    w.receive(1, pid, 2, 1500, {'box_id': box['id']})
    w.move(1, [{'product_id': pid, 'qty': 1}], {'box_id': box['id']}, {'box_id': unnamed['id']})
    w.receive(1, pid, 1, 1500, {'location_id': w._q1("SELECT id FROM location WHERE code = 'CUSTOMER'")['id']},
              allow_virtual=True)
    texts = [a['summary'] for a in w.list_actions(limit=5)]
    assert texts == ['Sold 1 x Beyblade X Brace Triceratops Dual Pack, never on a shelf',
                     f"Moved 1 unit A36 -> Box {unnamed['id']}",
                     'Received 2 x Beyblade X Brace Triceratops Dual Pack into A36']


def test_old_history_lines_are_reworded_on_read(env):
    w, com, _ = env
    product(w)
    box = w.create_boxes(1, 1, name='A36')[0]
    rows = [{'summary': f"Received 1 x {ASIN} into Box {box['id']}"}, {'summary': f'Received 1 x {ASIN} into CUSTOMER'},
            {'summary': f"Moved 10 items Box {box['id']} -> Box 999"}, {'summary': '1 out of RECV to CUSTOMER'}]
    assert [r['summary'] for r in w.pretty_actions(rows)] == [
        'Received 1 x Beyblade X Brace Triceratops Dual Pack into A36',
        'Sold 1 x Beyblade X Brace Triceratops Dual Pack, never on a shelf',
        'Moved 10 units A36 -> Box 999',
        '1 out of RECV to the customer']
