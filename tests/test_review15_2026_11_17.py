import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

from merge import Merge  # noqa: E402
from test_sorting import setup  # noqa: E402

Merge.enabled = True


def test_merge_drops_shipped_extras_from_extras_list(tmp_path):
    """Extras that already left (shipped) were extras; merging their item into another must not erase them from the
    Stock audit extras list (before the merge the list showed 2 units, after it 1)."""
    w, au, so, cat, inbound, boxes = setup(tmp_path)
    m = Merge(w, au.commerce, au)
    s = w.save_product({'sku': 'S-1', 'title': 'Mug'})['id']
    so.add(boxes['A1'], {'kind': 'new', 'product_id': s}, 1, 1, cost_cents=400)
    so.add(boxes['A1'], {'kind': 'new', 'product_id': s}, 1, 1, cost_cents=400)
    w.remove(1, boxes['A1'], [{'product_id': s, 'qty': 1}], 'CUSTOMER')   # one of the two extras shipped
    before = so.extras()['units']
    assert before == 2
    t = w.save_product({'sku': 'T-1', 'title': 'Mug (dup)'})['id']
    m.run(s, {'product_id': t}, 1, seen={'on_hand': 1})
    after = so.extras()['units']
    assert after == before, f'extras list went from {before} to {after} units on a merge that found nothing'
