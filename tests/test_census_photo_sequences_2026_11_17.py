import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

from test_card_listings import with_listings  # noqa: E402
from test_box_line_matrix import JPEG, photos_for  # noqa: E402


def _photos(w, pid):
    return w._q1('SELECT COUNT(*) AS n FROM photo WHERE product_id = ? AND removed = 0', (pid,))['n']


def _mug(w):
    return w.save_product({'sku': 'MUG-9', 'title': 'Blue mug', 'default_cost_cents': 300})


def test_g2_undo_of_a_second_save_takes_off_a_photo_a_person_attached(tmp_path):
    """Alex taps a photo match on the photo-id screen (/api/photo-id/attach puts the photo on the mug). Then on the
    box page the same photo's pick saves 1 mug (attach_photo's 'same' branch links the line). The line's Undo takes
    the person's photo off the mug, although this save never put it on."""
    w, so, boxes, lid = with_listings(tmp_path)
    photos_for(w, so)
    mug = _mug(w)
    token, _ = so.photos.save_photo(JPEG)
    assert so.photos.attach(token, mug['id'], 1, 'Blue mug')
    assert _photos(w, mug['id']) == 1
    r = so.attach_photo(so.put(boxes['A1'], 1, scan_id='s1', product_id=mug['id'], over_ok=True), token, 1)
    assert r['kind'] == 'added'
    so.undo_add(r['add_id'], 1)
    assert _photos(w, mug['id']) == 1, 'Undo of the box page save took off the photo a person attached on its own'


def test_g2_history_undo_of_a_second_save_takes_off_a_person_photo(tmp_path):
    """Same as above through History's Undo (wms._undo's box_add_photo block)."""
    w, so, boxes, lid = with_listings(tmp_path)
    photos_for(w, so)
    mug = _mug(w)
    token, _ = so.photos.save_photo(JPEG)
    assert so.photos.attach(token, mug['id'], 1, 'Blue mug')
    r = so.attach_photo(so.put(boxes['A1'], 1, scan_id='s1', product_id=mug['id'], over_ok=True), token, 1)
    row = w._q1('SELECT action_id FROM box_add WHERE id = ?', (r['add_id'],))
    w.undo(1, row['action_id'], '', '')
    assert _photos(w, mug['id']) == 1, 'History Undo took off the photo a person attached on its own'


def test_photo_id_attach_refused_after_the_box_save_is_undone(tmp_path):
    """Sam's photo pick saves 1 mug (wrong item), she taps Undo. Then on the photo-id screen she attaches the same
    photo to the duck (/api/photo-id/attach). As if the undone save never happened, the duck should get the photo;
    PhotoId.attach sees the freed (removed) photo row and refuses, so the photo can never go on the right item there."""
    w, so, boxes, lid = with_listings(tmp_path)
    photos_for(w, so)
    mug = _mug(w)
    duck = w.save_product({'sku': 'DUCK-9', 'title': 'Wooden duck', 'default_cost_cents': 300})
    token, _ = so.photos.save_photo(JPEG)
    r = so.attach_photo(so.put(boxes['A1'], 1, scan_id='s1', product_id=mug['id'], over_ok=True), token, 1)
    assert r.get('photo_id')
    so.undo_add(r['add_id'], 1)
    assert _photos(w, mug['id']) == 0
    so.photos.attach(token, duck['id'], 1, 'Wooden duck')
    assert _photos(w, duck['id']) == 1, 'photo-id attach refused a photo that an Undo freed'
