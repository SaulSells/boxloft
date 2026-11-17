"""Alex 11-13 (LEGO Alpine Lodge, product 573): Sam put 5 in a box with no cost, Alex typed $70.00 as the item's usual
cost, and Avg cost and Value stayed empty. Now a saved usual cost fills units that came in with no cost; a cost a
person typed at receiving is never touched."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from wms import Wms  # noqa: E402


@pytest.fixture
def w(tmp_path):
    return Wms(str(tmp_path / 'wms.db'))


def _setup(w):
    box = w.create_boxes(1, 1)[0]
    pid = w.save_product({'sku': 'LEGO-10325', 'title': 'LEGO Alpine Lodge'})['id']
    return pid, {'box_id': box['id']}


def _save(w, pid, cents, user=1):
    p = w.get_product(pid)
    return w.save_product({'sku': p['sku'], 'title': p['title'], 'default_cost_cents': cents}, pid, user)


def test_usual_cost_fills_units_saved_with_no_cost(w):
    pid, box = _setup(w)
    for _ in range(5):
        w.receive(1, pid, 1, None, box)                 # Sam's 5 scans: cost not entered
    assert w.get_product(pid)['avg_cost_cents'] is None
    p = _save(w, pid, 7000)
    assert p['cost_filled'] == {'receipts': 5, 'units': 5, 'cents': 7000}
    assert p['avg_cost_cents'] == 7000
    assert all(r['unit_cost_cents'] == 7000 for r in p['receipts'])
    assert w._q1('SELECT COUNT(*) AS n FROM cost_fill')['n'] == 5
    assert w.check_integrity()['ok']


def test_a_typed_cost_is_never_changed_and_later_edits_follow(w):
    pid, box = _setup(w)
    w.receive(1, pid, 2, 6500, box)                     # typed at receiving
    w.receive(1, pid, 3, None, box)                     # not entered
    p = _save(w, pid, 7000)
    assert p['cost_filled']['units'] == 3
    assert sorted(r['unit_cost_cents'] for r in p['receipts']) == [6500, 7000]
    assert p['avg_cost_cents'] == round((2 * 6500 + 3 * 7000) / 5)
    p = _save(w, pid, 7200)                             # the usual cost changes: the filled units follow
    assert p['cost_filled']['units'] == 3 and sorted(r['unit_cost_cents'] for r in p['receipts']) == [6500, 7200]
    assert _save(w, pid, 7200)['cost_filled']['receipts'] == 0   # same cost again: nothing to do
    p = _save(w, pid, None)                             # blanked: back to "not entered"
    assert p['cost_filled']['units'] == 3
    assert sorted((r['unit_cost_cents'] is None, r['unit_cost_cents']) for r in p['receipts']) == [(False, 6500), (True, None)]
    assert p['avg_cost_cents'] == 6500


def test_unit_tracked_items_get_the_cost_on_each_unit(w):
    box = w.create_boxes(1, 1)[0]
    pid = w.save_product({'sku': 'ONE', 'title': 'One-off', 'tracking': 'unit'})['id']
    w.receive(1, pid, 2, None, {'box_id': box['id']})
    w.save_product({'sku': 'ONE', 'title': 'One-off', 'tracking': 'unit', 'default_cost_cents': 1500}, pid, 1)
    assert [r['cost_cents'] for r in w._q('SELECT cost_cents FROM unit WHERE product_id = ?', (pid,))] == [1500, 1500]


def test_cost_fill_is_append_only(w):
    pid, box = _setup(w)
    w.receive(1, pid, 1, None, box)
    _save(w, pid, 100)
    import sqlite3
    with pytest.raises(sqlite3.DatabaseError):
        with w._tx() as c:
            c.execute('DELETE FROM cost_fill')
