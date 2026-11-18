import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

from test_card_listings import with_listings  # noqa: E402
from test_box_line_matrix import JPEG, photos_for  # noqa: E402
from test_sorting import UPC  # noqa: E402


def live(w, pid):
    return w._q1('SELECT COUNT(*) AS n FROM photo WHERE product_id = ? AND removed = 0', (pid,))['n']


def test_extra_line_change_as_page_sends_loses_photo(tmp_path):
    """BoxAdd.tsx:429 builds the count change's extra line with no photo_token, so its 'Not this? Change' (line 442 ->
    350) sends none. A photo pick on the wrong item X lands on its Stock audit row; count change to 5 (3 on the row,
    2 extra); Undo the row line; then Change the extra line to the right item Y. The page's request carries no
    photo_token, the old line's undo frees the photo off X, and Y never gets it. The main line's Change, which has the
    token, would have moved it (model rule 5)."""
    w, so, boxes, _ = with_listings(tmp_path)
    photos_for(w, so)
    tok, _ = so.photos.save_photo(JPEG)
    r = so.attach_photo(so.put(boxes['A1'], 1, scan_id='a1', code=UPC), tok, 1)
    x = r['product_id']
    q = so.put_qty(r['add_id'], 5, 1, over_ok=True)
    ex = q['extra_line']
    so.undo_add(q['add_id'], 1)
    assert live(w, x) == 1
    y = w.save_product({'sku': 'Y1', 'title': 'Green bowl', 'default_cost_cents': 300})['id']
    page_token = ''   # what the page holds for the extra line (no photo_token at BoxAdd.tsx:429)
    c = so.attach_photo(so.put(boxes['A1'], 1, scan_id='s9', product_id=y, over_ok=True, replaces=ex['add_id']), page_token, 1)
    assert c['kind'] == 'added'
    assert live(w, x) == 0
    assert live(w, y) == 1, 'the photo was freed off the wrong item but never reached the right one'
