import os, sys
REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

import approval as approval_mod  # noqa: E402
from test_amazon_approval import srv, _boxes, _product, ANSWERS  # noqa: E402,F401


def _gated(server, uid):
    a, e = _boxes(server, uid)
    server.wms.receive(uid, _product(server, 'BRANDED', 'B0BRANDED1', '012345678905'), 1, 100, {'box_id': a['id']})
    server.approval.refresh()


def test_lost_answer_is_overwritten_by_another_persons_run(srv, monkeypatch):
    """Alex's phone loses the POST of run r1 (it finished on the server). Before his poll reads it, Sam presses Check
    again (r2). The state holds only the newest run, so r1's answer is gone and his app says "Amazon was not asked"
    although Amazon was asked and the answer saved."""
    monkeypatch.setattr(approval_mod, 'PACE', 0)
    server, call, calls, uid = srv
    _gated(server, uid)
    st, r1 = call('/api/amazon/approval/recheck', {'asins': ['B0BRANDED1'], 'run': 'r1'})
    assert st == 200
    st, r2 = call('/api/amazon/approval/recheck', {'asins': ['B0BRANDED1'], 'run': 'r2'})
    assert st == 200
    st, s = call('/api/amazon/approval/recheck?run=r1')
    assert s.get('run') == 'r1' and s.get('result') == r1, s
