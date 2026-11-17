"""Users, sessions and sign-in for Boxloft.

Built from an earlier dashboard's auth_manager.py, with these changes:
- Sign in with a username (or email). Warehouse logins should be short to type.
- Lockout counts failures per (username, IP), plus a per-IP cap. A stranger
  guessing at "sam" from elsewhere can no longer lock Sam out of the floor.
- Sessions slide: 30 days from last use, so a warehouse phone stays signed in.
  Each session records its device, and a user can sign out other devices.
- Setup links (one use, 48 h) replace emailed invites; an admin hands the link over.
- Roles are two: admin (manages users) and staff.

Passwords: PBKDF2-SHA256, 600k iterations, 32-byte salt. Session and setup
tokens are stored only as SHA-256 hashes.
"""

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import dbpool

SESSION_COOKIE = 'boxloft_session'
SESSION_DAYS = 30
SETUP_HOURS = 48
PBKDF2_ITERATIONS = 600_000
LOCK_WINDOW_S = 900
LOCK_PER_USER_IP = 5
LOCK_PER_IP = 20
MIN_PASSWORD = 10
ROLES = ('admin', 'staff')


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt: datetime) -> str:
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, '%Y-%m-%dT%H:%M:%SZ')


def hash_password(password: str) -> str:
    salt = secrets.token_hex(32)
    h = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), PBKDF2_ITERATIONS)
    return f'pbkdf2${PBKDF2_ITERATIONS}${salt}${h.hex()}'


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, expected = stored.split('$')
        h = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), int(iterations))
        return hmac.compare_digest(h.hex(), expected)
    except (ValueError, AttributeError):
        return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# Spend the same time on unknown usernames as on real ones, so timing never says which exist.
_DUMMY_HASH = hash_password(secrets.token_hex(8))


class AuthError(Exception):
    """Shown to the user as is."""


