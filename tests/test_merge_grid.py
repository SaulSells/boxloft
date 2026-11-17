"""Merge items, every kind of source against every kind of target (Alex 11-17: "The new merge feature needs clear
testing given the bugs we have found after implementing previous features").

Each source builder knows by construction how its units came in (extra stock, a buy line, the Stock audit), so the
expected numbers here come from the builder, not from merge.py. Checks on every pair: the preview says what the save
does, ledger integrity, units per box kept, nothing found or received twice, codes open the kept item, a stale screen
gets 409, the source's old receive can't be undone under the merge, Undo restores every number exactly, and a second
merge after Undo works the same. Pairs that must be refused are refused with nothing saved."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from merge import Merge  # noqa: E402
from test_ship_unscanned import sale  # noqa: E402
from test_sorting import ASIN, setup  # noqa: E402
from wms import LINE_LEFT_SQL, Conflict, WmsError, now_utc  # noqa: E402

Merge.enabled = True
S_EAN = '4006381333931'


def po(w, source, ext, qty, cost, product_id=None, asin=''):
    with w._tx() as c:
        o = c.execute('INSERT INTO purchase_order (source, external_id, ordered_at, imported_at) VALUES (?,?,?,?)',
                      (source, ext, '2026-10-01', 'x')).lastrowid
        return c.execute('INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents, product_id) VALUES (?,?,?,?,?,?,?)',
                         (o, ext, asin, 'Toploader', qty, cost, product_id)).lastrowid


def row_of(w):
    return w._q1('SELECT id FROM audit_item WHERE asin = ?', (ASIN,))['id']


# ---------------------------------------------------------------- sources: (S id, truth)
# truth: rebook = extras on hand a merge may re-book; old = an action that received extras as S.

def src_extras(w, au, b):
    s = w.save_product({'sku': 'S-1', 'title': 'Toploader (scanned)', 'identifiers': [
        {'kind': 'ASIN', 'value': 'B0OTHER001'}, {'kind': 'EAN', 'value': S_EAN}]})['id']
    old = w.receive(1, s, 3, 900, b['A1'], reference='box page')['action_id']
    return s, {'rebook': 3, 'old': old}


def src_two_boxes(w, au, b):
    s = w.save_product({'sku': 'S-1', 'title': 'Toploader (scanned)', 'identifiers': [{'kind': 'EAN', 'value': S_EAN}]})['id']
    old = w.receive(1, s, 2, 900, b['A1'])['action_id']
    w.receive(1, s, 1, 700, b['A2'])
    return s, {'rebook': 3, 'old': old}


def src_bought_line(w, au, b):
    s = w.save_product({'sku': 'S-1', 'title': 'Toploader (store)', 'identifiers': [{'kind': 'EAN', 'value': S_EAN}]})['id']
    ln = po(w, 'target.com', 'T9', 5, 700, s)
    old = w.receive(1, s, 2, 700, b['A1'], purchase_line_id=ln)['action_id']
    return s, {'rebook': 0, 'old': old, 'counted': True}


def src_found_own_row(w, au, b):
    row = row_of(w)
    au.found(row, 2, b['A1'], 1)                      # makes S with the row's ASIN; 1 still to find
    s = au.item(row)['product_id']
    with w._tx() as c:
        c.execute("INSERT INTO product_identifier (product_id, kind, value) VALUES (?, 'EAN', ?)", (s, S_EAN))
    return s, {'rebook': 0, 'old': None, 'counted': True}


def src_mixed_shipped(w, au, b):
    """2 on a buy line + 2 extras, then 1 shipped: which kind left is unknown, so only 1 extra may be re-booked."""
    s = w.save_product({'sku': 'S-1', 'title': 'Toploader (mixed)', 'identifiers': [{'kind': 'EAN', 'value': S_EAN}]})['id']
    ln = po(w, 'target.com', 'T9', 2, 700, s)
    w.receive(1, s, 2, 700, b['A1'], purchase_line_id=ln)
    old = w.receive(1, s, 2, 900, b['A1'])['action_id']
    w.remove(1, b['A1'], [{'product_id': s, 'qty': 1}], 'CUSTOMER')
    return s, {'rebook': 1, 'old': None}


def src_open_sale(w, au, b):
    s = w.save_product({'sku': 'S-1', 'title': 'Toploader (listed)', 'identifiers': [
        {'kind': 'ASIN', 'value': 'B0OTHER001'}, {'kind': 'EAN', 'value': S_EAN}]})['id']
    old = w.receive(1, s, 2, 900, b['A1'])['action_id']
    sale(w, '113-S', asin='B0OTHER001')
    with w._tx() as c:
        c.execute("""INSERT INTO listing (channel, sku, asin, title, status, qty, product_id, first_seen, seen_at)
                     VALUES ('amazon', 'S-SKU', 'B0OTHER001', 't', 'Active', 2, ?, ?, ?)""", (s, now_utc(), now_utc()))
    return s, {'rebook': 2, 'old': old}


def src_ebay_made(w, au, b):
    s = w.save_product({'sku': 'EBAY-111', 'title': 'Toploader (eBay listing)', 'identifiers': [
        {'kind': 'EBAY_ITEM', 'value': 'ITEM-111'}, {'kind': 'EAN', 'value': S_EAN}]})['id']
    old = w.receive(1, s, 2, None, b['A1'])['action_id']        # cost not entered
    return s, {'rebook': 2, 'old': old}


def src_zero(w, au, b):
    s = w.save_product({'sku': 'S-1', 'title': 'Toploader (none here)', 'identifiers': [{'kind': 'EAN', 'value': S_EAN}]})['id']
    return s, {'rebook': 0, 'old': None}


SOURCES = {'extras': src_extras, 'two_boxes': src_two_boxes, 'bought_line': src_bought_line,
           'found_own_row': src_found_own_row, 'mixed_shipped': src_mixed_shipped, 'open_sale': src_open_sale,
           'ebay_made': src_ebay_made, 'zero': src_zero}


# ---------------------------------------------------------------- targets: (target dict, T id or None, caps)
# caps: row = units the row can take, lines = units T's own buy lines take after the row.

def tgt_plain(w, au, b):
    t = w.save_product({'sku': 'T-1', 'title': 'Toploader 10ct', 'identifiers': [{'kind': 'UPC', 'value': '036000291452'}]})['id']
    w.receive(1, t, 1, 800, b['A2'])
    return {'product_id': t}, t, {'row': 0, 'lines': 0}


def tgt_row_item(w, au, b):
    t = w.save_product({'sku': 'T-1', 'title': 'Toploader 10ct', 'identifiers': [{'kind': 'ASIN', 'value': ASIN}]})['id']
    return {'product_id': t}, t, {'row': 3, 'lines': 0}


def tgt_row_partial(w, au, b):
    t = w.save_product({'sku': 'T-1', 'title': 'Toploader 10ct', 'identifiers': [{'kind': 'ASIN', 'value': ASIN}]})['id']
    au.found(row_of(w), 2, b['A3'], 1)
    return {'product_id': t}, t, {'row': 1, 'lines': 0}


def tgt_lines(w, au, b):
    t = w.save_product({'sku': 'T-1', 'title': 'Toploader 10ct'})['id']
    po(w, 'walmart.com', 'W1', 4, 650, t)
    return {'product_id': t}, t, {'row': 0, 'lines': 4}


def tgt_row_no_item(w, au, b):
    return {'audit_id': row_of(w)}, None, {'row': 3, 'lines': 0}


TARGETS = {'plain': tgt_plain, 'row_item': tgt_row_item, 'row_partial': tgt_row_partial, 'lines': tgt_lines,
           'row_no_item': tgt_row_no_item}


# ---------------------------------------------------------------- what the world looks like

def snapshot(w, au, pids):
    q = ','.join('?' * len(pids))
    return {
        'stock': w._q(f'SELECT product_id, box_id, location_id, qty FROM stock_level WHERE product_id IN ({q}) AND qty != 0 ORDER BY 1,2,3', pids),
        'idents': w._q(f'SELECT product_id, kind, value FROM product_identifier WHERE product_id IN ({q}) ORDER BY 1,2,3', pids),
        'sales': w._q('SELECT id, product_id FROM sales_line ORDER BY id'),
        'listings': w._q('SELECT id, product_id FROM listing ORDER BY id'),
        'lines': w._q(f'SELECT l.id, l.product_id, {LINE_LEFT_SQL} AS left_ FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id ORDER BY l.id'),
        'archived': w._q(f'SELECT id, archived FROM product WHERE id IN ({q}) ORDER BY id', pids),
        'rows': [(r['id'], r['accounted'], r['expected'], r['product_id']) for r in au._rows()],
        'cost': {p: w.get_product(p)['avg_cost_cents'] for p in pids},
    }


def per_box(w, pids):
    q = ','.join('?' * len(pids))
    return {(r['box_id'], r['location_id']): r['n'] for r in w._q(
        f"""SELECT s.box_id, s.location_id, SUM(s.qty) AS n FROM stock_level s LEFT JOIN location l ON l.id = s.location_id
             WHERE s.product_id IN ({q}) AND (s.box_id != 0 OR l.kind != 'virtual') GROUP BY 1, 2 HAVING n != 0""", pids)}


def accounted(au, row):
    return au.item(row)['accounted']


PAIRS = [(s, t) for s in SOURCES for t in TARGETS]


@pytest.mark.parametrize('src_kind,tgt_kind', PAIRS)
def test_merge_grid(tmp_path, src_kind, tgt_kind):
    w, au, so, cat, inbound, b = setup(tmp_path, expected=3)
    m = Merge(w, au.commerce, au)
    s, truth = SOURCES[src_kind](w, au, b)
    target, t, cap = TARGETS[tgt_kind](w, au, b)
    row = row_of(w)
    pids = [s] + ([t] if t else [])

    same = t is None
    if same:
        pids = [s]
    on_hand = sum(r['qty'] for r in m._places(s))
    to_row = min(truth['rebook'], cap['row'])
    to_lines = min(truth['rebook'] - to_row, cap['lines'])
    before = snapshot(w, au, pids)
    boxes_before = per_box(w, pids)
    acc_before = accounted(au, row)

    pre = m.preview(s, target)
    assert (pre['to_row'], pre['to_lines']) == (to_row, to_lines), pre
    assert pre['to_row'] + pre['to_lines'] + pre['extra'] + pre['as_is'] == on_hand

    with pytest.raises(Conflict):                                       # a stale screen saves nothing
        m.run(s, target, 1, seen={'on_hand': on_hand + 1})
    assert snapshot(w, au, pids) == before

    r = m.run(s, target, 1, seen={'on_hand': on_hand})
    assert w.check_integrity(record=False)['ok']
    assert per_box(w, pids) == boxes_before                             # every unit stays where it is
    assert accounted(au, row) - acc_before == to_row                    # found once, never twice
    assert all(x['left_'] >= 0 for x in snapshot(w, au, pids)['lines'])
    if tgt_kind == 'lines':
        wl = w._q1(f"SELECT {LINE_LEFT_SQL} AS n FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id WHERE o.external_id = 'W1'")['n']
        assert wl == 4 - to_lines
    if src_kind == 'bought_line':                                       # its buy line still waits for 3, not 1
        assert w._q1(f"SELECT {LINE_LEFT_SQL} AS n FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id WHERE o.external_id = 'T9'")['n'] == 3
    if same:
        assert w.get_product(s)['on_hand'] == on_hand and au.item(row)['product_id'] == s
    else:
        assert w.get_product(s)['on_hand'] == 0 and w.get_product(s)['archived'] == 1
        assert [p['id'] for p in w.lookup(S_EAN) if not p['archived']] == [t]
        with pytest.raises(WmsError):
            m.run(s, target, 1)                                         # archived now: no second merge
    keeper = t or s
    cost = w.get_product(keeper)['avg_cost_cents']
    if src_kind in ('extras', 'open_sale') and same:
        assert cost == 500                                              # the old extra receipt left Avg cost
    if cost is not None:
        assert 500 <= cost <= 900
    if truth['old'] and (not same or truth['rebook']):                  # the box page's Undo of the old add
        with pytest.raises(WmsError):
            w.undo(1, truth['old'])
        assert w.check_integrity(record=False)['ok']

    m.undo(r['merge_id'], 1)
    assert w.check_integrity(record=False)['ok']
    assert snapshot(w, au, pids) == before                              # Undo puts every number back

    r2 = m.run(s, target, 1, seen={'on_hand': on_hand})                 # and the same merge again gives the same result
    assert accounted(au, row) - acc_before == to_row and per_box(w, pids) == boxes_before
    m.undo(r2['merge_id'], 1)
    assert snapshot(w, au, pids) == before


# ---------------------------------------------------------------- life after a merge

def test_sale_moved_by_merge_auto_ships_from_the_kept_item(tmp_path):
    """An open Amazon sale of S follows it to T; the marketplace ships it; auto_ship takes T's unit. Undo of the merge
    then can't take back units that left: refused, nothing changed."""
    w, au, so, cat, inbound, b = setup(tmp_path)
    m = Merge(w, au.commerce, au)
    s, _ = src_open_sale(w, au, b)
    t = w.save_product({'sku': 'T-1', 'title': 'Toploader 10ct'})['id']
    r = m.run(s, {'product_id': t}, 1)
    with w._tx() as c:
        c.execute("UPDATE sales_order SET fulfilled_by = 'me', review = ? WHERE external_id = '113-S'", (au.commerce.MARKED_SHIPPED,))
    out = au.commerce.auto_ship()
    assert out['lines'] == 1
    shipped = w._q1("""SELECT m.product_id FROM sales_line l JOIN stock_move m ON m.action_id = l.shipped_action_id
                        JOIN sales_order o ON o.id = l.order_id WHERE o.external_id = '113-S'""")
    assert shipped['product_id'] == t and w.get_product(t)['on_hand'] == 1
    before = snapshot(w, au, [s, t])
    with pytest.raises(WmsError):
        m.undo(r['merge_id'], 1)
    assert snapshot(w, au, [s, t]) == before and w.check_integrity(record=False)['ok']
    assert not w._q1('SELECT undone_at FROM product_merge WHERE id = ?', (r['merge_id'],))['undone_at']


