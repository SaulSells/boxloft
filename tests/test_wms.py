import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from codes import make_code, parse_scan  # noqa: E402
from wms import Wms, WmsError  # noqa: E402

U = 1  # user id


@pytest.fixture
def w(tmp_path):
    return Wms(str(tmp_path / 'wms.db'))


def shelf(w, code='A-01-01'):
    return w.create_locations([code])[0]['id']


def product(w, sku='SKU1', tracking='qty', **kw):
    return w.save_product({'sku': sku, 'title': f'Title {sku}', 'tracking': tracking, **kw})['id']


def level(w, pid, box_id=0, location_id=0, unit_id=0):
    row = w._q1('SELECT qty FROM stock_level WHERE product_id=? AND unit_id=? AND box_id=? AND location_id=?',
                (pid, unit_id, box_id, location_id))
    return row['qty'] if row else 0


def assert_clean(w):
    r = w.check_integrity(record=False)
    assert r['ok'], r['problems']


# ---------------------------------------------------------------- codes

def test_codes_round_trip_and_reject_misreads():
    for kind in ('box', 'location', 'unit'):
        for i in (1, 42, 123, 9999999):
            code = make_code(kind, i)
            assert parse_scan(code) == {'kind': kind, 'id': i, 'code': code}
            assert parse_scan(f'{code[:2]}-{code[2:-1]}-{code[-1]}')['id'] == i
            bad = code[:-1] + str((int(code[-1]) + 1) % 10)
            assert parse_scan(bad)['kind'] == 'bad_check'
    # Retail barcodes are never ours.
    for value in ('012345678905', 'X001ABCDEF', 'B07XYZ1234', 'MY-SKU-1'):
        assert parse_scan(value)['kind'] == 'product'


def test_single_digit_typo_is_caught():
    code = make_code('box', 123)
    body = code[2:-1]
    for pos in range(len(body)):
        for d in '0123456789':
            if d == body[pos]:
                continue
            typo = code[:2] + body[:pos] + d + body[pos + 1:] + code[-1]
            assert parse_scan(typo)['kind'] == 'bad_check'


# ---------------------------------------------------------------- ledger

def test_receive_move_count_undo_keeps_ledger_clean(w):
    a = shelf(w)
    b1, b2 = [b['id'] for b in w.create_boxes(2, U, a)]
    p = product(w)
    w.receive(U, p, 10, 500, {'box_id': b1})
    w.receive(U, p, 10, 700, {'box_id': b1})
    assert level(w, p, box_id=b1) == 20
    assert w.get_product(p)['avg_cost_cents'] == 600
    mv = w.move(U, [{'product_id': p, 'qty': 5}], {'box_id': b1}, {'box_id': b2})
    assert (level(w, p, box_id=b1), level(w, p, box_id=b2)) == (15, 5)
    assert_clean(w)
    w.undo(U, mv['action_id'])
    assert (level(w, p, box_id=b1), level(w, p, box_id=b2)) == (20, 0)
    with pytest.raises(WmsError):
        w.undo(U, mv['action_id'])
    res = w.count(U, {'box_id': b1}, [{'product_id': p, 'qty': 18}])
    assert res['differences'] == [{'product_id': p, 'unit_id': 0, 'expected': 20, 'counted': 18}]
    assert level(w, p, box_id=b1) == 18
    assert_clean(w)


def test_cannot_take_more_than_is_there(w):
    a = shelf(w)
    b1, b2 = [b['id'] for b in w.create_boxes(2, U, a)]
    p = product(w)
    w.receive(U, p, 3, 100, {'box_id': b1})
    with pytest.raises(WmsError, match='Only 3'):
        w.move(U, [{'product_id': p, 'qty': 4}], {'box_id': b1}, {'box_id': b2})
    # The failed action left nothing behind.
    assert w._q1('SELECT COUNT(*) n FROM action')['n'] == 1
    assert_clean(w)


def test_short_move_names_the_box_and_what_is_there(w):
    a = shelf(w)
    b1, b2 = [b['id'] for b in w.create_boxes(2, U, a)]
    w._conn().execute("UPDATE box SET name = 'A8' WHERE id = ?", (b1,))
    w._conn().commit()
    p = product(w)
    w.receive(U, p, 3, 100, {'box_id': b1})
    with pytest.raises(WmsError, match='Only 3 of Title SKU1 in A8, cannot take 4'):
        w.move(U, [{'product_id': p, 'qty': 4}], {'box_id': b1}, {'box_id': b2})


def test_gather_puts_every_pile_in_one_box_with_one_undo(w):
    a = shelf(w)
    b1, b2, b3 = [b['id'] for b in w.create_boxes(3, U, a)]
    p, other = product(w), product(w, 'SKU2')
    w.receive(U, p, 3, 100, {'box_id': b1})
    w.receive(U, p, 2, 100, {'box_id': b2})
    w.receive(U, p, 4, 100, {'location_id': a})
    w.receive(U, other, 5, 100, {'box_id': b1})
    out = w.remove(U, {'box_id': b1}, [{'product_id': p, 'qty': 1}], 'CUSTOMER')   # a sold unit stays sold
    r = w.gather(U, p, {'box_id': b2})
    assert (r['moved'], r['places']) == (6, 2)
    assert (level(w, p, box_id=b1), level(w, p, box_id=b2), level(w, p, location_id=a)) == (0, 8, 0)
    assert level(w, other, box_id=b1) == 5          # other items in the same box stay
    assert w.get_product(p)['on_hand'] == 8
    assert_clean(w)
    with pytest.raises(WmsError, match='already in'):
        w.gather(U, p, {'box_id': b2})
    w.undo(U, r['action_id'])
    assert (level(w, p, box_id=b1), level(w, p, box_id=b2), level(w, p, location_id=a)) == (2, 2, 4)
    assert out['action_id'] and w.get_product(p)['on_hand'] == 8
    assert_clean(w)


