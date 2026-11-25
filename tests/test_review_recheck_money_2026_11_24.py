import os, sys
REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

from test_amazon_approval import srv, _product, _boxes, approval_mod  # noqa: F401,E402


def test_cap_drops_valid_asins_after_junk(srv, monkeypatch):
    """c13: the server cuts the list to 200 BEFORE recheck drops junk, so 200 junk entries + 50 real ASINs ask
    Amazon nothing and the answer says asked 0 with no word that 50 valid ASINs were dropped."""
    monkeypatch.setattr(approval_mod, 'PACE', 0)
    server, call, calls, uid = srv
    real = ['B0REAL%04d' % i for i in range(50)]
    st, r = call('/api/amazon/approval/recheck', {'asins': ['junk'] * 0 + ['x%d' % i for i in range(200)] + real})
    assert st == 200
    assert r['asked'] == 50 or r.get('dropped'), r


def test_403_every_try_leaves_no_error(srv, monkeypatch):
    """c18: Amazon answers 403 on every try of Check again: counted failed, cache keeps its answer, lock released,
    but last_error stays empty, so nothing says why Amazon gave no answer."""
    monkeypatch.setattr(approval_mod, 'PACE', 0)
    monkeypatch.setattr(approval_mod.time, 'sleep', lambda s: None)
    server, call, calls, uid = srv
    monkeypatch.setattr(approval_mod, '_http', lambda *a, **k: (403, {'errors': [{'message': 'Unauthorized'}]}))
    server.approval.last_error = ''
    st, r = call('/api/amazon/approval/recheck', {'asins': ['B0BRANDED1']})
    assert st == 200 and r['failed'] == 1
    assert server.approval._recheck_lock.acquire(blocking=False)
    server.approval._recheck_lock.release()
    assert server.approval.last_error, 'a 403 on every try leaves last_error empty'
