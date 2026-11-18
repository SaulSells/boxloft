"""Deploy review 11-17 (Add card photo): Undo and Change take back the photo a save put on its item."""
import os
import sys

import pytest

REPO = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, os.path.join(REPO, 'tests'))
sys.path.insert(0, os.path.join(REPO, 'backend'))

from test_card_listings import with_listings  # noqa: E402
from test_box_line_matrix import JPEG, photos_for  # noqa: E402
from wms import WmsError  # noqa: E402
from test_sorting import UPC  # noqa: E402


def test_change_to_the_same_item_keeps_the_photo(tmp_path):
    """Not this? Change to the same item: the photo stays on it, and the new line's Undo takes it off."""
    w, so, boxes, lid = with_listings(tmp_path)
    photos_for(w, so)
    mug = w.save_product({'sku': 'MUG-9', 'title': 'Blue mug', 'default_cost_cents': 300})
    token, _ = so.photos.save_photo(JPEG)
    r = so.attach_photo(so.put(boxes['A1'], 1, scan_id='s1', product_id=mug['id'], over_ok=True), token, 1)
    c = so.put(boxes['A1'], 1, scan_id='s2', product_id=mug['id'], option=r['option'], over_ok=True, replaces=r['add_id'])
    assert c['kind'] == 'added' and _photos(w, mug['id']) == 1
    so.undo_add(c['add_id'], 1)
    assert _photos(w, mug['id']) == 0


def _photos(w, pid):
    return w._q1('SELECT COUNT(*) AS n FROM photo WHERE product_id = ? AND removed = 0', (pid,))['n']


def test_change_after_photo_pick_leaves_photo_on_wrong_item(tmp_path):
    """Sam photographs a duck, taps the wrong match (a mug with no picture), then "Not this? Change" to the duck.
    The web app resends the photo_token with replaces. The photo stays on the mug and the duck never gets it."""
    w, so, boxes, lid = with_listings(tmp_path)
    ph = photos_for(w, so)
    mug = w.save_product({'sku': 'MUG-9', 'title': 'Blue mug', 'default_cost_cents': 300})
    duck = w.save_product({'sku': 'DUCK-9', 'title': 'Wooden duck', 'default_cost_cents': 300})
    token, _ = ph.save_photo(JPEG)
    r = so.attach_photo(so.put(boxes['A1'], 1, scan_id='s1', product_id=mug['id'], over_ok=True), token, 1)
    assert r['kind'] == 'added' and _photos(w, mug['id']) == 1
    r2 = so.attach_photo(so.put(boxes['A1'], 1, scan_id='s2', product_id=duck['id'], over_ok=True,
                                replaces=r['add_id']), token, 1)
    assert r2['kind'] == 'added' and r2['product_id'] == duck['id']
    # The photo is of the duck: after the Change it should be on the duck, not the mug.
    assert _photos(w, mug['id']) == 0, 'the wrong pick keeps the photo of another item'
    assert _photos(w, duck['id']) == 1, 'the right item never gets the photo'


def test_undo_of_photo_pick_leaves_photo_on_item(tmp_path):
    """Undo of a wrong photo pick takes the unit out but leaves the photo on that item for good."""
    w, so, boxes, lid = with_listings(tmp_path)
    ph = photos_for(w, so)
    mug = w.save_product({'sku': 'MUG-9', 'title': 'Blue mug', 'default_cost_cents': 300})
    token, _ = ph.save_photo(JPEG)
    r = so.attach_photo(so.put(boxes['A1'], 1, scan_id='s1', product_id=mug['id'], over_ok=True), token, 1)
    assert _photos(w, mug['id']) == 1
    so.undo_add(r['add_id'], 1)
    assert _photos(w, mug['id']) == 0, 'Undo did not take back the photo the save put on'


def test_count_change_keeps_the_photo_and_its_undo_takes_it(tmp_path):
    """A pile after a photo pick: the photo stays on the item, and the new line's Undo still takes it off."""
    w, so, boxes, lid = with_listings(tmp_path)
    ph = photos_for(w, so)
    mug = w.save_product({'sku': 'MUG-9', 'title': 'Blue mug', 'default_cost_cents': 300})
    token, _ = ph.save_photo(JPEG)
    r = so.attach_photo(so.put(boxes['A1'], 1, scan_id='s1', product_id=mug['id'], over_ok=True), token, 1)
    q = so.put_qty(r['add_id'], 3, 1, over_ok=True)
    assert _photos(w, mug['id']) == 1
    so.undo_add(q['add_id'], 1)
    assert _photos(w, mug['id']) == 0