def test_gather_moves_one_off_units_and_empty_box_as_target(w):
    a = shelf(w)
    b1, b2, b3 = [b['id'] for b in w.create_boxes(3, U, a)]
    p = product(w, tracking='unit')
    w.receive(U, p, 1, 100, {'box_id': b1})
    w.receive(U, p, 1, 100, {'box_id': b2})
    r = w.gather(U, p, {'box_id': b3})
    assert r['moved'] == 2
    assert sorted(x['box_id'] for x in w.get_product(p)['where']) == [b3, b3]
    assert_clean(w)


def test_undo_blocked_when_stock_already_moved_on(w):
    a = shelf(w)
    b1, b2, b3 = [b['id'] for b in w.create_boxes(3, U, a)]
    p = product(w)
    w.receive(U, p, 4, 100, {'box_id': b1})
    first = w.move(U, [{'product_id': p, 'qty': 4}], {'box_id': b1}, {'box_id': b2})
    w.move(U, [{'product_id': p, 'qty': 4}], {'box_id': b2}, {'box_id': b3})
    with pytest.raises(WmsError, match='Only 0'):
        w.undo(U, first['action_id'])
    assert_clean(w)


def test_ledger_is_append_only(w):
    a = shelf(w)
    b = w.create_boxes(1, U, a)[0]['id']
    p = product(w)
    w.receive(U, p, 1, 100, {'box_id': b})
    conn = w._conn()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute('UPDATE stock_move SET qty = 99')
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute('DELETE FROM stock_move')
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE action SET summary = 'edited'")


def test_units_move_alone_and_count_finds_strays(w):
    a = shelf(w)
    b1, b2 = [b['id'] for b in w.create_boxes(2, U, a)]
    p = product(w, 'ONEOFF', tracking='unit')
    r = w.receive(U, p, 3, 2500, {'box_id': b1})
    u1, u2, u3 = [x['id'] for x in r['units']]
    w.move(U, [{'product_id': p, 'unit_id': u1}], {'box_id': b1}, {'box_id': b2})
    assert w.get_unit(u1)['holder']['box_id'] == b2
    # Sam finds u2 in box 2 during a count of box 2: it moves there from box 1.
    w.count(U, {'box_id': b2}, [{'product_id': p, 'unit_id': u1, 'qty': 1}, {'product_id': p, 'unit_id': u2, 'qty': 1}])
    assert w.get_unit(u2)['holder']['box_id'] == b2
    assert w.get_product(p)['on_hand'] == 3
    w.remove(U, {'box_id': b1}, [{'product_id': p, 'unit_id': u3}], 'CUSTOMER')
    assert w.get_product(p)['on_hand'] == 2
    assert_clean(w)


def test_box_moves_carry_contents_and_undo(w):
    a, b_loc = shelf(w, 'A-01-01'), shelf(w, 'B-02-03')
    box = w.create_boxes(1, U, a)[0]['id']
    p = product(w)
    w.receive(U, p, 6, 100, {'box_id': box})
    act = w.move_boxes(U, [box], b_loc)
    assert w.get_box(box)['location_id'] == b_loc
    assert w.get_product(p)['where'][0]['location_code'] == 'B-02-03'
    w.undo(U, act['action_id'])
    assert w.get_box(box)['location_id'] == a
    assert_clean(w)


def test_undo_receive_drops_cost_from_average(w):
    a = shelf(w)
    b = w.create_boxes(1, U, a)[0]['id']
    p = product(w)
    w.receive(U, p, 1, 100, {'box_id': b})
    r = w.receive(U, p, 1, 900, {'box_id': b})
    assert w.get_product(p)['avg_cost_cents'] == 500
    w.undo(U, r['action_id'])
    assert w.get_product(p)['avg_cost_cents'] == 100
    assert_clean(w)


def test_lookup_by_identifier_and_upc_as_ean(w):
    p = product(w, identifiers=[{'kind': 'UPC', 'value': '012345678905'}, {'kind': 'FNSKU', 'value': 'X001ABCDEF'}])
    assert [x['id'] for x in w.lookup('X001ABCDEF')] == [p]
    assert [x['id'] for x in w.lookup('0012345678905')] == [p]
    assert [x['id'] for x in w.lookup('sku1')] == [p]


def test_integrity_catches_a_tampered_cache(w):
    a = shelf(w)
    b = w.create_boxes(1, U, a)[0]['id']
    p = product(w)
    w.receive(U, p, 5, 100, {'box_id': b})
    w._conn().execute('UPDATE stock_level SET qty = 7 WHERE box_id = ?', (b,))
    r = w.check_integrity(record=False)
    assert not r['ok']
    assert {x['type'] for x in r['problems']} >= {'cache_mismatch', 'nonzero_sum'}
