"""Refused saves, kept so a bug a person hits shows up the same day (Alex 11-16).

Sam made the Bratz Yasmin from the new item card on 11-16 and typed how many; the server refused with "That product is
not in Boxloft". The server logged only slow requests, so the same refusal had been happening since 11-12 with no
trace until she said so. Now every 400 and 409 a signed-in person gets is one JSON line in DATA/refused.jsonl
(2 MB, then one older file), and Overview shows today's count and the newest messages.

A file, not wms.db: a refusal can come from a request whose write was rolled back, and the error path should not touch
the database. Only a few known keys of the body are kept (codes, ids, qty), never passwords or keys.
"""
import config
import json
import os
import threading
from datetime import datetime, timezone

MAX_BYTES = 2 * 1024 * 1024
ET = config.TZ
# Routes whose refusals are not a sign of a bug, or whose bodies hold secrets.
SKIP = ('/api/auth/', '/api/connections', '/api/client-diag', '/api/inbound/')
KEEP = ('code', 'qty', 'product_id', 'item_id', 'box_id', 'location_id', 'line_id', 'line_ids', 'listing_id',
        'replaces', 'over_ok', 'action_id', 'add_id', 'scan_id')
_lock = threading.Lock()


def _path(data_dir: str) -> str:
    return os.path.join(data_dir, 'refused.jsonl')


def _brief(body) -> dict:
    if not isinstance(body, dict):
        return {}
    out = {k: body[k] for k in KEEP if k in body and isinstance(body[k], (str, int, float, bool, list))}
    opt = body.get('option')
    if isinstance(opt, dict) and opt.get('kind'):
        out['option'] = str(opt['kind'])[:20]
    return {k: (v[:80] if isinstance(v, str) else v[:20] if isinstance(v, list) else v) for k, v in out.items()}


def record(data_dir: str, method: str, path: str, status: int, message: str, user: str = '', device: str = '',
           body_bytes: bytes = None) -> None:
    """One line per refusal. Never raises: a full disk must not turn a refusal into a 500."""
    if not user or any(path.startswith(p) for p in SKIP):
        return
    try:
        body = json.loads(body_bytes) if body_bytes else {}
    except (ValueError, UnicodeDecodeError):
        body = {}
    line = json.dumps({'at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'user': user,
                       'device': (device or '')[:40], 'method': method, 'path': path[:120], 'status': status,
                       'message': (message or '')[:300], 'body': _brief(body)})
    p = _path(data_dir)
    try:
        with _lock:
            if os.path.exists(p) and os.path.getsize(p) >= MAX_BYTES:
                os.replace(p, p + '.1')
            with open(p, 'a') as f:
                f.write(line + '\n')
    except OSError:
        pass


def today(data_dir: str, limit: int = 8) -> dict:
    """Today's refusals (ET day): count, conflicts (409: someone else saved first, by design), and the newest lines."""
    day = datetime.now(ET).date()
    rows = []
    try:
        with open(_path(data_dir)) as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                    at = datetime.strptime(r['at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
                except (ValueError, KeyError):
                    continue
                if at.astimezone(ET).date() == day:
                    rows.append(r)
    except OSError:
        pass
    return {'count': len(rows), 'conflicts': sum(1 for r in rows if r.get('status') == 409),
            'recent': rows[::-1][:limit]}