# Round 2 of the deploy review 11-17.
def test_refused_undo_still_takes_the_photo_off(tmp_path):
    """Undo of a photo-pick line that the ledger refuses (the unit was moved to another box since) must change
    nothing. _detach_photo commits before _undo_units runs, so the refused Undo still strips the item's photo."""
    w, so, boxes, lid = with_listings(tmp_path)
    photos_for(w, so)
    mug = w.save_product({'sku': 'MUG-9', 'title': 'Blue mug', 'default_cost_cents': 300})
    token, _ = so.photos.save_photo(JPEG)
    r = so.attach_photo(so.put(boxes['A1'], 1, scan_id='s1', product_id=mug['id'], over_ok=True), token, 1)
    assert r['kind'] == 'added' and _photos(w, mug['id']) == 1
    other = [k for k in boxes if k != 'A1'][0]
    w.move(1, [{'product_id': mug['id'], 'qty': 1}], boxes['A1'], boxes[other])
    with pytest.raises(WmsError):
        so.undo_add(r['add_id'], 1)
    assert _photos(w, mug['id']) == 1, 'a refused Undo took the photo off the item (half a save written)'
    assert w._q1('SELECT COUNT(*) AS n FROM box_add_photo WHERE add_id = ?', (r['add_id'],))['n'] == 1


def test_history_undo_of_a_photo_pick_leaves_the_photo(tmp_path):
    """History's Undo (POST /api/undo = wms.undo) of the same photo-pick save takes the unit out but leaves the photo
    on the wrongly picked item, while the line's own Undo takes it off: the two Undos of one save disagree."""
    w, so, boxes, lid = with_listings(tmp_path)
    photos_for(w, so)
    mug = w.save_product({'sku': 'MUG-9', 'title': 'Blue mug', 'default_cost_cents': 300})
    token, _ = so.photos.save_photo(JPEG)
    r = so.attach_photo(so.put(boxes['A1'], 1, scan_id='s1', product_id=mug['id'], over_ok=True), token, 1)
    assert _photos(w, mug['id']) == 1
    row = w._q1('SELECT action_id FROM box_add WHERE id = ?', (r['add_id'],))
    w.undo(1, row['action_id'], '', '')
    assert _photos(w, mug['id']) == 0, 'History Undo left the photo on the item the undone save put it on'


# Round 3 of the deploy review 11-17.
def test_count_change_brings_back_a_photo_a_person_removed(tmp_path):
    """Sam removes the wrong photo on the item page, then changes the line's count: the photo comes back."""
    w, so, boxes, lid = with_listings(tmp_path)
    photos_for(w, so)
    mug = w.save_product({'sku': 'MUG-9', 'title': 'Blue mug', 'default_cost_cents': 300})
    token, _ = so.photos.save_photo(JPEG)
    r = so.attach_photo(so.put(boxes['A1'], 1, scan_id='s1', product_id=mug['id'], over_ok=True), token, 1)
    assert _photos(w, mug['id']) == 1
    w.remove_photo(r['photo_id'])   # the item page's Remove
    assert _photos(w, mug['id']) == 0
    so.put_qty(r['add_id'], 2, 1, over_ok=True)
    assert _photos(w, mug['id']) == 0, 'a count change put back the photo the person removed'


def test_change_same_item_brings_back_a_photo_a_person_removed(tmp_path):
    w, so, boxes, lid = with_listings(tmp_path)
    photos_for(w, so)
    mug = w.save_product({'sku': 'MUG-9', 'title': 'Blue mug', 'default_cost_cents': 300})
    token, _ = so.photos.save_photo(JPEG)
    r = so.attach_photo(so.put(boxes['A1'], 1, scan_id='s1', product_id=mug['id'], over_ok=True), token, 1)
    w.remove_photo(r['photo_id'])
    so.put(boxes['A1'], 1, scan_id='s2', product_id=mug['id'], option=r['option'], over_ok=True, replaces=r['add_id'])
    assert _photos(w, mug['id']) == 0, 'Change to the same item put back the photo the person removed'


