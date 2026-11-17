import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

import pytest  # noqa: E402

from merge import Merge  # noqa: E402
from test_sorting import ASIN, setup  # noqa: E402
from test_merge import made_by_scan, left  # noqa: E402
from wms import Conflict  # noqa: E402

Merge.enabled = True


def test_merge_from_stale_screen_after_row_was_found_is_refused(tmp_path):
    """Alex opens "Same item as" on the scanned item: the confirm screen says the Stock audit row counts its 3 units as
    found. Before he taps Merge, Sam finds the row's 3 units (another copy). Alex's save still runs: the row has
    nothing left, so the 3 units go in as extra stock on another item and the source is archived, though the screen
    he confirmed promised something else. on_hand is the only seen value, so no 409."""
    w, au, so, cat, inbound, boxes = setup(tmp_path, expected=3)
    m = Merge(w, au.commerce, au)
    row = w._q1('SELECT id FROM audit_item WHERE asin = ?', (ASIN,))['id']
    pid = made_by_scan(w, boxes['A1'])
    pre = m.preview(pid, {'audit_id': row})
    assert pre['to_row'] == 3 and pre['target_id'] == pid
    au.found(row, 3, boxes['A2'], 2)            # Sam, from her phone, meanwhile
    assert left(au, row) == 0
    with pytest.raises(Conflict):
        m.run(pid, {'audit_id': row}, 1, seen={'on_hand': pre['source']['on_hand']})
