"""Alex 11-11 9:30 PM ("Ensure no other quick math or mistakes are buried in here"): the Stock audit counts each ASIN
from Boxloft's own records, not the 10-02 sheet. Every Amazon buy up to the sheet's last buy, at Amazon's real
quantity, less seller returns, less every Amazon sale nothing here shipped (oldest buy first, only a buy that had
reached us by the sale day; no sales date since 11-11 night). The sheet's number stays beside it as history.

What the sheet got wrong on live data (11-11, an independent recount): tracker quantity 1 on orders Amazon charged for 5 or 3,
seller returns never taken off, units received against an order the sheet had marked sold. And Sent back on a
delivered-not-scanned row counted the unit twice."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from audit import Audit  # noqa: E402
from commerce import Commerce  # noqa: E402
from connections import Connections  # noqa: E402
from inbound import Inbound  # noqa: E402
from returns import Returns  # noqa: E402
from sales_sync import Sales  # noqa: E402
from wms import Wms  # noqa: E402

SNAP = 'Bought 2024-11-10 on, unsold as of Amazon sales through 2026-09-16 (units.csv built 2026-10-02)'
A, B, C, D, R = 'B0SHEETROW', 'B0NEWASIN1', 'B0SOLDOUT1', 'B0LATEBUY1', 'B0RETURNED'


def order(c, ext, day, account, asin, qty, cents=1000, state='ordered', delivered_on=None):
    po = c.execute("""INSERT INTO purchase_order (source, external_id, account, ordered_at, imported_at, state, delivered_on)
                      VALUES ('amazon',?,?,?,'x',?,?)""", (ext, account, day, state, delivered_on)).lastrowid
    return c.execute('INSERT INTO purchase_line (po_id, line_key, asin, title, qty, unit_cost_cents) VALUES (?,?,?,?,?,?)',
                     (po, asin, asin, f'Item {asin}', qty, cents)).lastrowid


def setup(tmp_path):
    w = Wms(str(tmp_path / 'w.db'))
    com = Commerce(w)
    inbound = Inbound(w)
    rt = Returns(w, com)
    au = Audit(w, com, returns=rt)
    s = Sales(w, com, Connections(str(tmp_path)))
    with w._tx() as c:
        order(c, '111-SHEET-1', '2026-01-10', 'Acmebuyer', A, 5)         # the tracker logged 1 of these 5
        order(c, '111-SHEET-2', '2026-01-11', 'Acmebuyer', A, 1, cents=2000)
        order(c, '111-TRACK-2', '2026-01-12', 'Acmebuyer', C, 2)         # sold out by the sales day
        order(c, '111-HIST-3', '2026-02-22', 'Amazon.com', A, 2)         # Order History, Feb gap: same ASIN as the row
        order(c, '111-HIST-4', '2026-02-23', 'Amazon.com', B, 3)         # Order History, an ASIN with no row
        order(c, '111-EXT-5', '2026-06-01', '', B, 1)                    # read by the extension, no tracker had it
        order(c, '111-RET-6', '2026-03-01', 'Acmebuyer', R, 4)           # 3 went back to the seller
        order(c, '111-X-7', '2026-03-02', 'Acmebuyer', R, 9, state='cancelled')
        order(c, '111-LATE-8', '2026-09-10', '', D, 2, state='delivered', delivered_on='2026-09-14')   # after the sheet
    au.import_snapshot([
        {'asin': A, 'title': 'Item A', 'expected': 2, 'avg_cost_cents': 1000, 'cost_cents': 2000, 'first_buy': '2026-01-10',
         'last_buy': '2026-01-11', 'orders': [{'id': '111-SHEET-1', 'date': '2026-01-10', 'units': 1},
                                              {'id': '111-SHEET-2', 'date': '2026-01-11', 'units': 1}]},
        {'asin': R, 'title': 'Item R', 'expected': 4, 'avg_cost_cents': 1000, 'cost_cents': 4000, 'first_buy': '2026-03-01',
         'last_buy': '2026-08-27', 'orders': [{'id': '111-RET-6', 'date': '2026-03-01', 'units': 4}]}], SNAP)
    rid = w._q1("SELECT l.id FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id WHERE o.external_id = '111-RET-6'")['id']
    with w._tx() as c:
        rt._save(c, rid, 'manual', 1, add_qty=3, state='refunded')
    return w, com, inbound, au, s


def sale(s, ext, day, asin, qty=1, state='shipped'):
    s.upsert('amazon', [{'external_id': ext, 'ordered_at': day, 'state': state, 'fulfilled_by': 'me',
                         'lines': [{'line_key': 'OI-' + ext, 'sku': asin, 'asin': asin, 'title': 'x', 'qty': qty, 'price_cents': 999}]}])


def rows(au):
    return {r['asin']: r for r in au.list()['items']}


def line(w, ext):
    return w._q1("SELECT l.id FROM purchase_line l JOIN purchase_order o ON o.id = l.po_id WHERE o.external_id = ?", (ext,))['id']


def test_count_comes_from_amazon_lines_not_the_sheet(tmp_path):
    w, com, inbound, au, s = setup(tmp_path)
    sale(s, '113-1', '2026-01-20', A)                     # sold before the sales day: the oldest buy's unit
    sale(s, '113-2', '2026-02-01', C, qty=2)              # C sold out
    sale(s, '113-OPEN', '2026-03-01', B, state='open')    # not shipped yet: the unit is still on our shelf
    r = rows(au)
    assert (r[A]['expected'], r[A]['sheet_expected'], r[A]['count']) == (5 + 1 + 2 - 1, 2, 'own')
    assert [(o['id'], o['units']) for o in r[A]['orders']] == [('111-SHEET-1', 4), ('111-SHEET-2', 1), ('111-HIST-3', 2)]
    assert r[A]['cost_cents'] == 4 * 1000 + 2000 + 2 * 1000 and r[A]['avg_cost_cents'] == round(8000 / 7)
    assert (r[A]['bought'], r[A]['amazon_sold'], r[A]['returned_before']) == (8, 1, 0)
    assert r[B]['expected'] == 3 + 1 and r[B]['amazon_sold'] == 0    # its own row, made from the lines
    assert C not in r
    assert (r[R]['expected'], r[R]['sheet_expected'], r[R]['seller_returns']) == (1, 4, 0)   # 3 refunded, 9 cancelled
    assert au.unaccounted(inbound)['lines'] == 0
    assert au.cap_cuts() == []                            # Amazon's own quantities: nothing to cap


def test_every_amazon_sale_comes_off_with_no_sales_date(tmp_path):
    """Alex 11-11 night: the 9-16 sales day is gone. A sale after it comes off the count like any other, and the card
    reads bought / sold on Amazon / should be here. A sale placed before a buy arrived can't have sold it."""
    w, com, inbound, au, s = setup(tmp_path)
    for ext, day, asin in (('113-1', '2026-03-01', B), ('113-2', '2026-03-02', A), ('113-3', '2026-09-20', A),
                           ('113-4', '2026-09-12', D), ('113-5', '2026-10-01', D)):
        s.upsert('amazon', [{'external_id': ext, 'ordered_at': day, 'state': 'shipped', 'fulfilled_by': 'me',
                             'lines': [{'line_key': 'OI-' + ext, 'sku': asin, 'asin': asin, 'title': 'x', 'qty': 1, 'price_cents': 999}]}])
    r = rows(au)
    assert (r[B]['expected'], r[B]['amazon_sold']) == (3, 1)            # 113-1 took one of its 4
    assert (r[A]['bought'], r[A]['amazon_sold'], r[A]['expected']) == (8, 2, 6)   # 113-3 (9-20) counts too
    # D was delivered 9-14: the 9-12 sale was other stock, the 10-01 sale sold one of D's 2.
    assert (r[D]['bought'], r[D]['amazon_sold'], r[D]['expected'], r[D]['exp_delivered']) == (2, 1, 1, 1)
    assert 'sold_after' not in r[A]
    assert au.unaccounted(inbound)['lines'] == 0