def test_resent_save_after_undo_puts_the_photo_back(tmp_path):
    """A resend of the first save (same scan_id, photo_token) after its Undo answers as the old save and
    attaches the photo again to the item whose save is undone."""
    w, so, boxes, lid = with_listings(tmp_path)
    photos_for(w, so)
    mug = w.save_product({'sku': 'MUG-9', 'title': 'Blue mug', 'default_cost_cents': 300})
    token, _ = so.photos.save_photo(JPEG)
    r = so.attach_photo(so.put(boxes['A1'], 1, scan_id='s1', product_id=mug['id'], over_ok=True), token, 1)
    so.undo_add(r['add_id'], 1)
    assert _photos(w, mug['id']) == 0
    so.attach_photo(so.put(boxes['A1'], 1, scan_id='s1', product_id=mug['id'], over_ok=True), token, 1)
    assert _photos(w, mug['id']) == 0, 'a resent, already undone save put the photo back on the item'


# Round 4 of the deploy review 11-17.
def test_change_same_item_as_the_route_does_brings_back_a_removed_photo(tmp_path):
    """The web app's Change resends the line's photo_token (BoxAdd.tsx: photo -> photo_token), and the put_away route
    runs attach_photo after put(). A photo the person removed on the item page comes back as a new photo row."""
    w, so, boxes, lid = with_listings(tmp_path)
    photos_for(w, so)
    mug = w.save_product({'sku': 'MUG-9', 'title': 'Blue mug', 'default_cost_cents': 300})
    token, _ = so.photos.save_photo(JPEG)
    r = so.attach_photo(so.put(boxes['A1'], 1, scan_id='s1', product_id=mug['id'], over_ok=True), token, 1)
    w.remove_photo(r['photo_id'])   # the item page's Remove: wrong photo
    assert _photos(w, mug['id']) == 0
    c = so.put(boxes['A1'], 1, scan_id='s2', product_id=mug['id'], option=r['option'], over_ok=True, replaces=r['add_id'])
    so.attach_photo(c, token, 1)   # server.put_away does this with the resent photo_token
    assert _photos(w, mug['id']) == 0, 'Change put back (as a new row) the photo the person removed'


def test_create_product_attach_reuses_a_photo_a_person_removed(tmp_path):
    """PhotoId.attach was "once; a reused link adds nothing". Now a token whose photo a person removed attaches again
    (Create product / POST /api/photo-id/attach from an old tab), so the removed photo is back on the item."""
    w, so, boxes, lid = with_listings(tmp_path)
    ph = photos_for(w, so)
    mug = w.save_product({'sku': 'MUG-9', 'title': 'Blue mug', 'default_cost_cents': 300})
    token, _ = ph.save_photo(JPEG)
    first = ph.attach(token, mug['id'], 1, 'Blue mug')
    assert first
    w.remove_photo(first['id'])
    assert ph.attach(token, mug['id'], 1, 'Blue mug') is None, 'a reused photo link attached the removed photo again'


def test_count_change_answer_shows_no_photo(tmp_path):
    """put_qty builds its answer before _carry_photo puts the photo back, so the line the web app redraws from the
    answer (BoxAdd.tsx setLines with r) says the item has no photo while it has one."""
    w, so, boxes, lid = with_listings(tmp_path)
    photos_for(w, so)
    mug = w.save_product({'sku': 'MUG-9', 'title': 'Blue mug', 'default_cost_cents': 300})
    token, _ = so.photos.save_photo(JPEG)
    r = so.attach_photo(so.put(boxes['A1'], 1, scan_id='s1', product_id=mug['id'], over_ok=True), token, 1)
    q = so.put_qty(r['add_id'], 3, 1, over_ok=True)
    assert _photos(w, mug['id']) == 1
    assert q['photo_id'] == r['photo_id'], 'count change answer says no photo'


