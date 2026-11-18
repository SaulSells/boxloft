import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

from test_card_listings import with_listings  # noqa: E402
from test_box_line_matrix import JPEG, photos_for  # noqa: E402
from wms import now_utc  # noqa: E402


def test_c25_old_data_two_rows_one_file_freed_newest_goes_on_a_second_item(tmp_path):
    """Old data: main's pre-7bced72 race (box page attach under op_lock vs the unlocked /api/photo-id/attach) could
    write 2 photo rows for one file: an older one live on X and a newer one on Y. An Undo of Y's line frees the newer
    row. PhotoId.claim reads only the newest row of the path (ORDER BY id DESC LIMIT 1), sees it freed, and moves it
    onto Z, never checking the older row: the one file is now live on X and on Z."""
    w, so, boxes, _ = with_listings(tmp_path)
    photos_for(w, so)
    x = w.save_product({'sku': 'X1', 'title': 'Blue mug', 'default_cost_cents': 300})['id']
    y = w.save_product({'sku': 'Y1', 'title': 'Green bowl', 'default_cost_cents': 300})['id']
    z = w.save_product({'sku': 'Z1', 'title': 'Red cup', 'default_cost_cents': 300})['id']
    tok, _ = so.photos.save_photo(JPEG)
    rel = so.photos.path_for(tok)
    with w._tx() as c:   # the old race's leftover: the same file, live on X
        c.execute("INSERT INTO photo (product_id, unit_id, path, user_id, created_at, source) VALUES (?, NULL, ?, 1, ?, 'photo-id')",
                  (x, rel, now_utc()))
    with w._tx() as c:   # ... and on Y, whose line was then undone (freed)
        c.execute("INSERT INTO photo (product_id, unit_id, path, user_id, created_at, source) VALUES (?, NULL, ?, 1, ?, 'photo-id')",
                  (y, rel, now_utc()))
        newest = c.execute('SELECT MAX(id) FROM photo').fetchone()[0]
        c.execute('UPDATE photo SET removed = 1 WHERE id = ?', (newest,))
        c.execute('INSERT INTO photo_freed (photo_id) VALUES (?)', (newest,))
    so.photos.attach(tok, z, 2)   # the photo-id screen: a person taps Z
    n = w._q1('SELECT COUNT(*) AS n FROM photo WHERE path = ? AND removed = 0', (rel,))['n']
    assert n == 1, f'one photo file is live on {n} items'
