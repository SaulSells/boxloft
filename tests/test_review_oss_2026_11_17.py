"""Deploy review 11-17 of the open-source export tooling: 2 proven bugs, each fixed with its test."""
import os
import shutil
import sqlite3
import subprocess
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(REPO, 'backend'))


def test_setup_demo_on_a_used_database_leaves_an_admin_with_no_link(tmp_path):
    """setup.py --demo made the admin first, then seed_demo exited on a non-empty warehouse: the admin row was
    written but its one-use link never printed, and a rerun said the user already exists."""
    from wms import Wms
    w = Wms(str(tmp_path / 'wms.db'))
    w.save_product({'sku': 'X1', 'title': 'Already here'})
    env = {**os.environ, 'BOXLOFT_DATA': str(tmp_path)}
    r = subprocess.run([sys.executable, os.path.join(REPO, 'scripts', 'setup.py'), 'boss', 'Boss', '--demo'],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode != 0 and 'already has products' in r.stderr, (r.stdout, r.stderr)
    n = sqlite3.connect(str(tmp_path / 'auth.db')).execute(
        "SELECT COUNT(*) FROM users WHERE username = 'boss'").fetchone()[0]
    assert n == 0   # a refused setup leaves no user whose setup link nobody saw


def test_setup_demo_with_a_box_named_a1_leaves_admin_without_link_and_half_a_demo(tmp_path):
    """Deploy review round 3 (11-17): no products yet, but a box A1 exists (made on the Boxes page). setup.py --demo
    passed its 'empty' check, made the admin, then seed_demo died on the duplicate box name: the admin existed with
    no link printed, and the demo's 3PL owner stayed written, so every later seed-demo refused."""
    import sqlite3
    sys.path.insert(0, os.path.join(REPO, 'backend'))
    from wms import Wms
    w = Wms(str(tmp_path / 'wms.db'))
    w.create_boxes(1, 1, None, 'A1')
    env = {**os.environ, 'BOXLOFT_DATA': str(tmp_path)}
    r = subprocess.run([sys.executable, os.path.join(REPO, 'scripts', 'setup.py'), 'boss', 'Boss', '--demo'],
                       capture_output=True, text=True, env=env, timeout=60)
    users = sqlite3.connect(str(tmp_path / 'auth.db')).execute(
        "SELECT COUNT(*) FROM users WHERE username = 'boss'").fetchone()[0]
    owners = sqlite3.connect(str(tmp_path / 'wms.db')).execute('SELECT COUNT(*) FROM owner').fetchone()[0]
    linked = 'setup?token=' in r.stdout
    assert linked or users == 0, ('admin made but its one-use link never printed', r.stdout, r.stderr[-400:])
    assert linked or owners == 1, 'a refused demo left its 3PL owner written'
    assert 'already has boxes' in r.stderr