def test_chain_of_merges_never_rebooks_counted_units(tmp_path):
    """S (2 on a buy line) -> T, then T -> the audit row's item U: the 2 came in on a buy, so U's row still has 3 to
    find. Undo in reverse order restores everything."""
    w, au, so, cat, inbound, b = setup(tmp_path, expected=3)
    m = Merge(w, au.commerce, au)
    s, _ = src_bought_line(w, au, b)
    t = w.save_product({'sku': 'T-1', 'title': 'Toploader 10ct'})['id']
    u = w.save_product({'sku': 'U-1', 'title': 'Toploader (audit)', 'identifiers': [{'kind': 'ASIN', 'value': ASIN}]})['id']
    row = row_of(w)
    before = snapshot(w, au, [s, t, u])
    r1 = m.run(s, {'product_id': t}, 1)
    r2 = m.run(t, {'product_id': u}, 1)
    assert au.item(row)['expected'] - au.item(row)['accounted'] == 3
    assert w.get_product(u)['on_hand'] == 2 and w.check_integrity(record=False)['ok']
    with pytest.raises(WmsError):
        m.undo(r1['merge_id'], 1)                                       # its units sit on U now: undo T -> U first
    m.undo(r2['merge_id'], 1)
    m.undo(r1['merge_id'], 1)
    assert snapshot(w, au, [s, t, u]) == before and w.check_integrity(record=False)['ok']


