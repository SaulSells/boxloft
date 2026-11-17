"""One-off repairs of rows saved wrong before the 11-11 sweep fixes (manage.py fix-sweep-data). Dry run by default;
apply=True writes. Each part finds only rows that are still wrong, so a second run changes nothing."""
from inbound import today_et
from store_mail import et_day, walmart_status
from wms import now_utc


def sweep_fix(wms, apply: bool = False) -> dict:
    out = {'applied': apply}
    # A4: receipts saved at $0 before receipt.cost_missing existed, where the person typed no cost (the audit extra
    # says so: its unit_cost_cents is NULL). They count as cost not entered, not as a real $0.
    a4 = wms._q("""SELECT r.id, r.product_id FROM receipt r JOIN audit_extra e ON e.action_id = r.action_id
                    WHERE r.unit_cost_cents = 0 AND r.cost_missing = 0 AND e.unit_cost_cents IS NULL""")
    out['a4_receipts'] = [r['id'] for r in a4]
    # D7: store orders dated by the email's UTC day; the order day is the ET day.
    d7 = []
    for r in wms._q("""SELECT o.id, o.ordered_at, m.sent_at FROM purchase_order o JOIN store_mail m ON m.message_id = o.mail_id
                        WHERE o.account = 'gmail' AND m.sent_at IS NOT NULL"""):
        day = et_day(r['sent_at'])
        if day and r['ordered_at'] == (r['sent_at'] or '')[:10] and day != r['ordered_at']:
            d7.append({'po_id': r['id'], 'from': r['ordered_at'], 'to': day})
    out['d7_orders'] = len(d7)
    out['d7_sample'] = d7[:5]
    # C1: Walmart pickups ("Picked up Sun, Mar 22, 3:19pm") saved as ordered, expected a year later.
    c1 = []
    for r in wms._q("""SELECT id, external_id, state, status_text, expected_to FROM purchase_order
                        WHERE source = 'walmart.com' AND state != 'delivered' AND LOWER(status_text) LIKE 'picked up%'"""):
        st = walmart_status(r['status_text'], today_et())
        if st['state'] == 'delivered':
            c1.append({'po_id': r['id'], 'order': r['external_id'], 'was': f"{r['state']} {r['expected_to']}",
                       'delivered_on': st['delivered_on']})
    out['c1_orders'] = c1
    if not apply:
        return out
    with wms._tx() as c:
        for r in a4:
            c.execute('UPDATE receipt SET cost_missing = 1 WHERE id = ?', (r['id'],))
            c.execute('UPDATE unit SET cost_cents = NULL WHERE receipt_id = ? AND cost_cents = 0', (r['id'],))
        for d in d7:
            c.execute('UPDATE purchase_order SET ordered_at = ? WHERE id = ? AND ordered_at = ?', (d['to'], d['po_id'], d['from']))
            # receipts copied the order day as their purchase date
            c.execute("""UPDATE receipt SET purchased_at = ? WHERE purchased_at = ? AND purchase_line_id IN
                         (SELECT id FROM purchase_line WHERE po_id = ?)""", (d['to'], d['from'], d['po_id']))
        for d in c1:
            c.execute("""UPDATE purchase_order SET state = 'delivered', delivered_on = ?, expected_from = NULL, expected_to = NULL,
                         status_changed_at = ? WHERE id = ?""", (d['delivered_on'], now_utc(), d['po_id']))
            c.execute("""INSERT INTO purchase_event (po_id, at, state, status_text, expected_from, expected_to)
                         VALUES (?,?,'delivered','Picked up (fixed: was read as next year)',NULL,NULL)""", (d['po_id'], now_utc()))
    return out
