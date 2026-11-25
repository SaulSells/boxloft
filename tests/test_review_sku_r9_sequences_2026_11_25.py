import os, sys, sqlite3
REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(REPO, 'tests')); sys.path.insert(0, os.path.join(REPO, 'backend'))

import pytest  # noqa: E402
from commerce import Commerce  # noqa: E402
from listings import Listings  # noqa: E402
from wms import Wms, WmsError  # noqa: E402
from test_sell_sku import setup, item, listing  # noqa: E402

OLD_SELL_SKU = """CREATE TABLE sell_sku (
    id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL, base TEXT NOT NULL, sku TEXT NOT NULL UNIQUE,
    day TEXT NOT NULL, cost_cents INTEGER NOT NULL, ref TEXT NOT NULL DEFAULT '', user_id INTEGER, at TEXT NOT NULL,
    UNIQUE (product_id, base));"""


def test_c2_old_table_no_price_copy_is_not_a_500(tmp_path):
    # A wms.db made by b09db53 (sell_sku.cost_cents NOT NULL): CREATE TABLE IF NOT EXISTS keeps the old column.
    db = str(tmp_path / 'w.db')
    w = Wms(db)
    w._conn().executescript(OLD_SELL_SKU)
    li = Listings(w, Commerce(w))
    pid = item(w, 'NP1', 'B0NOPRICE1')
    try:
        s = li.save_sell_sku(pid, 1)
    except WmsError:
        return  # a 400 in words is fine
    except sqlite3.IntegrityError as e:
        pytest.fail(f'Copy on an item with no price is a 500 on an older sell_sku table: {e}')
    assert s['saved']


def test_c32_item_with_odd_created_at_page_loads(tmp_path):
    w, li, _ = setup(tmp_path)
    pid = item(w, 'ODD', 'B0ODDDATE1')
    c = w._conn()
    try:
        c.execute('DROP TRIGGER IF EXISTS product_no_update')
    except Exception:
        pass
    c.execute("UPDATE product SET created_at = 'imported' WHERE id = ?", (pid,))
    c.commit()
    try:
        s = li.sell_sku(pid)
    except (ValueError, TypeError) as e:
        pytest.fail(f'item page crashes on a non-ISO created_at: {e!r}')
    assert s and s['sku']
