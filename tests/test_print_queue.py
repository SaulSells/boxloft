"""Alex 11-11: phones send labels to a queue; the computer with the printer prints them. A job prints once even if
two computers (or Alex and Sam) press Print at the same moment."""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
from wms import Wms, WmsError  # noqa: E402
from printq import PrintQueue  # noqa: E402

BOX = {'kind': 'box', 'code': 'SB00000018', 'big': 'BOX 1', 'lines': ['Blue tote', 'Home: A1']}


def make(tmp_path):
    w = Wms(os.path.join(tmp_path, 'wms.db'))
    return w, PrintQueue(w)


def test_phone_job_waits_then_prints_once(tmp_path):
    _, pq = make(tmp_path)
    j = pq.add(2, [BOX], "Sam's phone")
    assert j['title'] == 'BOX 1' and j['n'] == 1 and j['labels'][0]['lines'] == ['Blue tote', 'Home: A1']
    pq.add(2, [{'kind': 'location', 'code': f'SL{i:08d}', 'big': f'A{i}'} for i in range(1, 4)])
    q = pq.list()
    assert [x['title'] for x in q['waiting']] == ['BOX 1', '3 bin labels'] and q['labels_waiting'] == 4
    got = pq.claim([x['id'] for x in q['waiting']], 1, 'Packing desk')
    assert [x['n'] for x in got] == [1, 3]
    assert pq.claim([x['id'] for x in q['waiting']], 1) == []        # already printed: nothing to print again
    q = pq.list()
    assert q['waiting'] == [] and q['done'][0]['printed_device'] == 'Packing desk'


def test_two_computers_press_print_at_once(tmp_path):
    _, pq = make(tmp_path)
    ids = [pq.add(2, [BOX])['id'] for _ in range(5)]
    out, bar = [], threading.Barrier(2)

    def run(user):
        bar.wait()
        out.append(pq.claim(ids, user))
    ts = [threading.Thread(target=run, args=(u,)) for u in (1, 2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(j['id'] for got in out for j in got) == ids      # every job once, none twice


def test_remove_and_put_back(tmp_path):
    _, pq = make(tmp_path)
    j = pq.add(2, [BOX])
    pq.remove(j['id'], 2)
    assert pq.list()['waiting'] == [] and pq.claim([j['id']], 1) == []
    pq.back(j['id'])
    assert [x['id'] for x in pq.list()['waiting']] == [j['id']]
    pq.claim([j['id']], 1)
    with pytest.raises(WmsError):
        pq.remove(j['id'], 2)                                        # printed: nothing to remove


def test_bad_labels_refused(tmp_path):
    _, pq = make(tmp_path)
    for bad in ([], [{'kind': 'box'}], [{'kind': 'shoe', 'code': 'X'}], 'BOX 1', [BOX] * 501):
        with pytest.raises(WmsError):
            pq.add(1, bad)