# Round 5 of the deploy review 11-17.
def test_history_undo_of_audit_photo_line_leaves_the_photo(tmp_path):
    """A photo pick that lands on a Stock audit row (box_add kind 'audit') stores no action_id on box_add, only
    count_id. History's Undo (or the audit card's Undo) of that found action goes through wms._undo, whose photo hook
    looks for box_add.action_id / action_ids only: the unit comes out, the photo stays on the item, and its link points
    at an undone line. The right item then never gets the photo."""
    w, so, boxes, lid = with_listings(tmp_path)
    photos_for(w, so)
    token, _ = so.photos.save_photo(JPEG)
    r = so.attach_photo(so.put(boxes['A1'], 1, scan_id='a1', code=UPC), token, 1)
    assert r['kind'] == 'added' and r['how'] == 'audit' and r.get('photo_id')
    pid = r['product_id']
    assert _live(w, pid) == 1
    row = w._q1('SELECT count_id FROM box_add WHERE id = ?', (r['add_id'],))
    act = w._q1('SELECT action_id FROM audit_count WHERE id = ?', (row['count_id'],))['action_id']
    w.undo(1, act)   # History's Undo of the found save
    assert _live(w, pid) == 0, 'the undone audit line still leaves its photo on the item'
    # the wrong pick undone: the right item should be able to take the photo
    duck = w.save_product({'sku': 'DUCK-9', 'title': 'Wooden duck', 'default_cost_cents': 300})
    so.attach_photo(so.put(boxes['A1'], 1, scan_id='a2', product_id=duck['id'], over_ok=True), token, 1)
    assert _live(w, duck['id']) == 1


def _live(w, pid):
    return w._q1("SELECT COUNT(*) AS n FROM photo WHERE product_id = ? AND removed = 0", (pid,))["n"]


# Round 6 of the deploy review 11-17.
def test_undo_of_one_photo_line_strips_photo_another_standing_line_still_carries(tmp_path):
    """Sam photographs a no-barcode mug and taps its match twice (2 units, 2 scan ids, same photo token).
    The first line puts the photo on the mug; the second finds a photo already there. She undoes the first line
    (one too many). The second line still stands on the mug with that same photo, but the Undo takes it off:
    the mug ends with stock in the box and no photo, and the photo sits 'freed' for some other item."""
    w, so, boxes, lid = with_listings(tmp_path)
    photos_for(w, so)
    mug = w.save_product({'sku': 'MUG-9', 'title': 'Blue mug', 'default_cost_cents': 300})
    token, _ = so.photos.save_photo(JPEG)
    r1 = so.attach_photo(so.put(boxes['A1'], 1, scan_id='s1', product_id=mug['id'], over_ok=True), token, 1)
    r2 = so.attach_photo(so.put(boxes['A1'], 1, scan_id='s2', product_id=mug['id'], over_ok=True), token, 1)
    assert r1['kind'] == 'added' and r2['kind'] == 'added' and _photos(w, mug['id']) == 1
    so.undo_add(r1['add_id'], 1)
    assert w._q1('SELECT reversed_by FROM action WHERE id = ?',
                 (w._q1('SELECT action_id FROM box_add WHERE id = ?', (r2['add_id'],))['action_id'],))['reversed_by'] is None
    assert _photos(w, mug['id']) == 1, 'the standing line on the same item, same photo, lost its photo'


# Round 7 of the deploy review 11-17.
def test_pile_split_extra_line_loses_its_photo_when_the_main_line_is_undone(tmp_path):
    """A photo pick lands on the Stock audit row (3 expected). Sam types 5 (a pile): 3 go on the row, 2 on an extra
    line, both from that one save with the photo. She undoes the row line (they were not from that order). The extra
    line still stands with 2 units of the item in the box, but the photo the save gave the item comes off and sits
    'freed' for another item."""
    w, so, boxes, lid = with_listings(tmp_path)
    photos_for(w, so)
    token, _ = so.photos.save_photo(JPEG)
    r = so.attach_photo(so.put(boxes['A1'], 1, scan_id='a1', code=UPC), token, 1)
    assert r['kind'] == 'added' and r['how'] == 'audit' and r.get('photo_id')
    pid = r['product_id']
    q = so.put_qty(r['add_id'], 5, 1, over_ok=True)
    assert q.get('extra_line') and q['extra_line']['qty'] == 2
    assert _live(w, pid) == 1
    so.undo_add(q['add_id'], 1)
    extra = w._q1('SELECT action_id FROM box_add WHERE id = ?', (q['extra_line']['add_id'],))['action_id']
    assert w._q1('SELECT reversed_by FROM action WHERE id = ?', (extra,))['reversed_by'] is None
    assert _live(w, pid) == 1, 'the extra line of the same pile still stands, but its item lost the photo'