def test_a_sale_with_no_old_buy_left_goes_to_the_later_buy(tmp_path):
    """1 bought in May and sold in June; 2 more bought 9-01 (after the sheet's last buy) and 1 sold 9-05. The 9-05 sale
    is before the sales day, but nothing up to the cutoff was left for it: it sold one of the 9-01 units."""
    w, com, inbound, au, s = setup(tmp_path)
    with w._tx() as c:
        order(c, '111-MAY', '2026-05-01', 'Acmebuyer', 'B0BOTHBUYS', 1)
        order(c, '111-SEP', '2026-09-01', '', 'B0BOTHBUYS', 2, state='delivered', delivered_on='2026-09-04')
    sale(s, '113-JUN', '2026-06-01', 'B0BOTHBUYS')
    sale(s, '113-SEP', '2026-09-05', 'B0BOTHBUYS')
    sale(s, '113-AUG', '2026-08-01', 'B0BOTHBUYS')       # before the later buy, nothing old left: someone else's stock
    it = rows(au)['B0BOTHBUYS']
    assert (it['bought'], it['amazon_sold'], it['expected'], it['exp_delivered']) == (3, 2, 1, 1)


def test_receive_against_an_order_the_sales_took_still_counts(tmp_path):
    """B0B39S0W1E on live: 3 bought in November, 3 in December, 3 sold. Sales take the oldest buy, but Sam received 2
    against the November order on the Receive page. They are on the shelf: the row must ask for 1, not 3."""
    w, com, inbound, au, s = setup(tmp_path)
    owner = w.list_owners()[0]['id']
    p = w.save_product({'sku': A, 'title': 'Item A', 'owner_id': owner, 'identifiers': [{'kind': 'ASIN', 'value': A}]})
    loc = w._q1("SELECT id FROM location WHERE kind != 'virtual' LIMIT 1")['id']
    w.receive(1, p['id'], 2, 1000, {'location_id': loc}, purchase_line_id=line(w, '111-SHEET-1'))
    sale(s, '113-1', '2026-02-01', A, qty=5)            # the sales history lands after the receipt (as on live)
    it = rows(au)[A]
    assert (it['expected'], it['received'], it['accounted']) == (3, 2, 2)
    # The 2 scanned in were here, so the sales take SHEET-1's other 3, SHEET-2's 1 and 1 of HIST-3's 2.
    left = {x['order_id']: x['left_to_receive'] for x in com.open_lines_for(p['id'])}
    assert left == {'111-HIST-3': 1}
    assert au.unaccounted(inbound)['lines'] == 0


