"""First run on a new server: make the data folder, the first admin and their one-use setup link.

    python3 scripts/setup.py <username> "<Display Name>" [--demo]

The data folder is BOXLOFT_DATA (default ./data). The link starts with BOXLOFT_PUBLIC_URL, the address people
open. The admin opens it, sets a password, and adds the other people in Settings -> People.
--demo also fills the empty warehouse with sample boxes, items and moves (manage.py seed-demo).
Run it again for another admin: an existing username stops it, nothing is overwritten."""
import argparse
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'backend'))

import config  # noqa: E402
from auth import Auth, AuthError  # noqa: E402
from wms import Wms  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('username')
    ap.add_argument('display_name')
    ap.add_argument('--demo', action='store_true', help='add sample data to an empty warehouse')
    a = ap.parse_args()
    data = os.path.abspath(os.environ.get('BOXLOFT_DATA', os.path.join(ROOT, 'data')))
    os.makedirs(data, exist_ok=True)
    os.chmod(data, 0o700)
    w = Wms(os.path.join(data, 'wms.db'))   # makes the schema
    auth = Auth(os.path.join(data, 'auth.db'))
    if auth._conn().execute('SELECT 1 FROM users WHERE username = ?', (a.username.lower(),)).fetchone():
        sys.exit(f'{a.username} already exists. For a new link: python3 scripts/manage.py reset-user {a.username}')
    if a.demo:   # check before making the user (reviews 11-17), with the same rule seed_demo uses
        sys.path.insert(0, os.path.join(ROOT, 'scripts'))
        from manage import demo_blocker
        why = demo_blocker(w)
        if why:
            sys.exit(f'{why}; --demo only fills an empty one. Run it without --demo.')
    try:
        token = auth.create_user(a.username, a.display_name, 'admin')
    except AuthError as e:
        sys.exit(str(e))
    if a.demo:
        from manage import seed_demo
        check = seed_demo(w)
        n = w._q1('SELECT COUNT(*) n FROM box')['n']
        print(f"Sample warehouse: {n} boxes with items in them. Ledger check: {'ok' if check['ok'] else 'PROBLEM'}")
    print(f'Data folder: {data}')
    print(f'{a.display_name} is an admin. Open this link within 48 hours to set a password:')
    print(f'  {config.PUBLIC_URL.rstrip("/")}/setup?token={token}')
    print(f'Your username is {a.username.lower()}.')


if __name__ == '__main__':
    main()
