import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

from test_photo_line_model import JPEG, PhotoLines  # noqa: E402


def live(w, pid):
    return w._q('SELECT id, path FROM photo WHERE product_id = ? AND removed = 0', (pid,))


def test_c18_erase_path_leaves_photo_stuck(tmp_path):
    """c18's erase path: an item made from the photo (photo-id Create product puts the photo on it via attach), then
    erased with Delete (wms.delete_product: product_id NULL, removed 1, no photo_freed). A pick of the right item Y
    with the same token gets nothing: PhotoId.claim reads the row as removed by a person."""
    m = PhotoLines()
    w, so, box = m.w, m.so, m.box
    y = m.items[1]
    t = so.photos.save_photo(JPEG + b'erase')[0]
    n = w.save_product({'sku': 'DUCK-N', 'title': 'Wooden duck', 'default_cost_cents': 300})['id']
    assert so.photos.attach(t, n, 1)
    w.delete_product(n, 1)
    chk = so.add_check(box, None, y, None)
    opt = next(o for o in chk['options'] if o.get('kind') == 'new')
    r = so.attach_photo(so.put(box, 1, scan_id='e1', product_id=y, over_ok=True, option=opt), t, 1)
    assert r['kind'] == 'added'
    assert len(live(w, y)) == 1, 'the photo of an erased wrong item never reaches the right item'