def test_left_to_receive_leaves_out_units_sold_before_boxloft(tmp_path):
    """Alex 11-11: Purchases, Receive and Orders "coming" counted units Amazon sold before Boxloft as still coming
    (3,482 units on live; 2,681 after). They use the audit's own count of what sold (line_presold)."""
    w, com, inbound, au, s = setup(tmp_path)
    sale(s, '113-1', '2026-02-01', A, qty=6)                            # SHEET-1 (5) and SHEET-2 (1)
    sale(s, '113-2', '2026-09-20', A, qty=1)                            # after the old sales day: counts the same
    owner = w.list_owners()[0]['id']
    p = w.save_product({'sku': A, 'title': 'Item A', 'owner_id': owner, 'identifiers': [{'kind': 'ASIN', 'value': A}]})
    assert [(x['order_id'], x['left_to_receive']) for x in com.open_lines_for(p['id'])] == [('111-HIST-3', 1)]
    loc = w._q1("SELECT id FROM location WHERE kind != 'virtual' LIMIT 1")['id']
    import pytest
    from wms import WmsError
    with pytest.raises(WmsError):                                       # a sold line can't be received against
        w.receive(1, p['id'], 1, 1000, {'location_id': loc}, purchase_line_id=line(w, '111-SHEET-1'))
    it = rows(au)[A]
    assert (it['expected'], it['amazon_sold']) == (1, 7)
    by = {r['line_id']: r['units'] for r in w._q('SELECT * FROM line_presold')}
    assert by == {line(w, '111-SHEET-1'): 5, line(w, '111-SHEET-2'): 1, line(w, '111-HIST-3'): 1}


def test_found_receives_against_the_units_still_here(tmp_path):
    """The units left are on the newest orders: Found it takes their cost, not the sold order's."""
    w, com, inbound, au, s = setup(tmp_path)
    sale(s, '113-1', '2026-02-01', A, qty=5)                            # SHEET-1's 5 are gone
    it = rows(au)[A]
    box = w.create_boxes(1, 1)[0]['id']
    au.found(it['id'], 3, {'box_id': box}, 1)
    got = [(r['purchase_line_id'], r['qty'], r['unit_cost_cents']) for r in w._q('SELECT * FROM receipt ORDER BY id')]
    assert got == [(line(w, '111-SHEET-2'), 1, 2000), (line(w, '111-HIST-3'), 2, 1000)]
    assert w.check_integrity()['ok']


