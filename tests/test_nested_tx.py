"""Wms._tx nested in a _tx of the same thread is a SAVEPOINT (review round 9, 11-17): an error inside rolls back only
its own writes, and an error in the outer _tx rolls back everything, the inner writes included."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
from wms import Wms, WmsError  # noqa: E402


def _names(w):
    return {r['title'] for r in w._q('SELECT title FROM t')}


def _wms(tmp_path):
    w = Wms(str(tmp_path / 'w.db'))
    with w._tx() as c:
        c.execute('CREATE TABLE t (title TEXT)')
    return w


def test_inner_error_rolls_back_only_the_inner_writes(tmp_path):
    w = _wms(tmp_path)
    with w._tx() as c:
        c.execute("INSERT INTO t (title) VALUES ('outer')")
        with pytest.raises(WmsError):
            with w._tx() as c2:
                c2.execute("INSERT INTO t (title) VALUES ('inner')")
                raise WmsError('refused')
        with w._tx() as c3:
            c3.execute("INSERT INTO t (title) VALUES ('inner ok')")
    assert {'outer', 'inner ok'} <= _names(w) and 'inner' not in _names(w)


def test_outer_error_rolls_back_the_inner_writes_too(tmp_path):
    w = _wms(tmp_path)
    seq = w.write_seq
    with pytest.raises(WmsError):
        with w._tx():
            with w._tx() as c:
                c.execute("INSERT INTO t (title) VALUES ('inner')")
            raise WmsError('refused')
    assert 'inner' not in _names(w)
    assert w.write_seq > seq   # a cache filled inside the rolled-back transaction is dropped
    with w._tx() as c:   # the lock and the depth were given back
        c.execute("INSERT INTO t (title) VALUES ('after')")
    assert 'after' in _names(w)
