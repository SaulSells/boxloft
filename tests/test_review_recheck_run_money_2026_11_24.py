import os, sys
REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))
from test_amazon_approval import srv, _boxes, _product, approval_mod  # noqa: F401,E402


def test_lost_run_answer_is_gone_after_another_persons_run(srv, monkeypatch):
    """c8: Alex's s1 ran (Amazon was asked), then Sam's n1 ran. Alex's phone polls for s1: the server keeps no trace
    of s1, so waitForRun returns null and the app says 'Amazon was not asked', which is false."""
    monkeypatch.setattr(approval_mod, 'PACE', 0)
    server, call, calls, uid = srv
    a, e = _boxes(server, uid)
    server.wms.receive(uid, _product(server, 'BRANDED', 'B0BRANDED1', '012345678905'), 1, 100, {'box_id': a['id']})
    server.approval.refresh()
    st, _ = call('/api/amazon/approval/recheck', {'run': 's1'})
    assert st == 200
    st, _ = call('/api/amazon/approval/recheck', {'run': 'n1'})
    assert st == 200
    st, s = call('/api/amazon/approval/recheck')
    # The poll for s1 must be able to tell that s1 ran (its answer, or at least that it finished).
    assert s.get('run') == 's1' or 's1' in str(s), s