def test_sent_back_counts_once_on_every_kind_of_row(tmp_path):
    """Before 11-11, Sent back on a delivered-not-scanned row took the unit off `expected` (a new return on the line)
    and added it to `accounted`: 2 delivered, 1 sent back showed 1 of 1 done with 1 unit never looked for."""
    w, com, inbound, au, s = setup(tmp_path)
    late = rows(au)[D]
    assert (late['expected'], late['accounted']) == (2, 0)
    it = au.sent_back(late['id'], 1, 1, state='sent')
    assert (it['expected'], it['accounted'], it['sent_back']) == (2, 1, 1)
    undo = [h for h in it['history'] if h['kind'] == 'returned'][0]['outcome_id']
    it = au.undo_outcome(late['id'], undo, 1)
    assert (it['expected'], it['accounted']) == (2, 0)
    # A return Amazon already shows is taken off; claiming it moves the unit to accounted, once.
    r = rows(au)[R]
    assert (r['expected'], r['accounted']) == (1, 0)
    it = au.sent_back(r['id'], 1, 1)
    assert (it['expected'], it['accounted'], it['expected'] - it['accounted']) == (2, 1, 1)


def test_supplies_and_hidden_orders_never_join(tmp_path):
    w, com, inbound, au, s = setup(tmp_path)
    with w._tx() as c:
        order(c, '111-BOX-9', '2026-09-11', '', 'B0SHIPBOX1', 50, state='delivered', delivered_on='2026-09-15')
        order(c, '111-BOX-10', '2026-02-11', '', 'B0SHIPBOX2', 50)
        c.execute("UPDATE purchase_order SET kind = 'supplies' WHERE external_id IN ('111-BOX-9', '111-BOX-10')")
        order(c, '111-HID-11', '2026-02-12', '', 'B0PERSONAL', 1)
        c.execute("UPDATE purchase_order SET hidden = 1 WHERE external_id = '111-HID-11'")
    r = rows(au)
    assert not {'B0SHIPBOX1', 'B0SHIPBOX2', 'B0PERSONAL'} & set(r)


def test_a_sale_only_takes_a_unit_that_had_reached_us(tmp_path):
    """Live 11-11 night. PowerA controller B0F5WIY3CH: 2 bought 2025-12-03 and both sent back, 1 sold 2025-12-15,
    1 bought 2026-07-31. The December sale can't be the July unit: it takes nothing, and the July unit is still
    expected. Dunkin' K-Cups B0OZVEVGYB: Receive booked the units scanned in on 11-10 against the oldest order (the
    November one), but Amazon sold December units out of that November buy. The arrival check runs on the units
    bought, so those December sales still count, and they come off the units nobody scanned in."""
    w, com, inbound, au, s = setup(tmp_path)
    P, K = 'B0POWERA01', 'B0KCUPS001'
    with w._tx() as c:
        dec = order(c, '111-PA-1', '2025-12-03', 'Acmebuyer', P, 2)
        order(c, '111-PA-2', '2026-07-31', 'Acmebuyer', P, 1)
        nov = order(c, '111-KC-1', '2025-11-18', 'Acmebuyer', K, 3)
        order(c, '111-KC-2', '2026-01-28', 'Acmebuyer', K, 3)
    with w._tx() as c:
        au.returns._save(c, dec, 'manual', 1, add_qty=2, state='refunded')
    owner = w.list_owners()[0]['id']
    p = w.save_product({'sku': K, 'title': 'K-Cups', 'owner_id': owner, 'identifiers': [{'kind': 'ASIN', 'value': K}]})
    loc = w._q1("SELECT id FROM location WHERE kind != 'virtual' LIMIT 1")['id']
    w.receive(1, p['id'], 3, 1000, {'location_id': loc}, purchase_line_id=nov)
    sale(s, '113-PA', '2025-12-15', P)
    sale(s, '113-KC', '2025-12-09', K, qty=2)
    r = rows(au)
    assert (r[P]['bought'], r[P]['returned_before'], r[P]['amazon_sold'], r[P]['expected']) == (3, 2, 0, 1)
    assert (r[K]['bought'], r[K]['amazon_sold'], r[K]['expected'], r[K]['received']) == (6, 2, 4, 3)
    assert {x['order_id']: x['left_to_receive'] for x in com.open_lines_for(p['id'])} == {'111-KC-2': 1}
    assert au.unaccounted(inbound)['lines'] == 0
