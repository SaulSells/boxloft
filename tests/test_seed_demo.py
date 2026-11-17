"""manage.py seed-demo fills an empty warehouse and the ledger checks out (it failed on named boxes before)."""
import os
import subprocess
import sys

ROOT = os.path.join(os.path.dirname(__file__), '..')


def test_seed_demo_on_an_empty_database(tmp_path):
    env = {**os.environ, 'BOXLOFT_DATA': str(tmp_path)}
    r = subprocess.run([sys.executable, os.path.join(ROOT, 'scripts', 'manage.py'), 'seed-demo'],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert '"ok": true' in r.stdout and '"problem_count": 0' in r.stdout
    r = subprocess.run([sys.executable, os.path.join(ROOT, 'scripts', 'manage.py'), 'seed-demo'],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode != 0 and 'already has products' in r.stderr
