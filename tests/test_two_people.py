"""Alex 11-11: Alex and Sam work the same items at once. A save made from a screen that showed other numbers is
refused with who changed the row; an up-to-date save goes through. (The HTTP race harness covers the lock.)"""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
sys.path.insert(0, os.path.dirname(__file__))

from wms import Conflict  # noqa: E402
from test_audit import setup  # noqa: E402


def seen(it):
    return {'accounted': it['accounted'], 'expected': it['expected'], 'closed': it['closed']}


def test_a_save_from_an_old_screen_is_refused_with_who_and_what_is_left(tmp_path):
    w, au, iid, box = setup(tmp_path)                # 7 expected
    screen = seen(au.item(iid))                      # both screens show 0 of 7
    au.check_seen(iid, screen, {2: 'Sam'})           # nothing changed yet: goes through
    au.found(iid, 7, {'box_id': box}, 2)             # Sam finds all 7
    with pytest.raises(Conflict) as e:
        au.check_seen(iid, screen, {2: 'Sam'})       # Alex saves from his old screen
    msg = str(e.value)
    assert 'Sam found 7 at' in msg and 'Now 0 of 7 left' in msg and 'Nothing was saved' in msg
    au.check_seen(iid, seen(au.item(iid)), {2: 'Sam'})   # after the reload it goes through
    au.check_seen(iid, None)                         # old clients that send nothing are not checked


def test_closing_is_refused_after_the_other_person_closed_it(tmp_path):
    w, au, iid, box = setup(tmp_path)
    screen = seen(au.item(iid))
    au.close(iid, 'not_found', 2)
    with pytest.raises(Conflict, match='It is closed now'):
        au.check_seen(iid, screen, {2: 'Sam'})


def test_the_op_lock_is_reentrant_and_shared(tmp_path):
    w, au, iid, box = setup(tmp_path)
    with w.op_lock:
        with w.op_lock:                               # a locked route can call autolink_sales (locks again)
            au.autolink_sales()
    got = []
    with w.op_lock:                                   # another thread waits while a write request holds it
        t = threading.Thread(target=lambda: got.append(w.op_lock.acquire(timeout=0.2)))
        t.start()
        t.join()
    assert got == [False]
