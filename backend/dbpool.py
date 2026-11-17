"""Hand a thread's SQLite connection back after each request (Auth.release, Wms.release).

11-11 night: request threads each opened their own connections (threading.local) and never closed them. On
Python 3.12 only the cycle collector frees a sqlite3 connection, so finished threads left theirs open for
minutes, and idle keep-alive threads held theirs for their whole life (live: 253 auth.db handles at 199 threads,
43 at 3). SQLite also keeps a closed connection's database file handle open while any other connection in the
process holds a lock on that file, which WAL mode always does, so those handles never go below the highest
number of connections ever open at once. With release() that number is the requests running at once, plus
POOL_IDLE kept for reuse.
"""
import sqlite3

POOL_IDLE = 8


def release(local, idle: list, lock):
    """Move local.conn to the idle pool, rolled back if a request died mid-write, or close it when the pool is full."""
    conn = getattr(local, 'conn', None)
    if conn is None:
        return
    local.conn = None
    try:
        if conn.in_transaction:
            conn.rollback()
        with lock:
            if len(idle) < POOL_IDLE:
                idle.append(conn)
                return
    except sqlite3.Error:
        pass
    conn.close()