def test_usual_cost_fill_after_merge_keeps_voided_units_out(tmp_path):
    """The known gap before 11-17: the source's old extra receipt (cost not entered) stayed live after a row merge, so
    typing a usual cost later counted it in Avg cost. Now its units are voided."""
    w, au, so, cat, inbound, b = setup(tmp_path, expected=3)
    m = Merge(w, au.commerce, au)
    s = w.save_product({'sku': 'S-1', 'title': 'Toploader (scanned)'})['id']
    w.receive(1, s, 3, None, b['A1'])
    m.run(s, {'audit_id': row_of(w)}, 1)
    p = w.get_product(s)
    w.save_product({'sku': p['sku'], 'title': p['title'], 'default_cost_cents': 2000,
                    'identifiers': [{'kind': i['kind'], 'value': i['value']} for i in p['identifiers']]}, s)
    assert w.get_product(s)['avg_cost_cents'] == 500


def test_merged_source_shipped_units_count_in_bought_tally(tmp_path):
    """S bought 5 on a line, all in, 3 shipped, merged into T: T's 'past what was bought' tally counts the 3 that left
    as S, so one more extra scan asks first."""
    w, au, so, cat, inbound, b = setup(tmp_path)
    m = Merge(w, au.commerce, au)
    s = w.save_product({'sku': 'S-1', 'title': 'Mug'})['id']
    ln = po(w, 'target.com', 'T9', 5, 700, s)
    w.receive(1, s, 5, 700, b['A1'], purchase_line_id=ln)
    w.remove(1, b['A1'], [{'product_id': s, 'qty': 3}], 'CUSTOMER')
    t = w.save_product({'sku': 'T-1', 'title': 'Mug (dup)'})['id']
    r = m.run(s, {'product_id': t}, 1)
    tally = au.commerce.bought_tally(t, 1)
    assert (tally['bought'], tally['in'], tally['over']) == (5, 5, True)
    m.undo(r['merge_id'], 1)
    assert au.commerce.bought_tally(t, 1) is None                       # T bought nothing of its own