class Auth:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()
        self._lock = threading.Lock()
        self._idle = []
        self._pool_lock = threading.Lock()
        self._conn().executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT UNIQUE NOT NULL COLLATE NOCASE,
                email TEXT COLLATE NOCASE,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'staff',
                password_hash TEXT,
                setup_token TEXT,
                setup_expires_at TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_login_at TEXT
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                ip TEXT,
                user_agent TEXT
            );
            CREATE TABLE IF NOT EXISTS login_failures (
                username TEXT NOT NULL,
                ip TEXT NOT NULL,
                at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_fail_at ON login_failures(at);
            CREATE TABLE IF NOT EXISTS api_tokens (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                token TEXT UNIQUE NOT NULL,
                user_id INTEGER NOT NULL REFERENCES users(id),
                scope TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT,
                revoked INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS auth_log (
                id INTEGER PRIMARY KEY,
                at TEXT NOT NULL,
                user_id INTEGER,
                event TEXT NOT NULL,
                detail TEXT,
                ip TEXT
            );
        """)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            with self._pool_lock:
                conn = self._idle.pop() if self._idle else None
            if conn is None:
                conn = sqlite3.connect(self.db_path, check_same_thread=False)
                conn.execute('PRAGMA journal_mode=WAL')
                conn.execute('PRAGMA busy_timeout=5000')
            self._local.conn = conn
        return conn

    def release(self):
        """End of a request: this thread's connection goes back to the idle pool (closed past dbpool.POOL_IDLE).
        11-11 night: per-thread connections were never closed. On Python 3.12 only the cycle collector frees a
        sqlite3 connection, so finished threads left theirs open, and idle keep-alive threads held theirs."""
        dbpool.release(self._local, self._idle, self._pool_lock)

    def _write(self, sql: str, params=()):
        with self._lock:
            conn = self._conn()
            cur = conn.execute(sql, params)
            conn.commit()
            return cur

    def log(self, event: str, user_id: int = None, detail: str = '', ip: str = ''):
        self._write('INSERT INTO auth_log (at, user_id, event, detail, ip) VALUES (?,?,?,?,?)',
                    (_iso(_utcnow()), user_id, event, detail, ip))

    @staticmethod
    def public(row) -> dict:
        uid, username, email, display_name, role = row[:5]
        return {'id': uid, 'username': username, 'email': email, 'display_name': display_name, 'role': role,
                'is_admin': role == 'admin'}

    # ------------------------------------------------------------------ users

    def create_user(self, username: str, display_name: str, role: str = 'staff', email: str = '') -> str:
        """Create a user with no password yet. Returns a one-use setup token."""
        username = (username or '').strip().lower()
        if not username.isalnum() or len(username) > 32:
            raise AuthError('Username must be letters and numbers only')
        if role not in ROLES:
            raise AuthError('Role must be admin or staff')
        token = secrets.token_urlsafe(32)
        try:
            self._write("""INSERT INTO users (username, email, display_name, role, setup_token, setup_expires_at, created_at)
                           VALUES (?,?,?,?,?,?,?)""",
                        (username, email.strip() or None, display_name.strip() or username, role, _hash_token(token),
                         _iso(_utcnow() + timedelta(hours=SETUP_HOURS)), _iso(_utcnow())))
        except sqlite3.IntegrityError:
            raise AuthError(f'User {username} already exists')
        return token

    def reset_setup(self, user_id: int) -> str:
        """New setup link for a user; their password and sessions stop working."""
        token = secrets.token_urlsafe(32)
        cur = self._write("""UPDATE users SET password_hash = NULL, setup_token = ?, setup_expires_at = ? WHERE id = ?""",
                          (_hash_token(token), _iso(_utcnow() + timedelta(hours=SETUP_HOURS)), user_id))
        if not cur.rowcount:
            raise AuthError('User not found')
        self._write('DELETE FROM sessions WHERE user_id = ?', (user_id,))
        return token

    def list_users(self) -> list:
        rows = self._conn().execute("""SELECT id, username, email, display_name, role, is_active, last_login_at,
                                              password_hash IS NOT NULL, setup_expires_at FROM users ORDER BY id""").fetchall()
        out = []
        for r in rows:
            u = self.public(r)
            u.update({'is_active': bool(r[5]), 'last_login_at': r[6], 'has_password': bool(r[7]),
                      'setup_pending': not r[7] and bool(r[8]) and _parse(r[8]) > _utcnow()})
            out.append(u)
        return out

    def user_names(self) -> dict:
        return {r[0]: r[1] for r in self._conn().execute('SELECT id, display_name FROM users')}

    def set_active(self, user_id: int, active: bool):
        self._write('UPDATE users SET is_active = ? WHERE id = ?', (1 if active else 0, user_id))
        if not active:
            self._write('DELETE FROM sessions WHERE user_id = ?', (user_id,))

    def set_role(self, user_id: int, role: str):
        if role not in ROLES:
            raise AuthError('Role must be admin or staff')
        self._write('UPDATE users SET role = ? WHERE id = ?', (role, user_id))

    def setup_info(self, token: str):
        row = self._conn().execute("""SELECT id, username, email, display_name, role, setup_expires_at FROM users
                                       WHERE setup_token = ? AND is_active = 1""", (_hash_token(token or ''),)).fetchone()
        if not row or not row[5] or _parse(row[5]) < _utcnow():
            return None
        return self.public(row)

    def complete_setup(self, token: str, password: str, ip: str = '') -> dict:
        user = self.setup_info(token)
        if not user:
            raise AuthError('This setup link has expired or was already used. Ask an admin for a new one.')
        self._check_password_rules(password, user['username'])
        self._write("""UPDATE users SET password_hash = ?, setup_token = NULL, setup_expires_at = NULL WHERE id = ?""",
                    (hash_password(password), user['id']))
        self.log('setup_completed', user['id'], ip=ip)
        return user

    @staticmethod
    def _check_password_rules(password: str, username: str):
        if len(password or '') < MIN_PASSWORD:
            raise AuthError(f'Use at least {MIN_PASSWORD} characters')
        if username and username.lower() in password.lower():
            raise AuthError('The password cannot contain your username')

    def change_password(self, user_id: int, current: str, new: str, keep_token: str = None):
        row = self._conn().execute('SELECT username, password_hash FROM users WHERE id = ?', (user_id,)).fetchone()
        if not row or not verify_password(current or '', row[1] or ''):
            raise AuthError('Current password is wrong')
        self._check_password_rules(new, row[0])
        self._write('UPDATE users SET password_hash = ? WHERE id = ?', (hash_password(new), user_id))
        # Other devices sign out; this one stays.
        self._write('DELETE FROM sessions WHERE user_id = ? AND token != ?', (user_id, _hash_token(keep_token or '')))
        self.log('password_changed', user_id)

    # ------------------------------------------------------------------ sign in

    def _failures(self, username: str, ip: str) -> tuple:
        cutoff = time.time() - LOCK_WINDOW_S
        c = self._conn()
        per_user = c.execute('SELECT COUNT(*) FROM login_failures WHERE username = ? AND ip = ? AND at > ?',
                             (username, ip, cutoff)).fetchone()[0]
        per_ip = c.execute('SELECT COUNT(*) FROM login_failures WHERE ip = ? AND at > ?', (ip, cutoff)).fetchone()[0]
        return per_user, per_ip

    def authenticate(self, login: str, password: str, ip: str) -> dict:
        login = (login or '').strip().lower()
        per_user, per_ip = self._failures(login, ip)
        if per_user >= LOCK_PER_USER_IP or per_ip >= LOCK_PER_IP:
            time.sleep(0.5)
            raise AuthError('Too many tries. Wait 15 minutes, then try again.')
        row = self._conn().execute("""SELECT id, username, email, display_name, role, password_hash, is_active
                                        FROM users WHERE username = ? OR email = ?""", (login, login)).fetchone()
        ok = verify_password(password or '', row[5] if row and row[5] else _DUMMY_HASH)
        if not row or not row[6] or not row[5] or not ok:
            self._write('INSERT INTO login_failures (username, ip, at) VALUES (?,?,?)', (login, ip, time.time()))
            self.log('login_failed', row[0] if row else None, login, ip)
            time.sleep(0.5)
            left = LOCK_PER_USER_IP - per_user - 1
            hint = f' {left} {"try" if left == 1 else "tries"} left.' if 0 < left <= 2 else ''
            raise AuthError('Wrong username or password.' + hint)
        self._write('DELETE FROM login_failures WHERE username = ? AND ip = ?', (login, ip))
        self._write('UPDATE users SET last_login_at = ? WHERE id = ?', (_iso(_utcnow()), row[0]))
        self.log('login', row[0], ip=ip)
        return self.public(row)

    def create_session(self, user_id: int, ip: str, user_agent: str) -> str:
        token = secrets.token_urlsafe(32)
        now = _utcnow()
        self._write("""INSERT INTO sessions (token, user_id, created_at, last_seen_at, expires_at, ip, user_agent)
                       VALUES (?,?,?,?,?,?,?)""",
                    (_hash_token(token), user_id, _iso(now), _iso(now), _iso(now + timedelta(days=SESSION_DAYS)),
                     ip, (user_agent or '')[:300]))
        return token

    def validate(self, token: str):
        if not token:
            return None
        th = _hash_token(token)
        row = self._conn().execute("""SELECT u.id, u.username, u.email, u.display_name, u.role, u.is_active,
                                             s.expires_at, s.last_seen_at
                                        FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?""",
                                   (th,)).fetchone()
        if not row:
            return None
        now = _utcnow()
        if not row[5] or _parse(row[6]) < now:
            self._write('DELETE FROM sessions WHERE token = ?', (th,))
            return None
        if now - _parse(row[7]) > timedelta(hours=1):
            self._write('UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE token = ?',
                        (_iso(now), _iso(now + timedelta(days=SESSION_DAYS)), th))
        return self.public(row)

    def logout(self, token: str):
        self._write('DELETE FROM sessions WHERE token = ?', (_hash_token(token or ''),))

    def list_sessions(self, user_id: int, current_token: str) -> list:
        cur = _hash_token(current_token or '')
        rows = self._conn().execute("""SELECT token, created_at, last_seen_at, ip, user_agent FROM sessions
                                        WHERE user_id = ? ORDER BY last_seen_at DESC""", (user_id,)).fetchall()
        return [{'id': r[0][:12], 'created_at': r[1], 'last_seen_at': r[2], 'ip': r[3], 'user_agent': r[4],
                 'current': r[0] == cur} for r in rows]

    def logout_others(self, user_id: int, current_token: str) -> int:
        cur = self._write('DELETE FROM sessions WHERE user_id = ? AND token != ?', (user_id, _hash_token(current_token or '')))
        return cur.rowcount

    # ------------------------------------------------------------------ device tokens

    def create_token(self, user_id: int, name: str, scope: str = 'inbound') -> str:
        """A token for a device that syncs data in (the Chrome extension). Shown once, stored hashed."""
        token = 'bl_' + secrets.token_urlsafe(32)
        self._write('INSERT INTO api_tokens (name, token, user_id, scope, created_at) VALUES (?,?,?,?,?)',
                    ((name or 'device').strip()[:60], _hash_token(token), user_id, scope, _iso(_utcnow())))
        self.log('token_created', user_id, name)
        return token

    def validate_token(self, token: str):
        if not token or not token.startswith('bl_'):
            return None
        row = self._conn().execute("""SELECT u.id, u.username, u.email, u.display_name, u.role, t.scope, t.id, u.is_active
                                        FROM api_tokens t JOIN users u ON u.id = t.user_id
                                       WHERE t.token = ? AND t.revoked = 0""", (_hash_token(token),)).fetchone()
        if not row or not row[7]:
            return None
        self._write('UPDATE api_tokens SET last_used_at = ? WHERE id = ?', (_iso(_utcnow()), row[6]))
        user = self.public(row)
        user['scope'] = row[5]
        return user

    def list_tokens(self) -> list:
        rows = self._conn().execute('SELECT id, name, scope, created_at, last_used_at, revoked FROM api_tokens ORDER BY id DESC').fetchall()
        return [dict(zip(('id', 'name', 'scope', 'created_at', 'last_used_at', 'revoked'), r)) for r in rows]

    def revoke_token(self, token_id: int):
        self._write('UPDATE api_tokens SET revoked = 1 WHERE id = ?', (token_id,))

    def cleanup(self):
        self._write('DELETE FROM sessions WHERE expires_at < ?', (_iso(_utcnow()),))
        self._write('DELETE FROM login_failures WHERE at < ?', (time.time() - 86400,))
