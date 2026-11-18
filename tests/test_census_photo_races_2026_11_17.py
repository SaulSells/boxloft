"""Census 11-17, people angle: the box page's photo pick (/api/put, under op_lock) and the photo-id screen's attach
(/api/photo-id/attach, outside op_lock) each checked "the item has no photo", then wrote. Both passed, and the item
got 2 live photos, or one photo file went on 2 items. The reviewer's proofs interleaved them by patching internals;
these run the two saves on 2 real threads, many times. PhotoId.claim now checks and writes in one transaction."""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from test_photo_line_model import JPEG, PhotoLines  # noqa: E402

ROUNDS = 40


def live(w, pid):
    return w._q('SELECT id, path FROM photo WHERE product_id = ? AND removed = 0', (pid,))


def pick(so, box, pid, sid, token):
    r = so.put(box, 1, scan_id=sid, product_id=pid, over_ok=True)
    if r.get('kind') != 'added':
        r = so.put(box, 1, scan_id=sid, product_id=pid, over_ok=True, option={'kind': 'new', 'product_id': pid, 'cost_cents': None})
    return so.attach_photo(r, token, 1)


def race(a, b):
    go = threading.Barrier(2)
    errs = []

    def run(f):
        try:
            go.wait()
            f()
        except Exception as e:   # noqa: BLE001
            errs.append(e)
        finally:
            m.w.release()
    ts = [threading.Thread(target=run, args=(f,)) for f in (a, b)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs, errs


m = None


def test_pick_and_photo_id_attach_race_gives_item_one_photo():
    global m
    for i in range(ROUNDS):
        m = PhotoLines()
        w, so, box = m.w, m.so, m.box
        x = m.items[0]
        t1 = so.photos.save_photo(JPEG + bytes([i]))[0]
        t2 = so.photos.save_photo(JPEG + b'x' + bytes([i]))[0]
        race(lambda: (so.wms.op_lock.acquire(), pick(so, box, x, 's1', t1), so.wms.op_lock.release()),
             lambda: so.photos.attach(t2, x, 2))
        assert len(live(w, x)) == 1, live(w, x)


def test_one_photo_file_never_on_two_items():
    global m
    for i in range(ROUNDS):
        m = PhotoLines()
        w, so, box = m.w, m.so, m.box
        x, y = m.items[0], m.items[1]
        t = so.photos.save_photo(JPEG + bytes([i]))[0]
        race(lambda: (so.wms.op_lock.acquire(), pick(so, box, x, 's1', t), so.wms.op_lock.release()),
             lambda: so.photos.attach(t, y, 2))
        rel = so.photos.path_for(t)
        assert len(w._q('SELECT id FROM photo WHERE path = ? AND removed = 0', (rel,))) == 1


def test_freed_photo_move_and_photo_id_attach_race_gives_item_one_photo():
    global m
    for i in range(ROUNDS):
        m = PhotoLines()
        w, so, box = m.w, m.so, m.box
        x, y = m.items[0], m.items[1]
        t1 = so.photos.save_photo(JPEG + bytes([i]))[0]
        t2 = so.photos.save_photo(JPEG + b'x' + bytes([i]))[0]
        a = pick(so, box, x, 's1', t1)
        so.undo_add(a['add_id'], 1)
        race(lambda: (so.wms.op_lock.acquire(), pick(so, box, y, 's2', t1), so.wms.op_lock.release()),
             lambda: so.photos.attach(t2, y, 2))
        assert len(live(w, y)) == 1, live(w, y)