def test_refusals_save_nothing(tmp_path):
    w, au, so, cat, inbound, b = setup(tmp_path, expected=3)
    m = Merge(w, au.commerce, au)
    u = w.save_product({'sku': 'U-1', 'title': 'One-off', 'tracking': 'unit'})['id']
    w.receive(1, u, 1, 500, b['A1'])
    t = w.save_product({'sku': 'T-1', 'title': 'Toploader'})['id']
    archived = w.save_product({'sku': 'X-1', 'title': 'Old'})['id']
    w.archive_product(archived, 1)
    with w._tx() as c:
        c.execute("INSERT INTO owner (name, created_at) VALUES ('Client Co', ?)", (now_utc(),))
        client = c.execute("SELECT id FROM owner WHERE name = 'Client Co'").fetchone()[0]
    cl = w.save_product({'sku': 'CL-1', 'title': 'Client item', 'owner_id': client})['id']
    before = snapshot(w, au, [u, t, archived, cl])
    for src, tgt, words in ((u, {'product_id': t}, 'one unit at a time'), (t, {'product_id': archived}, 'archived'),
                            (t, {'product_id': cl}, 'owners'), (cl, {'audit_id': row_of(w)}, 'client')):
        with pytest.raises(WmsError, match=words):
            m.run(src, tgt, 1)
        assert snapshot(w, au, [u, t, archived, cl]) == before
